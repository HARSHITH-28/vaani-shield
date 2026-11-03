
import argparse
import random
from datetime import date, timedelta

from services.database import get_connection, initialize_database


# Reproducible synthetic data
RANDOM_SEED = 42

# Synthetic history: August 1 to October 31, 2026
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 10, 31)


CUSTOMERS = [
    {
        "name": "Arjun Sharma",
        "email": "arjun@example.com",
        "phone": "9000000001",
        "income": 75000,
        "opening_balance": 100000,
        "account_number": "VS10000001",
    },
    {
        "name": "Priya Nair",
        "email": "priya@example.com",
        "phone": "9000000002",
        "income": 95000,
        "opening_balance": 180000,
        "account_number": "VS10000002",
    },
    {
        "name": "Rahul Verma",
        "email": "rahul@example.com",
        "phone": "9000000003",
        "income": 55000,
        "opening_balance": 50000,
        "account_number": "VS10000003",
    },
]


MERCHANTS = [
    ("Swiggy", "FOOD", 250, 900),
    ("Zomato", "FOOD", 200, 800),
    ("Amazon", "SHOPPING", 500, 5000),
    ("Flipkart", "SHOPPING", 400, 4500),
    ("Uber", "TRANSPORT", 100, 700),
    ("Ola", "TRANSPORT", 100, 600),
    ("Netflix", "SUBSCRIPTION", 649, 649),
    ("Spotify", "SUBSCRIPTION", 119, 119),
    ("BigBasket", "GROCERIES", 500, 2500),
    ("DMart", "GROCERIES", 700, 3000),
    ("Electricity Board", "UTILITIES", 800, 2500),
    ("Mobile Recharge", "UTILITIES", 299, 999),
    ("Apollo Pharmacy", "HEALTH", 200, 1500),
    ("Hospital", "HEALTH", 500, 4000),
    ("Petrol Pump", "FUEL", 500, 2500),
]


def reset_synthetic_data(cursor):
    """Delete existing synthetic records in foreign-key-safe order."""

    tables = [
        "audit_logs",
        "reminders",
        "support_cases",
        "transactions",
        "cards",
        "loans",
        "accounts",
        "customers",
    ]

    for table in tables:
        cursor.execute(f"DELETE FROM {table}")


def random_transaction_date(rng):
    """Return a random date within the synthetic history."""

    days = (END_DATE - START_DATE).days
    chosen = START_DATE + timedelta(days=rng.randint(0, days))

    hour = rng.randint(8, 22)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)

    return (
        f"{chosen.isoformat()} "
        f"{hour:02d}:{minute:02d}:{second:02d}"
    )


def insert_transactions(cursor, account_id, monthly_income, rng):
    """Insert salary credits and realistic debit transactions."""

    transactions = []
    serial = 1

    current_month = date(2026, 8, 1)

    # Generate monthly salary credits.
    while current_month <= END_DATE:

        salary_date = f"{current_month.isoformat()} 09:00:00"

        transactions.append({
            "date": salary_date,
            "type": "CREDIT",
            "amount": monthly_income,
            "merchant": "Salary Credit",
            "category": "INCOME",
            "description": "Monthly salary credit",
        })

        # Advance to next month.
        if current_month.month == 12:
            current_month = date(
                current_month.year + 1, 1, 1
            )
        else:
            current_month = date(
                current_month.year,
                current_month.month + 1,
                1
            )

    # Generate 36 debit transactions per account.
    for _ in range(36):

        merchant, category, minimum, maximum = rng.choice(MERCHANTS)

        amount = rng.randrange(
            minimum,
            maximum + 1,
            10
        )

        transactions.append({
            "date": random_transaction_date(rng),
            "type": "DEBIT",
            "amount": amount,
            "merchant": merchant,
            "category": category,
            "description": f"{category} payment at {merchant}",
        })

    # Sort transactions chronologically.
    transactions.sort(key=lambda item: item["date"])

    for item in transactions:

        reference_id = f"VS-TXN-{account_id}-{serial:03}"

        cursor.execute("""
            INSERT INTO transactions (
                account_id,
                transaction_type,
                amount,
                merchant,
                category,
                status,
                reference_id,
                transaction_date,
                description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_id,
            item["type"],
            item["amount"],
            item["merchant"],
            item["category"],
            "SUCCESS",
            reference_id,
            item["date"],
            item["description"],
        ))

        serial += 1


def seed_database(reset=False):

    initialize_database()

    rng = random.Random(RANDOM_SEED)

    conn = get_connection()
    cursor = conn.cursor()

    try:

        customer_count = cursor.execute(
            "SELECT COUNT(*) FROM customers"
        ).fetchone()[0]

        if customer_count > 0:

            if not reset:
                print("Existing data found. No changes made.")
                print("Use --reset to replace synthetic data.")
                return

            reset_synthetic_data(cursor)

        # Store dynamically generated account IDs.
        account_ids = []

        # Insert customers and accounts.
        for customer in CUSTOMERS:

            cursor.execute("""
                INSERT INTO customers (
                    name, email, phone, monthly_income
                )
                VALUES (?, ?, ?, ?)
            """, (
                customer["name"],
                customer["email"],
                customer["phone"],
                customer["income"],
            ))

            customer_id = cursor.lastrowid

            cursor.execute("""
                INSERT INTO accounts (
                    customer_id,
                    account_number,
                    account_type,
                    balance,
                    status
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                customer_id,
                customer["account_number"],
                "SAVINGS",
                customer["opening_balance"],
                "ACTIVE",
            ))

            # Get the actual SQLite-generated account ID.
            account_id = cursor.lastrowid
            account_ids.append(account_id)

            # Generate transactions for this account.
            insert_transactions(
                cursor,
                account_id,
                customer["income"],
                rng,
            )

            # Reconcile account balance.
            totals = cursor.execute("""
                SELECT
                    SUM(
                        CASE
                            WHEN transaction_type = 'CREDIT'
                            AND status = 'SUCCESS'
                            THEN amount ELSE 0
                        END
                    ) AS credits,

                    SUM(
                        CASE
                            WHEN transaction_type = 'DEBIT'
                            AND status = 'SUCCESS'
                            THEN amount ELSE 0
                        END
                    ) AS debits

                FROM transactions
                WHERE account_id = ?
            """, (account_id,)).fetchone()

            closing_balance = (
                customer["opening_balance"]
                + (totals["credits"] or 0)
                - (totals["debits"] or 0)
            )

            cursor.execute("""
                UPDATE accounts
                SET balance = ?
                WHERE account_id = ?
            """, (closing_balance, account_id))

        # Insert cards using dynamically generated account IDs.
        cards = [
            (account_ids[0], "DEBIT", "1234", "12/29"),
            (account_ids[0], "CREDIT", "5678", "08/28"),
            (account_ids[1], "DEBIT", "9012", "05/30"),
            (account_ids[2], "DEBIT", "3456", "11/29"),
        ]

        cursor.executemany("""
            INSERT INTO cards (
                account_id,
                card_type,
                last_four,
                expiry_date
            )
            VALUES (?, ?, ?, ?)
        """, cards)

        # Insert loans using dynamically generated account IDs.
        loans = [
            (account_ids[0], "EDUCATION", 500000, 9.5, 60, 420000),
            (account_ids[1], "HOME", 2500000, 8.5, 240, 2300000),
            (account_ids[2], "PERSONAL", 200000, 12.0, 36, 150000),
        ]

        cursor.executemany("""
            INSERT INTO loans (
                account_id,
                loan_type,
                principal_amount,
                interest_rate,
                tenure_months,
                outstanding_amount
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, loans)

        conn.commit()

        print("\nSynthetic banking data created successfully!")
        print("--------------------------------------------")
        print("Customers:       3")
        print("Accounts:        3")
        print("Transactions:    117")
        print("Cards:           4")
        print("Loans:           3")
        print("History:         August - October 2026")
        print("Balances:        Reconciled")
        print("Account IDs:    ", account_ids)
        print("--------------------------------------------")

    except Exception as e:

        conn.rollback()
        print(f"Database seeding failed: {e}")
        raise

    finally:
        conn.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Replace existing synthetic banking data"
    )

    args = parser.parse_args()

    seed_database(reset=args.reset)
