"""Offline Vaani AI support-tool tests using a temporary SQLite database."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from backend.api_models import AssistantTurnRequest
from services import database
from services.assistant_service import (
    AssistantOrchestrator,
    LLM_TOOL_NAMES,
    _is_support_create_request,
    _llm_tool_catalog,
    _required_tools_for_question,
    _support_category_was_stated,
    _support_staff_workflow_question,
)
from services.support_draft_service import get_active_draft, save_or_update_draft
from services.customer_support_service import create_support_case, get_support_case, list_support_cases
from services.conversation_service import create_conversation, save_assistant_turn
from services.llm_adapters import MockLLMProvider
from services.llm_interfaces import LLMResponse, LLMToolCall
from services.principal import CustomerPrincipal
from services.speech_interfaces import Transcription


def make_principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20)
    )


def make_turn(text, conversation_id):
    return AssistantTurnRequest(
        conversation_id=conversation_id,
        client_turn_id=uuid4(),
        input_mode="text",
        output_mode="text",
        text=text,
    )


class CustomerSupportAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "support-assistant.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.test", "Synthetic Arjun"),
                    ("demo-priya", "priya@example.test", "Synthetic Priya"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.arjun = make_principal()
        self.priya = make_principal("demo-priya")
        self.conversation = create_conversation(self.arjun)["conversation_id"]
        self.current_principal = self.arjun
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
    def ticket_count():
        conn = database.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
        finally:
            conn.close()

    def own_ticket(self, principal_value=None, subject="Own ticket"):
        principal_value = principal_value or self.arjun
        return create_support_case(
            principal_value,
            category="card",
            priority="HIGH",
            subject=subject,
            description="Synthetic support test description.",
        )

    def run_llm(self, text, provider):
        return AssistantOrchestrator(llm_provider=provider).process_turn(
            self.arjun, make_turn(text, self.conversation)
        )

    def test_support_intent_routes_to_owner_scoped_tools(self):
        self.assertEqual(
            _required_tools_for_question("Show my open tickets"),
            frozenset({"list_support_tickets"}),
        )
        self.assertEqual(
            _required_tools_for_question("Check status of VS-0123456789ABCDEF0123456789ABCDEF"),
            frozenset({"get_support_ticket"}),
        )
        self.assertEqual(
            _required_tools_for_question("What happened to VS-0123456789ABCDEF0123456789ABCDEF?"),
            frozenset({"get_support_ticket", "get_support_ticket_events"}),
        )
        self.assertEqual(
            _required_tools_for_question("Please close VS-0123456789ABCDEF0123456789ABCDEF"),
            frozenset({"prepare_support_ticket_close"}),
        )
        self.assertEqual(_required_tools_for_question("शिकायत दर्ज करें"), frozenset())
        self.assertTrue(_is_support_create_request("I want to raise a complaint"))
        self.assertTrue(_is_support_create_request(
            "Please file a high priority card complaint about a failed payment."
        ))
        self.assertTrue(_is_support_create_request(
            "I need help with my card payment issue and have reviewed the account details. "
            "After checking everything, please create a support ticket for this problem."
        ))
        self.assertTrue(_is_support_create_request(
            "ನನ್ನ card payment ಸಮಸ್ಯೆ ಬಗ್ಗೆ support ಬೇಕು, ವಿವರಗಳನ್ನು ಪರಿಶೀಲಿಸಿದ್ದೇನೆ. "
            "ದಯವಿಟ್ಟು ticket create ಮಾಡಿ."
        ))
        self.assertFalse(_is_support_create_request("Please do not create a support ticket."))
        self.assertFalse(_is_support_create_request("I don't want to file a complaint."))
        self.assertTrue(_support_category_was_stated("मेरे कार्ड का भुगतान विफल हुआ", "card"))
        self.assertTrue(_support_category_was_stated("ನನ್ನ ಕಾರ್ಡ್ ಪಾವತಿ ವಿಫಲವಾಗಿದೆ", "card"))
        self.assertTrue(_support_staff_workflow_question("मेरा टिकट किस एजेंट को दिया गया?"))
        self.assertTrue(_support_staff_workflow_question("ನನ್ನ ಟಿಕೆಟ್ ಯಾವ ಸಿಬ್ಬಂದಿಗೆ ನೀಡಲಾಗಿದೆ?"))
        self.assertTrue(_is_support_create_request("शिकायत दर्ज करें"))
        self.assertTrue(_is_support_create_request("ದೂರು ದಾಖಲಿಸು"))
        self.assertTrue({
            "list_support_tickets", "get_support_ticket", "get_support_ticket_events",
            "propose_support_ticket", "prepare_support_ticket_close",
        }.issubset(LLM_TOOL_NAMES))
        catalog = {tool.name: tool for tool in _llm_tool_catalog()}
        for name in (
            "list_support_tickets", "get_support_ticket", "get_support_ticket_events",
            "propose_support_ticket", "prepare_support_ticket_close",
        ):
            self.assertNotIn("customer_id", catalog[name].parameters["properties"])
            self.assertNotIn("customer_key", catalog[name].parameters["properties"])

    def test_explicit_support_creation_precedes_banking_lookup_routing(self):
        phrases = (
            "Prepare a customer support ticket.",
            "Help me raise a support ticket.",
            "My debit card is not working. Please create a support ticket.",
            "I need to report an issue with my account.",
            "I want to raise a complaint.",
        )
        for text in phrases:
            with self.subTest(text=text):
                self.assertTrue(_is_support_create_request(text))
                self.assertEqual(_required_tools_for_question(text), frozenset())

        exact_failing_prompt = (
            "I need help with my debit card. It is not working when I try to make payments. "
            "Please prepare a customer support ticket for me."
        )
        provider = MockLLMProvider([LLMResponse(text="Please share the ticket subject and priority.")])
        response = self.run_llm(exact_failing_prompt, provider)
        supplied_names = {tool.name for tool in provider.requests[0][1]}
        self.assertEqual(supplied_names, {"propose_support_ticket"})
        self.assertNotIn("get_card_details", supplied_names)
        self.assertNotIn("get_recent_transactions", supplied_names)
        self.assertIsNone(response.pending_action)
        self.assertEqual(self.ticket_count(), 0)

    def test_ticket_status_and_ordinary_support_questions_keep_existing_routing(self):
        self.assertFalse(_is_support_create_request("What is the status of my support ticket?"))
        self.assertEqual(
            _required_tools_for_question("What is the status of my support ticket?"),
            frozenset({"list_support_tickets"}),
        )
        self.assertEqual(
            _required_tools_for_question(
                "What is the status of my support ticket VS-0123456789ABCDEF0123456789ABCDEF?"
            ),
            frozenset({"get_support_ticket"}),
        )
        contact_question = "How do I contact customer support?"
        self.assertFalse(_is_support_create_request(contact_question))
        self.assertEqual(_required_tools_for_question(contact_question), frozenset())

    def test_latest_and_recent_ticket_phrasings_route_to_list_support_tickets(self):
        phrases = (
            "Show me my latest support ticket.",
            "What is the status of my most recent ticket?",
            "Check my latest complaint.",
            "Show my recent support tickets.",
            "Show the details of my last ticket.",
            "What happened to my previous ticket?",
            "Show the history of my support ticket.",
            "Can you show me the status of my latest support ticket?",
            "Where is my ticket?",
            "Find my ticket",
            "Track my ticket",
            "What is the update on my ticket?",
            "Show all information about my ticket",
            "What is the status of my latest ticket about my card?",
            "Show my latest ticket about my card payment",
            "Check my latest complaint regarding card transaction",
            "मेरे कार्ड की शिकायत की स्थिति क्या है?",
            "मेरी कार्ड शिकायत जांचें",
            "ನನ್ನ ಕಾರ್ಡ್ ದೂರಿನ ಸ್ಥಿತಿ ಏನು?",
            "ನನ್ನ ಟಿಕೆಟ್ ಎಲ್ಲಿದೆ?",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertFalse(_is_support_create_request(phrase))
                self.assertEqual(
                    _required_tools_for_question(phrase),
                    frozenset({"list_support_tickets"}),
                )

    def test_explicit_ticket_reference_routing_and_banking_precedence(self):
        ref = "VS-0123456789ABCDEF0123456789ABCDEF"
        lookup_cases = (
            f"What is the status of {ref}?",
            f"Show details of {ref}",
            f"Check {ref}",
            f"Track ticket {ref}",
            f"{ref} कार्ड शिकायत की स्थिति",
            f"{ref} ಕಾರ್ಡ್ ದೂರು ಸ್ಥಿತಿ",
        )
        for phrase in lookup_cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(_required_tools_for_question(phrase), frozenset({"get_support_ticket"}))

        history_cases = (
            f"What happened to {ref}?",
            f"Any update on {ref}?",
            f"Status history of {ref}",
            f"Events for {ref}",
        )
        for phrase in history_cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    _required_tools_for_question(phrase),
                    frozenset({"get_support_ticket", "get_support_ticket_events"}),
                )

    def test_ordinary_card_and_payment_queries_keep_banking_tools(self):
        self.assertEqual(
            _required_tools_for_question("What is my debit card status?"),
            frozenset({"get_card_details"}),
        )
        self.assertEqual(
            _required_tools_for_question("Show me my recent payments"),
            frozenset({"get_recent_transactions"}),
        )
        self.assertEqual(
            _required_tools_for_question("Explain the difference between a debit card and a credit card."),
            frozenset(),
        )
        self.assertEqual(
            _required_tools_for_question("How can I report an unauthorized transaction?"),
            frozenset(),
        )
        self.assertEqual(
            _required_tools_for_question("Why was the first suspicious message risky?"),
            frozenset(),
        )
        self.assertEqual(
            _required_tools_for_question("Why was the first suspicious message risky?", assessment_id=uuid4()),
            frozenset(),
        )
        self.assertEqual(
            _required_tools_for_question("Explain my previous scam assessment.", assessment_id=uuid4()),
            frozenset({"get_scam_assessment"}),
        )

    def test_ticket_listing_is_scoped_to_authenticated_customer(self):
        own = self.own_ticket(subject="Arjun card issue")
        other = self.own_ticket(self.priya, subject="Priya private ticket")
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("list_support_tickets", {}, "list-1"),)),
            LLMResponse(text="Your open synthetic ticket is listed."),
        ])
        response = self.run_llm("Show my open tickets", provider)
        self.assertEqual(response.tool_results[0].status, "succeeded")
        self.assertIn(own["ticket_ref"], response.assistant_text)
        self.assertIn("OPEN", response.assistant_text)
        tool_message = provider.requests[1][0][-1]
        self.assertIn(own["ticket_ref"], tool_message.content)
        self.assertNotIn(other["ticket_ref"], tool_message.content)
        self.assertNotIn("case_id", tool_message.content)
        self.assertNotIn("customer_id", tool_message.content)

    def test_other_customers_ticket_reference_is_not_retrievable(self):
        other = self.own_ticket(self.priya, subject="Private Priya case")
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_support_ticket", {"ticket_ref": other["ticket_ref"]}, "get-1"
            ),)),
        ])
        response = self.run_llm(f"Check ticket status {other['ticket_ref']}", provider)
        self.assertEqual(response.tool_results[0].status, "failed")
        self.assertNotIn(other["ticket_ref"], response.assistant_text)
        self.assertNotIn("Private Priya case", response.assistant_text)
        self.assertIn("could not retrieve", response.assistant_text)

    def test_missing_details_are_requested_without_creating_pending_action(self):
        provider = MockLLMProvider([LLMResponse(text="I need more details.")])
        response = self.run_llm("Please file a complaint", provider)
        self.assertIn("category", response.assistant_text)
        self.assertIn("priority", response.assistant_text)
        self.assertIn("No ticket has been created", response.assistant_text)
        self.assertIsNone(response.pending_action)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(len(provider.requests), 1)

    def test_support_details_can_be_collected_across_conversation_turns(self):
        first = self.run_llm(
            "Please file a complaint about my card", MockLLMProvider([LLMResponse(text="Need details.")])
        )
        self.assertIsNone(first.pending_action)
        self.assertIn("priority", first.assistant_text)
        second_provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {"category": "card", "priority": "HIGH", "subject": "Card payment failed"},
            "multi-turn-proposal",
        ),))])
        second = self.run_llm("High priority; my card payment failed.", second_provider)
        self.assertIsNotNone(second.pending_action)
        self.assertEqual(second.pending_action.action.category, "card")
        self.assertEqual(second.pending_action.action.priority, "HIGH")
        self.assertEqual(self.ticket_count(), 0)

    def test_missing_support_details_keep_detected_kannada_and_hindi_language(self):
        examples = (
            ("शिकायत दर्ज करें", "hi-IN"),
            ("ದೂರು ದಾಖಲಿಸು", "kn-IN"),
        )
        for text, language in examples:
            self.conversation = create_conversation(self.arjun)["conversation_id"]
            provider = MockLLMProvider([LLMResponse(text="Please share more details.")])
            response = self.run_llm(text, provider)
            self.assertEqual(response.response_language, language)
            self.assertIsNone(response.pending_action)
            self.assertEqual(self.ticket_count(), 0)

    def test_new_native_language_input_updates_automatic_response_language(self):
        hindi = "\u0936\u093f\u0915\u093e\u092f\u0924 \u0926\u0930\u094d\u091c \u0915\u0930\u0947\u0902"
        kannada = "\u0ca6\u0cc2\u0cb0\u0cc1 \u0ca6\u0cbe\u0c96\u0cb2\u0cbf\u0cb8\u0cc1"
        first = self.run_llm(hindi, MockLLMProvider([LLMResponse(text="Details? ")]))
        self.assertEqual(first.response_language, "hi-IN")
        following = self.run_llm(kannada, MockLLMProvider([LLMResponse(text="Details? ")]))
        self.assertEqual(following.response_language, "kn-IN")

        explicit_request = make_turn("Please file a complaint", self.conversation)
        explicit_request.response_language = "en-IN"
        explicit = AssistantOrchestrator(llm_provider=MockLLMProvider([
            LLMResponse(text="Please share more details."),
        ])).process_turn(self.arjun, explicit_request)
        self.assertEqual(explicit.response_language, "en-IN")
        next_turn = self.run_llm(kannada, MockLLMProvider([LLMResponse(text="Details? ")]))
        self.assertEqual(next_turn.response_language, "kn-IN")

    def test_missing_customer_priority_cannot_be_invented_by_model(self):
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {"category": "card", "priority": "HIGH", "subject": "Card problem"},
            "propose-no-priority",
        ),))])
        response = self.run_llm("Please file a complaint about my card", provider)
        self.assertIsNone(response.pending_action)
        self.assertEqual(self.ticket_count(), 0)
        self.assertIn("priority", response.assistant_text)

    def test_model_cannot_invent_ticket_category_subject_or_description(self):
        cases = (
            (
                "Please file a complaint about my card payment failure, priority high.",
                {"category": "loan", "priority": "HIGH", "subject": "Card payment failure"},
            ),
            (
                "Please file a card complaint about my payment failure, priority high.",
                {"category": "card", "priority": "HIGH", "subject": "Card payment stolen"},
            ),
            (
                "Please file a card complaint about my payment failure, priority high.",
                {"category": "card", "priority": "HIGH", "subject": "Card payment failure", "description": "My account was hacked"},
            ),
        )
        for text, arguments in cases:
            with self.subTest(arguments=arguments):
                provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
                    "propose_support_ticket", arguments, "ungrounded-proposal",
                ),))])
                response = self.run_llm(text, provider)
                self.assertIsNone(response.pending_action)
                self.assertEqual(self.ticket_count(), 0)
                self.assertIn("No ticket has been created", response.assistant_text)

    def test_staff_escalation_and_bank_resolution_are_not_claimed(self):
        provider = MockLLMProvider([LLMResponse(text="I have escalated it to staff.")])
        response = self.run_llm("Who is assigned to my support case, and has the bank resolved it?", provider)
        self.assertIn("not available", response.assistant_text)
        self.assertIn("synthetic demo", response.assistant_text)
        self.assertEqual(provider.requests, [])

    def test_spoken_ticket_request_only_creates_a_pending_proposal(self):
        class FakeTranscriber:
            def transcribe(self, principal_value, upload_id, language_hint):
                self.principal_value = principal_value
                self.upload_id = upload_id
                self.language_hint = language_hint
                return Transcription(
                    "Please file a high priority card complaint about a failed payment.",
                    ("en-IN",),
                )

        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {"category": "card", "priority": "HIGH", "subject": "Failed card payment"},
            "spoken-proposal",
        ),))])
        request = AssistantTurnRequest(
            conversation_id=self.conversation,
            client_turn_id=uuid4(),
            input_mode="speech",
            output_mode="text",
            audio_upload_id=uuid4(),
            language_hint="en-IN",
        )
        response = AssistantOrchestrator(
            transcriber=FakeTranscriber(), llm_provider=provider,
        ).process_turn(self.arjun, request)
        self.assertIsNotNone(response.pending_action)
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertEqual(self.ticket_count(), 0)
        self.assertIsNotNone(get_active_draft(self.arjun.customer_key, str(self.conversation)))

    def test_labeled_voice_ticket_slots_continue_draft_without_transaction_risk_tools(self):
        save_or_update_draft(
            self.arjun.customer_key, str(self.conversation), status="collecting",
        )

        class FakeTranscriber:
            def transcribe(self, _principal, _upload_id, _language_hint):
                return Transcription(
                    "Category is fraud and priority is high. Subject is unauthorised transactions.",
                    ("en-IN",),
                )

        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {
                "category": "transaction", "priority": "HIGH",
                "subject": "Unauthorised transactions",
                "description": "Customer reports unauthorised transactions.",
            },
            "voice-slot-proposal",
        ),))])
        request = AssistantTurnRequest(
            conversation_id=self.conversation, client_turn_id=uuid4(),
            input_mode="speech", output_mode="text", audio_upload_id=uuid4(),
            language_hint="en-IN",
        )

        result = AssistantOrchestrator(
            transcriber=FakeTranscriber(), llm_provider=provider,
        ).process_turn(self.arjun, request)

        self.assertIsNone(result.pending_action)
        offered_tools = provider.requests[0][1]
        self.assertEqual([tool.name for tool in offered_tools], ["propose_support_ticket"])
        self.assertIn("Is the amount deducted", result.assistant_text)
        self.assertEqual(self.ticket_count(), 0)

    def test_yes_after_priority_question_sets_priority_without_debit_or_submission(self):
        save_or_update_draft(
            self.arjun.customer_key, str(self.conversation),
            category="transaction", subject="UPI payment issue",
            description="Customer reported a UPI payment issue.",
            status="collecting", debit_confirmed=False,
        )
        save_assistant_turn(
            self.arjun, self.conversation,
            response_text="Would you like to mark it as high priority?",
            response_language="en-IN", requested_output_mode="text",
        )

        response = self.run_llm("yes", MockLLMProvider([LLMResponse(text="Understood.")]))

        draft = get_active_draft(self.arjun.customer_key, str(self.conversation))
        self.assertEqual(draft["priority"], "HIGH")
        self.assertFalse(bool(draft["debit_confirmed"]))
        self.assertIsNotNone(response.pending_action)
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertEqual(self.ticket_count(), 0)
        self.assertNotIn("share the priority", response.assistant_text.casefold())
        self.assertIn("Please confirm if you would like me to submit", response.assistant_text)

    def _run_spoken_proposal(self, transcript, languages, is_code_mixed):
        class FakeTranscriber:
            def transcribe(self, _principal, _upload_id, _language_hint):
                return Transcription(transcript, languages, is_code_mixed)

        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {"category": "card", "priority": "HIGH", "subject": "Failed card payment"},
            "long-spoken-proposal",
        ),))])
        request = AssistantTurnRequest(
            conversation_id=self.conversation, client_turn_id=uuid4(), input_mode="speech",
            output_mode="text", audio_upload_id=uuid4(), language_hint="auto",
        )
        response = AssistantOrchestrator(
            transcriber=FakeTranscriber(), llm_provider=provider,
        ).process_turn(self.arjun, request)
        self.assertIsNotNone(response.pending_action)
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertEqual(self.ticket_count(), 0)
        return response

    def test_long_english_voice_ticket_request_routes_safely(self):
        text = (
            "I need help with my card payment issue and have already checked the account details. "
            "After reviewing the problem, please create a support ticket for it. "
            "This is high priority because my payment failed."
        )
        response = self._run_spoken_proposal(text, ("en-IN",), False)
        self.assertEqual([item.language for item in response.detected_languages], ["en-IN"])

    def test_long_kannada_english_voice_request_preserves_code_mixed_metadata(self):
        text = (
            "ನನ್ನ card payment ಸಮಸ್ಯೆ ಬಗ್ಗೆ support ಬೇಕು, ಎಲ್ಲಾ ವಿವರಗಳನ್ನು ಪರಿಶೀಲಿಸಿದ್ದೇನೆ. "
            "ದಯವಿಟ್ಟು ticket create ಮಾಡಿ. This is high priority because payment failed."
        )
        response = self._run_spoken_proposal(text, ("kn-IN", "en-IN"), True)
        self.assertEqual(
            [item.language for item in response.detected_languages], ["kn-IN", "en-IN"]
        )
        self.assertTrue(response.is_code_mixed)

    def test_ticket_proposal_is_turn_bound_and_confirmation_creates_ticket_once(self):
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {
                "category": "card",
                "priority": "HIGH",
                "subject": "Card payment failed",
            },
            "propose-1",
        ),))])
        response = self.run_llm(
            "Please file a complaint: card payment failed, priority high.", provider
        )
        pending = response.pending_action
        self.assertIsNotNone(pending)
        self.assertEqual(pending.original_turn_id, response.turn_id)
        self.assertEqual(pending.conversation_id, response.conversation_id)
        self.assertEqual(pending.action.description, "Customer reported: Card payment failed.")
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertEqual(self.ticket_count(), 0)

        confirmation = self.client.post(
            f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
            json={
                "confirmed": True,
                "confirmation_method": "button",
                "conversation_id": str(pending.conversation_id),
                "original_turn_id": str(pending.original_turn_id),
                "action_type": "create_support_ticket",
            },
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        self.assertEqual(confirmation.json()["status"], "executed")
        self.assertEqual(self.ticket_count(), 1)
        self.assertIsNone(get_active_draft(self.arjun.customer_key, str(self.conversation)))
        ticket_ref = confirmation.json()["action_result"]["data"]["ticket_ref"]
        listed = list_support_cases(self.arjun)
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["ticket_ref"], ticket_ref)
        self.assertEqual(
            get_support_case(self.arjun, ticket_ref)["description"],
            "Customer reported: Card payment failed.",
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/assistant/confirmations/{pending.confirmation_id}",
                json={
                    "confirmed": True,
                    "confirmation_method": "button",
                    "conversation_id": str(pending.conversation_id),
                    "original_turn_id": str(pending.original_turn_id),
                    "action_type": "create_support_ticket",
                },
            ).status_code,
            409,
        )
        self.assertEqual(self.ticket_count(), 1)
        self.assertEqual(len(list_support_cases(self.arjun)["items"]), 1)
        unrelated = self.run_llm(
            "How can I plan my savings over the next year?",
            MockLLMProvider([LLMResponse(text="Set a monthly target and review it regularly.")]),
        )
        self.assertIsNone(unrelated.pending_action)
        self.assertIsNone(get_active_draft(self.arjun.customer_key, str(self.conversation)))

    def test_unrelated_card_question_does_not_use_active_support_proposal(self):
        proposed = self.run_llm(
            "Please file a complaint: card payment failed, priority high.",
            MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
                "propose_support_ticket",
                {"category": "card", "priority": "HIGH", "subject": "Card payment failed"},
                "propose-before-general-question",
            ),))]),
        )
        self.assertIsNotNone(proposed.pending_action)
        provider = MockLLMProvider([LLMResponse(text="A debit card uses deposited funds; a credit card uses a credit line.")])
        answer = self.run_llm("Explain the difference between a debit card and a credit card.", provider)
        self.assertIn("debit card", answer.assistant_text.lower())
        self.assertEqual(provider.requests[0][1], ())
        self.assertEqual(self.ticket_count(), 0)

    def test_support_amount_and_subject_context_excludes_assistant_clarifications(self):
        self.run_llm(
            "Please create a complaint: unauthorized debit of INR 2,500, priority high.",
            MockLLMProvider([LLMResponse(text="Please provide a subject.")]),
        )
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "propose_support_ticket",
            {"category": "transaction", "priority": "HIGH", "subject": "upi payment issue"},
            "propose-grounded-subject",
        ),))])
        response = self.run_llm("Subject: upi payment issue", provider)
        self.assertIsNotNone(response.pending_action)
        self.assertEqual(response.pending_action.action.subject, "upi payment issue")
        self.assertEqual(response.pending_action.action.amount, 2500)

    def test_acknowledging_proposal_details_does_not_submit_or_recreate_ticket(self):
        proposed = self.run_llm(
            "Please file a complaint: card payment failed, priority high.",
            MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
                "propose_support_ticket",
                {"category": "card", "priority": "HIGH", "subject": "Card payment failed"},
                "propose-for-acknowledgement",
            ),))]),
        )
        provider = MockLLMProvider([])
        acknowledged = self.run_llm("ok details are correct", provider)
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(acknowledged.pending_action.confirmation_id, proposed.pending_action.confirmation_id)
        self.assertIn("confirmation button", acknowledged.assistant_text)
        self.assertEqual(provider.requests, [])

    def test_close_tool_only_prepares_explicit_button_confirmation(self):
        ticket = self.own_ticket(subject="Close me")
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "prepare_support_ticket_close",
            {"ticket_ref": ticket["ticket_ref"]},
            "close-prepare-1",
        ),))])
        response = self.run_llm(f"Close my ticket {ticket['ticket_ref']}", provider)
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertEqual(response.support_close_confirmation.ticket_ref, ticket["ticket_ref"])
        self.assertIn("No status change", response.assistant_text)
        self.assertEqual(get_support_case(self.arjun, ticket["ticket_ref"])["status"], "OPEN")

        closed = self.client.patch(
            f"/api/v1/me/support/tickets/{ticket['ticket_ref']}/status",
            json={
                "status": "CLOSED",
                "confirmed": True,
                "confirmation_method": "button",
            },
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["status"], "CLOSED")

    def test_already_closed_ticket_gets_recorded_status_without_confirmation(self):
        from services.customer_support_service import close_support_case

        ticket = self.own_ticket(subject="Already closed")
        close_support_case(self.arjun, ticket["ticket_ref"])
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "prepare_support_ticket_close", {"ticket_ref": ticket["ticket_ref"]}, "closed-1"
        ),))])
        response = self.run_llm(f"Please close my ticket {ticket['ticket_ref']}", provider)
        self.assertIsNone(response.support_close_confirmation)
        self.assertIn("recorded as CLOSED", response.assistant_text)
        self.assertIn("no status change was made", response.assistant_text)
        self.assertEqual(response.tool_results[0].status, "succeeded")
        self.assertEqual(get_support_case(self.arjun, ticket["ticket_ref"])["status"], "CLOSED")

    def test_ticket_status_history_uses_owner_scoped_event_service(self):
        from services.customer_support_service import close_support_case

        ticket = self.own_ticket(subject="Status history")
        close_support_case(self.arjun, ticket["ticket_ref"])
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_support_ticket", {"ticket_ref": ticket["ticket_ref"]}, "history-detail"
            ),)),
            LLMResponse(tool_calls=(LLMToolCall(
                "get_support_ticket_events", {"ticket_ref": ticket["ticket_ref"]}, "history-events"
            ),)),
            LLMResponse(text="The ticket is closed and its history is available."),
        ])
        response = self.run_llm(
            f"What happened to {ticket['ticket_ref']}?", provider
        )
        self.assertEqual(
            [result.tool_name for result in response.tool_results],
            ["get_support_ticket", "get_support_ticket_events"],
        )
        self.assertIn("OPEN → CLOSED", response.assistant_text)
        self.assertIn("customer_closed", response.assistant_text)

    def test_model_cannot_supply_customer_identity_as_tool_argument(self):
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "list_support_tickets", {"customer_id": "demo-priya"}, "bad-identity"
        ),))])
        response = self.run_llm("Show my open tickets", provider)
        self.assertEqual(response.tool_results[0].status, "failed")
        self.assertIn("could not retrieve", response.assistant_text)
        self.assertEqual(self.ticket_count(), 0)

    def test_detailed_complaint_prompt_creates_proposal_without_banking_lookups(self):
        prompt = (
            "I want to report a problem with my banking services. My debit card payments are repeatedly failing. "
            "Please register a complaint under Card Services with the subject 'Repeated Debit Card Payment Failures', "
            "priority HIGH, and description 'My debit card transactions are repeatedly being declined at online and offline merchants. "
            "Please investigate the issue and resolve it as soon as possible.' Show me the ticket draft and request my confirmation before submitting it."
        )
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "propose_support_ticket",
                {
                    "category": "card",
                    "subject": "Repeated Debit Card Payment Failures",
                    "priority": "HIGH",
                    "description": "My debit card transactions are repeatedly being declined at online and offline merchants. Please investigate the issue and resolve it as soon as possible.",
                },
                "prop-1",
            ),)),
        ])
        response = self.run_llm(prompt, provider)
        self.assertEqual(len(response.tool_results), 1)
        self.assertEqual(response.tool_results[0].tool_name, "propose_support_ticket")
        self.assertEqual(response.tool_results[0].status, "needs_confirmation")
        self.assertIsNotNone(response.pending_action)
        self.assertEqual(response.pending_action.action.subject, "Repeated Debit Card Payment Failures")
        self.assertEqual(response.pending_action.action.category, "card")
        self.assertEqual(response.pending_action.action.priority, "HIGH")
        self.assertEqual(self.ticket_count(), 0)
        self.assertEqual(provider.requests[0][3], frozenset({"propose_support_ticket"}))


if __name__ == "__main__":
    unittest.main()
