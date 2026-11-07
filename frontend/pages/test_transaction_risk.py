"""Offline tests for the authenticated Transaction Risk frontend flow."""

import unittest
from contextlib import nullcontext

from frontend.components.conversation import render_transaction_risk_assessment
from frontend.pages.scam_detection import (
    _render_transaction_risk,
    _safe_transaction_rows,
    assess_selected_transaction,
    begin_transaction_risk_discussion,
    load_transaction_risk_history,
    load_transaction_risk_transactions,
    open_transaction_risk_assessment,
)
from frontend.api_client import APIError


TRANSACTION_REF = "TR-" + "A" * 48
ASSESSMENT_ID = "123e4567-e89b-42d3-a456-426614174000"


def assessment(level="HIGH"):
    return {
        "assessment_id": ASSESSMENT_ID,
        "transaction_ref": TRANSACTION_REF,
        "created_at": "2026-11-05T10:30:00+00:00",
        "assessment": {
            "risk_level": level,
            "risk_score": 72,
            "indicators": [{
                "indicator_code": "unusual_amount", "severity": "HIGH",
                "explanation": "Amount is unusual against the available history.",
                "evidence": ["Amount exceeded the historical baseline."],
            }],
            "evidence": ["Compared with prior customer transactions."],
            "evidence_strength": "MODERATE",
            "explanation": "A deterministic screening explanation.",
            "warning": "This result does not confirm fraud.",
            "recommended_actions": ["Verify the transaction in your bank's official app."],
            "history_sufficiency": {
                "level": "ADEQUATE", "matching_history_count": 20,
                "minimum_for_amount_baseline": 5, "time_coverage_days": 120,
                "frequency_baseline_ready": True, "excluded_history_count": 0,
            },
            "engine_version": "test-engine",
            "context_notes": [],
        },
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.transactions = [{
            "transaction_id": 31, "account_id": 9, "customer_id": 7,
            "reference_id": "acct-derived-secret", "transaction_ref": TRANSACTION_REF,
            "transaction_date": "2026-11-04", "merchant": "Sample Store",
            "amount": 1250.0, "transaction_type": "DEBIT", "status": "COMPLETED",
        }]
        self.assessments = {
            "items": [{
                "assessment_id": ASSESSMENT_ID, "transaction_ref": TRANSACTION_REF,
                "risk_level": "HIGH", "risk_score": 72,
                "evidence_strength": "MODERATE", "created_at": "2026-11-05T10:30:00+00:00",
            }],
            "next_cursor": "cursor-2", "has_more": True,
        }
        self.result = assessment()

    def get_my_transactions(self, limit):
        self.calls.append(("transactions", limit))
        return self.transactions

    def assess_transaction_risk(self, transaction_ref):
        self.calls.append(("assess", transaction_ref))
        return self.result

    def list_transaction_risk_assessments(self, limit, cursor=None):
        self.calls.append(("history", limit, cursor))
        return self.assessments

    def get_transaction_risk_assessment(self, assessment_id):
        self.calls.append(("detail", assessment_id))
        return self.result

    def create_conversation(self):
        self.calls.append(("create_conversation",))
        return {"conversation_id": "new-conversation"}


class FakeUI:
    def __init__(self, click=None):
        self.click = click
        self.rendered = []

    def container(self, **kwargs):
        return nullcontext()

    def spinner(self, *args, **kwargs):
        return nullcontext()

    def columns(self, count):
        return [self for _ in range(count)]

    def button(self, label, **kwargs):
        return kwargs.get("key") == self.click

    def rerun(self):
        raise RuntimeError("rerun")

    def selectbox(self, label, options, **kwargs):
        self.rendered.append(("options", tuple(options)))
        return options[0]

    def __getattr__(self, name):
        if name in {"success", "info", "warning", "error", "markdown", "caption", "write", "text", "metric", "title", "subheader", "divider"}:
            return lambda *args, **kwargs: self.rendered.append((name, args))
        raise AttributeError(name)


class TransactionRiskFrontendTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.state = {"customer": {"customer_key": "demo-arjun"}}

    def test_transaction_projection_uses_opaque_reference_only(self):
        rows = _safe_transaction_rows(self.api.transactions)
        self.assertEqual(rows[0]["transaction_ref"], TRANSACTION_REF)
        self.assertNotIn("transaction_id", rows[0])
        self.assertNotIn("account_id", rows[0])
        self.assertNotIn("customer_id", rows[0])
        self.assertNotIn("reference_id", rows[0])
        self.assertEqual(_safe_transaction_rows([{"transaction_id": 1}]), [])

    def test_load_transactions_is_authenticated_api_cached_and_customer_scoped(self):
        rows = load_transaction_risk_transactions(self.api, self.state)
        self.assertEqual(rows[0]["transaction_ref"], TRANSACTION_REF)
        load_transaction_risk_transactions(self.api, self.state)
        self.assertEqual(self.api.calls.count(("transactions", 100)), 1)
        self.state["customer"] = {"customer_key": "demo-priya"}
        load_transaction_risk_transactions(self.api, self.state)
        self.assertEqual(self.api.calls.count(("transactions", 100)), 2)

    def test_assessment_requires_a_listed_opaque_reference(self):
        load_transaction_risk_transactions(self.api, self.state)
        result = assess_selected_transaction(self.api, self.state, TRANSACTION_REF)
        self.assertEqual(result["assessment"]["risk_level"], "HIGH")
        self.assertEqual(self.api.calls[-1], ("assess", TRANSACTION_REF))
        with self.assertRaises(ValueError):
            assess_selected_transaction(self.api, self.state, "internal-id-31")

    def test_history_pagination_and_public_detail(self):
        items = load_transaction_risk_history(self.api, self.state)
        self.assertEqual(items[0]["assessment_id"], ASSESSMENT_ID)
        load_transaction_risk_history(self.api, self.state, append=True)
        self.assertEqual(self.api.calls[-1], ("history", 10, "cursor-2"))
        detail = open_transaction_risk_assessment(self.api, self.state, ASSESSMENT_ID)
        self.assertEqual(detail["assessment"]["risk_level"], "HIGH")
        self.assertEqual(self.api.calls[-1], ("detail", ASSESSMENT_ID))
        with self.assertRaises(ValueError):
            open_transaction_risk_assessment(self.api, self.state, "not-a-public-id")

    def test_transaction_list_selection_calls_assessment_and_renders_result(self):
        ui = FakeUI(click="transaction-risk-assess")
        _render_transaction_risk(ui, self.api, self.state)
        self.assertEqual(self.api.calls[1], ("assess", TRANSACTION_REF))
        self.assertIn(("options", (TRANSACTION_REF,)), ui.rendered)
        self.assertTrue(any("Transaction risk assessment" in str(item) for item in ui.rendered))
        self.assertNotIn("acct-derived-secret", repr(ui.rendered))

    def test_transaction_selection_pagination_advances_page(self):
        self.api.transactions = [
            {
                "transaction_ref": "TR-" + format(index, "048X"),
                "transaction_date": "2026-11-04", "merchant": f"Store {index}",
                "amount": float(index), "transaction_type": "DEBIT", "status": "COMPLETED",
            }
            for index in range(15)
        ]
        ui = FakeUI(click="transaction-risk-next")
        with self.assertRaisesRegex(RuntimeError, "rerun"):
            _render_transaction_risk(ui, self.api, self.state)
        self.assertEqual(self.state["transaction_risk_tx_page"], 1)

    def test_history_card_opens_detail_and_renders_backend_assessment(self):
        ui = FakeUI(click=f"transaction-risk-open-{ASSESSMENT_ID}")
        _render_transaction_risk(ui, self.api, self.state)
        self.assertIn(("detail", ASSESSMENT_ID), self.api.calls)
        self.assertEqual(self.state["transaction_risk_detail_id"], ASSESSMENT_ID)
        self.assertIn("Transaction risk assessment", repr(ui.rendered))

    def test_empty_and_error_states_render_without_leaking_backend_details(self):
        class EmptyAPI(FakeAPI):
            def get_my_transactions(self, limit):
                return []

            def list_transaction_risk_assessments(self, limit, cursor=None):
                return {"items": [], "next_cursor": None, "has_more": False}

        ui = FakeUI()
        _render_transaction_risk(ui, EmptyAPI(), self.state)
        self.assertIn("No assessable transactions", repr(ui.rendered))
        self.assertIn("No transaction-risk assessments", repr(ui.rendered))

        class FailedAPI(EmptyAPI):
            def get_my_transactions(self, limit):
                raise APIError(503, "private backend detail")

        state = {"customer": {"customer_key": "demo-arjun"}}
        failed_ui = FakeUI()
        _render_transaction_risk(failed_ui, FailedAPI(), state)
        self.assertIn("Transactions are temporarily unavailable", repr(failed_ui.rendered))
        self.assertNotIn("private backend detail", repr(failed_ui.rendered))

    def test_risk_report_renders_all_levels_and_backend_fields(self):
        for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"):
            with self.subTest(level=level):
                ui = FakeUI()
                render_transaction_risk_assessment(ui, assessment(level), self.api.transactions[0])
                rendered = repr(ui.rendered)
                self.assertIn(level, rendered)
                self.assertIn("not a fraud probability", rendered)
                self.assertIn("unusual_amount", rendered)
                self.assertIn("Amount exceeded the historical baseline.", rendered)
                self.assertIn("MODERATE", rendered)
                self.assertIn("ADEQUATE", rendered)
                self.assertIn("Verify the transaction", rendered)

    def test_handoff_opens_assistant_in_fresh_conversation_with_public_assessment_id(self):
        conversation = begin_transaction_risk_discussion(self.api, self.state, ASSESSMENT_ID)
        self.assertEqual(conversation["conversation_id"], "new-conversation")
        self.assertTrue(self.state["assistant_open"])
        self.assertEqual(self.state["transaction_risk_follow_up_assessment_id"], ASSESSMENT_ID)
        self.assertEqual(self.state["transaction_risk_follow_up_conversation_id"], "new-conversation")


if __name__ == "__main__":
    unittest.main()
