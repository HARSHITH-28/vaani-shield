"""Offline tests for the authenticated Investigations page helpers."""

import unittest

from frontend.pages.scam_detection import (
    _available_investigation_evidence, load_investigations, open_investigation,
)
from frontend.session_state import clear_investigation_cache


REF = "INV-" + "A" * 32


class FakeAPI:
    def __init__(self):
        self.calls = []

    def list_investigations(self, limit, cursor=None, **filters):
        self.calls.append(("list", limit, cursor, filters))
        return {"items": [{"investigation_ref": REF, "title": "Demo report"}], "next_cursor": "next", "has_more": True}

    def get_investigation(self, ref):
        self.calls.append(("detail", ref))
        return {"investigation_ref": ref, "status": "OPEN", "events": []}


class InvestigationPageTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.state = {"customer": {"customer_key": "demo-arjun"}}

    def test_list_and_detail_use_only_opaque_owner_scoped_reference(self):
        self.assertEqual(load_investigations(self.api, self.state)[0]["investigation_ref"], REF)
        result = open_investigation(self.api, self.state, REF)
        self.assertEqual(result["investigation_ref"], REF)
        self.assertEqual(self.api.calls, [("list", 10, None, {"status": None, "category": None, "search": None}), ("detail", REF)])
        with self.assertRaises(ValueError):
            open_investigation(self.api, self.state, "1")
        with self.assertRaises(ValueError):
            open_investigation(self.api, self.state, "INV-" + "B" * 32)

    def test_cache_is_customer_scoped_and_resettable(self):
        load_investigations(self.api, self.state)
        self.state["investigation_detail"] = {"investigation_ref": REF}
        clear_investigation_cache(self.state)
        self.assertIsNone(self.state["investigation_items"])
        self.assertIsNone(self.state["investigation_detail"])
        self.assertEqual(self.state["investigation_action_outcomes"], {})

    def test_evidence_picker_uses_only_existing_public_assessment_references(self):
        choices = _available_investigation_evidence({
            "latest_scam_assessment": {"assessment_id": "message-uuid", "source_type": "typed_message", "risk_level": "HIGH"},
            "transaction_risk_history_items": [{"assessment_id": "transaction-uuid", "risk_level": "MEDIUM"}],
        })
        self.assertEqual(len(choices), 2)
        refs = [item["reference"] for item in choices.values()]
        self.assertIn({"evidence_type": "MESSAGE_ASSESSMENT", "assessment_id": "message-uuid"}, refs)
        self.assertIn({"evidence_type": "TRANSACTION_RISK_ASSESSMENT", "assessment_id": "transaction-uuid"}, refs)


if __name__ == "__main__":
    unittest.main()
