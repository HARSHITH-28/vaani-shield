"""Offline tests for persistent floating assistant panel state."""

import unittest
from contextlib import nullcontext

from frontend.components.floating_assistant import (
    render_floating_assistant,
    toggle_assistant_panel,
)


class StopRerun(Exception):
    pass


class FakeUI:
    def __init__(self, click=False):
        self.click = click

    def container(self, **kwargs):
        return nullcontext()

    def button(self, *args, **kwargs):
        return self.click and kwargs.get("key") == "floating-assistant-toggle"

    def rerun(self):
        raise StopRerun


class FloatingAssistantStateTests(unittest.TestCase):
    def test_toggle_preserves_conversation_across_feature_navigation(self):
        state = {
            "assistant_open": False,
            "conversation_id": "existing-conversation",
            "conversation_messages": [{"role": "assistant", "text": "Existing reply"}],
            "main_feature": "Scam Detection",
        }
        toggle_assistant_panel(state)
        self.assertTrue(state["assistant_open"])
        self.assertEqual(state["conversation_id"], "existing-conversation")
        self.assertEqual(state["conversation_messages"][0]["text"], "Existing reply")
        state["main_feature"] = "Customer Support"
        self.assertTrue(state["assistant_open"])
        self.assertEqual(state["conversation_id"], "existing-conversation")

    def test_floating_button_opens_panel(self):
        state = {"assistant_open": False, "conversation_id": None, "conversation_messages": []}
        with self.assertRaises(StopRerun):
            render_floating_assistant(FakeUI(click=True), object(), state)
        self.assertTrue(state["assistant_open"])


if __name__ == "__main__":
    unittest.main()
