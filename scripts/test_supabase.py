"""Supabase PostgreSQL Connectivity and Diagnostic Tool for VaaniShield.

Usage:
    python scripts/test_supabase.py [--init]

Options:
    --init    Automatically initialize the VaaniShield schema and seed demo data if empty.

Examples:
    python scripts/test_supabase.py
    python scripts/test_supabase.py --init
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

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
    get_supabase_url,
    initialize_supabase_database,
    is_supabase_configured,
    seed_supabase_demo_data,
)


def mask_db_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.password:
            masked = parsed._replace(netloc=f"{parsed.username}:********@{parsed.hostname}:{parsed.port}")
            return masked.geturl()
        return url
    except Exception:
        return "postgresql://...:********@..."


def test_supabase(do_init: bool = False) -> bool:
    print("=" * 65)
    print("           VaaniShield Supabase PostgreSQL Diagnostic")
    print("=" * 65)

    if not is_supabase_configured():
        print("❌ CONFIGURATION ERROR: Supabase PostgreSQL URL is not set in .env")
        print("\nRequired Environment Variable in .env:")
        print("   SUPABASE_DB_URL=postgresql://postgres.[project-ref]:[db-password]@aws-0-[region].pooler.supabase.com:6543/postgres?sslmode=require")
        print("   (or direct connection: postgresql://postgres:[db-password]@db.[project-ref].supabase.co:5432/postgres?sslmode=require)")
        print("\nHow to obtain your Supabase PostgreSQL connection string:")
        print(" 1. Log in to your Supabase Dashboard: https://supabase.com/dashboard")
        print(" 2. Open your Project -> Click on 'Project Settings' (gear icon on sidebar).")
        print(" 3. Navigate to 'Database' -> Scroll down to 'Connection string'.")
        print(" 4. Select 'URI' (or 'Transaction Pooler') and copy the URI.")
        print(" 5. Replace [YOUR-PASSWORD] with your actual Supabase database password.")
        print(" 6. Add it to your .env file as SUPABASE_DB_URL=...")
        return False

    raw_url = get_supabase_url()
    masked_url = mask_db_url(raw_url)
    print(f"Connecting to: {masked_url}")

    start_time = time.perf_counter()
    try:
        conn = get_supabase_connection()
    except Exception as exc:
        print(f"\n❌ Connection Failed: {exc}")
        print("\nTroubleshooting tips:")
        print(" - Verify your database password in the connection string.")
        print(" - If using Transaction Pooler, ensure port is 6543.")
        print(" - If using Direct Connection, ensure port is 5432 and SSL is enabled (sslmode=require).")
        return False

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    try:
        cur = conn._conn.cursor()
        cur.execute("SELECT version(), current_database(), current_user;")
        row = cur.fetchone()
        pg_version = row[0].split()[0] + " " + row[0].split()[1]
        db_name = row[1]
        db_user = row[2]

        print(f"\n✅ Connection Established! (Latency: {elapsed_ms:.1f}ms)")
        print(f"   PostgreSQL Version: {pg_version}")
        print(f"   Database Name:      {db_name}")
        print(f"   Connected As:       {db_user}")

        # Check existing tables
        cur.execute(
            """
            SELECT COUNT(*) 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
              AND table_name IN ('customers', 'accounts', 'transactions', 'conversations', 'scam_assessments');
            """
        )
        core_tables = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) 
            FROM information_schema.tables 
            WHERE table_schema = 'public';
            """
        )
        total_tables = cur.fetchone()[0]

        print(f"\n📊 Schema Inspection:")
        print(f"   Total Public Tables: {total_tables}")
        print(f"   VaaniShield Core Tables: {core_tables}/5 detected")

        if core_tables < 5:
            print("\n⚠️  VaaniShield schema is not fully initialized yet in Supabase.")
            if do_init or "--init" in sys.argv:
                print("   [+] Initializing VaaniShield schema and demo data...")
                initialize_supabase_database(conn)
                seed_supabase_demo_data(conn)
                print("   ✅ Schema initialized and demo data seeded successfully!")
            else:
                print("   Tip: Run `python scripts/test_supabase.py --init` to automatically create all tables and demo data.")
        else:
            cur.execute("SELECT COUNT(*) FROM customers;")
            cust_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM accounts;")
            acct_count = cur.fetchone()[0]
            print(f"   Registered Customers: {cust_count}")
            print(f"   Bank Accounts:        {acct_count}")

        conn.close()
        print("\n🎉 Supabase PostgreSQL is fully functional and ready for VaaniShield!")
        return True

    except Exception as exc:
        print(f"\n❌ Diagnostic query failed: {exc}")
        conn.close()
        return False


if __name__ == "__main__":
    do_init_flag = "--init" in sys.argv
    success = test_supabase(do_init_flag)
    sys.exit(0 if success else 1)
