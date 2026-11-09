"""Short-lived, one-time confirmation for trusted assistant action proposals."""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from backend.api_models import (
    ConfirmationRequest,
    ConfirmationResponse,
    PendingAction,
    SupportTicketCreateAction,
    ToolExecutionResult,
)
from services import database
from services.conversation_service import ConversationNotFoundError, get_conversation
from services.customer_support_service import (
    SupportCaseError,
    SupportCaseNotFoundError,
    _customer_id,
    create_support_case,
)
from services.principal import CustomerPrincipal


DEFAULT_PENDING_ACTION_TTL_SECONDS = 300
MIN_PENDING_ACTION_TTL_SECONDS = 30
MAX_PENDING_ACTION_TTL_SECONDS = 900


class PendingActionError(ValueError):
    """Base class for safe pending-action errors."""


class PendingActionNotFound(LookupError):
    """The action does not exist or is not owned by this principal."""


class PendingActionConflict(PendingActionError):
    """The action is mismatched, already consumed, or otherwise unusable."""


class PendingActionConfigurationError(PendingActionError):
    """The pending-action TTL configuration is invalid."""


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


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _ttl_seconds() -> int:
    configured = os.getenv("VAANISHIELD_PENDING_ACTION_TTL_SECONDS")
    try:
        seconds = int(configured) if configured is not None else DEFAULT_PENDING_ACTION_TTL_SECONDS
    except (TypeError, ValueError):
        raise PendingActionConfigurationError("Pending-action expiry configuration is invalid.") from None
    if not MIN_PENDING_ACTION_TTL_SECONDS <= seconds <= MAX_PENDING_ACTION_TTL_SECONDS:
        raise PendingActionConfigurationError("Pending-action expiry configuration is invalid.")
    return seconds


def _clear_expired_payloads(conn, now_text: str):
    conn.execute(
        """UPDATE pending_actions
           SET status = 'expired', action_json = '{}', consumed_at = COALESCE(consumed_at, ?)
           WHERE status = 'pending' AND expires_at <= ?""",
        (now_text, now_text),
    )


def cleanup_expired_pending_actions():
    """Mark expired actions and erase their proposed ticket content."""
    now_text = _timestamp(_now())
    with _transaction() as conn:
        _clear_expired_payloads(conn, now_text)


def create_pending_action(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    original_turn_id: UUID | str,
    action: SupportTicketCreateAction | dict,
) -> PendingAction:
    """Persist a validated support-ticket proposal without creating a ticket."""
    # Reuse the support service's principal validation and server-side stable-key lookup.
    _customer_id(principal)
    try:
        conversation = get_conversation(principal, conversation_id)
    except (ConversationNotFoundError, ValueError):
        raise PendingActionNotFound("Conversation not found.") from None

    try:
        validated_action = (
            action if isinstance(action, SupportTicketCreateAction)
            else SupportTicketCreateAction.model_validate(action)
        )
    except ValidationError as exc:
        raise PendingActionError("The proposed support action is invalid.") from exc
    if validated_action.action_type != "create_support_ticket":
        raise PendingActionError("Unsupported pending action type.")

    ttl = _ttl_seconds()
    now = _now()
    expires_at = now + timedelta(seconds=ttl)
    confirmation_id = uuid4()
    conversation_id = str(conversation["conversation_id"])
    try:
        original_turn_id = str(UUID(str(original_turn_id)))
    except (TypeError, ValueError, AttributeError):
        raise PendingActionNotFound("Originating customer turn not found.") from None
    action_json = json.dumps(
        validated_action.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    summary = f"Create synthetic support ticket: {validated_action.subject}"
    with _transaction() as conn:
        now_text = _timestamp(now)
        _clear_expired_payloads(conn, now_text)
        # Recheck the conversation within the write transaction so pending
        # creation and its ownership binding cannot race with conversation state.
        owner = conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ? AND customer_key = ?",
            (conversation_id, principal.customer_key),
        ).fetchone()
        if owner is None:
            raise PendingActionNotFound("Conversation not found.")
        user_turn = conn.execute(
            "SELECT 1 FROM conversation_turns "
            "WHERE turn_id = ? AND conversation_id = ? AND role = 'user' AND deleted_at IS NULL",
            (original_turn_id, conversation_id),
        ).fetchone()
        if user_turn is None:
            raise PendingActionNotFound("Originating customer turn not found.")
        conn.execute(
            """INSERT INTO pending_actions
               (confirmation_id, customer_key, session_id, conversation_id,
                original_turn_id, action_type, action_json, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 'create_support_ticket', ?, 'pending', ?, ?)""",
            (
                str(confirmation_id), principal.customer_key, str(principal.session_id),
                conversation_id, original_turn_id, action_json, now_text, _timestamp(expires_at),
            ),
        )
    return PendingAction(
        confirmation_id=confirmation_id,
        conversation_id=UUID(conversation_id),
        original_turn_id=UUID(original_turn_id),
        summary=summary,
        action=validated_action,
        expires_at=expires_at,
    )


def confirm_pending_action(
    principal: CustomerPrincipal,
    confirmation_id: UUID | str,
    request: ConfirmationRequest,
) -> ConfirmationResponse:
    """Confirm, decline, or expire an action atomically with its side effect."""
    _customer_id(principal)
    confirmation_id = str(confirmation_id)
    conversation_id = str(request.conversation_id)
    now = _now()
    now_text = _timestamp(now)

    with _transaction() as conn:
        _clear_expired_payloads(conn, now_text)
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
        if (
            row is None
            or row["customer_key"] != principal.customer_key
            or row["session_id"] != str(principal.session_id)
        ):
            raise PendingActionNotFound("Confirmation not found.")
        if row["conversation_id"] != conversation_id:
            raise PendingActionConflict("Confirmation context does not match.")
        if row["original_turn_id"] != str(request.original_turn_id):
            raise PendingActionConflict("Confirmation context does not match.")
        if row["action_type"] != request.action_type or row["action_type"] != "create_support_ticket":
            raise PendingActionConflict("Confirmation action type does not match.")

        original_turn = conn.execute(
            "SELECT 1 FROM conversation_turns "
            "WHERE turn_id = ? AND conversation_id = ? AND role = 'user'",
            (row["original_turn_id"], row["conversation_id"]),
        ).fetchone()
        if original_turn is None:
            raise PendingActionConflict("Originating customer turn is no longer valid.")

        if row["status"] == "expired":
            return ConfirmationResponse(
                confirmation_id=UUID(confirmation_id), status="expired"
            )
        if row["status"] != "pending":
            raise PendingActionConflict("Confirmation has already been consumed.")

        expires_at = _parse_timestamp(row["expires_at"])
        if expires_at <= now:
            conn.execute(
                "UPDATE pending_actions SET status = 'expired', action_json = '{}', "
                "consumed_at = COALESCE(consumed_at, ?) WHERE confirmation_id = ?",
                (now_text, confirmation_id),
            )
            return ConfirmationResponse(
                confirmation_id=UUID(confirmation_id), status="expired"
            )

        if not request.confirmed:
            conn.execute(
                "UPDATE pending_actions SET status = 'declined', action_json = '{}', consumed_at = ? "
                "WHERE confirmation_id = ? AND status = 'pending'",
                (now_text, confirmation_id),
            )
            return ConfirmationResponse(
                confirmation_id=UUID(confirmation_id), status="declined"
            )

        try:
            action_data = json.loads(row["action_json"])
            action = SupportTicketCreateAction.model_validate(action_data)
        except (json.JSONDecodeError, ValidationError, TypeError):
            raise PendingActionConflict("The stored confirmation action is invalid.") from None

        # Existing support service revalidates principal and input. Its
        # trusted-connection path shares this transaction, preventing a
        # consumed action without a ticket or duplicate tickets on replay.
        try:
            created = create_support_case(
                principal,
                category=action.category,
                priority=action.priority,
                subject=action.subject,
                description=action.description or "No additional description provided.",
                conversation_id=row["conversation_id"],
                language=action.language,
                _origin="assistant_confirmed",
                _conn=conn,
            )
        except (SupportCaseError, SupportCaseNotFoundError):
            raise PendingActionConflict("The proposed support ticket can no longer be created.") from None

        conn.execute(
            "UPDATE pending_actions SET status = 'executed', action_json = '{}', "
            "consumed_at = ?, result_ticket_ref = ? "
            "WHERE confirmation_id = ? AND status = 'pending'",
            (now_text, created["ticket_ref"], confirmation_id),
        )
        # Close the exact conversation's matching proposal in the same
        # transaction as ticket creation and confirmation consumption.
        conn.execute(
            "UPDATE support_ticket_drafts "
            "SET status = 'submitted', result_ticket_ref = ?, updated_at = ? "
            "WHERE customer_key = ? AND conversation_id = ? "
            "AND confirmation_id = ? AND status = 'proposed'",
            (created["ticket_ref"], now_text, principal.customer_key,
             conversation_id, confirmation_id),
        )
        result = ToolExecutionResult(
            tool_call_id=UUID(confirmation_id),
            tool_name="create_support_ticket",
            status="succeeded",
            summary="Created the confirmed synthetic support ticket.",
            data={
                "ticket_ref": created["ticket_ref"],
                "is_synthetic_demo": True,
            },
        )
        return ConfirmationResponse(
            confirmation_id=UUID(confirmation_id),
            status="executed",
            action_result=result,
        )
