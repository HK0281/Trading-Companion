from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_synthesis import build_research_synthesis


START = date(2026, 1, 1)
PRIVATE = "unverified-model-or-provider-prose-not-for-display"
SPECS = (
    IndicatorSpec("sma", window=3), IndicatorSpec("ema", window=3),
    IndicatorSpec("macd", fast=2, slow=3, signal=2), IndicatorSpec("rsi", window=2),
    IndicatorSpec("boll", window=3), IndicatorSpec("atr", window=2),
    IndicatorSpec("volume", window=3),
)


class ArtificialSource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def __init__(self, bars):
        self.bars, self.reads = bars, 0

    def read_daily_bars(self):
        self.reads += 1
        return self.bars


def analyze(closes, *, specs=SPECS, volumes=None):
    if volumes is None:
        volumes = [100] * len(closes)
    bars = [DailyBar("000001.SZ", START + timedelta(days=index), close, close,
                     close, close, volume, 0)
            for index, (close, volume) in enumerate(zip(closes, volumes))]
    request = MultiIndicatorRequest("000001.SZ", START, bars[-1].trade_date if bars else START,
                                    specs, "csv", "synthesis-test")
    source = ArtificialSource(bars)
    result = analyze_indicators(source, request, allow_development_csv=True)
    return request, result, source


def changed_field(result, *, kind="sma", name="value", offset=0, update_fact=False, **changes):
    indicators = list(result.indicators)
    index = next(index for index, item in enumerate(indicators) if item.kind == kind)
    indicator = indicators[index]
    rows = list(indicator.series)
    row_index = len(rows) - 1 - offset
    original = rows[row_index].fields[name]
    value = replace(original, **changes)
    rows[row_index] = replace(rows[row_index], fields={**rows[row_index].fields, name: value})
    indicators[index] = replace(indicator, series=tuple(rows))
    facts = result.facts
    if update_fact:
        facts = tuple(replace(fact, value=value.value) if fact.fact_id == original.fact_id else fact for fact in facts)
    return replace(result, indicators=tuple(indicators), facts=facts)


class ResearchSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.request, self.result, self.source = analyze([10, 10, 10, 10, 10, 11, 13, 17, 25])

    def synthesis(self, result=None, request=None):
        return build_research_synthesis(self.result if result is None else result,
                                         expected_request=self.request if request is None else request)

    def assert_blocked(self, result, request=None):
        synthesis = self.synthesis(result, request)
        self.assertEqual((synthesis.status, synthesis.code), ("blocked", "synthesis_evidence_mismatch"))
        self.assertEqual(synthesis.dimensions, ())
        self.assertIsNone(synthesis.request_id)
        self.assertNotIn(PRIVATE, json.dumps(synthesis.to_dict()))

    def test_real_artificial_csv_covers_seven_families_with_request_bound_dependencies(self):
        request = replace(self.request, start_date=date(2026, 9, 21), as_of_date=date(2026, 9, 25))
        source = CsvDailyBarSource(Path(__file__).resolve().parents[1] / "data" / "sample_daily_bars_synthetic.csv",
                                   is_synthetic=True)
        result = analyze_indicators(source, request, allow_development_csv=True)
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.status, "complete")
        self.assertEqual([item.dimension for item in synthesis.dimensions], ["trend", "momentum", "volatility", "volume"])
        facts = {item.fact_id: item for item in result.facts}
        for dimension in synthesis.dimensions:
            for observation in dimension.observations:
                self.assertTrue(observation.dependencies)
                for identifier in observation.dependencies:
                    self.assertIn(identifier, facts)
                    self.assertEqual(facts[identifier].request_id, request.request_id)
                    self.assertEqual(facts[identifier].instrument_id, request.instrument_id)
        payload = synthesis.to_dict()
        self.assertEqual(payload["validation_scope"], "request_local_numeric_reference_comparisons_not_predictions")
        self.assertEqual(payload["request_id"], request.request_id)
        self.assertEqual(payload["trade_date"], "2026-09-25")
        self.assertTrue(payload["evidence_ids"])
        self.assertEqual(len(payload["evidence_ids"]), len(set(payload["evidence_ids"])))

    def test_accelerating_reference_positions_can_be_aligned_without_a_prediction(self):
        synthesis = self.synthesis()
        self.assertEqual(synthesis.status, "complete")
        self.assertEqual(synthesis.cross_dimension.state, "aligned_upper_reference")
        self.assertIn("仅说明本次参考关系相容", synthesis.cross_dimension.text)
        self.assertIn("不代表独立确认或未来走势", synthesis.text)

    def test_lower_reference_alignment_does_not_become_a_sell_instruction(self):
        request, result, _ = analyze([25, 25, 25, 25, 25, 24, 22, 18, 10])
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.cross_dimension.state, "aligned_lower_reference")
        self.assertNotIn("应该卖", synthesis.text)

    def test_trend_and_momentum_reference_conflict_is_explicit(self):
        specs = (IndicatorSpec("sma", window=8), *SPECS[2:])
        request, result, _ = analyze([1, 2, 3, 4, 5, 6, 7, 15, 14, 13, 12, 11], specs=specs)
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.dimensions[0].state, "upper_reference")
        self.assertEqual(synthesis.dimensions[1].state, "lower_reference")
        self.assertEqual(synthesis.cross_dimension.state, "conflicting")
        self.assertIn("不一致", synthesis.cross_dimension.text)

    def test_many_same_family_instances_do_not_outvote_one_conflicting_instance(self):
        means = tuple(IndicatorSpec("sma", window=window) for window in (1, 2, 3, 4, 5, 6, 7))
        request, result, _ = analyze([20, 20, 20, 20, 20, 10, 10, 11], specs=(*means, *SPECS[2:]))
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.dimensions[0].state, "mixed_reference")
        self.assertEqual(synthesis.cross_dimension.state, "conflicting")
        payload = synthesis.to_dict()
        for forbidden in ("score", "probability", "votes", "confidence", "target_price"):
            self.assertNotIn(forbidden, payload)
        self.assertIn("同族多参数实例分别展示但不计票", synthesis.text)

    def test_reference_equality_is_neutral_not_a_false_contradiction(self):
        result = changed_field(self.result, kind="rsi", value=50, update_fact=True)
        synthesis = self.synthesis(result)
        self.assertEqual(synthesis.dimensions[1].state, "boundary_mixed")
        self.assertEqual(synthesis.cross_dimension.state, "indeterminate")
        self.assertNotEqual(synthesis.cross_dimension.state, "conflicting")

    def test_flat_zero_macd_atr_and_rsi50_are_available_not_missing(self):
        request, result, _ = analyze([10] * 9, volumes=[100] * 9)
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.status, "complete")
        self.assertEqual(synthesis.dimensions[0].state, "at_reference")
        self.assertEqual(synthesis.dimensions[1].state, "at_reference")
        self.assertEqual(synthesis.cross_dimension.state, "indeterminate")
        observations = [item for dimension in synthesis.dimensions for item in dimension.observations]
        self.assertTrue(any(item.relation == "atr_scale" and item.value == "zero" for item in observations))
        self.assertTrue(any(item.relation == "bands_state" and item.value == "coincident" for item in observations))

    def test_rsi_30_70_boundaries_are_inclusive_and_zero_is_valid(self):
        cases = ((0, "below_30", "lower_reference"), (30, "between_30_and_70", "lower_reference"),
                 (50, "between_30_and_70", "at_reference"), (70, "between_30_and_70", "upper_reference"),
                 (100, "above_70", "upper_reference"))
        for value, zone, midpoint in cases:
            with self.subTest(value=value):
                synthesis = self.synthesis(changed_field(self.result, kind="rsi", value=value, update_fact=True))
                observations = synthesis.dimensions[1].observations
                self.assertEqual(next(item.value for item in observations if item.relation == "rsi_zone"), zone)
                self.assertEqual(next(item.value for item in observations if item.relation == "rsi_midpoint"), midpoint)

    def test_zero_prior_volume_is_undefined_not_zero_or_infinite_volume(self):
        request, result, _ = analyze([10] * 9, volumes=[0] * 8 + [100])
        synthesis = self.synthesis(result, request)
        volume = synthesis.dimensions[3]
        self.assertEqual((volume.status, volume.state), ("limited", "unknown"))
        self.assertTrue(any(item.status == "undefined" for item in volume.missing))
        self.assertIn("不能当作零倍或无限放量", volume.text)

    def test_current_zero_volume_against_nonzero_baseline_is_a_valid_lower_reference(self):
        request, result, _ = analyze([10] * 9, volumes=[100] * 8 + [0])
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.dimensions[3].state, "lower_reference")
        self.assertEqual(synthesis.dimensions[3].status, "available")

    def test_relative_volume_reference_must_agree_with_its_numerical_operands(self):
        result = changed_field(self.result, kind="volume", name="relative_volume", value=0.5, update_fact=True)
        self.assert_blocked(result)

    def test_provider_reported_unadjusted_prices_keep_company_action_limits(self):
        request = replace(self.request, source="tencent")
        result = replace(self.result, request=request, source="tencent", data_authenticity="provider_reported",
                         price_adjustment="unadjusted")
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.status, "complete")
        self.assertIn("不是实时行情", synthesis.text)
        self.assertIn("公司行动可能影响价格和指标", synthesis.text)
        self.assertNotIn("人工样例 / 非真实行情", synthesis.text)

    def test_volume_does_not_vote_for_price_direction(self):
        request, result, _ = analyze([10, 10, 10, 10, 10, 11, 13, 17, 25], volumes=[100] * 8 + [0])
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.dimensions[3].state, "lower_reference")
        self.assertEqual(synthesis.cross_dimension.state, "aligned_upper_reference")
        fact_index = {item.fact_id: item for item in result.facts}
        self.assertTrue(all(fact_index[key].indicator_id != SPECS[-1].indicator_id
                            for key in synthesis.cross_dimension.dependencies))

    def test_volatility_is_described_as_scale_not_direction(self):
        synthesis = self.synthesis()
        volatility = synthesis.dimensions[2]
        self.assertEqual(volatility.state, "increasing")
        self.assertIn("不是涨跌判断", volatility.text)
        self.assertIn("不把波动增加解释为上涨或下跌", volatility.text)
        self.assertIn("波动与量能不参与价格方向表决", synthesis.text)

    def test_disagreement_between_boll_width_and_atr_is_not_forced_into_one_scale(self):
        result = changed_field(self.result, kind="atr", value=0, update_fact=True)
        synthesis = self.synthesis(result)
        self.assertEqual(synthesis.dimensions[2].state, "mixed_scale")
        self.assertEqual(synthesis.cross_dimension.state, "aligned_upper_reference")

    def test_absent_dimensions_are_unknown_not_implicitly_normal(self):
        request, result, _ = analyze([10, 11, 12, 13, 14], specs=(IndicatorSpec("sma", window=3),))
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.status, "limited")
        for dimension in synthesis.dimensions[1:]:
            self.assertEqual((dimension.status, dimension.state), ("unknown", "unknown"))
            self.assertTrue(all(item.status == "not_requested" for item in dimension.missing))
        self.assertEqual(synthesis.cross_dimension.state, "incomplete")

    def test_missing_one_momentum_family_prevents_claiming_complete_cross_dimension_agreement(self):
        specs = tuple(spec for spec in SPECS if spec.kind != "rsi")
        request, result, _ = analyze([10, 10, 10, 10, 10, 11, 13, 17, 25], specs=specs)
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.dimensions[1].status, "limited")
        self.assertEqual(synthesis.cross_dimension.state, "incomplete")

    def test_short_history_and_unavailable_previous_record_do_not_fabricate_changes(self):
        for closes in ([10], [10, 11, 12]):
            request, result, _ = analyze(closes)
            synthesis = self.synthesis(result, request)
            self.assertEqual(synthesis.status, "limited")
            self.assertTrue(synthesis.dimensions[0].missing)
            self.assertFalse(any(item.relation == "mean_change" for item in synthesis.dimensions[0].observations))
            self.assertEqual(synthesis.cross_dimension.state, "incomplete")

    def test_calculation_failure_keeps_other_dimensions_without_leaking_exception(self):
        with patch("trading_companion.multi_analysis.calculations.macd_close", side_effect=ValueError(PRIVATE)):
            request, result, _ = analyze([10] * 9)
        synthesis = self.synthesis(result, request)
        self.assertEqual(synthesis.status, "limited")
        self.assertTrue(synthesis.dimensions[0].observations)
        self.assertTrue(any(item.status == "calculation_error" for item in synthesis.dimensions[1].missing))
        self.assertNotIn(PRIVATE, synthesis.text)

    def test_initialization_adjustment_and_actual_date_limitations_are_preserved(self):
        request = replace(self.request, as_of_date=self.request.as_of_date + timedelta(days=1))
        result = replace(self.result, request=request)
        synthesis = self.synthesis(result, request)
        for text in ("复权口径未知", "计算起点", "实际最新交易日早于请求截止日", "不等于严格point-in-time"):
            self.assertIn(text, synthesis.text)

    def test_wrong_request_id_stock_dates_source_and_indicator_parameters_block_all_conclusions(self):
        for request in (replace(self.request, request_id="other"), replace(self.request, instrument_id="600000.SH"),
                        replace(self.request, start_date=self.request.start_date - timedelta(days=1)),
                        replace(self.request, as_of_date=self.request.as_of_date + timedelta(days=1)),
                        replace(self.request, source="tencent"),
                        replace(self.request, indicators=(IndicatorSpec("sma", window=4),))):
            self.assert_blocked(self.result, request)

    def test_missing_duplicate_or_corrupt_current_numeric_evidence_blocks(self):
        self.assert_blocked(replace(self.result, facts=()))
        self.assert_blocked(replace(self.result, facts=(*self.result.facts, self.result.facts[0])))
        self.assert_blocked(changed_field(self.result, value=123))
        self.assert_blocked(changed_field(self.result, unit="shares"))
        self.assert_blocked(changed_field(self.result, fact_id=PRIVATE))
        for value in (True, float("nan"), float("inf"), float("-inf")):
            self.assert_blocked(changed_field(self.result, value=value, update_fact=True))

    def test_preceding_operand_evidence_and_exact_threshold_are_validated_independently(self):
        for changes in ({"value": 99}, {"actual_bars": 999}, {"required_bars": 1}, {"missing_bars": 2},
                        {"unit": "shares"}, {"trade_date": START}, {"fact_id": PRIVATE}):
            self.assert_blocked(changed_field(self.result, offset=1, **changes))
        self.assert_blocked(changed_field(self.result, kind="macd", name="histogram", offset=1, value=100))
        self.assert_blocked(changed_field(self.result, kind="boll", name="upper", offset=1, value=100))
        self.assert_blocked(changed_field(self.result, kind="atr", offset=1, value=100))

    def test_operand_fact_request_stock_field_parameters_and_dependencies_cannot_be_swapped(self):
        identifier = self.result.indicators[0].series[-2].fields["value"].fact_id
        for changes in ({"request_id": "other"}, {"instrument_id": "600000.SH"}, {"field": "close_price"},
                        {"parameters": {"window": 4}}, {"dependencies": (identifier,)}, {"unit": "multiple"}):
            facts = tuple(replace(item, **changes) if item.fact_id == identifier else item for item in self.result.facts)
            self.assert_blocked(replace(self.result, facts=facts))

    def test_noncanonical_fact_ids_are_not_echoed_even_if_latest_report_would_match(self):
        identifier = self.result.indicators[0].latest["value"].fact_id
        result = changed_field(self.result, fact_id=PRIVATE)
        facts = tuple(replace(item, fact_id=PRIVATE) if item.fact_id == identifier else item for item in result.facts)
        self.assert_blocked(replace(result, facts=facts))

    def test_malformed_fields_metadata_or_internal_daily_bar_are_not_used(self):
        indicator = self.result.indicators[0]
        variants = (replace(indicator, algorithm_version=PRIVATE),
                    replace(indicator, parameters={"window": 3.0}),
                    replace(indicator, series=indicator.series[:-1]),
                    replace(indicator, components=(replace(indicator.components[0], start_effect_warning=PRIVATE),)))
        for value in variants:
            self.assert_blocked(replace(self.result, indicators=(value, *self.result.indicators[1:])))
        bar = replace(self.result.bars[-1])
        object.__setattr__(bar, "open_price", 0)
        self.assert_blocked(replace(self.result, bars=(*self.result.bars[:-1], bar)))

    def test_out_of_contract_rsi_atr_volume_and_boll_bounds_are_blocked(self):
        self.assert_blocked(changed_field(self.result, kind="rsi", value=101, update_fact=True))
        self.assert_blocked(changed_field(self.result, kind="rsi", value=-1, update_fact=True))
        self.assert_blocked(changed_field(self.result, kind="atr", value=-1, update_fact=True))
        self.assert_blocked(changed_field(self.result, kind="boll", name="lower", value=999, update_fact=True))
        self.assert_blocked(changed_field(self.result, kind="volume", name="relative_volume", value=-1, update_fact=True))

    def test_unverified_relation_labels_and_model_or_provider_text_are_never_promoted(self):
        relations = tuple(replace(item, value=PRIVATE, rule=PRIVATE) for item in self.result.relations)
        facts = tuple(replace(item, value=PRIVATE) if item.unit == "relation" else item for item in self.result.facts)
        result = replace(self.result, relations=relations, facts=facts, message=PRIVATE, reason=PRIVATE, warnings=(PRIVATE,))
        expected = self.synthesis()
        observed = self.synthesis(result)
        self.assertEqual(observed, expected)
        self.assertNotIn(PRIVATE, json.dumps(observed.to_dict()))

    def test_conditions_have_current_evidence_and_require_new_records_not_prediction(self):
        synthesis = self.synthesis()
        self.assertEqual(len(synthesis.conditions), 4)
        self.assertIn("需要新的记录核对", synthesis.text)
        for item in synthesis.conditions:
            self.assertTrue(item.dependencies)
            self.assertIn("若后续", item.text)
        for unsupported in ("保证上涨", "建议买入", "建议卖出", "明天会上涨", "上涨概率为", "目标价为"):
            self.assertNotIn(unsupported, synthesis.text)

    def test_no_data_and_provider_failures_have_safe_unavailable_summary(self):
        for state in ("no_data", "source_error", "invalid_data"):
            result = replace(self.result, data_status=state, bars=(), message=PRIVATE)
            synthesis = self.synthesis(result)
            self.assertEqual((synthesis.status, synthesis.code), ("unavailable", "synthesis_data_unavailable"))
            self.assertEqual(synthesis.dimensions, ())
            self.assertNotIn(PRIVATE, synthesis.text)

    def test_null_wrong_type_and_malformed_trusted_objects_fail_closed(self):
        for result, request in ((None, self.request), (self.result, None), ({"message": PRIVATE}, self.request)):
            synthesis = build_research_synthesis(result, expected_request=request)
            self.assertEqual(synthesis.status, "blocked")
            self.assertIsNone(synthesis.request_id)
            self.assertNotIn(PRIVATE, json.dumps(synthesis.to_dict()))

    def test_payload_limit_is_whole_response_not_a_partial_summary(self):
        synthesis = self.synthesis()
        size = len(json.dumps(synthesis.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        with patch("trading_companion.research_synthesis.MAX_PUBLIC_BYTES", size):
            self.assertEqual(self.synthesis().status, "complete")
        with patch("trading_companion.research_synthesis.MAX_PUBLIC_BYTES", size - 1):
            rejected = self.synthesis()
            self.assertEqual(rejected.code, "synthesis_payload_limit")
            self.assertEqual(rejected.dimensions, ())

    def test_projection_is_pure_repeatable_and_to_dict_is_deeply_detached(self):
        with patch.object(ArtificialSource, "read_daily_bars", side_effect=AssertionError("No reads")) as source, \
                patch("trading_companion.multi_analysis.calculations.sma_close", side_effect=AssertionError("No calculation")) as calculate, \
                patch("socket.create_connection", side_effect=AssertionError("No network")) as network:
            synthesis, repeated = self.synthesis(), self.synthesis()
        self.assertEqual(synthesis, repeated)
        payload = synthesis.to_dict()
        payload["dimensions"][0]["observations"][0]["text"] = PRIVATE
        payload["dimensions"].clear()
        payload["evidence_ids"].clear()
        payload["limitations"].clear()
        self.assertNotIn(PRIVATE, json.dumps(synthesis.to_dict()))
        self.assertTrue(synthesis.to_dict()["evidence_ids"])
        for guard in (source, calculate, network):
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
