"""Offline orchestration tests backed by disposable SQLite databases."""

import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from starlette.requests import Request

from backend.api_models import AssistantTurnRequest, SpeechOutput
from backend.auth import get_authenticated_principal
from services import database
from services.assistant_service import (
    AssistantOrchestrator,
    AssistantServiceError,
    InputProcessingUnavailable,
    OutputProcessingUnavailable,
    Transcription,
    _is_support_create_request,
    _required_tools_for_question,
)
from services.conversation_service import (
    ConversationNotFoundError,
    create_conversation,
    get_conversation_history,
)
from services.principal import CustomerPrincipal


def principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20))


def turn_request(text="What is my balance?", **kwargs):
    values = {
        "client_turn_id": uuid4(),
        "input_mode": "text",
        "output_mode": "text",
        "text": text,
        **kwargs,
    }
    return AssistantTurnRequest(**values)


class FakeTranscriber:
    def transcribe(self, principal, audio_upload_id, language_hint):
        return Transcription("ನನ್ನ balance", ("kn-IN", "en-IN"), True)


class FakeSynthesizer:
    def synthesize(self, principal, text, language):
        return SpeechOutput(
            audio_url="/demo/audio/temporary",
            media_type="audio/wav",
            language=language,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )


class AssistantOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3"
        )
        self.db_path_patch.start()
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
        self.principal = principal()
        self.provider_patch = patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "rule_based"})
        self.provider_patch.start()
        self.orchestrator = AssistantOrchestrator()

    def tearDown(self):
        self.provider_patch.stop()
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_authenticated_dispatch_response_and_turn_persistence(self):
        with patch(
            "services.assistant_service.execute_tool",
            return_value=[{"account_type": "SAVINGS", "balance": 1250.5}],
        ) as execute:
            response = self.orchestrator.process_turn(
                self.principal, turn_request("What is my balance?")
            )
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1], self.principal)
        self.assertEqual(response.tool_results[0].status, "succeeded")
        self.assertIn("1,250.50", response.assistant_text)
        self.assertEqual(response.response_language, "en-IN")
        history = get_conversation_history(self.principal, response.conversation_id)
        self.assertEqual([item["role"] for item in history["items"]], ["user", "assistant"])
        self.assertEqual([item["processing_status"] for item in history["items"]], ["completed", "completed"])

    def test_customer_cannot_use_another_principals_conversation(self):
        conversation = create_conversation(self.principal)
        request = turn_request(conversation_id=conversation["conversation_id"])
        with self.assertRaises(ConversationNotFoundError):
            self.orchestrator.process_turn(principal("demo-priya"), request)

    def test_contract_validates_text_and_speech_inputs(self):
        with self.assertRaises(ValidationError):
            AssistantTurnRequest(
                client_turn_id=uuid4(), input_mode="text", output_mode="text"
            )
        with self.assertRaises(ValidationError):
            AssistantTurnRequest(
                client_turn_id=uuid4(), input_mode="speech", output_mode="text"
            )

    def test_temporary_intent_routing_covers_financial_and_unavailable_features(self):
        self.assertEqual(
            self.orchestrator.intent_router.route("Show category spending")[:2],
            ("financial_intelligence", "category_spending"),
        )
        self.assertEqual(
            self.orchestrator.intent_router.route("Is this a scam message?")[:2],
            ("scam_detection", "message_assessment"),
        )
        result = self.orchestrator.process_turn(
            self.principal, turn_request("Is this a scam message?")
        )
        self.assertEqual(result.tool_results[0].error_code, "FEATURE_UNAVAILABLE")
        self.assertIn("No action was taken", result.assistant_text)

    def test_service_errors_are_sanitized_and_turns_persist(self):
        with patch(
            "services.assistant_service.execute_tool",
            side_effect=RuntimeError("database details and internal customer_id=123"),
        ):
            response = self.orchestrator.process_turn(
                self.principal, turn_request("What is my balance?")
            )
        self.assertEqual(response.tool_results[0].error_code, "SERVICE_UNAVAILABLE")
        self.assertNotIn("123", response.assistant_text)
        history = get_conversation_history(self.principal, response.conversation_id)
        self.assertEqual(len(history["items"]), 2)

    def test_state_changing_request_never_executes_without_confirmation(self):
        with patch("services.assistant_service.execute_tool") as execute:
            response = self.orchestrator.process_turn(
                self.principal, turn_request("Please block my card")
            )
        execute.assert_not_called()
        self.assertEqual(
            response.tool_results[0].error_code, "CONFIRMATION_FLOW_UNAVAILABLE"
        )
        self.assertIn("No action was taken", response.assistant_text)

    def test_support_ticket_creation_routing_precedes_banking_lookups(self):
        creation_prompts = (
            "Prepare a customer support ticket.",
            "Help me raise a support ticket.",
            "My debit card is not working. Please create a support ticket.",
            "I need to report an issue with my account.",
            "I want to raise a complaint.",
            "I want to register a complaint.",
            "Please register a complaint under Card Services with the subject 'Repeated Debit Card Payment Failures'.",
            (
                "I want to report a problem with my banking services. My debit card payments are repeatedly failing. "
                "Please register a complaint under Card Services with the subject 'Repeated Debit Card Payment Failures', "
                "priority HIGH, and description 'My debit card transactions are repeatedly being declined at online and offline merchants. "
                "Please investigate the issue and resolve it as soon as possible.' Show me the ticket draft and request my confirmation before submitting it."
            ),
            "I want to report a problem with my account.",
            "Please register a ticket for my card.",
        )
        for prompt in creation_prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_support_create_request(prompt))
                self.assertEqual(_required_tools_for_question(prompt), frozenset())

    def test_support_status_and_ordinary_banking_routes_are_preserved(self):
        self.assertFalse(_is_support_create_request("What is the status of my support ticket?"))
        self.assertEqual(
            _required_tools_for_question("What is the status of my support ticket?"),
            frozenset({"list_support_tickets"}),
        )
        self.assertFalse(_is_support_create_request("How do I contact customer support?"))
        self.assertEqual(_required_tools_for_question("How do I contact customer support?"), frozenset())
        self.assertEqual(
            _required_tools_for_question("My debit card is not working"),
            frozenset({"get_card_details"}),
        )
        self.assertEqual(
            _required_tools_for_question("Show my recent payments"),
            frozenset({"get_recent_transactions"}),
        )

    def test_cross_feature_banking_query_routing_consistency(self):
        """Banking questions must route identically regardless of attached assessment reference."""
        # Clean balance question
        self.assertEqual(
            _required_tools_for_question("What is my current account balance?"),
            frozenset({"get_account_balance"}),
        )
        # Balance question asked while a scam assessment reference exists (e.g. from Scam Detection page)
        self.assertEqual(
            _required_tools_for_question("What is my current account balance?", assessment_id="test-scam-uuid-1234"),
            frozenset({"get_account_balance"}),
        )
        # Alternate phrasing for balance/savings
        self.assertEqual(
            _required_tools_for_question("How much do I have in my savings?"),
            frozenset({"get_account_balance"}),
        )
        self.assertEqual(
            _required_tools_for_question("Show my accounts"),
            frozenset({"get_account_balance"}),
        )
        # Cards and loans
        self.assertEqual(
            _required_tools_for_question("Show my cards"),
            frozenset({"get_card_details"}),
        )
        self.assertEqual(
            _required_tools_for_question("Show my loans"),
            frozenset({"get_loan_details"}),
        )
        # Scam assessment follow-up with assessment reference
        self.assertEqual(
            _required_tools_for_question("Explain this assessment", assessment_id="test-scam-uuid-1234"),
            frozenset({"get_scam_assessment"}),
        )
        self.assertEqual(
            _required_tools_for_question("Is this message safe?", assessment_id="test-scam-uuid-1234"),
            frozenset({"get_scam_assessment"}),
        )

    def test_principal_must_be_live_and_allowlisted(self):
        invalid = CustomerPrincipal(
            "other-customer", uuid4(),
            datetime.now(timezone.utc) - timedelta(minutes=1),
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        with self.assertRaises(PermissionError):
            self.orchestrator.process_turn(invalid, turn_request())

    def test_speech_interfaces_are_injected_and_language_metadata_is_kept(self):
        orchestrator = AssistantOrchestrator(
            transcriber=FakeTranscriber(), synthesizer=FakeSynthesizer()
        )
        request = AssistantTurnRequest(
            client_turn_id=uuid4(),
            input_mode="speech",
            output_mode="speech_and_text",
            audio_upload_id=uuid4(),
            language_hint="auto",
        )
        with patch("services.assistant_service.execute_tool", return_value=[]):
            response = orchestrator.process_turn(self.principal, request)
        self.assertEqual(response.transcript, "ನನ್ನ balance")
        self.assertEqual([item.language for item in response.detected_languages], ["kn-IN", "en-IN"])
        self.assertTrue(response.is_code_mixed)
        self.assertIsNotNone(response.speech_output)

    def test_unconfigured_speech_fails_without_external_calls(self):
        speech_request = AssistantTurnRequest(
            client_turn_id=uuid4(),
            input_mode="speech",
            output_mode="text",
            audio_upload_id=uuid4(),
        )
        with self.assertRaises(InputProcessingUnavailable):
            self.orchestrator.process_turn(self.principal, speech_request)

        output_request = turn_request(output_mode="speech")
        with patch.object(
            self.orchestrator.synthesizer,
            "synthesize",
            side_effect=OutputProcessingUnavailable("safe provider error"),
        ):
            with patch("services.assistant_service.execute_tool", return_value=[]):
                response = self.orchestrator.process_turn(self.principal, output_request)
        self.assertTrue(response.assistant_text)
        self.assertIsNone(response.speech_output)

    def test_unauthenticated_dependency_rejects_request(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/v1/assistant/turns",
            "raw_path": b"/api/v1/assistant/turns", "query_string": b"",
            "headers": [], "client": ("test", 1), "server": ("test", 80),
        }
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            get_authenticated_principal(Request(scope))
        self.assertEqual(error.exception.status_code, 401)

    def test_retry_after_failure_does_not_corrupt_history_with_consecutive_assistant_messages(self):
        from services.llm_interfaces import LLMMessage, LLMProviderError, LLMResponse, LLMToolCall
        from services.llm_adapters import MockLLMProvider

        failing_provider = MockLLMProvider([LLMProviderError("Temporary outage")])
        orchestrator = AssistantOrchestrator(llm_provider=failing_provider)

        client_turn_id = uuid4()
        req = AssistantTurnRequest(
            client_turn_id=client_turn_id,
            input_mode="text",
            output_mode="text",
            text="What is my balance?",
        )
        with self.assertRaises(AssistantServiceError) as err:
            orchestrator.process_turn(self.principal, req)
        self.assertIn("temporarily unavailable", str(err.exception))

        captured_messages = []
        class InspectingProvider(MockLLMProvider):
            def generate(self, messages, tools, *, response_language, required_tools=frozenset(), request_stage="unspecified"):
                captured_messages.append(list(messages))
                return super().generate(messages, tools, response_language=response_language, required_tools=required_tools, request_stage=request_stage)

        retry_provider = InspectingProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "call-1"),)),
            LLMResponse(text="Your balance is ₹500.00."),
        ])
        orchestrator.llm_provider = retry_provider

        with patch("services.assistant_service.execute_tool", return_value=[{"account_type": "SAVINGS", "balance": 500.0}]):
            retry_resp = orchestrator.process_turn(self.principal, req)

        self.assertIn("500.00", retry_resp.assistant_text)
        self.assertEqual(retry_resp.tool_results[0].status, "succeeded")

        self.assertTrue(len(captured_messages) >= 1)
        initial_round_messages = captured_messages[0]
        for msg in initial_round_messages:
            self.assertNotEqual(msg.content, "Retrying the saved request.")
        roles = [m.role for m in initial_round_messages]
        self.assertEqual(roles[-1], "user")
        for i in range(len(roles) - 1):
            self.assertFalse(roles[i] == "assistant" and roles[i+1] == "assistant", f"Found consecutive assistants at {i}: {roles}")

    def test_groq_safe_error_metadata_recognizes_tool_use_failed(self):
        from services.llm_adapters import _safe_groq_error_metadata
        class MockGroqError(Exception):
            status_code = 400
            body = {
                "error": {
                    "message": "Tool choice is none, but model called a tool",
                    "type": "invalid_request_error",
                    "code": "tool_use_failed",
                    "failed_generation": '{"name": "propose_support_ticket", "arguments": {"secret": "hide-me"}}'
                }
            }
        metadata = _safe_groq_error_metadata(MockGroqError())
        self.assertEqual(metadata["http_status"], 400)
        self.assertEqual(metadata["error_code"], "tool_use_failed")
        self.assertEqual(metadata["error_description"], "Provider rejected the generated tool-call structure.")
        self.assertTrue(metadata["failed_generation_present"])
        self.assertNotIn("hide-me", str(metadata))

    def test_financial_intelligence_intent_routing_acceptance_criteria(self):
        """Test the 5 pre-demo acceptance criteria queries and multi-intent composite."""
        # 1. Balance questions route to banking balance tools
        ac1 = _required_tools_for_question("What is my current account balance?")
        self.assertEqual(ac1, frozenset({"get_account_balance"}))

        # 2. Spending analysis routes to financial intelligence tools
        ac2 = _required_tools_for_question("Analyze my spending.")
        self.assertEqual(ac2, frozenset({"get_spending_summary"}))
        self.assertNotIn("get_account_balance", ac2)

        # 3. Category analysis retrieves spending grouped by category
        ac3 = _required_tools_for_question("Which category has the highest spending?")
        self.assertEqual(ac3, frozenset({"get_category_breakdown"}))
        self.assertNotIn("get_account_balance", ac3)

        # 4. Merchant analysis retrieves spending grouped by merchant
        ac4 = _required_tools_for_question("Which merchant did I spend the most with?")
        self.assertEqual(ac4, frozenset({"get_top_merchants"}))
        self.assertNotIn("get_account_balance", ac4)

        # 5. Budget recommendations use the financial intelligence service
        ac5 = _required_tools_for_question("Suggest three ways to save money based on my spending.")
        self.assertTrue(bool(ac5 & {"generate_budget_recommendations", "get_spending_summary"}))
        self.assertNotIn("get_account_balance", ac5)

        # Multi-intent composite query:
        # "Analyze my spending, identify my highest spending category, and suggest three practical ways to save money."
        composite = _required_tools_for_question(
            "Analyze my spending, identify my highest spending category, and suggest three practical ways to save money."
        )
        self.assertEqual(
            composite,
            frozenset({"get_spending_summary", "get_category_breakdown", "generate_budget_recommendations"}),
        )
        self.assertNotIn("get_account_balance", composite)

    def test_customer_support_slot_retention_across_turns(self):
        """When partial complaint details are given, assistant retains them and only asks for missing fields."""
        from services.llm_interfaces import LLMResponse, LLMToolCall
        from services.llm_adapters import MockLLMProvider
        from services.customer_support_service import create_support_case, get_support_case
        from services.assistant_service import _extract_support_ticket_slots

        # Turn 1: User provides Category, Priority, and Description, missing Subject
        t1_text = (
            "I want to raise a complaint. Category: Card Payment, Priority: High, "
            "Description: Debit card payment failed but amount was deducted"
        )
        slots_t1 = _extract_support_ticket_slots(t1_text)
        self.assertEqual(slots_t1["category"], "card")
        self.assertEqual(slots_t1["priority"], "HIGH")
        self.assertEqual(slots_t1["description"], "Debit card payment failed but amount was deducted")
        self.assertIsNone(slots_t1["subject"])

        # Orchestrator handles Turn 1
        provider_t1 = MockLLMProvider([LLMResponse(text="")])
        orchestrator = AssistantOrchestrator(llm_provider=provider_t1)
        req1 = turn_request(t1_text)
        resp1 = orchestrator.process_turn(self.principal, req1)

        # Asserts Turn 1 acknowledges category & priority and asks only for Subject
        self.assertIn("Category: Card", resp1.assistant_text)
        self.assertIn("Priority: HIGH", resp1.assistant_text)
        self.assertIn("subject", resp1.assistant_text.casefold())
        self.assertIn("No ticket has been created", resp1.assistant_text)
        self.assertIsNone(resp1.pending_action)

        # Turn 2: User provides the missing Subject in the same conversation
        t2_text = "Subject: Failed debit card payment at POS"
        conv_id = resp1.conversation_id
        t2_context = t1_text + " " + t2_text
        slots_t2 = _extract_support_ticket_slots(t2_context)
        self.assertEqual(slots_t2["category"], "card")
        self.assertEqual(slots_t2["priority"], "HIGH")
        self.assertEqual(slots_t2["subject"], "Failed debit card payment at POS")

        # Mock LLM calls propose_support_ticket using the accumulated slots
        proposal_provider = MockLLMProvider([
            LLMResponse(
                tool_calls=(
                    LLMToolCall(
                        name="propose_support_ticket",
                        arguments={
                            "category": "card",
                            "priority": "HIGH",
                            "subject": "Failed debit card payment at POS",
                            "description": "Debit card payment failed but amount was deducted",
                        },
                        call_id="call-prop-1",
                    ),
                )
            ),
        ])
        orchestrator.llm_provider = proposal_provider
        req2 = turn_request(t2_text, conversation_id=conv_id)
        resp2 = orchestrator.process_turn(self.principal, req2)

        # Asserts Turn 2 proposes ticket with needs_confirmation
        self.assertIsNotNone(resp2.pending_action)
        self.assertEqual(resp2.pending_action.action.category, "card")
        self.assertEqual(resp2.pending_action.action.priority, "HIGH")
        self.assertEqual(resp2.pending_action.action.subject, "Failed debit card payment at POS")
        self.assertEqual(resp2.tool_results[0].status, "needs_confirmation")

    def test_customer_support_ticket_retrieval_prioritizes_latest_ticket(self):
        """Ticket retrieval with 'complaint I just raised' displays latest ticket with full details."""
        from services.assistant_service import AssistantOrchestrator
        from backend.api_models import ToolExecutionResult

        tickets = [
            {
                "ticket_ref": "VS-NEW11111111111111111111111111111",
                "subject": "Payment deducted but not credited",
                "category": "card",
                "priority": "HIGH",
                "status": "OPEN",
                "created_at": "2026-11-06T15:30:00Z",
            },
            {
                "ticket_ref": "VS-OLD22222222222222222222222222222",
                "subject": "Statement request",
                "category": "account",
                "priority": "LOW",
                "status": "RESOLVED",
                "created_at": "2026-10-01T10:00:00Z",
            },
        ]
        result_item = ToolExecutionResult(
            tool_call_id=uuid4(),
            tool_name="list_support_tickets",
            status="succeeded",
            summary="Retrieved support tickets",
            data={"items": tickets},
        )

        query = "Show me my support tickets and the current status of the complaint I just raised."
        formatted = AssistantOrchestrator._format_support_read_results([result_item], "en-IN", input_text=query)

        # Check latest ticket section with all mandatory fields
        self.assertIn("Latest complaint / ticket:", formatted)
        self.assertIn("Reference: VS-NEW11111111111111111111111111111", formatted)
        self.assertIn("Subject: Payment deducted but not credited", formatted)
        self.assertIn("Category: card", formatted)
        self.assertIn("Priority: HIGH", formatted)
        self.assertIn("Status: OPEN", formatted)
        self.assertIn("Created: 2026-11-06T15:30:00Z", formatted)

        # Check compact list of other tickets
        self.assertIn("Other synthetic support tickets:", formatted)
        self.assertIn("VS-OLD22222222222222222222222222222: Statement request — RESOLVED (LOW, account).", formatted)


if __name__ == "__main__":
    unittest.main()
