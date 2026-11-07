"""Offline checks for demo auth state and isolation cleanup."""

import unittest

from frontend.session_state import (
    bind_pending_scam_assessment,
    clear_pending_scam_assessment,
    initialize_session_state,
    mark_session_valid,
    reset_conversation_state,
    sign_out,
)


class FakeAPI:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SessionStateTests(unittest.TestCase):
    def test_initialize_and_authenticate_state(self):
        state = {}
        initialize_session_state(state, api_factory=FakeAPI)
        mark_session_valid(state, {"customer_key": "demo-priya", "display_name": "Priya"})
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["customer"]["customer_key"], "demo-priya")

    def test_signout_clears_customer_conversation_and_closes_cookie_session(self):
        state = {"api_client": FakeAPI(), "authenticated": True, "customer": {"customer_key": "demo-arjun"}}
        state.update({"conversation_id": "c1", "conversation_messages": [{"text": "private"}], "audio_cache": {"a": b"x"}})
        previous = state["api_client"]
        sign_out(state, api_factory=FakeAPI)
        self.assertTrue(previous.closed)
        self.assertFalse(state["authenticated"])
        self.assertIsNone(state["customer"])
        self.assertIsNone(state["conversation_id"])
        self.assertEqual(state["conversation_messages"], [])
        self.assertEqual(state["audio_cache"], {})

    def test_reset_drops_audio_and_history(self):
        state = {"conversation_messages": [1], "audio_cache": {"a": 1}, "audio_playback_visible": {"a": True}}
        reset_conversation_state(state)
        self.assertEqual(state["conversation_messages"], [])
        self.assertEqual(state["audio_cache"], {})
        self.assertEqual(state["audio_playback_visible"], {})

    def test_assessment_reference_is_conversation_bound_and_reset_clears_it(self):
        state = {}
        bind_pending_scam_assessment(state, "assessment-9", "conversation-2")
        self.assertEqual(state["pending_scam_assessment_id"], "assessment-9")
        self.assertEqual(state["pending_scam_conversation_id"], "conversation-2")
        reset_conversation_state(state)
        self.assertIsNone(state["pending_scam_assessment_id"])

    def test_changing_authenticated_demo_customer_clears_support_ticket_cache(self):
        state = {
            "customer": {"customer_key": "demo-arjun"},
            "support_ticket_items": [{"ticket_ref": "VS-private-to-Arjun"}],
            "support_selected_ticket_ref": "VS-private-to-Arjun",
            "support_ticket_cache_customer_key": "demo-arjun",
        }
        mark_session_valid(state, {"customer_key": "demo-priya"})
        self.assertIsNone(state["support_ticket_items"])
        self.assertIsNone(state["support_selected_ticket_ref"])
        self.assertEqual(state["support_ticket_cache_customer_key"], None)
        self.assertIsNone(state["pending_scam_conversation_id"])
        bind_pending_scam_assessment(state, "assessment-10", "conversation-3")
        clear_pending_scam_assessment(state)
        self.assertIsNone(state["pending_scam_assessment_id"])

    def test_signout_and_customer_switch_clear_transaction_risk_data(self):
        state = {
            "api_client": FakeAPI(), "customer": {"customer_key": "demo-arjun"},
            "transaction_risk_customer_key": "demo-arjun",
            "transaction_risk_transactions": [{"transaction_ref": "private-to-Arjun"}],
            "transaction_risk_latest_result": {"assessment_id": "private-assessment"},
            "transaction_risk_follow_up_assessment_id": "private-assessment",
        }
        mark_session_valid(state, {"customer_key": "demo-priya"})
        self.assertIsNone(state["transaction_risk_transactions"])
        self.assertIsNone(state["transaction_risk_latest_result"])
        self.assertIsNone(state["transaction_risk_follow_up_assessment_id"])
        state.update({
            "transaction_risk_customer_key": "demo-priya",
            "transaction_risk_transactions": [{"transaction_ref": "private-to-Priya"}],
        })
        sign_out(state, api_factory=FakeAPI)
        self.assertIsNone(state["transaction_risk_transactions"])
        self.assertIsNone(state["transaction_risk_customer_key"])


if __name__ == "__main__":
    unittest.main()
