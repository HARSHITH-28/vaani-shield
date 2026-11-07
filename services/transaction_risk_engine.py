"""Deterministic transaction-pattern screening over caller-supplied snapshots.

This module intentionally has no database, network, LLM, or authentication
dependency. The caller must supply a selected transaction and history scoped to
the same customer and account. Scope identifiers are used only for comparison
and are never returned. A risk score is a versioned rule score, not a fraud
probability or a finding that a transaction is fraudulent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from statistics import median
from typing import Any, Iterable, Mapping


ENGINE_VERSION = "1.0.0"
MAX_HISTORY_SNAPSHOTS = 500
MIN_AMOUNT_HISTORY = 10
MIN_MERCHANT_HISTORY = 5
MIN_FREQUENCY_HISTORY = 10
MIN_FREQUENCY_COVERAGE_DAYS = 14

# All weights are bounded rule contributions, not percentages or probabilities.
SCORE_WEIGHTS = {
    "unusual_amount": 25,
    "first_seen_merchant": 10,
    "unusual_frequency": 20,
    "rapid_transaction_cluster": 30,
    "unusual_timing": 5,
}
RISK_SCORE_BANDS = {
    "LOW": (0, 14),
    "MEDIUM": (15, 39),
    "HIGH": (40, 59),
    "CRITICAL": (60, 100),
}


@dataclass(frozen=True, slots=True)
class TransactionSnapshot:
    """Minimal transaction evidence plus non-public scoping values.

    ``customer_scope`` and ``account_scope`` must be supplied by a trusted
    caller. They may be stable keys and internal account IDs respectively;
    this pure engine only compares them and never includes them in its output.
    """

    customer_scope: str
    account_scope: str | int
    amount: float
    transaction_type: str
    transaction_date: datetime | str | None
    merchant: str | None = None
    category: str | None = None
    status: str | None = None
    transaction_ref: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TransactionSnapshot":
        if not isinstance(value, Mapping):
            raise ValueError("Transaction snapshot must be a mapping.")
        try:
            customer_scope = value["customer_scope"]
            account_scope = value["account_scope"]
            amount = value["amount"]
            transaction_type = value["transaction_type"]
        except KeyError as exc:
            raise ValueError("Transaction snapshot is missing required fields.") from exc
        return cls(
            customer_scope=customer_scope,
            account_scope=account_scope,
            amount=amount,
            transaction_type=transaction_type,
            transaction_date=value.get("transaction_date"),
            merchant=value.get("merchant"),
            category=value.get("category"),
            status=value.get("status"),
            transaction_ref=value.get("transaction_ref", value.get("reference_id")),
        )


@dataclass(frozen=True, slots=True)
class TransactionRiskContext:
    """Explicit caller-supplied context; a reported link is not verified evidence."""

    customer_reported_scam_association: bool = False


@dataclass(frozen=True, slots=True)
class TransactionRiskIndicator:
    indicator_code: str
    severity: str
    explanation: str
    evidence: tuple[str, ...]
    score_contribution: int


@dataclass(frozen=True, slots=True)
class HistorySufficiency:
    level: str
    matching_history_count: int
    minimum_for_amount_baseline: int
    time_coverage_days: int | None
    frequency_baseline_ready: bool
    excluded_history_count: int


@dataclass(frozen=True, slots=True)
class TransactionRiskAssessment:
    risk_level: str
    risk_score: int
    indicators: tuple[TransactionRiskIndicator, ...]
    evidence: tuple[str, ...]
    evidence_strength: str
    explanation: str
    warning: str
    recommended_actions: tuple[str, ...]
    history_sufficiency: HistorySufficiency
    engine_version: str = ENGINE_VERSION
    context_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-friendly assessment data without transaction identities."""
        result = asdict(self)
        result["indicators"] = [asdict(item) for item in self.indicators]
        result["history_sufficiency"] = asdict(self.history_sufficiency)
        return result


@dataclass(frozen=True, slots=True)
class _NormalizedSnapshot:
    customer_scope: str
    account_scope: str
    amount: float
    transaction_type: str
    transaction_date: datetime | None
    merchant: str | None
    category: str | None
    status: str
    transaction_ref: str | None


def _parse_date(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_snapshot(
    value: TransactionSnapshot | Mapping[str, Any], *, selected: bool
) -> _NormalizedSnapshot | None:
    try:
        snapshot = value if isinstance(value, TransactionSnapshot) else TransactionSnapshot.from_mapping(value)
        if not isinstance(snapshot.customer_scope, str) or not snapshot.customer_scope.strip():
            raise ValueError("Customer scope is required.")
        if isinstance(snapshot.account_scope, bool) or not isinstance(snapshot.account_scope, (str, int)):
            raise ValueError("Account scope is required.")
        if not str(snapshot.account_scope).strip():
            raise ValueError("Account scope is required.")
        if isinstance(snapshot.amount, bool):
            raise ValueError("Transaction amount must be finite and non-negative.")
        amount = float(snapshot.amount)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Transaction amount must be finite and non-negative.")
        if not isinstance(snapshot.transaction_type, str) or not snapshot.transaction_type.strip():
            raise ValueError("Transaction type is required.")
        transaction_type = snapshot.transaction_type.strip().upper()
        if transaction_type not in {"DEBIT", "CREDIT"}:
            raise ValueError("Transaction type must be DEBIT or CREDIT.")
        merchant = snapshot.merchant.strip() if isinstance(snapshot.merchant, str) else None
        category = snapshot.category.strip() if isinstance(snapshot.category, str) else None
        status = snapshot.status.strip().upper() if isinstance(snapshot.status, str) else "UNKNOWN"
        reference = snapshot.transaction_ref.strip() if isinstance(snapshot.transaction_ref, str) else None
        return _NormalizedSnapshot(
            customer_scope=snapshot.customer_scope.strip(),
            account_scope=str(snapshot.account_scope).strip(),
            amount=amount,
            transaction_type=transaction_type,
            transaction_date=_parse_date(snapshot.transaction_date),
            merchant=merchant or None,
            category=category or None,
            status=status,
            transaction_ref=reference or None,
        )
    except (TypeError, ValueError, OverflowError):
        if selected:
            raise ValueError("Selected transaction snapshot is malformed.") from None
        return None


def _same_time_basis(selected: datetime, candidate: datetime) -> bool:
    if (selected.tzinfo is None) != (candidate.tzinfo is None):
        return False
    if selected.tzinfo is None:
        return True
    return selected.utcoffset() == candidate.utcoffset()


def _time_delta_seconds(left: datetime, right: datetime) -> float | None:
    if not _same_time_basis(left, right):
        return None
    try:
        if left.tzinfo is not None:
            left = left.astimezone(timezone.utc)
            right = right.astimezone(timezone.utc)
        return abs((left - right).total_seconds())
    except (OverflowError, ValueError):
        return None


def _median_absolute_deviation(values: list[float], center: float) -> float:
    return float(median([abs(value - center) for value in values]))


def _eligible_history(
    selected: _NormalizedSnapshot,
    raw_history: Iterable[TransactionSnapshot | Mapping[str, Any]],
) -> tuple[list[_NormalizedSnapshot], int]:
    try:
        items = list(raw_history)
    except TypeError as exc:
        raise ValueError("Transaction history must be iterable.") from exc
    if len(items) > MAX_HISTORY_SNAPSHOTS:
        raise ValueError(f"Transaction history cannot exceed {MAX_HISTORY_SNAPSHOTS} snapshots.")

    eligible: list[_NormalizedSnapshot] = []
    excluded = 0
    for item in items:
        history_item = _normalize_snapshot(item, selected=False)
        if history_item is None:
            excluded += 1
            continue
        if (
            history_item.customer_scope != selected.customer_scope
            or history_item.account_scope != selected.account_scope
        ):
            excluded += 1
            continue
        if (
            selected.transaction_ref
            and history_item.transaction_ref == selected.transaction_ref
        ):
            excluded += 1
            continue
        # Successful debits are the comparable purchase history. A successful
        # status is not interpreted as proof of legitimacy.
        if history_item.transaction_type != "DEBIT" or history_item.status != "SUCCESS":
            continue
        eligible.append(history_item)
    return eligible, excluded


def _history_sufficiency(history: list[_NormalizedSnapshot], excluded: int) -> HistorySufficiency:
    dates = [item.transaction_date.date() for item in history if item.transaction_date is not None]
    coverage = None
    if len(dates) >= 2:
        coverage = max((max(dates) - min(dates)).days, 0)
    frequency_ready = (
        len(history) >= MIN_FREQUENCY_HISTORY
        and coverage is not None
        and coverage >= MIN_FREQUENCY_COVERAGE_DAYS
    )
    adequate = len(history) >= MIN_AMOUNT_HISTORY and coverage is not None and coverage >= MIN_FREQUENCY_COVERAGE_DAYS
    if adequate:
        level = "ADEQUATE"
    elif history:
        level = "LIMITED"
    else:
        level = "INSUFFICIENT"
    return HistorySufficiency(
        level=level,
        matching_history_count=len(history),
        minimum_for_amount_baseline=MIN_AMOUNT_HISTORY,
        time_coverage_days=coverage,
        frequency_baseline_ready=frequency_ready,
        excluded_history_count=excluded,
    )


def _amount_indicator(selected: _NormalizedSnapshot, history: list[_NormalizedSnapshot]):
    amounts = [item.amount for item in history if item.amount > 0]
    if len(amounts) < MIN_AMOUNT_HISTORY or selected.amount <= 0:
        return None
    center = float(median(amounts))
    deviation = _median_absolute_deviation(amounts, center)
    threshold = max(center * 3.0, center + 6.0 * deviation)
    if selected.amount < threshold:
        return None
    return TransactionRiskIndicator(
        indicator_code="unusual_amount",
        severity="MEDIUM",
        explanation="The amount is substantially above the median of the available successful debit history.",
        evidence=(
            f"Selected amount {selected.amount:.2f}; historical median {center:.2f}; rule threshold {threshold:.2f}.",
        ),
        score_contribution=SCORE_WEIGHTS["unusual_amount"],
    )


def _merchant_indicator(selected: _NormalizedSnapshot, history: list[_NormalizedSnapshot]):
    if len(history) < MIN_MERCHANT_HISTORY or not selected.merchant:
        return None
    observed = {
        item.merchant.casefold()
        for item in history
        if item.merchant
    }
    if selected.merchant.casefold() in observed:
        return None
    return TransactionRiskIndicator(
        indicator_code="first_seen_merchant",
        severity="LOW",
        explanation="This merchant does not appear in the available matching account history; limited history can make this comparison incomplete.",
        evidence=(f"Selected merchant {selected.merchant!r} was not present in {len(history)} matching historical debits.",),
        score_contribution=SCORE_WEIGHTS["first_seen_merchant"],
    )


def _velocity_indicators(selected: _NormalizedSnapshot, history: list[_NormalizedSnapshot], sufficiency: HistorySufficiency):
    if selected.transaction_date is None:
        return None
    dated = [item for item in history if item.transaction_date is not None]
    comparable = [
        (item, _time_delta_seconds(selected.transaction_date, item.transaction_date))
        for item in dated
    ]
    comparable = [(item, seconds) for item, seconds in comparable if seconds is not None]
    burst_members = [item for item, seconds in comparable if seconds <= 10 * 60]
    if len(burst_members) >= 2:
        return TransactionRiskIndicator(
            indicator_code="rapid_transaction_cluster",
            severity="HIGH",
            explanation="Several successful debits occurred within ten minutes of the selected transaction.",
            evidence=(
                f"Observed {len(burst_members) + 1} matching debits within ten minutes, including the selected transaction.",
            ),
            score_contribution=SCORE_WEIGHTS["rapid_transaction_cluster"],
        )

    if not sufficiency.frequency_baseline_ready:
        return None
    prior_24h = [
        item for item, seconds in comparable
        if seconds <= 24 * 60 * 60
        and item.transaction_date is not None
        and item.transaction_date <= selected.transaction_date
    ]
    observed_count = len(prior_24h) + 1
    dated_history = [item.transaction_date for item in dated if item.transaction_date]
    start_date = min(dated_history).date()
    end_date = max(dated_history).date()
    total_days = max((end_date - start_date).days + 1, 1)
    average_per_day = len(dated) / total_days
    threshold = max(5, math.ceil(average_per_day + 3.0 * math.sqrt(average_per_day + 1.0)))
    if observed_count < threshold:
        return None
    return TransactionRiskIndicator(
        indicator_code="unusual_frequency",
        severity="MEDIUM",
        explanation="The number of matching debits in the preceding 24 hours is above the configured activity threshold.",
        evidence=(
            f"Observed {observed_count} debits in the selected 24-hour window; rule threshold {threshold}; history spans {total_days} calendar days.",
        ),
        score_contribution=SCORE_WEIGHTS["unusual_frequency"],
    )


def _timing_indicator(selected: _NormalizedSnapshot, history: list[_NormalizedSnapshot]):
    selected_date = selected.transaction_date
    if selected_date is None or selected_date.tzinfo is None:
        return None
    dated = [item.transaction_date for item in history if item.transaction_date is not None]
    comparable = [item for item in dated if item.tzinfo is not None]
    if len(comparable) < MIN_AMOUNT_HISTORY:
        return None
    selected_utc = selected_date.astimezone(timezone.utc)
    historical_utc = [item.astimezone(timezone.utc) for item in comparable]
    # A known-offset time is required. If history already contains activity
    # within two hours of this time of day, it is not an unusual-time signal.
    distances = [
        min(abs((selected_utc.hour + selected_utc.minute / 60) - (item.hour + item.minute / 60)),
            24 - abs((selected_utc.hour + selected_utc.minute / 60) - (item.hour + item.minute / 60)))
        for item in historical_utc
    ]
    if not distances or min(distances) <= 2:
        return None
    return TransactionRiskIndicator(
        indicator_code="unusual_timing",
        severity="LOW",
        explanation="The transaction time differs from the times observed in the available timezone-aware history.",
        evidence=(
            f"Selected UTC hour {selected_utc.hour:02d}; nearest historical transaction hour is more than two hours away.",
        ),
        score_contribution=SCORE_WEIGHTS["unusual_timing"],
    )


def _risk_level(score: int, indicator_count: int) -> str:
    if score >= RISK_SCORE_BANDS["CRITICAL"][0] and indicator_count >= 3:
        return "CRITICAL"
    if score >= RISK_SCORE_BANDS["HIGH"][0]:
        return "HIGH"
    if score >= RISK_SCORE_BANDS["MEDIUM"][0]:
        return "MEDIUM"
    return "LOW"


def _evidence_strength(history_sufficiency: HistorySufficiency, indicators: list[TransactionRiskIndicator]) -> str:
    if not indicators:
        return "NONE"
    if history_sufficiency.level != "ADEQUATE":
        return "LIMITED"
    if len(indicators) >= 3:
        return "STRONG"
    return "MODERATE"


def _recommendations(indicators: list[TransactionRiskIndicator], risk_level: str) -> tuple[str, ...]:
    codes = {item.indicator_code for item in indicators}
    actions: list[str] = []
    if "unusual_amount" in codes:
        actions.append("Check that the amount and merchant match a purchase you recognize.")
    if "first_seen_merchant" in codes:
        actions.append("Confirm the merchant name independently; a first-seen merchant alone is not evidence of fraud.")
    if codes & {"unusual_frequency", "rapid_transaction_cluster"}:
        actions.append("Review each nearby transaction separately and contact your bank through an official channel if any are unrecognized.")
    if "unusual_timing" in codes:
        actions.append("Verify the recorded time and timezone against your bank's transaction details.")
    if not actions:
        actions.append("Review the transaction in your authenticated banking history; this assessment cannot prove that it is safe.")
    if risk_level in {"HIGH", "CRITICAL"} and not (codes & {"unusual_frequency", "rapid_transaction_cluster"}):
        actions.append("If you do not recognize the transaction, contact your bank using its official contact channel promptly.")
    return tuple(actions)


def analyze_transaction_risk(
    selected_transaction: TransactionSnapshot | Mapping[str, Any],
    historical_transactions: Iterable[TransactionSnapshot | Mapping[str, Any]],
    *,
    context: TransactionRiskContext | None = None,
) -> TransactionRiskAssessment:
    """Analyze one debit against same-customer, same-account history.

    Explicitly mismatched or malformed history snapshots are excluded. A
    selected transaction that lacks scope or has invalid core data is rejected.
    Descriptions, transaction IDs, account numbers, and reported scam narratives
    never contribute to the score.
    """
    selected = _normalize_snapshot(selected_transaction, selected=True)
    assert selected is not None
    history, excluded = _eligible_history(selected, historical_transactions)
    sufficiency = _history_sufficiency(history, excluded)
    notes: tuple[str, ...] = ()
    if context is not None and context.customer_reported_scam_association:
        notes = (
            "The caller reported a connection to a scam conversation; this report was not independently verified and did not change the score.",
        )

    if selected.transaction_type != "DEBIT":
        explanation = "This initial ruleset assesses debit transactions only; no risk level was assigned to this transaction type."
        return TransactionRiskAssessment(
            risk_level="UNKNOWN", risk_score=0, indicators=(), evidence=(),
            evidence_strength="NONE", explanation=explanation,
            warning="This is an automated pattern screen, not a determination of fraud. A successful status does not prove legitimacy.",
            recommended_actions=("Review the transaction in your authenticated banking history or contact your bank through an official channel if concerned.",),
            history_sufficiency=sufficiency, context_notes=notes,
        )

    indicators: list[TransactionRiskIndicator] = []
    for indicator in (
        _amount_indicator(selected, history),
        _merchant_indicator(selected, history),
        _velocity_indicators(selected, history, sufficiency),
        _timing_indicator(selected, history),
    ):
        if indicator is not None:
            indicators.append(indicator)

    score = min(sum(item.score_contribution for item in indicators), 100)
    if sufficiency.level != "ADEQUATE" and not indicators:
        risk_level = "UNKNOWN"
    else:
        risk_level = _risk_level(score, len(indicators))
    if sufficiency.level != "ADEQUATE" and indicators and score < RISK_SCORE_BANDS["MEDIUM"][0]:
        risk_level = "UNKNOWN"

    if risk_level == "UNKNOWN":
        explanation = "There is not enough matching transaction history to establish a meaningful personal baseline."
    elif indicators:
        explanation = (
            f"The rule score is {score} from {len(indicators)} observed indicator(s). "
            "This describes patterns in the supplied data, not a finding that fraud occurred."
        )
    else:
        explanation = "No configured transaction-pattern indicators were found in the supplied history. This does not prove the transaction is legitimate."

    warning = (
        "Automated transaction-risk screening is advisory. It cannot confirm fraud, identify a criminal, "
        "or verify who authorized a transaction. Successful status does not prove legitimacy."
    )
    if risk_level == "UNKNOWN":
        warning = "Risk is UNKNOWN because the available same-account history is insufficient or not comparable. Do not interpret this as proof of safety."

    return TransactionRiskAssessment(
        risk_level=risk_level,
        risk_score=score,
        indicators=tuple(indicators),
        evidence=tuple(evidence for item in indicators for evidence in item.evidence),
        evidence_strength=_evidence_strength(sufficiency, indicators),
        explanation=explanation,
        warning=warning,
        recommended_actions=_recommendations(indicators, risk_level),
        history_sufficiency=sufficiency,
        context_notes=notes,
    )
