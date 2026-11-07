"""Owner-scoped persistence for synthetic VaaniShield support cases.

This module deliberately contains no route, LLM, or staff-workflow logic.
Customer identity is resolved from the authenticated principal on the server.
"""

import base64
import binascii
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from services import database
from services.banking_service import get_customer_profile_by_key
from services.conversation_service import (
    ConversationNotFoundError,
    get_conversation,
)
from services.principal import CustomerPrincipal


CATEGORIES = frozenset({
    "account", "card", "transaction", "digital_banking", "loan", "deposit", "other",
})
PRIORITIES = frozenset({"LOW", "MEDIUM", "HIGH", "URGENT"})
STATUSES = frozenset({
    "OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER", "RESOLVED", "CLOSED",
})
CUSTOMER_CLOSE_ELIGIBLE = frozenset({
    "OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER", "RESOLVED",
})
LANGUAGES = frozenset({"kn-IN", "en-IN", "hi-IN"})
ORIGINS = frozenset({"customer_form", "assistant_confirmed"})
_TICKET_REF = re.compile(r"^VS-[A-F0-9]{32}$")


class SupportCaseError(ValueError):
    """Base class for safe, expected support-service errors."""


class SupportCaseNotFoundError(LookupError):
    """A ticket is unknown or not owned by the authenticated customer."""


class InvalidSupportCaseTransition(SupportCaseError):
    """The requested customer transition is not permitted."""


class InvalidSupportCursor(SupportCaseError):
    """The support list cursor is malformed."""


@contextmanager
def _transaction():
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _read_connection():
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _customer_id(principal: CustomerPrincipal) -> int:
    if (
        not isinstance(principal, CustomerPrincipal)
        or principal.auth_method != "demo_cookie"
        or principal.expires_at <= datetime.now(timezone.utc)
        or principal.customer_key not in frozenset(database.DEMO_CUSTOMER_KEYS.values())
    ):
        raise PermissionError("An authenticated customer principal is required.")
    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise PermissionError("The authenticated customer is unavailable.")
    return int(profile["customer_id"])


def _validate_text(name: str, value, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise SupportCaseError(f"{name} must be text.")
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise SupportCaseError(f"{name} must contain between {minimum} and {maximum} characters.")
    return normalized


def _validate_category(category: str) -> str:
    if not isinstance(category, str) or category not in CATEGORIES:
        raise SupportCaseError("Unsupported support category.")
    return category


def _validate_priority(priority: str) -> str:
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise SupportCaseError("Unsupported support priority.")
    return priority


def _validate_language(language):
    if language is not None and (not isinstance(language, str) or language not in LANGUAGES):
        raise SupportCaseError("Unsupported support language.")
    return language


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _public_timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _new_ticket_ref() -> str:
    return "VS-" + secrets.token_hex(16).upper()


def _ticket_projection(row, *, detail: bool):
    if row is None:
        return None
    fields = (
        "ticket_ref", "category", "subject", "priority", "status",
        "created_at", "updated_at", "origin", "is_synthetic_demo",
        "conversation_id", "language",
    )
    result = {field: row[field] for field in fields}
    result["created_at"] = _public_timestamp(result["created_at"])
    result["updated_at"] = _public_timestamp(result["updated_at"])
    if detail:
        result["description"] = row["description"]
    result["is_synthetic_demo"] = bool(result["is_synthetic_demo"])
    return result


def _assert_conversation_owner(principal, conversation_id, conn=None):
    if conversation_id is None:
        return
    if conn is None:
        try:
            get_conversation(principal, conversation_id)
        except (ConversationNotFoundError, ValueError):
            raise SupportCaseNotFoundError("Conversation not found.") from None
        return
    row = conn.execute(
        "SELECT 1 FROM conversations WHERE conversation_id = ? AND customer_key = ?",
        (str(conversation_id), principal.customer_key),
    ).fetchone()
    if row is None:
        raise SupportCaseNotFoundError("Conversation not found.")


def _insert_support_case(
    conn,
    customer_id,
    *,
    category,
    priority,
    subject,
    description,
    conversation_id,
    language,
    origin,
):
    created_at = _now()
    for _ in range(5):
        ticket_ref = _new_ticket_ref()
        if conn.execute(
            "SELECT 1 FROM support_cases WHERE ticket_ref = ? LIMIT 1", (ticket_ref,)
        ).fetchone():
            continue
        try:
            cursor = conn.execute(
                """INSERT INTO support_cases
                   (customer_id, issue_type, description, status, created_at,
                    ticket_ref, category, subject, priority, updated_at, origin,
                    is_synthetic_demo, conversation_id, language)
                   VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    customer_id, category, description, created_at, ticket_ref,
                    category, subject, priority, created_at, origin,
                    str(conversation_id) if conversation_id is not None else None,
                    language,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "ticket_ref" in str(exc).casefold():
                continue
            raise
        case_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO support_case_events
               (event_id, case_id, from_status, to_status, actor_type, occurred_at, reason_code)
               VALUES (?, ?, NULL, 'OPEN', 'customer', ?, 'case_created')""",
            (str(uuid4()), case_id, created_at),
        )
        row = conn.execute(
            "SELECT * FROM support_cases WHERE case_id = ? AND customer_id = ?",
            (case_id, customer_id),
        ).fetchone()
        return _ticket_projection(row, detail=True)
    raise SupportCaseError("Could not allocate a unique ticket reference.")


def _decode_cursor(cursor):
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or len(cursor) > 64:
        raise InvalidSupportCursor("Invalid support cursor.")
    try:
        value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        offset = int(value)
        if offset < 0 or str(offset) != value:
            raise ValueError
        return offset
    except (ValueError, UnicodeError, binascii.Error):
        raise InvalidSupportCursor("Invalid support cursor.") from None


def _encode_cursor(offset):
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).rstrip(b"=").decode("ascii")


def create_support_case(
    principal: CustomerPrincipal,
    *,
    category: str,
    priority: str,
    subject: str,
    description: str,
    conversation_id: str | None = None,
    language: str | None = None,
    _origin: str = "customer_form",
    _conn=None,
):
    """Create a synthetic case; `_origin` is reserved for trusted backend callers."""
    customer_id = _customer_id(principal)
    category = _validate_category(category)
    priority = _validate_priority(priority)
    subject = _validate_text("subject", subject, 1, 160)
    description = _validate_text("description", description, 1, 4000)
    language = _validate_language(language)
    if not isinstance(_origin, str) or _origin not in ORIGINS:
        raise SupportCaseError("Unsupported support case origin.")

    _assert_conversation_owner(principal, conversation_id, _conn)
    if _conn is not None:
        return _insert_support_case(
            _conn,
            customer_id,
            category=category,
            priority=priority,
            subject=subject,
            description=description,
            conversation_id=conversation_id,
            language=language,
            origin=_origin,
        )
    with _transaction() as conn:
        return _insert_support_case(
            conn,
            customer_id,
            category=category,
            priority=priority,
            subject=subject,
            description=description,
            conversation_id=conversation_id,
            language=language,
            origin=_origin,
        )


def list_support_cases(
    principal: CustomerPrincipal,
    *,
    limit: int = 25,
    cursor: str | None = None,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
):
    customer_id = _customer_id(principal)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise SupportCaseError("limit must be between 1 and 100.")
    offset = _decode_cursor(cursor)
    if status is not None and status not in STATUSES:
        raise SupportCaseError("Unsupported support status.")
    if category is not None:
        category = _validate_category(category)
    if priority is not None:
        priority = _validate_priority(priority)

    clauses = ["customer_id = ?"]
    parameters: list[object] = [customer_id]
    for field, value in (("status", status), ("category", category), ("priority", priority)):
        if value is not None:
            clauses.append(f"{field} = ?")
            parameters.append(value)
    parameters.extend((limit + 1, offset))
    with _read_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM support_cases WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC, case_id DESC LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
    has_more = len(rows) > limit
    items = [_ticket_projection(row, detail=False) for row in rows[:limit]]
    return {
        "items": items,
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }


def get_support_case(principal: CustomerPrincipal, ticket_ref: str):
    customer_id = _customer_id(principal)
    if not isinstance(ticket_ref, str) or not _TICKET_REF.fullmatch(ticket_ref):
        raise SupportCaseNotFoundError("Support case not found.")
    with _read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM support_cases WHERE ticket_ref = ? AND customer_id = ?",
            (ticket_ref, customer_id),
        ).fetchone()
    if row is None:
        raise SupportCaseNotFoundError("Support case not found.")
    return _ticket_projection(row, detail=True)


def close_support_case(principal: CustomerPrincipal, ticket_ref: str):
    customer_id = _customer_id(principal)
    if not isinstance(ticket_ref, str) or not _TICKET_REF.fullmatch(ticket_ref):
        raise SupportCaseNotFoundError("Support case not found.")
    occurred_at = _now()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM support_cases WHERE ticket_ref = ? AND customer_id = ?",
            (ticket_ref, customer_id),
        ).fetchone()
        if row is None:
            raise SupportCaseNotFoundError("Support case not found.")
        previous_status = row["status"]
        if previous_status not in CUSTOMER_CLOSE_ELIGIBLE:
            raise InvalidSupportCaseTransition("This support case cannot be closed from its current status.")
        updated = conn.execute(
            "UPDATE support_cases SET status = 'CLOSED', updated_at = ? "
            "WHERE case_id = ? AND customer_id = ? AND status = ?",
            (occurred_at, row["case_id"], customer_id, previous_status),
        )
        if updated.rowcount != 1:
            raise InvalidSupportCaseTransition("The support case status changed; refresh and try again.")
        conn.execute(
            """INSERT INTO support_case_events
               (event_id, case_id, from_status, to_status, actor_type, occurred_at, reason_code)
               VALUES (?, ?, ?, 'CLOSED', 'customer', ?, 'customer_closed')""",
            (str(uuid4()), row["case_id"], previous_status, occurred_at),
        )
        result = conn.execute(
            "SELECT * FROM support_cases WHERE case_id = ? AND customer_id = ?",
            (row["case_id"], customer_id),
        ).fetchone()
    return _ticket_projection(result, detail=True)


def get_support_case_events(principal: CustomerPrincipal, ticket_ref: str):
    customer_id = _customer_id(principal)
    if not isinstance(ticket_ref, str) or not _TICKET_REF.fullmatch(ticket_ref):
        raise SupportCaseNotFoundError("Support case not found.")
    with _read_connection() as conn:
        case = conn.execute(
            "SELECT case_id FROM support_cases WHERE ticket_ref = ? AND customer_id = ?",
            (ticket_ref, customer_id),
        ).fetchone()
        if case is None:
            raise SupportCaseNotFoundError("Support case not found.")
        rows = conn.execute(
            """SELECT event_id, from_status, to_status, actor_type, occurred_at, reason_code
               FROM support_case_events WHERE case_id = ? ORDER BY rowid""",
            (case["case_id"],),
        ).fetchall()
    # event_id is omitted because legacy migration IDs encode internal case IDs.
    return [
        {
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "actor_type": row["actor_type"],
            "occurred_at": _public_timestamp(row["occurred_at"]),
            "reason_code": row["reason_code"],
        }
        for row in rows
    ]
