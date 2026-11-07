"""Isolated SQLite tests for investigation persistence and confirmation."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from backend.api_models import InvestigationConfirmationRequest
from services import database
from services.conversation_service import create_conversation, save_user_turn
from services.investigation_service import (
    InvestigationConflict,
    InvestigationConfirmationConflict,
    InvestigationConfirmationNotFound,
    InvestigationEvidenceNotFound,
    InvestigationError,
    InvestigationNotFound,
    add_investigation_note,
    cleanup_expired_investigation_actions,
    close_investigation,
    confirm_investigation_action,
    create_investigation,
    create_pending_investigation_action,
    get_investigation,
    link_investigation_evidence,
    list_investigations,
    update_investigation_status,
)
from services.principal import CustomerPrincipal
from services.scam_assessment_service import create_assessment
from services.transaction_risk_persistence import ensure_transaction_public_refs


def principal(key="demo-arjun", session_id=None):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, session_id or uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


class InvestigationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "investigations.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers(customer_id, customer_key, name, email) VALUES (?, ?, ?, ?)",
                [(1, "demo-arjun", "Arjun Demo", "arjun@example.test"),
                 (2, "demo-priya", "Priya Demo", "priya@example.test")],
            )
            conn.executemany(
                "INSERT INTO accounts(account_id, customer_id, account_number, account_type) VALUES (?, ?, ?, ?)",
                [(10, 1, "1111222233334444", "SAVINGS"), (20, 2, "5555666677778888", "SAVINGS")],
            )
            conn.execute(
                "INSERT INTO transactions(transaction_id, account_id, transaction_type, amount, merchant, category, status, reference_id, transaction_date) "
                "VALUES (100, 10, 'DEBIT', 250, 'Demo Market', 'SHOPPING', 'SUCCESS', 'legacy-ref', '2026-10-30T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO transactions(transaction_id, account_id, transaction_type, amount, merchant, category, status, reference_id, transaction_date) "
                "VALUES (200, 20, 'DEBIT', 100, 'Other Market', 'SHOPPING', 'SUCCESS', 'other-ref', '2026-10-30T00:00:00+00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        self.arjun = principal()
        self.priya = principal("demo-priya")
        self.message = create_assessment(
            self.arjun, "A caller requested my OTP to prevent account blocking.", "en-IN"
        )
        self.call = create_assessment(
            self.arjun, "Caller requested remote access to my banking app.", "en-IN",
            _source_mode="voice_call",
        )
        self.tx_assessment_id = str(uuid4())
        tx_ref = ensure_transaction_public_refs("demo-arjun", [100])[100]
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO transaction_risk_assessments "
                "(assessment_id, customer_key, transaction_id, assessment_json, engine_version, history_fingerprint, created_at) "
                "VALUES (?, 'demo-arjun', 100, ?, 'test', 'fingerprint', '2026-10-30T00:00:00+00:00')",
                (self.tx_assessment_id, json.dumps({
                    "risk_level": "MEDIUM", "risk_score": 30, "evidence_strength": "LIMITED",
                    "explanation": "A synthetic transaction screening result.",
                })),
            )
            conn.commit()
        finally:
            conn.close()
        self.evidence = [
            {"evidence_type": "MESSAGE_ASSESSMENT", "assessment_id": self.message["assessment_id"]},
            {"evidence_type": "CALL_ASSESSMENT", "assessment_id": self.call["assessment_id"]},
            {"evidence_type": "TRANSACTION_RISK_ASSESSMENT", "assessment_id": self.tx_assessment_id},
        ]

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def _create(self, title="Suspicious contact", category="MESSAGE_SCAM", summary="A caller asked for an OTP and applied pressure.", evidence=None):
        return create_investigation(
            self.arjun, title=title, category=category, summary=summary,
            evidence=self.evidence if evidence is None else evidence,
        ).model_dump(mode="json")

    def _pending(self, action, principal_override=None):
        owner = principal_override or self.arjun
        conversation = create_conversation(owner)
        turn = save_user_turn(
            owner, conversation["conversation_id"], input_type="text",
            text_or_transcript="Please update my investigation.", input_language="en-IN",
            requested_output_mode="text",
        )
        pending = create_pending_investigation_action(
            owner, conversation["conversation_id"], turn["turn_id"], action,
        )
        request = InvestigationConfirmationRequest(
            confirmed=True, confirmation_method="button",
            conversation_id=conversation["conversation_id"],
            original_turn_id=turn["turn_id"], action_type=action["action_type"],
        )
        return pending, request

    def test_create_links_message_call_and_transaction_evidence_without_copying_records(self):
        detail = self._create()
        self.assertRegex(detail["investigation_ref"], r"^INV-[A-F0-9]{32}$")
        self.assertEqual(detail["status"], "OPEN")
        self.assertTrue(detail["is_synthetic_demo"])
        self.assertEqual(detail["evidence_count"], 3)
        self.assertEqual({item["evidence_type"] for item in detail["evidence"]}, {
            "MESSAGE_ASSESSMENT", "CALL_ASSESSMENT", "TRANSACTION_RISK_ASSESSMENT",
        })
        serialized = json.dumps(detail)
        for internal in ("investigation_id", "evidence_id", "customer_key", "transaction_id", "reference_id"):
            self.assertNotIn(internal, serialized)
        conn = database.get_connection()
        try:
            stored = conn.execute("SELECT * FROM investigations").fetchone()
            links = conn.execute("SELECT * FROM investigation_evidence").fetchall()
            self.assertEqual(len(links), 3)
            self.assertNotIn("123456", stored["summary"])
            self.assertNotIn("my OTP", conn.execute("SELECT metadata_json FROM investigation_events WHERE event_type='CREATED'").fetchone()[0])
        finally:
            conn.close()

    def test_owner_scoped_detail_and_equivalent_not_found(self):
        detail = self._create()
        with self.assertRaises(InvestigationNotFound):
            get_investigation(self.priya, detail["investigation_ref"])
        with self.assertRaises(InvestigationNotFound):
            get_investigation(self.arjun, "INV-" + "F" * 32)
        self.assertEqual(get_investigation(self.arjun, detail["investigation_ref"])["investigation_ref"], detail["investigation_ref"])

    def test_cross_customer_and_unknown_assessments_are_rejected(self):
        with self.assertRaises(InvestigationEvidenceNotFound):
            create_investigation(
                self.priya, title="Other customer's report", category="OTHER",
                summary="This is a synthetic security report.", evidence=self.evidence[:1],
            )
        with self.assertRaises(InvestigationEvidenceNotFound):
            create_investigation(
                self.arjun, title="Unknown assessment", category="OTHER",
                summary="This is a synthetic security report.",
                evidence=[{"evidence_type": "MESSAGE_ASSESSMENT", "assessment_id": str(uuid4())}],
            )

    def test_duplicate_evidence_is_rejected(self):
        with self.assertRaises(InvestigationError):
            create_investigation(
                self.arjun, title="Duplicate evidence", category="OTHER",
                summary="This is a synthetic security report.",
                evidence=[self.evidence[0], self.evidence[0]],
            )
        detail = self._create(evidence=[self.evidence[0]])
        with self.assertRaises(InvestigationConflict):
            link_investigation_evidence(self.arjun, detail["investigation_ref"], self.evidence[0])

    def test_add_note_redacts_credentials_and_is_labeled_unverified(self):
        detail = self._create(evidence=[])
        updated = add_investigation_note(
            self.arjun, detail["investigation_ref"], "The caller asked for OTP 123456 yesterday."
        )
        event = updated["events"][-1]
        self.assertEqual(event["event_type"], "INFORMATION_ADDED")
        self.assertIn("[REDACTED]", event["note"])
        self.assertNotIn("123456", event["note"])
        self.assertIn("unverified", event["summary"])

    def test_transitions_close_and_history_are_preserved(self):
        detail = self._create(evidence=[])
        ref = detail["investigation_ref"]
        reviewed = update_investigation_status(self.arjun, ref, "UNDER_REVIEW")
        waiting = update_investigation_status(self.arjun, ref, "WAITING_FOR_INFORMATION")
        resolved = update_investigation_status(self.arjun, ref, "RESOLVED")
        closed = close_investigation(self.arjun, ref)
        self.assertEqual(reviewed["status"], "UNDER_REVIEW")
        self.assertEqual(waiting["status"], "WAITING_FOR_INFORMATION")
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertIsNotNone(closed["resolved_at"])
        self.assertEqual(len(closed["events"]), 5)
        with self.assertRaises(InvestigationConflict):
            update_investigation_status(self.arjun, ref, "UNDER_REVIEW")
        with self.assertRaises(InvestigationConflict):
            add_investigation_note(self.arjun, ref, "I have another update.")

    def test_invalid_transition_and_validation(self):
        detail = self._create(evidence=[])
        close_investigation(self.arjun, detail["investigation_ref"])
        with self.assertRaises(InvestigationConflict):
            update_investigation_status(self.arjun, detail["investigation_ref"], "UNDER_REVIEW")
        with self.assertRaises(InvestigationError):
            self._create(title="x")
        with self.assertRaises(InvestigationError):
            list_investigations(self.arjun, limit=101)
        with self.assertRaises(InvestigationError):
            list_investigations(self.arjun, cursor="%%%bad")

    def test_owner_scoped_list_filters_and_pagination(self):
        first = self._create(evidence=[])
        second = self._create(title="Another report", category="OTHER", summary="A second customer-reported security concern.", evidence=[])
        page = list_investigations(self.arjun, limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["has_more"])
        next_page = list_investigations(self.arjun, limit=1, cursor=page["next_cursor"])
        self.assertEqual(len(next_page["items"]), 1)
        filtered = list_investigations(self.arjun, status="OPEN", category="OTHER", search="Another")
        self.assertEqual(filtered["items"][0]["investigation_ref"], second["investigation_ref"])
        self.assertEqual(list_investigations(self.priya)["items"], [])

    def test_confirmation_success_decline_and_replay(self):
        action = {
            "action_type": "create_investigation", "title": "Proposed report",
            "category": "MESSAGE_SCAM", "summary": "A suspicious caller asked for OTP 123456 to block my account.",
            "evidence": [self.evidence[0]],
        }
        pending, request = self._pending(action)
        conn = database.get_connection()
        try:
            pending_payload = conn.execute(
                "SELECT action_json FROM investigation_pending_actions WHERE confirmation_id=?",
                (str(pending.confirmation_id),),
            ).fetchone()[0]
            self.assertNotIn("123456", pending_payload)
        finally:
            conn.close()
        result = confirm_investigation_action(self.arjun, pending.confirmation_id, request)
        self.assertEqual(result.status, "executed")
        self.assertTrue(result.action_result.data["investigation_ref"].startswith("INV-"))
        with self.assertRaises(InvestigationConfirmationConflict):
            confirm_investigation_action(self.arjun, pending.confirmation_id, request)

        decline_action = {**action, "title": "Declined report"}
        decline_pending, decline_request = self._pending(decline_action)
        declined = confirm_investigation_action(
            self.arjun, decline_pending.confirmation_id,
            decline_request.model_copy(update={"confirmed": False}),
        )
        self.assertEqual(declined.status, "declined")
        items = list_investigations(self.arjun)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Proposed report")
        conn = database.get_connection()
        try:
            row = conn.execute("SELECT action_json, status FROM investigation_pending_actions WHERE confirmation_id=?", (str(decline_pending.confirmation_id),)).fetchone()
            self.assertEqual(row["action_json"], "{}")
            self.assertEqual(row["status"], "declined")
        finally:
            conn.close()

    def test_pending_actions_reject_wrong_customer_session_conversation_turn_and_action(self):
        action = {"action_type": "create_investigation", "title": "Pending report", "category": "OTHER", "summary": "A customer reported a security concern."}
        pending, request = self._pending(action)
        with self.assertRaises(InvestigationConfirmationNotFound):
            confirm_investigation_action(self.priya, pending.confirmation_id, request)
        other_session = principal("demo-arjun")
        with self.assertRaises(InvestigationConfirmationNotFound):
            confirm_investigation_action(other_session, pending.confirmation_id, request)
        with self.assertRaises(InvestigationConfirmationConflict):
            confirm_investigation_action(self.arjun, pending.confirmation_id, request.model_copy(update={"conversation_id": uuid4()}))
        with self.assertRaises(InvestigationConfirmationConflict):
            confirm_investigation_action(self.arjun, pending.confirmation_id, request.model_copy(update={"original_turn_id": uuid4()}))
        with self.assertRaises(InvestigationConfirmationConflict):
            confirm_investigation_action(self.arjun, pending.confirmation_id, request.model_copy(update={"action_type": "close_investigation"}))

    def test_pending_target_change_expires_and_scrubs_action(self):
        detail = self._create(evidence=[])
        action = {
            "action_type": "add_investigation_note",
            "investigation_ref": detail["investigation_ref"],
            "note": "The customer added another observation.",
        }
        pending, request = self._pending(action)
        update_investigation_status(self.arjun, detail["investigation_ref"], "UNDER_REVIEW")
        result = confirm_investigation_action(self.arjun, pending.confirmation_id, request)
        self.assertEqual(result.status, "expired")
        stored = get_investigation(self.arjun, detail["investigation_ref"])
        self.assertEqual(stored["status"], "UNDER_REVIEW")

    def test_pending_expiry_cleanup_and_expired_confirmation(self):
        action = {"action_type": "create_investigation", "title": "Expired report", "category": "OTHER", "summary": "A customer reported a security concern."}
        pending, request = self._pending(action)
        conn = database.get_connection()
        try:
            conn.execute("UPDATE investigation_pending_actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE confirmation_id=?", (str(pending.confirmation_id),))
            conn.commit()
        finally:
            conn.close()
        cleanup_expired_investigation_actions()
        result = confirm_investigation_action(self.arjun, pending.confirmation_id, request)
        self.assertEqual(result.status, "expired")

    def test_concurrent_confirmation_executes_once(self):
        action = {"action_type": "create_investigation", "title": "Concurrent report", "category": "OTHER", "summary": "A customer reported a security concern."}
        pending, request = self._pending(action)
        import concurrent.futures

        def confirm():
            try:
                return confirm_investigation_action(self.arjun, pending.confirmation_id, request).status
            except InvestigationConfirmationConflict:
                return "replay"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: confirm(), range(2)))
        self.assertEqual(sorted(outcomes), ["executed", "replay"])
        self.assertEqual(list_investigations(self.arjun)["items"][0]["title"], "Concurrent report")

    def test_idempotent_migration_preserves_support_and_existing_assessment_records(self):
        conn = database.get_connection()
        try:
            conversation = create_conversation(self.arjun)
            turn = save_user_turn(
                self.arjun, conversation["conversation_id"], input_type="text",
                text_or_transcript="Please open a demo support case.", input_language="en-IN",
                requested_output_mode="text",
            )
            conn.execute("INSERT INTO support_cases(customer_id, issue_type, description, status, created_at) VALUES (1, 'account', 'synthetic support case', 'OPEN', '2026-01-01T00:00:00+00:00')")
            case_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO support_case_events(event_id, case_id, from_status, to_status, actor_type, occurred_at, reason_code) VALUES ('event-keep', ?, NULL, 'OPEN', 'customer', '2026-01-01T00:00:00+00:00', 'case_created')", (case_id,))
            conn.execute("INSERT INTO pending_actions(confirmation_id, customer_key, session_id, conversation_id, original_turn_id, action_type, action_json, status, created_at, expires_at) VALUES ('pending-keep', 'demo-arjun', ?, ?, ?, 'create_support_ticket', '{}', 'pending', ?, ?)", (str(self.arjun.session_id), conversation["conversation_id"], turn["turn_id"], "2026-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"))
            conn.commit()
        finally:
            conn.close()
        database.initialize_database()
        database.initialize_database()
        conn = database.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM support_case_events WHERE event_id='event-keep'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_actions WHERE confirmation_id='pending-keep'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scam_assessments").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table in ("investigations", "investigation_evidence", "investigation_events", "investigation_pending_actions"):
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
