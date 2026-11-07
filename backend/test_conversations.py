"""Offline conversation persistence tests using isolated temporary databases."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from backend.auth import get_authenticated_principal
from backend.conversation_routes import router
from services import database
from services.conversation_service import (
    ConversationNotFoundError,
    create_conversation,
    delete_conversation_turn,
    get_conversation,
    get_conversation_history,
    get_recent_conversation_turns,
    list_conversations,
    save_assistant_turn,
    save_user_turn,
    update_conversation,
)
from services.principal import CustomerPrincipal


def _principal(customer_key: str) -> CustomerPrincipal:
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key=customer_key,
        session_id=uuid4(),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class ConversationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "isolated.sqlite3"
        self.db_path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            for key, email, name in (
                ("demo-arjun", "arjun@example.com", "Arjun Demo"),
                ("demo-priya", "priya@example.com", "Priya Demo"),
            ):
                conn.execute(
                    "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                    (key, email, name),
                )
            conn.commit()
        finally:
            conn.close()
        self.arjun = _principal("demo-arjun")
        self.priya = _principal("demo-priya")

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_create_turns_history_order_and_updated_timestamp(self):
        conversation = create_conversation(self.arjun, "Demo chat")
        before = conversation["updated_at"]
        user_turn = save_user_turn(
            self.arjun,
            conversation["conversation_id"],
            input_type="text",
            text_or_transcript="What is my balance?",
            input_language="en-IN",
            requested_output_mode="text",
        )
        assistant_turn = save_assistant_turn(
            self.arjun,
            conversation["conversation_id"],
            response_text="Your demo balance is available.",
            response_language="en-IN",
            requested_output_mode="text",
        )
        history = get_conversation_history(self.arjun, conversation["conversation_id"])
        self.assertEqual([turn["role"] for turn in history["items"]], ["user", "assistant"])
        self.assertEqual([turn["sequence_number"] for turn in history["items"]], [1, 2])
        self.assertEqual(user_turn["turn_id"], history["items"][0]["turn_id"])
        self.assertEqual(assistant_turn["turn_id"], history["items"][1]["turn_id"])
        self.assertGreater(get_conversation(self.arjun, conversation["conversation_id"])["updated_at"], before)

    def test_conversation_and_turn_pagination(self):
        created = [create_conversation(self.arjun, f"chat {n}") for n in range(3)]
        first = list_conversations(self.arjun, limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        second = list_conversations(self.arjun, limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 1)
        for _ in range(3):
            save_user_turn(
                self.arjun, created[0]["conversation_id"], input_type="audio",
                text_or_transcript="Transcript", requested_output_mode="both",
            )
        page1 = get_conversation_history(self.arjun, created[0]["conversation_id"], limit=2)
        page2 = get_conversation_history(
            self.arjun, created[0]["conversation_id"], limit=2,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(len(page1["items"]), 2)
        self.assertTrue(page1["has_more"])
        self.assertEqual([row["sequence_number"] for row in page1["items"]], [1, 2])
        self.assertEqual([row["sequence_number"] for row in page2["items"]], [3])

    def test_recent_context_selects_latest_twelve_in_chronological_order(self):
        conversation = create_conversation(self.arjun)
        conversation_id = conversation["conversation_id"]
        for pair in range(1, 26):
            save_user_turn(
                self.arjun, conversation_id, input_type="text",
                text_or_transcript=f"Question {pair}", requested_output_mode="text",
            )
            save_assistant_turn(
                self.arjun, conversation_id,
                response_text=(
                    "VaaniShield D is successfully testing ಜ್ಞಾನಿ Timber."
                    if pair == 6 else f"Answer {pair}"
                ),
                requested_output_mode="text",
            )

        recent = get_recent_conversation_turns(self.arjun, conversation_id, limit=12)
        self.assertEqual(len(recent), 12)
        self.assertEqual([turn["sequence_number"] for turn in recent], list(range(39, 51)))
        self.assertEqual(recent[-1]["assistant_response_text"], "Answer 25")
        self.assertNotIn(
            "VaaniShield D is successfully testing ಜ್ಞಾನಿ Timber.",
            [turn["assistant_response_text"] for turn in recent],
        )

    def test_customer_isolation_and_cross_owner_access_rejected(self):
        conversation = create_conversation(self.arjun)
        own_list = list_conversations(self.priya)
        self.assertEqual(own_list["items"], [])
        with self.assertRaises(ConversationNotFoundError):
            get_conversation(self.priya, conversation["conversation_id"])
        with self.assertRaises(ConversationNotFoundError):
            get_conversation_history(self.priya, conversation["conversation_id"])
        with self.assertRaises(ConversationNotFoundError):
            get_recent_conversation_turns(self.priya, conversation["conversation_id"])
        with self.assertRaises(ConversationNotFoundError):
            save_user_turn(
                self.priya, conversation["conversation_id"], input_type="text",
                text_or_transcript="No", requested_output_mode="text",
            )

    def test_update_status_and_title(self):
        conversation = create_conversation(self.arjun)
        before = conversation["updated_at"]
        updated = update_conversation(
            self.arjun, conversation["conversation_id"], status="archived", title="Archived"
        )
        self.assertEqual(updated["status"], "archived")
        self.assertEqual(updated["title"], "Archived")
        self.assertGreater(updated["updated_at"], before)

    def test_first_user_turn_sets_safe_meaningful_title(self):
        conversation = create_conversation(self.arjun)
        save_user_turn(
            self.arjun, conversation["conversation_id"], input_type="text",
            text_or_transcript="Please check my balance; OTP: 984321, email alex@example.com",
            requested_output_mode="text",
        )
        title = get_conversation(self.arjun, conversation["conversation_id"])["title"]
        self.assertIn("Please check my balance", title)
        self.assertNotIn("984321", title)
        self.assertNotIn("alex@example.com", title)

    def test_unauthenticated_dependency_rejects_request(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/v1/assistant/conversations",
            "raw_path": b"/api/v1/assistant/conversations", "query_string": b"",
            "headers": [], "client": ("test", 1), "server": ("test", 80),
        }
        with self.assertRaises(HTTPException) as error:
            get_authenticated_principal(Request(scope))
        self.assertEqual(error.exception.status_code, 401)

    def test_routes_are_authenticated_and_registered_contract_paths(self):
        from backend.main import app

        paths = app.openapi()["paths"]
        for path in (
            "/api/v1/assistant/conversations",
            "/api/v1/assistant/conversations/{conversation_id}",
            "/api/v1/assistant/conversations/{conversation_id}/turns",
            "/api/v1/assistant/conversations/{conversation_id}/turns/{turn_id}",
        ):
            self.assertIn(path, paths)
        self.assertEqual(len(router.routes), 5)

    def test_message_deletion_scrubs_one_turn_and_cancels_only_bound_actions(self):
        from uuid import uuid4
        conversation = create_conversation(self.arjun, "Delete test")
        user = save_user_turn(
            self.arjun, conversation["conversation_id"], input_type="text",
            text_or_transcript="Private support text", input_language="en-IN",
            requested_output_mode="text", detected_languages=("en-IN",),
        )
        assistant = save_assistant_turn(
            self.arjun, conversation["conversation_id"], response_text="Private assistant text",
            response_language="en-IN", requested_output_mode="text",
        )
        other_conversation = create_conversation(self.arjun, "Other")
        other_user = save_user_turn(
            self.arjun, other_conversation["conversation_id"], input_type="text",
            text_or_transcript="Other conversation", requested_output_mode="text",
        )
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO pending_actions (confirmation_id, customer_key, session_id, conversation_id, "
                "original_turn_id, action_type, action_json, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'create_support_ticket', ?, 'pending', ?, ?)",
                (str(uuid4()), self.arjun.customer_key, str(self.arjun.session_id),
                 conversation["conversation_id"], user["turn_id"], '{"description":"private"}',
                 "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
            )
            unrelated_id = str(uuid4())
            conn.execute(
                "INSERT INTO pending_actions (confirmation_id, customer_key, session_id, conversation_id, "
                "original_turn_id, action_type, action_json, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'create_support_ticket', ?, 'pending', ?, ?)",
                (unrelated_id, self.arjun.customer_key, str(self.arjun.session_id),
                 other_conversation["conversation_id"], other_user["turn_id"], '{"other":"keep"}',
                 "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        delete_conversation_turn(self.arjun, conversation["conversation_id"], user["turn_id"])
        from services.pending_action_service import PendingActionNotFound, create_pending_action
        with self.assertRaises(PendingActionNotFound):
            create_pending_action(
                self.arjun, conversation["conversation_id"], user["turn_id"],
                {
                    "action_type": "create_support_ticket", "category": "card",
                    "priority": "MEDIUM", "subject": "Deleted message proposal",
                },
            )
        history = get_conversation_history(self.arjun, conversation["conversation_id"])
        self.assertEqual([item["turn_id"] for item in history["items"]], [assistant["turn_id"]])
        self.assertIsNone(get_conversation(self.arjun, conversation["conversation_id"])["title"])
        recent = get_recent_conversation_turns(self.arjun, conversation["conversation_id"])
        self.assertEqual([item["turn_id"] for item in recent], [assistant["turn_id"]])
        conn = database.get_connection()
        try:
            deleted = conn.execute("SELECT user_text_or_transcript, deleted_at FROM conversation_turns WHERE turn_id = ?", (user["turn_id"],)).fetchone()
            self.assertIsNone(deleted["user_text_or_transcript"])
            self.assertIsNotNone(deleted["deleted_at"])
            self.assertEqual(conn.execute("SELECT status, action_json FROM pending_actions WHERE original_turn_id = ?", (user["turn_id"],)).fetchone()[:], ("declined", "{}"))
            self.assertEqual(conn.execute("SELECT status, action_json FROM pending_actions WHERE confirmation_id = ?", (unrelated_id,)).fetchone()[:], ("pending", '{"other":"keep"}'))
        finally:
            conn.close()

    def test_message_deletion_is_owner_scoped_and_not_repeatable(self):
        conversation = create_conversation(self.arjun, "Owned")
        user = save_user_turn(
            self.arjun, conversation["conversation_id"], input_type="text",
            text_or_transcript="Hello", requested_output_mode="text",
        )
        with self.assertRaises(ConversationNotFoundError):
            delete_conversation_turn(self.priya, conversation["conversation_id"], user["turn_id"])
        delete_conversation_turn(self.arjun, conversation["conversation_id"], user["turn_id"])
        with self.assertRaises(ConversationNotFoundError):
            delete_conversation_turn(self.arjun, conversation["conversation_id"], user["turn_id"])

    def test_additive_turn_deletion_migration_is_idempotent_and_preserves_existing_text(self):
        conversation = create_conversation(self.arjun, "Migration")
        turn = save_user_turn(
            self.arjun, conversation["conversation_id"], input_type="text",
            text_or_transcript="Preserve this existing message", requested_output_mode="text",
        )
        database.initialize_database()
        database.initialize_database()
        history = get_conversation_history(self.arjun, conversation["conversation_id"])
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["turn_id"], turn["turn_id"])
        self.assertEqual(history["items"][0]["user_text_or_transcript"], "Preserve this existing message")


if __name__ == "__main__":
    unittest.main()
