"""Align Supabase PostgreSQL schema to match services/database.py exactly."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import psycopg2

db_url = os.environ.get("SUPABASE_DB_URL")
if not db_url:
    print("Error: SUPABASE_DB_URL not found")
    sys.exit(1)

conn = psycopg2.connect(db_url)
conn.autocommit = False
cur = conn.cursor()

try:
    print("Dropping misaligned empty tables if they exist...")
    drop_sql = """
    DROP TABLE IF EXISTS sms_threat_events CASCADE;
    DROP TABLE IF EXISTS paired_devices CASCADE;
    DROP TABLE IF EXISTS uploaded_attachments CASCADE;
    DROP TABLE IF EXISTS support_ticket_drafts CASCADE;
    DROP TABLE IF EXISTS account_application_events CASCADE;
    DROP TABLE IF EXISTS account_application_documents CASCADE;
    DROP TABLE IF EXISTS account_applications CASCADE;
    DROP TABLE IF EXISTS investigation_pending_actions CASCADE;
    DROP TABLE IF EXISTS investigation_events CASCADE;
    DROP TABLE IF EXISTS investigation_evidence CASCADE;
    DROP TABLE IF EXISTS investigations CASCADE;
    DROP TABLE IF EXISTS pending_actions CASCADE;
    DROP TABLE IF EXISTS support_case_events CASCADE;
    """
    cur.execute(drop_sql)

    print("Creating aligned tables...")
    create_sql = """
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
    cur.execute(create_sql)
    conn.commit()
    print("Schema successfully aligned and committed!")
except Exception as e:
    conn.rollback()
    print("Failed to align schema:", e)
    sys.exit(1)
finally:
    conn.close()

