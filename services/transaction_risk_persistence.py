"""Owner-scoped public references and persistence for transaction-risk results."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import re
import secrets
import sqlite3
from uuid import UUID, uuid4

from services import database
from services.principal import CustomerPrincipal
from services.transaction_risk_service import assess_customer_transaction_with_fingerprint


PAGE_MAX = 100
PUBLIC_REF_PREFIX = "TR-"
PUBLIC_REF_HEX_LENGTH = 48


class InvalidTransactionRiskRequest(ValueError):
    """Malformed public input or pagination cursor."""


class TransactionRiskTransactionNotFound(LookupError):
    """Selected transaction is absent or not owned by this principal."""


class TransactionRiskAssessmentNotFound(LookupError):
    """Assessment is absent or owned by a different principal."""


class TransactionRiskAssessmentError(RuntimeError):
    """Safe boundary for persistence or engine failures."""


def _owner(principal: CustomerPrincipal) -> str:
    if not isinstance(principal, CustomerPrincipal):
        raise TypeError("An authenticated customer principal is required.")
    if principal.customer_key not in frozenset(database.DEMO_CUSTOMER_KEYS.values()):
        raise TypeError("An authenticated customer principal is required.")
    if principal.expires_at <= datetime.now(timezone.utc):
        raise TypeError("An active authenticated customer principal is required.")
    return principal.customer_key


def ensure_transaction_public_refs(
    customer_key: str,
    transaction_ids: list[int] | tuple[int, ...],
) -> dict[int, str]:
    """Create/retrieve random public refs for transactions owned by ``customer_key``.

    Internal transaction IDs are accepted only for trusted backend callers.
    Unknown and non-owned IDs are omitted from the returned mapping.
    """
    if customer_key not in frozenset(database.DEMO_CUSTOMER_KEYS.values()):
        raise TypeError("An authenticated customer key is required.")
    normalized_ids: list[int] = []
    for value in transaction_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        normalized_ids.append(value)

    conn = database.get_connection()
    result: dict[int, str] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for transaction_id in dict.fromkeys(normalized_ids):
            owned = conn.execute(
                """SELECT t.transaction_id
                   FROM transactions t
                   JOIN accounts a ON a.account_id = t.account_id
                   JOIN customers c ON c.customer_id = a.customer_id
                   WHERE t.transaction_id = ? AND c.customer_key = ?""",
                (transaction_id, customer_key),
            ).fetchone()
            if owned is None:
                continue
            row = conn.execute(
                "SELECT transaction_ref FROM transaction_public_refs WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            while row is None:
                public_ref = PUBLIC_REF_PREFIX + secrets.token_hex(PUBLIC_REF_HEX_LENGTH // 2).upper()
                conn.execute(
                    "INSERT OR IGNORE INTO transaction_public_refs "
                    "(transaction_ref, transaction_id, created_at) VALUES (?, ?, ?)",
                    (public_ref, transaction_id, datetime.now(timezone.utc).isoformat()),
                )
                row = conn.execute(
                    "SELECT transaction_ref FROM transaction_public_refs WHERE transaction_id = ?",
                    (transaction_id,),
                ).fetchone()
            result[transaction_id] = row["transaction_ref"]
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        decoded = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
        offset = int(decoded)
        if offset < 0 or str(offset) != decoded:
            raise ValueError
        return offset
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise InvalidTransactionRiskRequest("Invalid pagination cursor.") from None


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).rstrip(b"=").decode("ascii")


def _assessment_result(row) -> dict:
    return json.loads(row["assessment_json"])


def create_transaction_assessment(
    principal: CustomerPrincipal,
    transaction_ref: str,
) -> dict:
    """Assess and persist a transaction, idempotent for an unchanged history snapshot."""
    owner = _owner(principal)
    if not isinstance(transaction_ref, str) or not re.fullmatch(r"TR-[A-F0-9]{48}", transaction_ref):
        raise InvalidTransactionRiskRequest("Invalid transaction reference.")

    conn = database.get_connection()
    try:
        row = conn.execute(
            """SELECT t.transaction_id, c.customer_id
               FROM transaction_public_refs p
               JOIN transactions t ON t.transaction_id = p.transaction_id
               JOIN accounts a ON a.account_id = t.account_id
               JOIN customers c ON c.customer_id = a.customer_id
               WHERE p.transaction_ref = ? AND c.customer_key = ?""",
            (transaction_ref, owner),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise TransactionRiskTransactionNotFound

    try:
        result = assess_customer_transaction_with_fingerprint(
            int(row["customer_id"]), int(row["transaction_id"])
        )
        if result is None:
            raise TransactionRiskTransactionNotFound
        fingerprint = result.pop("history_fingerprint")
        assessment = result["assessment"]
        assessment_id = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        conn = database.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO transaction_risk_assessments
                   (assessment_id, customer_key, transaction_id, assessment_json,
                    engine_version, history_fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    assessment_id, owner, int(row["transaction_id"]),
                    json.dumps(assessment, ensure_ascii=False, sort_keys=True),
                    assessment["engine_version"], fingerprint, created_at,
                ),
            )
            stored = conn.execute(
                """SELECT assessment_id, assessment_json, created_at
                   FROM transaction_risk_assessments
                   WHERE customer_key = ? AND transaction_id = ? AND history_fingerprint = ?""",
                (owner, int(row["transaction_id"]), fingerprint),
            ).fetchone()
            reused = stored["assessment_id"] != assessment_id
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        result["transaction"]["transaction_ref"] = transaction_ref
        return {
            "assessment_id": stored["assessment_id"],
            "transaction": result["transaction"],
            "assessment": json.loads(stored["assessment_json"]),
            "created_at": stored["created_at"],
            "reused": reused,
        }
    except TransactionRiskTransactionNotFound:
        raise
    except Exception as exc:
        raise TransactionRiskAssessmentError from exc


def list_transaction_assessments(
    principal: CustomerPrincipal,
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    owner = _owner(principal)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_MAX:
        raise InvalidTransactionRiskRequest("Page limit must be between 1 and 100.")
    offset = _decode_cursor(cursor)
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """SELECT r.assessment_id, r.assessment_json, r.engine_version, r.created_at,
                      p.transaction_ref, t.transaction_type, t.amount, t.merchant,
                      t.category, t.status, t.transaction_date
               FROM transaction_risk_assessments r
               JOIN transaction_public_refs p ON p.transaction_id = r.transaction_id
               JOIN transactions t ON t.transaction_id = r.transaction_id
               WHERE r.customer_key = ?
               ORDER BY r.created_at DESC, r.assessment_id DESC
               LIMIT ? OFFSET ?""",
            (owner, limit + 1, offset),
        ).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = []
    for row in page_rows:
        assessment = _assessment_result(row)
        items.append({
            "assessment_id": row["assessment_id"],
            "transaction_ref": row["transaction_ref"],
            "transaction": {
                "transaction_ref": row["transaction_ref"],
                "transaction_type": row["transaction_type"],
                "amount": row["amount"],
                "merchant": row["merchant"],
                "category": row["category"],
                "status": row["status"],
                "transaction_date": row["transaction_date"],
            },
            "risk_level": assessment["risk_level"],
            "risk_score": assessment["risk_score"],
            "evidence_strength": assessment["evidence_strength"],
            "engine_version": row["engine_version"],
            "created_at": row["created_at"],
        })
    return {
        "items": items,
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }


def get_transaction_assessment(
    principal: CustomerPrincipal,
    assessment_id: UUID,
) -> dict:
    owner = _owner(principal)
    conn = database.get_connection()
    try:
        row = conn.execute(
            """SELECT r.assessment_id, r.assessment_json, r.created_at,
                      p.transaction_ref, t.transaction_type, t.amount, t.merchant,
                      t.category, t.status, t.transaction_date
               FROM transaction_risk_assessments r
               JOIN transaction_public_refs p ON p.transaction_id = r.transaction_id
               JOIN transactions t ON t.transaction_id = r.transaction_id
               WHERE r.assessment_id = ? AND r.customer_key = ?""",
            (str(assessment_id), owner),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise TransactionRiskAssessmentNotFound
    return {
        "assessment_id": row["assessment_id"],
        "transaction_ref": row["transaction_ref"],
        "transaction": {
            "transaction_ref": row["transaction_ref"],
            "transaction_type": row["transaction_type"],
            "amount": row["amount"],
            "merchant": row["merchant"],
            "category": row["category"],
            "status": row["status"],
            "transaction_date": row["transaction_date"],
        },
        "assessment": json.loads(row["assessment_json"]),
        "created_at": row["created_at"],
    }
