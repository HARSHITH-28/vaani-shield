"""Offline persistence/API coverage using an isolated temporary SQLite file."""

import json
import os
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


def _principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


class ScamAssessmentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "scam-tests.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        self._insert_customers()
        self.old_overrides = main.app.dependency_overrides.copy()
        self.principal = _principal("demo-arjun")
        main.app.dependency_overrides[auth.get_authenticated_principal] = lambda: self.principal
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _insert_customers():
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [("demo-arjun", "arjun@example.test", "Arjun"),
                 ("demo-priya", "priya@example.test", "Priya")],
            )
            conn.commit()
        finally:
            conn.close()

    def test_unauthenticated_is_rejected_before_route_execution(self):
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        response = self.client.post(
            "/api/v1/me/scam/assessments/messages", json={"message_text": "Hello"}
        )
        self.assertEqual(response.status_code, 401)

    def test_demo_cookie_authenticates_assessment_route(self):
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        with patch.dict(os.environ, {"VAANISHIELD_DEMO_SESSION_SECRET": "phase2-test-secret-value-32-bytes-minimum"}):
            login = self.client.post(
                "/api/v1/auth/demo/session", json={"customer_key": "demo-arjun"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            result = self.client.post(
                "/api/v1/me/scam/assessments/messages",
                json={"message_text": "Bank says share your OTP 123456."},
            )
        self.assertEqual(result.status_code, 201, result.text)

    def test_create_detail_history_and_redaction(self):
        text = "Hello Arjun, bank support says share your OTP 654321 now. Call me at 9876543210."
        created = self.client.post(
            "/api/v1/me/scam/assessments/messages",
            json={"message_text": text, "language_hint": "en-IN"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()
        self.assertIn(result["risk_level"], {"HIGH", "CRITICAL"})
        self.assertTrue(result["assessment_id"])
        self.assertNotIn("message_text", result)
        self.assertNotIn("customer_key", result)
        serialized = json.dumps(result)
        self.assertNotIn("654321", serialized)
        self.assertNotIn("9876543210", serialized)
        self.assertNotIn("Arjun", serialized)

        detail = self.client.get(
            f"/api/v1/me/scam/assessments/{result['assessment_id']}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["assessment_id"], result["assessment_id"])
        page = self.client.get("/api/v1/me/scam/assessments?limit=1")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.json()["items"]), 1)

        conn = database.get_connection()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(scam_assessments)")}
            self.assertNotIn("message_text", columns)
            self.assertNotIn("transcript", columns)
            stored = conn.execute(
                "SELECT * FROM scam_assessments WHERE assessment_id = ?",
                (result["assessment_id"],),
            ).fetchone()
            stored_text = json.dumps(dict(stored))
            self.assertNotIn(text, stored_text)
            self.assertNotIn("654321", stored_text)
        finally:
            conn.close()

    def test_consecutive_assessments_are_independent_high_low_high_low(self):
        messages = (
            "Your bank account will be blocked today. Share your OTP immediately to complete KYC.",
            "Your grocery order has been delivered. Thank you for shopping.",
            "Your KYC will expire today. Visit http://secure-bank-verify.example to prevent account suspension.",
            "Your friend has shared the photos from yesterday.",
        )
        results = []
        for message in messages:
            response = self.client.post(
                "/api/v1/me/scam/assessments/messages",
                json={"message_text": message, "language_hint": "en-IN"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            results.append(response.json())
        self.assertEqual([item["risk_level"] for item in results], ["HIGH", "LOW", "HIGH", "LOW"])
        ids = [item["assessment_id"] for item in results]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(len({item["created_at"] for item in results}), 4)
        first_categories = {item["category"] for item in results[0]["indicators"]}
        second_categories = {item["category"] for item in results[1]["indicators"]}
        self.assertTrue(first_categories)
        self.assertEqual(second_categories, set())
        self.assertEqual(results[1]["indicators"], [])
        self.assertEqual(results[1]["evidence_strength"], "NONE")
        self.assertNotIn("account will be blocked", results[1]["explanation"].casefold())
        for assessment_id, expected in zip(ids, ("HIGH", "LOW", "HIGH", "LOW")):
            detail = self.client.get(f"/api/v1/me/scam/assessments/{assessment_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["risk_level"], expected)
            report = self.client.get(
                f"/api/v1/me/scam/reports/MANUAL_MESSAGE/{assessment_id}"
            )
            self.assertEqual(report.status_code, 200, report.text)
            self.assertEqual(report.json()["report_id"], assessment_id)
            self.assertEqual(report.json()["risk_level"], expected)

    def test_customer_isolation_list_and_detail(self):
        created = self.client.post(
            "/api/v1/me/scam/assessments/messages", json={"message_text": "Hello from my bank."}
        ).json()
        self.principal = _principal("demo-priya")
        self.assertEqual(self.client.get("/api/v1/me/scam/assessments").json()["items"], [])
        self.assertEqual(
            self.client.get(f"/api/v1/me/scam/assessments/{created['assessment_id']}").status_code,
            404,
        )

    def test_pagination_and_invalid_or_oversized_input(self):
        for text in ("first message", "second message", "third message"):
            self.assertEqual(self.client.post(
                "/api/v1/me/scam/assessments/messages", json={"message_text": text}
            ).status_code, 201)
        first = self.client.get("/api/v1/me/scam/assessments?limit=2").json()
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        second = self.client.get(
            "/api/v1/me/scam/assessments", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["has_more"])
        self.assertEqual(self.client.post(
            "/api/v1/me/scam/assessments/messages", json={"message_text": " "}
        ).status_code, 422)
        self.assertEqual(self.client.post(
            "/api/v1/me/scam/assessments/messages", json={"message_text": "x" * 12001}
        ).status_code, 422)

    def test_engine_failure_is_safe_and_does_not_persist(self):
        with patch("services.scam_assessment_service.analyze_scam_text", side_effect=RuntimeError("secret/internal")):
            response = self.client.post(
                "/api/v1/me/scam/assessments/messages", json={"message_text": "hello"}
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret/internal", response.text)
        self.assertEqual(self.client.get("/api/v1/me/scam/assessments").json()["items"], [])

    def test_additive_migration_is_idempotent_and_preserves_existing_rows(self):
        created = self.client.post(
            "/api/v1/me/scam/assessments/messages", json={"message_text": "An ordinary message."}
        )
        self.assertEqual(created.status_code, 201)
        database.initialize_database()
        conn = database.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scam_assessments").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_scam_assessments_owner_created'"
            ).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
