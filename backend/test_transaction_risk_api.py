"""Offline transaction-risk API, migration, and persistence tests."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from services import database
from services.principal import CustomerPrincipal
from services.transaction_risk_persistence import ensure_transaction_public_refs


BASE = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


class TransactionRiskApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "transaction-risk.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        self._seed()
        self.old_overrides = main.app.dependency_overrides.copy()
        self.current_principal = principal()
        main.app.dependency_overrides[auth.get_authenticated_principal] = lambda: self.current_principal
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _seed():
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers(customer_id, customer_key, name, email) VALUES (?, ?, ?, ?)",
                [(1, "demo-arjun", "Arjun Demo", "arjun@example.test"),
                 (2, "demo-priya", "Priya Demo", "priya@example.test")],
            )
            conn.executemany(
                "INSERT INTO accounts(account_id, customer_id, account_number, account_type) VALUES (?, ?, ?, ?)",
                [(10, 1, "1111222233334444", "SAVINGS"), (20, 2, "5555666677778888", "SAVINGS")],
            )
            for index in range(12):
                days_ago = max(1, 30 - (index * 30 // 12))
                conn.execute(
                    """INSERT INTO transactions
                       (transaction_id, account_id, transaction_type, amount, merchant,
                        category, status, reference_id, transaction_date, description)
                       VALUES (?, 10, 'DEBIT', 100, 'Familiar Market', 'SHOPPING',
                               'SUCCESS', ?, ?, 'ordinary purchase')""",
                    (1000 + index, f"legacy-account-ref-{index}", (BASE - timedelta(days=days_ago)).isoformat()),
                )
            for index in range(3):
                conn.execute(
                    """INSERT INTO transactions
                       (transaction_id, account_id, transaction_type, amount, merchant,
                        category, status, reference_id, transaction_date, description)
                       VALUES (?, 10, 'DEBIT', ?, 'Familiar Market', 'SHOPPING',
                               'SUCCESS', ?, ?, 'selected purchase')""",
                    (index + 1, 120 + index, f"selected-account-ref-{index}", (BASE + timedelta(days=index)).isoformat()),
                )
            conn.execute(
                """INSERT INTO transactions
                   (transaction_id, account_id, transaction_type, amount, merchant,
                    category, status, reference_id, transaction_date)
                   VALUES (50, 20, 'DEBIT', 80, 'Priya Shop', 'SHOPPING', 'SUCCESS',
                           'priya-account-ref', ?)""",
                (BASE.isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

    def _ref(self, transaction_id, key="demo-arjun"):
        return ensure_transaction_public_refs(key, [transaction_id])[transaction_id]

    def test_schema_creation_and_migration_idempotency_preserve_existing_data(self):
        conn = database.get_connection()
        try:
            conn.execute(
                """INSERT INTO support_cases(customer_id, issue_type, description, status, created_at)
                   VALUES (1, 'account', 'legacy synthetic support case', 'OPEN', '2026-01-01T00:00:00+00:00')"""
            )
            conn.commit()
        finally:
            conn.close()
        database.initialize_database()
        database.initialize_database()
        conn = database.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 16)
            for table in ("transaction_public_refs", "transaction_risk_assessments"):
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone())
            scam_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='scam_assessments'"
            ).fetchone()[0]
            self.assertIn("CHECK (source_type = 'typed_message')", scam_sql)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_public_references_are_random_unique_stable_and_owner_scoped(self):
        arjun = ensure_transaction_public_refs("demo-arjun", [1, 2, 3])
        priya = ensure_transaction_public_refs("demo-priya", [50])
        self.assertEqual(len(set(arjun.values())), 3)
        self.assertNotEqual(arjun[1], priya[50])
        self.assertRegex(arjun[1], r"^TR-[A-F0-9]{48}$")
        self.assertEqual(self._ref(1), arjun[1])
        self.assertNotEqual(arjun[1], "TR-" + "1".zfill(48))

    def test_self_service_transactions_add_public_ref_without_changing_legacy_ref(self):
        response = self.client.get("/api/v1/me/transactions?limit=3")
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertTrue(all(row["transaction_ref"].startswith("TR-") for row in rows))
        self.assertTrue(all("reference_id" in row for row in rows))
        serialized = json.dumps(rows)
        self.assertNotIn("transaction_id", serialized)
        self.assertNotIn("account_id", serialized)
        self.assertNotIn("customer_id", serialized)

    def test_assessment_creation_persists_structured_result_without_internal_ids(self):
        reference = self._ref(1)
        response = self.client.post(
            "/api/v1/me/scam/assessments/transactions", json={"transaction_ref": reference}
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["transaction"]["transaction_ref"], reference)
        self.assertIn(body["assessment"]["risk_level"], {"LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"})
        serialized = json.dumps(body)
        for forbidden in ("transaction_id", "account_id", "customer_id", "reference_id", "selected-account-ref"):
            self.assertNotIn(forbidden, serialized)
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM transaction_risk_assessments WHERE assessment_id = ?",
                (body["assessment_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("selected-account-ref", json.dumps(dict(row)))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transaction_risk_assessments").fetchone()[0], 1)
        finally:
            conn.close()

    def test_identical_request_reuses_result_and_changed_history_creates_fresh_result(self):
        reference = self._ref(1)
        path = "/api/v1/me/scam/assessments/transactions"
        first = self.client.post(path, json={"transaction_ref": reference})
        second = self.client.post(path, json={"transaction_ref": reference})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["assessment_id"], second.json()["assessment_id"])
        self.assertTrue(second.json()["reused"])
        conn = database.get_connection()
        try:
            conn.execute(
                """INSERT INTO transactions(account_id, transaction_type, amount, merchant,
                   category, status, reference_id, transaction_date)
                   VALUES (10, 'DEBIT', 900, 'New Store', 'SHOPPING', 'SUCCESS', ?, ?)""",
                ("new-history-ref", (BASE - timedelta(hours=2)).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        third = self.client.post(path, json={"transaction_ref": reference})
        self.assertEqual(third.status_code, 201)
        self.assertNotEqual(third.json()["assessment_id"], first.json()["assessment_id"])

    def test_history_pagination_and_assessment_detail_are_owner_scoped(self):
        refs = [self._ref(tx_id) for tx_id in (1, 2, 3)]
        ids = []
        for reference in refs:
            response = self.client.post(
                "/api/v1/me/scam/assessments/transactions", json={"transaction_ref": reference}
            )
            self.assertEqual(response.status_code, 201, response.text)
            ids.append(response.json()["assessment_id"])
        first = self.client.get("/api/v1/me/scam/transaction-assessments?limit=2")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(len(first.json()["items"]), 2)
        self.assertTrue(first.json()["has_more"])
        second = self.client.get(
            "/api/v1/me/scam/transaction-assessments",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        self.assertEqual(len(second.json()["items"]), 1)
        detail = self.client.get(f"/api/v1/me/scam/transaction-assessments/{ids[0]}")
        self.assertEqual(detail.status_code, 200)
        detail_body = detail.json()
        self.assertEqual(detail_body["transaction"]["merchant"], "Familiar Market")
        self.assertEqual(detail_body["transaction"]["amount"], 120)
        self.assertNotIn("transaction_id", json.dumps(detail_body))
        self.assertNotIn("reference_id", json.dumps(detail_body))
        self.assertEqual(first.json()["items"][0]["transaction"]["merchant"], "Familiar Market")
        self.assertNotIn("account_id", json.dumps(first.json()))
        self.current_principal = principal("demo-priya")
        self.assertEqual(
            self.client.get(f"/api/v1/me/scam/transaction-assessments/{ids[0]}").status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/v1/me/scam/transaction-assessments").json()["items"], [])

    def test_unknown_and_other_customer_refs_have_equivalent_404(self):
        other_customer_ref = self._ref(50, "demo-priya")
        unknown_ref = "TR-" + "A" * 48
        path = "/api/v1/me/scam/assessments/transactions"
        other = self.client.post(path, json={"transaction_ref": other_customer_ref})
        missing = self.client.post(path, json={"transaction_ref": unknown_ref})
        self.assertEqual(other.status_code, 404)
        self.assertEqual(other.json(), missing.json())

    def test_invalid_and_oversized_inputs_are_rejected(self):
        path = "/api/v1/me/scam/assessments/transactions"
        self.assertEqual(self.client.post(path, json={"transaction_ref": "TR-short"}).status_code, 422)
        self.assertEqual(self.client.post(path, json={"transaction_ref": "TR-" + "A" * 100}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/me/scam/transaction-assessments?limit=101").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/me/scam/transaction-assessments?cursor=%%%bad").status_code, 400)

    def test_unauthenticated_assessment_is_rejected(self):
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        response = self.client.post(
            "/api/v1/me/scam/assessments/transactions",
            json={"transaction_ref": self._ref(1)},
        )
        self.assertEqual(response.status_code, 401)

    def test_foreign_keys_prevent_deleting_assessed_transactions_and_scam_routes_remain(self):
        response = self.client.post(
            "/api/v1/me/scam/assessments/transactions",
            json={"transaction_ref": self._ref(1)},
        )
        self.assertEqual(response.status_code, 201)
        conn = database.get_connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM transactions WHERE transaction_id = 1")
            conn.rollback()
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scam_assessments'"
            ).fetchone())
        finally:
            conn.close()
        scam = self.client.post(
            "/api/v1/me/scam/assessments/messages",
            json={"message_text": "A normal greeting", "language_hint": "en-IN"},
        )
        self.assertEqual(scam.status_code, 201, scam.text)


if __name__ == "__main__":
    unittest.main()
