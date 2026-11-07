"""Owner-scoped transaction retrieval for the deterministic risk engine.

The caller must pass the authenticated customer's internal SQLite ID. The
transaction ID is accepted only as a trusted internal service argument; neither
identifier is included in the returned projection. Public opaque references
belong to a later API/persistence phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import hashlib
import json
import sqlite3
from typing import Any, Callable

from services.database import get_connection
from services.transaction_risk_engine import (
    MAX_HISTORY_SNAPSHOTS,
    TransactionRiskAssessment,
    TransactionSnapshot,
    analyze_transaction_risk,
)


HISTORY_WINDOW_DAYS = 180
HISTORY_ROW_LIMIT = MAX_HISTORY_SNAPSHOTS


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _comparable(left: datetime, right: datetime) -> tuple[datetime, datetime] | None:
    """Normalize comparable timestamps; unknown timezone relationships stay unknown."""
    left_aware = left.tzinfo is not None and left.utcoffset() is not None
    right_aware = right.tzinfo is not None and right.utcoffset() is not None
    if left_aware != right_aware:
        return None
    if left_aware:
        return left.astimezone(timezone.utc), right.astimezone(timezone.utc)
    return left, right


def _inside_history_window(
    selected_value: Any,
    history_value: Any,
) -> bool:
    """Keep prior rows in the window; retain unparseable dates for engine handling.

    A malformed selected date disables temporal filtering, while the database
    row cap still bounds history. A missing/malformed historical date is kept
    as a non-temporal snapshot; the engine will not use it for timing signals.
    """
    selected = _parse_timestamp(selected_value)
    historical = _parse_timestamp(history_value)
    if selected is None or historical is None:
        return True
    pair = _comparable(selected, historical)
    if pair is None:
        return True
    selected_cmp, history_cmp = pair
    cutoff = selected_cmp - timedelta(days=HISTORY_WINDOW_DAYS)
    return cutoff <= history_cmp <= selected_cmp


def _snapshot(row: sqlite3.Row | dict[str, Any], *, customer_id: int) -> TransactionSnapshot:
    """Map one row to the exact Phase 1 snapshot contract."""
    return TransactionSnapshot(
        customer_scope=str(customer_id),
        account_scope=int(row["account_id"]),
        amount=row["amount"],
        transaction_type=row["transaction_type"],
        transaction_date=row["transaction_date"],
        merchant=row["merchant"],
        category=row["category"],
        status=row["status"],
        # reference_id is deliberately not copied: it may contain
        # account-derived information and is not a public transaction ID.
    )


def _transaction_projection(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Return only the transaction fields needed for a future risk API."""
    return {
        "transaction_type": row["transaction_type"],
        "amount": row["amount"],
        "merchant": row["merchant"],
        "category": row["category"],
        "status": row["status"],
        "transaction_date": row["transaction_date"],
    }


def assess_customer_transaction(
    customer_id: int,
    transaction_id: int,
    *,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    _include_history_fingerprint: bool = False,
) -> dict[str, Any] | None:
    """Assess an owned transaction against bounded history from its account.

    ``customer_id`` must be derived by the trusted backend from the authenticated
    principal. ``transaction_id`` is internal-only. Returns ``None`` both when
    the transaction is unknown and when it is not owned by the customer, so the
    service does not distinguish another customer's transaction to its caller.

    History is limited to the same account (and therefore the same customer),
    ordered by timestamp then internal row ID, capped at 500 rows, and filtered
    to the 180 days preceding the selected transaction when timestamps permit.
    """
    if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id <= 0:
        raise ValueError("A valid authenticated customer ID is required.")
    if isinstance(transaction_id, bool) or not isinstance(transaction_id, int) or transaction_id <= 0:
        raise ValueError("A valid internal transaction ID is required.")

    connect = connection_factory or get_connection
    conn = connect()
    try:
        conn.row_factory = sqlite3.Row
        selected_row = conn.execute(
            """SELECT t.transaction_id, t.account_id, a.customer_id,
                      t.transaction_type, t.amount, t.merchant, t.category,
                      t.status, t.transaction_date
               FROM transactions AS t
               JOIN accounts AS a ON a.account_id = t.account_id
               WHERE t.transaction_id = ? AND a.customer_id = ?""",
            (transaction_id, customer_id),
        ).fetchone()
        if selected_row is None:
            return None

        history_rows = conn.execute(
            """SELECT t.transaction_id, t.account_id, a.customer_id,
                      t.transaction_type, t.amount, t.merchant, t.category,
                      t.status, t.transaction_date
               FROM transactions AS t
               JOIN accounts AS a ON a.account_id = t.account_id
               WHERE a.customer_id = ?
                 AND t.account_id = ?
                 AND t.transaction_id != ?
               ORDER BY julianday(t.transaction_date) DESC,
                        t.transaction_date DESC,
                        t.transaction_id DESC
               LIMIT ?""",
            (customer_id, selected_row["account_id"], transaction_id, HISTORY_ROW_LIMIT),
        ).fetchall()
    finally:
        conn.close()

    selected_snapshot = _snapshot(selected_row, customer_id=customer_id)
    bounded_history = [
        _snapshot(row, customer_id=customer_id)
        for row in history_rows
        if _inside_history_window(selected_row["transaction_date"], row["transaction_date"])
    ]
    assessment: TransactionRiskAssessment = analyze_transaction_risk(
        selected_snapshot,
        bounded_history,
    )
    result = {
        "transaction": _transaction_projection(selected_row),
        "assessment": assessment.to_dict(),
    }
    if _include_history_fingerprint:
        fingerprint_input = {
            "engine_version": assessment.engine_version,
            "selected": asdict(selected_snapshot),
            "history": [asdict(item) for item in bounded_history],
        }
        encoded = json.dumps(
            fingerprint_input, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        result["history_fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return result


def assess_customer_transaction_with_fingerprint(
    customer_id: int,
    transaction_id: int,
    *,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
) -> dict[str, Any] | None:
    """Internal Phase 3 variant including a non-reversible context fingerprint.

    The ordinary Phase 2 result contract is unchanged. The hash contains the
    exact risk-engine snapshots and version, allowing safe duplicate reuse only
    while the selected transaction and bounded history are unchanged.
    """
    return assess_customer_transaction(
        customer_id,
        transaction_id,
        connection_factory=connection_factory,
        _include_history_fingerprint=True,
    )

