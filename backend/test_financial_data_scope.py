"""Financial aggregation contracts using a disposable SQLite database."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import database
from services.financial_intelligence import (
    get_monthly_transaction_summary,
    get_spending_summary,
    get_top_merchants,
)


class FinancialDataScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp.name) / "scope.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_key, name, email) VALUES ('demo-arjun','Demo','demo@example.com')"
        )
        customer_id = conn.execute("SELECT customer_id FROM customers WHERE customer_key='demo-arjun'").fetchone()[0]
        conn.execute(
            "INSERT INTO accounts (customer_id, account_number, account_type, balance) VALUES (?, 'masked-test','SAVINGS', 1000)",
            (customer_id,),
        )
        account_id = conn.execute("SELECT account_id FROM accounts WHERE customer_id=?", (customer_id,)).fetchone()[0]
        rows = (
            ("CREDIT", 2000, "Employer", "INCOME", "SUCCESS", "2026-10-02"),
            ("DEBIT", 250, "Market", "FOOD", "SUCCESS", "2026-10-03"),
            ("DEBIT", 100, "Market", "FOOD", "FAILED", "2026-10-04"),
            ("DEBIT", 500, "Power", "UTILITIES", "SUCCESS", "2026-09-03"),
            ("CREDIT", 75, "Refund", "REFUND", "SUCCESS", "2026-09-04"),
        )
        conn.executemany(
            "INSERT INTO transactions (account_id, transaction_type, amount, merchant, category, status, reference_id, transaction_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(account_id, *row[:4], row[4], f"r{index}", row[5]) for index, row in enumerate(rows)],
        )
        conn.commit()
        conn.close()
        self.customer_id = customer_id

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_monthly_counts_and_totals_have_explicit_calendar_success_scope(self):
        result = get_monthly_transaction_summary(self.customer_id, 2026, 10)
        self.assertEqual(result, {
            "year": 2026, "month": 10, "scope": "calendar_month",
            "transaction_count": 3, "successful_count": 2, "non_successful_count": 1,
            "successful_credits": 2000.0, "successful_debits": 250.0,
        })

    def test_all_time_summary_and_merchant_totals_use_successful_records(self):
        summary = get_spending_summary(self.customer_id)
        merchants = get_top_merchants(self.customer_id)
        self.assertEqual(summary["total_spent"], 750.0)
        self.assertEqual(summary["total_credited"], 2075.0)
        self.assertEqual(merchants[0]["merchant"], "Power")
        self.assertEqual(merchants[1]["merchant"], "Market")
        self.assertEqual(merchants[1]["total_spent"], 250.0)
        # The failure row is counted only by the explicitly scoped monthly
        # record count; it never contributes to successful money totals.
        self.assertEqual(merchants[1]["transaction_count"], 1)


if __name__ == "__main__":
    unittest.main()
