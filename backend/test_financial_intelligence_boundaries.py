"""Isolated amount-boundary regression tests for financial queries."""

import sqlite3
import unittest
from unittest.mock import patch

from services.financial_intelligence import find_large_transactions


class LargeTransactionBoundaryTests(unittest.TestCase):
    def test_above_threshold_is_strict_at_currency_cent_boundaries(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, customer_id INTEGER);
            CREATE TABLE transactions (
                transaction_id INTEGER PRIMARY KEY, account_id INTEGER,
                amount REAL, transaction_type TEXT, merchant TEXT, category TEXT,
                transaction_date TEXT, reference_id TEXT, status TEXT
            );
            INSERT INTO accounts VALUES (1, 7);
            INSERT INTO transactions VALUES
                (1, 1, 1999.99, 'DEBIT', 'A', 'SHOPPING', '2026-10-01', 'r1', 'SUCCESS'),
                (2, 1, 2000.00, 'DEBIT', 'B', 'SHOPPING', '2026-10-02', 'r2', 'SUCCESS'),
                (3, 1, 2000.01, 'DEBIT', 'C', 'SHOPPING', '2026-10-03', 'r3', 'SUCCESS');
        """)
        try:
            with patch("services.financial_intelligence.get_connection", return_value=connection):
                results = find_large_transactions(7, 2000)
            self.assertEqual([row["amount"] for row in results], [2000.01])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
