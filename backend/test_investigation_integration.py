"""Offline API and assistant tests for authenticated investigations."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api_models import InvestigationConfirmationRequest
from backend.auth import get_authenticated_principal
from backend.investigation_routes import router as investigations_router
from backend.assistant_routes import router as assistant_router
from services import database
from services.assistant_service import AssistantOrchestrator, LLM_TOOL_NAMES, _required_tools_for_question
from services.conversation_service import create_conversation, save_user_turn
from services.investigation_service import (
    confirm_investigation_action, create_investigation,
)
from services.llm_interfaces import LLMResponse, LLMToolCall
from services.llm_adapters import MockLLMProvider
from services.principal import CustomerPrincipal


def _principal(key="demo-arjun", session_id=None):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        key, session_id or uuid4(), now - timedelta(minutes=1),
        now + timedelta(minutes=30),
    )


def _request(text, conversation_id=None):
    from backend.api_models import AssistantTurnRequest
    return AssistantTurnRequest(
        conversation_id=conversation_id, client_turn_id=uuid4(),
        input_mode="text", output_mode="text", text=text,
    )


class InvestigationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp.name) / "integration.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers(customer_key, name, email) VALUES (?, ?, ?)",
                [("demo-arjun", "Arjun Demo", "arjun@example.test"),
                 ("demo-priya", "Priya Demo", "priya@example.test")],
            )
            conn.commit()
        finally:
            conn.close()
        self.principal = _principal()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_authenticated_api_owner_scope_and_unauthenticated_rejection(self):
        app = FastAPI()
        app.include_router(investigations_router)
        with TestClient(app) as client:
            unauthenticated = client.get("/api/v1/me/scam/investigations")
            self.assertEqual(unauthenticated.status_code, 401)
            app.dependency_overrides[get_authenticated_principal] = lambda: self.principal
            created = client.post("/api/v1/me/scam/investigations", json={
                "title": "Suspicious caller", "category": "CALL_SCAM",
                "summary": "Caller requested a one-time passcode.",
                "evidence": [], "confirmed": True,
            })
            self.assertEqual(created.status_code, 201)
            ref = created.json()["investigation_ref"]
            self.assertNotIn("investigation_id", created.text)
            listed = client.get("/api/v1/me/scam/investigations")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["items"][0]["investigation_ref"], ref)
            detail = client.get(f"/api/v1/me/scam/investigations/{ref}")
            self.assertEqual(detail.status_code, 200)
            app.dependency_overrides[get_authenticated_principal] = lambda: _principal("demo-priya")
            self.assertEqual(client.get(f"/api/v1/me/scam/investigations/{ref}").status_code, 404)

    def test_assistant_proposes_and_confirms_creation_once(self):
        conversation = create_conversation(self.principal)
        provider = MockLLMProvider([LLMResponse(
            text="", tool_calls=(LLMToolCall("propose_create_investigation", {
                "title": "Suspicious caller", "category": "CALL_SCAM",
                "summary": "Caller pressured me to share an OTP.", "evidence": [],
            }, "investigation-call-1"),),
        )])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal,
            _request("Create an investigation to track this suspicious call.", conversation["conversation_id"]),
        )
        self.assertIsNotNone(response.pending_action, f"assistant={response.assistant_text!r}, tools={response.tool_results!r}")
        self.assertEqual(response.pending_action.action.action_type, "create_investigation")
        self.assertIn("Nothing has been created yet", response.assistant_text)
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        request = InvestigationConfirmationRequest(
            confirmed=True, confirmation_method="button",
            conversation_id=response.conversation_id,
            original_turn_id=response.turn_id,
            action_type="create_investigation",
        )
        app = FastAPI()
        app.include_router(assistant_router)
        app.dependency_overrides[get_authenticated_principal] = lambda: self.principal
        with TestClient(app) as client:
            http_result = client.post(
                f"/api/v1/assistant/investigation-confirmations/{response.pending_action.confirmation_id}",
                json=request.model_dump(mode="json"),
            )
        self.assertEqual(http_result.status_code, 200)
        from backend.api_models import ConfirmationResponse
        result = ConfirmationResponse.model_validate(http_result.json())
        self.assertEqual(result.status, "executed")
        self.assertTrue(result.action_result.data["investigation_ref"].startswith("INV-"))
        with self.assertRaises(Exception):
            confirm_investigation_action(
                self.principal, response.pending_action.confirmation_id, request,
            )

    def test_assistant_list_uses_owner_scoped_service_facts(self):
        create_investigation(
            self.principal, title="My report", category="OTHER",
            summary="A customer-reported security concern.", evidence=[],
        )
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("list_investigations", {}, "list-1"),)),
            LLMResponse(text="invented or model-authored results are ignored"),
        ])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal, _request("Show my investigations"),
        )
        self.assertIn("INV-", response.assistant_text)
        self.assertIn("My report", response.assistant_text)
        self.assertIn("OPEN", response.assistant_text)

    def test_assistant_tool_registry_and_required_routes_are_scoped(self):
        self.assertTrue({
            "list_investigations", "get_investigation", "propose_create_investigation",
            "propose_link_investigation_evidence", "propose_investigation_note",
            "propose_investigation_status", "propose_close_investigation",
        }.issubset(LLM_TOOL_NAMES))
        self.assertEqual(_required_tools_for_question("What investigations do I have?"), frozenset({"list_investigations"}))
        self.assertEqual(_required_tools_for_question("Show details for INV-" + "A" * 32), frozenset({"get_investigation"}))


if __name__ == "__main__":
    unittest.main()
