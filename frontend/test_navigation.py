"""Navigation state tests for feature/subfeature switching."""

import unittest

from frontend.navigation import (
    FEATURE_SUBFEATURES,
    apply_pending_navigation,
    ensure_subfeature,
    get_previous_navigation,
    navigate_back,
    page_key,
    request_navigation,
    set_subfeature,
)


class NavigationStateTests(unittest.TestCase):
    def test_each_feature_has_expected_default_and_subfeatures(self):
        state = {}
        for feature, subfeatures in FEATURE_SUBFEATURES.items():
            self.assertEqual(ensure_subfeature(state, feature), subfeatures[0])
        self.assertEqual(len(FEATURE_SUBFEATURES), 5)

    def test_subfeature_selection_is_retained_per_feature(self):
        state = {}
        set_subfeature(state, "Banking Information", "Transactions")
        set_subfeature(state, "Financial Intelligence", "Merchant Analysis")
        self.assertEqual(ensure_subfeature(state, "Banking Information"), "Transactions")
        self.assertEqual(ensure_subfeature(state, "Financial Intelligence"), "Merchant Analysis")
        self.assertEqual(page_key("Banking Information", "Cards"), "Cards")

    def test_invalid_subfeature_is_rejected(self):
        with self.assertRaises(ValueError):
            set_subfeature({}, "Banking Information", "Transfer")
        with self.assertRaises(ValueError):
            request_navigation({}, "Banking Information", "Transfer")

    def test_request_and_apply_pending_navigation_stages_without_immediate_widget_mutation(self):
        state = {
            "main_feature": "Scam Detection",
            "subfeature_banking_information": "Overview",
        }
        request_navigation(state, "Banking Information", "Review & Submit", step="review")
        self.assertEqual(state["main_feature"], "Scam Detection")
        self.assertEqual(state["subfeature_banking_information"], "Overview")
        self.assertEqual(state["account_application_step"], "review")

        applied = apply_pending_navigation(state)
        self.assertTrue(applied)
        self.assertIsNone(state["pending_navigation"])
        self.assertEqual(state["main_feature"], "Account Opening Assistant")
        self.assertEqual(state["subfeature_account_opening_assistant"], "Review & Submit")
        self.assertFalse(apply_pending_navigation(state))

    def test_navigation_history_and_back_functionality(self):
        state = {
            "main_feature": "Banking Information",
            "subfeature_banking_information": "Accounts",
            "nav_history": [],
            "account_application_cache": {"application_ref": "APP-TEST-123"},
        }
        # Navigate to Account Opening Assistant > Open New Account
        request_navigation(state, "Account Opening Assistant", "Open New Account")
        apply_pending_navigation(state)
        self.assertEqual(state["main_feature"], "Account Opening Assistant")
        self.assertEqual(state["subfeature_account_opening_assistant"], "Open New Account")
        # History recorded previous location
        self.assertIn(("Banking Information", "Accounts"), state["nav_history"])

        # Check previous navigation helper
        prev_feat, prev_sub = get_previous_navigation(state)
        self.assertEqual(prev_feat, "Banking Information")
        self.assertEqual(prev_sub, "Accounts")

        # Navigate back safely
        target = navigate_back(state)
        self.assertEqual(target, ("Banking Information", "Accounts"))
        apply_pending_navigation(state)
        self.assertEqual(state["main_feature"], "Banking Information")
        self.assertEqual(state["subfeature_banking_information"], "Accounts")
        # Application draft state is preserved intact!
        self.assertEqual(state["account_application_cache"]["application_ref"], "APP-TEST-123")


if __name__ == "__main__":
    unittest.main()
