"""Unit tests for the natural slot-filling conversational support workflow."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.api_models import AssistantTurnRequest
from services import database
from services.assistant_service import (
    AssistantOrchestrator,
    _extract_support_ticket_slots,
    _is_support_create_request,
    _support_summary_is_grounded,
    _support_priority_was_stated,
)
from services.conversation_service import create_conversation
from services.llm_adapters import MockLLMProvider
from services.llm_interfaces import LLMResponse
from services.principal import CustomerPrincipal


def make_principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20)
    )


def make_turn(text, conversation_id, language="en-IN"):
    return AssistantTurnRequest(
        conversation_id=conversation_id,
        client_turn_id=uuid4(),
        input_mode="text",
        output_mode="text",
        text=text,
        response_language=language,
    )


class ConversationalSupportAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "conv-support.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                ("demo-arjun", "arjun@example.test", "Synthetic Arjun"),
            )
            conn.commit()
        finally:
            conn.close()
        self.arjun = make_principal()
        self.conversation = create_conversation(self.arjun)["conversation_id"]

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def ticket_count():
        conn = database.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
        finally:
            conn.close()

    def test_english_conversational_slot_filling_three_steps(self):
        orchestrator = AssistantOrchestrator(
            llm_provider=MockLLMProvider([
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
            ])
        )

        # Step 1: Initial user query without amount/deduction
        req1 = make_turn(
            "I made a UPI payment, but the merchant hasn't received the money.",
            self.conversation,
        )
        res1 = orchestrator.process_turn(self.arjun, req1)
        self.assertEqual(res1.assistant_text, "Sure. Is the amount deducted from your account?")
        self.assertIsNone(res1.pending_action)
        self.assertEqual(self.ticket_count(), 0)

        # Step 2: User confirms amount deducted
        req2 = make_turn("Yes, ₹2,500 was deducted.", self.conversation)
        res2 = orchestrator.process_turn(self.arjun, req2)
        self.assertIn("Would you like to mark it as high priority?", res2.assistant_text)
        self.assertIsNone(res2.pending_action)
        self.assertEqual(self.ticket_count(), 0)

        # Step 3: User confirms high priority
        req3 = make_turn("Yes, high priority.", self.conversation)
        res3 = orchestrator.process_turn(self.arjun, req3)
        self.assertEqual(
            res3.assistant_text,
            "Here are the complaint details. Please review them before I submit.",
        )
        self.assertIsNotNone(res3.pending_action)
        self.assertEqual(res3.pending_action.action.category, "transaction")
        self.assertEqual(res3.pending_action.action.priority, "HIGH")
        self.assertEqual(
            res3.pending_action.action.subject,
            "UPI payment debited but not received by merchant",
        )
        self.assertEqual(self.ticket_count(), 0)  # Zero DB records until explicit button confirm

    def test_kannada_conversational_slot_filling_three_steps(self):
        orchestrator = AssistantOrchestrator(
            llm_provider=MockLLMProvider([
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
            ])
        )

        req1 = make_turn(
            "ನಾನು ಯುಪಿಐ ಪಾವತಿ ಮಾಡಿದ್ದೇನೆ, ಆದರೆ ವ್ಯಾಪಾರಿಗೆ ಹಣ ತಲುಪಿಲ್ಲ",
            self.conversation,
            language="kn-IN",
        )
        res1 = orchestrator.process_turn(self.arjun, req1)
        self.assertEqual(res1.assistant_text, "ಖಂಡಿತ. ನಿಮ್ಮ ಖಾತೆಯಿಂದ ಹಣ ಕಡಿತಗೊಂಡಿದೆಯೇ?")
        self.assertIsNone(res1.pending_action)

        req2 = make_turn("ಹೌದು, ₹2500 ಕಡಿತಗೊಂಡಿದೆ", self.conversation, language="kn-IN")
        res2 = orchestrator.process_turn(self.arjun, req2)
        self.assertIn("ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?", res2.assistant_text)
        self.assertIsNone(res2.pending_action)

        req3 = make_turn("ಹೌದು", self.conversation, language="kn-IN")
        res3 = orchestrator.process_turn(self.arjun, req3)
        self.assertEqual(
            res3.assistant_text,
            "ದೂರಿನ ವಿವರಗಳು ಇಲ್ಲಿವೆ. ನಾನು ಸಲ್ಲಿಸುವ ಮೊದಲು ದಯವಿಟ್ಟು ಪರಿಶೀಲಿಸಿ.",
        )
        self.assertIsNotNone(res3.pending_action)
        self.assertEqual(self.ticket_count(), 0)

    def test_hindi_conversational_slot_filling_three_steps(self):
        orchestrator = AssistantOrchestrator(
            llm_provider=MockLLMProvider([
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
                LLMResponse(text="Mock"),
            ])
        )

        req1 = make_turn(
            "मैंने यूपीआई भुगतान किया है, लेकिन व्यापारी को पैसे नहीं मिले",
            self.conversation,
            language="hi-IN",
        )
        res1 = orchestrator.process_turn(self.arjun, req1)
        self.assertEqual(res1.assistant_text, "ज़रूर। क्या आपके खाते से राशि कट गई है?")
        self.assertIsNone(res1.pending_action)

        req2 = make_turn("हाँ, ₹2500 कट गए हैं", self.conversation, language="hi-IN")
        res2 = orchestrator.process_turn(self.arjun, req2)
        self.assertIn("उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?", res2.assistant_text)
        self.assertIsNone(res2.pending_action)

        req3 = make_turn("हाँ", self.conversation, language="hi-IN")
        res3 = orchestrator.process_turn(self.arjun, req3)
        self.assertEqual(
            res3.assistant_text,
            "शिकायत का विवरण यहाँ है। सबमिट करने से पहले कृपया इसकी समीक्षा करें.",
        )
        self.assertIsNotNone(res3.pending_action)
        self.assertEqual(self.ticket_count(), 0)

    def test_support_summary_grounded_accepts_synonyms_and_banking_terms(self):
        self.assertTrue(
            _support_summary_is_grounded(
                "UPI payment debited but not received by merchant",
                "I made a UPI payment, but the merchant hasn't received the money. Yes, 2500 deducted. High priority",
            )
        )
        # Rejects ungrounded claims like 'hacked' or 'loan'
        self.assertFalse(
            _support_summary_is_grounded(
                "My loan account was hacked",
                "I made a UPI payment, but the merchant hasn't received the money.",
            )
        )


if __name__ == "__main__":
    unittest.main()

