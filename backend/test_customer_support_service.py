"""Offline Customer Support persistence tests using disposable SQLite files."""

import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from services import database
from services.conversation_service import create_conversation
from services.customer_support_service import (
    InvalidSupportCaseTransition,
    SupportCaseError,
    SupportCaseNotFoundError,
    close_support_case,
    create_support_case,
    get_support_case,
    get_support_case_events,
    list_support_cases,
)
from services.principal import CustomerPrincipal


@contextmanager
def test_connection():
    conn = database.get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def make_principal(customer_key):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30)
    )


class CustomerSupportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp_dir.name) / "support.sqlite3"
        )
        self.db_patch.start()
        database.initialize_database()
        with test_connection() as conn:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.com", "Synthetic Customer A"),
                    ("demo-priya", "priya@example.com", "Synthetic Customer B"),
                ],
            )
        self.arjun = make_principal("demo-arjun")
        self.priya = make_principal("demo-priya")

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def create(self, principal=None, **overrides):
        values = {
            "category": "digital_banking",
            "priority": "MEDIUM",
            "subject": "Cannot sign in",
            "description": "The synthetic demo sign-in screen did not accept my attempt.",
        }
        values.update(overrides)
        return create_support_case(principal or self.arjun, **values)

    def test_creation_sets_synthetic_fields_and_hides_internal_ids(self):
        case = self.create()
        self.assertRegex(case["ticket_ref"], r"^VS-[A-F0-9]{32}$")
        self.assertEqual(case["status"], "OPEN")
        self.assertEqual(case["origin"], "customer_form")
        self.assertIs(case["is_synthetic_demo"], True)
        self.assertNotIn("case_id", case)
        self.assertNotIn("customer_id", case)

    def test_public_ticket_references_are_unique(self):
        cases = [self.create(subject=f"Issue {index}") for index in range(8)]
        references = [case["ticket_ref"] for case in cases]
        self.assertEqual(len(references), len(set(references)))

    def test_collision_retries_with_a_new_random_reference(self):
        existing = self.create()
        fresh = "VS-" + "A" * 32
        with patch(
            "services.customer_support_service._new_ticket_ref",
            side_effect=[existing["ticket_ref"], fresh],
        ):
            created = self.create(subject="Collision retry")
        self.assertEqual(created["ticket_ref"], fresh)

    def test_listing_and_retrieval_are_customer_scoped(self):
        arjun_case = self.create()
        priya_case = self.create(self.priya, subject="Another demo issue")
        arjun_list = list_support_cases(self.arjun)
        self.assertEqual([item["ticket_ref"] for item in arjun_list["items"]], [arjun_case["ticket_ref"]])
        self.assertEqual(get_support_case(self.arjun, arjun_case["ticket_ref"])["ticket_ref"], arjun_case["ticket_ref"])
        with self.assertRaises(SupportCaseNotFoundError) as inaccessible:
            get_support_case(self.arjun, priya_case["ticket_ref"])
        with self.assertRaises(SupportCaseNotFoundError) as unknown:
            get_support_case(self.arjun, "VS-" + "F" * 32)
        self.assertEqual(str(inaccessible.exception), str(unknown.exception))

    def test_case_listing_filters_and_paginates(self):
        first = self.create(subject="First")
        self.create(category="loan", priority="HIGH", subject="Second")
        page = list_support_cases(self.arjun, limit=1, category="digital_banking")
        self.assertEqual([item["ticket_ref"] for item in page["items"]], [first["ticket_ref"]])
        self.assertFalse(page["has_more"])
        first_page = list_support_cases(self.arjun, limit=1)
        self.assertTrue(first_page["has_more"])
        second_page = list_support_cases(self.arjun, limit=1, cursor=first_page["next_cursor"])
        self.assertNotEqual(first_page["items"][0]["ticket_ref"], second_page["items"][0]["ticket_ref"])

    def test_close_is_owner_scoped_and_records_append_only_history(self):
        case = self.create()
        closed = close_support_case(self.arjun, case["ticket_ref"])
        self.assertEqual(closed["status"], "CLOSED")
        self.assertGreaterEqual(closed["updated_at"], case["updated_at"])
        events = get_support_case_events(self.arjun, case["ticket_ref"])
        self.assertEqual([event["to_status"] for event in events], ["OPEN", "CLOSED"])
        self.assertEqual(events[-1]["from_status"], "OPEN")
        self.assertEqual(events[-1]["actor_type"], "customer")
        self.assertNotIn("case_id", events[-1])
        self.assertNotIn("event_id", events[-1])
        with self.assertRaises(InvalidSupportCaseTransition):
            close_support_case(self.arjun, case["ticket_ref"])
        with self.assertRaises(SupportCaseNotFoundError):
            close_support_case(self.priya, case["ticket_ref"])

    def test_customer_can_close_eligible_nonclosed_statuses_but_not_set_staff_status(self):
        for status in ("IN_PROGRESS", "WAITING_FOR_CUSTOMER", "RESOLVED"):
            case = self.create(subject=f"Close {status}")
            with test_connection() as conn:
                conn.execute(
                    "UPDATE support_cases SET status = ? WHERE ticket_ref = ?",
                    (status, case["ticket_ref"]),
                )
            result = close_support_case(self.arjun, case["ticket_ref"])
            self.assertEqual(result["status"], "CLOSED")
        with test_connection() as conn:
            stored = conn.execute(
                "SELECT status FROM support_cases WHERE ticket_ref = ?",
                (case["ticket_ref"],),
            ).fetchone()["status"]
        self.assertEqual(stored, "CLOSED")

    def test_validation_rejects_unknown_categories_priorities_and_bad_lengths(self):
        with self.assertRaises(SupportCaseError):
            self.create(category="arbitrary")
        with self.assertRaises(SupportCaseError):
            self.create(priority="CRITICAL")
        with self.assertRaises(SupportCaseError):
            self.create(subject="   ")
        with self.assertRaises(SupportCaseError):
            self.create(description="x" * 4001)
        with self.assertRaises(SupportCaseError):
            self.create(_origin="client_supplied")

    def test_conversation_link_must_belong_to_same_customer(self):
        conversation = create_conversation(self.arjun)
        case = self.create(conversation_id=conversation["conversation_id"])
        self.assertEqual(case["conversation_id"], conversation["conversation_id"])
        another = create_conversation(self.priya)
        with self.assertRaises(SupportCaseNotFoundError):
            self.create(conversation_id=another["conversation_id"])


class SupportCaseMigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_legacy_row_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "legacy.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE customers (
                    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    phone TEXT,
                    monthly_income REAL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO customers (customer_id, name, email)
                VALUES (41, 'Synthetic Legacy', 'arjun@example.com');
                CREATE TABLE support_cases (
                    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER NOT NULL,
                    issue_type TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'OPEN',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
                );
                INSERT INTO support_cases
                    (case_id, customer_id, issue_type, description, status, created_at)
                VALUES (77, 41, 'Legacy category label', 'Synthetic legacy description', 'open', '2025-01-02T03:04:05+00:00');
            """)
            conn.close()

            with patch.object(database, "DB_PATH", db_path):
                database.initialize_database()
                with test_connection() as migrated:
                    row = migrated.execute(
                        "SELECT * FROM support_cases WHERE case_id = 77"
                    ).fetchone()
                    ref = row["ticket_ref"]
                    self.assertEqual(row["case_id"], 77)
                    self.assertEqual(row["customer_id"], 41)
                    self.assertEqual(row["issue_type"], "Legacy category label")
                    self.assertEqual(row["description"], "Synthetic legacy description")
                    self.assertEqual(row["category"], "other")
                    self.assertEqual(row["status"], "OPEN")
                    self.assertRegex(ref, r"^VS-[A-F0-9]{32}$")
                    self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(
                        migrated.execute(
                            "SELECT COUNT(*) FROM support_case_events WHERE case_id = 77"
                        ).fetchone()[0],
                        1,
                    )

                database.initialize_database()
                with test_connection() as migrated:
                    row = migrated.execute(
                        "SELECT case_id, issue_type, description, ticket_ref FROM support_cases WHERE case_id = 77"
                    ).fetchone()
                    self.assertEqual(row["ticket_ref"], ref)
                    self.assertEqual(row["case_id"], 77)
                    self.assertEqual(row["issue_type"], "Legacy category label")
                    self.assertEqual(row["description"], "Synthetic legacy description")
                    self.assertEqual(
                        migrated.execute(
                            "SELECT COUNT(*) FROM support_case_events WHERE case_id = 77"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        migrated.execute(
                            "SELECT COUNT(*) FROM pragma_index_list('support_cases') WHERE name='idx_support_cases_ticket_ref'"
                        ).fetchone()[0],
                        1,
                    )


if __name__ == "__main__":
    unittest.main()
