"""Offline pending-action confirmation tests using temporary SQLite databases."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from backend.api_models import ConfirmationRequest, SupportTicketCreateAction
from services import database
from services.conversation_service import create_conversation, save_user_turn
from services.pending_action_service import (
    PendingActionConflict,
    PendingActionNotFound,
    confirm_pending_action,
    create_pending_action,
)
from services.customer_support_service import create_support_case
from services.principal import CustomerPrincipal


def principal(customer_key):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30)
    )


class PendingActionConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "pending-actions.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.test", "Synthetic Customer A"),
                    ("demo-priya", "priya@example.test", "Synthetic Customer B"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.arjun = principal("demo-arjun")
        self.priya = principal("demo-priya")
        self.current_principal = self.arjun
        self.arjun_conversation = create_conversation(self.arjun)["conversation_id"]
        self.other_arjun_conversation = create_conversation(self.arjun)["conversation_id"]
        self.priya_conversation = create_conversation(self.priya)["conversation_id"]
        self.arjun_turn = save_user_turn(
            self.arjun,
            self.arjun_conversation,
            input_type="text",
            text_or_transcript="I need help with demo sign-in.",
            requested_output_mode="text",
        )["turn_id"]
        self.old_overrides = main.app.dependency_overrides.copy()
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
    def action():
        return SupportTicketCreateAction(
            action_type="create_support_ticket",
            category="digital_banking",
            priority="MEDIUM",
            subject="Demo sign-in issue",
            description="The synthetic demo sign-in page did not accept my attempt.",
            language="en-IN",
        )

    def propose(self, principal_value=None, conversation_id=None):
        target_conversation = conversation_id or self.arjun_conversation
        return create_pending_action(
            principal_value or self.arjun,
            target_conversation,
            self.arjun_turn,
            self.action(),
        )

    def confirm_payload(self, pending, **updates):
        body = {
            "confirmed": True,
            "confirmation_method": "button",
            "conversation_id": str(pending.conversation_id),
            "original_turn_id": str(pending.original_turn_id),
            "action_type": "create_support_ticket",
        }
        body.update(updates)
        return body

    def ticket_count(self):
        conn = database.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
        finally:
            conn.close()

    def pending_row(self, confirmation_id):
        conn = database.get_connection()
        try:
            return dict(conn.execute(
                "SELECT * FROM pending_actions WHERE confirmation_id = ?",
                (str(confirmation_id),),
            ).fetchone())
        finally:
            conn.close()

    def test_confirmation_creates_one_ticket_and_replay_is_rejected(self):
        pending = self.propose()
        self.assertEqual(self.ticket_count(), 0)
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "executed")
        self.assertTrue(body["action_result"]["data"]["is_synthetic_demo"])
        self.assertRegex(body["action_result"]["data"]["ticket_ref"], r"^VS-[A-F0-9]{32}$")
        self.assertNotIn("case_id", json.dumps(body))
        self.assertEqual(self.ticket_count(), 1)
        ticket = self.client.get(
            f"/api/v1/me/support/tickets/{body['action_result']['data']['ticket_ref']}"
        )
        self.assertEqual(ticket.status_code, 200)
        self.assertEqual(ticket.json()["origin"], "assistant_confirmed")
        self.assertEqual(ticket.json()["description"], self.action().description)

        replay = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.ticket_count(), 1)
        self.assertEqual(self.pending_row(pending.confirmation_id)["action_json"], "{}")
        conn = database.get_connection()
        try:
            conn.execute(
                "UPDATE pending_actions SET expires_at = ? WHERE confirmation_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), str(pending.confirmation_id)),
            )
            conn.commit()
        finally:
            conn.close()
        expired_replay = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(expired_replay.status_code, 409)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "executed")

    def test_decline_consumes_action_without_creating_ticket(self):
        pending = self.propose()
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending, confirmed=False),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "declined")
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "declined")
        self.assertEqual(self.pending_row(pending.confirmation_id)["action_json"], "{}")

    def test_expired_action_is_rejected_and_payload_scrubbed(self):
        pending = self.propose()
        conn = database.get_connection()
        try:
            conn.execute(
                "UPDATE pending_actions SET expires_at = ? WHERE confirmation_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), str(pending.confirmation_id)),
            )
            conn.commit()
        finally:
            conn.close()
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "expired")
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.pending_row(pending.confirmation_id)["action_json"], "{}")

    def test_cross_customer_confirmation_is_non_disclosing(self):
        pending = self.propose()
        self.current_principal = self.priya
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(response.status_code, 404)
        unknown = self.client.post(
            f"/api/v1/assistant/confirmations/{uuid4()}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(response.json(), unknown.json())
        self.assertEqual(self.ticket_count(), 0)

    def test_confirmation_is_bound_to_the_originating_demo_session(self):
        pending = self.propose()
        self.current_principal = principal("demo-arjun")
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.ticket_count(), 0)

    def test_wrong_conversation_is_rejected_without_consuming_action(self):
        pending = self.propose()
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(
                pending, conversation_id=str(self.other_arjun_conversation)
            ),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "pending")
        correct = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(self.ticket_count(), 1)

    def test_wrong_action_type_is_rejected_without_consuming_action(self):
        pending = self.propose()
        wrong_contract = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending, action_type="block_card"),
        )
        self.assertEqual(wrong_contract.status_code, 422)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "pending")

        wrong_service_request = ConfirmationRequest.model_construct(
            confirmed=True,
            confirmation_method="button",
            conversation_id=pending.conversation_id,
            original_turn_id=pending.original_turn_id,
            action_type="block_card",
        )
        with self.assertRaises(PendingActionConflict):
            confirm_pending_action(
                self.arjun, pending.confirmation_id, wrong_service_request
            )
        self.assertEqual(self.ticket_count(), 0)

    def test_confirmation_is_bound_to_an_owned_conversation_at_proposal_time(self):
        with self.assertRaises(PendingActionNotFound):
            self.propose(self.arjun, self.priya_conversation)
        self.assertEqual(self.ticket_count(), 0)

    def test_unauthenticated_confirmation_is_rejected(self):
        pending = self.propose()
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.ticket_count(), 0)

    def test_concurrent_confirmation_can_create_only_one_ticket(self):
        pending = self.propose()
        request = ConfirmationRequest(
            confirmed=True,
            confirmation_method="button",
            conversation_id=pending.conversation_id,
            original_turn_id=pending.original_turn_id,
            action_type="create_support_ticket",
        )

        def attempt():
            try:
                return confirm_pending_action(
                    self.arjun, pending.confirmation_id, request
                ).status
            except PendingActionConflict:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(outcomes, ["executed", "rejected"])
        self.assertEqual(self.ticket_count(), 1)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "executed")

    def test_wrong_original_turn_is_rejected_without_consuming_action(self):
        pending = self.propose()
        wrong_turn = save_user_turn(
            self.arjun,
            self.arjun_conversation,
            input_type="text",
            text_or_transcript="A separate unrelated request.",
            requested_output_mode="text",
        )["turn_id"]
        response = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending, original_turn_id=wrong_turn),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(self.pending_row(pending.confirmation_id)["status"], "pending")

        correct = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json=self.confirm_payload(pending),
        )
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json()["status"], "executed")
        self.assertEqual(self.ticket_count(), 1)

    def test_additive_pending_migration_is_idempotent_and_preserves_support_case_ids(self):
        case = create_support_case(
            self.arjun,
            category="account",
            priority="LOW",
            subject="Existing synthetic case",
            description="Existing case must remain intact.",
        )
        conn = database.get_connection()
        try:
            before = conn.execute(
                "SELECT case_id, ticket_ref, description FROM support_cases WHERE ticket_ref = ?",
                (case["ticket_ref"],),
            ).fetchone()
        finally:
            conn.close()

        database.initialize_database()
        database.initialize_database()
        conn = database.get_connection()
        try:
            after = conn.execute(
                "SELECT case_id, ticket_ref, description FROM support_cases WHERE ticket_ref = ?",
                (case["ticket_ref"],),
            ).fetchone()
            self.assertEqual(tuple(after), tuple(before))
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_actions'"
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_pending_actions_owner_session_expiry'"
            ).fetchone())
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
