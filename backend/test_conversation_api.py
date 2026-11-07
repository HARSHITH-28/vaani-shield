"""Contract-level tests for conversation create/list endpoints."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from backend.api_models import (
    ConversationCreateRequest,
    ConversationDetail,
    ConversationSummary,
    CursorPage,
)
from backend.conversation_routes import (
    get_conversation_detail,
    get_conversations,
    post_conversation,
)
from services import database
from services.principal import CustomerPrincipal


def _principal(key):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key=key,
        session_id=uuid4(),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class ConversationEndpointContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "conversation-api.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                ("demo-arjun", "arjun@example.com", "Arjun Demo"),
            )
            conn.execute(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                ("demo-priya", "priya@example.com", "Priya Demo"),
            )
            conn.commit()
        finally:
            conn.close()
        self.current_principal = _principal("demo-arjun")

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_create_and_list_match_public_response_models(self):
        created = post_conversation(
            ConversationCreateRequest(title="Demo chat"), self.current_principal
        )
        detail = ConversationDetail.model_validate(created).model_dump(mode="json")
        self.assertEqual(detail["title"], "Demo chat")
        self.assertEqual(detail["turn_count"], 0)
        self.assertNotIn("customer_key", detail)

        listed = get_conversations(25, None, self.current_principal)
        page = CursorPage[ConversationSummary].model_validate(listed).model_dump(mode="json")
        self.assertEqual(len(page["items"]), 1)
        summary = page["items"][0]
        self.assertEqual(summary["conversation_id"], detail["conversation_id"])
        self.assertNotIn("customer_key", summary)
        self.assertNotIn("turn_count", summary)

    def test_conversation_isolation_remains_enforced(self):
        created = post_conversation(ConversationCreateRequest(), self.current_principal)
        conversation_id = created["conversation_id"]

        self.current_principal = _principal("demo-priya")
        listed = get_conversations(25, None, self.current_principal)
        page = CursorPage[ConversationSummary].model_validate(listed)
        self.assertEqual(page.items, [])
        with self.assertRaises(HTTPException) as raised:
            get_conversation_detail(conversation_id, self.current_principal)
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
