"""Offline conversation response and history mapping checks."""

import unittest

from frontend.components.conversation import (
    message_from_history_turn,
    messages_from_turn_response,
    render_investigation_detail,
    render_messages,
)
from frontend.components.conversation_history import create_new_conversation, select_conversation
from frontend.pages.assistant import _api_error
from frontend.api_client import APIError
from contextlib import nullcontext


class FakeAPI:
    def create_conversation(self):
        return {"conversation_id": "server-created"}


class FakeStreamlit:
    def __init__(self):
        self.rendered = []

    def chat_message(self, role):
        return nullcontext()

    def container(self, **kwargs):
        return nullcontext()

    def markdown(self, value):
        self.rendered.append(("markdown", value))

    def caption(self, value):
        self.rendered.append(("caption", value))

    def write(self, value):
        self.rendered.append(("write", value))

    def text(self, value):
        self.rendered.append(("text", value))

    def success(self, value):
        self.rendered.append(("success", value))

    def warning(self, value):
        self.rendered.append(("warning", value))

    def info(self, value):
        self.rendered.append(("info", value))

    def audio(self, value, **kwargs):
        self.rendered.append(("audio", value))

    def button(self, *args, **kwargs):
        return False

    def error(self, value):
        self.rendered.append(("error", value))


class ConversationTests(unittest.TestCase):
    def test_history_transcript_and_response_are_mapped(self):
        user = message_from_history_turn({
            "turn_id": "t1", "role": "user", "input_type": "audio",
            "user_text_or_transcript": "Hello", "input_language": "en-IN",
        })
        self.assertEqual(user["transcript"], "Hello")
        self.assertEqual(user["detected_languages"], ["en-IN"])
        mapped = messages_from_turn_response({
            "turn_id": "t2", "conversation_id": "c1", "input_mode": "speech",
            "transcript": "Balance please", "detected_languages": [{"language": "kn-IN"}],
            "assistant_text": "Your balance is available.", "response_language": "en-IN",
            "speech_output": {"audio_url": "/api/v1/voice/responses/a1", "media_type": "audio/wav"},
        })
        self.assertEqual(mapped[0]["text"], "Balance please")
        self.assertEqual(mapped[1]["text"], "Your balance is available.")
        self.assertEqual(mapped[1]["speech_audio_id"], "/api/v1/voice/responses/a1")

    def test_structured_assessment_is_attached_to_the_correct_assistant_turn(self):
        assessment = {"assessment_id": "assessment-1", "risk_level": "HIGH"}
        mapped = messages_from_turn_response({
            "turn_id": "turn-7", "conversation_id": "c1", "input_mode": "text",
            "assistant_text": "Deterministic screening result.",
            "scam_assessment": assessment,
        }, input_text="Check this message")
        self.assertIsNone(mapped[0].get("scam_assessment"))
        self.assertEqual(mapped[1]["id"], "assistant-turn-7")
        self.assertEqual(mapped[1]["scam_assessment"], assessment)

    def test_investigation_proposal_stays_on_originating_assistant_turn(self):
        pending = {
            "confirmation_id": "c-id", "conversation_id": "c1",
            "original_turn_id": "turn-8", "expires_at": "2099-01-01T00:00:00+00:00",
            "action": {"action_type": "create_investigation", "title": "Demo report"},
        }
        mapped = messages_from_turn_response({
            "turn_id": "turn-8", "conversation_id": "c1", "input_mode": "text",
            "assistant_text": "Review and confirm this proposal.", "pending_action": pending,
        }, input_text="Track this incident")
        self.assertNotIn("pending_action", mapped[0])
        self.assertEqual(mapped[1]["pending_action"], pending)
        self.assertEqual(mapped[1]["conversation_id"], "c1")

    def test_investigation_detail_renders_evidence_and_timeline(self):
        ui = FakeStreamlit()
        render_investigation_detail(ui, {
            "investigation_ref": "INV-" + "A" * 32, "title": "Caller report",
            "category": "CALL_SCAM", "status": "UNDER_REVIEW",
            "summary": "Customer-reported concern.", "updated_at": "now",
            "evidence": [{"evidence_type": "CALL_ASSESSMENT", "assessment_id": "public-id", "risk_level": "HIGH", "evidence_strength": "STRONG", "created_at": "today", "summary": "Risk summary"}],
            "events": [{"event_type": "CREATED", "occurred_at": "today", "summary": "Record created."}],
        })
        rendered = repr(ui.rendered)
        self.assertIn("CALL_ASSESSMENT", rendered)
        self.assertIn("Event timeline", rendered)
        self.assertIn("public-id", rendered)

    def test_assessment_card_renders_all_risk_levels_sources_and_evidence(self):
        cases = (
            ("LOW", "typed_message", "LOW risk", "Text message"),
            ("MEDIUM", "voice_call", "MEDIUM risk", "Voice / call transcript"),
            ("HIGH", "typed_message", "HIGH risk", "Text message"),
            ("CRITICAL", "voice_call", "CRITICAL risk", "Voice / call transcript"),
            ("UNKNOWN", "typed_message", "UNKNOWN risk", "Text message"),
        )
        for risk, source, label, source_label in cases:
            with self.subTest(risk=risk, source=source):
                ui = FakeStreamlit()
                assessment = {
                    "risk_level": risk, "source_type": source,
                    "created_at": "2026-11-05T10:30:00+00:00",
                    "explanation": "A deterministic assessment explanation.",
                    "evidence_strength": "STRONG",
                    "indicators": [{
                        "category": "credential_request", "severity": risk,
                        "explanation": "A credential was requested.",
                        "evidence": ["Please provide your OTP."],
                    }],
                    "warning": "Never disclose a secret code.",
                    "recommended_steps": ["Contact your bank through its official channel."],
                }
                render_messages(ui, [{
                    "id": f"assistant-{risk}", "role": "assistant",
                    "text": "Assessment summary.", "scam_assessment": assessment,
                }], {"audio_cache": {}, "audio_playback_visible": {}})
                output = repr(ui.rendered)
                self.assertIn("Security assessment", output)
                self.assertIn(label, output)
                self.assertIn(source_label, output)
                self.assertIn("not confirmation that fraud occurred", output)
                self.assertIn("STRONG", output)
                self.assertIn("credential_request", output)
                self.assertIn("Please provide your OTP.", output)
                self.assertIn("official channel", output)

    def test_normal_assistant_response_does_not_render_assessment_card(self):
        ui = FakeStreamlit()
        render_messages(ui, [{
            "id": "assistant-plain", "role": "assistant", "text": "Hello.",
        }], {"audio_cache": {}, "audio_playback_visible": {}})
        self.assertIn(("markdown", "Hello."), ui.rendered)
        self.assertNotIn("Security assessment", repr(ui.rendered))

    def test_server_created_conversation_and_selection_reset(self):
        state = {"conversation_id": None, "conversation_messages": [], "audio_cache": {}, "audio_playback_visible": {}}
        conversation = create_new_conversation(FakeAPI(), state)
        self.assertEqual(conversation["conversation_id"], "server-created")
        state["conversation_messages"].append({"text": "old"})
        select_conversation(state, "other-server-id")
        self.assertEqual(state["conversation_id"], "other-server-id")
        self.assertEqual(state["conversation_messages"], [])

    def test_render_text_and_audio_and_suppress_backend_details(self):
        ui = FakeStreamlit()
        message = {
            "id": "assistant-1", "role": "assistant", "text": "Hello",
            "speech_audio_id": "/api/v1/voice/responses/a1", "speech_media_type": "audio/wav",
        }
        render_messages(ui, [message], {"audio_cache": {"assistant-1": b"wav"}, "audio_playback_visible": {}})
        self.assertIn(("markdown", "Hello"), ui.rendered)
        self.assertIn(("audio", b"wav"), ui.rendered)
        for status in (404, 413, 415, 503):
            _api_error(ui, {}, APIError(status, "internal provider secret detail"))
        self.assertNotIn("internal provider secret detail", repr(ui.rendered))

    def test_transport_timeout_and_connectivity_errors_are_distinguished_safely(self):
        ui = FakeStreamlit()
        details = (
            "The VaaniShield API connection timed out.",
            "The VaaniShield API did not respond before the request timed out.",
            "The VaaniShield API could not be reached. Check that the backend is running.",
        )
        for detail in details:
            _api_error(ui, {}, APIError(0, detail, {"transport": "safe-category"}))
        output = repr(ui.rendered)
        self.assertIn("connection timed out", output)
        self.assertIn("did not respond", output)
        self.assertIn("could not be reached", output)
        self.assertNotIn("safe-category", output)


if __name__ == "__main__":
    unittest.main()
