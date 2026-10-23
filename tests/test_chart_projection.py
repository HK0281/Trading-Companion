from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.chart_projection import (
    MAX_JSON_BYTES, VALIDATION_SCOPE, build_chart_projection,
)
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "sample_daily_bars_synthetic.csv"
PRIVATE = "private-data-that-must-not-be-exported"
SPECS = (
    IndicatorSpec("sma", window=3), IndicatorSpec("ema", window=3),
    IndicatorSpec("macd", fast=2, slow=3, signal=2), IndicatorSpec("rsi", window=2),
    IndicatorSpec("boll", window=3), IndicatorSpec("atr", window=2),
    IndicatorSpec("volume", window=3),
)


class ChartProjectionTests(unittest.TestCase):
    def setUp(self):
        self.request = MultiIndicatorRequest(
            "000001.SZ", date(2026, 9, 21), date(2026, 9, 25),
            SPECS, "csv", "chart-test",
        )
        self.source = CsvDailyBarSource(FIXTURE, is_synthetic=True)
        self.result = analyze_indicators(self.source, self.request, allow_development_csv=True)

    def chart(self, result=None, request=None, **context):
        return build_chart_projection(
            self.result if result is None else result,
            expected_request=self.request if request is None else request, **context,
        )

    def changed_field(self, *, indicator_index=0, row_index=0, name="value", **changes):
        indicators = list(self.result.indicators)
        indicator = indicators[indicator_index]
        rows = list(indicator.series)
        row = rows[row_index]
        fields = {**row.fields, name: replace(row.fields[name], **changes)}
        rows[row_index] = replace(row, fields=fields)
        indicators[indicator_index] = replace(indicator, series=tuple(rows))
        return replace(self.result, indicators=tuple(indicators))

    def changed_indicator(self, index=0, **changes):
        indicators = list(self.result.indicators)
        indicators[index] = replace(indicators[index], **changes)
        return replace(self.result, indicators=tuple(indicators))

    def assert_unavailable(self, result, code="chart_integrity_mismatch", **kwargs):
        payload = self.chart(result, **kwargs)
        self.assertEqual(payload, {"version": "research-chart-v1", "status": "unavailable", "code": code})
        self.assertNotIn(PRIVATE, json.dumps(payload))

    def test_seven_real_fixture_indicators_have_stable_aligned_chart_contract(self):
        chart = self.chart()
        self.assertEqual((chart["version"], chart["status"], chart["code"]),
                         ("research-chart-v1", "available", "aligned_research_series"))
        self.assertEqual(chart["validation_scope"], VALIDATION_SCOPE)
        self.assertEqual((chart["request_id"], chart["instrument_id"], chart["source"]),
                         ("chart-test", "000001.SZ", "csv"))
        self.assertEqual((chart["start_date"], chart["as_of_date"]), ("2026-09-21", "2026-09-25"))
        self.assertEqual((chart["price_adjustment"], chart["data_authenticity"]), ("unknown", "synthetic"))
        self.assertEqual((chart["context"], chart["saved_at"]), ("current", None))
        self.assertEqual(chart["retrieved_at"], self.result.retrieved_at.isoformat())
        self.assertEqual(chart["bars"][0], {"date": "2026-09-21", "open": 10.0, "high": 10.3,
                                           "low": 9.9, "close": 10.2, "volume": 100000})
        self.assertEqual(len(chart["series"]), 14)
        self.assertEqual({item["kind"] for item in chart["series"]}, {spec.kind for spec in SPECS})
        by_id = {item.indicator_id: item for item in self.result.indicators}
        for item in chart["series"]:
            indicator = by_id[item["indicator_id"]]
            self.assertEqual(item["id"], indicator.indicator_id + ":" + item["field"])
            self.assertEqual(item["parameters"], indicator.parameters)
            self.assertEqual(item["algorithm_version"], indicator.algorithm_version)
            self.assertIs(type(item["start_effect_warning"]), bool)
            self.assertEqual(item["values"], [row.fields[item["field"]].value for row in indicator.series])
            self.assertEqual(item["statuses"], [row.fields[item["field"]].status for row in indicator.series])
            self.assertEqual(len(item["values"]), len(chart["bars"]))

    def test_schema_does_not_export_facts_model_raw_paths_reasons_or_arbitrary_warnings(self):
        result = replace(self.result, reason=PRIVATE, message=PRIVATE, warnings=(PRIVATE,))
        payload = self.chart(result)
        self.assertEqual(set(payload), {
            "version", "status", "code", "validation_scope", "request_id", "instrument_id",
            "start_date", "as_of_date", "source", "price_adjustment", "data_authenticity",
            "retrieved_at", "context", "saved_at", "bars", "series",
        })
        for item in payload["series"]:
            self.assertEqual(set(item), {"id", "indicator_id", "kind", "parameters", "algorithm_version",
                                         "field", "label", "unit", "start_effect_warning", "values", "statuses"})
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        for forbidden in (PRIVATE, str(FIXTURE), "facts", "fact_id", "model_draft", "raw", "reason"):
            self.assertNotIn(forbidden, encoded)

    def test_partial_history_keeps_null_not_zero_and_does_not_fetch_extra_history(self):
        chart = self.chart()
        sma = next(item for item in chart["series"] if item["kind"] == "sma")
        self.assertEqual(sma["values"][:2], [None, None])
        self.assertEqual(sma["statuses"][:3], ["insufficient_history", "insufficient_history", "available"])
        request = replace(self.request, indicators=(IndicatorSpec("sma", window=120),))
        result = analyze_indicators(self.source, request, allow_development_csv=True)
        payload = self.chart(result, request)
        self.assertEqual(payload["status"], "available")
        self.assertEqual(payload["series"][0]["values"], [None] * 5)

    def test_zero_baseline_is_an_undefined_gap_not_a_zero_relative_volume(self):
        bars = [replace(bar, volume_shares=0, amount_cny=0) for bar in self.result.bars]
        bars[-1] = replace(bars[-1], volume_shares=10)
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(self.source, self.request, allow_development_csv=True)
        payload = self.chart(result)
        ratio = next(item for item in payload["series"] if item["field"] == "relative_volume")
        self.assertEqual(ratio["values"][-2:], [None, None])
        self.assertEqual(ratio["statuses"][-2:], ["undefined", "undefined"])

    def test_numeric_calculation_error_is_a_gap_and_other_indicators_remain_available(self):
        with patch("trading_companion.multi_analysis.calculations.sma_close", side_effect=ValueError(PRIVATE)):
            result = analyze_indicators(self.source, self.request, allow_development_csv=True)
        chart = self.chart(result)
        self.assertEqual(chart["status"], "available")
        self.assertEqual(chart["series"][0]["statuses"], ["calculation_error"] * 5)
        self.assertEqual(chart["series"][0]["values"], [None] * 5)
        self.assertNotIn(PRIVATE, json.dumps(chart))

    def test_recursive_start_effect_warning_is_per_field_not_a_chart_confidence_score(self):
        chart = self.chart()
        self.assertTrue(all(item["start_effect_warning"] for item in chart["series"]
                            if item["kind"] in ("ema", "macd", "rsi", "atr")))
        self.assertFalse(any(item["start_effect_warning"] for item in chart["series"]
                             if item["kind"] in ("sma", "boll", "volume")))

    def test_saved_context_requires_aware_timestamp_and_is_not_labeled_current(self):
        stamp = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)
        chart = self.chart(context="saved", saved_at=stamp)
        self.assertEqual((chart["context"], chart["saved_at"]), ("saved", stamp.isoformat()))

    def test_invalid_contexts_do_not_return_any_market_data(self):
        stamp = datetime(2026, 10, 22, tzinfo=timezone.utc)
        for context in ({"context": "other"}, {"context": []}, {"context": True},
                        {"context": "saved"}, {"context": "saved", "saved_at": date(2026, 10, 22)},
                        {"context": "saved", "saved_at": datetime(2026, 10, 22)},
                        {"context": "current", "saved_at": stamp}):
            with self.subTest(context=context):
                self.assert_unavailable(self.result, "chart_context_invalid", **context)

    def test_unknown_retrieval_time_is_null_but_naive_time_is_rejected(self):
        self.assertIsNone(self.chart(replace(self.result, retrieved_at=None))["retrieved_at"])
        self.assert_unavailable(replace(self.result, retrieved_at=datetime(2026, 10, 22)))

    def test_unverified_csv_and_provider_reported_metadata_are_distinguished(self):
        self.assertEqual(self.chart(replace(self.result, data_authenticity="unverified_csv"))["data_authenticity"],
                         "unverified_csv")
        request = replace(self.request, source="tencent")
        result = replace(self.result, request=request, source="tencent",
                         price_adjustment="unadjusted", data_authenticity="provider_reported")
        self.assertEqual(self.chart(result, request)["data_authenticity"], "provider_reported")

    def test_latest_report_evidence_must_still_validate(self):
        for result in (replace(self.result, facts=()),
                       replace(self.result, facts=(*self.result.facts, self.result.facts[0])),
                       self.changed_field(row_index=-1, value=999),
                       self.changed_field(row_index=-1, fact_id=PRIVATE)):
            with self.subTest(result=result):
                self.assert_unavailable(result)

    def test_request_binding_cannot_be_swapped_for_other_stock_dates_source_or_instances(self):
        for request in (replace(self.request, request_id="another-turn"),
                        replace(self.request, instrument_id="600000.SH"),
                        replace(self.request, start_date=date(2026, 9, 20)),
                        replace(self.request, as_of_date=date(2026, 9, 26)),
                        replace(self.request, source="tencent"),
                        replace(self.request, indicators=(IndicatorSpec("sma", window=20),))):
            with self.subTest(request=request):
                self.assert_unavailable(self.result, request=request)

    def test_reordered_instances_are_equivalent_but_display_uses_confirmed_order(self):
        request = replace(self.request, indicators=tuple(reversed(self.request.indicators)))
        chart = self.chart(request=request)
        self.assertEqual(chart["status"], "available")
        self.assertEqual(chart["series"][0]["kind"], "volume")

    def test_report_rejects_wrong_source_authenticity_and_adjustment(self):
        for changes in ({"source": "tencent"}, {"data_authenticity": "provider_reported"},
                        {"price_adjustment": "unadjusted"}, {"data_status": PRIVATE}):
            with self.subTest(changes=changes):
                self.assert_unavailable(replace(self.result, **changes))

    def test_source_failure_invalid_data_and_no_data_have_no_chart(self):
        for status in ("source_error", "invalid_data", "no_data"):
            self.assert_unavailable(replace(self.result, data_status=status, message=PRIVATE),
                                    "chart_data_unavailable")

    def test_every_bar_not_only_latest_must_have_same_instrument_valid_date_and_order(self):
        bars = self.result.bars
        variants = ((replace(bars[0], instrument_id="600000.SH"), *bars[1:]),
                    (replace(bars[0], trade_date=date(2026, 9, 20)), *bars[1:]),
                    (*bars[:-1], replace(bars[-1], trade_date=date(2026, 9, 26))),
                    (bars[0], bars[0], *bars[2:]), tuple(reversed(bars)), ())
        for variant in variants:
            with self.subTest(bars=variant):
                self.assert_unavailable(replace(self.result, bars=variant))

    def test_internal_bar_mutation_is_revalidated_including_non_exported_amount(self):
        for field, value in (("open_price", 0), ("low_price", 11), ("high_price", 9),
                             ("close_price", float("nan")), ("open_price", True),
                             ("volume_shares", -1), ("volume_shares", 1.5),
                             ("volume_shares", True), ("amount_cny", float("inf")),
                             ("trade_date", datetime(2026, 9, 21))):
            with self.subTest(field=field, value=value):
                bar = replace(self.result.bars[0])
                object.__setattr__(bar, field, value)
                self.assert_unavailable(replace(self.result, bars=(bar, *self.result.bars[1:])))

    def test_all_exported_numbers_are_bounded_finite_and_not_boolean(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, 10 ** 15 + 1, -10 ** 15 - 1):
            with self.subTest(value=value):
                self.assert_unavailable(self.changed_field(row_index=2, value=value))
        bar = replace(self.result.bars[0], volume_shares=10 ** 15 + 1)
        self.assert_unavailable(replace(self.result, bars=(bar, *self.result.bars[1:])))

    def test_historical_series_must_have_one_row_per_bar_in_identical_order(self):
        original = self.result.indicators[0]
        variants = (original.series[:-1], (*original.series, original.series[-1]),
                    tuple(reversed(original.series)),
                    (replace(original.series[0], trade_date=date(2026, 9, 20)), *original.series[1:]))
        for series in variants:
            with self.subTest(series=series):
                self.assert_unavailable(self.changed_indicator(series=series))

    def test_historical_fields_cannot_be_missing_extra_or_use_incorrect_units(self):
        indicator = self.result.indicators[0]
        for fields in ({}, {**indicator.series[0].fields, "unknown": indicator.series[0].fields["value"]}):
            self.assert_unavailable(self.changed_indicator(series=(
                replace(indicator.series[0], fields=fields), *indicator.series[1:])))
        self.assert_unavailable(self.changed_field(unit="shares"))
        self.assert_unavailable(self.changed_field(trade_date=date(2026, 9, 22)))

    def test_historical_field_counts_and_status_nullability_are_checked(self):
        for changes in ({"required_bars": True}, {"required_bars": 2}, {"actual_bars": 5},
                        {"missing_bars": 0}, {"missing_bars": True}, {"value": 0},
                        {"status": "unknown"}, {"fact_id": PRIVATE}, {"reason": PRIVATE},
                        {"status": "available", "reason": "ready", "value": 10},
                        {"status": "undefined", "reason": "zero_prior_volume_baseline"}):
            with self.subTest(changes=changes):
                self.assert_unavailable(self.changed_field(**changes))
        self.assert_unavailable(self.changed_field(row_index=2, value=None))
        self.assert_unavailable(self.changed_field(row_index=2, status="insufficient_history",
                                                   reason="insufficient_records", value=None, fact_id=None))

    def test_malformed_instances_parameters_algorithms_or_components_are_not_guessed(self):
        indicator = self.result.indicators[0]
        for changes in ({"kind": "ema"}, {"indicator_id": "invented"}, {"parameters": {"window": 20}},
                        {"parameters": {"window": True}}, {"parameters": {"window": 3.0}},
                        {"algorithm_version": PRIVATE},
                        {"components": ()}, {"components": (*indicator.components, indicator.components[0])},
                        {"components": (replace(indicator.components[0], start_effect_warning="yes"),)}):
            with self.subTest(changes=changes):
                self.assert_unavailable(self.changed_indicator(**changes))
        self.assert_unavailable(replace(self.result, indicators=self.result.indicators[:-1]))

    def test_malformed_trusted_objects_fail_closed_without_an_exception_or_echo(self):
        for result, request in ((None, self.request), ({"raw": PRIVATE}, self.request),
                                (self.result, None), (self.result, {"source": PRIVATE})):
            payload = build_chart_projection(result, expected_request=request)
            self.assertEqual(payload["code"], "chart_integrity_mismatch")
            self.assertNotIn(PRIVATE, json.dumps(payload))
        request = replace(self.request)
        object.__setattr__(request, "request_id", PRIVATE + "../")
        self.assert_unavailable(self.result, request=request)

    def test_history_is_structurally_checked_not_claimed_as_full_historical_evidence(self):
        original = self.result.indicators[0].series[2].fields["value"]
        self.assertIsNone(original.fact_id)
        chart = self.chart(self.changed_field(row_index=2, value=original.value + 0.1))
        self.assertEqual(chart["status"], "available")
        self.assertEqual(chart["validation_scope"], VALIDATION_SCOPE)
        self.assertNotIn("evidence_ids", chart)

    def test_historical_domain_checks_match_the_browser_without_recalculating_values(self):
        for index, name, value in ((3, "value", -1), (3, "value", 101),
                                   (5, "value", -1), (6, "volume_ma", -1)):
            with self.subTest(index=index, value=value):
                self.assert_unavailable(self.changed_field(indicator_index=index, row_index=2,
                                                          name=name, value=value))
        for index, name, value in ((3, "value", 0), (3, "value", 100),
                                   (5, "value", 0), (6, "volume_ma", 0),
                                   (2, "dif", -1), (4, "lower", -1)):
            with self.subTest(index=index, value=value):
                chart = self.chart(self.changed_field(indicator_index=index, row_index=2,
                                                      name=name, value=value))
                self.assertEqual(chart["status"], "available")

    def test_overlarge_bar_or_instance_count_is_rejected_not_truncated(self):
        self.assert_unavailable(replace(self.result, bars=(self.result.bars[0],) * 367), "chart_payload_limit")
        self.assert_unavailable(replace(self.result, indicators=(self.result.indicators[0],) * 13),
                                "chart_payload_limit")

    def test_series_count_bound_is_enforced(self):
        with patch("trading_companion.chart_projection.MAX_SERIES", 13):
            self.assert_unavailable(self.result, "chart_payload_limit")

    def test_maximum_instance_series_and_numeric_bounds_are_inclusive(self):
        request = replace(self.request, indicators=tuple(
            IndicatorSpec("volume", window=value) for value in range(1, 13)))
        bars = [replace(bar, volume_shares=10 ** 15) for bar in self.result.bars]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(self.source, request, allow_development_csv=True)
        chart = self.chart(result, request)
        self.assertEqual(chart["status"], "available")
        self.assertEqual(len(chart["series"]), 48)
        self.assertEqual(chart["bars"][0]["volume"], 10 ** 15)

    def test_maximum_bar_count_is_inclusive_when_payload_fits(self):
        start = date(2026, 1, 1)
        bars = [DailyBar("000001.SZ", start + timedelta(days=index), 10 ** 15, 10 ** 15,
                         10 ** 15, 10 ** 15, 0, 0) for index in range(366)]
        request = MultiIndicatorRequest("000001.SZ", start, bars[-1].trade_date,
                                        (IndicatorSpec("sma", window=3),), "csv", "maximum-bars")
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(self.source, request, allow_development_csv=True)
        chart = self.chart(result, request)
        self.assertEqual(chart["status"], "available")
        self.assertEqual(len(chart["bars"]), 366)
        self.assertEqual(chart["bars"][-1]["close"], 10 ** 15)

    def test_full_utf8_json_byte_limit_counts_metadata_and_labels(self):
        payload = self.chart()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(encoded), MAX_JSON_BYTES)
        with patch("trading_companion.chart_projection.MAX_JSON_BYTES", len(encoded)):
            self.assertEqual(self.chart()["status"], "available")
        with patch("trading_companion.chart_projection.MAX_JSON_BYTES", len(encoded) - 1):
            self.assert_unavailable(self.result, "chart_payload_limit")

    def test_real_maximum_dimensions_can_exceed_bytes_without_truncating(self):
        start = date(2026, 1, 1)
        bars = [DailyBar("000001.SZ", start + timedelta(days=index), 10, 10, 10, 10,
                         10 ** 14 + index, 0) for index in range(366)]
        request = MultiIndicatorRequest("000001.SZ", start, bars[-1].trade_date,
                                        tuple(IndicatorSpec("volume", window=value) for value in range(1, 13)),
                                        "csv", "large-chart")
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(self.source, request, allow_development_csv=True)
        self.assert_unavailable(result, "chart_payload_limit", request=request)

    def test_returned_nested_collections_are_deeply_detached(self):
        payload = self.chart()
        payload["bars"][0]["close"] = 999
        payload["series"][0]["parameters"]["window"] = 99
        payload["series"][0]["values"].clear()
        payload["series"][0]["statuses"].clear()
        payload["series"].clear()
        fresh = self.chart()
        self.assertEqual(fresh["bars"][0]["close"], 10.2)
        self.assertEqual(fresh["series"][0]["parameters"], {"window": 3})
        self.assertEqual(len(fresh["series"][0]["values"]), 5)
        self.assertEqual(self.result.bars[0].close_price, 10.2)

    def test_projection_is_pure_no_source_reads_model_calls_or_indicator_recalculation(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No reads")) as read, \
                patch("trading_companion.multi_analysis.analyze_indicators", side_effect=AssertionError("No analysis")) as analyze, \
                patch("trading_companion.multi_analysis.calculations.sma_close", side_effect=AssertionError("No calculation")) as calculate:
            first, second = self.chart(), self.chart()
        self.assertEqual(first, second)
        read.assert_not_called()
        analyze.assert_not_called()
        calculate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
