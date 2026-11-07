"""Frontend tests for Phase 5 Copilot UI Redesign, Composer and Attachment Interactions."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frontend.components.floating_assistant import render_floating_assistant
from frontend.pages.assistant import _handle_file_upload


class DummyUploadedFile:
    def __init__(self, name: str, content: bytes, file_type: str = "application/pdf"):
        self.name = name
        self._content = content
        self.type = file_type

    def getvalue(self) -> bytes:
        return self._content


class TestCopilotUXAndComposer(unittest.TestCase):
    def setUp(self):
        self.state = {
            "assistant_open": True,
            "conversation_id": "test-conv-001",
            "conversation_messages": [],
            "audio_widget_epoch": 0,
            "pending_attachments": [],
            "uploaded_attachment_hashes": set(),
            "doc_upload_epoch": 0,
            "img_upload_epoch": 0,
            "input_epoch": 0,
            "response_mode": "text",
            "response_language": "en-IN",
            "language_hint": "auto",
        }
        self.api = MagicMock()

    def test_handle_file_upload_rejects_oversized(self):
        st = MagicMock()
        oversized_content = b"X" * (10 * 1024 * 1024 + 1)
        f = DummyUploadedFile("statement_large.pdf", oversized_content)
        _handle_file_upload(st, self.api, self.state, f)

        st.error.assert_called_once()
        self.assertIn("10MB", st.error.call_args[0][0])
        self.assertEqual(len(self.state["pending_attachments"]), 0)
        self.api.upload_attachment.assert_not_called()

    def test_handle_file_upload_rejects_empty(self):
        st = MagicMock()
        f = DummyUploadedFile("empty.txt", b"")
        _handle_file_upload(st, self.api, self.state, f)

        st.warning.assert_called_once()
        self.assertIn("empty", st.warning.call_args[0][0])
        self.assertEqual(len(self.state["pending_attachments"]), 0)

    def test_handle_file_upload_success_and_epoch_increment(self):
        st = MagicMock()
        self.api.upload_attachment.return_value = {
            "attachment_id": "att-new-1",
            "file_name": "passbook.pdf",
            "file_type": "pdf",
            "file_size_bytes": 2048,
            "extracted_text": "Sample text",
            "summary_metadata": {},
            "thumbnail_base64": None,
            "is_safe": True,
            "error": None,
        }
        f = DummyUploadedFile("passbook.pdf", b"%PDF-1.4 sample content")
        _handle_file_upload(st, self.api, self.state, f, is_image=False)

        self.assertEqual(len(self.state["pending_attachments"]), 1)
        self.assertEqual(self.state["pending_attachments"][0]["file_name"], "passbook.pdf")
        self.assertEqual(self.state["doc_upload_epoch"], 1)
        st.rerun.assert_called_once()

    def test_handle_photo_upload_epoch_increment(self):
        st = MagicMock()
        self.api.upload_attachment.return_value = {
            "attachment_id": "att-img-1",
            "file_name": "receipt.png",
            "file_type": "png",
            "file_size_bytes": 5000,
            "extracted_text": "Store: DMart, Total: 1500",
            "summary_metadata": {},
            "thumbnail_base64": "data:image/jpeg;base64,abc",
            "is_safe": True,
            "error": None,
        }
        f = DummyUploadedFile("receipt.png", b"\x89PNG\r\n\x1a\nfake png", file_type="image/png")
        _handle_file_upload(st, self.api, self.state, f, is_image=True)

        self.assertEqual(len(self.state["pending_attachments"]), 1)
        self.assertEqual(self.state["img_upload_epoch"], 1)
        st.rerun.assert_called_once()

    def test_floating_assistant_header_has_no_plus_button(self):
        """Header must only have brand, language selector and close button (no duplicate '+' button)."""
        st = MagicMock()
        st.container.return_value.__enter__.return_value = st
        st.columns.side_effect = lambda spec, **kw: tuple(MagicMock() for _ in range(len(spec) if isinstance(spec, (list, tuple)) else spec))
        st.file_uploader.return_value = None
        st.audio_input.return_value = None
        self.api.list_conversations.return_value = {"items": []}

        render_floating_assistant(st, self.api, self.state, in_dock=True)

        # Header columns should be 3 (brand, lang, close) with spec [3.0, 1.4, 0.4]
        calls = [c for c in st.columns.call_args_list if c[0] and c[0][0] == [3.0, 1.4, 0.4]]
        self.assertTrue(len(calls) >= 1)
        # Verify '+' button with key 'floating-assistant-new' is NEVER created in header
        button_keys = [
            c[1].get("key") for c in st.button.call_args_list if c[1].get("key")
        ]
        self.assertNotIn("floating-assistant-new", button_keys)

    def test_floating_assistant_has_no_blocking_backdrop(self):
        """When Copilot opens, NO blocking backdrop overlay is rendered, keeping dashboard fully interactive."""
        st = MagicMock()
        st.container.return_value.__enter__.return_value = st
        st.columns.side_effect = lambda spec, **kw: tuple(MagicMock() for _ in range(len(spec) if isinstance(spec, (list, tuple)) else spec))
        st.file_uploader.return_value = None
        st.audio_input.return_value = None
        self.api.list_conversations.return_value = {"items": []}

        self.state["assistant_open"] = True
        render_floating_assistant(st, self.api, self.state, in_dock=False)

        markdown_calls = [c[0][0] for c in st.markdown.call_args_list if c[0]]
        backdrop_found = any("copilot-backdrop-overlay" in m for m in markdown_calls)
        self.assertFalse(backdrop_found, "Backdrop overlay MUST NOT be rendered; assistant must be non-blocking")

    def test_copilot_welcome_screen_and_prompts(self):
        """When conversation has no messages, welcome screen and 4 prompts are displayed."""
        from frontend.pages.assistant import render as render_assistant

        st = MagicMock()
        st.container.return_value.__enter__.return_value = st
        st.popover.return_value.__enter__.return_value = st
        st.expander.return_value.__enter__.return_value = st

        def make_col():
            c = MagicMock()
            c.button.side_effect = lambda *args, **kwargs: st.button(*args, **kwargs)
            c.__enter__.return_value = c
            return c

        st.columns.side_effect = lambda spec, **kw: tuple(make_col() for _ in range(len(spec) if isinstance(spec, (list, tuple)) else spec))
        st.file_uploader.return_value = None
        st.audio_input.return_value = None
        st.button.return_value = False
        st.text_input.return_value = ""
        st.text_area.return_value = ""

        self.api.get_conversation_turns.return_value = {"items": [], "has_more": False}
        self.state["conversation_messages"] = []

        render_assistant(st, self.api, self.state, compact=True)

        # Verify welcome screen markdown was called
        markdown_calls = [c[0][0] for c in st.markdown.call_args_list if c[0]]
        welcome_found = any("How can I help you today?" in m and "chatgpt-welcome-container" in m for m in markdown_calls)
        self.assertTrue(welcome_found, "ChatGPT welcome container not found in st.markdown calls")

        # Verify 4 prompt buttons were rendered
        button_calls = [c[0][0] for c in st.button.call_args_list if c[0]]
        self.assertIn("💳 Check my balance", button_calls)
        self.assertIn("📊 Analyze spending", button_calls)
        self.assertIn("📄 View active loans", button_calls)
        self.assertIn("🛡️ Security alerts", button_calls)

    def test_copilot_composer_and_actions(self):
        """Sticky composer renders popover actions, multiline text input with 'Message Vaani...' and send button."""
        from frontend.pages.assistant import render as render_assistant

        st = MagicMock()
        st.container.return_value.__enter__.return_value = st
        st.popover.return_value.__enter__.return_value = st
        st.expander.return_value.__enter__.return_value = st
        st.columns.side_effect = lambda spec, **kw: tuple(MagicMock() for _ in range(len(spec) if isinstance(spec, (list, tuple)) else spec))
        st.file_uploader.return_value = None
        st.audio_input.return_value = None
        st.button.return_value = False
        st.text_input.return_value = ""
        st.text_area.return_value = ""

        self.api.get_conversation_turns.return_value = {"items": [], "has_more": False}
        self.state["conversation_messages"] = [{"role": "assistant", "text": "Hello!"}]

        render_assistant(st, self.api, self.state, compact=True)

        # Verify popover was created with icon=":material/add:" or "+"
        popover_calls = st.popover.call_args_list
        has_add_popover = any(
            c[1].get("icon") == ":material/add:" or (c[0] and ("+" in c[0][0] or "＋" in c[0][0] or "➕" in c[0][0]))
            for c in popover_calls
        )
        self.assertTrue(has_add_popover, "Plus popover with add icon or plus sign not found")

        # Verify '🔄 New conversation' action button in popover
        button_calls = [c[0][0] for c in st.button.call_args_list if c[0]]
        self.assertIn("🔄 New conversation", button_calls)

        # Verify send button ➤
        self.assertIn("➤", button_calls)

        # Verify microphone button is in composer pill
        button_keys = [c[1].get("key") for c in st.button.call_args_list if c[1].get("key")]
        self.assertIn("copilot_mic_toggle_btn", button_keys)

        # Verify multiline text area placeholder is 'Message Vaani...'
        text_area_calls = st.text_area.call_args_list
        self.assertTrue(len(text_area_calls) >= 1)
        self.assertEqual(text_area_calls[0][1].get("placeholder"), "Message Vaani...")

    def test_copilot_docked_voice_tray(self):
        """When copilot_show_voice is True, docked voice tray renders above composer without covering chat."""
        from frontend.pages.assistant import render as render_assistant

        st = MagicMock()
        st.container.return_value.__enter__.return_value = st
        st.popover.return_value.__enter__.return_value = st
        st.expander.return_value.__enter__.return_value = st
        st.columns.side_effect = lambda spec, **kw: tuple(MagicMock() for _ in range(len(spec) if isinstance(spec, (list, tuple)) else spec))
        st.file_uploader.return_value = None
        st.audio_input.return_value = None
        st.button.return_value = False
        st.text_input.return_value = ""
        st.text_area.return_value = ""

        self.api.get_conversation_turns.return_value = {"items": [], "has_more": False}
        self.state["copilot_show_voice"] = True
        self.state["conversation_messages"] = [{"role": "assistant", "text": "Hello!"}]

        render_assistant(st, self.api, self.state, compact=True)

        container_keys = [c[1].get("key") for c in st.container.call_args_list if c[1].get("key")]
        self.assertIn("copilot-voice-tray", container_keys)

        button_keys = [c[1].get("key") for c in st.button.call_args_list if c[1].get("key")]
        self.assertIn("btn_close_voice_tray", button_keys)


if __name__ == "__main__":
    unittest.main()
