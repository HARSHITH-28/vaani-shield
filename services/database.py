
import sqlite3
import secrets
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "vaanishield.db"

# Stable, non-sequential identities for the synthetic demo customers.
DEMO_CUSTOMER_KEYS = {
    "arjun@example.com": "demo-arjun",
    "priya@example.com": "demo-priya",
    "rahul@example.com": "demo-rahul",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database():
    conn = get_connection()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_key TEXT,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            phone TEXT,
            monthly_income REAL DEFAULT 0,
            password_hash TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
        account_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        account_number TEXT UNIQUE NOT NULL,
        account_type TEXT NOT NULL,
        balance REAL DEFAULT 0,
        status TEXT DEFAULT 'ACTIVE',
        FOREIGN KEY (customer_id)
            REFERENCES customers(customer_id)
    );

        CREATE TABLE IF NOT EXISTS transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        amount REAL NOT NULL,
        merchant TEXT,
        category TEXT,
        status TEXT DEFAULT 'SUCCESS',
        reference_id TEXT UNIQUE,
        transaction_date DATETIME DEFAULT CURRENT_TIMESTAMP,
        description TEXT,
        FOREIGN KEY (account_id)
            REFERENCES accounts(account_id)
    );

        CREATE TABLE IF NOT EXISTS cards (
        card_id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        card_type TEXT NOT NULL,
        last_four TEXT NOT NULL,
        status TEXT DEFAULT 'ACTIVE',
        expiry_date TEXT,
        FOREIGN KEY (account_id)
            REFERENCES accounts(account_id)
    );

        CREATE TABLE IF NOT EXISTS loans (
        loan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        loan_type TEXT NOT NULL,
        principal_amount REAL NOT NULL,
        interest_rate REAL NOT NULL,
        tenure_months INTEGER NOT NULL,
        outstanding_amount REAL,
        status TEXT DEFAULT 'ACTIVE',
        monthly_emi REAL,
        remaining_tenure_months INTEGER,
        next_emi_date TEXT,
        completion_date TEXT,
        FOREIGN KEY (account_id)
            REFERENCES accounts(account_id)
    );

        CREATE TABLE IF NOT EXISTS support_cases (
        case_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        issue_type TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'OPEN',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id)
            REFERENCES customers(customer_id)
    );

        CREATE TABLE IF NOT EXISTS reminders (
        reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        due_date TEXT,
        status TEXT DEFAULT 'PENDING',
        FOREIGN KEY (customer_id)
            REFERENCES customers(customer_id)
    );

        CREATE TABLE IF NOT EXISTS audit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        action TEXT NOT NULL,
        details TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id)
            REFERENCES customers(customer_id)
    );

        CREATE TABLE IF NOT EXISTS signin_audit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        email_attempted TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        timestamp_ist TEXT NOT NULL,
        login_result TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        device_platform TEXT,
        FOREIGN KEY (customer_id)
            REFERENCES customers(customer_id)
    );

        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived', 'closed'))
            -- Ownership is enforced by the service against the authenticated
            -- stable key. Avoid a parent FK because legacy DB migration adds
            -- the customer_key uniqueness index after this schema script.
        );

        CREATE TABLE IF NOT EXISTS conversation_turns (
            turn_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            input_type TEXT CHECK (input_type IN ('text', 'audio')),
            user_text_or_transcript TEXT,
            input_language TEXT,
            detected_languages_json TEXT,
            is_code_mixed INTEGER NOT NULL DEFAULT 0 CHECK (is_code_mixed IN (0, 1)),
            assistant_response_text TEXT,
            response_language TEXT,
            requested_output_mode TEXT
                CHECK (requested_output_mode IN ('text', 'speech', 'both')),
            client_turn_id TEXT,
            processing_status TEXT NOT NULL DEFAULT 'completed'
                CHECK (processing_status IN ('pending', 'processing', 'completed', 'failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (conversation_id, sequence_number),
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
            ON conversations(customer_key, updated_at DESC, conversation_id DESC);
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_order
            ON conversation_turns(conversation_id, sequence_number);

        -- Scam assessments intentionally store only redacted evidence and
        -- structured findings. Original message text/audio is never persisted.
        CREATE TABLE IF NOT EXISTS scam_assessments (
            assessment_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type = 'typed_message'),
            source_mode TEXT NOT NULL DEFAULT 'typed_message'
                CHECK (source_mode IN ('typed_message', 'voice_call')),
            risk_level TEXT NOT NULL CHECK (risk_level IN ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
            indicators_json TEXT NOT NULL,
            explanation TEXT NOT NULL,
            evidence_strength TEXT NOT NULL CHECK (evidence_strength IN ('NONE', 'LIMITED', 'MODERATE', 'STRONG')),
            warning TEXT NOT NULL,
            recommended_steps_json TEXT NOT NULL,
            detected_languages_json TEXT NOT NULL,
            is_code_mixed INTEGER NOT NULL CHECK (is_code_mixed IN (0, 1)),
            language_status TEXT NOT NULL CHECK (language_status IN ('supported', 'uncertain', 'unsupported')),
            engine_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scam_assessments_owner_created
            ON scam_assessments(customer_key, created_at DESC, assessment_id DESC);

        -- Transaction-risk public references are independent random handles;
        -- existing transaction.reference_id values remain untouched.
        CREATE TABLE IF NOT EXISTS transaction_public_refs (
            transaction_ref TEXT PRIMARY KEY,
            transaction_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_transaction_public_refs_transaction
            ON transaction_public_refs(transaction_id);

        -- Store only the structured engine result and ownership/freshness
        -- metadata. Transaction facts remain in the transactions table.
        CREATE TABLE IF NOT EXISTS transaction_risk_assessments (
            assessment_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL,
            transaction_id INTEGER NOT NULL,
            assessment_json TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            history_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (customer_key, transaction_id, history_fingerprint),
            FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id)
                ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_transaction_risk_assessments_owner_created
            ON transaction_risk_assessments(customer_key, created_at DESC, assessment_id DESC);
        CREATE INDEX IF NOT EXISTS idx_transaction_risk_assessments_transaction
            ON transaction_risk_assessments(transaction_id, created_at DESC);
        """)

        # Serialize schema inspection and migration so two app processes can
        # safely start against the same older database at the same time.
        conn.execute("BEGIN IMMEDIATE")

        # Assistant retry idempotency is additive. Existing persisted turns are
        # preserved; only new requests populate client_turn_id.
        turn_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(conversation_turns)")
        }
        if "client_turn_id" not in turn_columns:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN client_turn_id TEXT")
        if "detected_languages_json" not in turn_columns:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN detected_languages_json TEXT")
        if "is_code_mixed" not in turn_columns:
            conn.execute(
                "ALTER TABLE conversation_turns ADD COLUMN is_code_mixed INTEGER NOT NULL DEFAULT 0"
            )
        if "deleted_at" not in turn_columns:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN deleted_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_turns_visible "
            "ON conversation_turns(conversation_id, deleted_at, sequence_number)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_client_id "
            "ON conversation_turns(conversation_id, client_turn_id) "
            "WHERE role = 'user' AND client_turn_id IS NOT NULL"
        )

        # Customer Support Phase 2: add the ticket contract without rebuilding
        # support_cases or changing its internal integer primary keys. Legacy
        # issue_type values remain untouched; unrecognized values map to the
        # new public category "other" while the original stays available in
        # issue_type for preservation.
        support_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(support_cases)")
        }
        required_legacy_columns = {
            "case_id", "customer_id", "issue_type", "description", "status", "created_at"
        }
        if not required_legacy_columns.issubset(support_columns):
            raise RuntimeError("The existing support_cases schema is not recognized.")

        support_additions = (
            ("ticket_ref", "TEXT"),
            ("category", "TEXT NOT NULL DEFAULT 'other' CHECK (category IN ('account','card','transaction','digital_banking','loan','deposit','other'))"),
            ("subject", "TEXT NOT NULL DEFAULT 'Existing support case'"),
            ("priority", "TEXT NOT NULL DEFAULT 'MEDIUM' CHECK (priority IN ('LOW','MEDIUM','HIGH','URGENT'))"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("origin", "TEXT NOT NULL DEFAULT 'legacy_import' CHECK (origin IN ('customer_form','assistant_confirmed','legacy_import'))"),
            ("is_synthetic_demo", "INTEGER NOT NULL DEFAULT 1 CHECK (is_synthetic_demo IN (0,1))"),
            ("conversation_id", "TEXT"),
            ("language", "TEXT CHECK (language IS NULL OR language IN ('kn-IN','en-IN','hi-IN'))"),
        )
        for column_name, column_definition in support_additions:
            if column_name not in support_columns:
                conn.execute(
                    f"ALTER TABLE support_cases ADD COLUMN {column_name} {column_definition}"
                )

        # Known legacy status spellings can be normalized safely. Unknown
        # states stop the migration transaction instead of being guessed.
        status_map = {
            "open": "OPEN",
            "in_progress": "IN_PROGRESS",
            "in progress": "IN_PROGRESS",
            "waiting_for_customer": "WAITING_FOR_CUSTOMER",
            "waiting for customer": "WAITING_FOR_CUSTOMER",
            "resolved": "RESOLVED",
            "closed": "CLOSED",
        }
        existing_cases = conn.execute(
            "SELECT case_id, issue_type, status, created_at, ticket_ref, category, updated_at "
            "FROM support_cases ORDER BY case_id"
        ).fetchall()
        for existing_case in existing_cases:
            raw_status = existing_case["status"]
            normalized_status = status_map.get(str(raw_status or "").strip().casefold())
            if normalized_status is None:
                raise RuntimeError("An unrecognized legacy support status prevents migration.")
            raw_issue_type = str(existing_case["issue_type"] or "").strip().casefold()
            category_aliases = {
                "account": "account", "card": "card", "transaction": "transaction",
                "digital_banking": "digital_banking", "digital banking": "digital_banking",
                "loan": "loan", "deposit": "deposit", "other": "other",
            }
            category = category_aliases.get(raw_issue_type, "other")
            ticket_ref = existing_case["ticket_ref"]
            # Repair only missing or colliding references, making interrupted
            # or partially applied migrations safe to rerun.
            if not ticket_ref or conn.execute(
                "SELECT 1 FROM support_cases WHERE ticket_ref = ? AND case_id <> ? LIMIT 1",
                (ticket_ref, existing_case["case_id"]),
            ).fetchone():
                while True:
                    ticket_ref = "VS-" + secrets.token_hex(16).upper()
                    if not conn.execute(
                        "SELECT 1 FROM support_cases WHERE ticket_ref = ? LIMIT 1",
                        (ticket_ref,),
                    ).fetchone():
                        break
            conn.execute(
                "UPDATE support_cases SET ticket_ref = ?, category = ?, "
                "status = ?, updated_at = COALESCE(NULLIF(updated_at, ''), created_at) "
                "WHERE case_id = ?",
                (ticket_ref, category, normalized_status, existing_case["case_id"]),
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_support_cases_ticket_ref "
            "ON support_cases(ticket_ref)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_cases_owner_created "
            "ON support_cases(customer_id, created_at DESC, case_id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_cases_conversation "
            "ON support_cases(conversation_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_case_events (
                event_id TEXT PRIMARY KEY,
                case_id INTEGER NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                actor_type TEXT NOT NULL CHECK (actor_type IN ('system','customer')),
                occurred_at TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                -- Normal service operations append only. Cascade deletion is
                -- retained for the repository's explicit full demo-data reset.
                FOREIGN KEY (case_id) REFERENCES support_cases(case_id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_case_events_case_time "
            "ON support_case_events(case_id, occurred_at, event_id)"
        )
        # One stable initial event per legacy ticket; the deterministic ID
        # makes migration reruns idempotent without changing existing cases.
        for existing_case in existing_cases:
            event_id = "legacy-" + str(existing_case["case_id"])
            conn.execute(
                "INSERT OR IGNORE INTO support_case_events "
                "(event_id, case_id, from_status, to_status, actor_type, occurred_at, reason_code) "
                "VALUES (?, ?, NULL, ?, 'system', ?, 'legacy_import')",
                (event_id, existing_case["case_id"], status_map[str(existing_case["status"] or "").strip().casefold()], existing_case["created_at"] or ""),
            )

        # Short-lived, single-use assistant actions. The action payload is
        # validated JSON and is cleared when consumed or expired. Ownership is
        # bound to the authenticated stable key and an owner-checked conversation.
        conn.execute("""
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
            )
        """)
        pending_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pending_actions)")
        }
        if "session_id" not in pending_columns:
            conn.execute("ALTER TABLE pending_actions ADD COLUMN session_id TEXT")
            # Older pending rows cannot be proven to belong to the current
            # authenticated session; invalidate and scrub them conservatively.
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', action_json = '{}', "
                "consumed_at = COALESCE(consumed_at, created_at) "
                "WHERE session_id IS NULL OR session_id = ''"
            )
        if "original_turn_id" not in pending_columns:
            conn.execute("ALTER TABLE pending_actions ADD COLUMN original_turn_id TEXT")
            # Existing proposals cannot be safely tied to the customer turn
            # that produced them, so invalidate and scrub them conservatively.
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', action_json = '{}', "
                "consumed_at = COALESCE(consumed_at, created_at) "
                "WHERE original_turn_id IS NULL OR original_turn_id = ''"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_owner_session_expiry "
            "ON pending_actions(customer_key, session_id, status, expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_conversation "
            "ON pending_actions(conversation_id, customer_key)"
        )

        # Phase 3 adds voice as an assessment source without rebuilding the
        # existing table or rewriting prior assessment rows.
        scam_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(scam_assessments)")
        }
        if "source_mode" not in scam_columns:
            conn.execute(
                "ALTER TABLE scam_assessments ADD COLUMN source_mode TEXT NOT NULL "
                "DEFAULT 'typed_message' CHECK (source_mode IN ('typed_message', 'voice_call'))"
            )

        # Additive migration for databases created before customer_key existed.
        customer_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(customers)")
        }
        if "customer_key" not in customer_columns:
            conn.execute("ALTER TABLE customers ADD COLUMN customer_key TEXT")

        for email, customer_key in DEMO_CUSTOMER_KEYS.items():
            conn.execute(
                """
                UPDATE customers
                SET customer_key = ?
                WHERE LOWER(email) = LOWER(?)
                  AND (customer_key IS NULL OR TRIM(customer_key) = '')
                """,
                (customer_key, email),
            )

        # Give any non-demo legacy customers a persistent opaque key too.
        legacy_customers = conn.execute(
            """
            SELECT customer_id
            FROM customers
            WHERE customer_key IS NULL OR TRIM(customer_key) = ''
            """
        ).fetchall()
        for customer in legacy_customers:
            conn.execute(
                "UPDATE customers SET customer_key = ? WHERE customer_id = ?",
                (f"customer-{uuid.uuid4().hex}", customer["customer_id"]),
            )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_customers_customer_key
            ON customers(customer_key)
            """
        )

        # Scam Investigation: additive owner-scoped tracking and evidence links.
        # Existing assessment rows are referenced by public UUIDs and are never
        # copied into these tables. Internal integer keys stay service-only.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investigations (
                investigation_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investigation_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                investigation_id INTEGER NOT NULL,
                evidence_type TEXT NOT NULL CHECK (evidence_type IN (
                    'MESSAGE_ASSESSMENT','CALL_ASSESSMENT','TRANSACTION_RISK_ASSESSMENT'
                )),
                assessment_id TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                UNIQUE (investigation_id, evidence_type, assessment_id),
                FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id)
                    ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investigation_events (
                event_id TEXT PRIMARY KEY,
                investigation_id INTEGER NOT NULL,
                customer_key TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'CREATED','EVIDENCE_LINKED','INFORMATION_ADDED',
                    'STATUS_CHANGED','CLOSED'
                )),
                summary TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id)
                    ON DELETE CASCADE
            )
        """)
        conn.execute("""
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
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigations_owner_updated "
            "ON investigations(customer_key, updated_at DESC, investigation_id DESC)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_investigations_public_ref "
            "ON investigations(investigation_ref)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigation_evidence_parent "
            "ON investigation_evidence(investigation_id, linked_at, evidence_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigation_events_parent "
            "ON investigation_events(investigation_id, occurred_at, event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigation_events_owner "
            "ON investigation_events(customer_key, occurred_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigation_pending_owner_expiry "
            "ON investigation_pending_actions(customer_key, session_id, status, expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigation_pending_conversation "
            "ON investigation_pending_actions(conversation_id, customer_key)"
        )
        # Synthetic account-opening applications. Additive only: no existing
        # table is altered and no existing row is read or rewritten.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS account_applications (
                application_id INTEGER PRIMARY KEY AUTOINCREMENT,
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

            CREATE TABLE IF NOT EXISTS account_application_documents (
                document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                customer_key TEXT NOT NULL,
                document_type TEXT NOT NULL CHECK (
                    document_type IN ('AADHAAR_DEMO', 'PAN_DEMO', 'ADDRESS_PROOF_DEMO')
                ),
                storage_id TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE (application_id, document_type),
                FOREIGN KEY (application_id) REFERENCES account_applications(application_id)
            );

            CREATE TABLE IF NOT EXISTS account_application_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                customer_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (application_id) REFERENCES account_applications(application_id)
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_applications_owner "
            "ON account_applications(customer_key, created_at DESC, application_id DESC)"
        )
        # At most one unfinished application per customer, enforced by SQLite.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_applications_one_active "
            "ON account_applications(customer_key) "
            "WHERE status IN ('DRAFT', 'PENDING_CONFIRMATION')"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_application_documents_owner "
            "ON account_application_documents(customer_key, application_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_application_events_parent "
            "ON account_application_events(application_id, occurred_at, event_id)"
        )

        # Support Ticket Drafts: Persistent structured draft state across multi-turn voice/chat flows.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_ticket_drafts (
                draft_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                customer_key TEXT NOT NULL,
                category TEXT,
                priority TEXT,
                subject TEXT,
                description TEXT,
                amount REAL,
                debit_confirmed INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'collecting'
                    CHECK (status IN ('collecting', 'proposed', 'submitted', 'cancelled')),
                confirmation_id TEXT,
                result_ticket_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, customer_key)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_support_ticket_drafts_owner "
            "ON support_ticket_drafts(customer_key, updated_at DESC)"
        )

        # Notification Center: Customer-isolated event alerts and reminders.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                customer_key TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('loan_emi', 'banking_alert', 'support_update', 'security_alert')),
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                event_ref TEXT,
                is_read INTEGER DEFAULT 0 CHECK (is_read IN (0, 1)),
                created_at TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_event_unique "
            "ON notifications(customer_key, event_ref) WHERE event_ref IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_owner_created "
            "ON notifications(customer_key, created_at DESC, notification_id DESC)"
        )

        # Uploaded Attachments: Customer-isolated document and photo uploads with extracted text.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uploaded_attachments (
                attachment_id TEXT PRIMARY KEY,
                customer_key TEXT NOT NULL,
                conversation_id TEXT,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                extracted_text TEXT NOT NULL,
                summary_metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_owner "
            "ON uploaded_attachments(customer_key, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploaded_attachments_conversation "
            "ON uploaded_attachments(conversation_id, customer_key)"
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("VaaniShield database initialized successfully!")


if __name__ == "__main__":
    initialize_database()
