"""Offline tests for provider-neutral LLM orchestration and tool safety."""

import tempfile
import unittest
import os
import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from services import database
from dotenv import load_dotenv
from services.assistant_service import AssistantOrchestrator, AssistantServiceError, LLM_TOOL_NAMES, _history_turn_limit, _orchestration_round_limit, _required_tools_for_question, _response_language_for_text, _memory_summary_sources, _parse_conversation_memory, _select_relevant_memory, _memory_tools_for_follow_up, _resolve_contextual_follow_up, _extract_banking_contexts, _month_period_from_text, speech_friendly_text
from services.conversation_service import create_conversation, list_conversations, save_assistant_turn, save_user_turn, get_conversation_history, get_conversation_memory, update_conversation_memory, ConversationNotFoundError
from services.llm_adapters import GeminiLLMProvider, GroqLLMProvider, MockLLMProvider
from services.llm_interfaces import LLMMessage, LLMProviderError, LLMRateLimited, LLMRequestTooLarge, LLMResponse, LLMTimeout, LLMTool, LLMToolCall
from services.llm_registry import create_llm_provider, get_provider_settings
from services.principal import CustomerPrincipal
from services.speech_adapters import GnaniPrismaTranscriber
from services.speech_interfaces import InputProcessingUnavailable


def make_principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20))


def make_request(text="List my spending", conversation_id=None):
    from backend.api_models import AssistantTurnRequest
    return AssistantTurnRequest(
        conversation_id=conversation_id,
        client_turn_id=uuid4(), input_mode="text", output_mode="text", text=text,
    )


def groq_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def groq_response(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=tool_calls or [],
    ))])


def mock_groq_client(responses):
    class Completions:
        def __init__(self):
            self.responses = list(responses)
            self.requests = []
        def create(self, **kwargs):
            self.requests.append(kwargs)
            if not self.responses:
                raise AssertionError("Unexpected Groq completion request")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class LLMOrchestrationTests(unittest.TestCase):
    def setUp(self):
        import services.llm_adapters as adapters
        adapters._GEMINI_RATE_LIMIT_UNTIL = 0.0
        adapters._GROQ_RATE_LIMIT_UNTIL = 0.0
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "llm.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [("demo-arjun", "arjun@example.com", "Arjun Demo"),
                 ("demo-priya", "priya@example.com", "Priya Demo")],
            )
            conn.commit()
        finally:
            conn.close()
        self.principal = make_principal()

    def tearDown(self):
        import services.llm_adapters as adapters
        adapters._GEMINI_RATE_LIMIT_UNTIL = 0.0
        adapters._GROQ_RATE_LIMIT_UNTIL = 0.0
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _scam_assessment_count(self):
        conn = database.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM scam_assessments").fetchone()[0]
        finally:
            conn.close()

    def test_scam_intent_requires_deterministic_assessment_tool(self):
        self.assertEqual(
            _required_tools_for_question("Can you assess this message?"),
            frozenset({"assess_current_scam_message"}),
        )
        self.assertEqual(
            _required_tools_for_question("\u092f\u0939 \u0938\u094d\u0915\u0948\u092e \u0939\u0948?"),
            frozenset({"assess_current_scam_message"}),
        )
        self.assertEqual(
            _required_tools_for_question("\u0cb8\u0c82\u0ca6\u0cc7\u0cb6 \u0caa\u0cb0\u0cbf\u0cb6\u0cc0\u0cb2\u0cbf\u0cb8\u0cbf"),
            frozenset({"assess_current_scam_message"}),
        )
        for name in ("assess_current_scam_message", "get_scam_assessment"):
            tool = next(tool for tool in __import__(
                "services.assistant_service", fromlist=["_llm_tool_catalog"]
            )._llm_tool_catalog() if tool.name == name)
            self.assertEqual(tool.parameters["properties"], {})
            self.assertFalse(tool.parameters.get("required"))

    def test_scam_text_tool_result_is_authoritative_and_principal_is_server_context(self):
        content = (
            "Message to check: We are calling from your bank. Your account will be "
            "blocked in 10 minutes unless you send your OTP now. Is this a scam?"
        )
        provider = MockLLMProvider([LLMResponse(
            text="This is LOW risk and safe.",
            tool_calls=(LLMToolCall("assess_current_scam_message", {}, "scam-call-1"),),
        )])
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        actual_execute = __import__("services.agent_tools", fromlist=["execute_tool"]).execute_tool
        with patch("services.assistant_service.execute_tool", wraps=actual_execute) as execute:
            response = orchestrator.process_turn(self.principal, make_request(content))

        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:3], (
            "assess_current_scam_message", self.principal, {},
        ))
        self.assertEqual(execute.call_args.kwargs["current_user_text"], content)
        self.assertEqual(response.scam_assessment.risk_level, "CRITICAL")
        self.assertIn("Risk level: CRITICAL", response.assistant_text)
        self.assertNotIn("LOW risk and safe", response.assistant_text)
        self.assertIn("not a verified finding", response.assistant_text)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(response.tool_results[0].status, "succeeded")

    def test_explicit_analyze_requests_route_exact_source_through_assessment_tool(self):
        messages = (
            ("Analyze: ", "Your bank account will be blocked today. Share your OTP immediately to complete KYC."),
            ("Analyze this message: ", "Your grocery order has been delivered. Thank you for shopping."),
            ("Check whether this SMS is a scam: ", "Your KYC will expire today. Visit http://secure-bank-verify.example to prevent account suspension."),
            ("Screen this text for phishing: ", "Your friend has shared the photos from yesterday."),
        )
        expected_risks = ("HIGH", "LOW", "HIGH", "LOW")
        conversation_id = None
        ids = set()
        assessments = []
        for index, ((prefix, source_text), expected_risk) in enumerate(zip(messages, expected_risks), start=1):
            provider = MockLLMProvider([LLMResponse(
                text="The message is harmless.",
                tool_calls=(LLMToolCall("assess_current_scam_message", {}, f"explicit-scam-{index}"),),
            )])
            orchestrator = AssistantOrchestrator(llm_provider=provider)
            request = make_request(prefix + source_text, conversation_id)
            actual_execute = __import__("services.assistant_service", fromlist=["execute_tool"]).execute_tool
            with patch("services.assistant_service.execute_tool", wraps=actual_execute) as execute:
                response = orchestrator.process_turn(self.principal, request)
            conversation_id = response.conversation_id

            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0], "assess_current_scam_message")
            self.assertEqual(execute.call_args.kwargs["current_user_text"], source_text)
            self.assertEqual(response.scam_assessment.risk_level, expected_risk)
            self.assertEqual(response.tool_results[0].tool_name, "assess_current_scam_message")
            self.assertEqual(response.tool_results[0].status, "succeeded")
            self.assertIn(f"Risk level: {expected_risk}", response.assistant_text)
            self.assertNotIn("The message is harmless.", response.assistant_text)
            self.assertNotIn("GROCERIES", response.assistant_text)
            ids.add(str(response.scam_assessment.assessment_id))
            assessments.append(response.scam_assessment)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len({item.created_at for item in assessments}), 4)
        self.assertEqual(assessments[1].indicators, [])
        self.assertEqual(assessments[3].indicators, [])

    def test_fraudulent_question_prefix_extracts_following_message_exactly(self):
        source = "Your grocery order has been delivered. Thank you for shopping."
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "assess_current_scam_message", {}, "fraudulent-question-scam",
        ),))])
        actual_execute = __import__("services.assistant_service", fromlist=["execute_tool"]).execute_tool
        with patch("services.assistant_service.execute_tool", wraps=actual_execute) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Is this message fraudulent? " + source),
            )
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["current_user_text"], source)
        self.assertEqual(response.scam_assessment.risk_level, "LOW")

    def test_ambiguous_notification_text_asks_intent_instead_of_running_spending_tools(self):
        provider = MockLLMProvider([])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal,
            make_request("Your grocery order has been delivered. Thank you for shopping."),
        )
        self.assertIn("Would you like me to analyze this message", response.assistant_text)
        self.assertEqual(response.tool_results, [])
        self.assertIsNone(response.scam_assessment)
        self.assertEqual(provider.requests, [])

    def test_voice_transcript_uses_same_explicit_scam_route_and_exact_text(self):
        from backend.api_models import AssistantTurnRequest
        from services.speech_interfaces import Transcription

        source_text = "Your grocery order has been delivered. Thank you for shopping."
        transcript = "Analyze: " + source_text

        class Transcriber:
            def transcribe(self, _principal, _upload_id, _language_hint):
                return Transcription(transcript, ("en-IN",))

        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "assess_current_scam_message", {}, "voice-explicit-scam",
        ),))])
        request = AssistantTurnRequest(
            client_turn_id=uuid4(), input_mode="speech", output_mode="text",
            audio_upload_id=uuid4(), language_hint="en-IN",
        )
        actual_execute = __import__("services.assistant_service", fromlist=["execute_tool"]).execute_tool
        with patch("services.assistant_service.execute_tool", wraps=actual_execute) as execute:
            response = AssistantOrchestrator(
                llm_provider=provider, transcriber=Transcriber(),
            ).process_turn(self.principal, request)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["current_user_text"], source_text)
        self.assertEqual(response.scam_assessment.risk_level, "LOW")
        self.assertEqual(response.tool_results[0].tool_name, "assess_current_scam_message")

    def test_failed_transaction_phrasing_uses_authenticated_status_filtered_recent_read(self):
        phrases = (
            "List last my failed transactions",
            "Show my failed transactions",
            "List my recent failed payments",
        )
        rows = [
            {"merchant": f"Merchant {i}", "amount": float(i * 10),
             "status": "FAILED" if i % 2 else "SUCCESS", "transaction_date": f"2026-01-{i:02d}"}
            for i in range(1, 21)
        ]
        for index, phrase in enumerate(phrases, start=1):
            provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 1}, f"failed-transactions-{index}",
            ),))])
            with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
                response = AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request(phrase),
                )
            self.assertEqual(_required_tools_for_question(phrase), frozenset({"get_recent_transactions"}))
            execute.assert_called_once_with(
                "get_recent_transactions", self.principal, {"limit": 100},
            )
            result_rows = response.tool_results[0].data["result"]
            self.assertTrue(all(row["status"] == "FAILED" for row in result_rows))
            self.assertEqual(len(result_rows), 10)
            self.assertIn("Merchant 1", response.assistant_text)
            self.assertNotIn("Merchant 2", response.assistant_text)

    def test_failed_transaction_limit_empty_result_and_service_failure_are_distinct(self):
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "get_recent_transactions", {"limit": 5}, "failed-limit",
        ),))])
        rows = [
            {"merchant": f"Failed {i}", "amount": 1.0,
             "status": "FAILED", "transaction_date": "2026-01-01"}
            for i in range(8)
        ]
        with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("List last 2 failed transactions"),
            )
        execute.assert_called_once_with(
            "get_recent_transactions", self.principal, {"limit": 100},
        )
        self.assertEqual(len(response.tool_results[0].data["result"]), 2)
        self.assertEqual(response.assistant_text.count(" · FAILED"), 2)

        empty_provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "get_recent_transactions", {}, "failed-empty",
        ),))])
        with patch("services.assistant_service.execute_tool", return_value=[]) as execute_empty:
            empty = AssistantOrchestrator(llm_provider=empty_provider).process_turn(
                self.principal, make_request("Show my failed transactions"),
            )
        execute_empty.assert_called_once()
        self.assertIn("No failed transactions were found", empty.assistant_text)
        self.assertNotIn("could not retrieve", empty.assistant_text.casefold())

        failed_provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "get_recent_transactions", {}, "failed-service-error",
        ),))])
        with patch("services.assistant_service.execute_tool", side_effect=RuntimeError("database offline")) as execute_failed:
            failed = AssistantOrchestrator(llm_provider=failed_provider).process_turn(
                self.principal, make_request("Show my failed transactions"),
            )
        execute_failed.assert_called_once()
        self.assertEqual(failed.tool_results[0].status, "failed")
        self.assertIn("could not retrieve failed transaction records", failed.assistant_text)
        self.assertNotIn("No failed transactions were found", failed.assistant_text)

    def test_failed_transaction_text_and_voice_transcript_share_filter_and_limit(self):
        from backend.api_models import AssistantTurnRequest
        from services.speech_interfaces import Transcription

        query = "List last 2 failed transactions"
        expected_rows = [
            {"merchant": "Failed A", "amount": 10.0, "status": "FAILED", "transaction_date": "2026-01-01"},
            {"merchant": "Failed B", "amount": 20.0, "status": "DECLINED", "transaction_date": "2026-01-02"},
        ]
        outputs = []
        for voice in (False, True):
            provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 1}, f"failed-parity-{voice}",
            ),))])
            transcriber = None
            if voice:
                class Transcriber:
                    def transcribe(self, _principal, _upload_id, _language_hint):
                        return Transcription(query, ("en-IN",))
                transcriber = Transcriber()
                request = AssistantTurnRequest(
                    client_turn_id=uuid4(), input_mode="speech", output_mode="text",
                    audio_upload_id=uuid4(), language_hint="en-IN",
                )
            else:
                request = make_request(query)
            with patch("services.assistant_service.execute_tool", return_value=expected_rows) as execute:
                response = AssistantOrchestrator(
                    llm_provider=provider, transcriber=transcriber,
                ).process_turn(self.principal, request)
            execute.assert_called_once_with(
                "get_recent_transactions", self.principal, {"limit": 100},
            )
            outputs.append((response.assistant_text, response.tool_results[0].data["result"]))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(outputs[0][1]), 2)

    def test_new_banking_and_scam_intents_do_not_reuse_old_financial_answers(self):
        conversation_id = None
        financial_provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_category_breakdown", {}, "old-category-read",
            ),)),
            LLMResponse(text="GROCERIES ₹12,950; SHOPPING ₹11,550."),
        ])
        with patch("services.assistant_service.execute_tool", return_value=[
            {"category": "GROCERIES", "amount": 12950},
            {"category": "SHOPPING", "amount": 11550},
        ]):
            financial = AssistantOrchestrator(llm_provider=financial_provider).process_turn(
                self.principal, make_request("Show my spending by category"),
            )
        conversation_id = financial.conversation_id

        banking_provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 5}, "fresh-transaction-read",
            ),)),
            LLMResponse(text="The authenticated transaction records are ready."),
        ])
        tx_rows = [{
            "transaction_date": "2026-11-01", "merchant": "Fresh Market",
            "amount": 90.0, "status": "SUCCESS", "transaction_ref": "TR-" + "A" * 48,
        }]
        with patch("services.assistant_service.execute_tool", return_value=tx_rows) as read:
            banking = AssistantOrchestrator(llm_provider=banking_provider).process_turn(
                self.principal,
                make_request("List my 5 transaction recent", conversation_id),
            )
        read.assert_called_once_with("get_recent_transactions", self.principal, {"limit": 5})
        banking_prompt = " ".join(message.content for message in banking_provider.requests[0][0])
        self.assertIn("List my 5 transaction recent", banking_prompt)
        self.assertNotIn("GROCERIES", banking_prompt)
        self.assertNotIn("12,950", banking_prompt)
        self.assertEqual(len(banking.tool_results[0].data["result"]), 1)

        scam_provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "assess_current_scam_message", {}, "fresh-scam-after-banking",
        ),))])
        source = "Your grocery order has been delivered. Thank you for shopping."
        actual_execute = __import__("services.assistant_service", fromlist=["execute_tool"]).execute_tool
        with patch("services.assistant_service.execute_tool", wraps=actual_execute) as assess:
            scam = AssistantOrchestrator(llm_provider=scam_provider).process_turn(
                self.principal, make_request("Analyze: " + source, conversation_id),
            )
        assess.assert_called_once()
        self.assertEqual(assess.call_args.kwargs["current_user_text"], source)
        scam_prompt = " ".join(message.content for message in scam_provider.requests[0][0])
        self.assertNotIn("Fresh Market", scam_prompt)
        self.assertNotIn("GROCERIES", scam_prompt)
        self.assertEqual(scam.scam_assessment.risk_level, "LOW")
        self.assertEqual([item.tool_name for item in scam.tool_results], ["assess_current_scam_message"])

    def test_scam_request_without_message_asks_for_content_without_provider_call(self):
        for question in (
            "Is this a scam message?",
            "ಇದು scamನಾ?",
            "क्या यह स्कैम है?",
        ):
            provider = MockLLMProvider([])
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request(question)
            )
            self.assertIn("Please share the suspicious message", response.assistant_text)
            self.assertIsNone(response.scam_assessment)
            self.assertEqual(response.tool_results, [])
            self.assertEqual(provider.requests, [])
        self.assertEqual(self._scam_assessment_count(), 0)

    def test_speech_transcript_and_code_mixed_language_metadata_reach_assessment(self):
        from services.speech_interfaces import Transcription

        class Transcriber:
            def transcribe(self, principal, upload_id, language_hint):
                return Transcription(
                    "ನಿಮ್ಮ account block ಆಗುತ್ತದೆ, OTP ಕಳುಹಿಸಿ",
                    ("kn-IN", "en-IN"), True,
                )

        from backend.api_models import AssistantTurnRequest
        request = AssistantTurnRequest(
            client_turn_id=uuid4(), input_mode="speech", output_mode="text",
            audio_upload_id=uuid4(), language_hint="auto",
        )
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "assess_current_scam_message", {}, "voice-scam-call",
        ),))])
        response = AssistantOrchestrator(
            llm_provider=provider, transcriber=Transcriber(),
        ).process_turn(self.principal, request)
        self.assertEqual(response.transcript, "ನಿಮ್ಮ account block ಆಗುತ್ತದೆ, OTP ಕಳುಹಿಸಿ")
        self.assertTrue(response.is_code_mixed)
        self.assertEqual(set(response.scam_assessment.detected_languages), {"kn-IN", "en-IN"})
        self.assertTrue(response.scam_assessment.is_code_mixed)
        self.assertEqual(response.scam_assessment.source_type, "voice_call")

    def test_reusing_voice_assessment_is_owner_scoped_and_does_not_create_a_copy(self):
        from services.scam_assessment_service import create_assessment

        existing = create_assessment(
            self.principal,
            "Bank says account blocked unless OTP is sent immediately.",
            _detected_languages=("en-IN",),
        )
        before = self._scam_assessment_count()
        provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "get_scam_assessment", {}, "assessment-call-1",
        ),))])
        request = make_request("Explain my previous scam assessment.")
        request.scam_assessment_id = existing["assessment_id"]
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal, request,
        )
        self.assertEqual(str(response.scam_assessment.assessment_id), existing["assessment_id"])
        self.assertEqual(self._scam_assessment_count(), before)
        self.assertEqual(response.tool_results[0].tool_name, "get_scam_assessment")

        other_principal = make_principal("demo-priya")
        isolated_provider = MockLLMProvider([LLMResponse(tool_calls=(LLMToolCall(
            "get_scam_assessment", {}, "assessment-call-2",
        ),))])
        isolated_request = make_request("Explain my previous scam assessment.")
        isolated_request.scam_assessment_id = existing["assessment_id"]
        isolated = AssistantOrchestrator(llm_provider=isolated_provider).process_turn(
            other_principal, isolated_request,
        )
        self.assertIsNone(isolated.scam_assessment)
        self.assertEqual(isolated.tool_results[0].status, "failed")
        self.assertNotIn(str(existing["assessment_id"]), isolated.assistant_text)

    def test_repeated_scam_tool_call_creates_only_one_assessment(self):
        provider = MockLLMProvider([LLMResponse(tool_calls=(
            LLMToolCall("assess_current_scam_message", {}, "scam-call-1"),
            LLMToolCall("assess_current_scam_message", {}, "scam-call-2"),
        ))])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal,
            make_request("Please assess this message: send your OTP or the account is blocked."),
        )
        self.assertEqual(self._scam_assessment_count(), 1)
        self.assertEqual(len(response.tool_results), 2)
        ids = {item.data["assessment_id"] for item in response.tool_results}
        self.assertEqual(ids, {str(response.scam_assessment.assessment_id)})

    def test_invalid_or_unknown_scam_tool_requests_never_create_assessment(self):
        for call in (
            LLMToolCall("execute_sql", {"query": "SELECT *"}, "unknown-call"),
            LLMToolCall("assess_current_scam_message", {"message_text": "forged"}, "bad-args"),
        ):
            provider = MockLLMProvider([LLMResponse(tool_calls=(call,))])
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal,
                make_request("Check this scam message: send OTP now."),
            )
            self.assertIsNone(response.scam_assessment)
            self.assertEqual(response.tool_results[0].status, "failed")
            self.assertEqual(self._scam_assessment_count(), 0)

    def test_simple_spending_request_exposes_only_the_summary_tool(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_spending_summary", {}, "call-1"),)),
            LLMResponse(text="Your spending summary is ready."),
        ])
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value={"total_debits": 1200}) as execute:
            response = orchestrator.process_turn(
                self.principal, make_request("List my spending")
            )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual([r.tool_name for r in response.tool_results], ["get_spending_summary"])
        self.assertEqual(response.tool_results[0].data, {"total_debits": 1200})
        self.assertIn("Total debits: ₹1,200.00", response.assistant_text)
        saved = get_conversation_history(self.principal, response.conversation_id)
        self.assertEqual([turn["role"] for turn in saved["items"]], ["user", "assistant"])
        self.assertEqual(saved["items"][-1]["assistant_response_text"], response.assistant_text)
        self.assertEqual({tool.name for tool in provider.requests[0][1]}, {"get_spending_summary"})
        self.assertEqual(len(provider.requests), 2)
        self.assertNotIn("get_customer_profile", {tool.name for tool in provider.requests[0][1]})
        self.assertNotIn("get_customer_context", {tool.name for tool in provider.requests[0][1]})
        self.assertTrue({"get_card_details", "get_loan_details"}.issubset(LLM_TOOL_NAMES))

    def test_explicit_spending_overview_reads_summary_categories_and_merchants(self):
        question = "Show my spending summary, spending categories, and top merchants."
        expected = {
            "get_spending_summary", "get_category_breakdown", "get_top_merchants",
        }
        self.assertEqual(_required_tools_for_question(question), expected)

        provider = MockLLMProvider([])
        outputs = {
            "get_spending_summary": {"total_debits": 1200},
            "get_category_breakdown": [{"category": "Food", "amount": 500}],
            "get_top_merchants": [{"merchant": "Demo Shop", "total_spent": 500}],
        }
        with patch(
            "services.assistant_service.execute_tool",
            side_effect=lambda name, _principal, _arguments: outputs[name],
        ) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request(question)
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual({item.tool_name for item in response.tool_results}, expected)
        self.assertEqual(provider.requests, [])
        for section in ("Spending summary:", "Spending by category:", "Top merchants:"):
            self.assertIn(section, response.assistant_text)

    def test_llm_context_uses_latest_twelve_and_includes_current_user_once(self):
        conversation = create_conversation(self.principal)
        conversation_id = conversation["conversation_id"]
        for pair in range(1, 26):
            save_user_turn(
                self.principal, conversation_id, input_type="text",
                text_or_transcript=f"Earlier question {pair}",
                requested_output_mode="text",
            )
            save_assistant_turn(
                self.principal, conversation_id,
                response_text=(
                    "VaaniShield D is successfully testing ಜ್ಞಾನಿ Timber."
                    if pair == 6 else f"Earlier answer {pair}"
                ),
                requested_output_mode="text",
            )

        current_question = "Show me my last five transactions."
        provider = MockLLMProvider([
            LLMResponse(text=json.dumps({
                "preferences": [], "goals": [], "ongoing_tasks": [],
                "decisions": [], "topic_refs": [],
            })),
            LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 5}, "recent-turn-call",
            ),)),
            LLMResponse(text="Here are your recent transactions."),
        ])
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value=[]) as execute:
            response = orchestrator.process_turn(
                self.principal,
                make_request(current_question, conversation_id),
            )

        initial_messages = provider.requests[1][0]
        self.assertEqual(len(initial_messages), 12)
        self.assertEqual(initial_messages[-1].role, "user")
        self.assertEqual(initial_messages[-1].content, current_question)
        self.assertEqual(
            sum(message.content == current_question for message in initial_messages), 1
        )
        follow_up_messages = provider.requests[2][0]
        self.assertEqual(
            sum(message.content == current_question for message in follow_up_messages), 1
        )
        self.assertNotIn(
            "VaaniShield D is successfully testing ಜ್ಞಾನಿ Timber.",
            [message.content for message in initial_messages],
        )
        self.assertEqual(initial_messages[0].content, "Earlier answer 20")
        execute.assert_called_once_with(
            "get_recent_transactions", self.principal, {"limit": 5}
        )
        self.assertEqual(str(response.conversation_id), conversation_id)

    def test_conversation_memory_is_bounded_selective_and_never_a_banking_source(self):
        from services.assistant_service import _redact_memory_text
        summary = _parse_conversation_memory(json.dumps({
            "preferences": ["Please use concise explanations"],
            "goals": ["User plans to save INR 10,000 monthly"],
            "ongoing_tasks": ["Discuss holiday planning"],
            "decisions": ["Current balance is INR 99,999"],
            "topic_refs": ["Earlier topic involved get_recent_transactions"],
            "banking_topics": ["get_recent_transactions", "DROP TABLE customers"],
        }))
        self.assertEqual(summary["preferences"], ["Please use concise explanations"])
        self.assertEqual(summary["goals"], ["User plans to save INR 10,000 monthly"])
        self.assertEqual(summary["decisions"], [])
        self.assertEqual(summary["banking_topics"], ["get_recent_transactions"])
        self.assertNotIn("OTP 123456", _redact_memory_text("OTP 123456"))
        selected = _select_relevant_memory(summary, "Can you keep explanations concise?")
        self.assertTrue(any("concise" in item for item in selected))
        self.assertEqual(_select_relevant_memory(summary, "What is the weather tomorrow?"), [])
        self.assertEqual(
            _memory_tools_for_follow_up("Which one was largest?", summary),
            frozenset({"find_large_transactions"}),
        )

    def test_remembered_savings_goal_allows_simple_arithmetic_without_banking_tools(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        for index in range(14):
            save_user_turn(
                self.principal, conversation_id, input_type="text",
                text_or_transcript=("Remember that my monthly savings goal is INR 12,000."
                                    if index == 0 else f"Earlier planning question {index}"),
                requested_output_mode="text",
            )
            save_assistant_turn(
                self.principal, conversation_id, response_text=f"Planning response {index}",
                requested_output_mode="text",
            )
        summary = json.dumps({
            "preferences": [], "goals": ["User's monthly savings goal is INR 12,000"],
            "ongoing_tasks": [], "decisions": [], "topic_refs": [],
            "banking_topics": [], "banking_contexts": [],
        })
        self.assertTrue(update_conversation_memory(
            self.principal, conversation_id, summary_json=summary,
            expected_through_sequence=0, through_sequence=28,
        ))
        provider = MockLLMProvider([LLMResponse(
            text="Your conversational goal is INR 12,000 per month, or INR 96,000 over eight months."
        )])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal,
            make_request("What was my savings goal, and how much is that over eight months?", conversation_id),
        )
        self.assertIn("96,000", response.assistant_text)
        self.assertEqual(provider.requests[0][1], ())
        self.assertTrue(any("12,000" in message.content for message in provider.requests[0][0]))

    def test_memory_summary_excludes_support_proposals_and_financial_answers(self):
        turns = [
            {"sequence_number": 1, "role": "user", "user_text_or_transcript": "Create a support ticket for a failed payment"},
            {"sequence_number": 2, "role": "assistant", "assistant_response_text": "I propose a high priority ticket for ₹5,000"},
            {"sequence_number": 3, "role": "user", "user_text_or_transcript": "Priority: urgent"},
            {"sequence_number": 4, "role": "assistant", "assistant_response_text": "Please confirm the support ticket"},
            {"sequence_number": 5, "role": "user", "user_text_or_transcript": "What is my account balance?"},
            {"sequence_number": 6, "role": "assistant", "assistant_response_text": "Your balance is ₹123456"},
        ]
        sources, topics = _memory_summary_sources(turns)
        serialized = json.dumps(sources)
        self.assertNotIn("ticket", serialized.casefold())
        self.assertNotIn("₹5,000", serialized)
        self.assertNotIn("₹123456", serialized)
        self.assertIn("get_account_balance", topics)

    def test_long_history_summary_persists_and_preserves_recent_messages(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        for index in range(14):
            save_user_turn(
                self.principal, conversation_id, input_type="text",
                text_or_transcript=(
                    "I prefer concise explanations" if index == 0 else
                    "Show my transactions at DMart last month" if index == 1 else
                    f"Old discussion item {index}"
                ),
                requested_output_mode="text",
            )
            save_assistant_turn(
                self.principal, conversation_id,
                response_text=("I will keep future replies concise" if index == 0 else f"Response item {index}"),
                requested_output_mode="text",
            )
        provider = MockLLMProvider([
            LLMResponse(text=json.dumps({
                "preferences": ["User prefers concise explanations"],
                "goals": [], "ongoing_tasks": [], "decisions": [], "topic_refs": [],
            })),
            LLMResponse(text="I will keep the answer concise."),
        ])
        response = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal, make_request("Please keep explanations concise", conversation_id),
        )
        memory = get_conversation_memory(self.principal, conversation_id)
        self.assertGreater(memory["through_sequence"], 0)
        self.assertIn("concise", memory["summary_json"])
        parsed_memory = _parse_conversation_memory(memory["summary_json"])
        merchant_context = next(
            item for item in parsed_memory["banking_contexts"]
            if item.get("merchant") == "DMart"
        )
        self.assertEqual(merchant_context["period"], _month_period_from_text("last month"))
        self.assertEqual(len(provider.requests), 2)
        first_messages = provider.requests[1][0]
        self.assertEqual(sum(m.content == "Please keep explanations concise" for m in first_messages), 1)
        self.assertTrue(any(m.content == "Response item 13" for m in first_messages))
        self.assertTrue(any("Conversation memory" in m.content for m in first_messages))
        self.assertEqual(str(response.conversation_id), conversation_id)

    def test_contextual_failed_transaction_filter_uses_fresh_scoped_tool_results(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        save_user_turn(
            self.principal, conversation_id, input_type="text",
            text_or_transcript="Show my last 5 transactions", requested_output_mode="text",
        )
        save_assistant_turn(
            self.principal, conversation_id,
            response_text="I found your recent transactions.", requested_output_mode="text",
        )
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 1}, "failed-follow-up-call",
            ),)),
            LLMResponse(text="Here are the failed transactions from your latest five records."),
        ])
        rows = [
            {"amount": 100, "status": "SUCCESS", "merchant": "Shop A"},
            {"amount": 70, "status": "FAILED", "merchant": "Shop B"},
            {"amount": 20, "status": "DECLINED", "merchant": "Shop C"},
        ]
        with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Show only the failed ones", conversation_id),
            )
        execute.assert_called_once_with(
            "get_recent_transactions", self.principal, {"limit": 5},
        )
        result = next(item for item in response.tool_results if item.tool_name == "get_recent_transactions")
        filtered = result.data["result"]
        self.assertEqual([item["status"] for item in filtered], ["FAILED", "DECLINED"])
        self.assertTrue(all(item["merchant"] != "Shop A" for item in filtered))

    def test_merchant_spending_follow_up_uses_exact_successful_debit_total(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        save_user_turn(
            self.principal, conversation_id, input_type="text",
            text_or_transcript="Show my transactions at DMart", requested_output_mode="text",
        )
        save_assistant_turn(
            self.principal, conversation_id, response_text="DMart transactions were retrieved.",
            requested_output_mode="text",
        )
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "search_transactions", {}, "merchant-follow-up-call",
            ),)),
            LLMResponse(text="You spent INR 150 at DMart."),
        ])
        rows = [
            {"amount": 120, "status": "SUCCESS", "transaction_type": "DEBIT", "merchant": "DMart"},
            {"amount": 20, "status": "FAILED", "transaction_type": "DEBIT", "merchant": "DMart"},
            {"amount": 50, "status": "SUCCESS", "transaction_type": "CREDIT", "merchant": "DMart"},
            {"amount": 30, "status": "SUCCESS", "transaction_type": "DEBIT", "merchant": "DMart"},
        ]
        with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("How much did I spend there?", conversation_id),
            )
        execute.assert_called_once_with(
            "search_transactions", self.principal,
            {"merchant": "DMart", "transaction_type": "DEBIT"},
        )
        result = next(item for item in response.tool_results if item.tool_name == "search_transactions")
        summary = result.data
        self.assertEqual(summary["total_spent"], 150.0)
        self.assertEqual(summary["transaction_count"], 2)
        self.assertEqual([item["amount"] for item in summary["transactions"]], [120, 30])

    def test_memory_summary_provider_failure_does_not_fail_turn_or_advance_watermark(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        for index in range(14):
            save_user_turn(
                self.principal, conversation_id, input_type="text",
                text_or_transcript=f"I prefer concise response style item {index}",
                requested_output_mode="text",
            )
            save_assistant_turn(
                self.principal, conversation_id, response_text=f"Response item {index}",
                requested_output_mode="text",
            )
        provider = MockLLMProvider([
            LLMProviderError("private provider detail"),
            LLMResponse(text="A normal response."),
        ])
        with self.assertLogs("services.assistant_service", level="INFO") as logs:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("An unrelated query", conversation_id),
            )
        memory = get_conversation_memory(self.principal, conversation_id)
        self.assertEqual(memory["through_sequence"], 0)
        self.assertIsNone(memory["summary_json"])
        self.assertNotIn("private provider detail", "\n".join(logs.output))
        self.assertEqual(response.assistant_text, "A normal response.")

    def test_memory_is_owner_scoped_and_banking_follow_up_fetches_fresh_data(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        save_user_turn(
            self.principal, conversation_id, input_type="text",
            text_or_transcript="Show my recent transactions", requested_output_mode="text",
        )
        save_assistant_turn(
            self.principal, conversation_id, response_text="Transactions were retrieved.",
            requested_output_mode="text",
        )
        update_conversation_memory(
            self.principal, conversation_id,
            summary_json=json.dumps({
                "preferences": [], "goals": [], "ongoing_tasks": [], "decisions": [],
                "topic_refs": [], "banking_topics": ["get_recent_transactions"],
            }), expected_through_sequence=0, through_sequence=1,
        )
        with self.assertRaises(ConversationNotFoundError):
            get_conversation_memory(make_principal("demo-priya"), conversation_id)
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall(
                "get_recent_transactions", {}, "memory-follow-up-call",
            ),)),
            LLMResponse(text="I checked the largest transactions using current records."),
        ])
        rows = [
            {"amount": 12, "transaction_type": "DEBIT", "merchant": "Small"},
            {"amount": 99, "transaction_type": "DEBIT", "merchant": "Largest"},
        ]
        with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Which one was largest?", conversation_id),
            )
        execute.assert_called_once_with(
            "get_recent_transactions", self.principal, {"limit": 10},
        )
        self.assertEqual(response.tool_results[0].data["result"][0]["merchant"], "Largest")

    def test_contextual_transaction_follow_up_plans_resolve_filters_and_positions(self):
        def history_for(prompt):
            return [
                {"role": "user", "user_text_or_transcript": prompt},
                {"role": "assistant", "assistant_response_text": "Earlier transaction results."},
                {"role": "user", "user_text_or_transcript": "CURRENT QUERY"},
            ]

        failed = _resolve_contextual_follow_up(
            "Show only the failed ones", history_for("Show my last 5 transactions"),
        )
        self.assertEqual(failed["tool_name"], "get_recent_transactions")
        self.assertEqual(failed["arguments"], {"limit": 5})
        self.assertEqual(failed["status_filter"], "FAILED")

        largest = _resolve_contextual_follow_up(
            "Which one was the largest?", history_for("Show my transactions"),
        )
        self.assertEqual(largest["ranking"], "desc")
        self.assertEqual(largest["tool_name"], "get_recent_transactions")

        second = _resolve_contextual_follow_up(
            "Explain the second transaction", history_for("Show my last 5 transactions"),
        )
        self.assertEqual(second["position"], 2)
        self.assertEqual(second["arguments"]["limit"], 5)

        merchant_parent = history_for("Show my transactions at DMart")
        merchant_parent[-1]["user_text_or_transcript"] = "How much did I spend there?"
        merchant = _resolve_contextual_follow_up("How much did I spend there?", merchant_parent)
        self.assertEqual(merchant["tool_name"], "search_transactions")
        self.assertEqual(merchant["arguments"], {"merchant": "DMart", "transaction_type": "DEBIT"})
        self.assertTrue(merchant["summarize_debits"])

        explicit_merchant = _resolve_contextual_follow_up(
            "Show my transactions at DMart", [],
        )
        self.assertEqual(explicit_merchant["arguments"], {"merchant": "DMart"})

    def test_month_before_reference_is_calendar_correct_and_ambiguous_context_clarifies(self):
        fixed_now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        history = [
            {"role": "user", "user_text_or_transcript": "How much did I spend last month?"},
            {"role": "assistant", "assistant_response_text": "Monthly spending retrieved."},
            {"role": "user", "user_text_or_transcript": "What about the month before that?"},
        ]
        plan = _resolve_contextual_follow_up(
            "What about the month before that?", history, now=fixed_now,
        )
        self.assertEqual(plan["tool_name"], "get_monthly_transaction_summary")
        self.assertEqual(plan["arguments"], {"year": 2026, "month": 9})
        self.assertIn("clarification", _resolve_contextual_follow_up(
            "Which one was largest?", [{"role": "user", "user_text_or_transcript": "Which one was largest?"}],
        ))

    def test_contextual_follow_up_uses_validated_summary_constraints_and_support_barrier(self):
        memory = {"banking_contexts": [{
            "tools": ["search_transactions"], "merchant": "DMart",
        }]}
        plan = _resolve_contextual_follow_up("How much did I spend there?", [], memory)
        self.assertEqual(plan["arguments"]["merchant"], "DMart")
        self.assertEqual(_extract_banking_contexts([{
            "role": "user", "user_text_or_transcript": "Show my transactions at DMart last month",
        }], now=datetime(2026, 11, 9, tzinfo=timezone.utc)), [{
            "tools": ["search_transactions"], "merchant": "DMart", "period": {"year": 2026, "month": 10},
        }])
        history = [
            {"role": "user", "user_text_or_transcript": "Show my last 5 transactions"},
            {"role": "assistant", "assistant_response_text": "Transactions were retrieved."},
            {"role": "user", "user_text_or_transcript": "Create a support ticket for a failed payment"},
            {"role": "assistant", "assistant_response_text": "I propose a ticket."},
            {"role": "user", "user_text_or_transcript": "Which one was largest?"},
        ]
        self.assertIn("clarification", _resolve_contextual_follow_up(
            "Which one was largest?", history,
        ))

    def test_multilingual_transaction_follow_up_patterns_resolve(self):
        history = [
            {"role": "user", "user_text_or_transcript": "Show my recent transactions"},
            {"role": "assistant", "assistant_response_text": "Transactions were retrieved."},
            {"role": "user", "user_text_or_transcript": "CURRENT QUERY"},
        ]
        cases = (
            ("\u0938\u092c\u0938\u0947 \u092c\u0921\u093c\u093e \u0915\u094c\u0928 \u0938\u093e \u0925\u093e?", "ranking", "desc"),
            ("\u0926\u0942\u0938\u0930\u093e \u0932\u0947\u0928\u0926\u0947\u0928 \u0938\u092e\u091d\u093e\u090f\u0902", "position", 2),
            ("\u0ca6\u0cca\u0ca1\u0ccd\u0ca1 \u0cb5\u0ccd\u0caf\u0cb5\u0cb9\u0cbe\u0cb0 \u0caf\u0cbe\u0cb5\u0cc1\u0ca6\u0cc1?", "ranking", "desc"),
            ("\u0cb5\u0cbf\u0cab\u0cb2\u0cb5\u0cbe\u0ca6\u0cb5\u0cc1\u0c97\u0cb3\u0ca8\u0ccd\u0ca8\u0cc1 \u0cae\u0cbe\u0ca4\u0ccd\u0cb0 \u0ca4\u0ccb\u0cb0\u0cbf\u0cb8\u0cbf", "status_filter", "FAILED"),
        )
        for query, field, expected in cases:
            with self.subTest(query=query):
                history[-1]["user_text_or_transcript"] = query
                plan = _resolve_contextual_follow_up(query, history)
                self.assertEqual(plan[field], expected)

    def test_summary_updates_are_incremental_and_context_stays_bounded(self):
        conversation_id = create_conversation(self.principal)["conversation_id"]
        for index in range(14):
            save_user_turn(
                self.principal, conversation_id, input_type="text",
                text_or_transcript=f"I prefer concise explanations about subject {index}",
                requested_output_mode="text",
            )
            save_assistant_turn(
                self.principal, conversation_id, response_text=f"Discussion response {index}",
                requested_output_mode="text",
            )
        provider = MockLLMProvider([
            LLMResponse(text=json.dumps({
                "preferences": ["User prefers concise explanations"],
                "goals": [], "ongoing_tasks": [], "decisions": [], "topic_refs": [],
            })),
            LLMResponse(text="First answer."),
        ])
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        orchestrator.process_turn(
            self.principal, make_request("Please keep explanations concise", conversation_id),
        )
        memory_before = get_conversation_memory(self.principal, conversation_id)
        self.assertLessEqual(len(provider.requests[1][0]), 13)
        second_provider = MockLLMProvider([LLMResponse(text="Second answer.")])
        orchestrator = AssistantOrchestrator(llm_provider=second_provider)
        orchestrator.process_turn(
            self.principal, make_request("A separate question", conversation_id),
        )
        memory_after = get_conversation_memory(self.principal, conversation_id)
        self.assertEqual(len(second_provider.requests), 1)
        self.assertEqual(memory_after["through_sequence"], memory_before["through_sequence"])

    def test_two_consecutive_questions_use_their_correct_context_once(self):
        provider = MockLLMProvider([
            LLMResponse(text="Answer to the first question."),
            LLMResponse(text="Answer to the second question."),
        ])
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        first = orchestrator.process_turn(
            self.principal, make_request("First distinct question"),
        )
        second = orchestrator.process_turn(
            self.principal,
            make_request("Second distinct question", first.conversation_id),
        )

        first_context = provider.requests[0][0]
        second_context = provider.requests[1][0]
        self.assertEqual([item.content for item in first_context], ["First distinct question"])
        self.assertEqual([item.content for item in second_context], [
            "First distinct question", "Answer to the first question.",
            "Second distinct question",
        ])
        self.assertEqual(
            sum(item.content == "Second distinct question" for item in second_context), 1
        )
        self.assertEqual(first.conversation_id, second.conversation_id)
        self.assertEqual(first.assistant_text, "Answer to the first question.")
        self.assertEqual(second.assistant_text, "Answer to the second question.")

    def test_unknown_tool_and_invalid_arguments_are_never_executed(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(
                LLMToolCall("execute_sql", {"query": "select *"}, "bad-1"),
                LLMToolCall("get_account_balance", {"customer_id": 2}, "bad-2"),
            )),
            LLMResponse(text="I couldn't access that information."),
        ])
        with patch("services.assistant_service.execute_tool") as execute:
            result = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Tell me a joke")
            )
        execute.assert_not_called()
        self.assertEqual([item.error_code for item in result.tool_results], ["INVALID_TOOL", "INVALID_ARGUMENTS"])

    def test_balance_query_requires_tool_and_final_amount_is_guarded_by_tool_result(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "balance-call"),)),
            LLMResponse(text="Your current balance is ₹999,999.00."),
        ])
        with patch("services.assistant_service.execute_tool", return_value=[
            {"account_type": "SAVINGS", "balance": 123.45},
        ]) as execute:
            with self.assertLogs("services.assistant_service", level="INFO") as logs:
                response = AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request("What is my current account balance?")
                )
        execute.assert_called_once_with("get_account_balance", self.principal, {})
        self.assertIn("₹123.45", response.assistant_text)
        self.assertNotIn("999,999", response.assistant_text)
        self.assertIn("get_account_balance", provider.requests[0][3])
        self.assertEqual(provider.request_stages, ["initial", "follow_up"])
        tool_result_message = next(m for m in provider.requests[1][0] if m.role == "tool")
        self.assertIn("123.45", tool_result_message.content)
        self._assert_trace_sequence(logs.output)

    def test_gemini_function_call_parts_round_trip_authenticated_balance_tool(self):
        from google.genai import types

        function_call_response = types.GenerateContentResponse(candidates=[
            types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="Checking your account."),
                types.Part(function_call=types.FunctionCall(
                    id="balance-call-1", name="get_account_balance", args={},
                )),
            ])),
        ])
        final_response = types.GenerateContentResponse(candidates=[
            types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="Your current account balance is ₹123.45."),
            ])),
        ])

        class ModelAPI:
            def __init__(self):
                self.responses = [function_call_response, final_response]
                self.requests = []
            def generate_content(inner, **kwargs):
                inner.requests.append(kwargs)
                return inner.responses.pop(0)
        class Client:
            def __init__(self): self.models = ModelAPI()

        client = Client()
        provider = GeminiLLMProvider(
            "offline-test-credential", "gemini-3.6-flash", client=client, sdk_types=types,
        )
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value=[{
            "account_type": "SAVINGS", "balance": 123.45,
        }]) as execute:
            response = orchestrator.process_turn(
                self.principal, make_request("What is my current account balance?"),
            )

        execute.assert_called_once_with("get_account_balance", self.principal, {})
        self.assertIn("₹123.45", response.assistant_text)
        self.assertEqual(response.tool_results[0].tool_name, "get_account_balance")
        self.assertEqual(response.tool_results[0].data["result"][0]["balance"], 123.45)
        self.assertEqual(len(client.models.requests), 2)
        self.assertTrue(client.models.requests[0]["config"].automatic_function_calling.disable)
        returned_calls = [
            part.function_response
            for content in client.models.requests[1]["contents"]
            for part in content.parts
            if part.function_response is not None
        ]
        function_call_parts = [
            (content, part.function_call)
            for content in client.models.requests[1]["contents"]
            for part in content.parts
            if part.function_call is not None
        ]
        self.assertEqual(len(function_call_parts), 1)
        call_content, original_call = function_call_parts[0]
        self.assertEqual(call_content.role, "model")
        self.assertEqual(original_call.id, "balance-call-1")
        self.assertEqual(original_call.name, "get_account_balance")
        self.assertIsInstance(original_call.args, dict)
        declaration = next(
            item for item in client.models.requests[0]["config"].tools[0].function_declarations
            if item.name == "get_account_balance"
        )
        self.assertEqual(declaration.name, "get_account_balance")
        self.assertEqual(declaration.parameters_json_schema["type"], "object")
        self.assertEqual(len(returned_calls), 1)
        response_content = next(
            content for content in client.models.requests[1]["contents"]
            if any(part.function_response is not None for part in content.parts)
        )
        self.assertEqual(response_content.role, "user")
        self.assertEqual(returned_calls[0].name, "get_account_balance")
        self.assertEqual(returned_calls[0].id, "balance-call-1")
        self.assertEqual(returned_calls[0].response["result"]["result"][0]["balance"], 123.45)
        serialized_contents = [
            content.model_dump(mode="json", by_alias=True, exclude_none=True)
            for content in client.models.requests[1]["contents"]
        ]
        serialized_response = next(
            part["functionResponse"]
            for content in serialized_contents for part in content["parts"]
            if "functionResponse" in part
        )
        self.assertEqual(serialized_response["id"], "balance-call-1")
        self.assertEqual(serialized_response["name"], "get_account_balance")
        serialized_call = next(
            part["functionCall"]
            for content in serialized_contents for part in content["parts"]
            if "functionCall" in part
        )
        self.assertEqual(serialized_call["id"], serialized_response["id"])
        self.assertEqual(serialized_call["args"], {})
        self.assertEqual(serialized_contents[-1]["role"], "user")
        model_parts = [
            part for content in client.models.requests[1]["contents"]
            if content.role == "model" for part in content.parts
        ]
        self.assertTrue(any(part.text == "Checking your account." for part in model_parts))
        self.assertTrue(any(part.function_call is not None for part in model_parts))

    def test_groq_balance_tool_round_trip_preserves_principal_and_call_id(self):
        client, api = mock_groq_client([
            groq_response(tool_calls=[groq_tool_call("groq-call-1", "get_account_balance", "{}")]),
            groq_response("Your current account balance is ₹123.45."),
        ])
        provider = GroqLLMProvider("offline-key", "openai/gpt-oss-20b", client=client)
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value=[{
            "account_type": "SAVINGS", "balance": 123.45,
        }]) as execute:
            response = orchestrator.process_turn(
                self.principal, make_request("What is my current account balance?"),
            )

        execute.assert_called_once_with("get_account_balance", self.principal, {})
        self.assertIn("₹123.45", response.assistant_text)
        self.assertEqual(response.tool_results[0].data["result"][0]["balance"], 123.45)
        self.assertEqual(api.requests[0]["model"], "openai/gpt-oss-20b")
        self.assertFalse(api.requests[0]["parallel_tool_calls"])
        self.assertEqual(api.requests[0]["tool_choice"], {
            "type": "function", "function": {"name": "get_account_balance"},
        })
        declared = [item["function"]["name"] for item in api.requests[0]["tools"]]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0], "get_account_balance")
        followup = api.requests[1]["messages"]
        assistant_call = next(item for item in followup if item.get("tool_calls"))
        tool_result = next(item for item in followup if item["role"] == "tool")
        self.assertEqual(assistant_call["tool_calls"][0]["id"], "groq-call-1")
        self.assertEqual(assistant_call["tool_calls"][0]["function"]["name"], "get_account_balance")
        self.assertEqual(tool_result["tool_call_id"], "groq-call-1")
        self.assertEqual(tool_result["name"], "get_account_balance")
        self.assertEqual(json.loads(tool_result["content"])["result"]["result"][0]["balance"], 123.45)

    def test_groq_recent_transactions_round_trip_validates_limit_and_call_id(self):
        client, api = mock_groq_client([
            groq_response(tool_calls=[groq_tool_call(
                "transactions-call-1", "get_recent_transactions", '{"limit":5}'
            )]),
            groq_response("Here are your five recent transactions."),
        ])
        provider = GroqLLMProvider("offline-key", "openai/gpt-oss-20b", client=client)
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        rows = [{"transaction_type": "DEBIT", "amount": 42.0}]
        with patch("services.assistant_service.execute_tool", return_value=rows) as execute:
            response = orchestrator.process_turn(
                self.principal, make_request("List my last 5 transactions"),
            )

        execute.assert_called_once_with(
            "get_recent_transactions", self.principal, {"limit": 5}
        )
        self.assertEqual(response.tool_results[0].tool_name, "get_recent_transactions")
        first_request = api.requests[0]
        self.assertEqual(first_request["tool_choice"], {
            "type": "function", "function": {"name": "get_recent_transactions"},
        })
        schema = first_request["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["limit"], {
            "type": "integer", "minimum": 1, "maximum": 100,
        })
        followup = api.requests[1]["messages"]
        assistant_call = next(item for item in followup if item.get("tool_calls"))
        tool_result = next(item for item in followup if item["role"] == "tool")
        self.assertEqual(assistant_call["tool_calls"][0]["id"], "transactions-call-1")
        self.assertEqual(tool_result["tool_call_id"], "transactions-call-1")
        self.assertEqual(tool_result["name"], "get_recent_transactions")

    def test_groq_simple_spending_request_selects_only_summary_tool(self):
        client, api = mock_groq_client([
            groq_response(tool_calls=[groq_tool_call("summary-1", "get_spending_summary", "{}")]),
            groq_response("Your spending summary is ready."),
        ])
        provider = GroqLLMProvider("offline-key", "openai/gpt-oss-20b", client=client)
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value={"total_debits": 100}) as execute:
            result = orchestrator.process_turn(self.principal, make_request("List my spending"))

        self.assertEqual(execute.call_count, 1)
        self.assertEqual([entry.tool_name for entry in result.tool_results], ["get_spending_summary"])
        self.assertEqual(len(api.requests), 2)
        self.assertTrue(all(request["parallel_tool_calls"] is False for request in api.requests))
        self.assertEqual(api.requests[0]["tool_choice"], {
            "type": "function", "function": {"name": "get_spending_summary"},
        })
        self.assertEqual(
            {item["function"]["name"] for item in api.requests[0]["tools"]},
            {"get_spending_summary"},
        )

        self.assertIn("Total debits: ₹100.00", result.assistant_text)
        messages = api.requests[1]["messages"]
        assistant_call = next(item for item in messages if any(
            call["id"] == "summary-1" for call in item.get("tool_calls", [])
        ))
        tool_result = next(item for item in messages if item.get("tool_call_id") == "summary-1")
        call = next(call for call in assistant_call["tool_calls"] if call["id"] == "summary-1")
        self.assertEqual(tool_result["name"], call["function"]["name"])

    def test_five_required_read_tools_execute_and_render_in_one_validated_plan(self):
        names = (
            "get_card_details", "get_loan_details", "get_recent_transactions",
            "get_spending_summary", "get_category_breakdown",
        )
        replies = [
            LLMResponse(tool_calls=(LLMToolCall(name, {}, f"call-{index}"),))
            for index, name in enumerate(names, start=1)
        ] + [LLMResponse(text="I checked all five requested sources.")]
        provider = MockLLMProvider(replies)
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        outputs = {
            "get_card_details": [{"card_type": "DEBIT", "status": "ACTIVE"}],
            "get_loan_details": [{"loan_type": "DEMO", "status": "ACTIVE"}],
            "get_recent_transactions": [{"merchant": "Shop", "amount": 10}],
            "get_spending_summary": {"total_spent": 10},
            "get_category_breakdown": [{"category": "FOOD", "amount": 10}],
        }
        with patch("services.assistant_service._required_tools_for_question", return_value=frozenset(names)), \
             patch("services.assistant_service.execute_tool", side_effect=lambda name, _principal, _arguments: outputs[name]) as execute:
            response = orchestrator.process_turn(self.principal, make_request("Please check five things"))

        self.assertEqual(execute.call_count, 5)
        self.assertEqual(provider.requests, [])
        self.assertEqual({item.tool_name for item in response.tool_results}, set(names))
        for section in ("Cards:", "Loans:", "Recent transactions:", "Spending summary:", "Spending by category:"):
            self.assertIn(section, response.assistant_text)

    def test_complex_financial_overview_routes_and_collects_all_requested_sources(self):
        question = (
            "Give me a complete financial overview: current balance, October credits and debits, "
            "five highest debit transactions, category-wise spending, September comparison, "
            "unusual transactions, estimated savings, active and blocked cards, and education "
            "loan outstanding amount and interest rate."
        )
        expected = {
            "get_account_balance", "get_monthly_transaction_summary", "find_large_transactions",
            "get_category_breakdown", "compare_monthly_spending", "detect_unusual_spending",
            "calculate_savings_rate", "get_card_details", "get_loan_details",
        }
        self.assertEqual(_required_tools_for_question(question), frozenset(expected))

        arguments = {
            "get_monthly_transaction_summary": {"year": 2026, "month": 10},
            "find_large_transactions": {"threshold": 0, "limit": 5},
            "compare_monthly_spending": {"year": 2026, "month": 10},
            "detect_unusual_spending": {"year": 2026, "month": 10},
            "calculate_savings_rate": {"year": 2026, "month": 10},
        }
        names = sorted(expected)
        provider = MockLLMProvider([
            *[
                LLMResponse(tool_calls=(LLMToolCall(
                    name, arguments.get(name, {}), f"overview-{index}",
                ),))
                for index, name in enumerate(names, start=1)
            ],
            LLMResponse(text=(
                "The balance is INR 1,000. October credits were INR 2,000 and debits INR 750. "
                "The highest debit was INR 600. Food spending was INR 750, compared with INR 500 "
                "in September; estimated savings were INR 1,250."
            )),
        ])
        outputs = {
            "get_account_balance": [{"balance": 1000.0}],
            "get_monthly_transaction_summary": {"successful_credits": 2000.0, "successful_debits": 750.0},
            "find_large_transactions": [{"amount": 600.0, "merchant": "Demo Shop"}],
            "get_category_breakdown": [{"category": "FOOD", "total_amount": 750.0}],
            "compare_monthly_spending": {"current_spending": 750.0, "previous_spending": 500.0},
            "detect_unusual_spending": [],
            "calculate_savings_rate": {"estimated_savings": 1250.0, "savings_rate": 62.5},
            "get_card_details": [{"card_type": "DEBIT", "status": "ACTIVE"}],
            "get_loan_details": [{"loan_type": "EDUCATION", "outstanding_amount": 420000.0, "interest_rate": 9.5, "tenure_months": 60}],
        }

        def execute(name, _principal, _arguments):
            return outputs[name]

        with patch("services.assistant_service.execute_tool", side_effect=execute) as tool_execution:
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request(question),
            )

        self.assertEqual(tool_execution.call_count, len(expected))
        self.assertEqual({item.tool_name for item in response.tool_results}, expected)
        self.assertEqual(provider.requests, [])
        for section in (
            "Account balance:", "Monthly transactions:", "Large transactions:",
            "Spending by category:", "Monthly spending comparison:",
            "Unusual spending:", "Savings analysis:", "Cards:", "Loans:",
        ):
            self.assertIn(section, response.assistant_text)
        self.assertIn("Successful credits: ₹2,000.00", response.assistant_text)

    def test_tts_receives_speech_friendly_copy_while_visual_text_is_preserved(self):
        visual = "## Summary\n\n| Month | Debits |\n|---|---:|\n| October | INR 2,000 |\nUpdated 2026-10-31T12:00:00Z"
        spoken = []

        class Synthesizer:
            def synthesize(self, _principal, text, language):
                spoken.append((text, language))
                return None

        request = make_request("Give me the summary.")
        request = request.model_copy(update={"output_mode": "speech"})
        response = AssistantOrchestrator(
            llm_provider=MockLLMProvider([LLMResponse(text=visual)]),
            synthesizer=Synthesizer(),
        ).process_turn(self.principal, request)
        self.assertEqual(response.assistant_text, visual)
        self.assertEqual(len(spoken), 1)
        self.assertIn("Debits: INR 2,000", spoken[0][0])
        self.assertNotIn("|", spoken[0][0])
        self.assertIn("October 31, 2026", spoken[0][0])

    def test_current_kannada_followup_overrides_stale_hindi_conversation_language(self):
        conversation = create_conversation(self.principal, "language follow-up")
        prior = save_user_turn(
            self.principal, conversation["conversation_id"], input_type="text",
            text_or_transcript="Mera balance batao", input_language="hi-IN",
            requested_output_mode="text", detected_languages=("hi-IN",),
        )
        save_assistant_turn(
            self.principal, conversation["conversation_id"], response_text="आपका बैलेंस उपलब्ध है।",
            response_language="hi-IN", requested_output_mode="text",
        )
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "lang-call"),)),
            LLMResponse(text="ನಿಮ್ಮ ಖಾತೆಯ ಬಾಕಿ ಲಭ್ಯವಿದೆ."),
        ])
        with patch("services.assistant_service.execute_tool", return_value=[{"balance": 100.0}]):
            response = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Nanna account balance eshtu?", conversation["conversation_id"]),
            )
        self.assertEqual(provider.requests[0][2], "kn-IN")
        self.assertEqual(response.response_language, "kn-IN")
        self.assertNotEqual(prior["turn_id"], response.turn_id)

    def test_context_and_orchestration_limits_are_configurable_and_bounded(self):
        with patch.dict(os.environ, {
            "VAANISHIELD_LLM_HISTORY_TURNS": "8",
            "VAANISHIELD_LLM_MAX_ORCHESTRATION_ROUNDS": "9",
        }):
            self.assertEqual(_history_turn_limit(), 8)
            self.assertEqual(_orchestration_round_limit(8), 9)
            self.assertEqual(_orchestration_round_limit(20), 9)
        with patch.dict(os.environ, {
            "VAANISHIELD_LLM_HISTORY_TURNS": "999",
            "VAANISHIELD_LLM_MAX_ORCHESTRATION_ROUNDS": "1",
        }):
            self.assertEqual(_history_turn_limit(), 30)
            self.assertEqual(_orchestration_round_limit(30), 2)

    def test_groq_unknown_tool_is_rejected_by_backend_allowlist(self):
        client, api = mock_groq_client([
            groq_response(tool_calls=[groq_tool_call("unknown-1", "execute_sql", '{"query":"select *"}')]),
            groq_response("I cannot access that tool."),
        ])
        orchestrator = AssistantOrchestrator(
            llm_provider=GroqLLMProvider("offline-key", "openai/gpt-oss-20b", client=client),
        )
        with patch("services.assistant_service.execute_tool") as execute:
            response = orchestrator.process_turn(self.principal, make_request("Tell me a joke"))
        execute.assert_not_called()
        self.assertEqual(response.tool_results[0].error_code, "INVALID_TOOL")
        self.assertIsNone(api.requests[0]["tools"])

    def test_follow_up_history_includes_persisted_failed_turns_and_valid_five_content_sequence(self):
        from google.genai import types

        conversation = create_conversation(self.principal)
        save_user_turn(
            self.principal, conversation["conversation_id"], input_type="text",
            text_or_transcript="Earlier failed user question", input_language="en-IN",
            requested_output_mode="text", processing_status="failed",
        )
        save_assistant_turn(
            self.principal, conversation["conversation_id"],
            response_text="Earlier safe failure response", response_language="en-IN",
            requested_output_mode="text", input_type="text", processing_status="failed",
        )
        first = types.GenerateContentResponse(candidates=[
            types.Candidate(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(
                    id="history-balance-call", name="get_account_balance", args={},
                )),
            ])),
        ])
        final = types.GenerateContentResponse(candidates=[
            types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="Your current account balance is ₹123.45."),
            ])),
        ])

        class ModelAPI:
            def __init__(self):
                self.responses = [first, final]
                self.requests = []
            def generate_content(inner, **kwargs):
                inner.requests.append(kwargs)
                return inner.responses.pop(0)
        class Client:
            def __init__(self): self.models = ModelAPI()

        client = Client()
        provider = GeminiLLMProvider(
            "offline-test-credential", "gemini-3.6-flash", client=client, sdk_types=types,
        )
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        with patch("services.assistant_service.execute_tool", return_value=[{
            "account_type": "SAVINGS", "balance": 123.45,
        }]):
            result = orchestrator.process_turn(
                self.principal,
                make_request("What is my current account balance?", conversation["conversation_id"]),
            )

        initial_contents = client.models.requests[0]["contents"]
        follow_up_contents = client.models.requests[1]["contents"]
        self.assertEqual([item.role for item in initial_contents], ["user", "model", "user"])
        self.assertEqual(
            [item.role for item in follow_up_contents],
            ["user", "model", "user", "model", "user"],
        )
        self.assertEqual(initial_contents[0].parts[0].text, "Earlier failed user question")
        self.assertEqual(initial_contents[1].parts[0].text, "Earlier safe failure response")
        self.assertEqual(initial_contents[2].parts[0].text, "What is my current account balance?")
        self.assertEqual(len(client.models.requests), 2)

        declarations = client.models.requests[0]["config"].tools[0].function_declarations
        self.assertEqual({item.name for item in declarations}, {"get_account_balance"})
        for declaration in declarations:
            self.assertTrue(declaration.name)
            self.assertIsInstance(declaration.parameters_json_schema, dict)
            self.assertEqual(declaration.parameters_json_schema.get("type"), "object")
        call = next(
            part.function_call for content in follow_up_contents
            for part in content.parts if part.function_call is not None
        )
        response = next(
            part.function_response for content in follow_up_contents
            for part in content.parts if part.function_response is not None
        )
        self.assertEqual(call.name, response.name)
        self.assertEqual(call.id, response.id)
        self.assertIsInstance(call.args, dict)
        self.assertIsInstance(response.response, dict)
        self.assertEqual(result.tool_results[0].tool_name, "get_account_balance")

    def test_text_only_balance_answer_is_rejected(self):
        provider = MockLLMProvider([LLMResponse(text="Your balance is ₹999.99.")])
        with patch("services.assistant_service.execute_tool") as execute:
            with self.assertRaises(AssistantServiceError):
                AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request("What is my current account balance?")
                )
        execute.assert_not_called()
        self.assertEqual(provider.requests[0][3], frozenset({"get_account_balance"}))

    def test_tool_failure_cannot_be_replaced_by_model_fabrication(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "balance-fail"),)),
            LLMResponse(text="The balance is ₹888.00."),
        ])
        with patch("services.assistant_service.execute_tool", side_effect=RuntimeError("sensitive service failure")):
            with self.assertRaises(AssistantServiceError) as raised:
                AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request("What is my balance?")
                )
        self.assertNotIn("888.00", str(raised.exception))

    def test_trace_correlation_and_logs_exclude_banking_data(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "trace-call"),)),
            LLMResponse(text="Your balance is ₹987,654.32."),
        ])
        with patch("services.assistant_service.execute_tool", return_value=[
            {"customer_id": 44, "balance": 987654.32, "account_number": "987654321012"},
        ]):
            with self.assertLogs("services.assistant_service", level="INFO") as captured:
                AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request("What is my account balance?")
                )
        joined = "\n".join(captured.output)
        self.assertNotIn("987654", joined)
        self.assertNotIn("customer_id", joined)
        events = [json.loads(line.split("assistant_execution_trace ", 1)[1]) for line in captured.output if "assistant_execution_trace " in line]
        self.assertGreaterEqual(len(events), 8)
        self.assertEqual(len({event["correlation_id"] for event in events}), 1)
        names = [event["event"] for event in events]
        self.assertLess(names.index("tool_call_requested"), names.index("tool_execution_completed"))
        self.assertLess(names.index("tool_execution_completed"), names.index("tool_result_handed_back"))
        self.assertLess(names.index("tool_result_handed_back"), names.index("final_response_generated"))
        timings = [event for event in events if event["event"] == "assistant_stage_timing"]
        stages = {event.get("stage") for event in timings}
        self.assertTrue({
            "request_setup_and_persistence", "intent_classification", "tool_selection",
            "tool_and_database", "llm_generation", "response_formatting",
            "response_persistence",
        }.issubset(stages))
        self.assertTrue(all(
            isinstance(event.get("duration_ms"), (int, float))
            and event["duration_ms"] >= 0 for event in timings
        ))
        total_event = next(event for event in events if event["event"] == "turn_completed")
        self.assertGreaterEqual(total_event["duration_ms"], 0)

    def test_timing_trace_is_emitted_for_tool_and_provider_failures_without_sensitive_data(self):
        secret = "987654321012 TR-" + "A" * 48 + " provider-private-detail"
        tool_provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "timed-tool"),)),
            LLMResponse(text="The balance is unavailable."),
        ])
        with patch("services.assistant_service.execute_tool", side_effect=RuntimeError(secret)):
            with self.assertLogs("services.assistant_service", level="INFO") as captured:
                with self.assertRaises(AssistantServiceError):
                    AssistantOrchestrator(llm_provider=tool_provider).process_turn(
                        self.principal, make_request("What is my account balance?"),
                    )
        events = [
            json.loads(line.split("assistant_execution_trace ", 1)[1])
            for line in captured.output if "assistant_execution_trace " in line
        ]
        timings = [event for event in events if event["event"] == "assistant_stage_timing"]
        self.assertTrue(any(event.get("stage") == "tool_and_database" and event.get("tool") == "get_account_balance" for event in timings))
        self.assertTrue(any(event.get("stage") == "llm_generation" and event.get("step") == 1 for event in timings))
        self.assertTrue(any(event["event"] == "turn_failed" and "duration_ms" in event for event in events))
        self.assertNotIn(secret, "\n".join(captured.output))

        provider = MockLLMProvider([LLMProviderError(secret)])
        with self.assertLogs("services.assistant_service", level="INFO") as captured:
            with self.assertRaises(AssistantServiceError):
                AssistantOrchestrator(llm_provider=provider).process_turn(
                    self.principal, make_request("Hello, can you help?"),
                )
        events = [
            json.loads(line.split("assistant_execution_trace ", 1)[1])
            for line in captured.output if "assistant_execution_trace " in line
        ]
        self.assertTrue(any(
            event.get("stage") == "llm_generation" and "duration_ms" in event
            for event in events if event["event"] == "assistant_stage_timing"
        ))
        self.assertTrue(any(event["event"] == "turn_failed" for event in events))
        self.assertNotIn(secret, "\n".join(captured.output))

        class FailingSynthesizer:
            def synthesize(self, *_args):
                raise RuntimeError(secret)

        speech_request = make_request("Give me a short summary.")
        speech_request = speech_request.model_copy(update={"output_mode": "speech"})
        with self.assertLogs("services.assistant_service", level="INFO") as captured:
            response = AssistantOrchestrator(
                llm_provider=MockLLMProvider([LLMResponse(text="A short summary.")]),
                synthesizer=FailingSynthesizer(),
            ).process_turn(self.principal, speech_request)
        events = [
            json.loads(line.split("assistant_execution_trace ", 1)[1])
            for line in captured.output if "assistant_execution_trace " in line
        ]
        self.assertTrue(any(
            event.get("stage") == "tts" and "duration_ms" in event
            for event in events if event["event"] == "assistant_stage_timing"
        ))
        self.assertIsNone(response.speech_output)
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_deterministic_latency_baseline_for_representative_read_scenarios(self):
        """Print a local mock-backed baseline; values are not production latency."""
        scenarios = {
            "balance": ("What is my account balance?", "get_account_balance"),
            "transactions": ("Show my last five transactions", "get_recent_transactions"),
            "loans": ("Show my loan details", "get_loan_details"),
            "multi_balance_transactions_loans": (
                "Show my balance, last five transactions and loans", None,
            ),
            "contextual_follow_up": ("And what about it?", "get_account_balance"),
        }
        summaries = {}
        sample_count = 24

        def trace_for(operation):
            with self.assertLogs("services.assistant_service", level="INFO") as captured:
                operation()
            return [
                json.loads(line.split("assistant_execution_trace ", 1)[1])
                for line in captured.output if "assistant_execution_trace " in line
            ]

        for scenario, (question, tool_name) in scenarios.items():
            wall_samples = []
            stage_samples = {}
            cold_stages = None
            llm_round_durations = []
            llm_round_counts = []
            for sample in range(sample_count):
                if scenario == "contextual_follow_up":
                    seed_provider = MockLLMProvider([
                        LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "seed-call"),)),
                        LLMResponse(text="Your current balance is recorded."),
                    ])
                    with patch("services.assistant_service.execute_tool", return_value=[]):
                        seed = AssistantOrchestrator(llm_provider=seed_provider).process_turn(
                            self.principal, make_request("What is my account balance?"),
                        )
                    follow_up_id = seed.conversation_id
                    provider = MockLLMProvider([
                        LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "follow-up-call"),)),
                        LLMResponse(text="I checked that for your account."),
                    ])
                    request = make_request(question, follow_up_id)
                else:
                    provider = MockLLMProvider(
                        [
                            LLMResponse(tool_calls=(LLMToolCall(tool_name, {}, "profile-call"),)),
                            LLMResponse(text="The requested information was retrieved."),
                        ] if tool_name else []
                    )
                    request = make_request(question)

                def invoke():
                    with patch("services.assistant_service.execute_tool", return_value=[]):
                        return AssistantOrchestrator(llm_provider=provider).process_turn(
                            self.principal, request,
                        )

                started = time.perf_counter()
                try:
                    events = trace_for(invoke)
                except Exception as exc:
                    raise AssertionError(f"Mock baseline failed for {scenario}, sample {sample + 1}") from exc
                wall_ms = (time.perf_counter() - started) * 1000
                wall_samples.append(wall_ms)
                stages = {}
                for event in events:
                    if event.get("event") == "assistant_stage_timing":
                        stage = event.get("stage")
                        stages.setdefault(stage, []).append(event.get("duration_ms", 0))
                        if stage == "llm_generation":
                            llm_round_durations.append(event.get("duration_ms", 0))
                        if stage == "tool_and_database":
                            stages.setdefault(f"tool:{event.get('tool')}", []).append(event.get("duration_ms", 0))
                llm_round_counts.append(len(stages.get("llm_generation", [])))
                if sample == 0:
                    cold_stages = {key: sum(values) for key, values in stages.items()}
                for key, values in stages.items():
                    stage_samples.setdefault(key, []).append(sum(values))

            def percentile(values, fraction):
                ordered = sorted(values)
                return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 2)

            summaries[scenario] = {
                "samples": sample_count,
                "first_request_ms": round(wall_samples[0], 2),
                "warm_median_ms": round(statistics.median(wall_samples[1:]), 2),
                "warm_p95_ms": percentile(wall_samples[1:], 0.95),
                "llm_rounds_per_request": sorted(set(llm_round_counts)),
                "llm_round_duration_median_ms": round(statistics.median(llm_round_durations), 2) if llm_round_durations else None,
                "llm_round_duration_p95_ms": percentile(llm_round_durations, 0.95) if llm_round_durations else None,
                "cold_stage_ms": cold_stages,
                "warm_stage_medians_ms": {
                    key: round(statistics.median(values[1:]), 2)
                    for key, values in stage_samples.items()
                },
            }
        print("DETERMINISTIC_MOCK_LATENCY_BASELINE=" + json.dumps(summaries, sort_keys=True))

    def _assert_trace_sequence(self, lines):
        events = [json.loads(line.split("assistant_execution_trace ", 1)[1]) for line in lines if "assistant_execution_trace " in line]
        names = [event["event"] for event in events]
        expected = ("provider_selected", "model_request_started", "model_response_received", "tool_call_requested", "tool_validation_passed", "tool_execution_completed", "tool_result_handed_back", "final_response_generated", "turn_completed")
        for required in expected:
            self.assertIn(required, names)
        positions = [names.index(required) for required in expected]
        self.assertEqual(positions, sorted(positions))

    def test_required_tool_classifier_and_provider_configuration_selection(self):
        self.assertEqual(_required_tools_for_question("What is my current account balance?"), frozenset({"get_account_balance"}))
        self.assertEqual(_required_tools_for_question("Show my recent transactions"), frozenset({"get_recent_transactions"}))
        self.assertEqual(_required_tools_for_question("How much did I spend on groceries?"), frozenset({"get_category_breakdown"}))
        self.assertEqual(_required_tools_for_question("Which merchant did I spend most with?"), frozenset({"get_top_merchants"}))
        with patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "gemini", "VAANISHIELD_LLM_MODEL": "gemini-3.8-flash", "GEMINI_API_KEY": "offline-test-credential"}):
            selected = create_llm_provider()
            self.assertIsInstance(selected, GeminiLLMProvider)
            self.assertEqual(get_provider_settings()["provider"], "gemini")
        with patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "rule_based"}):
            self.assertIsNone(create_llm_provider())
            self.assertEqual(get_provider_settings()["provider"], "legacy_keyword_router")

    def test_groq_provider_selection_and_missing_key_are_safe(self):
        settings_env = {
            "VAANISHIELD_LLM_PROVIDER": "groq",
            "VAANISHIELD_LLM_MODEL": "openai/gpt-oss-20b",
            "GROQ_API_KEY": "offline-test-groq-key",
        }
        with patch.dict(os.environ, settings_env, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "groq")
            self.assertEqual(settings["model"], "openai/gpt-oss-20b")
            self.assertTrue(settings["credential_configured"])
            self.assertIsInstance(create_llm_provider(), GroqLLMProvider)

        settings_env["GROQ_API_KEY"] = ""
        with patch.dict(os.environ, settings_env, clear=True):
            self.assertFalse(get_provider_settings()["credential_configured"])
            with self.assertRaises(LLMProviderError) as raised:
                create_llm_provider()
            self.assertIn("API key is not configured", str(raised.exception))
            self.assertNotIn("offline-test-groq-key", str(raised.exception))

    def test_provider_selection_defaults_to_groq_when_unset_or_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "groq")
            self.assertEqual(settings["model"], "openai/gpt-oss-20b")
            self.assertFalse(settings["credential_configured"])
        with patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": ""}, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "groq")
            self.assertEqual(settings["model"], "openai/gpt-oss-20b")

    def test_explicit_groq_selection_configures_groq_provider(self):
        env = {
            "VAANISHIELD_LLM_PROVIDER": "groq",
            "VAANISHIELD_LLM_MODEL": "openai/gpt-oss-20b",
            "GROQ_API_KEY": "offline-groq-key",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "groq")
            self.assertEqual(settings["model"], "openai/gpt-oss-20b")
            self.assertTrue(settings["credential_configured"])
            provider = create_llm_provider()
            self.assertIsInstance(provider, GroqLLMProvider)
            self.assertEqual(provider.model, "openai/gpt-oss-20b")

    def test_explicit_gemini_selection_configures_gemini_provider(self):
        env = {
            "VAANISHIELD_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "offline-gemini-key",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "gemini")
            self.assertEqual(settings["model"], "gemini-2.5-flash")
            self.assertTrue(settings["credential_configured"])
            with patch("google.genai.Client"):
                provider = create_llm_provider()
            self.assertIsInstance(provider, GeminiLLMProvider)
            self.assertEqual(provider.model, "gemini-2.5-flash")

    def test_gemini_model_configuration_independent_and_validation(self):
        env = {
            "VAANISHIELD_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "offline-gemini-key",
            "GEMINI_MODEL": "gemini-2.5-pro",
            "VAANISHIELD_LLM_MODEL": "openai/gpt-oss-20b",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["provider"], "gemini")
            self.assertEqual(settings["model"], "gemini-2.5-pro")

        env_invalid = {
            "VAANISHIELD_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "offline-gemini-key",
            "GEMINI_MODEL": "invalid model name!",
        }
        with patch.dict(os.environ, env_invalid, clear=True):
            settings = get_provider_settings()
            self.assertEqual(settings["model"], "invalid-model-identifier")
            with self.assertRaises(LLMProviderError) as cm:
                create_llm_provider()
            self.assertIn("valid", str(cm.exception).lower())

    def test_missing_gemini_credentials_produce_safe_error(self):
        env = {
            "VAANISHIELD_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_provider_settings()
            self.assertFalse(settings["credential_configured"])
            with self.assertRaises(LLMProviderError) as cm:
                create_llm_provider()
            error_message = str(cm.exception)
            self.assertIn("Gemini is selected but its API key is not configured", error_message)
            self.assertNotIn("AIza", error_message)

    def test_provider_diagnostics_never_expose_credentials(self):
        env = {
            "VAANISHIELD_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "AIzaSySuperSecretCredential123456",
            "GEMINI_MODEL": "gemini-2.5-flash",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_provider_settings()
            for key, value in settings.items():
                self.assertNotIn("AIzaSySuperSecretCredential123456", str(key))
                self.assertNotIn("AIzaSySuperSecretCredential123456", str(value))
            self.assertEqual(settings["provider"], "gemini")
            self.assertTrue(settings["credential_configured"])

    def test_gemini_tool_call_and_result_round_trip(self):
        from google.genai import types

        call_part = types.Part(function_call=types.FunctionCall(
            name="get_account_balance", args={}, id="test-gemini-call-101",
        ))
        first_resp = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[call_part]))],
            usage_metadata=SimpleNamespace(prompt_token_count=50, candidates_token_count=20, total_token_count=70),
        )
        second_resp = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                types.Part.from_text(text="Your balance is ₹5,432.10."),
            ]))],
            usage_metadata=SimpleNamespace(prompt_token_count=80, candidates_token_count=15, total_token_count=95),
        )

        class ModelAPI:
            def __init__(self):
                self.requests = []
                self.responses = [first_resp, second_resp]
            def generate_content(inner, **kwargs):
                inner.requests.append(kwargs)
                return inner.responses.pop(0)

        class Client:
            def __init__(self):
                self.models = ModelAPI()

        client = Client()
        provider = GeminiLLMProvider("offline-key", "gemini-2.5-flash", client=client, sdk_types=types)
        orchestrator = AssistantOrchestrator(llm_provider=provider)

        with patch("services.assistant_service.execute_tool", return_value=[{"balance": 5432.10}]) as exec_tool:
            resp = orchestrator.process_turn(self.principal, make_request("What is my balance?"))

        exec_tool.assert_called_once_with("get_account_balance", self.principal, {})
        self.assertIn("₹5,432.10", resp.assistant_text)
        self.assertEqual(len(client.models.requests), 2)
        second_req_contents = client.models.requests[1]["contents"]
        func_resp_parts = [
            part.function_response for content in second_req_contents
            for part in content.parts if part.function_response is not None
        ]
        self.assertEqual(len(func_resp_parts), 1)
        self.assertEqual(func_resp_parts[0].id, "test-gemini-call-101")
        self.assertEqual(func_resp_parts[0].name, "get_account_balance")

    def test_failed_gemini_request_never_executes_tool_twice(self):
        from google.genai import types
        from google.genai.errors import ServerError

        call_part = types.Part(function_call=types.FunctionCall(
            name="get_account_balance", args={}, id="test-gemini-call-202",
        ))
        first_resp = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[call_part]))],
        )

        server_err = ServerError(503, {
            "error": {"code": 503, "status": "UNAVAILABLE", "message": "Service unavailable"}
        })

        class ModelAPI:
            def __init__(self):
                self.calls = 0
            def generate_content(inner, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    return first_resp
                raise server_err

        class Client:
            def __init__(self):
                self.models = ModelAPI()

        client = Client()
        provider = GeminiLLMProvider("offline-key", "gemini-2.5-flash", client=client, sdk_types=types)
        orchestrator = AssistantOrchestrator(llm_provider=provider)

        tool_execution_count = [0]
        def counting_execute(*args, **kwargs):
            tool_execution_count[0] += 1
            return [{"balance": 1000.0}]

        with patch("services.assistant_service.execute_tool", side_effect=counting_execute):
            with patch("services.llm_adapters.time.sleep"):
                with self.assertRaises(AssistantServiceError):
                    orchestrator.process_turn(self.principal, make_request("What is my balance?"))

        self.assertEqual(tool_execution_count[0], 1)
        self.assertGreaterEqual(client.models.calls, 2)

    def test_gemini_retries_transient_503_and_succeeds(self):
        from google.genai import types
        from google.genai.errors import ServerError

        server_err = ServerError(503, {
            "error": {"code": 503, "status": "UNAVAILABLE", "message": "Temporary glitch",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.1s"}]}
        })
        good_resp = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                types.Part.from_text(text="Success after retry"),
            ]))],
        )

        class ModelAPI:
            def __init__(self):
                self.calls = 0
            def generate_content(inner, **kwargs):
                inner.calls += 1
                if inner.calls == 1:
                    raise server_err
                return good_resp

        class Client:
            def __init__(self):
                self.models = ModelAPI()

        provider = GeminiLLMProvider("offline-key", "gemini-2.5-flash", client=Client(), sdk_types=types)
        with patch("services.llm_adapters.time.sleep") as mock_sleep:
            resp = provider.generate([LLMMessage("user", "Hello")], [], response_language="en-IN")

        self.assertEqual(resp.text, "Success after retry")
        mock_sleep.assert_called_once()

    def test_dotenv_loading_does_not_override_process_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "phase6b.env"
            config_path.write_text("VAANISHIELD_LLM_PROVIDER=rule_based\nVAANISHIELD_LLM_MODEL=gemini-2.0-flash\n")
            with patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "offline-test-credential"}):
                load_dotenv(config_path, override=False)
                self.assertEqual(os.environ["VAANISHIELD_LLM_PROVIDER"], "gemini")
                self.assertEqual(get_provider_settings()["provider"], "gemini")

    def test_startup_diagnostic_reports_only_safe_provider_metadata(self):
        from backend import main
        from services.assistant_service import orchestrator
        safe_settings = {"provider": "gemini", "model": "gemini-3.8-flash", "credential_configured": True}
        with patch.object(orchestrator, "_provider_settings", safe_settings):
            with patch("backend.main.initialize_database"), patch("backend.main.start_cleanup_worker"):
                with self.assertLogs("uvicorn.error", level="INFO") as logs:
                    main.initialize_database_on_startup()
        output = "\n".join(logs.output)
        self.assertIn("provider=gemini", output)
        self.assertIn("model=gemini-3.8-flash", output)
        self.assertIn("credentials_configured=True", output)
        self.assertNotIn("GEMINI_API_KEY", output)

    def test_gemini_configuration_error_never_falls_back_to_keyword_router(self):
        class SpyRouter:
            def __init__(self): self.called = False
            def route(self, text):
                self.called = True
                return "banking", "account_balance", "get_account_balance", {}
        router = SpyRouter()
        with patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "", "VAANISHIELD_LLM_MODEL": "gemini-3.8-flash"}):
            orchestrator = AssistantOrchestrator(intent_router=router)
        with self.assertRaises(AssistantServiceError):
            orchestrator.process_turn(self.principal, make_request("What is my balance?"))
        self.assertFalse(router.called)

    def test_customer_identity_and_full_account_number_are_stripped_before_model_context(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "balance-1"),)),
            LLMResponse(text="Your account balance is available."),
        ])
        with patch("services.assistant_service.execute_tool", return_value={
            "customer_id": 31, "customer_key": "demo-arjun", "internal_id": 31,
            "account_number": "VS1234567890", "balance": 100.0,
        }):
            AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("What is my account balance?")
            )
        tool_messages = [message.content for message in provider.requests[1][0] if message.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertNotIn("customer_id", tool_messages[0])
        self.assertNotIn("customer_key", tool_messages[0])
        self.assertNotIn("VS1234567890", tool_messages[0])
        self.assertIn("7890", tool_messages[0])

    def test_provider_failure_persists_failed_turn_without_leaking_error(self):
        from services.llm_interfaces import LLMProviderError
        provider = MockLLMProvider([LLMProviderError("secret-bearing sdk detail")])
        with self.assertRaises(Exception) as raised:
            AssistantOrchestrator(llm_provider=provider).process_turn(self.principal, make_request())
        self.assertNotIn("secret-bearing", str(raised.exception))
        conversation_id = list_conversations(self.principal)["items"][0]["conversation_id"]
        history = get_conversation_history(self.principal, conversation_id)
        self.assertEqual([turn["role"] for turn in history["items"]], ["user", "assistant"])
        self.assertEqual([turn["processing_status"] for turn in history["items"]], ["failed", "failed"])

    def test_retry_reuses_failed_turn_and_completed_retry_is_idempotent(self):
        from services.llm_interfaces import LLMProviderError
        conversation = create_conversation(self.principal)
        request = make_request("Hello, please help me.", conversation["conversation_id"])
        failing = MockLLMProvider([LLMProviderError("offline failure")])
        with self.assertRaises(AssistantServiceError):
            AssistantOrchestrator(llm_provider=failing).process_turn(self.principal, request)

        successful = MockLLMProvider([LLMResponse(text="I can help with that." )])
        retried = AssistantOrchestrator(llm_provider=successful).process_turn(self.principal, request)
        self.assertEqual(retried.assistant_text, "I can help with that.")
        self.assertEqual(len(successful.requests), 1)
        history = get_conversation_history(self.principal, conversation["conversation_id"])
        self.assertEqual([turn["role"] for turn in history["items"]], ["user", "assistant"])
        self.assertTrue(all(turn["processing_status"] == "completed" for turn in history["items"]))

        replay_provider = MockLLMProvider([])
        replayed = AssistantOrchestrator(llm_provider=replay_provider).process_turn(self.principal, request)
        self.assertEqual(replayed.assistant_text, "I can help with that.")
        self.assertEqual(replay_provider.requests, [])
        replay_history = get_conversation_history(self.principal, conversation["conversation_id"])
        self.assertEqual(len(replay_history["items"]), 2)

    def test_failed_retry_with_changed_message_is_rejected(self):
        from services.llm_interfaces import LLMProviderError
        conversation = create_conversation(self.principal)
        request = make_request("First text", conversation["conversation_id"])
        with self.assertRaises(AssistantServiceError):
            AssistantOrchestrator(llm_provider=MockLLMProvider([LLMProviderError("offline")])).process_turn(
                self.principal, request,
            )
        request.text = "Different text"
        with self.assertRaises(AssistantServiceError):
            AssistantOrchestrator(llm_provider=MockLLMProvider([])).process_turn(
                self.principal, request,
            )

    def test_provider_timeout_is_handled_safely(self):
        provider = MockLLMProvider([LLMTimeout("provider internals")])
        with self.assertRaises(Exception) as raised:
            AssistantOrchestrator(llm_provider=provider).process_turn(self.principal, make_request())
        self.assertIn("too long", str(raised.exception))
        self.assertNotIn("provider internals", str(raised.exception))

    def test_conversation_context_is_owner_scoped_and_preserved(self):
        conversation = create_conversation(self.principal)
        conv_id = conversation["conversation_id"]
        save_user_turn(self.principal, conv_id, input_type="text", text_or_transcript="What about last month?", requested_output_mode="text")
        save_assistant_turn(self.principal, conv_id, response_text="I can compare that.", requested_output_mode="text")
        provider = MockLLMProvider([LLMResponse(text="Last month is compared." )])
        result = AssistantOrchestrator(llm_provider=provider).process_turn(
            self.principal, make_request("Compare it", conv_id)
        )
        roles_and_text = [(item.role, item.content) for item in provider.requests[0][0]]
        self.assertIn(("user", "What about last month?"), roles_and_text)
        self.assertIn(("assistant", "I can compare that."), roles_and_text)
        self.assertIn(("user", "Compare it"), roles_and_text)
        self.assertEqual(result.assistant_text, "Last month is compared.")

        other = make_principal("demo-priya")
        other_provider = MockLLMProvider([LLMResponse(text="not used")])
        with self.assertRaises(Exception):
            AssistantOrchestrator(llm_provider=other_provider).process_turn(
                other, make_request("Show history", conv_id)
            )
        self.assertEqual(other_provider.requests, [])

    def test_card_and_loan_questions_select_only_the_matching_owner_scoped_tools(self):
        cases = (
            ("Is my debit card active or blocked?", {"get_card_details"}),
            ("Explain my education loan status", {"get_loan_details"}),
            ("What is my balance and is my card blocked?", {"get_account_balance", "get_card_details"}),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                provider = MockLLMProvider([LLMResponse(tool_calls=(
                    LLMToolCall(sorted(expected)[0], {}, "banking-data-call"),
                ))])
                if len(expected) > 1:
                    result = AssistantOrchestrator(llm_provider=provider).process_turn(
                        self.principal, make_request(question),
                    )
                    self.assertEqual(provider.requests, [])
                    self.assertEqual({item.tool_name for item in result.tool_results}, expected)
                else:
                    try:
                        AssistantOrchestrator(llm_provider=provider).process_turn(
                            self.principal, make_request(question),
                        )
                    except Exception:
                        # The provider fixture only needs to reach its initial
                        # constrained request; the selected schema is the assertion.
                        pass
                    selected = {tool.name for tool in provider.requests[0][1]}
                    self.assertEqual(selected, expected)

    def test_card_and_loan_tools_return_owner_service_results_without_model_ids(self):
        cases = (
            ("What is my debit card status?", "get_card_details", {
                "card_type": "DEBIT", "last_four": "1234", "status": "BLOCKED",
                "card_id": 444, "customer_id": 9,
            }, "BLOCKED"),
            ("Explain my education loan", "get_loan_details", {
                "loan_type": "EDUCATION", "principal_amount": 500000,
                "outstanding_amount": 420000, "interest_rate": 9.5,
                "tenure_months": 60, "status": "ACTIVE", "loan_id": 777,
            }, "ACTIVE"),
        )
        for question, tool_name, service_result, expected_value in cases:
            with self.subTest(tool=tool_name):
                provider = MockLLMProvider([
                    LLMResponse(tool_calls=(LLMToolCall(tool_name, {}, "banking-record-call"),)),
                    LLMResponse(text=f"The record status is {expected_value}."),
                ])
                with patch("services.agent_tools.get_card_details", return_value=[service_result]) as cards, \
                     patch("services.agent_tools.get_loan_details", return_value=[service_result]) as loans:
                    result = AssistantOrchestrator(llm_provider=provider).process_turn(
                        self.principal, make_request(question),
                    )
                (cards if tool_name == "get_card_details" else loans).assert_called_once()
                self.assertIn(expected_value, result.assistant_text)
                self.assertNotIn("card_id", json.dumps(provider.requests[1][0][-1].content))
                self.assertNotIn("loan_id", json.dumps(provider.requests[1][0][-1].content))

    def test_loan_answer_never_claims_original_tenure_is_remaining(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_loan_details", {}, "loan-call"),)),
            LLMResponse(text="Your remaining tenure is 60 months."),
        ])
        loan = {
            "loan_type": "EDUCATION", "principal_amount": 500000,
            "outstanding_amount": 420000, "interest_rate": 9.5,
            "tenure_months": 60, "status": "ACTIVE", "loan_id": 777,
        }
        with patch("services.agent_tools.get_loan_details", return_value=[loan]):
            result = AssistantOrchestrator(llm_provider=provider).process_turn(
                self.principal, make_request("Explain my education loan"),
            )
        self.assertIn("original tenure 60 months", result.assistant_text)
        self.assertIn("Remaining tenure is not available", result.assistant_text)
        self.assertNotIn("remaining tenure is 60", result.assistant_text.casefold())

    def test_explicit_response_language_overrides_detected_input_language(self):
        provider = MockLLMProvider([
            LLMResponse(tool_calls=(LLMToolCall("get_account_balance", {}, "explicit-lang-balance"),)),
            LLMResponse(text="Your balance is ₹100."),
        ])
        request = make_request("\u092e\u0947\u0930\u0947 \u0916\u093e\u0924\u0947 \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u093e \u092c\u0948\u0932\u0947\u0902\u0938 \u0939\u0948?",)
        request.response_language = "en-IN"
        with patch("services.assistant_service.execute_tool", return_value=[{"balance": 100.0}]):
            response = AssistantOrchestrator(llm_provider=provider).process_turn(self.principal, request)
        self.assertEqual(response.response_language, "en-IN")
        self.assertEqual(provider.requests[0][2], "en-IN")

    def test_automatic_multilingual_banking_queries_keep_owner_scoped_tool_requirement(self):
        cases = (
            ("What is my account balance?", "en-IN"),
            ("Mere account mein kitna balance hai?", "hi-IN"),
            ("Nanna account balance eshtu?", "kn-IN"),
        )
        for text, expected_language in cases:
            with self.subTest(language=expected_language):
                provider = MockLLMProvider([
                    LLMResponse(tool_calls=(LLMToolCall(
                        "get_account_balance", {}, f"balance-{expected_language}",
                    ),)),
                    LLMResponse(text="The balance data was retrieved."),
                ])
                with patch("services.assistant_service.execute_tool", return_value=[
                    {"account_type": "SAVINGS", "balance": 100.0},
                ]) as execute:
                    result = AssistantOrchestrator(llm_provider=provider).process_turn(
                        self.principal, make_request(text),
                    )
                self.assertEqual(result.response_language, expected_language)
                self.assertEqual(provider.requests[0][2], expected_language)
                self.assertEqual(provider.requests[0][3], frozenset({"get_account_balance"}))
                execute.assert_called_once_with("get_account_balance", self.principal, {})

    def test_native_script_banking_intents_are_tool_gated(self):
        cases = (
            ("\u092e\u0947\u0930\u0947 \u0916\u093e\u0924\u0947 \u0915\u093e \u092c\u0948\u0932\u0947\u0902\u0938 \u0915\u093f\u0924\u0928\u093e \u0939\u0948?", "hi-IN", "get_account_balance"),
            ("\u092e\u0947\u0930\u0947 \u092a\u093f\u091b\u0932\u0947 \u0932\u0947\u0928\u0926\u0947\u0928 \u0926\u093f\u0916\u093e\u0913", "hi-IN", "get_recent_transactions"),
            ("\u092e\u0947\u0930\u093e \u0915\u093e\u0930\u094d\u0921 \u0938\u094d\u091f\u0947\u091f\u0938", "hi-IN", "get_card_details"),
            ("\u092e\u0947\u0930\u0947 \u0932\u094b\u0928 \u0915\u093e \u0935\u093f\u0935\u0930\u0923", "hi-IN", "get_loan_details"),
            ("\u092e\u0947\u0930\u0947 \u0916\u0930\u094d\u091a \u0915\u093e \u0938\u093e\u0930\u093e\u0902\u0936", "hi-IN", "get_spending_summary"),
            ("\u0ca8\u0ca8\u0ccd\u0ca8 \u0c96\u0cbe\u0ca4\u0cc6\u0caf \u0cac\u0ccd\u0caf\u0cbe\u0cb2\u0cc6\u0ca8\u0ccd\u0cb8\u0ccd \u0c8e\u0cb7\u0ccd\u0c9f\u0cc1?", "kn-IN", "get_account_balance"),
            ("\u0ca8\u0ca8\u0ccd\u0ca8 \u0cb9\u0cbf\u0c82\u0ca6\u0cbf\u0ca8 \u0cb5\u0ccd\u0caf\u0cb5\u0cb9\u0cbe\u0cb0\u0c97\u0cb3\u0ca8\u0ccd\u0ca8\u0cc1 \u0ca4\u0ccb\u0cb0\u0cbf\u0cb8\u0cbf", "kn-IN", "get_recent_transactions"),
        )
        for text, language, tool_name in cases:
            with self.subTest(tool=tool_name, language=language):
                provider = MockLLMProvider([
                    LLMResponse(tool_calls=(LLMToolCall(tool_name, {}, "native-call"),)),
                    LLMResponse(text=(
                        "ನಿಮ್ಮ ವಿನಂತಿಸಿದ ದಾಖಲೆ ಲಭ್ಯವಿದೆ."
                        if language == "kn-IN" else "आपका अनुरोधित रिकॉर्ड उपलब्ध है."
                        if language == "hi-IN" else "The requested record is available."
                    )),
                ])
                with patch("services.assistant_service.execute_tool", return_value=[]):
                    response = AssistantOrchestrator(llm_provider=provider).process_turn(
                        self.principal, make_request(text),
                    )
                self.assertEqual(provider.requests[0][3], frozenset({tool_name}))
                self.assertEqual(
                    response.response_language,
                    "en-IN" if tool_name == "get_loan_details" else language,
                )

    def test_transaction_amount_queries_preserve_strict_and_inclusive_boundaries(self):
        cases = (
            ("Show transactions above \u20b92,000", {"min_amount": 2000}, {"min_amount": 2000.01}),
            ("Find payments at least 2000", {"min_amount": 2000}, {"min_amount": 2000}),
            ("List transactions below 2000", {"max_amount": 2000}, {"max_amount": 1999.99}),
        )
        for question, arguments, expected in cases:
            with self.subTest(question=question):
                provider = MockLLMProvider([
                    LLMResponse(tool_calls=(LLMToolCall(
                        "search_transactions", arguments, "amount-filter-call",
                    ),)),
                    LLMResponse(text="The matching transactions were retrieved."),
                ])
                with patch("services.assistant_service.execute_tool", return_value=[]) as execute:
                    AssistantOrchestrator(llm_provider=provider).process_turn(
                        self.principal, make_request(question),
                    )
                self.assertEqual(execute.call_args.args[0], "search_transactions")
                self.assertEqual(execute.call_args.args[1], self.principal)
                self.assertEqual(execute.call_args.args[2], expected)

    def test_context_compaction_keeps_current_question_and_valid_bounded_tool_json(self):
        from services.assistant_service import _compact_llm_messages
        messages = [
            LLMMessage("user", f"Question {index}: " + ("old detail " * 120))
            for index in range(8)
        ]
        messages += [
            LLMMessage("assistant", "Tool call made.", tool_calls=(LLMToolCall(
                "get_recent_transactions", {"limit": 20}, "call-1",
            ),)),
            LLMMessage("tool", json.dumps({"transactions": [
                {"merchant": f"Merchant {index}", "amount": index} for index in range(20)
            ]}), tool_call_id="call-1", tool_name="get_recent_transactions"),
            LLMMessage("user", "What happened most recently?"),
        ]
        compacted, changed = _compact_llm_messages(messages, max_tokens=1000)
        self.assertTrue(changed)
        self.assertIn("What happened most recently?", [item.content for item in compacted])
        tool_message = next(item for item in compacted if item.role == "tool")
        payload = json.loads(tool_message.content)
        self.assertTrue(payload["partial_result"])
        self.assertEqual(len(payload["transactions"]), 8)
        self.assertLessEqual(sum(len(item.content or "") for item in compacted), sum(len(item.content or "") for item in messages))


class GroqAdapterTests(unittest.TestCase):
    def test_missing_api_key_is_rejected_without_sdk_import(self):
        with self.assertRaises(LLMProviderError):
            GroqLLMProvider("", "openai/gpt-oss-20b")

    def test_official_sdk_is_initialized_with_configured_url_lazily(self):
        captured = {}
        module = ModuleType("groq")
        class FakeGroq:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        module.Groq = FakeGroq
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b")
        self.assertIsNone(adapter._client)
        with patch.dict("sys.modules", {"groq": module}):
            adapter._get_client()
        self.assertEqual(captured["base_url"], "https://api.groq.com")
        self.assertEqual(captured["timeout"], 25.0)
        self.assertEqual(captured["api_key"], "offline-test-key")

    def test_normal_text_response(self):
        client, api = mock_groq_client([groq_response("Hello from Groq.")])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        response = adapter.generate(
            [LLMMessage("user", "Hello")], [], response_language="en-IN",
        )
        self.assertEqual(response.text, "Hello from Groq.")
        self.assertEqual(response.tool_calls, ())
        self.assertFalse(api.requests[0]["parallel_tool_calls"])
        self.assertEqual(api.requests[0]["messages"][0]["role"], "system")
        self.assertIn("mirror that mix naturally", api.requests[0]["messages"][0]["content"])
        self.assertEqual(api.requests[0]["messages"][1], {"role": "user", "content": "Hello"})
        self.assertEqual(api.requests[0]["max_completion_tokens"], 1024)

    def test_tool_schemas_are_narrowed_and_parallel_calls_disabled(self):
        client, api = mock_groq_client([groq_response("Done.")])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        tools = [
            LLMTool("get_account_balance", "balance", {"type": "object", "properties": {}}),
            LLMTool("get_recent_transactions", "transactions", {"type": "object", "properties": {}}),
        ]
        adapter.generate(
            [LLMMessage("user", "balance")], tools,
            response_language="en-IN", required_tools=frozenset({"get_account_balance"}),
        )
        self.assertEqual([tool["function"]["name"] for tool in api.requests[0]["tools"]], ["get_account_balance"])
        self.assertFalse(api.requests[0]["parallel_tool_calls"])

    def test_malformed_arguments_fail_closed(self):
        client, _ = mock_groq_client([
            groq_response(tool_calls=[groq_tool_call("bad-1", "get_account_balance", "not-json")]),
        ])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        with self.assertRaises(LLMProviderError):
            adapter.generate(
                [LLMMessage("user", "balance")], [], response_language="en-IN",
            )

    def test_provider_error_does_not_log_exception_contents(self):
        client, _ = mock_groq_client([RuntimeError(
            "private prompt customer 123456789012 balance 278354.00 API key secret-token",
        )])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate([LLMMessage("user", "private prompt")], [], response_language="en-IN")
        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        for sensitive in ("private prompt", "123456789012", "278354.00", "secret-token"):
            self.assertNotIn(sensitive, output)

    def test_bad_request_logs_safe_provider_metadata_and_request_stage(self):
        error = RuntimeError("private message with prompt and account data")
        error.status_code = 400
        error.body = {
            "error": {
                "type": "failed_generation",
                "code": "failed_generation",
                "param": "tools",
                "message": "customer=Demo; balance=278354.00; prompt=private prompt",
            },
            "failed_generation": "private generated tool call and account data",
        }
        error.response = SimpleNamespace(headers={"x-request-id": "req-safe-123"})
        client, _ = mock_groq_client([error])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate(
                    [LLMMessage("user", "private customer transaction question")], [],
                    response_language="en-IN", request_stage="follow_up",
                )

        output = "\n".join(logs.output)
        self.assertIn("http_status=400", output)
        self.assertIn("provider_error_type=failed_generation", output)
        self.assertIn("provider_error_code=failed_generation", output)
        self.assertIn("provider_error_param=tools", output)
        self.assertIn("request_id=req-safe-123", output)
        self.assertIn("failed_generation_present=True", output)
        self.assertIn("model=openai/gpt-oss-20b", output)
        self.assertIn("request_stage=follow_up", output)
        for sensitive in (
            "private message", "278354.00", "private prompt", "customer=Demo",
            "private generated tool call", "private customer transaction question",
        ):
            self.assertNotIn(sensitive, output)

    def test_bad_follow_up_logs_sanitized_error_and_tool_exchange_shape(self):
        error = RuntimeError("provider exception includes customer Arjun and secret-token")
        error.status_code = 400
        error.body = {
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_request_error",
                "param": "messages[3].tool_calls[0].function.name",
                "message": (
                    "Invalid tool call for customer Arjun: account 123456789012, "
                    "balance 278354.00, prompt=private prompt, API key secret-token"
                ),
            },
        }
        error.response = SimpleNamespace(headers={"x-request-id": "req-shape-123"})
        call_id = "call-support-17"
        messages = [
            LLMMessage("user", "Private customer support description."),
            LLMMessage(
                "assistant", "",
                tool_calls=(LLMToolCall(
                    "get_support_ticket", {"ticket_ref": "VS-" + "A" * 32}, call_id,
                ),),
            ),
            LLMMessage(
                "tool", json.dumps({"result": {"subject": "private support details"}}),
                tool_call_id=call_id, tool_name="get_support_ticket",
            ),
        ]
        client, _ = mock_groq_client([error])
        adapter = GroqLLMProvider("offline-api-key", "openai/gpt-oss-20b", client=client)
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate(
                    messages, [], response_language="en-IN", request_stage="follow_up",
                )

        output = "\n".join(logs.output)
        self.assertIn("provider_error_type=invalid_request_error", output)
        self.assertIn("provider_error_code=invalid_request_error", output)
        self.assertIn("provider_error_param=messages", output)
        self.assertIn("provider_error_description=Provider rejected the generated tool-call structure.", output)
        self.assertIn("request_id=req-shape-123", output)
        self.assertIn("http_status=400", output)
        self.assertIn("model=openai/gpt-oss-20b", output)
        self.assertIn("request_stage=follow_up", output)
        self.assertIn("message_count=4", output)  # system message plus the three supplied messages
        self.assertIn("message_roles=system,user,assistant,tool", output)
        self.assertIn("assistant_tool_call_count=1", output)
        self.assertIn("tool_result_count=1", output)
        self.assertIn("tool_names=get_support_ticket", output)
        self.assertIn("tool_call_result_ids_match=True", output)
        self.assertIn("tool_declaration_count=0", output)
        self.assertIn("tool_choice=none", output)
        self.assertIn("parallel_tool_calls=False", output)
        self.assertIn("max_completion_tokens=1024", output)
        for sensitive in (
            "Arjun", "123456789012", "278354.00", "private prompt", "secret-token",
            "offline-api-key", "Private customer support description", "private support details",
            "VS-" + "A" * 32,
        ):
            self.assertNotIn(sensitive, output)

    def test_bad_follow_up_reports_mismatched_call_result_ids_without_values(self):
        error = RuntimeError("invalid tool call")
        error.status_code = 400
        error.body = {"error": {"type": "invalid_request_error", "message": "invalid tool call"}}
        client, _ = mock_groq_client([error])
        adapter = GroqLLMProvider("offline-api-key", "openai/gpt-oss-20b", client=client)
        messages = [
            LLMMessage("assistant", "", tool_calls=(LLMToolCall("get_account_balance", {}, "call-a"),)),
            LLMMessage("tool", "{\"result\":\"redacted\"}", tool_call_id="call-b", tool_name="get_account_balance"),
        ]
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate(messages, [], response_language="en-IN", request_stage="follow_up")
        output = "\n".join(logs.output)
        self.assertIn("tool_call_result_ids_match=False", output)
        self.assertNotIn("call-a", output)
        self.assertNotIn("call-b", output)

    def test_429_respects_retry_after_once_and_logs_only_safe_dimensions(self):
        error = RuntimeError("prompt and account data must not be logged")
        error.status_code = 429
        error.body = {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded", "param": None}}
        error.response = SimpleNamespace(headers={
            "retry-after": "0", "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-remaining-tokens": "17",
            "x-ratelimit-reset-requests": "2m59.56s",
            "x-ratelimit-reset-tokens": "7.66s",
            "authorization": "secret",
        })
        client, api = mock_groq_client([error, groq_response("Recovered")])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        with patch.dict(os.environ, {"VAANISHIELD_GROQ_MAX_RETRIES": "1"}), \
             patch("services.llm_adapters._GROQ_RATE_LIMIT_UNTIL", 0.0), \
             patch("services.llm_adapters.time.sleep") as sleep, \
             self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            response = adapter.generate([LLMMessage("user", "private customer prompt")], [], response_language="en-IN")
        self.assertEqual(response.text, "Recovered")
        self.assertEqual(len(api.requests), 2)
        sleep.assert_called_once_with(0.0)
        output = "\n".join(logs.output)
        self.assertIn("http_status=429", output)
        self.assertIn("remaining-requests=0", output)
        self.assertIn("remaining-tokens=17", output)
        self.assertIn("reset-requests=2m59.56s", output)
        self.assertIn("reset-tokens=7.66s", output)
        for secret in ("prompt and account data", "private customer prompt", "secret"):
            self.assertNotIn(secret, output)

    def test_429_retry_exhaustion_uses_shared_cooldown_without_retry_storm(self):
        error = RuntimeError("sensitive provider message")
        error.status_code = 429
        error.body = {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}
        error.response = SimpleNamespace(headers={"retry-after": "60"})
        client, api = mock_groq_client([error, groq_response("must not be reached")])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        with patch.dict(os.environ, {"VAANISHIELD_GROQ_MAX_RETRIES": "1", "VAANISHIELD_GROQ_RETRY_WAIT_CAP_SECONDS": "2"}), \
             patch("services.llm_adapters._GROQ_RATE_LIMIT_UNTIL", 0.0), \
             self.assertLogs("services.llm_adapters", level="WARNING"):
            with self.assertRaises(LLMRateLimited):
                adapter.generate([LLMMessage("user", "prompt")], [], response_language="en-IN")
            with self.assertRaises(LLMRateLimited):
                adapter.generate([LLMMessage("user", "another prompt")], [], response_language="en-IN")
        self.assertEqual(len(api.requests), 1)

    def test_concurrent_groq_requests_are_bounded(self):
        lock = threading.Lock()
        active = 0
        maximum = 0
        class Completions:
            def create(self, **_kwargs):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                with lock:
                    active -= 1
                return groq_response("ok")
        adapter = GroqLLMProvider(
            "offline-test-key", "openai/gpt-oss-20b",
            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        )
        with patch("services.llm_adapters._GROQ_RATE_LIMIT_UNTIL", 0.0):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _index: adapter.generate(
                        [LLMMessage("user", "hello")], [], response_language="en-IN",
                    ).text,
                    range(8),
                ))
        self.assertEqual(results, ["ok"] * 8)
        self.assertLessEqual(maximum, 4)

    def test_groq_tool_use_failed_recovers_for_initial_and_follow_up_stages(self):
        tool_error = RuntimeError("Tool choice is required, but model did not call a tool")
        tool_error.status_code = 400
        tool_error.body = {
            "error": {
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "message": "Tool choice is required, but model did not call a tool",
            },
        }
        # 1. Single required zero-arg tool recovers deterministically without extra network call
        client, api = mock_groq_client([tool_error])
        adapter = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client)
        balance_tool = LLMTool("get_account_balance", "balance", {"type": "object", "properties": {}})
        recovered = adapter.generate(
            [LLMMessage("user", "What is my savings balance?")],
            [balance_tool],
            response_language="en-IN",
            required_tools=frozenset({"get_account_balance"}),
            request_stage="initial",
        )
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(len(recovered.tool_calls), 1)
        self.assertEqual(recovered.tool_calls[0].name, "get_account_balance")
        self.assertEqual(recovered.tool_calls[0].arguments, {})

        # 2. Parameterized required tool on follow_up scopes to current turn when prior history causes tool_use_failed
        ticket_tool = LLMTool(
            "get_support_ticket",
            "ticket",
            {"type": "object", "properties": {"ticket_ref": {"type": "string"}}, "required": ["ticket_ref"]},
        )
        client2, api2 = mock_groq_client([
            tool_error,
            groq_response(tool_calls=[groq_tool_call("call-2", "get_support_ticket", '{"ticket_ref":"VS-' + "A" * 32 + '"}')]),
        ])
        adapter2 = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client2)
        msgs = [
            LLMMessage("user", "Earlier question"),
            LLMMessage("assistant", "Earlier ticket summary"),
            LLMMessage("user", "Show event history for VS-" + "A" * 32),
            LLMMessage("assistant", "", tool_calls=(LLMToolCall("get_support_ticket_events", {"ticket_ref": "VS-" + "A" * 32}, "call-1"),)),
            LLMMessage("tool", '{"result":[]}', tool_call_id="call-1", tool_name="get_support_ticket_events"),
        ]
        recovered2 = adapter2.generate(
            msgs,
            [ticket_tool],
            response_language="en-IN",
            required_tools=frozenset({"get_support_ticket"}),
            request_stage="follow_up",
        )
        self.assertEqual(len(api2.requests), 2)
        self.assertEqual(len(api2.requests[1]["messages"]), 4)  # system + current user + assistant tool_call + tool result
        self.assertEqual(recovered2.tool_calls[0].name, "get_support_ticket")

        # 3. Final follow_up with tool_choice=none recovers by flattening tool messages if model attempts another tool call
        none_error = RuntimeError("Tool choice is none, but model called a tool")
        none_error.status_code = 400
        none_error.body = {
            "error": {
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "message": "Tool choice is none, but model called a tool",
            },
        }
        client3, api3 = mock_groq_client([none_error, groq_response("Your savings balance is ₹278,354.00.")])
        adapter3 = GroqLLMProvider("offline-test-key", "openai/gpt-oss-20b", client=client3)
        followup_msgs = [
            LLMMessage("user", "What is my balance?"),
            LLMMessage("assistant", "", tool_calls=(LLMToolCall("get_account_balance", {}, "call-bal"),)),
            LLMMessage("tool", '{"result":[{"balance":278354.0}]}', tool_call_id="call-bal", tool_name="get_account_balance"),
        ]
        recovered3 = adapter3.generate(
            followup_msgs,
            [],
            response_language="en-IN",
            required_tools=frozenset(),
            request_stage="follow_up",
        )
        self.assertEqual(recovered3.text, "Your savings balance is ₹278,354.00.")
        self.assertEqual(len(api3.requests), 2)
        self.assertTrue(all(m["role"] != "tool" for m in api3.requests[1]["messages"]))


class SpeechPresentationTests(unittest.TestCase):
    def test_speech_text_removes_visual_markup_and_speaks_table_fields(self):
        visual = (
            "## Monthly summary\n\n| Month | Credits | Debits |\n|---|---:|---:|\n"
            "| October | INR 2,000 | INR 750 |\n\n"
            "- Estimated savings: **INR 1,250**.\nUpdated 2026-10-31T12:00:00Z"
        )
        spoken = speech_friendly_text(visual)
        self.assertIn("Month: October. Credits: INR 2,000. Debits: INR 750.", spoken)
        self.assertIn("Estimated savings: INR 1,250.", spoken)
        self.assertIn("October 31, 2026", spoken)
        self.assertNotIn("|", spoken)
        self.assertNotIn("**", spoken)

    def test_response_language_tracks_observable_native_script(self):
        self.assertEqual(_response_language_for_text("ನಿಮ್ಮ ಖಾತೆಯ ಬಾಕಿ ವಿವರ ಇಲ್ಲಿದೆ", "en-IN"), "kn-IN")
        self.assertEqual(_response_language_for_text("आपका खाता शेष उपलब्ध है", "kn-IN"), "hi-IN")
        self.assertEqual(_response_language_for_text("Your account balance is available.", "kn-IN"), "en-IN")


class GeminiAdapterTests(unittest.TestCase):
    class Types:
        class Part:
            def __init__(self, **kwargs): self.kwargs = kwargs
            @staticmethod
            def from_text(*, text): return ("text", text)
            @staticmethod
            def from_function_call(*, name, args): return ("call", name, args)
            @staticmethod
            def from_function_response(*, name, response, id=None): return ("result", name, response, id)
        class Content:
            def __init__(self, *, role, parts): self.role, self.parts = role, parts
        class FunctionDeclaration:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class FunctionCall:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class Tool:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class FunctionCallingConfig:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class ToolConfig:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class AutomaticFunctionCallingConfig:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class HttpOptions:
            def __init__(self, **kwargs): self.kwargs = kwargs
        class GenerateContentConfig:
            def __init__(self, **kwargs): self.kwargs = kwargs

    def test_request_construction_disables_sdk_auto_execution(self):
        class ModelAPI:
            def generate_content(inner, **kwargs):
                inner.kwargs = kwargs
                return type("Response", (), {"text": "Done", "function_calls": []})()
        class Client:
            def __init__(self): self.models = ModelAPI()
        client = Client()
        adapter = GeminiLLMProvider("test-key", "gemini-3.8-flash", client=client, sdk_types=self.Types)
        from services.llm_interfaces import LLMTool
        answer = adapter.generate(
            [LLMMessage("user", "What is my balance?")],
            [LLMTool("get_account_balance", "Get balance", {"type": "object", "properties": {}})],
            response_language="en-IN",
        )
        self.assertEqual(answer.text, "Done")
        self.assertEqual(client.models.kwargs["model"], "gemini-3.8-flash")
        config = client.models.kwargs["config"]
        self.assertTrue(config.kwargs["automatic_function_calling"].kwargs["disable"])
        adapter.generate(
            [LLMMessage("user", "What is my balance?")],
            [LLMTool("get_account_balance", "Get balance", {"type": "object", "properties": {}})],
            response_language="en-IN", required_tools=frozenset({"get_account_balance"}),
        )
        forced = client.models.kwargs["config"].kwargs["tool_config"].kwargs["function_calling_config"]
        self.assertEqual(forced.kwargs["mode"], "ANY")
        self.assertEqual(forced.kwargs["allowed_function_names"], ["get_account_balance"])

    def test_installed_google_sdk_builds_forced_tool_request_without_network(self):
        from google.genai import types
        class ModelAPI:
            def generate_content(inner, **kwargs):
                inner.kwargs = kwargs
                return type("Response", (), {"text": "Verified", "function_calls": []})()
        class Client:
            def __init__(self): self.models = ModelAPI()
        client = Client()
        adapter = GeminiLLMProvider("offline-test-credential", "gemini-3.8-flash", client=client, sdk_types=types)
        adapter.generate(
            [LLMMessage("user", "What is my balance?")],
            [LLMTool("get_account_balance", "Get balance", {"type": "object", "properties": {}})],
            response_language="en-IN", required_tools=frozenset({"get_account_balance"}),
        )
        config = client.models.kwargs["config"]
        self.assertIsInstance(config, types.GenerateContentConfig)
        self.assertEqual(config.tool_config.function_calling_config.mode, "ANY")
        self.assertEqual(config.tool_config.function_calling_config.allowed_function_names, ["get_account_balance"])

    def test_provider_failure_logs_only_allowlisted_error_metadata(self):
        from google.genai.errors import ServerError

        class Response:
            headers = {
                "x-goog-request-id": "request-123",
                "retry-after": "2",
                "authorization": "never-log-this-header",
            }

        error = ServerError(503, {
            "error": {
                "code": 503,
                "status": "UNAVAILABLE",
                "message": "sensitive response body text",
                "details": [{
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "2s",
                }, {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [{
                        "field": "contents[1].role",
                        "description": "sensitive prompt/customer data",
                    }, {
                        "field": "contents[1].parts[0].function_response.response.private_value",
                        "description": "do not expose this value",
                    }],
                }],
            },
        }, Response())

        class ModelAPI:
            def generate_content(inner, **kwargs):
                raise error
        class Client:
            def __init__(self): self.models = ModelAPI()

        adapter = GeminiLLMProvider("offline-test-credential", "gemini-3.8-flash", client=Client(), sdk_types=self.Types)
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate([LLMMessage("user", "private customer prompt")], [], response_language="en-IN")

        output = "\n".join(logs.output)
        self.assertIn("exception_class=ServerError", output)
        self.assertIn("http_status=503", output)
        self.assertIn("provider_error_code=UNAVAILABLE", output)
        self.assertIn("request_id=request-123", output)
        self.assertIn("retry_after=2", output)
        self.assertIn("retry_delay=2s", output)
        self.assertIn("provider_detail_types=google.rpc.BadRequest,google.rpc.RetryInfo", output)
        self.assertIn("invalid_fields=contents[1].role", output)
        self.assertNotIn("sensitive response body", output)
        self.assertNotIn("sensitive prompt/customer data", output)
        self.assertNotIn("do not expose this value", output)
        self.assertNotIn("never-log-this-header", output)
        self.assertNotIn("private customer prompt", output)

    def test_invalid_argument_message_is_redacted_and_request_stage_is_recorded(self):
        from google.genai import types
        from google.genai.errors import ClientError

        error = ClientError(400, {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": (
                    'Invalid value at \'contents[1].role\' (TYPE_STRING), "tool"; '
                    'prompt="private customer question" api_key=AIzaSyFakeSensitive '
                    'balance=₹987654.32'
                ),
                "details": [{
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [{
                        "field": "contents[1].role",
                        "description": "customer balance ₹987654.32 and prompt text",
                    }, {
                        "field": "contents[2].parts[0].functionResponse.id",
                        "description": "API key and account values omitted",
                    }],
                }],
            },
        })

        class ModelAPI:
            def generate_content(inner, **kwargs):
                raise error
        class Client:
            def __init__(self): self.models = ModelAPI()

        adapter = GeminiLLMProvider(
            "offline-test-credential", "offline-model-id", client=Client(), sdk_types=types,
        )
        messages = [
            LLMMessage("user", "private customer question"),
            LLMMessage("assistant", tool_calls=(LLMToolCall(
                "get_account_balance", {}, "call-id-1",
            ),)),
            LLMMessage("tool", '{"result":{"balance":987654.32}}',
                       tool_call_id="call-id-1", tool_name="get_account_balance"),
        ]
        with self.assertLogs("services.llm_adapters", level="WARNING") as logs:
            with self.assertRaises(LLMProviderError):
                adapter.generate(
                    messages,
                    [LLMTool("get_account_balance", "Get balance", {"type": "object", "properties": {}})],
                    response_language="en-IN", required_tools=frozenset(), request_stage="follow_up",
                )

        output = "\n".join(logs.output)
        self.assertIn("http_status=400", output)
        self.assertIn("provider_error_code=INVALID_ARGUMENT", output)
        self.assertIn("provider_error_category=invalid_content_role", output)
        self.assertIn("provider_error_field=contents[1].role", output)
        self.assertIn("provider_message=Content role must be either user or model.", output)
        self.assertIn("contents[2].parts[0].functionResponse.id", output)
        self.assertIn("model=offline-model-id", output)
        self.assertIn("sdk_version=", output)
        self.assertIn("request_stage=follow_up", output)
        self.assertIn("roles=user,model,user", output)
        self.assertIn("function_call:get_account_balance", output)
        self.assertIn("function_response:get_account_balance", output)
        self.assertIn("invalid_fields=contents[1].role", output)
        for sensitive in (
            "private customer question", "AIzaSyFakeSensitive", "987654.32",
            "customer balance", "call-id-1",
        ):
            self.assertNotIn(sensitive, output)

    def test_provider_error_message_categories_never_echo_untrusted_text(self):
        from types import SimpleNamespace
        from services.llm_adapters import _safe_provider_error_message

        cases = [
            (
                "Invalid value at 'contents[2].parts[0].functionResponse.id', "
                "'call-123 account 123456789012 amount 278354.00 API key AIzaSySecret'",
                "invalid_request_value", "contents[2].parts[0].functionResponse.id",
            ),
            (
                "Function call id does not match customer prompt: private customer details",
                "function_call_id_mismatch", "-",
            ),
            (
                "Missing required field 'contents[4].parts[0].functionResponse.response'; "
                "account=123456789012 balance=278354.00",
                "missing_required_field", "contents[4].parts[0].functionResponse.response",
            ),
            (
                "Unknown name \"customer@email.example\" at 'tools[0].functionDeclarations': "
                "Cannot find field; bearer abcdefghijklmnop",
                "unknown_request_field", "tools[0].functionDeclarations",
            ),
            (
                "Unexpected response with account 123456789012 and balance ₹278354.00",
                "unclassified_provider_error", "-",
            ),
        ]
        sensitive_fragments = (
            "call-123", "123456789012", "278354.00", "AIzaSySecret",
            "private customer details", "customer@email.example", "abcdefghijklmnop",
        )
        for raw_message, expected_category, expected_field in cases:
            with self.subTest(expected_category=expected_category):
                safe = _safe_provider_error_message(SimpleNamespace(
                    message=raw_message,
                    status="INTERNAL" if expected_category == "unclassified_provider_error" else "INVALID_ARGUMENT",
                ))
                self.assertEqual(safe["category"], expected_category)
                self.assertEqual(safe["field"], expected_field)
                for fragment in sensitive_fragments:
                    self.assertNotIn(fragment, safe["description"])


class SpeechDiagnosticTests(unittest.TestCase):
    def test_timeout_has_safe_specific_message_and_code(self):
        from services.speech_storage import store_audio_upload
        from backend.test_speech_integration import _wav_bytes
        import services.speech_adapters as adapters
        principal = make_principal()
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(adapters, "gnani_speech") as gnani:
                gnani.API_KEY = "offline-test-key"
                gnani.transcribe_audio.side_effect = __import__("requests").Timeout("secret detail")
                with patch("services.speech_adapters.consume_audio_upload") as consume:
                    from contextlib import contextmanager
                    @contextmanager
                    def fake_consume(*args):
                        path = Path(temp) / "audio.wav"
                        path.write_bytes(_wav_bytes())
                        yield path
                    consume.side_effect = fake_consume
                    with self.assertRaises(InputProcessingUnavailable) as failure:
                        GnaniPrismaTranscriber().transcribe(principal, uuid4(), "auto")
        self.assertEqual(failure.exception.code, "SPEECH_TIMEOUT")
        self.assertNotIn("secret detail", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
