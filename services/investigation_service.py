"""Owner-scoped persistence and confirmation for synthetic security investigations."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from backend.api_models import (
    AddInvestigationNoteAction,
    ChangeInvestigationStatusAction,
    CloseInvestigationAction,
    ConfirmationResponse,
    CreateInvestigationAction,
    InvestigationConfirmationRequest,
    InvestigationDetail,
    InvestigationEvidenceLinkRequest,
    InvestigationEvidenceReference,
    InvestigationEvidenceSummary,
    InvestigationEvent,
    InvestigationNoteRequest,
    InvestigationStatusUpdateRequest,
    InvestigationSummary,
    LinkInvestigationEvidenceAction,
    PendingAction,
    ToolExecutionResult,
)
from services import database
from services.banking_service import get_customer_profile_by_key
from services.conversation_service import ConversationNotFoundError, get_conversation
from services.principal import CustomerPrincipal


STATUSES = frozenset({
    "OPEN", "UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED", "CLOSED",
})
CATEGORIES = frozenset({
    "MESSAGE_SCAM", "CALL_SCAM", "TRANSACTION_RISK", "ACCOUNT_SECURITY", "OTHER",
})
EVIDENCE_TYPES = frozenset({
    "MESSAGE_ASSESSMENT", "CALL_ASSESSMENT", "TRANSACTION_RISK_ASSESSMENT",
})
TRANSITIONS = {
    "OPEN": frozenset({"UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED", "CLOSED"}),
    "UNDER_REVIEW": frozenset({"WAITING_FOR_INFORMATION", "RESOLVED", "CLOSED"}),
    "WAITING_FOR_INFORMATION": frozenset({"UNDER_REVIEW", "RESOLVED", "CLOSED"}),
    "RESOLVED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
}
_INV_REF = re.compile(r"^INV-[A-F0-9]{32}$")
_ACTION_MODELS = {
    "create_investigation": CreateInvestigationAction,
    "link_investigation_evidence": LinkInvestigationEvidenceAction,
    "add_investigation_note": AddInvestigationNoteAction,
    "change_investigation_status": ChangeInvestigationStatusAction,
    "close_investigation": CloseInvestigationAction,
}


class InvestigationError(ValueError):
    """Invalid input or unsupported customer operation."""


class InvestigationNotFound(LookupError):
    """Investigation is unknown or not owned by the authenticated customer."""


class InvestigationConflict(InvestigationError):
    """Requested mutation conflicts with current investigation state."""


class InvestigationEvidenceNotFound(LookupError):
    """Evidence does not exist or belongs to another customer."""


class InvestigationConfirmationNotFound(LookupError):
    """Confirmation does not exist or belongs to another principal/session."""


class InvestigationConfirmationConflict(InvestigationError):
    """Confirmation context is invalid, consumed, or stale."""


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | str | None = None) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = value or _now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _public_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


def _owner(principal: CustomerPrincipal) -> str:
    if (
        not isinstance(principal, CustomerPrincipal)
        or principal.auth_method != "demo_cookie"
        or principal.customer_key not in frozenset(database.DEMO_CUSTOMER_KEYS.values())
        or principal.expires_at <= _now()
    ):
        raise PermissionError("An authenticated demo customer is required.")
    if get_customer_profile_by_key(principal.customer_key) is None:
        raise PermissionError("The authenticated demo customer is unavailable.")
    return principal.customer_key


def _clean_text(value: str, *, name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise InvestigationError(f"{name} must be text.")
    text = value.strip()
    if len(text) > maximum:
        raise InvestigationError(f"{name} is too long.")
    # Sensitive values are not retained in investigation summaries or notes.
    patterns = (
        (r"(?i)\b(otp|one[- ]time password|upi\s*pin|pin|password|passcode|security code)\b\s*(?::|is|=|-)?\s*[A-Z0-9@#$%*!_.-]{3,80}", r"\1 [REDACTED]"),
        (r"(?i)\b(account number|card number|cvv|cvc)\b\s*(?::|is|=|-)?\s*[0-9 -]{4,24}", r"\1 [REDACTED]"),
        (r"(?<!\w)(?:\+?\d[\d -]{8,16}\d)(?!\w)", "[NUMBER REDACTED]"),
        (r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL REDACTED]"),
        (r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    text = text[:maximum].strip()
    if len(text) < minimum:
        raise InvestigationError(f"{name} must contain at least {minimum} characters after sensitive data is removed.")
    return text


def _validate_ref(value: str) -> str:
    if not isinstance(value, str) or not _INV_REF.fullmatch(value):
        raise InvestigationNotFound("Investigation not found.")
    return value


def _validate_evidence_reference(evidence: InvestigationEvidenceReference | dict) -> InvestigationEvidenceReference:
    try:
        parsed = evidence if isinstance(evidence, InvestigationEvidenceReference) else InvestigationEvidenceReference.model_validate(evidence)
    except ValidationError:
        raise InvestigationError("The evidence reference is invalid.") from None
    if parsed.evidence_type not in EVIDENCE_TYPES:
        raise InvestigationError("Unsupported evidence type.")
    return parsed


def _evidence_row(conn, owner: str, evidence: InvestigationEvidenceReference):
    assessment_id = str(evidence.assessment_id)
    if evidence.evidence_type in {"MESSAGE_ASSESSMENT", "CALL_ASSESSMENT"}:
        mode = "typed_message" if evidence.evidence_type == "MESSAGE_ASSESSMENT" else "voice_call"
        row = conn.execute(
            "SELECT assessment_id, risk_level, evidence_strength, explanation, created_at "
            "FROM scam_assessments WHERE assessment_id = ? AND customer_key = ? AND source_mode = ?",
            (assessment_id, owner, mode),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT assessment_id, assessment_json, created_at FROM transaction_risk_assessments "
            "WHERE assessment_id = ? AND customer_key = ?",
            (assessment_id, owner),
        ).fetchone()
        if row is not None:
            risk = json.loads(row["assessment_json"])
            row = {
                "assessment_id": row["assessment_id"],
                "risk_level": risk.get("risk_level", "UNKNOWN"),
                "evidence_strength": risk.get("evidence_strength", "NONE"),
                "explanation": risk.get("explanation", ""),
                "created_at": row["created_at"],
            }
    if row is None:
        raise InvestigationEvidenceNotFound("Assessment not found.")
    return row


def _link_evidence(conn, investigation, owner, evidence, *, at=None):
    evidence = _validate_evidence_reference(evidence)
    _evidence_row(conn, owner, evidence)
    existing = conn.execute(
        "SELECT 1 FROM investigation_evidence WHERE investigation_id=? AND evidence_type=? AND assessment_id=?",
        (investigation["investigation_id"], evidence.evidence_type, str(evidence.assessment_id)),
    ).fetchone()
    if existing:
        raise InvestigationConflict("This assessment is already linked to the investigation.")
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM investigation_evidence WHERE investigation_id=?",
        (investigation["investigation_id"],),
    ).fetchone()["count"]
    if count >= 5:
        raise InvestigationError("An investigation can link at most five assessments.")
    linked_at = _ts(at)
    try:
        conn.execute(
            "INSERT INTO investigation_evidence "
            "(investigation_id, evidence_type, assessment_id, linked_at) VALUES (?, ?, ?, ?)",
            (investigation["investigation_id"], evidence.evidence_type, str(evidence.assessment_id), linked_at),
        )
    except sqlite3.IntegrityError:
        raise InvestigationConflict("This assessment is already linked to the investigation.") from None
    _event(
        conn, investigation, owner, "EVIDENCE_LINKED", "Linked an existing automated assessment.",
        {"evidence_type": evidence.evidence_type, "assessment_id": str(evidence.assessment_id)}, linked_at,
    )
    return evidence


def _event(conn, investigation, owner, event_type, summary, metadata=None, at=None):
    conn.execute(
        "INSERT INTO investigation_events "
        "(event_id, investigation_id, customer_key, event_type, summary, metadata_json, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid4()), investigation["investigation_id"], owner, event_type,
            summary, json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")), _ts(at),
        ),
    )


def _find_investigation(conn, owner: str, investigation_ref: str):
    ref = _validate_ref(investigation_ref)
    row = conn.execute(
        "SELECT * FROM investigations WHERE investigation_ref = ? AND customer_key = ?",
        (ref, owner),
    ).fetchone()
    if row is None:
        raise InvestigationNotFound("Investigation not found.")
    return row


def _summary(conn, row):
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM investigation_evidence WHERE investigation_id = ?",
        (row["investigation_id"],),
    ).fetchone()["count"]
    return InvestigationSummary(
        investigation_ref=row["investigation_ref"], title=row["title"],
        summary=row["summary"], category=row["category"], status=row["status"],
        created_at=_public_ts(row["created_at"]), updated_at=_public_ts(row["updated_at"]),
        resolved_at=_public_ts(row["resolved_at"]), evidence_count=count,
    )


def _evidence_summary(conn, owner, link):
    evidence = InvestigationEvidenceReference(
        evidence_type=link["evidence_type"], assessment_id=UUID(link["assessment_id"]),
    )
    source = _evidence_row(conn, owner, evidence)
    return InvestigationEvidenceSummary(
        evidence_type=evidence.evidence_type,
        assessment_id=evidence.assessment_id,
        risk_level=source["risk_level"],
        evidence_strength=source["evidence_strength"],
        summary=str(source["explanation"] or "Automated screening result linked by the customer.")[:500],
        created_at=_public_ts(source["created_at"]),
    )


def _detail(conn, owner, row):
    summary = _summary(conn, row)
    evidence_rows = conn.execute(
        "SELECT evidence_type, assessment_id FROM investigation_evidence "
        "WHERE investigation_id = ? ORDER BY linked_at, evidence_id",
        (row["investigation_id"],),
    ).fetchall()
    evidence = [_evidence_summary(conn, owner, item) for item in evidence_rows]
    event_rows = conn.execute(
        "SELECT event_type, summary, metadata_json, occurred_at FROM investigation_events "
        "WHERE investigation_id = ? AND customer_key = ? ORDER BY occurred_at, rowid",
        (row["investigation_id"], owner),
    ).fetchall()
    events = []
    for item in event_rows:
        metadata = json.loads(item["metadata_json"] or "{}")
        events.append(InvestigationEvent(
            event_type=item["event_type"], summary=item["summary"],
            occurred_at=_public_ts(item["occurred_at"]),
            note=metadata.get("note"),
            from_status=metadata.get("from_status"),
            to_status=metadata.get("to_status"),
            evidence_type=metadata.get("evidence_type"),
            assessment_id=metadata.get("assessment_id"),
        ))
    return InvestigationDetail(**summary.model_dump(), evidence=evidence, events=events)


def _insert_investigation(conn, owner, *, title, category, summary, evidence=()):
    title = _clean_text(title, name="Title", minimum=3, maximum=120)
    summary = _clean_text(summary, name="Summary", minimum=8, maximum=1500)
    if category not in CATEGORIES:
        raise InvestigationError("Unsupported investigation category.")
    parsed_evidence = [_validate_evidence_reference(item) for item in evidence]
    keys = [(item.evidence_type, str(item.assessment_id)) for item in parsed_evidence]
    if len(keys) != len(set(keys)):
        raise InvestigationError("Duplicate assessment references are not allowed.")
    for item in parsed_evidence:
        _evidence_row(conn, owner, item)
    created_at = _ts()
    for _ in range(5):
        ref = "INV-" + secrets.token_hex(16).upper()
        try:
            cursor = conn.execute(
                "INSERT INTO investigations "
                "(investigation_ref, customer_key, title, summary, category, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?)",
                (ref, owner, title, summary, category, created_at, created_at),
            )
            break
        except sqlite3.IntegrityError as exc:
            if "investigation_ref" not in str(exc).casefold():
                raise
    else:
        raise InvestigationError("Could not allocate a public investigation reference.")
    row = conn.execute("SELECT * FROM investigations WHERE investigation_id = ?", (cursor.lastrowid,)).fetchone()
    _event(
        conn, row, owner, "CREATED", "Created a personal security tracking record.",
        {"category": category, "reported_information_unverified": True}, created_at,
    )
    for item in parsed_evidence:
        _link_evidence(conn, row, owner, item, at=created_at)
    return _detail(conn, owner, row)


def create_investigation(
    principal: CustomerPrincipal, *, title: str, category: str,
    summary: str, evidence=(),
):
    owner = _owner(principal)
    with _transaction() as conn:
        return _insert_investigation(
            conn, owner, title=title, category=category,
            summary=summary, evidence=evidence,
        )


def list_investigations(
    principal: CustomerPrincipal, *, limit=25, cursor=None,
    status=None, category=None, search=None,
):
    owner = _owner(principal)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InvestigationError("Page limit must be between 1 and 100.")
    try:
        decoded = "0" if not cursor else base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
        offset = int(decoded)
        if offset < 0 or str(offset) != decoded:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error, TypeError):
        raise InvestigationError("Invalid investigation cursor.") from None
    if status is not None and status not in STATUSES:
        raise InvestigationError("Unsupported investigation status.")
    if category is not None and category not in CATEGORIES:
        raise InvestigationError("Unsupported investigation category.")
    if search is not None:
        search = _clean_text(search, name="Search", minimum=1, maximum=100)
    clauses = ["customer_key = ?"]
    params: list[object] = [owner]
    if status:
        clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if search:
        clauses.append("(title LIKE ? OR summary LIKE ? OR investigation_ref LIKE ?)")
        needle = f"%{search}%"
        params.extend((needle, needle, needle))
    params.extend((limit + 1, offset))
    conn = database.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM investigations WHERE " + " AND ".join(clauses) +
            " ORDER BY updated_at DESC, investigation_id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        has_more = len(rows) > limit
        items = [_summary(conn, row) for row in rows[:limit]]
    finally:
        conn.close()
    next_cursor = base64.urlsafe_b64encode(str(offset + limit).encode("ascii")).rstrip(b"=").decode("ascii") if has_more else None
    return {"items": [item.model_dump(mode="json") for item in items], "next_cursor": next_cursor, "has_more": has_more}


def get_investigation(principal: CustomerPrincipal, investigation_ref: str):
    owner = _owner(principal)
    conn = database.get_connection()
    try:
        row = _find_investigation(conn, owner, investigation_ref)
        return _detail(conn, owner, row).model_dump(mode="json")
    finally:
        conn.close()


def _link_owned(principal, investigation_ref, evidence):
    owner = principal.customer_key
    with _transaction() as conn:
        row = _find_investigation(conn, owner, investigation_ref)
        if row["status"] == "CLOSED":
            raise InvestigationConflict("Closed investigations cannot be changed.")
        _link_evidence(conn, row, owner, evidence)
        conn.execute(
            "UPDATE investigations SET updated_at = ? WHERE investigation_id = ?",
            (_ts(), row["investigation_id"]),
        )
        updated = _find_investigation(conn, owner, investigation_ref)
        return _detail(conn, owner, updated).model_dump(mode="json")


def link_investigation_evidence(
    principal: CustomerPrincipal, investigation_ref: str,
    evidence: InvestigationEvidenceReference | dict,
):
    _owner(principal)
    return _link_owned(principal, investigation_ref, evidence)


def _add_note_owned(principal, investigation_ref, note):
    owner = principal.customer_key
    safe_note = _clean_text(note, name="Additional information", minimum=3, maximum=1500)
    with _transaction() as conn:
        row = _find_investigation(conn, owner, investigation_ref)
        if row["status"] == "CLOSED":
            raise InvestigationConflict("Closed investigations cannot be changed.")
        return _add_note_in_transaction(conn, row, owner, safe_note)


def _add_note_in_transaction(conn, row, owner, note):
    at = _ts()
    _event(
        conn, row, owner, "INFORMATION_ADDED",
        "Customer added unverified information.", {"note": note}, at,
    )
    conn.execute(
        "UPDATE investigations SET updated_at = ? WHERE investigation_id = ?",
        (at, row["investigation_id"]),
    )
    updated = _find_investigation(conn, owner, row["investigation_ref"])
    return _detail(conn, owner, updated).model_dump(mode="json")


def add_investigation_note(principal: CustomerPrincipal, investigation_ref: str, note: str):
    _owner(principal)
    return _add_note_owned(principal, investigation_ref, note)


def _change_status_owned(principal, investigation_ref, target_status):
    owner = principal.customer_key
    if target_status not in STATUSES:
        raise InvestigationError("Unsupported investigation status.")
    with _transaction() as conn:
        row = _find_investigation(conn, owner, investigation_ref)
        current = row["status"]
        if target_status not in TRANSITIONS[current]:
            raise InvestigationConflict("That investigation status transition is not permitted.")
        at = _ts()
        resolved_at = at if target_status == "RESOLVED" else row["resolved_at"]
        closed_at = at if target_status == "CLOSED" else row["closed_at"]
        conn.execute(
            "UPDATE investigations SET status = ?, updated_at = ?, resolved_at = ?, closed_at = ? WHERE investigation_id = ?",
            (target_status, at, resolved_at, closed_at, row["investigation_id"]),
        )
        updated = _find_investigation(conn, owner, investigation_ref)
        event_type = "CLOSED" if target_status == "CLOSED" else "STATUS_CHANGED"
        _event(
            conn, updated, owner, event_type,
            "Customer marked the personal tracking status as " + target_status + ".",
            {"from_status": current, "to_status": target_status}, at,
        )
        return _detail(conn, owner, updated).model_dump(mode="json")


def update_investigation_status(
    principal: CustomerPrincipal,
    investigation_ref: str,
    payload: InvestigationStatusUpdateRequest | str,
):
    _owner(principal)
    target = payload.status if isinstance(payload, InvestigationStatusUpdateRequest) else payload
    return _change_status_owned(principal, investigation_ref, target)


def close_investigation(principal: CustomerPrincipal, investigation_ref: str):
    _owner(principal)
    return _change_status_owned(principal, investigation_ref, "CLOSED")


def _ttl_seconds():
    try:
        seconds = int(os.getenv("VAANISHIELD_PENDING_ACTION_TTL_SECONDS", "300"))
    except (TypeError, ValueError):
        raise InvestigationError("Confirmation expiry configuration is invalid.") from None
    if not 30 <= seconds <= 900:
        raise InvestigationError("Confirmation expiry configuration is invalid.")
    return seconds


def _scrub_expired(conn, now_text):
    conn.execute(
        "UPDATE investigation_pending_actions SET status='expired', action_json='{}', "
        "consumed_at=COALESCE(consumed_at, ?) WHERE status='pending' AND expires_at <= ?",
        (now_text, now_text),
    )


def cleanup_expired_investigation_actions():
    now_text = _ts()
    with _transaction() as conn:
        _scrub_expired(conn, now_text)


def _parse_action(action):
    if not isinstance(action, dict) or action.get("action_type") not in _ACTION_MODELS:
        raise InvestigationError("Unsupported investigation action.")
    try:
        parsed = _ACTION_MODELS[action["action_type"]].model_validate(action)
    except ValidationError:
        raise InvestigationError("The proposed investigation action is invalid.") from None
    # Pending payloads have a short retention window, but still scrub likely
    # authentication and payment secrets before they reach SQLite.
    if isinstance(parsed, CreateInvestigationAction):
        parsed = parsed.model_copy(update={
            "title": _clean_text(parsed.title, name="Title", minimum=3, maximum=120),
            "summary": _clean_text(parsed.summary, name="Summary", minimum=8, maximum=1500),
        })
    elif isinstance(parsed, AddInvestigationNoteAction):
        parsed = parsed.model_copy(update={
            "note": _clean_text(parsed.note, name="Note", minimum=3, maximum=1500),
        })
    return parsed


def create_pending_investigation_action(
    principal: CustomerPrincipal, conversation_id, original_turn_id, action,
) -> PendingAction:
    owner = _owner(principal)
    try:
        conversation = get_conversation(principal, conversation_id)
        conversation_id = str(conversation["conversation_id"])
        turn_id = str(UUID(str(original_turn_id)))
    except (ConversationNotFoundError, ValueError, TypeError, AttributeError):
        raise InvestigationNotFound("Conversation context not found.") from None
    parsed = _parse_action(action)
    action_type = parsed.action_type
    expected_updated_at = None
    with _transaction() as conn:
        if not conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=? AND customer_key=?",
            (conversation_id, owner),
        ).fetchone():
            raise InvestigationNotFound("Conversation context not found.")
        if not conn.execute(
            "SELECT 1 FROM conversation_turns WHERE turn_id=? AND conversation_id=? AND role='user' AND deleted_at IS NULL",
            (turn_id, conversation_id),
        ).fetchone():
            raise InvestigationNotFound("Originating customer turn not found.")
        if action_type == "create_investigation":
            for item in parsed.evidence:
                _evidence_row(conn, owner, item)
        else:
            target = _find_investigation(conn, owner, parsed.investigation_ref)
            if target["status"] == "CLOSED":
                raise InvestigationConflict("Closed investigations cannot be changed.")
            expected_updated_at = target["updated_at"]
            parsed = parsed.model_copy(update={"expected_updated_at": _public_ts(expected_updated_at)})
        now = _now()
        expires_at = now + timedelta(seconds=_ttl_seconds())
        confirmation_id = uuid4()
        conn.execute(
            "INSERT INTO investigation_pending_actions "
            "(confirmation_id, customer_key, session_id, conversation_id, original_turn_id, "
            "action_type, action_json, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                str(confirmation_id), owner, str(principal.session_id), conversation_id,
                turn_id, action_type,
                json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                _ts(now), _ts(expires_at),
            ),
        )
    summaries = {
        "create_investigation": "Propose creating a personal security tracking record.",
        "link_investigation_evidence": "Propose linking an existing assessment.",
        "add_investigation_note": "Propose adding customer-reported information.",
        "change_investigation_status": "Propose changing the personal tracking status.",
        "close_investigation": "Propose closing the personal tracking record.",
    }
    return PendingAction(
        confirmation_id=confirmation_id,
        conversation_id=UUID(conversation_id),
        original_turn_id=UUID(turn_id),
        summary=summaries[action_type],
        action=parsed,
        expires_at=expires_at,
    )


def _apply_action(conn, principal, action):
    owner = principal.customer_key
    if isinstance(action, CreateInvestigationAction):
        return _insert_investigation(
            conn, owner, title=action.title, category=action.category,
            summary=action.summary, evidence=action.evidence,
        ).model_dump(mode="json")
    if isinstance(action, LinkInvestigationEvidenceAction):
        row = _find_investigation(conn, owner, action.investigation_ref)
        if row["updated_at"] != (action.expected_updated_at.isoformat() if action.expected_updated_at else ""):
            raise InvestigationConfirmationConflict("The investigation changed after this proposal. Prepare a new proposal.")
        if row["status"] == "CLOSED":
            raise InvestigationConfirmationConflict("Closed investigations cannot be changed.")
        _link_evidence(conn, row, owner, action.evidence)
        at = _ts()
        conn.execute("UPDATE investigations SET updated_at=? WHERE investigation_id=?", (at, row["investigation_id"]))
        return _detail(conn, owner, _find_investigation(conn, owner, action.investigation_ref)).model_dump(mode="json")
    if isinstance(action, AddInvestigationNoteAction):
        row = _find_investigation(conn, owner, action.investigation_ref)
        if row["updated_at"] != (action.expected_updated_at.isoformat() if action.expected_updated_at else ""):
            raise InvestigationConfirmationConflict("The investigation changed after this proposal. Prepare a new proposal.")
        if row["status"] == "CLOSED":
            raise InvestigationConfirmationConflict("Closed investigations cannot be changed.")
        note = _clean_text(action.note, name="Additional information", minimum=3, maximum=1500)
        return _add_note_in_transaction(conn, row, owner, note)
    if isinstance(action, ChangeInvestigationStatusAction):
        row = _find_investigation(conn, owner, action.investigation_ref)
        expected = action.expected_updated_at.isoformat() if action.expected_updated_at else ""
        if row["updated_at"] != expected:
            raise InvestigationConfirmationConflict("The investigation changed after this proposal. Prepare a new proposal.")
        current = row["status"]
        if action.status not in TRANSITIONS[current]:
            raise InvestigationConfirmationConflict("That status transition is no longer permitted.")
        at = _ts()
        resolved_at = at if action.status == "RESOLVED" else row["resolved_at"]
        conn.execute(
            "UPDATE investigations SET status=?, updated_at=?, resolved_at=? WHERE investigation_id=?",
            (action.status, at, resolved_at, row["investigation_id"]),
        )
        updated = _find_investigation(conn, owner, action.investigation_ref)
        _event(conn, updated, owner, "STATUS_CHANGED", "Customer marked the personal tracking status as " + action.status + ".", {"from_status": current, "to_status": action.status}, at)
        return _detail(conn, owner, updated).model_dump(mode="json")
    if isinstance(action, CloseInvestigationAction):
        row = _find_investigation(conn, owner, action.investigation_ref)
        expected = action.expected_updated_at.isoformat() if action.expected_updated_at else ""
        if row["updated_at"] != expected:
            raise InvestigationConfirmationConflict("The investigation changed after this proposal. Prepare a new proposal.")
        current = row["status"]
        if "CLOSED" not in TRANSITIONS[current]:
            raise InvestigationConfirmationConflict("This investigation cannot be closed in its current state.")
        at = _ts()
        conn.execute("UPDATE investigations SET status='CLOSED', updated_at=?, closed_at=? WHERE investigation_id=?", (at, at, row["investigation_id"]))
        updated = _find_investigation(conn, owner, action.investigation_ref)
        _event(conn, updated, owner, "CLOSED", "Customer closed the personal security tracking record.", {"from_status": current, "to_status": "CLOSED"}, at)
        return _detail(conn, owner, updated).model_dump(mode="json")
    raise InvestigationError("Unsupported investigation action.")


def confirm_investigation_action(
    principal: CustomerPrincipal,
    confirmation_id: UUID | str,
    request: InvestigationConfirmationRequest,
) -> ConfirmationResponse:
    owner = _owner(principal)
    try:
        confirmation_id = str(UUID(str(confirmation_id)))
    except (ValueError, TypeError, AttributeError):
        raise InvestigationConfirmationNotFound("Confirmation not found.") from None
    now = _now()
    now_text = _ts(now)
    with _transaction() as conn:
        _scrub_expired(conn, now_text)
        row = conn.execute(
            "SELECT * FROM investigation_pending_actions WHERE confirmation_id=?",
            (confirmation_id,),
        ).fetchone()
        if row is None or row["customer_key"] != owner or row["session_id"] != str(principal.session_id):
            raise InvestigationConfirmationNotFound("Confirmation not found.")
        if (
            row["conversation_id"] != str(request.conversation_id)
            or row["original_turn_id"] != str(request.original_turn_id)
            or row["action_type"] != request.action_type
        ):
            raise InvestigationConfirmationConflict("Confirmation context does not match.")
        if not conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=? AND customer_key=?",
            (row["conversation_id"], owner),
        ).fetchone() or not conn.execute(
            "SELECT 1 FROM conversation_turns WHERE turn_id=? AND conversation_id=? AND role='user'",
            (row["original_turn_id"], row["conversation_id"]),
        ).fetchone():
            raise InvestigationConfirmationConflict("The originating conversation is no longer valid.")
        if row["status"] == "expired":
            return ConfirmationResponse(confirmation_id=UUID(confirmation_id), status="expired")
        if row["status"] != "pending":
            raise InvestigationConfirmationConflict("Confirmation has already been consumed.")
        if _public_ts(row["expires_at"]) <= now:
            conn.execute(
                "UPDATE investigation_pending_actions SET status='expired', action_json='{}', consumed_at=? WHERE confirmation_id=?",
                (now_text, confirmation_id),
            )
            return ConfirmationResponse(confirmation_id=UUID(confirmation_id), status="expired")
        if not request.confirmed:
            conn.execute(
                "UPDATE investigation_pending_actions SET status='declined', action_json='{}', consumed_at=? WHERE confirmation_id=?",
                (now_text, confirmation_id),
            )
            return ConfirmationResponse(confirmation_id=UUID(confirmation_id), status="declined")
        try:
            action = _parse_action(json.loads(row["action_json"]))
        except (json.JSONDecodeError, InvestigationError):
            raise InvestigationConfirmationConflict("The stored action is invalid.") from None
        if action.action_type != row["action_type"]:
            raise InvestigationConfirmationConflict("The stored action type does not match.")
        try:
            outcome = _apply_action(conn, principal, action)
        except (InvestigationNotFound, InvestigationEvidenceNotFound, InvestigationConflict, InvestigationConfirmationConflict) as exc:
            # Target changes expire and scrub the proposal without applying an action.
            if "changed after this proposal" in str(exc) or "no longer permitted" in str(exc):
                conn.execute(
                    "UPDATE investigation_pending_actions SET status='expired', action_json='{}', consumed_at=? WHERE confirmation_id=?",
                    (now_text, confirmation_id),
                )
                return ConfirmationResponse(confirmation_id=UUID(confirmation_id), status="expired")
            raise InvestigationConfirmationConflict("The proposed action is no longer available.") from None
        ref = outcome.get("investigation_ref") if isinstance(outcome, dict) else None
        status = outcome.get("status") if isinstance(outcome, dict) else None
        conn.execute(
            "UPDATE investigation_pending_actions SET status='executed', action_json='{}', consumed_at=?, result_investigation_ref=? WHERE confirmation_id=? AND status='pending'",
            (now_text, ref, confirmation_id),
        )
        result = ToolExecutionResult(
            tool_call_id=UUID(confirmation_id), tool_name=action.action_type,
            status="succeeded", summary="The confirmed personal tracking action was completed.",
            data={"investigation_ref": ref, "status": status},
        )
        return ConfirmationResponse(
            confirmation_id=UUID(confirmation_id), status="executed", action_result=result,
        )
