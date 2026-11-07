"""Offline tests for owner-scoped transaction-risk retrieval."""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from services.transaction_risk_engine import MAX_HISTORY_SNAPSHOTS
from services.transaction_risk_service import (
    HISTORY_ROW_LIMIT,
    HISTORY_WINDOW_DAYS,
    assess_customer_transaction,
)


BASE = datetime(2026, 10, 31, 12, 0, 0)


class TransactionRiskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "risk-test.sqlite3"
        self._create_schema()
        self._seed_accounts()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def db(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_schema(self):
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE accounts (
                    account_id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL
                );
                CREATE TABLE transactions (
                    transaction_id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    transaction_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    merchant TEXT,
                    category TEXT,
                    status TEXT,
                    reference_id TEXT,
                    transaction_date TEXT,
                    description TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );
            """)

    def _seed_accounts(self):
        with self.db() as conn:
            conn.executemany(
                "INSERT INTO accounts(account_id, customer_id) VALUES (?, ?)",
                [(10, 1), (11, 1), (20, 2)],
            )

    def add_tx(
        self, txn_id, *, account=10, customer=None, date=None, amount=100,
        merchant="Familiar Market", status="SUCCESS", kind="DEBIT", ref=None,
        description="ignored description",
    ):
        if customer is not None:
            with self.db() as conn:
                conn.execute("INSERT OR IGNORE INTO accounts(account_id, customer_id) VALUES (?, ?)", (account, customer))
        stamp = date.isoformat() if isinstance(date, datetime) else date
        with self.db() as conn:
            conn.execute(
                """INSERT INTO transactions
                   (transaction_id, account_id, transaction_type, amount, merchant,
                    category, status, reference_id, transaction_date, description)
                   VALUES (?, ?, ?, ?, ?, 'SHOPPING', ?, ?, ?, ?)""",
                (txn_id, account, kind, amount, merchant, status, ref, stamp, description),
            )

    def add_baseline(self, *, customer=1, account=10, count=12):
        for index in range(count):
            days_ago = max(1, 30 - (index * 30 // max(count, 1)))
            self.add_tx(
                1000 + index,
                account=account,
                customer=customer if account not in (10, 20) else None,
                date=BASE - timedelta(days=days_ago),
            )

    def assess(self, customer_id=1, transaction_id=1):
        return assess_customer_transaction(
            customer_id, transaction_id, connection_factory=self.connect
        )

    def test_customer_can_assess_owned_transaction(self):
        self.add_baseline()
        self.add_tx(1, date=BASE, ref="account-number-derived-secret")
        result = self.assess()
        self.assertIsNotNone(result)
        self.assertEqual(result["transaction"]["amount"], 100)
        self.assertEqual(result["assessment"]["risk_level"], "LOW")
        self.assertNotIn("transaction_id", str(result))
        self.assertNotIn("reference_id", str(result))
        self.assertNotIn("account-number-derived-secret", str(result))

    def test_customer_cannot_assess_another_customers_transaction(self):
        self.add_tx(90, account=20, customer=2, date=BASE)
        self.assertIsNone(self.assess(customer_id=1, transaction_id=90))

    def test_unknown_transaction_returns_none(self):
        self.assertIsNone(self.assess(transaction_id=404))

    def test_multiple_accounts_use_only_selected_accounts_history(self):
        self.add_baseline()
        for index in range(12):
            self.add_tx(2000 + index, account=11, date=BASE - timedelta(days=20-index), merchant="Other Account Merchant")
        self.add_tx(1, account=10, date=BASE, merchant="Familiar Market")
        result = self.assess()
        self.assertEqual(result["assessment"]["history_sufficiency"]["matching_history_count"], 12)
        self.assertNotIn("Other Account Merchant", str(result))

    def test_history_is_customer_and_account_scoped(self):
        self.add_baseline()
        for index in range(5):
            self.add_tx(3000 + index, account=20, customer=2, date=BASE - timedelta(days=15-index), merchant="Other Customer Merchant")
        self.add_tx(1, date=BASE)
        result = self.assess()
        self.assertEqual(result["assessment"]["history_sufficiency"]["matching_history_count"], 12)
        self.assertNotIn("Other Customer Merchant", str(result))

    def test_empty_history_returns_unknown(self):
        self.add_tx(1, date=BASE)
        result = self.assess()
        self.assertEqual(result["assessment"]["risk_level"], "UNKNOWN")
        self.assertEqual(result["assessment"]["history_sufficiency"]["matching_history_count"], 0)

    def test_insufficient_history_is_reported_without_failure(self):
        self.add_baseline(count=3)
        self.add_tx(1, date=BASE)
        result = self.assess()
        self.assertEqual(result["assessment"]["risk_level"], "UNKNOWN")
        self.assertEqual(result["assessment"]["history_sufficiency"]["level"], "LIMITED")

    def test_history_is_bounded_and_deterministically_ordered(self):
        # More rows than the engine's maximum; the newest matching rows are selected.
        for index in range(MAX_HISTORY_SNAPSHOTS + 25):
            self.add_tx(5000 + index, date=BASE - timedelta(days=index % (HISTORY_WINDOW_DAYS - 1) + 1))
        self.add_tx(1, date=BASE)
        first = self.assess()
        second = self.assess()
        count = first["assessment"]["history_sufficiency"]["matching_history_count"]
        self.assertLessEqual(count, HISTORY_ROW_LIMIT)
        self.assertEqual(first, second)

    def test_history_window_excludes_old_rows(self):
        self.add_baseline()
        self.add_tx(4000, date=BASE - timedelta(days=HISTORY_WINDOW_DAYS + 1))
        self.add_tx(1, date=BASE)
        result = self.assess()
        self.assertEqual(result["assessment"]["history_sufficiency"]["matching_history_count"], 12)

    def test_missing_and_malformed_timestamps_do_not_crash(self):
        self.add_tx(10, date=None)
        self.add_tx(11, date="not-a-timestamp")
        self.add_tx(1, date="also-not-a-timestamp")
        result = self.assess()
        self.assertEqual(result["assessment"]["risk_level"], "UNKNOWN")
        self.assertEqual(result["transaction"]["transaction_date"], "also-not-a-timestamp")

    def test_projection_maps_exact_engine_snapshot_fields_and_preserves_assessment(self):
        self.add_baseline()
        self.add_tx(1, date=BASE, amount=10_000, merchant="New Merchant", ref="sensitive-ref")
        result = self.assess()
        assessment = result["assessment"]
        self.assertEqual(assessment["risk_level"], "MEDIUM")
        self.assertEqual(assessment["risk_score"], 35)
        self.assertEqual(assessment["engine_version"], "1.0.0")
        self.assertIn("unusual_amount", {item["indicator_code"] for item in assessment["indicators"]})
        self.assertEqual(set(result["transaction"]), {
            "transaction_type", "amount", "merchant", "category", "status", "transaction_date"
        })

    def test_status_type_and_description_do_not_get_lost_or_used_as_signal(self):
        self.add_baseline()
        self.add_tx(1, date=BASE, kind="CREDIT", status="PENDING", description="high risk urgent scam")
        result = self.assess()
        self.assertEqual(result["transaction"]["transaction_type"], "CREDIT")
        self.assertEqual(result["transaction"]["status"], "PENDING")
        self.assertEqual(result["assessment"]["risk_level"], "UNKNOWN")

    def test_invalid_internal_identifiers_are_rejected(self):
        for customer_id, transaction_id in ((True, 1), (1, False), (0, 1), (1, -1)):
            with self.assertRaises(ValueError):
                self.assess(customer_id, transaction_id)


if __name__ == "__main__":
    unittest.main()
