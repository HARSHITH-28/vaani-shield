"""Offline API tests for authenticated synthetic Customer Support tickets."""

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


def principal(customer_key):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30)
    )


class CustomerSupportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "support-api.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.com", "Synthetic Customer A"),
                    ("demo-priya", "priya@example.com", "Synthetic Customer B"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.old_overrides = main.app.dependency_overrides.copy()
        self.current_principal = principal("demo-arjun")
        main.app.dependency_overrides[auth.get_authenticated_principal] = (
            lambda: self.current_principal
        )
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.db_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(**updates):
        result = {
            "category": "digital_banking",
            "priority": "MEDIUM",
            "subject": "Cannot access the demo app",
            "description": "Sign-in repeatedly fails in the synthetic demo.",
        }
        result.update(updates)
        return result

    def create(self, *, principal_key=None, **payload_updates):
        if principal_key:
            self.current_principal = principal(principal_key)
        return self.client.post(
            "/api/v1/me/support/tickets", json=self.payload(**payload_updates)
        )

    def test_unauthenticated_access_is_rejected(self):
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        response = self.client.get("/api/v1/me/support/tickets")
        self.assertEqual(response.status_code, 401)

    def test_demo_cookie_session_authenticates_support_route(self):
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        with patch.dict(
            os.environ,
            {
                "VAANISHIELD_DEMO_SESSION_SECRET": "support-api-test-secret-32-bytes-minimum",
                "VAANISHIELD_DEMO_COOKIE_SECURE": "false",
            },
        ):
            login = self.client.post(
                "/api/v1/auth/demo/session", json={"customer_key": "demo-arjun"}
            )
            self.assertEqual(login.status_code, 200, login.text)
            response = self.client.post(
                "/api/v1/me/support/tickets", json=self.payload()
            )
        self.assertEqual(response.status_code, 201, response.text)

    def test_creation_sets_server_controlled_origin_and_synthetic_label(self):
        response = self.client.post(
            "/api/v1/me/support/tickets",
            json=self.payload(origin="assistant_confirmed", is_synthetic_demo=False),
        )
        # Unknown fields are rejected; callers cannot set server-owned fields.
        self.assertEqual(response.status_code, 422)
        created = self.client.post(
            "/api/v1/me/support/tickets", json=self.payload()
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertRegex(body["ticket_ref"], r"^VS-[A-F0-9]{32}$")
        self.assertEqual(body["status"], "OPEN")
        self.assertEqual(body["origin"], "customer_form")
        self.assertTrue(body["is_synthetic_demo"])
        serialized = json.dumps(body)
        self.assertNotIn("case_id", serialized)
        self.assertNotIn("customer_id", serialized)

    def test_owner_scoped_list_detail_and_non_disclosing_404(self):
        arjun = self.create().json()
        priya = self.create(principal_key="demo-priya", subject="Priya demo issue").json()
        self.current_principal = principal("demo-arjun")
        page = self.client.get("/api/v1/me/support/tickets")
        self.assertEqual(page.status_code, 200)
        self.assertEqual([item["ticket_ref"] for item in page.json()["items"]], [arjun["ticket_ref"]])
        detail = self.client.get(f"/api/v1/me/support/tickets/{arjun['ticket_ref']}")
        self.assertEqual(detail.status_code, 200)
        cross = self.client.get(f"/api/v1/me/support/tickets/{priya['ticket_ref']}")
        unknown = self.client.get(
            "/api/v1/me/support/tickets/VS-" + "F" * 32
        )
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(cross.json(), unknown.json())

    def test_pagination_and_approved_filters(self):
        self.create()
        self.create(category="loan", priority="HIGH", subject="Loan demo issue")
        page = self.client.get("/api/v1/me/support/tickets?limit=1&category=loan&priority=HIGH")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.json()["items"]), 1)
        self.assertEqual(page.json()["items"][0]["category"], "loan")
        self.assertEqual(page.json()["items"][0]["priority"], "HIGH")
        self.assertEqual(
            self.client.get("/api/v1/me/support/tickets?status=NOT_A_STATUS").status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/api/v1/me/support/tickets?cursor=bad-cursor").status_code,
            400,
        )

    def test_explicit_close_and_event_history_are_owner_scoped(self):
        created = self.create().json()
        path = f"/api/v1/me/support/tickets/{created['ticket_ref']}/status"
        self.assertEqual(
            self.client.patch(path, json={"status": "CLOSED", "confirmation_method": "button"}).status_code,
            422,
        )
        closed = self.client.patch(
            path,
            json={"status": "CLOSED", "confirmed": True, "confirmation_method": "button"},
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["status"], "CLOSED")
        events_path = f"/api/v1/me/support/tickets/{created['ticket_ref']}/events"
        events = self.client.get(events_path)
        self.assertEqual(events.status_code, 200)
        self.assertEqual([event["to_status"] for event in events.json()], ["OPEN", "CLOSED"])
        self.assertNotIn("case_id", json.dumps(events.json()))

        self.current_principal = principal("demo-priya")
        cross_events = self.client.get(events_path)
        self.assertEqual(cross_events.status_code, 404)
        self.assertEqual(cross_events.json(), {"detail": "Support ticket not found."})

    def test_cross_customer_and_unknown_ticket_closure_are_non_disclosing(self):
        arjun_ticket = self.create(subject="Arjun-owned ticket").json()
        priya_ticket = self.create(
            principal_key="demo-priya", subject="Priya-owned ticket"
        ).json()
        self.current_principal = principal("demo-arjun")
        close_body = {
            "status": "CLOSED", "confirmed": True,
            "confirmation_method": "button",
        }
        cross = self.client.patch(
            f"/api/v1/me/support/tickets/{priya_ticket['ticket_ref']}/status",
            json=close_body,
        )
        unknown = self.client.patch(
            f"/api/v1/me/support/tickets/VS-{'F' * 32}/status",
            json=close_body,
        )
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json(), unknown.json())

        self.current_principal = principal("demo-priya")
        unchanged = self.client.get(
            f"/api/v1/me/support/tickets/{priya_ticket['ticket_ref']}"
        )
        self.assertEqual(unchanged.status_code, 200)
        self.assertEqual(unchanged.json()["status"], "OPEN")
        self.current_principal = principal("demo-arjun")
        own = self.client.get(
            f"/api/v1/me/support/tickets/{arjun_ticket['ticket_ref']}"
        )
        self.assertEqual(own.json()["status"], "OPEN")

    def test_staff_transitions_reopening_and_repeated_close_are_rejected(self):
        created = self.create().json()
        path = f"/api/v1/me/support/tickets/{created['ticket_ref']}/status"
        body = {"confirmed": True, "confirmation_method": "button"}
        for disallowed in ("OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER", "RESOLVED"):
            response = self.client.patch(path, json={**body, "status": disallowed})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.client.patch(path, json={**body, "status": "CLOSED"}).status_code,
            200,
        )
        repeated = self.client.patch(path, json={**body, "status": "CLOSED"})
        self.assertEqual(repeated.status_code, 409)

    def test_input_validation_and_public_response_projection(self):
        invalid = self.client.post(
            "/api/v1/me/support/tickets",
            json=self.payload(category="other; DROP TABLE support_cases", priority="CRITICAL"),
        )
        self.assertEqual(invalid.status_code, 422)
        too_long = self.client.post(
            "/api/v1/me/support/tickets", json=self.payload(description="x" * 4001)
        )
        self.assertEqual(too_long.status_code, 422)

    def test_ticket_conversation_reference_must_be_owned(self):
        # A malformed or foreign conversation reference cannot be attached.
        response = self.client.post(
            "/api/v1/me/support/tickets",
            json=self.payload(conversation_id=str(uuid4())),
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
