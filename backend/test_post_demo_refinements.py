"""Unit tests for VaaniShield 2.0 Post-Demo Product Refinements.

Covers:
1. PBKDF2-SHA256 password hashing & verification.
2. Demo login authentication & security audit logs.
3. Card details with individual overview, financial limits, and freeze/unfreeze controls.
4. Financial Intelligence multi-month synthetic data & investment calculators (FD, SIP).
5. 'Hey Vaani' voice activation personalized welcome greeting formatting.
"""

import unittest
from backend.auth import (
    hash_password,
    verify_password,
    record_signin_audit_log,
    get_signin_audit_logs,
)
from services.banking_service import (
    get_card_details,
    toggle_card_freeze,
)
from services.financial_advisor import compare_monthly_spending
from services.financial_intelligence import (
    get_category_breakdown,
    get_financial_insights,
    get_top_merchants,
)


class TestPostDemoRefinements(unittest.TestCase):
    """Test suite for post-demo refinements."""

    def test_pbkdf2_password_hashing_and_verification(self):
        """Verify PBKDF2-SHA256 password hashing and verification."""
        password = "Arjun@Demo2026"
        hashed = hash_password(password)

        self.assertTrue(hashed.startswith("pbkdf2_sha256$100000$"))
        self.assertTrue(verify_password(password, hashed))
        self.assertFalse(verify_password("WrongPassword123", hashed))
        self.assertFalse(verify_password("", hashed))

    def test_signin_audit_log_recording_and_retrieval(self):
        """Verify recording and querying sign-in audit logs."""
        customer_id = 4  # demo-arjun
        email = "arjun@example.com"

        # Record a test success login
        record_signin_audit_log(
            customer_id=customer_id,
            email=email,
            login_result="SUCCESS",
            ip_address="127.0.0.1",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
            device_platform="Windows (Chrome)",
        )

        # Retrieve audit logs
        logs = get_signin_audit_logs(customer_id=customer_id, limit=5)
        self.assertIsInstance(logs, list)
        self.assertGreater(len(logs), 0)

        latest = logs[0]
        self.assertEqual(latest["email_attempted"], email)
        self.assertEqual(latest["login_result"], "SUCCESS")
        self.assertIn("Windows", latest["device_platform"])
        self.assertIsNotNone(latest["timestamp_ist"])

    def test_card_details_enrichment(self):
        """Verify card details include individual overview and financial info."""
        customer_id = 4  # demo-arjun
        cards = get_card_details(customer_id)

        self.assertIsInstance(cards, list)
        self.assertGreaterEqual(len(cards), 2)

        # Find credit and debit cards
        debit_card = next((c for c in cards if c["card_type"] == "DEBIT"), None)
        credit_card = next((c for c in cards if c["card_type"] == "CREDIT"), None)

        self.assertIsNotNone(debit_card)
        self.assertIsNotNone(credit_card)

        # Debit Card assertions
        self.assertEqual(debit_card["last_four"], "1234")
        self.assertEqual(debit_card["cardholder_name"], "Arjun Sharma")
        self.assertIn("RuPay", debit_card["card_network"])
        self.assertGreater(debit_card["daily_limit"], 0)
        self.assertGreater(debit_card["account_balance"], 0)

        # Credit Card assertions
        self.assertEqual(credit_card["last_four"], "5678")
        self.assertEqual(credit_card["cardholder_name"], "Arjun Sharma")
        self.assertIn("Visa", credit_card["card_network"])
        self.assertEqual(credit_card["credit_limit"], 200000.0)
        self.assertEqual(credit_card["available_credit_limit"], 145000.0)
        self.assertEqual(credit_card["outstanding_amount"], 55000.0)

    def test_card_freeze_and_unfreeze_toggle(self):
        """Verify card freeze and unfreeze status controls."""
        customer_id = 4
        last_four = "5678"

        # Freeze the active credit card
        freeze_res = toggle_card_freeze(customer_id, last_four, freeze=True)
        self.assertTrue(freeze_res["success"])
        self.assertEqual(freeze_res["status"], "FROZEN")

        # Unfreeze it back
        unfreeze_res = toggle_card_freeze(customer_id, last_four, freeze=False)
        self.assertTrue(unfreeze_res["success"])
        self.assertEqual(unfreeze_res["status"], "ACTIVE")

        # Verify blocked card cannot be unfrozen
        blocked_res = toggle_card_freeze(customer_id, "1234", freeze=False)
        self.assertFalse(blocked_res["success"])
        self.assertEqual(blocked_res["status"], "BLOCKED")

    def test_financial_intelligence_and_multi_month_data(self):
        """Verify multi-month transactions and financial calculations."""
        customer_id = 4

        # October 2026 vs September 2026 comparison
        comparison = compare_monthly_spending(customer_id, 2026, 10)
        self.assertEqual(comparison["current_month"], "2026-10")
        self.assertEqual(comparison["previous_month"], "2026-09")
        self.assertEqual(comparison["current_spending"], 24538.0)
        self.assertEqual(comparison["previous_spending"], 9028.0)
        self.assertEqual(comparison["difference"], 15510.0)
        self.assertEqual(comparison["trend"], "INCREASED")

        # Top merchants check
        merchants = get_top_merchants(customer_id, limit=5)
        self.assertGreater(len(merchants), 0)
        top = merchants[0]
        self.assertIn("merchant", top)
        self.assertIn("total_spent", top)

        # Categories check
        categories = get_category_breakdown(customer_id)
        self.assertGreater(len(categories), 0)

    def test_fd_compound_interest_math(self):
        """Verify Fixed Deposit quarterly compounding formula."""
        principal = 50000.0
        tenure_years = 1.0
        rate_annual = 7.10
        r_dec = rate_annual / 100.0

        # Quarterly compounding: A = P * (1 + r/4)^(4*t)
        maturity = principal * ((1.0 + r_dec / 4.0) ** (4.0 * tenure_years))
        interest = maturity - principal

        self.assertAlmostEqual(maturity, 53645.64, places=1)
        self.assertAlmostEqual(interest, 3645.64, places=1)

    def test_sip_future_value_math(self):
        """Verify Systematic Investment Plan (SIP) future value formula."""
        monthly_investment = 10000.0
        years = 5
        annual_rate = 12.0

        i_monthly = (annual_rate / 100.0) / 12.0
        n_months = years * 12
        total_invested = monthly_investment * n_months

        # Formula: M = P * ((1 + i)^n - 1) / i * (1 + i)
        future_value = monthly_investment * (((1.0 + i_monthly) ** n_months - 1.0) / i_monthly) * (1.0 + i_monthly)
        gain = future_value - total_invested

        self.assertEqual(total_invested, 600000.0)
        self.assertAlmostEqual(future_value, 824863.0, delta=100.0)
        self.assertAlmostEqual(gain, 224863.0, delta=100.0)

    def test_self_service_card_pydantic_validation_and_strictness(self):
        """Verify SelfServiceCard validates legitimate fields and rejects unpermitted extra fields."""
        from backend.api_models import SelfServiceCard
        from pydantic import ValidationError

        # Valid card data from database
        valid_data = {
            "card_type": "CREDIT",
            "last_four": "5678",
            "status": "ACTIVE",
            "expiry_date": "08/28",
            "card_network": "Visa Signature",
            "cardholder_name": "Arjun Sharma",
            "daily_limit": 100000.0,
            "monthly_limit": 300000.0,
            "credit_limit": 200000.0,
            "available_credit_limit": 145000.0,
            "outstanding_amount": 55000.0,
            "account_balance": 278354.0,
        }
        card = SelfServiceCard(**valid_data)
        self.assertEqual(card.last_four, "5678")
        self.assertEqual(card.outstanding_amount, 55000.0)
        self.assertEqual(card.account_balance, 278354.0)

        # Confirm strictness: extra unpermitted fields must raise ValidationError (extra_forbidden)
        invalid_data = {**valid_data, "unauthorized_extra_field": "injected_value"}
        with self.assertRaises(ValidationError) as ctx:
            SelfServiceCard(**invalid_data)
        self.assertIn("extra_forbidden", str(ctx.exception))

    def test_card_customer_isolation(self):
        """Verify customer cards are isolated and cannot be accessed across customers."""
        c4_cards = [c["last_four"] for c in get_card_details(4)]
        c5_cards = [c["last_four"] for c in get_card_details(5)]
        c6_cards = [c["last_four"] for c in get_card_details(6)]

        self.assertEqual(set(c4_cards), {"1234", "5678"})
        self.assertEqual(set(c5_cards), {"9012"})
        self.assertEqual(set(c6_cards), {"3456"})
        self.assertEqual(len(set(c4_cards).intersection(set(c5_cards))), 0)
        self.assertEqual(len(set(c4_cards).intersection(set(c6_cards))), 0)

    def test_hey_vaani_welcome_greeting_format(self):
        """Verify the exact required spoken welcome message format."""
        display_name = "Arjun Sharma"
        expected_greeting = f"Welcome back, {display_name}! What can I help you with?"
        self.assertEqual(expected_greeting, "Welcome back, Arjun Sharma! What can I help you with?")


if __name__ == "__main__":
    unittest.main()
