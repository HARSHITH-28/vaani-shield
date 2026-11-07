"""Persistence operations for principal-scoped assistant conversations."""

import base64
import json
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import UUID, uuid4

from services import database
from services.principal import CustomerPrincipal


@contextmanager
def _connection():
    conn = database.get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class ConversationNotFoundError(LookupError):
    """The conversation does not exist or is not owned by this principal."""


class InvalidCursorError(ValueError):
    """A pagination cursor is malformed."""


class TurnConflictError(ValueError):
    """An idempotent assistant request conflicts with an existing turn."""


def _turn_is_stale(turn, seconds=120):
    try:
        created = datetime.fromisoformat(str(turn.get("created_at")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds() >= seconds
    except (TypeError, ValueError):
        return False


def _owner(principal: CustomerPrincipal) -> str:
    if not isinstance(principal, CustomerPrincipal):
        raise TypeError("A server-authenticated customer principal is required.")
    return principal.customer_key


def _dict(row):
    return dict(row) if row is not None else None


def _conversation_title(text):
    """Create a concise first-request title without retaining obvious secrets."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"\b(otp|one[- ]time password|pin|password|cvv)\s*[:#-]?\s*[A-Za-z0-9-]+", r"\1 [redacted]", value, flags=re.I)
    value = re.sub(r"\b\d{7,}\b", "[number]", value)
    value = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email]", value, flags=re.I)
    value = value[:72].strip(" .,:;-")
    if len(value) < 4:
        return None
    return value[0].upper() + value[1:]


def _conversation_response(row, *, include_turn_count=False):
    """Project persistence rows onto the public conversation contract.

    Ownership data stays in SQLite and is used for all authorization checks,
    but it is not part of the API response. List summaries do not include the
    detail-only turn count.
    """
    if row is None:
        return None
    values = dict(row)
    result = {
        key: values[key]
        for key in ("conversation_id", "title", "created_at", "updated_at", "status")
    }
    if include_turn_count:
        result["turn_count"] = values["turn_count"]
    return result


def _conversation(conn, principal, conversation_id):
    row = conn.execute(
        "SELECT * FROM conversations WHERE conversation_id = ? AND customer_key = ?",
        (str(conversation_id), _owner(principal)),
    ).fetchone()
    if row is None:
        raise ConversationNotFoundError
    return row


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode()
        offset = int(value)
        if offset < 0 or str(offset) != value:
            raise ValueError
        return offset
    except (ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise InvalidCursorError from exc


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).rstrip(b"=").decode()


def create_conversation(principal: CustomerPrincipal, title: str | None = None):
    conversation_id = str(uuid4())
    with _connection() as conn:
        conn.execute(
            "INSERT INTO conversations (conversation_id, customer_key, title) VALUES (?, ?, ?)",
            (conversation_id, _owner(principal), title),
        )
        row = conn.execute(
            "SELECT conversation_id, title, created_at, updated_at, status, "
            "0 AS turn_count FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    return _conversation_response(row, include_turn_count=True)


def list_conversations(
    principal: CustomerPrincipal, limit: int = 25, cursor: str | None = None
):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    offset = _decode_cursor(cursor)
    with _connection() as conn:
        rows = conn.execute(
            """SELECT c.conversation_id, c.title, c.created_at, c.updated_at, c.status
               FROM conversations c WHERE c.customer_key = ?
               ORDER BY c.updated_at DESC, c.conversation_id DESC LIMIT ? OFFSET ?""",
            (_owner(principal), limit + 1, offset),
        ).fetchall()
    has_more = len(rows) > limit
    items = [_conversation_response(row) for row in rows[:limit]]
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def get_conversation(principal: CustomerPrincipal, conversation_id: UUID | str):
    with _connection() as conn:
        row = _conversation(conn, principal, conversation_id)
        turn_count = conn.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ? AND deleted_at IS NULL",
            (str(conversation_id),),
        ).fetchone()[0]
        result = dict(row)
        result["turn_count"] = turn_count
    return _conversation_response(result, include_turn_count=True)


def _save_turn(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    role: str,
    *,
    input_type: str | None,
    user_text_or_transcript: str | None,
    input_language: str | None,
    assistant_response_text: str | None,
    response_language: str | None,
    requested_output_mode: str | None,
    processing_status: str,
    client_turn_id: str | None = None,
):
    turn_id = str(uuid4())
    conversation_id = str(conversation_id)
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _conversation(conn, principal, conversation_id)
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO conversation_turns (
                   turn_id, conversation_id, sequence_number, role, input_type,
                   user_text_or_transcript, input_language, assistant_response_text,
                   response_language, requested_output_mode, client_turn_id, processing_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (turn_id, conversation_id, sequence, role, input_type,
             user_text_or_transcript, input_language, assistant_response_text,
              response_language, requested_output_mode, client_turn_id, processing_status),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE conversation_id = ?",
            (conversation_id,),
        )
        if role == "user":
            title = _conversation_title(user_text_or_transcript)
            if title:
                conn.execute(
                    "UPDATE conversations SET title = COALESCE(NULLIF(title, ''), ?) "
                    "WHERE conversation_id = ? AND customer_key = ?",
                    (title, conversation_id, _owner(principal)),
                )
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        conn.commit()
        return _dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_user_turn(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    *,
    input_type: str,
    text_or_transcript: str,
    input_language: str | None = None,
    requested_output_mode: str,
    processing_status: str = "completed",
    client_turn_id: UUID | str | None = None,
    detected_languages=(),
    is_code_mixed: bool = False,
):
    conversation_id = str(conversation_id)
    client_turn_id = str(client_turn_id) if client_turn_id else None
    if client_turn_id:
        conn = database.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _conversation(conn, principal, conversation_id)
            existing = conn.execute(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? "
                "AND client_turn_id = ? AND role = 'user'",
                (conversation_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                existing = dict(existing)
                if (
                    existing["user_text_or_transcript"] != text_or_transcript
                    or existing["input_type"] != input_type
                    or existing["requested_output_mode"] != requested_output_mode
                ):
                    raise TurnConflictError("The retry does not match the original user turn.")
                if existing["processing_status"] == "completed":
                    raise TurnConflictError("This assistant turn has already completed.")
                if existing["processing_status"] == "processing" and not _turn_is_stale(existing):
                    raise TurnConflictError("This assistant turn is already processing.")
                if existing["processing_status"] not in {"failed", "processing"}:
                    raise TurnConflictError("This assistant turn cannot be retried.")
                conn.execute(
                    "UPDATE conversation_turns SET processing_status = 'processing', "
                    "requested_output_mode = ?, input_language = ?, detected_languages_json = ?, "
                    "is_code_mixed = ? WHERE turn_id = ?",
                    (requested_output_mode, input_language,
                     json.dumps(list(detected_languages), ensure_ascii=False),
                     int(bool(is_code_mixed)), existing["turn_id"]),
                )
                conn.execute(
                    "UPDATE conversation_turns SET processing_status = 'processing', "
                    "assistant_response_text = 'Retrying the saved request.' "
                    "WHERE conversation_id = ? AND role = 'assistant' "
                    "AND sequence_number = ? AND processing_status = 'failed'",
                    (conversation_id, existing["sequence_number"] + 1),
                )
                conn.execute(
                    "UPDATE conversations SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
                    "WHERE conversation_id = ? AND customer_key = ?",
                    (conversation_id, _owner(principal)),
                )
                row = conn.execute(
                    "SELECT * FROM conversation_turns WHERE turn_id = ?",
                    (existing["turn_id"],),
                ).fetchone()
                conn.commit()
                return _dict(row)

            turn_id = str(uuid4())
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO conversation_turns (turn_id, conversation_id, sequence_number, role, "
                "input_type, user_text_or_transcript, input_language, detected_languages_json, "
                "is_code_mixed, requested_output_mode, client_turn_id, processing_status) "
                "VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, conversation_id, sequence, input_type, text_or_transcript,
                 input_language, json.dumps(list(detected_languages), ensure_ascii=False),
                 int(bool(is_code_mixed)), requested_output_mode, client_turn_id, processing_status),
            )
            title = _conversation_title(text_or_transcript)
            if title:
                conn.execute(
                    "UPDATE conversations SET title = COALESCE(NULLIF(title, ''), ?), "
                    "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
                    "WHERE conversation_id = ? AND customer_key = ?",
                    (title, conversation_id, _owner(principal)),
                )
            row = conn.execute("SELECT * FROM conversation_turns WHERE turn_id = ?", (turn_id,)).fetchone()
            conn.commit()
            return _dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return _save_turn(
        principal, conversation_id, "user", input_type=input_type,
        user_text_or_transcript=text_or_transcript, input_language=input_language,
        assistant_response_text=None, response_language=None,
        requested_output_mode=requested_output_mode,
        processing_status=processing_status,
    )


def save_assistant_turn(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    *,
    response_text: str,
    response_language: str | None = None,
    requested_output_mode: str,
    input_type: str | None = None,
    processing_status: str = "completed",
    for_user_turn_id: UUID | str | None = None,
):
    if for_user_turn_id is not None:
        conversation_id = str(conversation_id)
        conn = database.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _conversation(conn, principal, conversation_id)
            user = conn.execute(
                "SELECT sequence_number, deleted_at FROM conversation_turns WHERE turn_id = ? "
                "AND conversation_id = ? AND role = 'user'",
                (str(for_user_turn_id), conversation_id),
            ).fetchone()
            if user is None:
                raise LookupError("User turn not found")
            if user["deleted_at"] is not None:
                raise LookupError("User turn was deleted")
            assistant = conn.execute(
                "SELECT turn_id FROM conversation_turns WHERE conversation_id = ? "
                "AND sequence_number = ? AND role = 'assistant' "
                "AND processing_status IN ('failed', 'processing')",
                (conversation_id, user["sequence_number"] + 1),
            ).fetchone()
            if assistant is not None:
                conn.execute(
                    "UPDATE conversation_turns SET assistant_response_text = ?, "
                    "response_language = ?, requested_output_mode = ?, input_type = ?, "
                    "processing_status = ? WHERE turn_id = ?",
                    (response_text, response_language, requested_output_mode, input_type,
                     processing_status, assistant["turn_id"]),
                )
                conn.execute(
                    "UPDATE conversations SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
                    "WHERE conversation_id = ? AND customer_key = ?",
                    (conversation_id, _owner(principal)),
                )
                row = conn.execute("SELECT * FROM conversation_turns WHERE turn_id = ?", (assistant["turn_id"],)).fetchone()
                conn.commit()
                return _dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return _save_turn(
        principal, conversation_id, "assistant", input_type=input_type,
        user_text_or_transcript=None, input_language=None,
        assistant_response_text=response_text, response_language=response_language,
        requested_output_mode=requested_output_mode,
        processing_status=processing_status,
    )


def get_user_turn_by_client_id(principal, conversation_id, client_turn_id):
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id = ? "
            "AND client_turn_id = ? AND role = 'user'",
            (str(conversation_id), str(client_turn_id)),
        ).fetchone()
    return _dict(row)


def get_completed_response_for_user_turn(principal, conversation_id, user_turn_id):
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        user = conn.execute(
            "SELECT sequence_number FROM conversation_turns WHERE turn_id = ? "
            "AND conversation_id = ? AND role = 'user'",
            (str(user_turn_id), str(conversation_id)),
        ).fetchone()
        if user is None:
            return None
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id = ? "
            "AND sequence_number = ? AND role = 'assistant' "
            "AND processing_status = 'completed' AND deleted_at IS NULL",
            (str(conversation_id), user["sequence_number"] + 1),
        ).fetchone()
    return _dict(row)


def update_turn_status(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    turn_id: UUID | str,
    processing_status: str,
):
    if processing_status not in {"pending", "processing", "completed", "failed"}:
        raise ValueError("Invalid turn processing status")
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        cursor = conn.execute(
            "UPDATE conversation_turns SET processing_status = ? WHERE turn_id = ? AND conversation_id = ?",
            (processing_status, str(turn_id), str(conversation_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError("Turn not found")
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id = ?", (str(turn_id),)
        ).fetchone()
    return _dict(row)


def get_conversation_history(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    limit: int = 25,
    cursor: str | None = None,
):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    offset = _decode_cursor(cursor)
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        rows = conn.execute(
            """SELECT * FROM conversation_turns WHERE conversation_id = ? AND deleted_at IS NULL
               ORDER BY sequence_number ASC LIMIT ? OFFSET ?""",
            (str(conversation_id), limit + 1, offset),
        ).fetchall()
    has_more = len(rows) > limit
    items = []
    for row in rows[:limit]:
        item = _dict(row)
        item.pop("client_turn_id", None)
        try:
            item["detected_languages"] = json.loads(item.pop("detected_languages_json", "[]") or "[]")
        except (TypeError, ValueError):
            item["detected_languages"] = []
        item["is_code_mixed"] = bool(item.get("is_code_mixed"))
        item.pop("deleted_at", None)
        items.append(item)
    return {
        "items": items,
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }


def get_recent_conversation_turns(
    principal: CustomerPrincipal,
    conversation_id: UUID | str,
    limit: int = 12,
):
    """Return the newest turns for LLM context, in chronological order.

    This is separate from the public, offset-paginated history function so its
    established ordering and pagination contract remain unchanged.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        rows = conn.execute(
            """SELECT * FROM conversation_turns WHERE conversation_id = ? AND deleted_at IS NULL
               ORDER BY sequence_number DESC LIMIT ?""",
            (str(conversation_id), limit),
        ).fetchall()
    return [_dict(row) for row in reversed(rows)]


def delete_conversation_turn(principal: CustomerPrincipal, conversation_id, turn_id):
    """Soft-delete one owner-scoped message and cancel only its pending actions.

    The row remains to preserve sequence numbers and foreign-key references;
    its user/assistant content is erased and excluded from history and LLM context.
    """
    conversation_id, turn_id = str(conversation_id), str(turn_id)
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _conversation(conn, principal, conversation_id)
        row = conn.execute(
            "SELECT turn_id, role, sequence_number FROM conversation_turns "
            "WHERE turn_id = ? AND conversation_id = ? AND deleted_at IS NULL",
            (turn_id, conversation_id),
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError
        origin_turn_id = turn_id
        if row["role"] == "assistant":
            previous = conn.execute(
                "SELECT turn_id FROM conversation_turns WHERE conversation_id = ? "
                "AND sequence_number < ? AND role = 'user' ORDER BY sequence_number DESC LIMIT 1",
                (conversation_id, row["sequence_number"]),
            ).fetchone()
            if previous:
                origin_turn_id = previous["turn_id"]
        now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
        conn.execute(
            "UPDATE pending_actions SET status = 'declined', action_json = '{}', "
            "consumed_at = ? WHERE original_turn_id = ? AND status = 'pending'",
            (now, origin_turn_id),
        )
        conn.execute(
            "UPDATE investigation_pending_actions SET status = 'declined', action_json = '{}', "
            "consumed_at = ? WHERE original_turn_id = ? AND status = 'pending'",
            (now, origin_turn_id),
        )
        conn.execute(
            "UPDATE conversation_turns SET user_text_or_transcript = NULL, "
            "input_language = NULL, detected_languages_json = '[]', is_code_mixed = 0, "
            "assistant_response_text = NULL, response_language = NULL, deleted_at = ? "
            "WHERE turn_id = ? AND conversation_id = ?",
            (now, turn_id, conversation_id),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ? AND customer_key = ?",
            (now, conversation_id, _owner(principal)),
        )
        if row["role"] == "user" and int(row["sequence_number"]) == 1:
            conn.execute(
                "UPDATE conversations SET title = NULL WHERE conversation_id = ? AND customer_key = ?",
                (conversation_id, _owner(principal)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_conversation(
    principal: CustomerPrincipal, conversation_id: UUID | str, *,
    status: str | None = None, title: str | None = None,
):
    if status is not None and status not in {"active", "archived", "closed"}:
        raise ValueError("Invalid conversation status")
    conversation_id = str(conversation_id)
    with _connection() as conn:
        _conversation(conn, principal, conversation_id)
        if status is None and title is None:
            raise ValueError("At least one field must be updated")
        if status is None:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE conversation_id = ? AND customer_key = ?",
                (title, conversation_id, _owner(principal)),
            )
        elif title is None:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE conversation_id = ? AND customer_key = ?",
                (status, conversation_id, _owner(principal)),
            )
        else:
            conn.execute(
                "UPDATE conversations SET status = ?, title = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') WHERE conversation_id = ? AND customer_key = ?",
                (status, title, conversation_id, _owner(principal)),
            )
    return get_conversation(principal, conversation_id)
