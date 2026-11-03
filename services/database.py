
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "vaanishield.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database():
    conn = get_connection()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        phone TEXT,
        monthly_income REAL DEFAULT 0,
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
    """)

    conn.commit()
    conn.close()

    print("VaaniShield database initialized successfully!")


if __name__ == "__main__":
    initialize_database()
