"""Persistent structured draft state for multi-turn support ticket creation.

Maintains customer-isolated draft fields (category, priority, subject, description,
amount, debit confirmation) across turns, ensuring slots are not dropped and
only one missing field is requested at a time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from services.database import get_connection

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def get_active_draft(customer_key: str, conversation_id: str) -> dict | None:
    """Retrieve the currently active draft for the authenticated customer and conversation."""
    if not customer_key or not conversation_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM support_ticket_drafts
            WHERE customer_key = ? AND conversation_id = ?
              AND status IN ('collecting', 'proposed')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (customer_key, str(conversation_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_or_update_draft(
    customer_key: str,
    conversation_id: str,
    *,
    category: str | None = None,
    priority: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    amount: float | None = None,
    debit_confirmed: bool | int | None = None,
    status: str | None = None,
    confirmation_id: str | None = None,
    result_ticket_ref: str | None = None,
) -> dict:
    """Create or merge slots into an existing active support ticket draft."""
    if not customer_key or not conversation_id:
        raise ValueError("customer_key and conversation_id are required.")

    conv_id = str(conversation_id)
    now_text = _now_iso()
    conn = get_connection()
    try:
        existing = conn.execute(
            """
            SELECT * FROM support_ticket_drafts
            WHERE customer_key = ? AND conversation_id = ?
              AND status IN ('collecting', 'proposed')
            LIMIT 1
            """,
            (customer_key, conv_id),
        ).fetchone()

        if existing:
            current = dict(existing)
            # Merge slots: new non-None values take precedence, otherwise keep existing
            new_category = category if category is not None else current.get("category")
            new_priority = priority if priority is not None else current.get("priority")
            new_subject = subject if subject is not None else current.get("subject")
            new_description = description if description is not None else current.get("description")
            new_amount = amount if amount is not None else current.get("amount")

            if debit_confirmed is not None:
                new_debit = 1 if debit_confirmed else 0
            else:
                new_debit = current.get("debit_confirmed", 0)

            # Keep existing status unless explicitly specified
            new_status = status if status is not None else current.get("status", "collecting")
            new_conf_id = confirmation_id if confirmation_id is not None else current.get("confirmation_id")
            new_ref = result_ticket_ref if result_ticket_ref is not None else current.get("result_ticket_ref")

            conn.execute(
                """
                UPDATE support_ticket_drafts
                SET category = ?,
                    priority = ?,
                    subject = ?,
                    description = ?,
                    amount = ?,
                    debit_confirmed = ?,
                    status = ?,
                    confirmation_id = ?,
                    result_ticket_ref = ?,
                    updated_at = ?
                WHERE draft_id = ?
                """,
                (
                    new_category,
                    new_priority,
                    new_subject,
                    new_description,
                    new_amount,
                    new_debit,
                    new_status,
                    new_conf_id,
                    new_ref,
                    now_text,
                    current["draft_id"],
                ),
            )
            conn.commit()
            return {
                "draft_id": current["draft_id"],
                "conversation_id": conv_id,
                "customer_key": customer_key,
                "category": new_category,
                "priority": new_priority,
                "subject": new_subject,
                "description": new_description,
                "amount": new_amount,
                "debit_confirmed": new_debit,
                "status": new_status,
                "confirmation_id": new_conf_id,
                "result_ticket_ref": new_ref,
                "created_at": current["created_at"],
                "updated_at": now_text,
            }
        else:
            draft_id = str(uuid4())
            new_status = status or "collecting"
            debit_val = 1 if debit_confirmed else 0
            conn.execute(
                """
                INSERT INTO support_ticket_drafts (
                    draft_id, conversation_id, customer_key,
                    category, priority, subject, description,
                    amount, debit_confirmed, status,
                    confirmation_id, result_ticket_ref,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    conv_id,
                    customer_key,
                    category,
                    priority,
                    subject,
                    description,
                    amount,
                    debit_val,
                    new_status,
                    confirmation_id,
                    result_ticket_ref,
                    now_text,
                    now_text,
                ),
            )
            conn.commit()
            return {
                "draft_id": draft_id,
                "conversation_id": conv_id,
                "customer_key": customer_key,
                "category": category,
                "priority": priority,
                "subject": subject,
                "description": description,
                "amount": amount,
                "debit_confirmed": debit_val,
                "status": new_status,
                "confirmation_id": confirmation_id,
                "result_ticket_ref": result_ticket_ref,
                "created_at": now_text,
                "updated_at": now_text,
            }
    finally:
        conn.close()


def mark_proposed(customer_key: str, conversation_id: str, confirmation_id: str) -> dict | None:
    """Mark the draft as proposed and attach the confirmation action ID."""
    return save_or_update_draft(
        customer_key,
        conversation_id,
        status="proposed",
        confirmation_id=str(confirmation_id),
    )


def mark_submitted(customer_key: str, conversation_id: str, result_ticket_ref: str) -> dict | None:
    """Mark the draft as submitted with the public ticket reference."""
    return save_or_update_draft(
        customer_key,
        conversation_id,
        status="submitted",
        result_ticket_ref=str(result_ticket_ref),
    )


def cancel_draft(customer_key: str, conversation_id: str) -> None:
    """Cancel any active draft for this customer and conversation."""
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE support_ticket_drafts
            SET status = 'cancelled', updated_at = ?
            WHERE customer_key = ? AND conversation_id = ?
              AND status IN ('collecting', 'proposed')
            """,
            (_now_iso(), customer_key, str(conversation_id)),
        )
        conn.commit()
    finally:
        conn.close()

