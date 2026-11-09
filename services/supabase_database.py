"""Supabase PostgreSQL adapter and connection manager for VaaniShield banking.

Provides:
- Thread-safe connection pooling to Supabase PostgreSQL via psycopg2.
- Automatic SQLite-to-PostgreSQL SQL query translation (? -> %s, INSERT OR IGNORE -> ON CONFLICT DO NOTHING).
- Case-preserving, indexable dict-like row wrappers (PostgresRow) matching sqlite3.Row semantics.
- Automatic sequence primary key retrieval for cursor.lastrowid on INSERT statements.
- Schema auto-initialization and demo data seeding for fresh Supabase projects.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# Global connection pool
_supabase_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_supabase_url() -> str:
    """Return configured Supabase / PostgreSQL database connection URL."""
    return os.getenv(
        "SUPABASE_DB_URL",
        os.getenv("DATABASE_URL", os.getenv("POSTGRES_URL", "")),
    ).strip()


def is_supabase_configured() -> bool:
    """Check whether a Supabase PostgreSQL connection string is configured."""
    url = get_supabase_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


class PostgresRow(dict):
    """Dict-like row compatible with sqlite3.Row supporting row['col'], row[0], and dict(row)."""

    def __init__(self, data_dict: dict[str, Any], cols: Sequence[str]):
        super().__init__(data_dict)
        self._cols = list(cols)
        self._lower_map = {k.lower(): k for k in data_dict.keys()}

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            col_name = self._cols[key]
            return super().__getitem__(col_name)
        if key in self:
            return super().__getitem__(key)
        lower_k = str(key).lower()
        if lower_k in self._lower_map:
            return super().__getitem__(self._lower_map[lower_k])
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def keys(self):
        return super().keys()

    def values(self):
        return super().values()

    def items(self):
        return super().items()


def _convert_query(sql: str) -> tuple[str, bool]:
    """Convert SQLite syntax to PostgreSQL syntax.
    
    1. Replaces '?' placeholders with '%s' outside of quotes, escapes literal '%' to '%%'.
    2. Converts 'INSERT OR IGNORE INTO' to 'INSERT INTO ... ON CONFLICT DO NOTHING'.
    3. Handles SQLite 'PRAGMA' (including table_info) and 'BEGIN IMMEDIATE' commands.
    4. Translates SQLite strftime() to PostgreSQL TO_CHAR().
    5. Detects if RETURNING should be added for cursor.lastrowid.
    """
    clean_sql = sql.strip()

    # Handle PRAGMA table_info(table_name)
    pragma_info_match = re.match(
        r"^\s*PRAGMA\s+table_info\s*\(\s*['\"]?(\w+)['\"]?\s*\)",
        clean_sql,
        re.IGNORECASE,
    )
    if pragma_info_match:
        table_name = pragma_info_match.group(1).lower()
        return (
            f"SELECT column_name AS name FROM information_schema.columns WHERE table_name = '{table_name}'",
            False,
        )

    # Ignore other SQLite pragma statements
    if clean_sql.upper().startswith("PRAGMA "):
        return "-- PRAGMA ignored in Postgres\nSELECT 1", False

    # Convert transaction begin
    if clean_sql.upper().startswith("BEGIN IMMEDIATE") or clean_sql.upper().startswith("BEGIN EXCLUSIVE"):
        return "BEGIN", False

    # Handle INSERT OR IGNORE
    is_insert_ignore = bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", clean_sql, re.IGNORECASE))
    if is_insert_ignore:
        clean_sql = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", clean_sql, flags=re.IGNORECASE)
        if "on conflict" not in clean_sql.lower():
            clean_sql = clean_sql.rstrip(" ;") + " ON CONFLICT DO NOTHING"

    # Translate SQLite strftime patterns to Postgres TO_CHAR
    clean_sql = re.sub(
        r"strftime\s*\(\s*['\"]%Y-%m-%d %H:%M:%f['\"]\s*,\s*['\"]now['\"]\s*\)",
        "TO_CHAR(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS.US')",
        clean_sql,
        flags=re.IGNORECASE,
    )
    clean_sql = re.sub(
        r"strftime\s*\(\s*['\"]%Y['\"]\s*,\s*([^)]+)\)",
        r"TO_CHAR((\1)::timestamp, 'YYYY')",
        clean_sql,
        flags=re.IGNORECASE,
    )
    clean_sql = re.sub(
        r"strftime\s*\(\s*['\"]%m['\"]\s*,\s*([^)]+)\)",
        r"TO_CHAR((\1)::timestamp, 'MM')",
        clean_sql,
        flags=re.IGNORECASE,
    )

    # Handle SQLite DDL compatibility (AUTOINCREMENT, DATETIME)
    clean_sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "SERIAL PRIMARY KEY",
        clean_sql,
        flags=re.IGNORECASE,
    )
    clean_sql = re.sub(r"\bAUTOINCREMENT\b", "", clean_sql, flags=re.IGNORECASE)
    clean_sql = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", clean_sql, flags=re.IGNORECASE)

    # Translate parameter placeholders '?' -> '%s' and escape '%' -> '%%'
    out = []
    in_quote = False
    quote_char = ""
    i = 0
    n = len(clean_sql)
    while i < n:
        char = clean_sql[i]
        if in_quote:
            if char == "%":
                out.append("%%")
            else:
                out.append(char)
            if char == quote_char:
                if i + 1 < n and clean_sql[i + 1] == quote_char:
                    out.append(clean_sql[i + 1])
                    i += 1
                else:
                    in_quote = False
        else:
            if char in ("'", '"'):
                in_quote = True
                quote_char = char
                out.append(char)
            elif char == "?":
                out.append("%s")
            elif char == "%":
                out.append("%%")
            else:
                out.append(char)
        i += 1

    converted_sql = "".join(out)

    # Check if INSERT without RETURNING
    is_insert = bool(re.match(r"^\s*INSERT\s+INTO\s+", converted_sql, re.IGNORECASE))
    has_returning = "returning" in converted_sql.lower()
    append_returning = is_insert and not has_returning and "on conflict do nothing" not in converted_sql.lower()

    if append_returning:
        converted_sql = converted_sql.rstrip(" ;") + " RETURNING *"

    return converted_sql, append_returning


class SupabaseCursorWrapper:
    """Cursor wrapper delivering sqlite3.Row-like access and query translation."""

    def __init__(self, raw_cursor):
        self._cursor = raw_cursor
        self.lastrowid: int | None = None
        self._last_row: PostgresRow | None = None

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> SupabaseCursorWrapper:
        converted_sql, appended_returning = _convert_query(sql)
        params_tuple = tuple(params) if params is not None else None

        try:
            self._cursor.execute(converted_sql, params_tuple)
        except Exception as exc:
            logger.error("Supabase SQL error: %s | Query: %s", exc, converted_sql[:250])
            raise

        if appended_returning:
            try:
                row = self._cursor.fetchone()
                if row:
                    cols = [desc[0] for desc in self._cursor.description]
                    self._last_row = PostgresRow(dict(zip(cols, row)), cols)
                    # Try to extract the primary key
                    for key in cols:
                        if key.endswith("_id") or key == "id":
                            val = self._last_row[key]
                            if isinstance(val, int):
                                self.lastrowid = val
                                break
            except Exception:
                pass

        return self

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> SupabaseCursorWrapper:
        converted_sql, _ = _convert_query(sql)
        self._cursor.executemany(converted_sql, seq_of_parameters)
        return self

    def fetchone(self) -> PostgresRow | None:
        if self._last_row is not None:
            ret = self._last_row
            self._last_row = None
            return ret
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in self._cursor.description]
        return PostgresRow(dict(zip(cols, row)), cols)

    def fetchall(self) -> list[PostgresRow]:
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = [desc[0] for desc in self._cursor.description]
        return [PostgresRow(dict(zip(cols, r)), cols) for r in rows]

    def fetchmany(self, size: int = 1) -> list[PostgresRow]:
        rows = self._cursor.fetchmany(size)
        if not rows:
            return []
        cols = [desc[0] for desc in self._cursor.description]
        return [PostgresRow(dict(zip(cols, r)), cols) for r in rows]

    def close(self):
        self._cursor.close()

    def __iter__(self) -> Iterator[PostgresRow]:
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class SupabaseConnectionWrapper:
    """Connection wrapper delivering sqlite3 connection ergonomics over psycopg2."""

    def __init__(self, raw_conn, from_pool: bool = False):
        self._conn = raw_conn
        self._from_pool = from_pool
        self._closed = False

    def cursor(self) -> SupabaseCursorWrapper:
        return SupabaseCursorWrapper(self._conn.cursor())

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> SupabaseCursorWrapper:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> SupabaseCursorWrapper:
        cur = self.cursor()
        cur.executemany(sql, seq_of_parameters)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            global _supabase_pool
            if self._from_pool and _supabase_pool is not None:
                try:
                    _supabase_pool.putconn(self._conn)
                except Exception:
                    self._conn.close()
            else:
                self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_supabase_connection() -> SupabaseConnectionWrapper:
    """Obtain a wrapped connection to Supabase PostgreSQL."""
    global _supabase_pool
    db_url = get_supabase_url()
    if not db_url:
        raise RuntimeError("Supabase database connection URL is not configured.")

    if _supabase_pool is None:
        try:
            # Create connection pool
            _supabase_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=20,
                dsn=db_url,
                connect_timeout=10,
            )
        except Exception as exc:
            logger.error("Failed to initialize Supabase connection pool: %s", exc)
            # Direct connection fallback
            raw = psycopg2.connect(db_url, connect_timeout=10)
            return SupabaseConnectionWrapper(raw, from_pool=False)

    try:
        raw_conn = _supabase_pool.getconn()
        return SupabaseConnectionWrapper(raw_conn, from_pool=True)
    except Exception as exc:
        logger.warning("Supabase pool getconn failed, falling back to direct connection: %s", exc)
        raw = psycopg2.connect(db_url, connect_timeout=10)
        return SupabaseConnectionWrapper(raw, from_pool=False)


SUPABASE_SCHEMA_SQL = """
-- VaaniShield Banking & Financial Security Schema for Supabase PostgreSQL

CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGSERIAL PRIMARY KEY,
    customer_key TEXT UNIQUE,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT,
    monthly_income NUMERIC DEFAULT 0,
    password_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    account_number TEXT UNIQUE NOT NULL,
    account_type TEXT NOT NULL,
    balance NUMERIC DEFAULT 0,
    status TEXT DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    transaction_type TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    merchant TEXT,
    category TEXT,
    status TEXT DEFAULT 'SUCCESS',
    reference_id TEXT UNIQUE,
    transaction_date TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    card_id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    card_type TEXT NOT NULL,
    last_four TEXT NOT NULL,
    status TEXT DEFAULT 'ACTIVE',
    expiry_date TEXT
);

CREATE TABLE IF NOT EXISTS loans (
    loan_id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    loan_type TEXT NOT NULL,
    principal_amount NUMERIC NOT NULL,
    interest_rate NUMERIC NOT NULL,
    tenure_months INT NOT NULL,
    outstanding_amount NUMERIC,
    status TEXT DEFAULT 'ACTIVE',
    monthly_emi NUMERIC,
    remaining_tenure_months INT,
    next_emi_date TEXT,
    completion_date TEXT
);

CREATE TABLE IF NOT EXISTS support_cases (
    case_id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    issue_type TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'OPEN',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    ticket_ref TEXT,
    category TEXT NOT NULL DEFAULT 'other',
    subject TEXT NOT NULL DEFAULT 'Support Case',
    priority TEXT NOT NULL DEFAULT 'MEDIUM',
    updated_at TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'customer_form',
    is_synthetic_demo INT NOT NULL DEFAULT 0,
    conversation_id TEXT,
    language TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_date TEXT,
    status TEXT DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(customer_id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signin_audit_logs (
    log_id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(customer_id) ON DELETE SET NULL,
    email_attempted TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    timestamp_ist TEXT NOT NULL,
    login_result TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    device_platform TEXT
);

CREATE TABLE IF NOT EXISTS email_verifications (
    verification_id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    otp_code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_verified INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_supabase_email_verif
    ON email_verifications(email, expires_at);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'active',
    context_summary_json TEXT,
    context_summary_through_sequence INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    sequence_number INT NOT NULL,
    role TEXT NOT NULL,
    input_type TEXT,
    user_text_or_transcript TEXT,
    input_language TEXT,
    detected_languages_json TEXT,
    is_code_mixed INT NOT NULL DEFAULT 0,
    assistant_response_text TEXT,
    response_language TEXT,
    requested_output_mode TEXT,
    client_turn_id TEXT,
    processing_status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT,
    UNIQUE (conversation_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_supabase_conv_turns_vis
    ON conversation_turns(conversation_id, deleted_at, sequence_number);

CREATE TABLE IF NOT EXISTS scam_assessments (
    assessment_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_mode TEXT NOT NULL DEFAULT 'typed_message',
    risk_level TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    evidence_strength TEXT NOT NULL,
    warning TEXT NOT NULL,
    recommended_steps_json TEXT NOT NULL,
    detected_languages_json TEXT NOT NULL,
    is_code_mixed INT NOT NULL,
    language_status TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    redacted_transcript TEXT,
    transcript_expires_at TEXT,
    transcript_status TEXT NOT NULL DEFAULT 'NOT_STORED'
);

CREATE TABLE IF NOT EXISTS transaction_public_refs (
    transaction_ref TEXT PRIMARY KEY,
    transaction_id BIGINT NOT NULL UNIQUE REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_risk_assessments (
    assessment_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    transaction_id BIGINT NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    assessment_json TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    history_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (customer_key, transaction_id, history_fingerprint)
);

CREATE TABLE IF NOT EXISTS support_case_events (
    event_id TEXT PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES support_cases(case_id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('system','customer')),
    occurred_at TEXT NOT NULL,
    reason_code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_support_case_events_case_time 
    ON support_case_events(case_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS pending_actions (
    confirmation_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    original_turn_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type = 'create_support_ticket'),
    action_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','executed','declined','expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    result_ticket_ref TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
    FOREIGN KEY (original_turn_id) REFERENCES conversation_turns(turn_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_owner_session_expiry 
    ON pending_actions(customer_key, session_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_actions_conversation 
    ON pending_actions(conversation_id, customer_key);

CREATE TABLE IF NOT EXISTS investigations (
    investigation_id BIGSERIAL PRIMARY KEY,
    investigation_ref TEXT NOT NULL UNIQUE,
    customer_key TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'MESSAGE_SCAM','CALL_SCAM','TRANSACTION_RISK',
        'ACCOUNT_SECURITY','OTHER'
    )),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN (
        'OPEN','UNDER_REVIEW','WAITING_FOR_INFORMATION','RESOLVED','CLOSED'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_investigations_owner_updated 
    ON investigations(customer_key, updated_at DESC, investigation_id DESC);

CREATE TABLE IF NOT EXISTS investigation_evidence (
    evidence_id BIGSERIAL PRIMARY KEY,
    investigation_id BIGINT NOT NULL REFERENCES investigations(investigation_id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN (
        'MESSAGE_ASSESSMENT','CALL_ASSESSMENT','TRANSACTION_RISK_ASSESSMENT'
    )),
    assessment_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    UNIQUE (investigation_id, evidence_type, assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_investigation_evidence_parent 
    ON investigation_evidence(investigation_id, linked_at, evidence_id);

CREATE TABLE IF NOT EXISTS investigation_events (
    event_id TEXT PRIMARY KEY,
    investigation_id BIGINT NOT NULL REFERENCES investigations(investigation_id) ON DELETE CASCADE,
    customer_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'CREATED','EVIDENCE_LINKED','INFORMATION_ADDED',
        'STATUS_CHANGED','CLOSED'
    )),
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_investigation_events_parent 
    ON investigation_events(investigation_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS idx_investigation_events_owner 
    ON investigation_events(customer_key, occurred_at);

CREATE TABLE IF NOT EXISTS investigation_pending_actions (
    confirmation_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    original_turn_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN (
        'create_investigation','link_investigation_evidence',
        'add_investigation_note','change_investigation_status',
        'close_investigation'
    )),
    action_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','executed','declined','expired'
    )),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    result_investigation_ref TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
    FOREIGN KEY (original_turn_id) REFERENCES conversation_turns(turn_id)
);
CREATE INDEX IF NOT EXISTS idx_investigation_pending_owner_expiry 
    ON investigation_pending_actions(customer_key, session_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_investigation_pending_conversation 
    ON investigation_pending_actions(conversation_id, customer_key);

CREATE TABLE IF NOT EXISTS account_applications (
    application_id BIGSERIAL PRIMARY KEY,
    application_ref TEXT NOT NULL UNIQUE,
    customer_key TEXT NOT NULL,
    account_type TEXT CHECK (
        account_type IS NULL
        OR account_type IN ('SAVINGS', 'SALARY', 'CURRENT')
    ),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (
        status IN ('DRAFT', 'PENDING_CONFIRMATION', 'SUBMITTED')
    ),
    full_name TEXT NOT NULL,
    date_of_birth TEXT,
    phone TEXT,
    email TEXT,
    address_line TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    employment_category TEXT,
    nominee_name TEXT,
    nominee_relationship TEXT,
    review_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    review_prepared_at TEXT,
    submitted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_applications_owner 
    ON account_applications(customer_key, created_at DESC, application_id DESC);

CREATE TABLE IF NOT EXISTS account_application_documents (
    document_id BIGSERIAL PRIMARY KEY,
    application_id BIGINT NOT NULL REFERENCES account_applications(application_id) ON DELETE CASCADE,
    customer_key TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK (
        document_type IN ('AADHAAR_DEMO', 'PAN_DEMO', 'ADDRESS_PROOF_DEMO')
    ),
    storage_id TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    content_sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE (application_id, document_type)
);
CREATE INDEX IF NOT EXISTS idx_account_application_documents_owner 
    ON account_application_documents(customer_key, application_id);

CREATE TABLE IF NOT EXISTS account_application_events (
    event_id BIGSERIAL PRIMARY KEY,
    application_id BIGINT NOT NULL REFERENCES account_applications(application_id) ON DELETE CASCADE,
    customer_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_application_events_parent 
    ON account_application_events(application_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS support_ticket_drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    customer_key TEXT NOT NULL,
    category TEXT,
    priority TEXT,
    subject TEXT,
    description TEXT,
    amount REAL,
    debit_confirmed INT DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'collecting'
        CHECK (status IN ('collecting', 'proposed', 'submitted', 'cancelled')),
    confirmation_id TEXT,
    result_ticket_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, customer_key)
);
CREATE INDEX IF NOT EXISTS idx_support_ticket_drafts_owner 
    ON support_ticket_drafts(customer_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    event_ref TEXT,
    is_read INT DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS uploaded_attachments (
    attachment_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL,
    conversation_id TEXT,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    extracted_text TEXT NOT NULL,
    summary_metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_owner 
    ON uploaded_attachments(customer_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_conversation 
    ON uploaded_attachments(conversation_id, customer_key);

CREATE TABLE IF NOT EXISTS paired_devices (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE,
    customer_key TEXT NOT NULL,
    device_name TEXT NOT NULL,
    device_model TEXT,
    android_version TEXT,
    app_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'PAUSED', 'REVOKED')),
    pairing_code_hash TEXT UNIQUE,
    pairing_code_expires_at TEXT,
    token_hash TEXT UNIQUE,
    created_at TEXT NOT NULL,
    paired_at TEXT,
    last_seen_at TEXT,
    revoked_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paired_devices_customer_status 
    ON paired_devices(customer_key, status, created_at DESC);

CREATE TABLE IF NOT EXISTS sms_threat_events (
    id BIGSERIAL PRIMARY KEY,
    sms_event_id TEXT NOT NULL UNIQUE,
    customer_key TEXT NOT NULL,
    device_id TEXT NOT NULL,
    dedup_fingerprint TEXT NOT NULL,
    source_package TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_type TEXT NOT NULL CHECK (
        sender_type IN (
            'dlt_header', 'phone_number', 'phone', 'alphanumeric',
            'short_code', 'shortcode', 'contact', 'unknown'
        )
    ),
    redacted_message TEXT NOT NULL,
    message_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (
        risk_level IN ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    ),
    evidence_strength TEXT NOT NULL CHECK (
        evidence_strength IN ('NONE', 'LIMITED', 'MODERATE', 'STRONG')
    ),
    indicators_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    warning TEXT NOT NULL,
    recommendations_json TEXT NOT NULL,
    detected_languages_json TEXT NOT NULL,
    code_mixed INT NOT NULL DEFAULT 0 CHECK (code_mixed IN (0, 1)),
    language_status TEXT NOT NULL CHECK (
        language_status IN ('supported', 'uncertain', 'unsupported')
    ),
    engine_version TEXT NOT NULL,
    alert_generated INT NOT NULL DEFAULT 0 CHECK (alert_generated IN (0, 1)),
    acknowledged INT NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    acknowledged_at TEXT,
    UNIQUE (customer_key, dedup_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_sms_threat_events_customer_received 
    ON sms_threat_events(customer_key, received_at DESC, id DESC);
"""


def initialize_supabase_database(conn: SupabaseConnectionWrapper) -> None:
    """Ensure all tables, constraints, and indexes exist in Supabase PostgreSQL."""
    try:
        cur = conn._conn.cursor()
        cur.execute(SUPABASE_SCHEMA_SQL)
        cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS metadata_json TEXT DEFAULT '{}';")
        conn.commit()
        logger.info("Supabase PostgreSQL schema initialized successfully.")
    except Exception as exc:
        conn.rollback()
        logger.error("Failed to initialize Supabase PostgreSQL schema: %s", exc)
        raise


def seed_supabase_demo_data(conn: SupabaseConnectionWrapper) -> None:
    """Seed demo accounts if Supabase database is newly created."""
    from backend.auth import hash_password

    # Check if demo data exists
    existing = conn.execute("SELECT COUNT(*) as count FROM customers").fetchone()
    if existing and existing["count"] > 0:
        return

    logger.info("Seeding initial demo customers into Supabase PostgreSQL...")

    demo_users = [
        ("demo-arjun", "Arjun Sharma", "arjun@example.com", "9000000001", 75000.0, "Arjun@Demo2026", "VS10000001", 124500.50, "SALARY"),
        ("demo-priya", "Priya Nair", "priya@example.com", "9000000002", 95000.0, "Priya@Demo2026", "VS10000002", 180000.00, "CURRENT"),
        ("demo-rahul", "Rahul Verma", "rahul@example.com", "9000000003", 55000.0, "Rahul@Demo2026", "VS10000003", 50000.00, "SAVINGS"),
    ]

    for key, name, email, phone, income, pwd, acct_num, balance, acct_type in demo_users:
        hashed = hash_password(pwd)
        cur = conn.execute(
            """
            INSERT INTO customers (customer_key, name, email, phone, monthly_income, password_hash)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO NOTHING
            RETURNING customer_id
            """,
            (key, name, email, phone, income, hashed),
        )
        row = cur.fetchone()
        if row:
            cust_id = row["customer_id"]
            cur_acct = conn.execute(
                """
                INSERT INTO accounts (customer_id, account_number, account_type, balance, status)
                VALUES (%s, %s, %s, %s, 'ACTIVE')
                ON CONFLICT (account_number) DO NOTHING
                RETURNING account_id
                """,
                (cust_id, acct_num, acct_type, balance),
            )
            acct_row = cur_acct.fetchone()
            if acct_row:
                acct_id = acct_row["account_id"]
                conn.execute(
                    """
                    INSERT INTO cards (account_id, card_type, last_four, status, expiry_date)
                    VALUES (%s, 'DEBIT', '4242', 'ACTIVE', '12/29')
                    """,
                    (acct_id,),
                )

    conn.commit()
    logger.info("Supabase PostgreSQL demo data seeded successfully.")

