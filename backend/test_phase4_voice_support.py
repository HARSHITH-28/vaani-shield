"""Tests for Phase 4 refinements: Voice-based support ticket flow, drafts, notifications, and link warnings."""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.api_models import AssistantTurnRequest
from services import assistant_service, conversation_service, database
from services.assistant_service import AssistantOrchestrator
from services.banking_service import get_loan_details
from services.llm_adapters import LLMResponse, LLMToolCall
from services.notification_service import (
    get_unread_count,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    sync_notifications_for_customer,
)
from services.principal import CustomerPrincipal
from services.scam_risk_engine import analyze_scam_text
from services.support_draft_service import (
    cancel_draft,
    get_active_draft,
    save_or_update_draft,
)


class _ScriptedSupportLLMProvider:
    provider_name = "scripted"
    model_name = "scripted-phase4"

    def __init__(self, final_text: str = ""):
        self.final_text = final_text

    def generate(self, messages, tools, *, response_language="en-IN", required_tools=frozenset(), request_stage="initial"):
        if required_tools:
            tool_name = sorted(required_tools)[0]
            return LLMResponse(
                text="",
                tool_calls=(LLMToolCall(call_id=f"call-{tool_name}", name=tool_name, arguments={}),),
            )
        return LLMResponse(text=self.final_text, tool_calls=())


class TestPhase4VoiceSupportFlow(unittest.TestCase):
    def setUp(self):
        database.initialize_database()
        now = datetime.now(timezone.utc)
        self.principal = CustomerPrincipal(
            customer_key="demo-arjun",
            session_id=uuid4(),
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
        )
        self.conversation = conversation_service.create_conversation(self.principal)
        self.conv_id = self.conversation["conversation_id"]
        self.orchestrator = AssistantOrchestrator(
            llm_provider=_ScriptedSupportLLMProvider()
        )

    def tearDown(self):
        cancel_draft(self.principal.customer_key, str(self.conv_id))

    def test_voice_support_ticket_multi_turn_persists_and_merges_slots(self):
        """Verify Turn 1 (amount/debit), Turn 2 (Kannada priority & subject), Turn 3 (review prompt), Turn 4 (submit)."""
        # Turn 1: User reports UPI debit issue
        req1 = AssistantTurnRequest(
            conversation_id=self.conv_id,
            client_turn_id=uuid4(),
            input_mode="text",
            output_mode="text",
            text="I made a UPI transaction of ₹2,500. The amount was debited from my account, but the merchant has not received the payment.",
            response_language="en-IN",
        )
        resp1 = self.orchestrator.process_turn(self.principal, req1)
        self.assertTrue(resp1.assistant_text)
        # Verify priority is asked
        self.assertTrue(
            "high priority" in resp1.assistant_text.lower()
            or "priority" in resp1.assistant_text.lower()
        )

        # Verify active draft was created and saved in SQLite
        draft1 = get_active_draft(self.principal.customer_key, str(self.conv_id))
        self.assertIsNotNone(draft1)
        self.assertEqual(draft1["amount"], 2500.0)
        self.assertEqual(draft1["category"], "transaction")
        self.assertEqual(draft1["debit_confirmed"], 1)

        # Turn 2: User responds in Kannada with priority and subject
        req2 = AssistantTurnRequest(
            conversation_id=self.conv_id,
            client_turn_id=uuid4(),
            input_mode="text",
            output_mode="text",
            text="ಹೌದು, high priority ಹಾಕು. subject UPI payment not received ಅಂತ ಇಡು.",
            response_language="kn-IN",
        )
        resp2 = self.orchestrator.process_turn(self.principal, req2)
        # Verify draft is now proposed and slots are merged
        draft2 = get_active_draft(self.principal.customer_key, str(self.conv_id))
        self.assertIsNotNone(draft2)
        self.assertEqual(draft2["amount"], 2500.0)
        self.assertEqual(draft2["category"], "transaction")
        self.assertEqual(draft2["priority"], "HIGH")
        self.assertTrue(
            "UPI payment not received" in draft2["subject"]
            and "ಅಂತ ಇಡು" not in draft2["subject"]
        )

        # Verify assistant response text contains FULL structured review details in Kannada
        self.assertIn("ಪರಿಶೀಲನೆಗಾಗಿ", resp2.assistant_text)
        self.assertIn("ವರ್ಗ", resp2.assistant_text)
        self.assertIn("ವಿಷಯ", resp2.assistant_text)
        self.assertIn("ಆದ್ಯತೆ", resp2.assistant_text)
        self.assertIn("₹2,500.00", resp2.assistant_text)
        self.assertIn("ಖಚಿತಪಡಿಸುತ್ತೀರಾ", resp2.assistant_text)

        # Turn 3: User says "ನನಗೆ ದೂರಿನ ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ."
        req3 = AssistantTurnRequest(
            conversation_id=self.conv_id,
            client_turn_id=uuid4(),
            input_mode="text",
            output_mode="text",
            text="ನನಗೆ ದೂರಿನ ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ.",
            response_language="kn-IN",
        )
        resp3 = self.orchestrator.process_turn(self.principal, req3)
        # Must re-display the structured proposal, NOT fall back to list_support_tickets
        self.assertIn("ಪರಿಶೀಲನೆಗಾಗಿ", resp3.assistant_text)
        self.assertIn("ವರ್ಗ", resp3.assistant_text)
        self.assertIn("ವಿಷಯ", resp3.assistant_text)
        self.assertIn("₹2,500.00", resp3.assistant_text)

        # Turn 4: User explicitly confirms verbally
        req4 = AssistantTurnRequest(
            conversation_id=self.conv_id,
            client_turn_id=uuid4(),
            input_mode="text",
            output_mode="text",
            text="ಹೌದು, ಸಲ್ಲಿಸು",
            response_language="kn-IN",
        )
        resp4 = self.orchestrator.process_turn(self.principal, req4)
        self.assertIn("ರಚಿಸಲಾಗಿದೆ", resp4.assistant_text)
        self.assertIn("VS-", resp4.assistant_text)

        # Draft should now be submitted
        draft4 = get_active_draft(self.principal.customer_key, str(self.conv_id))
        self.assertIsNone(draft4)  # No longer in ('collecting', 'proposed')


class TestPhase4NotificationCenter(unittest.TestCase):
    def setUp(self):
        database.initialize_database()
        self.customer_key = "demo-arjun"

    def test_sync_notifications_loan_emi_and_idempotency(self):
        """Notification sync creates loan EMI alert with 2026-12-05 date and is idempotent."""
        sync_notifications_for_customer(self.customer_key)
        notifs = list_notifications(self.customer_key)
        self.assertTrue(len(notifs) > 0)

        # Find Loan EMI notification
        loan_notifs = [n for n in notifs if n["category"] == "loan_emi"]
        self.assertTrue(len(loan_notifs) >= 1)
        loan_alert = loan_notifs[0]
        self.assertIn("13,080.00", loan_alert["message"])
        self.assertIn("2026-12-05", loan_alert["message"])

        # Second sync must not duplicate
        count_before = len(notifs)
        sync_notifications_for_customer(self.customer_key)
        notifs_after = list_notifications(self.customer_key)
        self.assertEqual(len(notifs_after), count_before)

    def test_mark_single_and_all_read(self):
        """Mark single notification read and mark all notifications read."""
        sync_notifications_for_customer(self.customer_key)
        notifs = list_notifications(self.customer_key)
        first_id = notifs[0]["notification_id"]

        # Mark single
        ok = mark_notification_read(self.customer_key, first_id)
        self.assertTrue(ok)

        # Mark all
        mark_all_notifications_read(self.customer_key)
        unread = get_unread_count(self.customer_key)
        self.assertEqual(unread, 0)

    def test_customer_isolation(self):
        """Notifications for Arjun are not visible to Priya."""
        sync_notifications_for_customer("demo-arjun")
        sync_notifications_for_customer("demo-priya")
        priya_notifs = list_notifications("demo-priya")
        for n in priya_notifs:
            self.assertEqual(n["customer_key"], "demo-priya")


class TestPhase4ScamLinkAssessment(unittest.TestCase):
    def test_suspicious_link_wording_clarifies_no_domain_lookup(self):
        """Link warning qualifies that no external domain reputation lookup was performed."""
        result = analyze_scam_text("Urgent! Your account is suspended. Click http://bank-update.xyz to verify KYC immediately.")
        link_indicators = [i for i in result.indicators if i.category == "suspicious_link"]
        self.assertTrue(len(link_indicators) > 0)
        self.assertIn("No external domain reputation", link_indicators[0].explanation)

        # Recommendations should mention no external domain reputation check
        rec_text = " ".join(result.recommended_steps)
        self.assertIn("No external domain reputation", rec_text)
        self.assertIn("official", rec_text.lower())


class TestPhase4LoanEMIConsistency(unittest.TestCase):
    def test_loan_details_authoritative_date_and_amount(self):
        """Arjun's active Personal Loan consistently reports ₹13,080.00 due on 2026-12-05."""
        loans = get_loan_details("demo-arjun")
        active_loans = [l for l in loans if l["status"] == "ACTIVE"]
        self.assertEqual(len(active_loans), 1)
        personal_loan = active_loans[0]
        self.assertEqual(personal_loan["loan_type"], "PERSONAL")
        self.assertEqual(personal_loan["next_emi_date"], "2026-12-05")
        self.assertEqual(personal_loan["monthly_emi"], 13080.0)


if __name__ == "__main__":
    unittest.main()
