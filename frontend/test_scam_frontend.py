"""Offline scam-assessment session and assistant handoff checks."""

import unittest

from frontend.api_client import build_text_turn_request
from frontend.pages.assistant import (
    assessment_reference_for_text,
    transaction_risk_reference_for_conversation,
)
from frontend.pages.scam_detection import (
    assess_message,
    assess_voice_recording,
    begin_assessment_discussion,
    begin_transaction_risk_discussion,
)
from frontend.session_state import bind_pending_scam_assessment


class FakeAPI:
    def create_conversation(self):
        return {"conversation_id": "conversation-new"}


class ScamFrontendTests(unittest.TestCase):
    def test_consecutive_text_assessments_keep_exact_fresh_result_and_clear_report_cache(self):
        messages = (
            "Your bank account will be blocked today. Share your OTP immediately to complete KYC.",
            "Your grocery order has been delivered. Thank you for shopping.",
            "Your KYC will expire today. Visit http://secure-bank-verify.example to prevent account suspension.",
            "Your friend has shared the photos from yesterday.",
        )

        class SequenceAPI(FakeAPI):
            def __init__(self):
                self.calls = []

            def assess_scam_message(self, text, language_hint):
                self.calls.append((text, language_hint))
                index = len(self.calls)
                return {
                    "assessment_id": f"assessment-{index}",
                    "created_at": f"2026-01-01T00:00:0{index}+00:00",
                    "risk_level": ("HIGH", "LOW", "HIGH", "LOW")[index - 1],
                    "indicators": [{"category": f"signal-{index}"}] if index in (1, 3) else [],
                }

        api = SequenceAPI()
        state = {
            "language_hint": "en-IN",
            "analysis_reports_cache_key": ("customer-1", None),
            "analysis_reports_items": [{"assessment_id": "old"}],
            "analysis_reports_cursor": "old-cursor",
            "analysis_reports_has_more": True,
            "analysis_reports_detail": {"assessment_id": "old"},
            "analysis_reports_detail_key": ("old", "customer-1"),
        }
        results = [assess_message(api, state, message) for message in messages]

        self.assertEqual(api.calls, [(message, "en-IN") for message in messages])
        self.assertEqual([item["risk_level"] for item in results], ["HIGH", "LOW", "HIGH", "LOW"])
        self.assertEqual([item["assessment_id"] for item in results], [f"assessment-{i}" for i in range(1, 5)])
        self.assertEqual(results[1]["indicators"], [])
        self.assertEqual(results[3]["indicators"], [])
        self.assertIs(state["latest_scam_assessment"], results[-1])
        self.assertIsNone(state["analysis_reports_cache_key"])
        self.assertIsNone(state["analysis_reports_items"])
        self.assertIsNone(state["analysis_reports_detail"])

    def test_discussion_starts_fresh_conversation_and_binds_existing_assessment(self):
        state = {
            "latest_scam_assessment": {"assessment_id": "voice-assessment"},
            "conversation_id": "old-conversation",
            "conversation_messages": [{"text": "old turn"}],
            "audio_cache": {},
            "audio_playback_visible": {},
        }
        conversation = begin_assessment_discussion(FakeAPI(), state)
        self.assertEqual(conversation["conversation_id"], "conversation-new")
        self.assertTrue(state["assistant_open"])
        self.assertEqual(state["pending_scam_assessment_id"], "voice-assessment")
        self.assertEqual(state["pending_scam_conversation_id"], "conversation-new")
        self.assertEqual(state["conversation_messages"], [])

    def test_only_relevant_text_in_the_bound_conversation_forwards_reference(self):
        state = {}
        bind_pending_scam_assessment(state, "assessment-3", "conversation-1")
        reference = assessment_reference_for_text(
            state, "conversation-1", "What does this assessment warning mean?",
        )
        self.assertEqual(reference, "assessment-3")
        payload = build_text_turn_request(
            "conversation-1", "What does this assessment warning mean?", "text",
            scam_assessment_id=reference,
        )
        self.assertEqual(payload["scam_assessment_id"], "assessment-3")

    def test_unrelated_text_and_conversation_switch_clear_stale_reference(self):
        state = {}
        bind_pending_scam_assessment(state, "assessment-3", "conversation-1")
        self.assertIsNone(assessment_reference_for_text(
            state, "conversation-1", "What is my current balance?",
        ))
        self.assertIsNone(state["pending_scam_assessment_id"])

    def test_voice_recording_upload_is_consumed_only_by_scam_call_endpoint(self):
        class VoiceAPI:
            def __init__(self):
                self.events = []

            def upload_audio(self, filename, content, media_type):
                self.events.append(("upload", filename, content, media_type))
                return {"audio_upload_id": "one-time-upload"}

            def assess_scam_call(self, upload_id, language_hint):
                self.events.append(("assess_call", upload_id, language_hint))
                return {"assessment_id": "voice-result", "source_type": "voice_call"}

        state = {"pending_scam_assessment_id": "stale", "pending_scam_conversation_id": "old"}
        api = VoiceAPI()
        result = assess_voice_recording(api, state, "call.wav", b"audio", "audio/wav")
        self.assertEqual(result["assessment_id"], "voice-result")
        self.assertEqual([event[0] for event in api.events], ["upload", "assess_call"])
        self.assertEqual(api.events[1][1], "one-time-upload")
        self.assertEqual(api.events[1][2], "auto")
        self.assertIsNone(state["pending_scam_assessment_id"])

    def test_transaction_risk_handoff_is_public_and_bound_to_its_new_conversation(self):
        state = {
            "conversation_id": "old-conversation", "conversation_messages": [],
            "audio_cache": {}, "audio_playback_visible": {},
        }
        conversation = begin_transaction_risk_discussion(
            FakeAPI(), state, "123e4567-e89b-42d3-a456-426614174000",
        )
        self.assertEqual(conversation["conversation_id"], "conversation-new")
        self.assertEqual(
            transaction_risk_reference_for_conversation(state, "conversation-new"),
            "123e4567-e89b-42d3-a456-426614174000",
        )
        self.assertIsNone(transaction_risk_reference_for_conversation(state, "other-conversation"))
        self.assertIsNone(state["transaction_risk_follow_up_assessment_id"])

        bind_pending_scam_assessment(state, "assessment-4", "conversation-1")
        self.assertIsNone(assessment_reference_for_text(
            state, "conversation-2", "Explain this scam risk.",
        ))
        self.assertIsNone(state["pending_scam_assessment_id"])


if __name__ == "__main__":
    unittest.main()
