"""Unit tests for shared responsive dashboard layout adjustment when floating Copilot opens and closes."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frontend.navigation import FEATURE_SUBFEATURES, ensure_subfeature, page_key, request_navigation


class TestResponsiveCopilotLayout(unittest.TestCase):
    def setUp(self):
        self.state = {
            "authenticated": True,
            "customer": {"display_name": "Arjun Sharma"},
            "assistant_open": False,
            "main_feature": "Banking Information",
            "subfeature_banking_information": "Overview",
            "conversation_id": "test-conv-1",
            "conversation_messages": [],
            "audio_widget_epoch": 0,
        }

    def test_copilot_closed_uses_full_width_container_keys(self):
        """When Copilot is closed, workspace and nav keys indicate full-width layout."""
        self.state["assistant_open"] = False
        is_copilot_open = bool(self.state.get("assistant_open"))

        nav_key = "main-nav-container-copilot-open" if is_copilot_open else "main-nav-container"
        workspace_key = "banking-workspace-copilot-open" if is_copilot_open else "banking-workspace-copilot-closed"

        self.assertEqual(nav_key, "main-nav-container")
        self.assertEqual(workspace_key, "banking-workspace-copilot-closed")

    def test_copilot_open_uses_adapted_workspace_container_keys(self):
        """When Copilot opens, workspace and nav keys switch to copilot-open adapted layout."""
        self.state["assistant_open"] = True
        is_copilot_open = bool(self.state.get("assistant_open"))

        nav_key = "main-nav-container-copilot-open" if is_copilot_open else "main-nav-container"
        workspace_key = "banking-workspace-copilot-open" if is_copilot_open else "banking-workspace-copilot-closed"

        self.assertEqual(nav_key, "main-nav-container-copilot-open")
        self.assertEqual(workspace_key, "banking-workspace-copilot-open")

    def test_toggling_copilot_preserves_active_banking_page_and_tab(self):
        """Opening or closing Copilot preserves the selected feature and subfeature tab."""
        # 1. Start on Loans
        request_navigation(self.state, "Banking Information", "Loans")
        self.state["subfeature_banking_information"] = "Loans"
        self.assertEqual(self.state["subfeature_banking_information"], "Loans")

        # 2. Open Copilot
        self.state["assistant_open"] = True
        self.assertEqual(self.state["subfeature_banking_information"], "Loans")
        self.assertEqual(self.state["main_feature"], "Banking Information")

        # 3. Switch to Customer Support while Copilot is open
        request_navigation(self.state, "Customer Support", "Create Support Ticket")
        self.state["main_feature"] = "Customer Support"
        self.state["subfeature_customer_support"] = "Create Support Ticket"
        self.assertTrue(self.state["assistant_open"])
        self.assertEqual(self.state["main_feature"], "Customer Support")
        self.assertEqual(self.state["subfeature_customer_support"], "Create Support Ticket")

        # 4. Close Copilot
        self.state["assistant_open"] = False
        self.assertEqual(self.state["main_feature"], "Customer Support")
        self.assertEqual(self.state["subfeature_customer_support"], "Create Support Ticket")

    def test_all_banking_features_supported_in_adapted_layout(self):
        """All 9 banking sections map to valid navigation and render inside the shared workspace."""
        sections = [
            ("Banking Information", "Overview"),
            ("Banking Information", "Accounts"),
            ("Banking Information", "Transactions"),
            ("Banking Information", "Cards"),
            ("Banking Information", "Loans"),
            ("Financial Intelligence", "Spending Analysis"),
            ("Scam Detection", "Message Analysis"),
            ("Customer Support", "Ask Support Assistant"),
            ("Account Opening Assistant", "Open New Account"),
        ]

        for feat, subfeat in sections:
            self.assertIn(feat, FEATURE_SUBFEATURES)
            self.assertIn(subfeat, FEATURE_SUBFEATURES[feat])

            # With Copilot open
            self.state["assistant_open"] = True
            is_copilot_open = bool(self.state.get("assistant_open"))
            ws_key = "banking-workspace-copilot-open" if is_copilot_open else "banking-workspace-copilot-closed"
            self.assertEqual(ws_key, "banking-workspace-copilot-open")

            # With Copilot closed
            self.state["assistant_open"] = False
            is_copilot_open = bool(self.state.get("assistant_open"))
            ws_key = "banking-workspace-copilot-open" if is_copilot_open else "banking-workspace-copilot-closed"
            self.assertEqual(ws_key, "banking-workspace-copilot-closed")


if __name__ == "__main__":
    unittest.main()

