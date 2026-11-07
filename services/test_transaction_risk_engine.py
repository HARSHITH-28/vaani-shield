"""Pure offline tests for the deterministic transaction-risk engine."""

import unittest
from datetime import datetime, timedelta, timezone

from services.transaction_risk_engine import (
    ENGINE_VERSION,
    MAX_HISTORY_SNAPSHOTS,
    TransactionRiskContext,
    TransactionSnapshot,
    analyze_transaction_risk,
)


BASE_TIME = datetime(2026, 10, 31, 12, 0, 0)


def tx(
    *, amount=100, merchant="Familiar Market", offset_days=-1, offset_minutes=0,
    customer="customer-a", account="account-a", date=None, reference=None,
    transaction_type="DEBIT", status="SUCCESS",
):
    return TransactionSnapshot(
        customer_scope=customer,
        account_scope=account,
        amount=amount,
        transaction_type=transaction_type,
        transaction_date=(date if date is not None else BASE_TIME + timedelta(days=offset_days, minutes=offset_minutes)),
        merchant=merchant,
        category="SHOPPING",
        status=status,
        transaction_ref=reference,
    )


def baseline(count=12, *, aware=False, amount=100, merchant="Familiar Market"):
    items = []
    for index in range(count):
        days_ago = max(1, 30 - (index * 30 // max(count, 1)))
        stamp = BASE_TIME - timedelta(days=days_ago)
        if aware:
            stamp = stamp.replace(tzinfo=timezone.utc)
        items.append(tx(amount=amount, merchant=merchant, date=stamp, reference=f"hist-{index}"))
    return items


class TransactionRiskEngineTests(unittest.TestCase):
    def test_ordinary_familiar_merchant_is_low(self):
        result = analyze_transaction_risk(
            tx(), baseline(),
        )
        self.assertEqual(result.risk_level, "LOW")
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(result.indicators, ())
        self.assertEqual(result.history_sufficiency.level, "ADEQUATE")
        self.assertIn("does not prove", result.explanation)

    def test_large_but_plausible_transaction_is_not_called_fraud(self):
        result = analyze_transaction_risk(tx(amount=250), baseline(),)
        self.assertEqual(result.risk_level, "LOW")
        self.assertNotIn("fraud is confirmed", result.warning.casefold())

    def test_first_seen_merchant_alone_is_low(self):
        result = analyze_transaction_risk(
            tx(merchant="New Corner Shop"), baseline(),
        )
        self.assertEqual(result.risk_level, "LOW")
        self.assertEqual(result.risk_score, 10)
        self.assertEqual([item.indicator_code for item in result.indicators], ["first_seen_merchant"])

    def test_large_amount_with_strong_historical_deviation_is_medium_alone(self):
        result = analyze_transaction_risk(tx(amount=10_000), baseline(),)
        self.assertEqual(result.risk_level, "MEDIUM")
        self.assertEqual(result.risk_score, 25)
        indicator = result.indicators[0]
        self.assertEqual(indicator.indicator_code, "unusual_amount")
        self.assertIn("above the median", indicator.explanation)

    def test_multiple_transactions_within_ten_minutes_are_flagged(self):
        history = baseline()
        history.extend([
            tx(offset_days=0, offset_minutes=-8, reference="rapid-1"),
            tx(offset_days=0, offset_minutes=-3, reference="rapid-2"),
        ])
        result = analyze_transaction_risk(tx(offset_days=0), history)
        self.assertEqual(result.risk_level, "MEDIUM")
        self.assertIn("rapid_transaction_cluster", {item.indicator_code for item in result.indicators})

    def test_unusual_daily_frequency_is_flagged_separately_from_short_burst(self):
        history = baseline(16)
        history.extend([
            tx(offset_days=0, offset_minutes=-60, reference="freq-1"),
            tx(offset_days=0, offset_minutes=-45, reference="freq-2"),
            tx(offset_days=0, offset_minutes=-30, reference="freq-3"),
            tx(offset_days=0, offset_minutes=-15, reference="freq-4"),
        ])
        result = analyze_transaction_risk(tx(offset_days=0), history)
        self.assertIn("unusual_frequency", {item.indicator_code for item in result.indicators})
        self.assertNotIn("rapid_transaction_cluster", {item.indicator_code for item in result.indicators})

    def test_insufficient_history_is_limited_and_empty_history_is_unknown(self):
        limited = analyze_transaction_risk(tx(), baseline(3))
        self.assertEqual(limited.risk_level, "UNKNOWN")
        self.assertEqual(limited.history_sufficiency.level, "LIMITED")
        self.assertEqual(limited.evidence_strength, "NONE")

        empty = analyze_transaction_risk(tx(), [])
        self.assertEqual(empty.risk_level, "UNKNOWN")
        self.assertEqual(empty.history_sufficiency.level, "INSUFFICIENT")
        self.assertIn("insufficient", empty.warning.casefold())

    def test_missing_or_malformed_timestamps_do_not_create_time_indicators(self):
        history = baseline()
        malformed = [
            tx(date=None, reference="no-date"),
            tx(date="not-a-timestamp", reference="bad-date"),
        ]
        result = analyze_transaction_risk(
            tx(date="not-a-timestamp"), history + malformed,
        )
        codes = {item.indicator_code for item in result.indicators}
        self.assertNotIn("unusual_timing", codes)
        self.assertNotIn("rapid_transaction_cluster", codes)
        self.assertEqual(result.history_sufficiency.level, "ADEQUATE")

    def test_naive_timestamps_never_trigger_time_of_day_risk(self):
        result = analyze_transaction_risk(
            tx(date=BASE_TIME.replace(hour=2)), baseline(),
        )
        self.assertNotIn("unusual_timing", {item.indicator_code for item in result.indicators})

    def test_different_customer_or_account_history_is_excluded(self):
        mixed = baseline()
        mixed[0] = tx(customer="customer-b", merchant="New Merchant", reference="other-customer")
        mixed[1] = tx(account="account-b", merchant="New Merchant", reference="other-account")
        result = analyze_transaction_risk(tx(merchant="New Merchant"), mixed)
        self.assertEqual(result.history_sufficiency.matching_history_count, 10)
        self.assertEqual(result.history_sufficiency.excluded_history_count, 2)
        self.assertIn("first_seen_merchant", {item.indicator_code for item in result.indicators})
        serialized = str(result.to_dict())
        self.assertNotIn("customer-a", serialized)
        self.assertNotIn("account-a", serialized)
        self.assertNotIn("transaction_id", serialized)

    def test_multiple_independent_indicators_can_reach_critical(self):
        history = baseline(12, aware=True)
        history.extend([
            tx(amount=100, merchant="Familiar Market", date=(BASE_TIME - timedelta(minutes=6)).replace(tzinfo=timezone.utc), reference="cluster-1"),
            tx(amount=100, merchant="Familiar Market", date=(BASE_TIME - timedelta(minutes=3)).replace(tzinfo=timezone.utc), reference="cluster-2"),
        ])
        selected = tx(
            amount=10_000,
            merchant="New Merchant",
            date=BASE_TIME.replace(hour=4, tzinfo=timezone.utc),
            reference="selected-critical",
        )
        # Keep the clustered events close to the selected event for this scenario.
        history[-2] = tx(amount=100, merchant="Familiar Market", date=BASE_TIME.replace(hour=3, minute=54, tzinfo=timezone.utc), reference="cluster-1")
        history[-1] = tx(amount=100, merchant="Familiar Market", date=BASE_TIME.replace(hour=3, minute=57, tzinfo=timezone.utc), reference="cluster-2")
        result = analyze_transaction_risk(selected, history)
        self.assertEqual(result.risk_score, 65)
        self.assertEqual(result.risk_level, "CRITICAL")
        self.assertEqual(
            {item.indicator_code for item in result.indicators},
            {"unusual_amount", "first_seen_merchant", "rapid_transaction_cluster"},
        )

    def test_reported_scam_association_is_not_scoring_evidence(self):
        history = baseline()
        ordinary = analyze_transaction_risk(tx(), history)
        reported = analyze_transaction_risk(
            tx(), history,
            context=TransactionRiskContext(customer_reported_scam_association=True),
        )
        self.assertEqual(ordinary.risk_score, reported.risk_score)
        self.assertEqual(ordinary.risk_level, reported.risk_level)
        self.assertIn("not independently verified", reported.context_notes[0])

    def test_repeat_analysis_is_deterministic_and_json_friendly(self):
        selected = tx(amount=10_000, merchant="New Merchant")
        history = baseline()
        first = analyze_transaction_risk(selected, history)
        second = analyze_transaction_risk(selected, history)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.engine_version, ENGINE_VERSION)
        self.assertIsInstance(first.to_dict()["risk_score"], int)
        self.assertIsInstance(first.to_dict()["indicators"], list)

    def test_score_band_boundaries_low_medium_high_critical_and_unknown(self):
        cases = (
            (analyze_transaction_risk(tx(), baseline()), "LOW"),
            (analyze_transaction_risk(tx(amount=10_000), baseline()), "MEDIUM"),
        )
        for result, expected in cases:
            self.assertEqual(result.risk_level, expected)

        high_history = baseline()
        high_history.extend([
            tx(offset_days=0, offset_minutes=-8, reference="high-1"),
            tx(offset_days=0, offset_minutes=-3, reference="high-2"),
        ])
        high = analyze_transaction_risk(tx(amount=10_000, offset_days=0), high_history)
        self.assertEqual(high.risk_score, 55)
        self.assertEqual(high.risk_level, "HIGH")

        critical_history = baseline(12, aware=True)
        critical_history.extend([
            tx(date=BASE_TIME.replace(hour=3, minute=54, tzinfo=timezone.utc), reference="c1"),
            tx(date=BASE_TIME.replace(hour=3, minute=57, tzinfo=timezone.utc), reference="c2"),
        ])
        critical = analyze_transaction_risk(
            tx(amount=10_000, merchant="New Merchant", date=BASE_TIME.replace(hour=4, tzinfo=timezone.utc)),
            critical_history,
        )
        self.assertEqual(critical.risk_level, "CRITICAL")
        self.assertEqual(analyze_transaction_risk(tx(), []).risk_level, "UNKNOWN")

    def test_malformed_selected_amount_is_rejected(self):
        selected = {
            "customer_scope": "customer-a", "account_scope": "account-a",
            "amount": float("nan"), "transaction_type": "DEBIT",
            "transaction_date": BASE_TIME,
        }
        with self.assertRaisesRegex(ValueError, "Selected transaction snapshot is malformed"):
            analyze_transaction_risk(selected, baseline())

    def test_history_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            analyze_transaction_risk(tx(), baseline(MAX_HISTORY_SNAPSHOTS + 1))


if __name__ == "__main__":
    unittest.main()
