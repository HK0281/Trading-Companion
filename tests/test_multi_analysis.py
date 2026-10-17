import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.indicators import MacdValue, volume_activity
from trading_companion.multi_analysis import (
    DEFAULT_INDICATORS, IndicatorSpec, MultiIndicatorRequest, analyze_indicators,
)


START = date(2026, 1, 1)


def make_bars(closes, volumes=None):
    if volumes is None:
        volumes = [100] * len(closes)
    return [DailyBar("000001.SZ", START + timedelta(days=index), close, close,
                     close, close, volume, 0.0)
            for index, (close, volume) in enumerate(zip(closes, volumes))]


class CountingSource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def __init__(self, bars, error=None):
        self.bars = bars
        self.error = error
        self.calls = 0

    def read_daily_bars(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.bars


def request_for(bars, indicators=DEFAULT_INDICATORS, **overrides):
    parameters = dict(instrument_id="000001.SZ", start_date=START,
                      as_of_date=bars[-1].trade_date if bars else START,
                      indicators=indicators, source="csv", request_id="query-test")
    parameters.update(overrides)
    return MultiIndicatorRequest(**parameters)


def analyze(bars, indicators=DEFAULT_INDICATORS, **overrides):
    return analyze_indicators(CountingSource(bars), request_for(bars, indicators, **overrides),
                              allow_development_csv=True)


def find_relation(result, field, indicator_id=None):
    return next(relation for relation in result.relations if relation.field == field
                and (indicator_id is None or relation.indicator_id == indicator_id))


class MultiRequestTests(unittest.TestCase):
    def test_default_eight_instances_and_explicit_deduplication(self):
        bars = make_bars([10] * 3)
        self.assertEqual(len(request_for(bars).indicators), 8)
        duplicate = IndicatorSpec("boll", stddev_multiplier=2)
        request = request_for(bars, (duplicate, IndicatorSpec("boll", stddev_multiplier=2.0)))
        self.assertEqual(len(request.indicators), 1)
        self.assertEqual(duplicate.indicator_id, IndicatorSpec("boll").indicator_id)

    def test_close_but_distinct_boll_multipliers_have_unique_full_precision_ids(self):
        bars = make_bars([1, 2, 3])
        specs = tuple(IndicatorSpec("boll", window=2, stddev_multiplier=value)
                      for value in (1.0000001, 1.0000002))
        result = analyze(bars, specs)
        self.assertNotEqual(specs[0].indicator_id, specs[1].indicator_id)
        self.assertEqual(len({fact.fact_id for fact in result.facts}), len(result.facts))
        self.assertEqual(len(result.indicators), 2)

    def test_product_parameter_boundaries_are_inclusive(self):
        for kind in ("sma", "ema", "volume"):
            self.assertEqual(IndicatorSpec(kind, window=1).parameters["window"], 1)
        for kind in ("sma", "ema", "volume", "rsi", "atr", "boll"):
            self.assertEqual(IndicatorSpec(kind, window=120).parameters["window"], 120)
        self.assertEqual(IndicatorSpec("macd", fast=2, slow=120, signal=60).parameters["slow"], 120)
        self.assertEqual(IndicatorSpec("boll", stddev_multiplier=4).parameters["stddev_multiplier"], 4)

    def test_invalid_or_unknown_parameters_are_rejected(self):
        cases = [("unknown", {}), ("sma", {"path": "secret.csv"}),
                 ("ema", {"window": True}), ("sma", {"window": 0}),
                 ("ema", {"window": 121}), ("rsi", {"window": 1}),
                 ("atr", {"window": 1}), ("boll", {"window": 1}),
                 ("macd", {"fast": 1}), ("macd", {"fast": 26}),
                 ("macd", {"signal": 61}), ("macd", {"slow": 121}),
                 ("boll", {"stddev_multiplier": 0}), ("boll", {"stddev_multiplier": True}),
                 ("boll", {"stddev_multiplier": float("nan")}),
                 ("boll", {"stddev_multiplier": float("inf")}),
                 ("boll", {"stddev_multiplier": 4.0000001})]
        for kind in ("sma", "ema", "rsi", "boll", "atr", "volume"):
            cases.extend((kind, {"window": value}) for value in (None, "3", 2.5, False))
        for kind, parameters in cases:
            with self.subTest(kind=kind, parameters=parameters):
                with self.assertRaises(InvalidRequestError):
                    IndicatorSpec(kind, **parameters)

    def test_empty_or_unvalidated_indicator_lists_are_rejected(self):
        for indicators in ((), [], None, "sma", ["sma"], [{"kind": "sma"}]):
            with self.subTest(indicators=indicators), self.assertRaises(InvalidRequestError):
                request_for(make_bars([10]), indicators)

    def test_maximum_applies_after_deduplication(self):
        bars = make_bars([10])
        twelve = tuple(IndicatorSpec("sma", window=window) for window in range(1, 13))
        self.assertEqual(len(request_for(bars, twelve * 2).indicators), 12)
        with self.assertRaises(InvalidRequestError):
            request_for(bars, (*twelve, IndicatorSpec("sma", window=13)))

    def test_dates_must_be_dates_in_order_with_inclusive_366_day_limit(self):
        bars = make_bars([10])
        request_for(bars, as_of_date=START + timedelta(days=365))
        for overrides in ({"as_of_date": START + timedelta(days=366)},
                          {"as_of_date": START - timedelta(days=1)},
                          {"start_date": "2026-01-01"},
                          {"as_of_date": datetime(2026, 1, 2)}):
            with self.subTest(overrides=overrides), self.assertRaises(InvalidRequestError):
                request_for(bars, **overrides)

    def test_identifier_and_selected_source_coverage_are_checked_without_reading(self):
        for instrument in ("000001", "000001.sz", "600000.SZ", "１２３４５６.SH", "foo", None):
            with self.subTest(instrument=instrument), self.assertRaises(InvalidRequestError):
                request_for([], instrument_id=instrument)
        for instrument in ("830001.BJ", "689001.SH", "510300.SH"):
            with self.subTest(instrument=instrument), self.assertRaises(InvalidRequestError):
                request_for([], instrument_id=instrument, source="tencent")
        request_for([], instrument_id="830001.BJ", source="akshare_eastmoney")
        with self.assertRaises(InvalidRequestError):
            request_for([], source="https://untrusted.example")

    def test_preconfigured_source_mismatch_rejects_before_read(self):
        bars = make_bars([10, 11, 12])
        request = request_for(bars)
        for name, value in (("source_name", "tencent"), ("instrument_id", "600000.SH"),
                            ("start_date", START - timedelta(days=10)),
                            ("end_date", request.as_of_date + timedelta(days=1))):
            source = CountingSource(bars)
            setattr(source, name, value)
            with self.subTest(name=name), self.assertRaises(InvalidRequestError):
                analyze_indicators(source, request, allow_development_csv=True)
            self.assertEqual(source.calls, 0)

    def test_csv_needs_explicit_developer_permission(self):
        source = CountingSource(make_bars([10]))
        with self.assertRaises(InvalidRequestError):
            analyze_indicators(source, request_for(source.bars))
        self.assertEqual(source.calls, 0)

    def test_online_current_day_and_adjusted_source_are_rejected_before_read(self):
        bars = make_bars([10])
        request = request_for(bars, source="tencent")
        source = CountingSource(bars)
        source.source_name = "tencent"
        with self.assertRaises(InvalidRequestError):
            analyze_indicators(source, request)
        source.adjustment = "unadjusted"
        future = replace(request, as_of_date=date.today() + timedelta(days=2),
                         start_date=date.today())
        with self.assertRaises(InvalidRequestError):
            analyze_indicators(source, future)
        self.assertEqual(source.calls, 0)


class MultiAvailabilityTests(unittest.TestCase):
    def test_thirty_default_bars_are_partially_available_after_exactly_one_read(self):
        bars = make_bars(list(range(10, 40)))
        source = CountingSource(bars)
        result = analyze_indicators(source, request_for(bars), allow_development_csv=True)
        self.assertEqual(source.calls, 1)
        self.assertEqual((result.data_status, result.calculation_status), ("ok", "limited"))
        self.assertTrue(result.synthesis_allowed)
        self.assertEqual(result.indicators[1].latest["value"].missing_bars, 30)
        macd = result.indicators[3].latest
        self.assertEqual(macd["dif"].status, "available")
        for name in ("dea", "histogram"):
            self.assertEqual(macd[name].status, "insufficient_history")
            self.assertEqual((macd[name].required_bars, macd[name].actual_bars,
                              macd[name].missing_bars), (34, 30, 4))
        for indicator in (result.indicators[0], *result.indicators[2:3], *result.indicators[4:]):
            self.assertTrue(all(value.status == "available" for value in indicator.latest.values()))

    def test_single_sma_three_has_only_its_own_requirements(self):
        bars = make_bars([10, 12, 11, 14, 13])
        result = analyze(bars, (IndicatorSpec("sma", window=3),))
        self.assertEqual(result.calculation_status, "complete")
        self.assertEqual(len(result.indicators), 1)
        self.assertEqual(result.indicators[0].latest["value"].value, 38 / 3)
        self.assertEqual(result.indicators[0].latest["value"].required_bars, 3)
        self.assertEqual(result.indicators[0].series[0].fields["value"].status, "insufficient_history")

    def test_just_initialized_value_is_complete_but_previous_point_relations_are_not(self):
        result = analyze(make_bars([10, 12, 11]), (IndicatorSpec("sma", window=3),))
        self.assertEqual(result.calculation_status, "complete")
        self.assertEqual(find_relation(result, "price_position").status, "available")
        for field in ("mean_change", "price_cross"):
            relation = find_relation(result, field)
            self.assertEqual((relation.status, relation.missing_bars), ("insufficient_history", 1))
            self.assertIsNone(relation.value)
            self.assertEqual(relation.dependencies, ())

    def test_all_unavailable_keeps_valid_bars_and_disallows_synthesis(self):
        result = analyze(make_bars([10] * 5), (IndicatorSpec("sma", window=20),))
        self.assertEqual((result.data_status, result.calculation_status), ("ok", "unavailable"))
        self.assertEqual(len(result.bars), 5)
        self.assertFalse(result.synthesis_allowed)
        self.assertTrue(any("禁止" in warning for warning in result.warnings))

    def test_raw_volume_alone_does_not_enable_technical_synthesis(self):
        result = analyze(make_bars([10] * 5), (IndicatorSpec("volume", window=20),))
        self.assertEqual(result.indicators[0].latest["volume_shares"].status, "available")
        self.assertFalse(result.synthesis_allowed)

    def test_zero_volume_baseline_preserves_undefined_and_valid_zero_fields(self):
        result = analyze(make_bars([10] * 4, [0, 0, 0, 30]), (IndicatorSpec("volume", window=3),))
        fields = result.indicators[0].latest
        self.assertEqual(fields["previous_volume_ma"].value, 0.0)
        self.assertEqual(fields["relative_volume"].status, "undefined")
        self.assertEqual(fields["relative_volume"].missing_bars, 0)
        self.assertIsNone(fields["relative_volume"].value)
        self.assertIsNone(fields["relative_volume"].fact_id)
        relative_component = next(item for item in result.indicators[0].components
                                  if item.name == "relative_volume")
        self.assertIsNone(relative_component.first_available_date)
        self.assertEqual(find_relation(result, "relative_volume_position").status, "undefined")
        self.assertEqual(find_relation(result, "current_vs_prior_mean").value, "above")
        zero_current = analyze(make_bars([10] * 4, [10, 20, 30, 0]),
                               (IndicatorSpec("volume", window=3),))
        self.assertEqual(zero_current.indicators[0].latest["relative_volume"].value, 0.0)
        self.assertEqual(zero_current.indicators[0].latest["relative_volume"].status, "available")
        recovered_bars = make_bars([10] * 5, [0, 0, 0, 30, 0])
        recovered = analyze(recovered_bars, (IndicatorSpec("volume", window=3),))
        recovered_component = next(item for item in recovered.indicators[0].components
                                   if item.name == "relative_volume")
        self.assertEqual(recovered_component.first_available_date, recovered_bars[4].trade_date)

    def test_explicit_volume_history_status_cannot_bypass_the_ready_threshold(self):
        bars = make_bars([10, 11, 12, 13])
        values = volume_activity(bars, 3)
        values[-1] = replace(values[-1], relative_volume=None,
                             relative_volume_status="insufficient_history")
        with patch("trading_companion.multi_analysis.calculations.volume_activity", return_value=values):
            result = analyze(bars, (IndicatorSpec("volume", window=3),
                                    IndicatorSpec("ema", window=2)))
        self.assertEqual(result.indicators[0].latest["relative_volume"].status, "calculation_error")
        self.assertEqual(result.indicators[1].latest["value"].status, "available")
        self.assertEqual(result.calculation_status, "limited")

    def test_real_numeric_overflow_is_isolated_to_the_instance_and_sanitized(self):
        result = analyze(make_bars([1e308] * 4),
                         (IndicatorSpec("sma", window=2), IndicatorSpec("ema", window=2)))
        failed, available = result.indicators
        self.assertEqual(failed.latest["value"].status, "calculation_error")
        self.assertEqual(available.latest["value"].value, 1e308)
        self.assertEqual(result.calculation_status, "limited")
        self.assertIsNone(failed.components[0].first_available_date)
        self.assertEqual(find_relation(result, "price_position", failed.indicator_id).status,
                         "calculation_error")
        json.dumps(result.to_dict(), allow_nan=False)

    def test_expected_numeric_exception_is_sanitized_but_unexpected_exception_surfaces(self):
        bars = make_bars([10, 11, 12])
        specs = (IndicatorSpec("sma", window=2), IndicatorSpec("ema", window=2))
        with patch("trading_companion.multi_analysis.calculations.sma_close",
                   side_effect=ValueError("secret-token /private/location")):
            result = analyze(bars, specs)
        self.assertEqual(result.indicators[0].latest["value"].status, "calculation_error")
        self.assertNotIn("secret-token", json.dumps(result.to_dict()))
        with patch("trading_companion.multi_analysis.calculations.sma_close",
                   side_effect=RuntimeError("implementation bug")):
            with self.assertRaisesRegex(RuntimeError, "implementation bug"):
                analyze(bars, specs)

    def test_latest_missing_value_after_threshold_is_a_contract_error_not_forward_filled(self):
        bars = make_bars([10] * 6)
        values = [MacdValue(None, None, None), MacdValue(None, None, None),
                  MacdValue(0, None, None), MacdValue(0, 0, 0),
                  MacdValue(0, 0, 0), MacdValue(0, None, None)]
        with patch("trading_companion.multi_analysis.calculations.macd_close", return_value=values):
            result = analyze(bars, (IndicatorSpec("macd", fast=2, slow=3, signal=2),))
        self.assertIsNone(result.indicators[0].latest["dea"].value)
        self.assertEqual(result.indicators[0].latest["dea"].status, "calculation_error")
        self.assertEqual(find_relation(result, "dif_dea_cross").status, "calculation_error")
        self.assertFalse(any(fact.field == "dea" and fact.trade_date == bars[-1].trade_date
                             for fact in result.facts))

    def test_value_before_threshold_is_a_contract_error(self):
        with patch("trading_companion.multi_analysis.calculations.sma_close",
                   return_value=[10.0, 10.5, 11.0]):
            result = analyze(make_bars([10, 11, 12]), (IndicatorSpec("sma", window=3),))
        self.assertEqual(result.indicators[0].latest["value"].status, "calculation_error")


class MultiMetadataTests(unittest.TestCase):
    def test_initialization_dates_and_component_recursive_update_counts(self):
        bars = make_bars(list(range(10, 45)))
        result = analyze(bars)
        ema = result.indicators[2].components[0]
        self.assertEqual((ema.required_bars, ema.first_available_date, ema.recursive_updates),
                         (20, bars[19].trade_date, 15))
        macd = {item.name: item for item in result.indicators[3].components}
        self.assertEqual(macd["fast_ema"].recursive_updates, 23)
        self.assertEqual(macd["slow_ema"].recursive_updates, 9)
        self.assertEqual(macd["dif"].recursive_updates, 9)
        self.assertEqual(macd["dea"].recursive_updates, 1)
        self.assertEqual(macd["histogram"].recursive_updates, 1)
        self.assertEqual(macd["dea"].first_available_date, bars[33].trade_date)
        self.assertEqual(macd["dea"].warning_threshold_updates, 78)
        self.assertTrue(macd["dea"].start_effect_warning)
        for index in (4, 6):
            component = result.indicators[index].components[0]
            self.assertEqual(component.required_bars, 15)
            self.assertEqual(component.recursive_updates, 20)
        for indicator in result.indicators:
            self.assertTrue(indicator.algorithm_version)
            self.assertTrue(all(item.calculation_start_date == START for item in indicator.components))
        raw_volume = result.indicators[-1].components[0]
        self.assertEqual((raw_volume.period, raw_volume.required_bars, raw_volume.seed_method),
                         (1, 1, "observed_share_count"))

    def test_three_times_period_is_a_hint_threshold_with_exact_boundary(self):
        spec = (IndicatorSpec("ema", window=2),)
        short = analyze(make_bars([10] * 7), spec).indicators[0].components[0]
        ready = analyze(make_bars([10] * 8), spec).indicators[0].components[0]
        self.assertEqual((short.recursive_updates, ready.recursive_updates), (5, 6))
        self.assertTrue(short.start_effect_warning)
        self.assertFalse(ready.start_effect_warning)

    def test_different_start_dates_expose_changed_seed_and_recursive_counts(self):
        bars = make_bars([10, 20, 11, 14, 13, 15, 12])
        specs = (IndicatorSpec("ema", window=3), IndicatorSpec("rsi", window=3),
                 IndicatorSpec("atr", window=3))
        full = analyze(bars, specs)
        restarted = analyze(bars, specs, start_date=bars[1].trade_date)
        for left, right in zip(full.indicators, restarted.indicators):
            self.assertNotAlmostEqual(left.latest["value"].value, right.latest["value"].value)
            self.assertEqual(right.components[0].calculation_start_date, bars[1].trade_date)
            self.assertEqual(left.components[0].recursive_updates, right.components[0].recursive_updates + 1)


class MultiRelationTests(unittest.TestCase):
    def test_price_cross_from_equal_boundary_in_both_directions(self):
        for closes, expected in (([10, 10, 11], "cross_up"), ([10, 10, 9], "cross_down"),
                                 ([10, 11, 12], "no_cross"), ([10, 10, 10], "no_cross")):
            with self.subTest(closes=closes):
                result = analyze(make_bars(closes), (IndicatorSpec("sma", window=2),))
                self.assertEqual(find_relation(result, "price_cross").value, expected)

    def test_price_comparison_uses_full_precision_without_display_rounding(self):
        result = analyze(make_bars([10.0, 10.00000001]), (IndicatorSpec("sma", window=2),))
        self.assertEqual(find_relation(result, "price_position").value, "above")

    def test_multiple_means_have_current_comparison_and_two_date_cross_dependencies(self):
        specs = (IndicatorSpec("sma", window=2), IndicatorSpec("ema", window=3))
        result = analyze(make_bars([10, 10, 10, 12]), specs)
        relation = find_relation(result, "mean_cross")
        self.assertEqual(relation.status, "available")
        self.assertEqual(len(relation.dependencies), 4)
        self.assertEqual(find_relation(result, "mean_position").value, "equal")
        dates = {next(f.trade_date for f in result.facts if f.fact_id == dependency)
                 for dependency in relation.dependencies}
        self.assertEqual(dates, {START + timedelta(days=2), START + timedelta(days=3)})

    def test_macd_current_comparison_is_ready_before_cross_or_histogram_change(self):
        for length, expected in ((33, "insufficient_history"), (34, "insufficient_history"),
                                 (35, "available")):
            with self.subTest(length=length):
                result = analyze(make_bars([10] * length), (IndicatorSpec("macd"),))
                self.assertEqual(find_relation(result, "dif_dea_cross").status, expected)
                self.assertEqual(find_relation(result, "histogram_change").status, expected)
                if length >= 34:
                    self.assertEqual(find_relation(result, "dif_dea_position").value, "equal")
                if length == 35:
                    self.assertEqual(find_relation(result, "dif_dea_cross").value, "no_cross")

    def test_macd_cross_from_equal_boundary_uses_program_numbers(self):
        for dif, expected in ((0.1, "cross_up"), (-0.1, "cross_down")):
            values = [MacdValue(None, None, None)] * 2 + [MacdValue(0, None, None),
                      MacdValue(0, 0, 0), MacdValue(dif, 0, 2 * dif)]
            with patch("trading_companion.multi_analysis.calculations.macd_close", return_value=values):
                result = analyze(make_bars([10] * 5),
                                 (IndicatorSpec("macd", fast=2, slow=3, signal=2),))
            self.assertEqual(find_relation(result, "dif_dea_cross").value, expected)

    def test_rsi_reference_boundaries_are_neutral(self):
        for value, expected in ((29.999, "below_30"), (30, "neutral"), (50, "neutral"),
                                (70, "neutral"), (70.001, "above_70")):
            with self.subTest(value=value), patch(
                    "trading_companion.multi_analysis.calculations.rsi_close",
                    return_value=[None, None, value]):
                result = analyze(make_bars([10] * 3), (IndicatorSpec("rsi", window=2),))
                self.assertEqual(find_relation(result, "reference_zone").value, expected)

    def test_boll_coincident_bands_and_exact_upper_boundary(self):
        constant = analyze(make_bars([10] * 3), (IndicatorSpec("boll", window=2),))
        self.assertEqual(find_relation(constant, "bands_state").value, "coincident")
        self.assertEqual(find_relation(constant, "price_position").value, "at_coincident_bands")
        upper = analyze(make_bars([1, 3]),
                        (IndicatorSpec("boll", window=2, stddev_multiplier=1),))
        self.assertEqual(find_relation(upper, "price_position").value, "at_upper")
        lower = analyze(make_bars([3, 1]),
                        (IndicatorSpec("boll", window=2, stddev_multiplier=1),))
        self.assertEqual(find_relation(lower, "price_position").value, "at_lower")

    def test_atr_change_describes_only_numeric_volatility_scale(self):
        result = analyze(make_bars([10, 11, 12, 16]), (IndicatorSpec("atr", window=2),))
        relation = find_relation(result, "atr_change")
        self.assertEqual(relation.value, "increasing")
        self.assertIn("no price-direction", relation.rule)


class MultiDataAndEvidenceTests(unittest.TestCase):
    def test_all_records_are_validated_before_date_selection(self):
        bars = make_bars([10, 11, 12])
        invalid = [None, "not-bars", [bars[0], None], [bars[1], bars[0]],
                   [bars[0], bars[0]], [bars[0], replace(bars[1], instrument_id="600000.SH")]]
        damaged = replace(bars[2])
        object.__setattr__(damaged, "close_price", float("nan"))
        invalid.append([*bars[:2], damaged])
        for records in invalid:
            with self.subTest(records=records):
                source = CountingSource(records)
                result = analyze_indicators(source, request_for(bars[:2]), allow_development_csv=True)
                self.assertEqual(result.data_status, "invalid_data")
                self.assertEqual(result.calculation_status, "unavailable")
                self.assertEqual(result.facts, ())
                self.assertEqual(source.calls, 1)

    def test_source_failure_invalid_data_and_empty_data_remain_distinct_and_sanitized(self):
        for error, expected in ((DataSourceError("secret /disk/path"), "source_error"),
                                (DataValidationError("secret-token"), "invalid_data"),
                                (None, "no_data")):
            with self.subTest(expected=expected):
                source = CountingSource([], error)
                result = analyze_indicators(source, request_for([]), allow_development_csv=True)
                self.assertEqual(result.data_status, expected)
                self.assertEqual(source.calls, 1)
                self.assertNotIn("secret", json.dumps(result.to_dict()))
                self.assertFalse(result.synthesis_allowed)
                if error is None:
                    self.assertEqual(result.reason, "empty_source")

    def test_nonempty_source_with_no_matching_dates_is_distinct_from_empty_source(self):
        bars = make_bars([10, 11])
        result = analyze(bars, start_date=START + timedelta(days=3),
                         as_of_date=START + timedelta(days=4))
        self.assertEqual((result.data_status, result.reason), ("no_data", "no_matching_bars"))

    def test_filtering_does_not_use_pre_start_history_or_future_bars(self):
        bars = make_bars([100, 200, 10, 12, 11, 1000])
        specs = (IndicatorSpec("sma", window=3), IndicatorSpec("ema", window=3))
        result = analyze(bars, specs, start_date=bars[2].trade_date, as_of_date=bars[4].trade_date)
        self.assertEqual(result.bars, tuple(bars[2:5]))
        self.assertEqual(result.indicators[0].latest["value"].value, 11.0)
        self.assertEqual(result.indicators[1].latest["value"].value, 11.0)
        for indicator in result.indicators:
            self.assertIsNone(indicator.series[0].fields["value"].value)
            self.assertEqual(indicator.components[0].recursive_updates, 0)

    def test_future_changes_and_appends_preserve_all_values_relations_and_evidence(self):
        bars = make_bars(list(range(10, 45)))
        request = request_for(bars)
        original = analyze_indicators(CountingSource(bars), request, allow_development_csv=True)
        future = replace(bars[-1], trade_date=bars[-1].trade_date + timedelta(days=2),
                         open_price=1000, high_price=1000, low_price=1000,
                         close_price=1000, volume_shares=1000000)
        extended = analyze_indicators(CountingSource([*bars, future]), request,
                                      allow_development_csv=True)
        self.assertEqual(original.indicators, extended.indicators)
        self.assertEqual(original.relations, extended.relations)
        self.assertEqual(original.facts, extended.facts)
        self.assertEqual(original.bars, extended.bars)

    def test_date_gaps_and_latest_date_are_exposed_without_added_bars(self):
        bars = make_bars([10, 12, 11])
        bars[-1] = replace(bars[-1], trade_date=START + timedelta(days=10))
        result = analyze(bars, (IndicatorSpec("sma", window=3),),
                         as_of_date=START + timedelta(days=11))
        self.assertEqual(len(result.bars), 3)
        self.assertEqual(result.latest_trade_date, bars[-1].trade_date)
        self.assertEqual(result.to_dict()["latest_trade_date"], bars[-1].trade_date.isoformat())
        self.assertTrue(any("实际最新日" in warning for warning in result.warnings))

    def test_facts_are_unique_current_query_ids_and_dependencies_are_complete(self):
        bars = make_bars(list(range(10, 45)))
        result = analyze(bars)
        facts = {fact.fact_id: fact for fact in result.facts}
        self.assertEqual(len(facts), len(result.facts))
        for fact in result.facts:
            self.assertIn("query-test|000001.SZ|", fact.fact_id)
            self.assertIn(fact.trade_date.isoformat(), fact.fact_id)
            self.assertIn(fact.indicator_id, fact.fact_id)
            self.assertIn(fact.field, fact.fact_id)
            self.assertIn(fact.trade_date, {bars[-1].trade_date, bars[-2].trade_date})
            for dependency in fact.dependencies:
                self.assertIn(dependency, facts)
        for relation in result.relations:
            if relation.status == "available":
                self.assertIn(relation.fact_id, facts)
                self.assertEqual(relation.dependencies, facts[relation.fact_id].dependencies)
                matching = next((item for item in result.indicators
                                 if item.indicator_id == relation.indicator_id), None)
                if matching is not None:
                    self.assertEqual(facts[relation.fact_id].parameters, matching.parameters)
            else:
                self.assertIsNone(relation.fact_id)
        dictionary = result.to_dict()
        json.dumps(dictionary, allow_nan=False)
        self.assertEqual(dictionary["indicators"][0]["latest"]["value"]["trade_date"],
                         bars[-1].trade_date.isoformat())

    def test_new_request_identity_changes_every_fact_identifier(self):
        bars = make_bars([10, 11, 12, 13])
        specs = (IndicatorSpec("sma", window=2),)
        first, second = analyze(bars, specs), analyze(bars, specs, request_id="other-query")
        self.assertFalse({fact.fact_id for fact in first.facts} & {fact.fact_id for fact in second.facts})

    def test_csv_authenticity_is_unverified_unless_trusted_configuration_marks_synthetic(self):
        path = Path(__file__).resolve().parents[1] / "data" / "sample_daily_bars_synthetic.csv"
        request = MultiIndicatorRequest("000001.SZ", date(2026, 9, 21), date(2026, 9, 25),
                                        (IndicatorSpec("sma", window=3),), source="csv")
        unknown = analyze_indicators(CsvDailyBarSource(path), request, allow_development_csv=True)
        synthetic = analyze_indicators(CsvDailyBarSource(path, is_synthetic=True), request,
                                       allow_development_csv=True)
        self.assertEqual(unknown.data_authenticity, "unverified_csv")
        self.assertEqual(synthetic.data_authenticity, "synthetic")
        self.assertEqual(unknown.price_adjustment, "unknown")
        self.assertTrue(any("真实性与复权未知" in warning for warning in unknown.warnings))
        self.assertTrue(any("人工样例/非真实行情" in warning for warning in synthetic.warnings))

    def test_tencent_result_exposes_upstream_volume_amount_rounding_limit(self):
        bars = make_bars([10, 11, 12])
        source = CountingSource(bars)
        source.source_name = "tencent"
        source.adjustment = "unadjusted"
        result = analyze_indicators(source, request_for(bars, source="tencent"))
        self.assertEqual(source.calls, 1)
        self.assertTrue(any("腾讯上游量额可能已舍入" in warning and "不能恢复" in warning
                            for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
