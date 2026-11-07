"""Security and compatibility tests for legacy customer dashboard routes."""

import os
import unittest
from datetime import datetime, timezone
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth, main
from services.principal import CustomerPrincipal


TEST_SECRET = "legacy-route-test-signing-secret-at-least-32-bytes"
ARJUN_PROFILE = {
    "customer_id": 41,
    "customer_key": "demo-arjun",
    "name": "Arjun Demo",
    "email": "arjun@example.test",
    "phone": "555-0100",
    "monthly_income": 90000,
}


class LegacyCustomerRouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "VAANISHIELD_DEMO_SESSION_SECRET": TEST_SECRET,
            "VAANISHIELD_DEMO_COOKIE_SECURE": "false",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.original_overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides.clear()
        self.addCleanup(self._restore_overrides)
        self.auth_profile_patch = patch.object(
            auth, "get_customer_profile_by_key", return_value=ARJUN_PROFILE.copy()
        )
        self.auth_profile_patch.start()
        self.addCleanup(self.auth_profile_patch.stop)
        self.main_profile_patch = patch.object(
            main, "get_customer_profile_by_key", return_value=ARJUN_PROFILE.copy()
        )
        self.main_profile_patch.start()
        self.addCleanup(self.main_profile_patch.stop)
        self._set_arjun_cookie()

    def _restore_overrides(self):
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.original_overrides)

    def _set_arjun_cookie(self):
        now = int(datetime.now(timezone.utc).timestamp())
        token = auth._encode_session_token("demo-arjun", now, now + 1800)
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, token)

    def test_unauthenticated_customer_data_request_returns_401_without_lookup(self):
        self.client.cookies.clear()
        with patch.object(main, "get_account_balance") as balance:
            response = self.client.get("/api/customers/demo-arjun/balance")
        self.assertEqual(response.status_code, 401)
        balance.assert_not_called()

    def test_invalid_and_expired_cookies_return_401(self):
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, "invalid.token.value")
        invalid = self.client.get("/api/customers/demo-arjun/context")
        self.assertEqual(invalid.status_code, 401)

        now = int(time.time())
        expired = auth._encode_session_token("demo-arjun", now - 120, now - 60)
        self.client.cookies.set(auth.SESSION_COOKIE_NAME, expired, path="/api")
        response = self.client.get("/api/customers/demo-arjun/context")
        self.assertEqual(response.status_code, 401)

    def test_authenticated_arjun_can_access_own_route(self):
        with patch.object(main, "get_customer_profile_by_key", return_value=ARJUN_PROFILE):
            with patch.object(main, "get_account_balance", return_value=[{
                "account_number": "1234567890123456", "account_type": "SAVINGS",
                "balance": 1000, "status": "ACTIVE",
            }]) as balance:
                response = self.client.get("/api/customers/demo-arjun/balance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["account_number"], "••••3456")
        balance.assert_called_once_with(41)

    def test_demo_login_cookie_authenticates_legacy_dashboard_context(self):
        self.client.cookies.clear()
        context = {
            "customer": ARJUN_PROFILE.copy(),
            "banking": {
                "account_balance": [], "recent_transactions": [], "cards": [], "loans": [],
            },
            "financial_intelligence": {"summary": {}, "insights": []},
            "financial_advisory": {},
            "metadata": {"customer_id": 41, "customer_key": "demo-arjun"},
        }
        with patch.object(main, "get_customer_context", return_value=context):
            login = self.client.post(
                "/api/v1/auth/demo/session", json={"customer_key": "demo-arjun"}
            )
            self.assertEqual(login.status_code, 200)
            set_cookie = login.headers["set-cookie"].lower()
            self.assertIn(f"{auth.SESSION_COOKIE_NAME}=", set_cookie)
            self.assertIn("path=/api;", set_cookie)
            self.assertIn("httponly", set_cookie)
            self.assertIn("samesite=strict", set_cookie)
            self.assertIn("max-age=3600", set_cookie)
            self.assertNotIn("domain=", set_cookie)  # host-only cookie
            self.assertNotIn("; secure", set_cookie)  # local HTTP demo configuration
            self.assertIn("max-age=0", set_cookie)  # prior /api/v1 cookie is retired

            dashboard = self.client.get("/api/customers/demo-arjun/context")

        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["customer"]["name"], "Arjun Demo")

    def test_cross_customer_reference_is_rejected_before_data_service(self):
        with patch.object(main, "get_customer_profile_by_key") as lookup:
            with patch.object(main, "get_account_balance") as balance:
                response = self.client.get("/api/customers/demo-priya/balance")
        self.assertEqual(response.status_code, 403)
        lookup.assert_not_called()
        balance.assert_not_called()

    def test_customer_listing_requires_auth_and_returns_only_current_profile(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/customers").status_code, 401)
        self._set_arjun_cookie()
        with patch.object(main, "get_customer_profile_by_key", return_value=ARJUN_PROFILE):
            response = self.client.get("/api/customers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"name": "Arjun Demo"}])

    def test_profile_and_context_omit_internal_and_contact_fields(self):
        with patch.object(main, "get_customer_profile_by_key", return_value=ARJUN_PROFILE):
            profile = self.client.get("/api/customers/demo-arjun/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json(), {"name": "Arjun Demo", "monthly_income": 90000})

        context = {
            "customer": ARJUN_PROFILE.copy(),
            "banking": {
                "account_balance": [{"account_number": "1234567890123456", "balance": 1000}],
                "recent_transactions": [{"transaction_id": 3, "merchant": "Shop", "amount": 2}],
                "cards": [{"card_id": 5, "last_four": "1111"}],
                "loans": [{"loan_id": 6, "loan_type": "HOME"}],
            },
            "financial_intelligence": {},
            "financial_advisory": {},
            "metadata": {"customer_id": 41, "customer_key": "demo-arjun", "report_period": "2026-01"},
        }
        with patch.object(main, "get_customer_context", return_value=context):
            response = self.client.get("/api/customers/demo-arjun/context")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["customer"], {"name": "Arjun Demo", "monthly_income": 90000})
        self.assertEqual(body["banking"]["account_balance"][0]["account_number"], "••••3456")
        self.assertNotIn("transaction_id", body["banking"]["recent_transactions"][0])
        self.assertNotIn("card_id", body["banking"]["cards"][0])
        self.assertNotIn("loan_id", body["banking"]["loans"][0])
        self.assertNotIn("customer_id", body["metadata"])
        self.assertNotIn("customer_key", body["metadata"])

    def test_dashboard_routes_preserve_required_shapes_for_authenticated_customer(self):
        prefix = "/api/customers/demo-arjun"
        cases = [
            ("/context", "get_customer_context", {
                "customer": {"name": "Arjun Demo", "monthly_income": 90000},
                "banking": {"account_balance": [], "recent_transactions": [], "cards": [], "loans": []},
                "financial_intelligence": {"summary": {"total_spent": 0}, "insights": []},
                "financial_advisory": {"savings_analysis": {}, "spending_comparison": {}, "budget_analysis": {}},
                "metadata": {"report_period": "2026-01", "is_synthetic_data": True},
            }),
            ("/balance", "get_account_balance", [{"account_number": "••••3456", "balance": 10}]),
            ("/transactions?limit=100", "get_recent_transactions", [{"merchant": "Shop", "amount": 2}]),
            ("/transactions/search?merchant=Shop", "search_transactions", [{"merchant": "Shop", "amount": 2}]),
            ("/cards", "get_card_details", [{"card_type": "DEBIT", "last_four": "1111"}]),
            ("/loans", "get_loan_details", [{"loan_type": "HOME", "outstanding_amount": 1}]),
            ("/financial/categories", "get_category_breakdown", [{"category": "Food", "amount": 3}]),
            ("/financial/merchants", "get_top_merchants", [{"merchant": "Shop", "amount": 2}]),
            ("/financial/monthly-comparison?year=2026&month=1", "compare_monthly_spending", {"difference": 0}),
        ]
        for suffix, service_name, result in cases:
            with self.subTest(route=suffix):
                with patch.object(main, service_name, return_value=result):
                    response = self.client.get(prefix + suffix)
                self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
