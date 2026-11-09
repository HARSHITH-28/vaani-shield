"""Unit tests for robust Streamlit-safe response language synchronization.

Covers:
1. Initial application load and defaults initialization.
2. Changing language through the sidebar preferences selectbox.
3. Changing language through the Copilot dock selectbox.
4. Changing language through the Voice Composer selectbox.
5. Sequential switching across English -> Kannada -> Hindi -> Automatic.
6. Automatic language detection vs explicit user selection precedence.
7. Conversation history preservation after changing language.
8. Repeated reruns and protection against StreamlitWidgetAlreadyInstantiatedError.
"""

import unittest
from unittest.mock import patch
from uuid import uuid4

import streamlit as st
from streamlit.errors import StreamlitWidgetAlreadyInstantiatedError

from frontend.api_client import build_text_turn_request
from frontend.session_state import (
    CODE_TO_FULL_LABEL,
    CODE_TO_SHORT_LABEL,
    FULL_LABEL_TO_CODE,
    RESPONSE_LANGUAGE_OPTIONS_FULL,
    RESPONSE_LANGUAGE_OPTIONS_SHORT,
    SHORT_LABEL_TO_CODE,
    initialize_session_state,
    on_composer_language_change,
    on_dock_response_mode_change,
    RESPONSE_MODE_CODES,
    on_dock_language_change,
    on_sidebar_language_change,
    sync_response_language,
)
from services.assistant_service import _preferred_response_language


class TestLanguageSync(unittest.TestCase):
    """Test suite for Streamlit-safe language state synchronization."""

    def test_initial_application_load(self):
        """Verify initial session state has correct defaults without crashing."""
        state = {}
        initialize_session_state(state)

        # Canonical response language is None (Automatic)
        self.assertIsNone(state.get("response_language"))
        # Widget selector defaults
        self.assertEqual(state.get("sidebar_assistant_language"), "Automatic")
        self.assertEqual(state.get("dock_copilot_language_selector"), "Auto")
        self.assertEqual(state.get("response_language_display"), "Automatic")

        # Verify options are valid
        self.assertIn(state["sidebar_assistant_language"], RESPONSE_LANGUAGE_OPTIONS_FULL)
        self.assertIn(state["dock_copilot_language_selector"], RESPONSE_LANGUAGE_OPTIONS_SHORT)
        self.assertIn(state["response_language_display"], RESPONSE_LANGUAGE_OPTIONS_FULL)

    def test_changing_language_through_sidebar(self):
        """Verify changing sidebar selector updates canonical state and other widget keys."""
        with patch.object(st, "session_state", {}) as mock_state:
            initialize_session_state(mock_state)

            # User chooses 'Kannada' in sidebar selectbox
            mock_state["sidebar_assistant_language"] = "Kannada"
            on_sidebar_language_change()

            self.assertEqual(mock_state["response_language"], "kn-IN")
            self.assertEqual(mock_state["dock_copilot_language_selector"], "Kannada")
            self.assertEqual(mock_state["response_language_display"], "Kannada")
            self.assertEqual(mock_state["sidebar_assistant_language"], "Kannada")

    def test_changing_language_through_copilot(self):
        """Verify changing Copilot selector updates canonical state and other widget keys."""
        with patch.object(st, "session_state", {}) as mock_state:
            initialize_session_state(mock_state)

            # User chooses 'Hindi' in dock copilot selectbox
            mock_state["dock_copilot_language_selector"] = "Hindi"
            on_dock_language_change()

            self.assertEqual(mock_state["response_language"], "hi-IN")
            self.assertEqual(mock_state["sidebar_assistant_language"], "Hindi")
            self.assertEqual(mock_state["response_language_display"], "Hindi")
            self.assertEqual(mock_state["dock_copilot_language_selector"], "Hindi")

    def test_dock_response_mode_choices_update_canonical_submission_mode(self):
        self.assertEqual(set(RESPONSE_MODE_CODES), {"Speech", "Text", "Speech + Text"})
        expected = {"Speech": "speech", "Text": "text", "Speech + Text": "speech_and_text"}
        for label, code in expected.items():
            with patch.object(st, "session_state", {
                "dock_response_mode_selector": label,
                "assistant_response_mode": "Text",
                "response_mode": "text",
                "response_mode_explicit": False,
            }) as mock_state:
                on_dock_response_mode_change()
                self.assertEqual(mock_state["response_mode"], code)
                self.assertEqual(mock_state["assistant_response_mode"], label)
                self.assertTrue(mock_state["response_mode_explicit"])

    def test_changing_language_through_composer(self):
        """Verify changing voice composer selector updates canonical state and other widget keys."""
        with patch.object(st, "session_state", {}) as mock_state:
            initialize_session_state(mock_state)

            # User chooses 'English' in composer selectbox
            mock_state["response_language_display"] = "English"
            on_composer_language_change()

            self.assertEqual(mock_state["response_language"], "en-IN")
            self.assertEqual(mock_state["sidebar_assistant_language"], "English")
            self.assertEqual(mock_state["dock_copilot_language_selector"], "English")
            self.assertEqual(mock_state["response_language_display"], "English")

    def test_switching_english_kannada_hindi_progression(self):
        """Verify continuous switching: None -> en-IN -> kn-IN -> hi-IN -> None."""
        with patch.object(st, "session_state", {}) as mock_state:
            initialize_session_state(mock_state)
            self.assertIsNone(mock_state["response_language"])

            # 1. Switch to English
            mock_state["sidebar_assistant_language"] = "English"
            on_sidebar_language_change()
            self.assertEqual(mock_state["response_language"], "en-IN")
            self.assertEqual(mock_state["dock_copilot_language_selector"], "English")
            self.assertEqual(mock_state["response_language_display"], "English")

            # 2. Switch to Kannada from Copilot
            mock_state["dock_copilot_language_selector"] = "Kannada"
            on_dock_language_change()
            self.assertEqual(mock_state["response_language"], "kn-IN")
            self.assertEqual(mock_state["sidebar_assistant_language"], "Kannada")
            self.assertEqual(mock_state["response_language_display"], "Kannada")

            # 3. Switch to Hindi from Composer
            mock_state["response_language_display"] = "Hindi"
            on_composer_language_change()
            self.assertEqual(mock_state["response_language"], "hi-IN")
            self.assertEqual(mock_state["sidebar_assistant_language"], "Hindi")
            self.assertEqual(mock_state["dock_copilot_language_selector"], "Hindi")

            # 4. Switch back to Automatic from Sidebar
            mock_state["sidebar_assistant_language"] = "Automatic"
            on_sidebar_language_change()
            self.assertIsNone(mock_state["response_language"])
            self.assertEqual(mock_state["dock_copilot_language_selector"], "Auto")
            self.assertEqual(mock_state["response_language_display"], "Automatic")

    def test_automatic_language_detection_and_explicit_precedence(self):
        """Verify automatic detection behavior and that explicit selection is preserved."""
        # When response_language is None, automatic detection inspects text
        self.assertEqual(_preferred_response_language("ನನ್ನ ಬ್ಯಾಲೆನ್ಸ್ ಎಷ್ಟು?"), "kn-IN")
        self.assertEqual(_preferred_response_language("मेरा बैलेंस कितना है?"), "hi-IN")
        self.assertEqual(_preferred_response_language("What is my balance?"), "en-IN")

        # In turn request payload:
        # None leaves response_language absent for backend auto-detection
        cid = uuid4()
        req_auto = build_text_turn_request(cid, "Hello", "text", response_language=None)
        self.assertNotIn("response_language", req_auto)

        # Explicit language choice overrides and is sent in payload
        req_kn = build_text_turn_request(cid, "Hello", "text", response_language="kn-IN")
        self.assertEqual(req_kn.get("response_language"), "kn-IN")

        req_hi = build_text_turn_request(cid, "Hello", "text", response_language="hi-IN")
        self.assertEqual(req_hi.get("response_language"), "hi-IN")

        req_en = build_text_turn_request(cid, "Hello", "text", response_language="en-IN")
        self.assertEqual(req_en.get("response_language"), "en-IN")

    def test_conversation_history_preservation_after_language_change(self):
        """Verify changing language preserves conversation ID, messages, and audio cache."""
        with patch.object(st, "session_state", {}) as mock_state:
            initialize_session_state(mock_state)
            mock_state["conversation_id"] = "conv-abc-123"
            mock_state["conversation_messages"] = [
                {"role": "user", "text": "What is my balance?"},
                {"role": "assistant", "text": "Your balance is ₹2,78,354.00."},
            ]
            mock_state["audio_cache"] = {"audio-1": b"sample_wav"}

            # Change language
            mock_state["sidebar_assistant_language"] = "Kannada"
            on_sidebar_language_change()

            # Ensure conversation history is completely intact
            self.assertEqual(mock_state["conversation_id"], "conv-abc-123")
            self.assertEqual(len(mock_state["conversation_messages"]), 2)
            self.assertEqual(mock_state["conversation_messages"][0]["text"], "What is my balance?")
            self.assertEqual(mock_state["conversation_messages"][1]["text"], "Your balance is ₹2,78,354.00.")
            self.assertIn("audio-1", mock_state["audio_cache"])

    def test_repeated_reruns_without_widget_instantiated_error(self):
        """Verify sync_response_language is safe even if a widget key raises StreamlitWidgetAlreadyInstantiatedError."""
        class MockInstantiatedSessionState(dict):
            """Simulates Streamlit's runtime check that forbids modifying instantiated widgets."""
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.instantiated_keys = set()

            def __setitem__(self, key, value):
                if key in self.instantiated_keys:
                    raise StreamlitWidgetAlreadyInstantiatedError(
                        f"st.session_state['{key}'] cannot be modified after the widget with that key is instantiated."
                    )
                super().__setitem__(key, value)

        simulated_state = MockInstantiatedSessionState()
        initialize_session_state(simulated_state)

        # Mark sidebar_assistant_language as already instantiated
        simulated_state.instantiated_keys.add("sidebar_assistant_language")

        # sync_response_language must not raise StreamlitWidgetAlreadyInstantiatedError!
        try:
            sync_response_language(simulated_state, "kn-IN")
        except StreamlitWidgetAlreadyInstantiatedError:
            self.fail("sync_response_language raised StreamlitWidgetAlreadyInstantiatedError!")

        # Canonical response_language must still be updated
        self.assertEqual(simulated_state["response_language"], "kn-IN")
        # Other non-instantiated keys updated
        self.assertEqual(simulated_state["dock_copilot_language_selector"], "Kannada")
        self.assertEqual(simulated_state["response_language_display"], "Kannada")


if __name__ == "__main__":
    unittest.main()

