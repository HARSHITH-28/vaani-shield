"""Offline assistant submission, recovery, and optional TTS regressions."""

import unittest
from unittest.mock import MagicMock

from frontend.api_client import APIError
from frontend.pages.assistant import _recover_interrupted_turn, _submit


def response(*, speech=False):
    return {
        "conversation_id": "conversation-1",
        "turn_id": "user-turn-1",
        "assistant_turn_id": "assistant-turn-1",
        "input_mode": "speech",
        "output_mode": "speech_and_text" if speech else "text",
        "transcript": "Show me my last 5 transactions." if speech else None,
        "detected_languages": [],
        "assistant_text": "Here are your last 5 transactions.",
        "response_language": "en-IN",
        "speech_output": {"audio_url": "/api/v1/voice/responses/audio-1", "media_type": "audio/wav"} if speech else None,
        "tool_results": [],
    }


class AssistantSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "conversation_id": "conversation-1",
            "conversation_messages": [],
            "response_mode": "text",
            "response_language": "en-IN",
            "language_hint": "auto",
            "pending_attachments": [],
            "audio_cache": {},
            "audio_playback_visible": {},
            "audio_widget_epoch": 0,
            "last_audio_hash": None,
        }
        self.st = MagicMock()
        self.api = MagicMock()

    def test_successful_assistant_response_is_saved_once(self):
        self.api.send_assistant_turn.return_value = response()

        _submit(self.api, self.st, self.state, text="Show me my last 5 transactions.")

        self.assertEqual(len(self.state["conversation_messages"]), 2)
        self.assertEqual(self.state["conversation_messages"][0]["text"], "Show me my last 5 transactions.")
        self.assertEqual(self.state["conversation_messages"][1]["text"], "Here are your last 5 transactions.")
        self.assertIsNone(self.state["assistant_failed_turn"])

    def test_lost_success_response_is_retried_with_same_idempotency_key(self):
        calls = []
        backend_side_effects = set()

        def send(payload):
            calls.append(dict(payload))
            backend_side_effects.add(payload["client_turn_id"])
            if len(calls) == 1:
                # The server completed the turn, but the client lost its response.
                raise APIError(0, "The VaaniShield API did not respond before the request timed out.")
            return response()

        self.api.send_assistant_turn.side_effect = send
        _submit(self.api, self.st, self.state, text="Show me my last 5 transactions.")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(backend_side_effects), 1)
        self.assertEqual(len(self.state["conversation_messages"]), 2)
        self.assertIsNone(self.state["assistant_failed_turn"])

    def test_ambiguous_transport_failure_is_not_reported_as_turn_failure(self):
        self.api.send_assistant_turn.side_effect = APIError(0, "The VaaniShield API did not respond before the request timed out.")

        with self.assertRaises(APIError):
            _submit(self.api, self.st, self.state, text="Show me my last 5 transactions.")

        self.assertEqual(self.state["assistant_failed_turn"]["status"], "processing")
        self.assertIsNone(self.state["assistant_failed_turn"]["error"])
        self.assertTrue(any("Show me my last 5 transactions." in item["text"] for item in self.state["conversation_messages"]))

    def test_streamlit_rerun_recovers_pending_turn_in_same_conversation(self):
        self.state.update({
            "assistant_retry_payload": {
                "conversation_id": "conversation-1",
                "client_turn_id": "same-client-turn",
                "input_mode": "speech",
                "output_mode": "speech_and_text",
                "audio_upload_id": "saved-upload",
                "language_hint": "auto",
                "response_language": "en-IN",
            },
            "assistant_failed_turn": {
                "client_turn_id": "same-client-turn",
                "text": "Voice message (transcript will be restored from the saved turn)",
                "is_voice": True,
                "status": "processing",
                "recovery_attempted": False,
            },
        })
        self.api.send_assistant_turn.return_value = response(speech=True)

        recovered = _recover_interrupted_turn(self.api, self.st, self.state)

        self.assertTrue(recovered)
        sent_payload = self.api.send_assistant_turn.call_args.args[0]
        self.assertEqual(sent_payload["conversation_id"], "conversation-1")
        self.assertEqual(sent_payload["client_turn_id"], "same-client-turn")
        self.assertEqual(self.state["conversation_messages"][0]["text"], "Show me my last 5 transactions.")
        self.assertIsNone(self.state["assistant_failed_turn"])

    def test_tts_retrieval_failure_keeps_successful_text_response(self):
        self.api.send_assistant_turn.return_value = response(speech=True)
        self.api.fetch_audio.side_effect = RuntimeError("audio transport dropped")
        self.state["response_mode"] = "speech_and_text"

        _submit(self.api, self.st, self.state, text="Show me my last 5 transactions.")

        self.assertEqual(self.state["conversation_messages"][-1]["text"], "Here are your last 5 transactions.")
        self.assertIsNone(self.state["assistant_failed_turn"])
        self.st.warning.assert_any_call("Your text response is ready, but its audio could not be loaded.")

    def test_retry_does_not_duplicate_existing_assistant_message(self):
        saved = response()
        self.state.update({
            "assistant_retry_payload": {
                "conversation_id": "conversation-1",
                "client_turn_id": "same-client-turn",
                "input_mode": "text",
                "output_mode": "text",
                "text": "Show me my last 5 transactions.",
                "language_hint": "auto",
                "response_language": "en-IN",
            },
            "assistant_failed_turn": {
                "client_turn_id": "same-client-turn", "text": "Show me my last 5 transactions.",
                "is_voice": False, "status": "failed", "error": "temporary failure",
            },
            "conversation_messages": [
                {"id": "user-user-turn-1", "role": "user", "text": "Show me my last 5 transactions."},
                {"id": "assistant-user-turn-1", "role": "assistant", "text": saved["assistant_text"]},
            ],
        })
        self.api.send_assistant_turn.return_value = saved

        _submit(self.api, self.st, self.state, retry=True)

        self.assertEqual(len(self.state["conversation_messages"]), 2)
        self.assertEqual(sum(item["role"] == "assistant" for item in self.state["conversation_messages"]), 1)

    def test_genuine_backend_failure_preserves_message_and_useful_error(self):
        self.api.send_assistant_turn.side_effect = APIError(503, "The assistant provider is temporarily unavailable.")

        with self.assertRaises(APIError):
            _submit(self.api, self.st, self.state, text="Show me my last 5 transactions.")

        self.assertEqual(self.state["assistant_failed_turn"]["status"], "failed")
        self.assertEqual(self.state["assistant_failed_turn"]["error"], "The assistant provider is temporarily unavailable.")
        self.assertTrue(any("Show me my last 5 transactions." in item["text"] for item in self.state["conversation_messages"]))


if __name__ == "__main__":
    unittest.main()
