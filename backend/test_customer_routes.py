"""Self-service API tests using a disposable SQLite database."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import auth, main
from services import database
from services.principal import CustomerPrincipal


def _principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


class SelfServiceCustomerRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "customer-api.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        self._insert_demo_records()
        self.original_overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides[auth.get_authenticated_principal] = lambda: _principal()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.original_overrides)
        self.db_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _insert_demo_records():
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_key, email, name, monthly_income) VALUES (?, ?, ?, ?)",
                ("demo-arjun", "arjun@example.test", "Arjun Demo", 10000),
            )
            conn.execute(
                "INSERT INTO customers (customer_key, email, name, monthly_income) VALUES (?, ?, ?, ?)",
                ("demo-priya", "priya@example.test", "Priya Demo", 20000),
            )
            arjun_id = conn.execute("SELECT customer_id FROM customers WHERE customer_key=?", ("demo-arjun",)).fetchone()[0]
            priya_id = conn.execute("SELECT customer_id FROM customers WHERE customer_key=?", ("demo-priya",)).fetchone()[0]
            arjun_account = conn.execute(
                "INSERT INTO accounts (customer_id, account_number, account_type, balance, status) VALUES (?, ?, ?, ?, ?)",
                (arjun_id, "1234567890123456", "SAVINGS", 777.25, "ACTIVE"),
            ).lastrowid
            priya_account = conn.execute(
                "INSERT INTO accounts (customer_id, account_number, account_type, balance, status) VALUES (?, ?, ?, ?, ?)",
                (priya_id, "9999999999999999", "SAVINGS", 9999, "ACTIVE"),
            ).lastrowid
            for txn_type, amount, merchant, category, date_value, ref in (
                ("DEBIT", 100, "Market", "FOOD", "2026-01-05 12:00:00", "ARJUN-1"),
                ("DEBIT", 50, "Cafe", "FOOD", "2026-01-12 12:00:00", "ARJUN-2"),
                ("CREDIT", 2000, "Payroll", "INCOME", "2026-01-20 12:00:00", "ARJUN-3"),
                ("DEBIT", 40, "Market", "FOOD", "2025-12-15 12:00:00", "ARJUN-4"),
            ):
                conn.execute(
                    """INSERT INTO transactions
                    (account_id, transaction_type, amount, merchant, category, status, reference_id, transaction_date, description)
                    VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?)""",
                    (arjun_account, txn_type, amount, merchant, category, ref, date_value, "Demo transaction"),
                )
            conn.execute(
                """INSERT INTO transactions
                (account_id, transaction_type, amount, merchant, category, status, reference_id, transaction_date)
                VALUES (?, 'DEBIT', 800, 'Other', 'SHOPPING', 'SUCCESS', 'PRIYA-1', '2026-01-10 12:00:00')""",
                (priya_account,),
            )
            conn.execute(
                "INSERT INTO cards (account_id, card_type, last_four, status, expiry_date) VALUES (?, ?, ?, ?, ?)",
                (arjun_account, "DEBIT", "4321", "ACTIVE", "12/29"),
            )
            conn.execute(
                """INSERT INTO loans (account_id, loan_type, principal_amount, interest_rate, tenure_months, outstanding_amount, status)
                VALUES (?, 'HOME', 100000, 7.5, 120, 75000, 'ACTIVE')""",
                (arjun_account,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_self_service_routes_scope_identity_and_mask_sensitive_fields(self):
        accounts = self.client.get("/api/v1/me/accounts?customer_key=demo-priya")
        self.assertEqual(accounts.status_code, 200)
        self.assertEqual(accounts.json(), [{
            "account_type": "SAVINGS", "account_number": "••••3456",
            "balance": 777.25, "status": "ACTIVE",
        }])
        self.assertNotIn("customer_id", accounts.text)
        self.assertNotIn("account_id", accounts.text)
        self.assertEqual(self.client.get("/api/v1/me/balance").json()["total_balance"], 777.25)

        context = self.client.get("/api/v1/me/context")
        self.assertEqual(context.status_code, 200)
        body = context.json()
        self.assertEqual(body["customer"], {"name": "Arjun Demo", "monthly_income": 10000})
        self.assertNotIn("customer_id", context.text)
        self.assertNotIn("customer_key", context.text)
        self.assertNotIn("arjun@example.test", context.text)
        self.assertEqual(body["banking"]["account_balance"][0]["account_number"], "••••3456")
        self.assertNotIn("transaction_id", body["banking"]["recent_transactions"][0])
        self.assertNotIn("customer_id", body["financial_advisory"])

    def test_transactions_recent_and_search_filters_are_customer_scoped(self):
        recent = self.client.get("/api/v1/me/transactions?limit=2&customer_id=2").json()
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(row["reference_id"].startswith("ARJUN-") for row in recent))
        self.assertNotIn("transaction_id", recent[0])

        filtered = self.client.get(
            "/api/v1/me/transactions/search",
            params={
                "merchant": "market", "transaction_type": "DEBIT",
                "min_amount": 50, "max_amount": 150,
                "start_date": "2026-01-01", "end_date": "2026-01-31",
            },
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([row["amount"] for row in filtered.json()], [100])
        self.assertEqual(
            self.client.get("/api/v1/me/transactions/search?start_date=2026-01-01").status_code,
            422,
        )

    def test_cards_loans_and_financial_metrics_preserve_existing_calculations(self):
        cards = self.client.get("/api/v1/me/cards").json()
        loans = self.client.get("/api/v1/me/loans").json()
        self.assertEqual(cards[0]["last_four"], "4321")
        self.assertNotIn("card_id", cards[0])
        self.assertEqual(loans[0]["outstanding_amount"], 75000)
        self.assertNotIn("loan_id", loans[0])

        summary = self.client.get("/api/v1/me/financial/summary").json()
        self.assertEqual(summary["total_spent"], 190)
        self.assertEqual(summary["total_credited"], 2000)
        self.assertEqual(summary["net_cash_flow"], 1810)
        categories = self.client.get("/api/v1/me/financial/categories").json()
        self.assertEqual(categories[0]["category"], "FOOD")
        self.assertEqual(categories[0]["total_amount"], 190)
        merchants = self.client.get("/api/v1/me/financial/merchants?limit=1").json()
        self.assertEqual(len(merchants), 1)
        comparison = self.client.get("/api/v1/me/financial/monthly-comparison?year=2026&month=1").json()
        self.assertEqual(comparison["current_spending"], 150)
        self.assertEqual(comparison["previous_spending"], 40)
        self.assertEqual(comparison["difference"], 110)
        advisory = self.client.get("/api/v1/me/financial/advisory?year=2026&month=1")
        self.assertEqual(advisory.status_code, 200)
        self.assertNotIn("customer_id", advisory.json())

    def test_unauthenticated_and_invalid_customer_query(self):
        main.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/api/v1/me/context").status_code, 401)
        main.app.dependency_overrides[auth.get_authenticated_principal] = lambda: _principal()
        response = self.client.get("/api/v1/me/accounts?customer_ref=demo-priya")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["account_number"], "••••3456")


if __name__ == "__main__":
    unittest.main()
