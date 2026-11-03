
from services.database import get_connection

conn = get_connection()
cursor = conn.cursor()

tables = [
    "customers",
    "accounts",
    "transactions",
    "cards",
    "loans",
    "support_cases",
    "reminders",
    "audit_logs"
]

print("\n===== VAANISHIELD DATABASE REPORT =====")

for table in tables:
    count = cursor.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]
    print(f"{table:<20}: {count}")

print("\n===== CUSTOMER ACCOUNTS =====")

rows = cursor.execute("""
    SELECT c.name, a.account_number,
           a.account_type, a.balance
    FROM customers c
    JOIN accounts a
    ON c.customer_id = a.customer_id
""").fetchall()

for row in rows:
    print(
        f"{row['name']} | {row['account_number']} | "
        f"{row['account_type']} | ₹{row['balance']:,.2f}"
    )

conn.close()
print("\nDatabase verification completed!")
