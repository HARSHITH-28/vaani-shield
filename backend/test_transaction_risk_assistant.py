"""Offline tool and orchestration coverage for Vaani AI Transaction Risk."""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend import main
from backend.api_models import AssistantTurnRequest
from services import database
from services.agent_tools import TOOL_DEFINITIONS, execute_tool
from services.assistant_service import (
    AssistantOrchestrator,
    LLM_TOOL_NAMES,
    _llm_tool_catalog,
    _required_tools_for_question,
    _validate_llm_arguments,
)
from services.llm_interfaces import LLMResponse, LLMToolCall
from services.principal import CustomerPrincipal
from services.transaction_risk_persistence import (
    TransactionRiskAssessmentNotFound,
    TransactionRiskTransactionNotFound,
    create_transaction_assessment,
    ensure_transaction_public_refs,
)


BASE = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


def turn(text):
    return AssistantTurnRequest(
        client_turn_id=uuid4(), input_mode="text", output_mode="text", text=text
    )


class LatestTransactionProvider:
    provider_name = "mock"
    model_name = "offline-test"

    def __init__(self):
        self.required_calls = []

    def generate(self, messages, tools, *, response_language, required_tools=frozenset(), request_stage="unspecified"):
        self.required_calls.append(required_tools)
        if not required_tools:
            return LLMResponse(text="FRAUD CONFIRMED. Risk score 99%.")
        if "get_recent_transactions" in required_tools:
            return LLMResponse(tool_calls=(LLMToolCall("get_recent_transactions", {"limit": 1}, "recent-1"),))
        user_text = " ".join(message.content for message in messages if message.role == "user")
        explicit = re.search(r"TR-[A-F0-9]{48}", user_text)
        if explicit:
            reference = explicit.group(0)
        else:
            recent_tool = next(
                message for message in reversed(messages)
                if message.role == "tool" and message.tool_name == "get_recent_transactions"
            )
            content = json.loads(recent_tool.content)
            transaction_rows = content["result"]["result"]
            reference = transaction_rows[0]["transaction_ref"]
        return LLMResponse(tool_calls=(LLMToolCall(
            "assess_transaction_risk", {"transaction_ref": reference}, "risk-1"
        ),))


class TransactionRiskAssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "risk-assistant.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        self._seed()
        self.principal = principal()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _seed():
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers(customer_id, customer_key, name, email) VALUES (?, ?, ?, ?)",
                [(1, "demo-arjun", "Arjun", "arjun@example.test"),
                 (2, "demo-priya", "Priya", "priya@example.test")],
            )
            conn.executemany(
                "INSERT INTO accounts(account_id, customer_id, account_number, account_type) VALUES (?, ?, ?, ?)",
                [(10, 1, "1234567890123456", "SAVINGS"), (20, 2, "9999999999999999", "SAVINGS")],
            )
            for index in range(12):
                days_ago = max(1, 30 - (index * 30 // 12))
                conn.execute(
                    """INSERT INTO transactions(account_id, transaction_type, amount, merchant,
                       category, status, reference_id, transaction_date, description)
                       VALUES (10, 'DEBIT', 100, 'Familiar Market', 'SHOPPING', 'SUCCESS', ?, ?, ?)""",
                    (f"legacy-ref-{index}", (BASE - timedelta(days=days_ago)).isoformat(), "safe history"),
                )
            conn.execute(
                """INSERT INTO transactions(transaction_id, account_id, transaction_type,
                   amount, merchant, category, status, reference_id, transaction_date, description)
                   VALUES (100, 10, 'DEBIT', 120, 'Familiar Market', 'SHOPPING', 'SUCCESS',
                           'legacy-account-derived-ref', ?, 'description not sent as risk evidence')""",
                (BASE.isoformat(),),
            )
            conn.execute(
                """INSERT INTO transactions(transaction_id, account_id, transaction_type,
                   amount, merchant, category, status, reference_id, transaction_date)
                   VALUES (500, 20, 'DEBIT', 80, 'Other Shop', 'SHOPPING', 'SUCCESS',
                           'other-customer-ref', ?)""",
                (BASE.isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

    def test_tools_are_registered_allowlisted_and_schema_validated(self):
        names = {tool["name"] for tool in TOOL_DEFINITIONS}
        risk_tools = {
            "assess_transaction_risk", "list_transaction_risk_assessments",
            "get_transaction_risk_assessment",
        }
        self.assertTrue(risk_tools <= names)
        self.assertTrue(risk_tools <= LLM_TOOL_NAMES)
        self.assertTrue(risk_tools <= {tool.name for tool in _llm_tool_catalog()})
        self.assertTrue(_validate_llm_arguments("assess_transaction_risk", {"transaction_ref": "TR-" + "A" * 48}))
        self.assertFalse(_validate_llm_arguments("assess_transaction_risk", {"transaction_ref": "TR-short", "customer_id": 1}))
        self.assertTrue({"assess_transaction_risk", "get_recent_transactions"} <= _required_tools_for_question(
            "Check my latest transaction for suspicious activity."
        ))
        self.assertEqual(_required_tools_for_question("Show my recent transaction risk assessments"), frozenset({"list_transaction_risk_assessments"}))
        self.assertTrue({"list_transaction_risk_assessments", "get_transaction_risk_assessment"} <= _required_tools_for_question(
            "Why was this transaction marked HIGH?"
        ))

    def test_existing_transaction_tool_returns_only_opaque_references_to_model(self):
        rows = execute_tool("get_recent_transactions", self.principal, {"limit": 1})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["transaction_ref"].startswith("TR-"))
        for forbidden in ("transaction_id", "account_id", "customer_id", "reference_id"):
            self.assertNotIn(forbidden, rows[0])
        self.assertNotIn("legacy-account-derived-ref", json.dumps(rows))

    def test_assess_invocation_uses_principal_and_preserves_deterministic_fields(self):
        reference = ensure_transaction_public_refs("demo-arjun", [100])[100]
        result = execute_tool("assess_transaction_risk", self.principal, {"transaction_ref": reference})
        self.assertEqual(result["transaction"]["transaction_ref"], reference)
        self.assertEqual(result["assessment"]["risk_level"], "LOW")
        self.assertIn("risk_score", result["assessment"])
        serialized = json.dumps(result)
        for forbidden in ("transaction_id", "account_id", "customer_id", "reference_id"):
            self.assertNotIn(forbidden, serialized)

    def test_unknown_or_other_customer_transaction_reference_is_rejected(self):
        missing = "TR-" + "A" * 48
        with self.assertRaises(TransactionRiskTransactionNotFound):
            execute_tool("assess_transaction_risk", self.principal, {"transaction_ref": missing})
        other_ref = ensure_transaction_public_refs("demo-priya", [500])[500]
        with self.assertRaises(TransactionRiskTransactionNotFound):
            execute_tool("assess_transaction_risk", self.principal, {"transaction_ref": other_ref})

    def test_missing_reference_and_unapproved_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            execute_tool("assess_transaction_risk", self.principal, {})
        with self.assertRaises(ValueError):
            execute_tool("assess_transaction_risk", self.principal, {
                "transaction_ref": "TR-" + "A" * 48, "customer_id": 2,
            })

    def test_assessment_history_and_detail_are_owner_scoped(self):
        reference = ensure_transaction_public_refs("demo-arjun", [100])[100]
        created = create_transaction_assessment(self.principal, reference)
        listing = execute_tool("list_transaction_risk_assessments", self.principal, {"limit": 10})
        self.assertEqual(listing["items"][0]["assessment_id"], created["assessment_id"])
        detail = execute_tool("get_transaction_risk_assessment", self.principal, {
            "assessment_id": created["assessment_id"],
        })
        self.assertEqual(detail["assessment"], created["assessment"])
        with self.assertRaises(TransactionRiskAssessmentNotFound):
            execute_tool("get_transaction_risk_assessment", principal("demo-priya"), {
                "assessment_id": created["assessment_id"],
            })

    def test_latest_transaction_is_retrieved_then_assessed_and_llm_cannot_rewrite_risk(self):
        provider = LatestTransactionProvider()
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        response = orchestrator.process_turn(
            self.principal,
            turn("Check my latest transaction for suspicious activity."),
        )
        self.assertEqual(len(provider.required_calls), 3)
        self.assertIn("get_recent_transactions", provider.required_calls[0])
        self.assertIn("assess_transaction_risk", provider.required_calls[1])
        self.assertEqual(response.transaction_risk_assessment.assessment.risk_level, "LOW")
        self.assertNotIn("FRAUD CONFIRMED", response.assistant_text)
        self.assertNotIn("99%", response.assistant_text)
        self.assertIn("not proof", response.assistant_text.casefold())
        assessment_tool = next(item for item in response.tool_results if item.tool_name == "assess_transaction_risk")
        self.assertEqual(
            assessment_tool.data["assessment"]["risk_score"],
            response.transaction_risk_assessment.assessment.risk_score,
        )

    def test_specific_transaction_ref_must_be_user_provided_or_retrieved(self):
        provider = LatestTransactionProvider()
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        # A model cannot submit a guessed reference before an owner-scoped read.
        # This intentionally supplies an explicit ref in the user text only for
        # the valid path; persistence still verifies ownership server-side.
        reference = ensure_transaction_public_refs("demo-arjun", [100])[100]
        response = orchestrator.process_turn(
            self.principal,
            turn(f"Assess transaction fraud risk for {reference}"),
        )
        self.assertIsNotNone(response.transaction_risk_assessment)
        self.assertEqual(response.transaction_risk_assessment.transaction_ref, reference)

    def test_requested_hindi_labels_are_preserved_and_english_engine_text_is_disclosed(self):
        provider = LatestTransactionProvider()
        orchestrator = AssistantOrchestrator(llm_provider=provider)
        reference = ensure_transaction_public_refs("demo-arjun", [100])[100]
        response = orchestrator.process_turn(
            self.principal,
            turn(f"Assess transaction fraud risk for {reference}").model_copy(
                update={"response_language": "hi-IN"}
            ),
        )
        self.assertIn("लेनदेन पैटर्न जाँच का परिणाम", response.assistant_text)
        self.assertIn("इंजन की व्याख्या और सुझाव अंग्रेज़ी में हैं", response.assistant_text)
        self.assertEqual(response.response_language, "en-IN")


if __name__ == "__main__":
    unittest.main()
