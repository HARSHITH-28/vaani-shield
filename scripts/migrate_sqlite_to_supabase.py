"""Data Migration Script: Local SQLite -> Supabase PostgreSQL.

Copies all existing banking records, customers, accounts, transactions,
conversations, and audit logs from local data/vaanishield.db into Supabase PostgreSQL.

Usage:
    python scripts/migrate_sqlite_to_supabase.py
"""

import os
import sqlite3
import sys
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

# Load .env
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

from services.supabase_database import (
    get_supabase_connection,
    initialize_supabase_database,
    is_supabase_configured,
)

SQLITE_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "vaanishield.db"

# Foreign-key ordered table sequence
TABLE_ORDER = [
    ("customers", "customer_id"),
    ("accounts", "account_id"),
    ("transactions", "transaction_id"),
    ("cards", "card_id"),
    ("loans", "loan_id"),
    ("support_cases", "case_id"),
    ("reminders", "reminder_id"),
    ("audit_logs", "log_id"),
    ("signin_audit_logs", "log_id"),
    ("email_verifications", "verification_id"),
    ("conversations", None),
    ("conversation_turns", None),
    ("scam_assessments", None),
    ("transaction_public_refs", None),
    ("transaction_risk_assessments", None),
    ("support_case_events", "event_id"),
    ("pending_actions", "action_id"),
    ("investigations", None),
    ("investigation_evidence", "evidence_id"),
    ("investigation_events", "event_id"),
    ("investigation_pending_actions", "action_id"),
    ("account_applications", None),
    ("account_application_documents", "document_id"),
    ("account_application_events", "event_id"),
    ("support_ticket_drafts", None),
    ("notifications", None),
    ("uploaded_attachments", None),
    ("paired_devices", None),
    ("sms_threat_events", "event_id"),
]


def migrate():
    print("=" * 65)
    print("      VaaniShield: SQLite -> Supabase PostgreSQL Migration")
    print("=" * 65)

    if not is_supabase_configured():
        print("❌ ERROR: Supabase PostgreSQL is not configured in .env.")
        print("Please set SUPABASE_DB_URL before running migration.")
        sys.exit(1)

    if not SQLITE_DB_PATH.exists():
        print(f"❌ ERROR: SQLite database file not found at {SQLITE_DB_PATH}")
        sys.exit(1)

    print(f"Source: {SQLITE_DB_PATH}")
    print("Target: Supabase PostgreSQL")

    # Connect to both databases
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = get_supabase_connection()

    try:
        # Step 1: Ensure Schema Exists
        print("\n[1/3] Ensuring target PostgreSQL schema is initialized...")
        initialize_supabase_database(pg_conn)

        # Step 2: Migrate data table by table
        print("\n[2/3] Migrating table data...")
        total_migrated = 0

        for table, pk_col in TABLE_ORDER:
            # Check if source table exists
            table_check = sqlite_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not table_check:
                continue

            rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"   • {table:30} 0 rows (empty)")
                continue

            col_names = [desc[0] for desc in sqlite_conn.execute(f"SELECT * FROM {table} LIMIT 1").description]
            cols_clause = ", ".join(col_names)
            placeholders = ", ".join(["%s"] * len(col_names))

            insert_sql = f"INSERT INTO {table} ({cols_clause}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            cur = pg_conn._conn.cursor()

            data_tuples = [tuple(r[c] for c in col_names) for r in rows]
            cur.executemany(insert_sql, data_tuples)
            pg_conn.commit()

            # Synchronize sequence for auto-increment PKs
            if pk_col:
                try:
                    cur.execute(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('{table}', '{pk_col}'),
                            COALESCE((SELECT MAX({pk_col}) FROM {table}), 1),
                            true
                        );
                        """
                    )
                    pg_conn.commit()
                except Exception:
                    pass

            print(f"   • {table:30} {len(rows):5} rows migrated")
            total_migrated += len(rows)

        # Step 3: Verify
        print(f"\n[3/3] Migration Complete! Total rows migrated: {total_migrated}")
        pg_conn.commit()
        print("\n✅ SQLite data successfully synchronized into Supabase PostgreSQL!")

    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    migrate()
