from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.risk_research import build_risk_scenario


START = date(2026, 1, 1)


class _MemorySource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def __init__(self, bars):
        self.bars = bars

    def read_daily_bars(self):
        return list(self.bars)


def _analyze(closes=(10, 11, 13, 12), specs=None, *, cutoff_extra=0):
    bars = tuple(DailyBar("000001.SZ", START + timedelta(days=index),
                         close, close, close, close, 100, 0)
                 for index, close in enumerate(closes))
    request = MultiIndicatorRequest("000001.SZ", START, bars[-1].trade_date + timedelta(days=cutoff_extra),
                                   (IndicatorSpec("atr", window=2),) if specs is None else specs,
                                   source="csv", request_id="risk-test")
    return analyze_indicators(_MemorySource(bars), request, allow_development_csv=True), request


class RiskResearchTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("No network in risk research"))
        guard.start()
        self.addCleanup(guard.stop)
        self.result, self.request = _analyze()

    def build(self, result=None, request=None, **parameters):
        return build_risk_scenario(self.result if result is None else result,
                                   expected_request=self.request if request is None else request, **parameters)

    def _change_atr(self, **changes):
        indicator = self.result.indicators[0]
        row = indicator.series[-1]
        field = replace(row.fields["value"], **changes)
        return replace(self.result, indicators=(replace(indicator,
            series=(*indicator.series[:-1], replace(row, fields={"value": field}))),))

    def test_known_wilder_atr_and_decimal_prices_are_calculated_from_latest_day(self):
        scenario = self.build()
        self.assertEqual((scenario.status, scenario.code), ("available", "historical_long_scenario_ready"))
        self.assertEqual(scenario.source_date, "2026-01-04")
        values = scenario.values
        # True ranges are 1, 2, 1; Wilder ATR2 is (1.5 + 1) / 2 = 1.25.
        self.assertEqual((values.reference_entry, values.atr), ("12", "1.25"))
        self.assertEqual((values.reference_stop, values.reference_target), ("10.13", "15.75"))
        self.assertEqual((values.risk_per_share, values.reward_per_share), ("1.87", "3.75"))
        self.assertEqual(values.risk_pct, "15.5834")
        self.assertEqual(values.effective_reward_risk, "2.005347")
        self.assertEqual((values.atr_multiple, values.reward_risk), ("1.5", "2"))

    def test_default_atr14_is_preferred_over_another_requested_period(self):
        result, request = _analyze(tuple(range(10, 25)),
                                  (IndicatorSpec("atr", window=2), IndicatorSpec("atr")))
        scenario = self.build(result, request)
        self.assertEqual(scenario.status, "available")
        self.assertEqual(scenario.atr_indicator_id, "atr(window=14)")
        self.assertEqual((scenario.values.reference_stop, scenario.values.reference_target), ("22.50", "27.00"))

    def test_partial_other_indicators_do_not_hide_available_atr(self):
        result, request = _analyze(specs=(IndicatorSpec("sma", window=60), IndicatorSpec("atr", window=2)))
        self.assertEqual(result.calculation_status, "limited")
        self.assertEqual(self.build(result, request).status, "available")

    def test_missing_atr_does_not_substitute_a_fixed_percentage(self):
        result, request = _analyze(specs=(IndicatorSpec("sma", window=2),))
        scenario = self.build(result, request)
        self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_atr_not_requested"))
        self.assertIsNone(scenario.values)
        self.assertEqual(scenario.evidence_ids, ())

    def test_unavailable_atr14_does_not_fall_back_to_shorter_available_period(self):
        result, request = _analyze(specs=(IndicatorSpec("atr", window=2), IndicatorSpec("atr")))
        scenario = self.build(result, request)
        self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_atr_unavailable"))
        self.assertEqual(scenario.atr_indicator_id, "atr(window=14)")

    def test_multiple_nondefault_periods_need_an_explicit_unambiguous_request(self):
        result, request = _analyze(specs=(IndicatorSpec("atr", window=2), IndicatorSpec("atr", window=3)))
        self.assertEqual(self.build(result, request).code, "risk_atr_selection_ambiguous")

    def test_latest_unavailable_atr_does_not_reuse_previous_available_value(self):
        self.assertEqual(self.result.indicators[0].series[-2].fields["value"].status, "available")
        old_id = self.result.indicators[0].latest["value"].fact_id
        result = self._change_atr(status="calculation_error", value=None, fact_id=None)
        result = replace(result, facts=tuple(fact for fact in result.facts if fact.fact_id != old_id))
        self.assertEqual(self.build(result).code, "risk_atr_unavailable")

    def test_only_latest_close_and_atr_leaf_facts_are_dependencies(self):
        scenario = self.build()
        index = {fact.fact_id: fact for fact in self.result.facts}
        self.assertEqual(len(scenario.evidence_ids), 2)
        self.assertEqual([(index[key].indicator_id, index[key].field) for key in scenario.evidence_ids],
                         [("daily_bar", "close_price"), ("atr(window=2)", "value")])
        for key in scenario.evidence_ids:
            self.assertEqual(index[key].trade_date, self.result.latest_trade_date)
            self.assertEqual(index[key].dependencies, ())

    def test_request_identity_date_source_and_indicator_scope_must_match(self):
        requests = (replace(self.request, request_id="other-request"),
                    replace(self.request, instrument_id="600000.SH"),
                    replace(self.request, as_of_date=self.request.as_of_date + timedelta(days=1)),
                    replace(self.request, source="tencent"),
                    replace(self.request, indicators=(IndicatorSpec("atr", window=3),)))
        for request in requests:
            with self.subTest(request=request):
                scenario = self.build(request=request)
                self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_evidence_mismatch"))
                self.assertIsNone(scenario.values)

    def test_tampered_current_fact_values_dates_units_and_dependencies_are_rejected(self):
        scenario = self.build()
        for identifier in scenario.evidence_ids:
            index = next(index for index, fact in enumerate(self.result.facts) if fact.fact_id == identifier)
            fact = self.result.facts[index]
            changes = ({"value": 999}, {"unit": "shares"}, {"dependencies": ("untrusted",)},
                       {"trade_date": fact.trade_date - timedelta(days=1)}, {"request_id": "other"})
            for change in changes:
                with self.subTest(identifier=identifier, change=change):
                    facts = list(self.result.facts)
                    facts[index] = replace(fact, **change)
                    self.assertEqual(self.build(replace(self.result, facts=tuple(facts))).code,
                                     "risk_evidence_mismatch")

    def test_missing_close_fact_and_duplicate_fact_ids_are_rejected(self):
        close_id = self.build().evidence_ids[0]
        missing = replace(self.result, facts=tuple(fact for fact in self.result.facts if fact.fact_id != close_id))
        duplicate = replace(self.result, facts=(*self.result.facts, self.result.facts[0]))
        for result in (missing, duplicate):
            with self.subTest(result=result):
                self.assertEqual(self.build(result).code, "risk_evidence_mismatch")

    def test_wrong_atr_field_date_unit_threshold_and_fact_binding_are_rejected(self):
        changes = ({"trade_date": START}, {"unit": "index_points"}, {"required_bars": 2},
                   {"fact_id": self.build().evidence_ids[0]})
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.build(self._change_atr(**change)).code, "risk_evidence_mismatch")

    def test_zero_atr_does_not_create_zero_risk_or_infinite_reward_risk(self):
        result, request = _analyze((10, 10, 10, 10))
        scenario = self.build(result, request)
        self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_nonpositive_reference"))
        self.assertIsNone(scenario.values)

    def test_subcent_distance_or_nonpositive_stop_has_no_reference_prices(self):
        cases = ((10, 10.000001, 10.000002), (1, 5, 1))
        for closes in cases:
            with self.subTest(closes=closes):
                result, request = _analyze(closes)
                scenario = self.build(result, request)
                self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_price_grid_unavailable"))
                self.assertIsNone(scenario.values)

    def test_parameters_accept_finite_positive_bounded_numbers_only(self):
        invalid = (True, False, None, "2", 0, -1, 10.00001,
                   float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"))
        for name in ("atr_multiple", "reward_risk"):
            for value in invalid:
                with self.subTest(name=name, value=value):
                    scenario = self.build(**{name: value})
                    self.assertEqual((scenario.status, scenario.code), ("invalid_request", "invalid_risk_parameters"))
                    self.assertIsNone(scenario.values)

    def test_custom_parameters_are_explicit_and_do_not_rewrite_market_facts(self):
        scenario = self.build(atr_multiple=Decimal("1.2"), reward_risk=Decimal("3"))
        self.assertEqual((scenario.values.reference_stop, scenario.values.reference_target), ("10.50", "16.50"))
        self.assertEqual((scenario.values.atr_multiple, scenario.values.reward_risk), ("1.2", "3"))
        self.assertEqual(self.result.indicators[0].latest["value"].value, 1.25)

    def test_price_rounding_does_not_assume_a_two_decimal_input_anchor(self):
        result, request = _analyze((10, 11, 13, 12.007))
        scenario = self.build(result, request)
        self.assertEqual(scenario.status, "available")
        self.assertEqual(scenario.values.reference_entry, "12.007")
        self.assertLess(Decimal(scenario.values.reference_stop), Decimal("12.007"))
        self.assertGreater(Decimal(scenario.values.reference_target), Decimal("12.007"))

    def test_stale_latest_day_is_labelled_with_actual_source_date(self):
        result, request = _analyze(cutoff_extra=4)
        scenario = self.build(result, request)
        self.assertEqual(scenario.source_date, "2026-01-04")
        self.assertTrue(any("早于请求截止日" in item for item in scenario.assumptions))

    def test_serialization_keeps_exact_decimal_strings_authenticity_and_no_execution(self):
        payload = self.build().to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.assertEqual(payload["values"]["reference_stop"], "10.13")
        self.assertEqual(payload["data_authenticity"], "synthetic")
        self.assertEqual(payload["price_adjustment"], "unknown")
        self.assertFalse(payload["execution_allowed"])
        for value in ("人工样例", "不代表真实持仓", "不保证止损成交", "ATR衡量历史波动"):
            self.assertIn(value, encoded)
        self.assertEqual(payload["evidence_ids"], list(self.build().evidence_ids))

    def test_no_account_position_or_trade_execution_arguments_are_accepted(self):
        for argument in ("capital", "risk_budget_percent", "position", "execute", "broker"):
            with self.subTest(argument=argument), self.assertRaises(TypeError):
                self.build(**{argument: 100})

    def test_no_data_and_source_failure_do_not_reuse_available_price_fields(self):
        for data_status in ("no_data", "source_error", "invalid_data"):
            with self.subTest(data_status=data_status):
                scenario = self.build(replace(self.result, data_status=data_status))
                self.assertEqual((scenario.status, scenario.code), ("unavailable", "risk_market_data_unavailable"))
                self.assertIsNone(scenario.values)
                self.assertEqual(scenario.evidence_ids, ())

    def test_invalid_inputs_return_classified_unavailable_without_raw_details(self):
        for result, request in ((None, self.request), (self.result, None), ("private-details", self.request)):
            with self.subTest(result_type=type(result), request_type=type(request)):
                scenario = build_risk_scenario(result, expected_request=request)
                self.assertEqual(scenario.code, "risk_evidence_mismatch")
                self.assertNotIn("private-details", json.dumps(scenario.to_dict()))


if __name__ == "__main__":
    unittest.main()
