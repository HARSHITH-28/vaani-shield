"""Customer-isolated Notification Center service.

Generates and manages notifications for loan EMI reminders, banking alerts,
support updates, and security/sign-in activities without duplicating events.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from services.banking_service import (
    get_customer_profile_by_reference,
    get_loan_details,
    get_recent_transactions,
)
from services.database import get_connection

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_notifications_for_customer(customer_key: str) -> None:
    """Sync authoritative events into the notifications table for this customer.

    Idempotent: Uses unique (customer_key, event_ref) index to avoid duplicates.
    """
    if not customer_key:
        return
    profile = get_customer_profile_by_reference(customer_key)
    if not profile:
        return
    customer_id = profile["customer_id"]

    conn = get_connection()
    try:
        now_text = _now_iso()

        # 1. Loan & EMI Reminders (authoritative loans table)
        try:
            loans = get_loan_details(customer_id)
            for loan in loans:
                if loan.get("status") == "ACTIVE" and loan.get("next_emi_date") and loan.get("monthly_emi"):
                    loan_id = loan["loan_id"]
                    emi_date = loan["next_emi_date"]
                    emi_amt = float(loan["monthly_emi"])
                    loan_type = str(loan.get("loan_type", "Personal")).title()
                    event_ref = f"loan-emi-{loan_id}-{emi_date}"
                    title = f"Loan EMI Reminder: {loan_type} Loan"
                    msg = (
                        f"Your monthly EMI of ₹{emi_amt:,.2f} for {loan_type} Loan "
                        f"is scheduled for {emi_date}."
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO notifications (
                            notification_id, customer_key, category, title, message,
                            event_ref, is_read, created_at, metadata_json
                        ) VALUES (?, ?, 'loan_emi', ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            customer_key,
                            title,
                            msg,
                            event_ref,
                            now_text,
                            json.dumps({"loan_id": loan_id, "emi_date": emi_date, "monthly_emi": emi_amt}),
                        ),
                    )
        except Exception as exc:
            logger.warning("Failed syncing loan notifications: %s", exc)

        # 2. Banking / Large Transaction Alerts
        try:
            txs = get_recent_transactions(customer_id, limit=10)
            for tx in txs:
                amount = float(tx.get("amount") or 0.0)
                if amount >= 5000.0:
                    ref_id = tx.get("reference_id") or str(tx.get("transaction_id"))
                    event_ref = f"tx-{ref_id}"
                    merchant = tx.get("merchant") or "Debit"
                    tx_date = str(tx.get("transaction_date", ""))[:10]
                    title = f"Banking Alert: Transaction at {merchant}"
                    msg = f"Payment of ₹{amount:,.2f} was processed on {tx_date}."
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO notifications (
                            notification_id, customer_key, category, title, message,
                            event_ref, is_read, created_at, metadata_json
                        ) VALUES (?, ?, 'banking_alert', ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            customer_key,
                            title,
                            msg,
                            event_ref,
                            now_text,
                            json.dumps({"amount": amount, "reference_id": ref_id, "merchant": merchant}),
                        ),
                    )
        except Exception as exc:
            logger.warning("Failed syncing transaction notifications: %s", exc)

        # 3. Support Updates
        try:
            cases = conn.execute(
                """
                SELECT ticket_ref, subject, status, updated_at
                FROM support_cases
                WHERE customer_id = ?
                ORDER BY case_id DESC LIMIT 5
                """,
                (customer_id,),
            ).fetchall()
            for case in cases:
                ticket_ref = case["ticket_ref"]
                status = case["status"]
                event_ref = f"support-{ticket_ref}-{status}"
                title = f"Support Update: {ticket_ref}"
                msg = f"Ticket regarding '{case['subject']}' is currently {status}."
                conn.execute(
                    """
                    INSERT OR IGNORE INTO notifications (
                        notification_id, customer_key, category, title, message,
                        event_ref, is_read, created_at, metadata_json
                    ) VALUES (?, ?, 'support_update', ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        customer_key,
                        title,
                        msg,
                        event_ref,
                        now_text,
                        json.dumps({"ticket_ref": ticket_ref, "status": status}),
                    ),
                )
        except Exception as exc:
            logger.warning("Failed syncing support notifications: %s", exc)

        # 4. Security & Sign-in Alerts
        try:
            signins = conn.execute(
                """
                SELECT log_id, timestamp_ist, login_result, ip_address, device_platform
                FROM signin_audit_logs
                WHERE customer_id = ?
                ORDER BY log_id DESC LIMIT 3
                """,
                (customer_id,),
            ).fetchall()
            for signin in signins:
                log_id = signin["log_id"]
                event_ref = f"signin-{log_id}"
                platform = signin["device_platform"] or "Web"
                ip = signin["ip_address"] or "127.0.0.1"
                time_str = signin["timestamp_ist"]
                title = "Security Activity: Account Sign-in"
                msg = f"Successful sign-in from {platform} ({ip}) at {time_str}."
                conn.execute(
                    """
                    INSERT OR IGNORE INTO notifications (
                        notification_id, customer_key, category, title, message,
                        event_ref, is_read, created_at, metadata_json
                    ) VALUES (?, ?, 'security_alert', ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        customer_key,
                        title,
                        msg,
                        event_ref,
                        now_text,
                        json.dumps({"log_id": log_id, "platform": platform, "ip": ip}),
                    ),
                )
        except Exception as exc:
            logger.warning("Failed syncing signin notifications: %s", exc)

        conn.commit()
    finally:
        conn.close()


def list_notifications(customer_key: str, limit: int = 25) -> list[dict]:
    """Retrieve notifications for the authenticated customer ordered newest first."""
    if not customer_key:
        return []
    sync_notifications_for_customer(customer_key)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT notification_id, customer_key, category, title, message,
                   event_ref, is_read, created_at, metadata_json
            FROM notifications
            WHERE customer_key = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (customer_key, limit),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["id"] = d.get("notification_id")
            items.append(d)
        return items
    finally:
        conn.close()


def get_unread_count(customer_key: str) -> int:
    """Return the unread notification count for the authenticated customer."""
    if not customer_key:
        return 0
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM notifications
            WHERE customer_key = ? AND is_read = 0
            """,
            (customer_key,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def mark_notification_read(customer_key: str, notification_id: str) -> bool:
    """Mark a single notification as read, customer-isolated."""
    if not customer_key or not notification_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE notifications
            SET is_read = 1
            WHERE customer_key = ? AND notification_id = ?
            """,
            (customer_key, str(notification_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_all_notifications_read(customer_key: str) -> int:
    """Mark all unread notifications as read for the authenticated customer."""
    if not customer_key:
        return 0
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE notifications
            SET is_read = 1
            WHERE customer_key = ? AND is_read = 0
            """,
            (customer_key,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def create_sms_threat_notification(
    customer_key: str,
    sms_event_id: str,
    risk_level: str,
    sender: str,
    warning: str,
    *,
    conn=None,
) -> bool:
    """Idempotently create a security_alert notification for HIGH/CRITICAL SMS threats."""
    if not customer_key or not sms_event_id:
        return False
    normalized_risk = str(risk_level or "").strip().upper()
    if normalized_risk not in {"HIGH", "CRITICAL"}:
        return False

    event_ref = f"sms-threat-{sms_event_id}"
    title = f"SMS Security Alert ({normalized_risk}): {sender}"
    message = warning or f"A {normalized_risk.lower()}-risk SMS message from {sender} was flagged by VaaniShield."
    metadata = json.dumps(
        {
            "source": "android_sms_threat_intelligence",
            "sms_event_id": str(sms_event_id),
            "risk_level": normalized_risk,
            "sender": sender,
        }
    )
    now_text = _now_iso()

    owns_conn = conn is None
    active_conn = get_connection() if owns_conn else conn
    try:
        cur = active_conn.execute(
            """
            INSERT OR IGNORE INTO notifications (
                notification_id, customer_key, category, title, message,
                event_ref, is_read, created_at, metadata_json
            ) VALUES (?, ?, 'security_alert', ?, ?, ?, 0, ?, ?)
            """,
            (
                str(uuid4()),
                customer_key,
                title[:200],
                message[:500],
                event_ref,
                now_text,
                metadata,
            ),
        )
        if owns_conn:
            active_conn.commit()
        return cur.rowcount > 0
    finally:
        if owns_conn:
            active_conn.close()


