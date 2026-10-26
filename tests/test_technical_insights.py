"""Known-value and temporal-boundary checks for derived technical observations."""

from dataclasses import replace
from datetime import date, timedelta
import json
import unittest

from trading_companion import indicators as calculations
from trading_companion.daily_bar import DailyBar
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.technical_insights import (
    aggregate_completed_weeks, build_technical_insights,
)


START = date(2026, 1, 5)
SPECS = (IndicatorSpec("sma", window=3), IndicatorSpec("ema", window=3),
         IndicatorSpec("rsi", window=2), IndicatorSpec("macd", fast=2, slow=3, signal=2))
BEARISH = [10, 10, 10, 11, 15, 14, 13, 12, 13, 14, 16, 15, 14, 13, 14]
BULLISH = [15, 15, 15, 14, 10, 11, 12, 13, 12, 11, 9.9, 11, 12, 13, 12]


class SnapshotSource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def __init__(self, bars):
        self.bars, self.reads = list(bars), 0

    def read_daily_bars(self):
        self.reads += 1
        return self.bars


def daily_rows(closes, *, weekdays=False):
    rows, current = [], START
    for index, close in enumerate(closes):
        if weekdays:
            while current.weekday() >= 5:
                current += timedelta(days=1)
        rows.append(DailyBar("000001.SZ", current, close, close + 0.2,
                             close - 0.2, close, 100 + index, 1000 + index))
        current += timedelta(days=1)
    return rows


def analyze(closes, *, specs=SPECS, weekdays=False, as_of=None):
    bars = daily_rows(closes, weekdays=weekdays)
    request = MultiIndicatorRequest("000001.SZ", START, as_of or bars[-1].trade_date,
                                    specs, "csv", "technical-insights-test")
    source = SnapshotSource(bars)
    result = analyze_indicators(source, request, allow_development_csv=True)
    return request, result, source


def find(insights, key):
    return next(item for item in insights.observations if item.key == key)


class TechnicalInsightsTests(unittest.TestCase):
    def test_known_bearish_divergence_is_candidate_only_with_complete_dependencies(self):
        request, result, source = analyze(BEARISH)
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(insights.code, "technical_insights_computed")
        for kind in ("rsi(window=2)", "macd(fast=2,slow=3,signal=2)"):
            observation = find(insights, f"divergence:{kind}:bearish")
            self.assertEqual(observation.value, "bearish_candidate")
            self.assertIn("规则候选", observation.text)
        self.assertEqual(source.reads, 1)
        facts = {fact.fact_id: fact for fact in insights.facts}
        self.assertEqual(len(facts), len(insights.facts))
        for fact in insights.facts:
            self.assertEqual(fact.instrument_id, request.instrument_id)
            self.assertEqual(fact.request_id, request.request_id)
            self.assertLessEqual(fact.trade_date, request.as_of_date)
            self.assertTrue(set(fact.dependencies) <= facts.keys())
        for observation in insights.observations:
            self.assertTrue(set(observation.dependencies) <= facts.keys())

    def test_known_bullish_divergence_is_not_a_buy_command(self):
        request, result, _ = analyze(BULLISH)
        insights = build_technical_insights(result, expected_request=request)
        observation = find(insights, "divergence:rsi(window=2):bullish")
        self.assertEqual(observation.value, "bullish_candidate")
        self.assertNotIn("买入", observation.text)
        self.assertNotIn("必然", observation.text)

    def test_evidence_graph_is_acyclic_and_dependencies_are_not_from_the_future(self):
        request, result, _ = analyze(BEARISH * 4, weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(insights.code, "technical_insights_computed")
        seen = {}
        for fact in insights.facts:
            self.assertNotIn(fact.fact_id, seen)
            for dependency in fact.dependencies:
                self.assertIn(dependency, seen)
                self.assertLessEqual(seen[dependency].trade_date, fact.trade_date)
            seen[fact.fact_id] = fact

    def test_right_confirmation_is_required_before_latest_high_is_a_pivot(self):
        request, result, _ = analyze(BEARISH[:12])
        insights = build_technical_insights(result, expected_request=request)
        observation = find(insights, "divergence:rsi(window=2):bearish")
        self.assertEqual(observation.status, "insufficient_history")
        request, result, _ = analyze(BEARISH[:13])
        confirmed = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(confirmed, "divergence:rsi(window=2):bearish").value, "bearish_candidate")

    def test_plateau_does_not_create_arbitrary_extrema(self):
        request, result, _ = analyze([10] * 30)
        insights = build_technical_insights(result, expected_request=request)
        divergence = [item for item in insights.observations if item.category == "divergence"]
        self.assertTrue(divergence)
        self.assertTrue(all(item.status == "insufficient_history" for item in divergence))
        self.assertFalse(any(item.value in ("bullish_candidate", "bearish_candidate") for item in divergence))

    def test_old_pivots_are_not_presented_as_current_divergence(self):
        request, result, _ = analyze([*BEARISH, *([14] * 30)])
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(insights, "divergence:rsi(window=2):bearish").value, "stale_pivots")

    def test_unrequested_oscillators_are_not_added(self):
        request, result, _ = analyze(BEARISH, specs=(IndicatorSpec("sma", window=3),))
        insights = build_technical_insights(result, expected_request=request)
        self.assertFalse(any(item.category == "divergence" for item in insights.observations))
        self.assertFalse(any("rsi(" in item.key or "macd(" in item.key for item in insights.observations))

    def test_custom_rsi_period_is_preserved_for_daily_and_weekly(self):
        request, result, _ = analyze([10 + (index % 7) for index in range(65)],
                                     specs=(IndicatorSpec("rsi", window=7),), weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        self.assertIn("divergence:rsi(window=7):bearish", [item.key for item in insights.observations])
        weekly = find(insights, "weekly:rsi(window=7):value")
        self.assertEqual(weekly.value, calculations.rsi_close(insights.weekly_bars, 7)[-1])
        self.assertNotIn("rsi(window=14)", json.dumps(insights.to_dict()))

    def test_historical_series_are_not_trusted_for_divergence(self):
        request, result, _ = analyze(BEARISH)
        original = build_technical_insights(result, expected_request=request)
        indicators = list(result.indicators)
        index = next(index for index, item in enumerate(indicators) if item.kind == "rsi")
        indicator = indicators[index]
        rows = list(indicator.series)
        rows[4] = replace(rows[4], fields={"value": replace(rows[4].fields["value"], value=0.0)})
        indicators[index] = replace(indicator, series=tuple(rows))
        changed = build_technical_insights(replace(result, indicators=tuple(indicators)), expected_request=request)
        self.assertEqual(find(original, "divergence:rsi(window=2):bearish"),
                         find(changed, "divergence:rsi(window=2):bearish"))

    def test_support_and_resistance_stay_on_the_correct_side(self):
        closes = [10, 11, 12, 11, 10, 9, 10, 11, 12.05, 11, 10, 9.04, 10, 11, 10.5]
        request, result, _ = analyze(closes)
        insights = build_technical_insights(result, expected_request=request)
        support, resistance = find(insights, "levels:support"), find(insights, "levels:resistance")
        self.assertLessEqual(support.value, closes[-1])
        self.assertGreaterEqual(resistance.value, closes[-1])
        self.assertAlmostEqual(support.value, (8.8 + 8.84) / 2)
        self.assertAlmostEqual(resistance.value, (12.2 + 12.25) / 2)
        self.assertEqual(find(insights, "levels:support:pivot_count").value, 2)
        self.assertIn("不等同有效触及次数", find(insights, "levels:support:pivot_count").text)

    def test_support_uses_confirmed_extrema_not_the_unconfirmed_latest_low(self):
        request, result, _ = analyze([10, 11, 12, 11, 10, 9, 10, 11, 12, 11, 8])
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(insights, "levels:support").status, "insufficient_history")

    def test_weekly_ohlcv_conserves_observed_values_without_creating_missing_days(self):
        bars = daily_rows([10, 12, 11, 13, 14, 16, 15, 17, 18, 19], weekdays=True)
        weeks = aggregate_completed_weeks(bars, start_date=START, as_of_date=date(2026, 1, 18))
        self.assertEqual(len(weeks), 2)
        first = weeks[0]
        self.assertEqual(first.trade_date, date(2026, 1, 9))
        self.assertEqual(first.open_price, bars[0].open_price)
        self.assertEqual(first.high_price, max(bar.high_price for bar in bars[:5]))
        self.assertEqual(first.low_price, min(bar.low_price for bar in bars[:5]))
        self.assertEqual(first.close_price, bars[4].close_price)
        self.assertEqual(first.volume_shares, sum(bar.volume_shares for bar in bars[:5]))
        self.assertEqual(first.amount_cny, sum(bar.amount_cny for bar in bars[:5]))
        self.assertEqual(sum(bar.volume_shares for bar in weeks), sum(bar.volume_shares for bar in bars))

    def test_friday_and_saturday_still_exclude_current_calendar_week(self):
        bars = daily_rows(range(10, 20), weekdays=True)
        for cutoff in (date(2026, 1, 16), date(2026, 1, 17)):
            weeks = aggregate_completed_weeks(bars, start_date=START, as_of_date=cutoff)
            self.assertEqual(len(weeks), 1)
            self.assertEqual(weeks[-1].trade_date, date(2026, 1, 9))
        self.assertEqual(len(aggregate_completed_weeks(bars, start_date=START,
                                                       as_of_date=date(2026, 1, 18))), 2)

    def test_initial_partial_week_is_not_used_as_a_complete_bar(self):
        bars = daily_rows(range(10, 20), weekdays=True)
        weeks = aggregate_completed_weeks(bars[1:], start_date=START + timedelta(days=1),
                                          as_of_date=date(2026, 1, 18))
        self.assertEqual(len(weeks), 1)
        self.assertEqual(weeks[0].trade_date, date(2026, 1, 16))

    def test_week_aggregation_at_fixed_cutoff_is_prefix_invariant(self):
        bars = daily_rows(range(10, 40), weekdays=True)
        cutoff = date(2026, 1, 18)
        expected = aggregate_completed_weeks(bars[:10], start_date=START, as_of_date=cutoff)
        self.assertEqual(aggregate_completed_weeks(bars, start_date=START, as_of_date=cutoff), expected)

    def test_week_aggregation_rejects_wrong_identity_reordering_and_invalid_prices(self):
        bars = daily_rows(range(10, 20), weekdays=True)
        for changed in ([*bars[:2], replace(bars[2], instrument_id="600000.SH"), *bars[3:]],
                        [bars[1], bars[0], *bars[2:]]):
            with self.assertRaises(ValueError):
                aggregate_completed_weeks(changed, start_date=START, as_of_date=date(2026, 1, 18))
        forged = replace(bars[0])
        object.__setattr__(forged, "close_price", float("nan"))
        with self.assertRaises(ValueError):
            aggregate_completed_weeks([forged, *bars[1:]], start_date=START, as_of_date=date(2026, 1, 18))

    def test_weekly_indicator_values_use_existing_algorithms_and_requested_parameters(self):
        request, result, _ = analyze([10 + index * .2 for index in range(60)], weekdays=True,
                                     as_of=date(2026, 3, 29))
        insights = build_technical_insights(result, expected_request=request)
        weeks = insights.weekly_bars
        self.assertEqual(find(insights, "weekly:sma(window=3):value").value, calculations.sma_close(weeks, 3)[-1])
        self.assertEqual(find(insights, "weekly:ema(window=3):value").value, calculations.ema_close(weeks, 3)[-1])
        self.assertEqual(find(insights, "weekly:rsi(window=2):value").value, calculations.rsi_close(weeks, 2)[-1])
        macd = calculations.macd_close(weeks, 2, 3, 2)[-1]
        self.assertEqual(find(insights, "weekly:macd(fast=2,slow=3,signal=2):histogram").value, macd.histogram)
        self.assertEqual(find(insights, "timeframes:alignment").value, "aligned_above")

    def test_weekly_insufficient_history_is_not_shortened_or_filled(self):
        specs = (IndicatorSpec("sma", window=20), IndicatorSpec("rsi", window=14))
        request, result, _ = analyze([10 + index * .1 for index in range(40)], specs=specs, weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        weekly = find(insights, "weekly:sma(window=20):value")
        self.assertIsNone(weekly.value)
        self.assertEqual(weekly.status, "insufficient_history")
        self.assertEqual(weekly.dependencies, ())
        self.assertEqual(find(insights, "timeframes:alignment").status, "insufficient_history")

    def test_weekly_alignment_requires_requested_mean(self):
        request, result, _ = analyze([10 + index * .1 for index in range(60)],
                                     specs=(IndicatorSpec("rsi", window=2),), weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(insights, "timeframes:alignment").status, "insufficient_history")
        self.assertFalse(any("sma(" in observation.key for observation in insights.observations))

    def test_daily_reversal_against_completed_week_is_explicit_conflict(self):
        closes = [10 + index for index in range(35)] + [60, 50, 40, 30, 20]
        request, result, _ = analyze(closes, weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(insights, "timeframes:alignment").value, "conflicting")

    def test_flat_price_is_weekly_boundary_not_directional_alignment(self):
        request, result, _ = analyze([10] * 40, weekdays=True)
        insights = build_technical_insights(result, expected_request=request)
        self.assertEqual(find(insights, "timeframes:alignment").value, "boundary")

    def test_future_source_suffix_does_not_change_historical_observations(self):
        request, result, _ = analyze(BEARISH)
        original = build_technical_insights(result, expected_request=request)
        source = SnapshotSource(daily_rows([*BEARISH, 1000, 1, 2000, 2]))
        future_result = analyze_indicators(source, request, allow_development_csv=True)
        future = build_technical_insights(future_result, expected_request=request)
        self.assertEqual(original, future)

    def test_request_and_registered_latest_facts_are_validated_before_analysis(self):
        request, result, _ = analyze(BEARISH)
        for changed, expected in ((result, replace(request, request_id="other-turn")),
                                  (replace(result, facts=tuple(fact for fact in result.facts
                                      if not (fact.indicator_id == "daily_bar" and fact.field == "close_price"
                                              and fact.trade_date == result.latest_trade_date))), request),
                                  (replace(result, source="tencent"), request)):
            insights = build_technical_insights(changed, expected_request=expected)
            self.assertEqual(insights.status, "blocked")
            self.assertEqual(insights.observations, ())
            self.assertEqual(insights.facts, ())

    def test_missing_expected_request_does_not_authorize_analysis(self):
        request, result, _ = analyze(BEARISH)
        self.assertEqual(build_technical_insights(result).status, "blocked")

    def test_serialization_is_json_compatible_and_detached(self):
        request, result, _ = analyze(BEARISH)
        insights = build_technical_insights(result, expected_request=request)
        payload = insights.to_dict()
        json.dumps(payload, allow_nan=False)
        payload["observations"].clear()
        payload["facts"].clear()
        self.assertTrue(insights.observations)
        self.assertTrue(insights.facts)


if __name__ == "__main__":
    unittest.main()
