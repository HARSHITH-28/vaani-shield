"""Tests for fintech UI components and responsive table-to-card parsing."""

import unittest
from frontend.components.fintech_ui import (
    format_inr,
    get_category_icon,
    parse_markdown_table_to_compact_cards,
    render_balance_hero_html,
    render_compact_assistant_tx_html,
    render_stepper_html,
    render_transaction_card_html,
)


class FintechUITests(unittest.TestCase):
    def test_format_inr(self):
        self.assertEqual(format_inr(142500), "₹1,42,500.00")
        self.assertEqual(format_inr(85000.5), "₹85,000.50")
        self.assertEqual(format_inr(1250.75), "₹1,250.75")
        self.assertEqual(format_inr(0), "₹0.00")
        self.assertEqual(format_inr(-4500), "- ₹4,500.00")
        self.assertEqual(format_inr(10000000), "₹1,00,00,000.00")

    def test_get_category_icon(self):
        self.assertEqual(get_category_icon("Shopping"), "🛍️")
        self.assertEqual(get_category_icon("Dining"), "☕")
        self.assertEqual(get_category_icon("Utilities"), "⚡")
        self.assertEqual(get_category_icon("Travel"), "🚗")
        self.assertEqual(get_category_icon("Salary"), "💰")
        self.assertEqual(get_category_icon("Unknown"), "💳")

    def test_render_balance_hero_html(self):
        html_out = render_balance_hero_html(142500, "VS1000001234", "SAVINGS", "Arjun Sharma")
        self.assertIn("₹1,42,500.00", html_out)
        self.assertIn("•••• •••• •••• 1234", html_out)
        self.assertIn("Arjun Sharma", html_out)
        self.assertIn("SAVINGS ACCOUNT · ACTIVE", html_out)
        self.assertTrue(html_out.startswith("<div class=\"fintech-hero-card\">"))

    def test_render_balance_hero_masked_when_hidden(self):
        html_out = render_balance_hero_html(142500, "VS1000001234", "SAVINGS", "Arjun Sharma", is_visible=False)
        self.assertIn("₹ ••••••••", html_out)
        self.assertNotIn("₹1,42,500.00", html_out)

    def test_render_stepper_html(self):
        steps = ["Application Details", "KYC Documents", "Review & Submit", "Track Application"]
        html_out = render_stepper_html(steps, 1)
        self.assertIn("stepper-item completed", html_out)
        self.assertIn("stepper-item active", html_out)
        self.assertIn("stepper-item upcoming", html_out)
        self.assertIn("KYC Documents", html_out)
        self.assertTrue(html_out.startswith("<div class='fintech-stepper'>"))

    def test_render_transaction_card_html(self):
        html_out = render_transaction_card_html("Amazon India", 1499.0, "2026-11-04", "Shopping", "SUCCESS", "DEBIT")
        self.assertIn("Amazon India", html_out)
        self.assertIn("- ₹1,499.00", html_out)
        self.assertIn("Shopping", html_out)
        self.assertIn("SUCCESS", html_out)

    def test_parse_markdown_table_to_compact_cards(self):
        sample_table = (
            "Here are your latest transactions:\n\n"
            "| Date | Merchant | Category | Type | Amount | Status |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| 2026-11-04 | Amazon Pay | Shopping | DEBIT | ₹1,499.00 | SUCCESS |\n"
            "| 2026-11-01 | Tech Corp | Salary | CREDIT | ₹85,000.00 | SUCCESS |\n\n"
            "Let me know if you need details on any of these."
        )
        converted, result_html = parse_markdown_table_to_compact_cards(sample_table)
        self.assertTrue(converted)
        self.assertIn("chat-tx-container", result_html)
        self.assertIn("Amazon Pay", result_html)
        self.assertIn("Tech Corp", result_html)
        self.assertIn("₹1,499.00", result_html)
        self.assertIn("₹85,000.00", result_html)

    def test_parse_non_table_text_returns_unchanged(self):
        text = "Your account balance is ₹1,42,500.00 in your primary savings account."
        converted, result = parse_markdown_table_to_compact_cards(text)
        self.assertFalse(converted)
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main()

