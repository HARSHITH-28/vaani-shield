"""Phase 3 verification tests for VaaniShield 2.0 assistant, voice, scam, and Copilot UI refinements."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from frontend.components.fintech_ui import parse_markdown_table_to_compact_cards
from frontend.pages.assistant import create_new_conversation
from frontend.session_state import initialize_session_state
from services import assistant_service, banking_service, financial_intelligence
from services.assistant_service import AssistantTurnRequest
from services.llm_adapters import LLMResponse, LLMToolCall
from services.principal import CustomerPrincipal
from services.scam_risk_engine import RiskLevel, analyze_scam_text


def _arjun_principal() -> CustomerPrincipal:
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key="demo-arjun",
        session_id=uuid4(),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class _ScriptedLLMProvider:
    provider_name = "scripted"
    model_name = "scripted-phase3"

    def __init__(self, final_text: str):
        self.final_text = final_text

    def generate(self, messages, tools, *, response_language="en-IN", required_tools=frozenset(), request_stage="initial"):
        if required_tools:
            tool_name = sorted(required_tools)[0]
            return LLMResponse(
                text="",
                tool_calls=(LLMToolCall(call_id=f"call-{tool_name}", name=tool_name, arguments={}),),
            )
        return LLMResponse(text=self.final_text, tool_calls=())


class Phase3RefinementTests(unittest.TestCase):
    def test_combined_and_standalone_loan_emi_queries_return_consistent_authoritative_emi(self):
        combined_query = (
            "Show me my savings account balance, my last 5 transactions, "
            "and the next EMI due date for my active loan."
        )
        required = assistant_service._required_tools_for_question(combined_query)
        self.assertEqual(
            required,
            frozenset({"get_account_balance", "get_recent_transactions", "get_loan_details"}),
        )

        # Verify standalone loan query formatting includes authoritative EMI & due date
        loans = banking_service.get_loan_details(4)
        active = next(item for item in loans if item["status"] == "ACTIVE")
        self.assertEqual(active["next_emi_date"], "2026-12-05")
        self.assertEqual(active["monthly_emi"], 13080.0)
        self.assertEqual(active["remaining_tenure_months"], 32)

        orchestrator = assistant_service.AssistantOrchestrator(
            llm_provider=_ScriptedLLMProvider(
                "1. Balance: ₹278,354.00\n"
                "2. Last 5 transactions: BigBasket ₹2,450.00, Swiggy ₹680.00.\n"
                "3. Active PERSONAL loan: Monthly EMI ₹13,080.00, Next EMI due date 2026-12-05 (32 months remaining)."
            )
        )
        principal = _arjun_principal()
        combined_resp = orchestrator.process_turn(
            principal,
            AssistantTurnRequest(
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text=combined_query,
            ),
        )
        self.assertIn("278,354.00", combined_resp.assistant_text)
        self.assertIn("13,080.00", combined_resp.assistant_text)
        self.assertIn("2026-12-05", combined_resp.assistant_text)

        standalone_resp = orchestrator.process_turn(
            principal,
            AssistantTurnRequest(
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text="What is the next EMI due date for my active loan?",
            ),
        )
        self.assertIn("2026-12-05", standalone_resp.assistant_text)
        self.assertIn("13,080.00", standalone_resp.assistant_text)
        self.assertIn("remaining tenure 32 months", standalone_resp.assistant_text)
        self.assertNotIn("Remaining tenure is not available", standalone_resp.assistant_text)

    def test_combined_loan_query_recovers_when_llm_falsely_claims_emi_unavailable(self):
        combined_query = (
            "Show me my savings account balance, my last 5 transactions, "
            "and the next EMI due date for my active loan."
        )
        orchestrator = assistant_service.AssistantOrchestrator(
            llm_provider=_ScriptedLLMProvider(
                "Your savings balance is ₹278,354.00.\n"
                "Recent transactions include BigBasket ₹2,450.00.\n"
                "Next EMI due date is unavailable in the stored loan records."
            )
        )
        resp = orchestrator.process_turn(
            _arjun_principal(),
            AssistantTurnRequest(
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text=combined_query,
            ),
        )
        self.assertIn("2026-12-05", resp.assistant_text)
        self.assertIn("13,080.00", resp.assistant_text)
        self.assertNotIn("unavailable in the stored loan records", resp.assistant_text)

    def test_kannada_and_english_category_queries_route_to_same_category_breakdown_tool(self):
        kannada_query = "ಕಳೆದ ತಿಂಗಳು ನಾನು ಯಾವ ವಿಭಾಗದಲ್ಲಿ ಎಷ್ಟು ಹಣ ಖರ್ಚು ಮಾಡಿದ್ದೇನೆ? ಹೆಚ್ಚು ಖರ್ಚಾದ ವಿಭಾಗ ಯಾವುದು?"
        english_query = "Show my expenditure category-wise for October 2026 and identify my highest spending category."

        kn_tools = assistant_service._required_tools_for_question(kannada_query)
        en_tools = assistant_service._required_tools_for_question(english_query)
        self.assertEqual(kn_tools, frozenset({"get_category_breakdown"}))
        self.assertEqual(en_tools, frozenset({"get_category_breakdown"}))

        categories = financial_intelligence.get_category_breakdown(4)
        self.assertEqual(categories[0]["category"], "GROCERIES")
        self.assertEqual(categories[0]["total_spent"], 12950.0)

    def test_scam_detection_reported_bank_call_otp_reversal_and_link_is_critical_not_low(self):
        scam_prompt = (
            "Someone claiming to be from my bank called and asked for my OTP to reverse "
            "a suspicious ₹15,000 transaction. They also sent me a link. Is this a scam, and what should I do?"
        )
        required = assistant_service._required_tools_for_question(scam_prompt)
        self.assertEqual(required, frozenset({"assess_current_scam_message"}))
        self.assertNotIn("assess_transaction_risk", required)

        result = analyze_scam_text(scam_prompt)
        self.assertIn(result.risk_level, {RiskLevel.HIGH, RiskLevel.CRITICAL})
        self.assertEqual(result.risk_level, RiskLevel.CRITICAL)
        indicator_cats = {item.category for item in result.indicators}
        self.assertIn("credential_request", indicator_cats)
        self.assertIn("authority_impersonation", indicator_cats)
        self.assertIn("account_threat", indicator_cats)
        self.assertIn("suspicious_link", indicator_cats)

        orchestrator = assistant_service.AssistantOrchestrator(
            llm_provider=_ScriptedLLMProvider("Scam assessment complete.")
        )
        turn_resp = orchestrator.process_turn(
            _arjun_principal(),
            AssistantTurnRequest(
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text=scam_prompt,
            ),
        )
        self.assertIsNotNone(turn_resp.scam_assessment)
        self.assertEqual(turn_resp.scam_assessment.risk_level, "CRITICAL")
        self.assertIsNone(turn_resp.transaction_risk_assessment)
        self.assertIn("Risk level: CRITICAL", turn_resp.assistant_text)
        self.assertIn("CVV", turn_resp.assistant_text)
        self.assertIn("1930", turn_resp.assistant_text)
        self.assertIn("advisory security assessment", turn_resp.assistant_text)

    def test_kannada_english_code_mixed_support_retains_kannada_language(self):
        code_mixed_input = "ನನ್ನ UPI transaction fail ಆಗಿದೆ, ₹500 debit ಆಗಿದೆ ಆದರೆ merchant ಗೆ ತಲುಪಿಲ್ಲ. ದೂರು ದಾಖಲಿಸಿ."
        lang = assistant_service._preferred_response_language(code_mixed_input, ("kn-IN", "en-IN"))
        self.assertEqual(lang, "kn-IN")

        orchestrator = assistant_service.AssistantOrchestrator(
            llm_provider=_ScriptedLLMProvider("ನಿಮ್ಮ UPI transaction ದೂರಿಗಾಗಿ priority ಮತ್ತು subject ತಿಳಿಸಿ.")
        )
        principal = _arjun_principal()
        turn1 = orchestrator.process_turn(
            principal,
            AssistantTurnRequest(
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text=code_mixed_input,
            ),
        )
        self.assertEqual(turn1.response_language, "kn-IN")

        # Follow-up with English banking slot terms should keep kn-IN in the same support flow
        orchestrator2 = assistant_service.AssistantOrchestrator(
            llm_provider=_ScriptedLLMProvider("ನಿಮ್ಮ UPI payment debited ದೂರು HIGH priority ನಲ್ಲಿ ಸಿದ್ಧವಾಗಿದೆ.")
        )
        turn2 = orchestrator2.process_turn(
            principal,
            AssistantTurnRequest(
                conversation_id=turn1.conversation_id,
                client_turn_id=uuid4(),
                input_mode="text",
                output_mode="text",
                text="Priority: HIGH, Subject: UPI payment debited",
            ),
        )
        self.assertEqual(turn2.response_language, "kn-IN")

    def test_wake_word_greeting_is_english_and_filters_english_voices(self):
        wake_src = Path("frontend/components/wake_word.py").read_text(encoding="utf-8")
        floating_src = Path("frontend/components/floating_assistant.py").read_text(encoding="utf-8")
        self.assertIn("Hello! I'm Vaani. How can I help you today?", wake_src)
        self.assertIn("v.lang.toLowerCase().startsWith('en')", wake_src)
        self.assertIn("Hello! I'm Vaani. How can I help you today?", floating_src)

    def test_markdown_table_prose_converts_bold_markers_and_copilot_has_sticky_plus_button(self):
        sample = (
            "**1. Savings Account Balance**\n"
            "Your available balance is **₹278,354.00**.\n\n"
            "| Date | Merchant | Category | Type | Amount | Status |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 2026-11-05 | **BigBasket** | GROCERIES | DEBIT | **₹2,450.00** | SUCCESS |\n\n"
            "**3. Next EMI Due Date**\n"
            "- Active Personal Loan EMI: **₹13,080.00** due on **2026-12-05**"
        )
        converted, rendered = parse_markdown_table_to_compact_cards(sample)
        self.assertTrue(converted)
        self.assertNotIn("**", rendered)
        self.assertIn("<strong>1. Savings Account Balance</strong>", rendered)
        self.assertIn("<strong>₹278,354.00</strong>", rendered)
        self.assertIn("<strong>3. Next EMI Due Date</strong>", rendered)

        floating_src = Path("frontend/components/floating_assistant.py").read_text(encoding="utf-8")
        self.assertIn('st.button("+", key="floating-assistant-new", help="New conversation")', floating_src)

        state = {}
        initialize_session_state(state)
        state["conversation_messages"] = [{"id": "1", "role": "user", "text": "Old message"}]
        state["support_action_outcomes"] = {"create:1": {"status": "pending"}}

        class _FakeAPI:
            def create_conversation(self):
                return {"conversation_id": str(uuid4()), "created_at": "2026-11-07T00:00:00Z"}

        new_conv = create_new_conversation(_FakeAPI(), state)
        self.assertEqual(state["conversation_id"], new_conv["conversation_id"])
        self.assertEqual(state["conversation_messages"], [])
        self.assertEqual(state["support_action_outcomes"], {})


if __name__ == "__main__":
    unittest.main()
