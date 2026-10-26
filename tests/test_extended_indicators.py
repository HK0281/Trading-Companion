"""Independent references and boundary cases for added numeric indicators."""

from datetime import date, timedelta
from decimal import Decimal, localcontext
from math import isfinite

import pytest

from trading_companion.daily_bar import DailyBar
from trading_companion.errors import InvalidRequestError
from trading_companion.indicators import AdxValue, VwmaValue, adx, obv, vwma
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import DEFAULT_INDICATORS, IndicatorSpec, MultiIndicatorRequest


def bars_for(closes, volumes=None, spreads=None):
    volumes = [100] * len(closes) if volumes is None else volumes
    spreads = [0] * len(closes) if spreads is None else spreads
    return [DailyBar("000001.SZ", date(2026, 1, 1) + timedelta(days=index),
                     close, close + spread, close - spread, close, volume, 0)
            for index, (close, volume, spread) in enumerate(zip(closes, volumes, spreads))]


def decimal_adx_reference(bars, window):
    """Use high-precision Wilder sums, independently of the mean-based implementation."""
    with localcontext() as context:
        context.prec = 65
        changes = []
        for previous, current in zip(bars, bars[1:]):
            ph, pl = Decimal(str(previous.high_price)), Decimal(str(previous.low_price))
            pc = Decimal(str(previous.close_price))
            ch, cl = Decimal(str(current.high_price)), Decimal(str(current.low_price))
            upward, downward = ch - ph, pl - cl
            changes.append((max(ch - cl, abs(ch - pc), abs(cl - pc)),
                            upward if upward > 0 and upward > downward else Decimal(0),
                            downward if downward > 0 and downward > upward else Decimal(0)))
        results = [AdxValue(None, None, None) for _ in bars]
        if len(changes) < window:
            return results
        totals = [sum((row[column] for row in changes[:window]), Decimal(0))
                  for column in range(3)]
        dx_history = []
        previous_adx = None
        for index in range(window, len(bars)):
            if index > window:
                totals = [value - value / window + change
                          for value, change in zip(totals, changes[index - 1])]
            tr, plus, minus = totals
            plus_di = 100 * plus / tr if tr else Decimal(0)
            minus_di = 100 * minus / tr if tr else Decimal(0)
            dx = (100 * abs(plus_di - minus_di) / (plus_di + minus_di)
                  if plus_di + minus_di else Decimal(0))
            dx_history.append(dx)
            if len(dx_history) == window:
                previous_adx = sum(dx_history, Decimal(0)) / window
            elif len(dx_history) > window:
                previous_adx = (previous_adx * (window - 1) + dx) / window
            results[index] = AdxValue(float(previous_adx) if previous_adx is not None else None,
                                      float(plus_di), float(minus_di))
        return results


@pytest.mark.parametrize("window", [1, 2, 3, 14, 30])
def test_adx_matches_high_precision_independent_wilder_reference(window):
    closes = [10.2, 10.35, 10.1, 10, 10.4, 9.8, 9.8, 10.3, 10.1, 10.8] * 9
    bars = bars_for(closes, spreads=[0.2, 0.1, 0.4] * 30)
    actual = adx(bars, window)
    expected = decimal_adx_reference(bars, window)
    for left, right in zip(actual, expected):
        for name in ("value", "plus_di", "minus_di"):
            value, reference = getattr(left, name), getattr(right, name)
            if reference is None:
                assert value is None
            else:
                assert value == pytest.approx(reference, abs=1e-11)


def test_adx_full_warmup_counts_flat_and_monotone_directions():
    flat = adx(bars_for([10] * 30))
    assert all(point.plus_di is None for point in flat[:14])
    assert flat[14] == AdxValue(None, 0, 0)
    assert all(point.value is None for point in flat[:27])
    assert flat[27:] == [AdxValue(0, 0, 0)] * 3
    assert adx(bars_for(list(range(1, 31))))[-1] == AdxValue(100, 100, 0)
    assert adx(bars_for(list(range(30, 0, -1))))[-1] == AdxValue(100, 0, 100)


def test_adx_equal_directional_moves_do_not_count_both_directions():
    bars = bars_for([50] * 20, spreads=list(range(20)))
    assert adx(bars, 3)[-1] == AdxValue(0, 0, 0)


def test_adx_extreme_finite_prices_do_not_overflow_smoothing():
    bars = bars_for([1e-308, 1e308, 1e-308, 1e308, 1e-308, 1e308])
    for point in adx(bars, 2):
        for value in (point.value, point.plus_di, point.minus_di):
            assert value is None or isfinite(value) and 0 <= value <= 100


def test_obv_first_anchor_flat_prices_and_exact_large_signed_shares():
    huge = 10**400 + 2**53 + 17
    bars = bars_for([10, 11, 11, 9, 8, 12], [huge, huge, 999, huge + 3, 7, 20])
    assert obv(bars) == [0, huge, huge, -3, -10, 10]
    assert all(type(value) is int for value in obv(bars))
    assert obv(bars[1:])[0] == 0


def test_vwma_hand_reference_and_full_window_zero_volume():
    bars = bars_for([10, 20, 30, 40, 50], [0, 0, 2, 1, 3])
    assert vwma(bars, 2) == [VwmaValue(None, "insufficient_history"),
                             VwmaValue(None, "undefined"),
                             VwmaValue(30, "available"),
                             VwmaValue(100 / 3, "available"),
                             VwmaValue(47.5, "available")]
    assert vwma(bars[:2], 3) == [VwmaValue(None, "insufficient_history")] * 2


def test_vwma_very_large_volume_and_price_products_remain_finite():
    huge = 10**400
    values = vwma(bars_for([1e308, 1e308], [huge, huge]), 2)
    assert values[-1] == VwmaValue(1e308, "available")
    # The first small volume still matters when its associated price is huge.
    assert vwma(bars_for([1e308, 1], [1, 10**20]), 2)[-1].value == pytest.approx(1e288)
    assert vwma(bars_for([10, 30], [huge, huge]), 2)[-1].value == 20


@pytest.mark.parametrize("function,kwargs", [(adx, {"window": 3}), (vwma, {"window": 3}), (obv, {})])
def test_new_indicators_are_prefix_invariant_and_leave_inputs_unchanged(function, kwargs):
    bars = bars_for([10, 12, 11, 13, 10, 15, 14, 16], [0, 3, 7, 2, 5, 9, 1, 4])
    original = list(bars)
    whole = function(bars, **kwargs)
    assert function([], **kwargs) == []
    for length in range(1, len(bars) + 1):
        assert function(bars[:length], **kwargs) == whole[:length]
    assert bars == original


@pytest.mark.parametrize("function", [adx, vwma])
@pytest.mark.parametrize("window", [0, -1, True, None, 2.5, "14"])
def test_new_numeric_functions_reject_bad_windows_before_reading(function, window):
    with pytest.raises(ValueError):
        function([], window)


@pytest.mark.parametrize("function", [adx, obv, vwma])
def test_new_numeric_functions_reject_mixed_instruments_and_reordered_dates(function):
    from dataclasses import replace

    bars = bars_for([10, 11, 12])
    for invalid in ([bars[1], bars[0]], [bars[0], replace(bars[1], instrument_id="600000.SH")],
                    [bars[0], bars[0]], "bars"):
        with pytest.raises(ValueError):
            function(invalid)


class Source:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def __init__(self, bars):
        self.bars, self.calls = bars, 0

    def read_daily_bars(self):
        self.calls += 1
        return self.bars


def analyze(bars, *specs):
    source = Source(bars)
    request = MultiIndicatorRequest("000001.SZ", bars[0].trade_date, bars[-1].trade_date,
                                    tuple(specs), "csv", "extended-test")
    result = analyze_indicators(source, request, allow_development_csv=True)
    assert source.calls == 1
    return result


def test_new_indicator_contracts_defaults_parameters_and_versions():
    assert len(DEFAULT_INDICATORS) == 8
    assert IndicatorSpec("obv").indicator_id == "obv()"
    assert IndicatorSpec("adx").parameters == {"window": 14}
    assert IndicatorSpec("vwma").parameters == {"window": 20}
    assert IndicatorSpec("vwma", window=1).parameters == {"window": 1}
    for kind, parameters in (("obv", {"window": 14}), ("adx", {"window": 1}),
                             ("adx", {"window": True}), ("vwma", {"window": 0})):
        with pytest.raises(InvalidRequestError):
            IndicatorSpec(kind, **parameters)


def test_new_results_have_bound_metadata_facts_and_deterministic_relations():
    huge = 2**53 + 17
    result = analyze(bars_for(list(range(1, 33)), [huge] * 32),
                     IndicatorSpec("adx"), IndicatorSpec("obv"), IndicatorSpec("vwma"))
    adx_result, obv_result, vwma_result = result.indicators
    assert adx_result.latest["value"].required_bars == 28
    assert adx_result.latest["plus_di"].required_bars == 15
    assert adx_result.latest["minus_di"].unit == "index_points"
    assert adx_result.components[0].first_available_date == result.bars[27].trade_date
    assert adx_result.algorithm_version == "adx-wilder-double-smoothed-v1"
    assert obv_result.latest["value"].value == huge * 31
    assert obv_result.latest["value"].unit == "shares"
    assert type(next(fact for fact in result.facts if fact.indicator_id == "obv()"
                     and fact.trade_date == result.latest_trade_date).value) is int
    assert vwma_result.latest["value"].value == 22.5
    relations = {(item.indicator_id, item.field): item for item in result.relations}
    assert relations[("adx(window=14)", "strength_zone")].value == "at_or_above_25"
    assert relations[("adx(window=14)", "di_position")].value == "above"
    assert relations[("obv()", "obv_change")].value == "increasing"
    assert relations[("vwma(window=20)", "price_position")].value == "above"
    ids = {fact.fact_id for fact in result.facts}
    assert all(dependency in ids for fact in result.facts for dependency in fact.dependencies)


def test_adx_directional_evidence_can_be_available_before_adx_warmup():
    result = analyze(bars_for(list(range(1, 16))), IndicatorSpec("adx"))
    assert result.calculation_status == "limited"
    assert result.indicators[0].latest["value"].missing_bars == 13
    assert result.indicators[0].latest["plus_di"].status == "available"
    assert next(item for item in result.relations if item.field == "strength_zone").status == "insufficient_history"
    assert next(item for item in result.relations if item.field == "di_position").value == "above"


def test_vwma_zero_volume_window_has_no_numeric_fact_and_can_recover():
    result = analyze(bars_for([10] * 4, [0, 0, 0, 0]), IndicatorSpec("vwma", window=3))
    value = result.indicators[0].latest["value"]
    assert (value.status, value.reason, value.value, value.missing_bars) == ("undefined", "zero_window_volume", None, 0)
    assert not any(fact.indicator_id.startswith("vwma(") for fact in result.facts)
    assert all(relation.status == "undefined" for relation in result.relations)
    recovery = analyze(bars_for([10] * 4, [0, 0, 0, 1]), IndicatorSpec("vwma", window=3))
    component = recovery.indicators[0].components[0]
    assert component.first_available_date == recovery.bars[3].trade_date
    assert recovery.indicators[0].latest["value"].value == 10


def execute_research_tool(bars, specs):
    """Exercise the real tool adapter with only the physical CSV read substituted."""
    from pathlib import Path
    from unittest.mock import patch

    from trading_companion.data_source import CsvDailyBarSource
    from trading_companion.research_tool import TOOL_NAME, ResearchToolExecutor
    from trading_companion.tool_model import ToolCall, ToolMessage, ToolModelResponse

    arguments = {"instrument_id": "000001.SZ", "start_date": bars[0].trade_date.isoformat(),
                 "as_of_date": bars[-1].trade_date.isoformat(),
                 "indicators": [{"kind": spec.kind, **spec.parameters} for spec in specs]}
    response = ToolModelResponse("tool_calls", tool_calls=(ToolCall("extended_call", TOOL_NAME, arguments),))
    source = CsvDailyBarSource(Path("trusted-extended-fixture-not-opened.csv"), is_synthetic=True)
    with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars) as read:
        execution = ResearchToolExecutor(development_source=source, request_id="extended-tool").execute(
            response, messages=[ToolMessage("user", "Explicit offline synthetic indicator research")])
    read.assert_called_once_with()
    assert execution.status == "executed"
    return execution


@pytest.mark.parametrize("case", ["descending", "zero_volume", "adx_threshold", "adx_warmup"])
def test_new_indicators_cross_report_synthesis_chart_and_tool_boundaries(case):
    from trading_companion.chart_projection import build_chart_projection
    from trading_companion.research_report import build_research_report
    from trading_companion.research_synthesis import build_research_synthesis
    from trading_companion.research_tool import SEED_METHOD_CATALOG, _project

    if case == "descending":
        bars = bars_for(list(range(40, 8, -1)))
        specs = (IndicatorSpec("adx"), IndicatorSpec("obv"), IndicatorSpec("vwma"))
    elif case == "zero_volume":
        bars = bars_for([10] * 32, [0] * 32)
        specs = (IndicatorSpec("adx"), IndicatorSpec("obv"), IndicatorSpec("vwma"))
    else:
        bars = bars_for([10, 11, 10, 11] if case == "adx_threshold" else [10, 11, 10])
        specs = (IndicatorSpec("adx", window=2), IndicatorSpec("obv"), IndicatorSpec("vwma", window=2))
    execution = execute_research_tool(bars, specs)
    result, request = execution.full_result, execution.full_result.request
    assert result.bars == tuple(bars)
    report = build_research_report(result, expected_request=request)
    synthesis = build_research_synthesis(result, expected_request=request)
    chart = build_chart_projection(result, expected_request=request)
    assert report.status in ("complete", "limited"), report.code
    assert synthesis.status in ("complete", "limited"), synthesis.code
    assert chart["status"] == "available", chart
    assert {series["kind"] for series in chart["series"]} == {"adx", "obv", "vwma"}
    payload, aliases = _project(result, development=True)
    assert payload == execution.messages[0].result
    assert dict(aliases) == execution.fact_aliases
    component_rows = payload["tables"]["components"]
    seed_index = component_rows["columns"].index("seed")
    projected_seeds = [SEED_METHOD_CATALOG[row[seed_index]] for row in component_rows["rows"]]
    assert projected_seeds == [component.seed_method for indicator in result.indicators
                               for component in indicator.components]
    latest_obv = next(field for field in report.fields if field.indicator_id == "obv()")
    if case == "descending":
        assert latest_obv.value == -3100 and type(latest_obv.value) is int
        assert any("OBV" in field.label for field in report.fields)
        assert next(series for series in chart["series"] if series["kind"] == "obv")["values"][-1] == -3100
    elif case == "zero_volume":
        vwma_field = next(field for field in report.fields if field.indicator_id.startswith("vwma("))
        assert vwma_field.status == "undefined"
        assert next(series for series in chart["series"] if series["kind"] == "vwma")["statuses"][-1] == "undefined"
        assert result.indicators[0].latest["value"].value == 0
    elif case == "adx_threshold":
        assert result.indicators[0].latest["value"].value == 25
        assert next(relation for relation in result.relations if relation.field == "strength_zone").value == "at_or_above_25"
    else:
        assert report.status == "limited"
        assert result.indicators[0].latest["value"].missing_bars == 1


def test_new_indicator_tool_projection_units_do_not_fall_back_to_prices():
    specs = (IndicatorSpec("adx", window=2), IndicatorSpec("obv"), IndicatorSpec("vwma", window=2))
    execution = execute_research_tool(bars_for([10, 11, 10, 11]), specs)
    units = execution.messages[0].result["units"]
    assert units["adx.value"] == "index_points"
    assert units["adx.plus_di"] == "index_points"
    assert units["adx.minus_di"] == "index_points"
    assert units["obv.value"] == "shares"


def test_negative_large_obv_preserves_report_tool_and_checked_claim_integer_values():
    from trading_companion.chart_projection import build_chart_projection
    from trading_companion.relation_explanation import EXPLANATION_VERSION, validate_indicator_explanation
    from trading_companion.research_report import build_research_report
    from trading_companion.research_synthesis import build_research_synthesis
    import json

    huge = 2**53 + 17
    bars = bars_for([10, 9, 8, 7], [huge] * 4)
    execution = execute_research_tool(bars, (IndicatorSpec("obv"),))
    result, request = execution.full_result, execution.full_result.request
    report = build_research_report(result, expected_request=request)
    assert report.status == "complete"
    value = next(field for field in report.fields if field.indicator_id == "obv()").value
    assert value == -huge * 3 and type(value) is int
    assert str(-huge * 3) in report.text
    assert build_research_synthesis(result, expected_request=request).status != "blocked"
    # The chart has a bounded JS-safe numeric domain; it must reject rather than round this.
    assert build_chart_projection(result, expected_request=request)["status"] == "unavailable"
    relation = next(item for item in result.relations if item.field == "obv_change")
    alias = next(alias for alias, fact in execution.fact_aliases.items() if fact.fact_id == relation.fact_id)
    payload = {"version": EXPLANATION_VERSION, "request_id": request.request_id,
               "instrument_id": request.instrument_id, "trade_date": result.latest_trade_date.isoformat(),
               "claims": [{"evidence_id": alias, "relation": relation.field, "value": relation.value}]}
    checked = validate_indicator_explanation(json.dumps(payload), execution=execution, expected_request=request)
    assert checked.status == "accepted", checked.code
    assert checked.claims[0].value == "decreasing"


def test_all_available_extended_relations_accept_checked_claims_and_reject_reversed_claims():
    from trading_companion.relation_explanation import EXPLANATION_VERSION, validate_indicator_explanation
    import json

    execution = execute_research_tool(bars_for([10, 11, 10, 11]),
                                      (IndicatorSpec("adx", window=2), IndicatorSpec("obv"),
                                       IndicatorSpec("vwma", window=2)))
    result, request = execution.full_result, execution.full_result.request
    aliases = {fact.fact_id: alias for alias, fact in execution.fact_aliases.items()}
    claims = [{"evidence_id": aliases[relation.fact_id], "relation": relation.field, "value": relation.value}
              for relation in result.relations if relation.status == "available"]
    assert len(claims) == 6
    payload = {"version": EXPLANATION_VERSION, "request_id": request.request_id,
               "instrument_id": request.instrument_id, "trade_date": result.latest_trade_date.isoformat(),
               "claims": claims}
    checked = validate_indicator_explanation(json.dumps(payload), execution=execution, expected_request=request)
    assert checked.status == "accepted", checked.code
    assert len(checked.claims) == 6
    payload["claims"][0]["value"] = "below_25"
    rejected = validate_indicator_explanation(json.dumps(payload), execution=execution, expected_request=request)
    assert rejected.status == "rejected"


def test_workflow_reserves_the_final_reminder_before_observations_fill_context():
    from trading_companion.research_workflow import _FINAL_BUDGET, _OPEN_BUDGET

    assert len(_OPEN_BUDGET) >= len(_FINAL_BUDGET)


@pytest.mark.parametrize("outlook", [False, True])
def test_default_workflow_can_add_all_three_new_indicators_without_losing_evidence(outlook):
    from test_research_workflow import ResearchWorkflowIntegrationTests, WorkflowScriptModel
    from trading_companion.tool_model import ToolCall, ToolModelResponse
    from trading_companion.workflow_tools import ADDITIONAL_TOOL_NAME

    class AddedIndicatorsModel(WorkflowScriptModel):
        def complete(self, messages, *, tools):
            response = super().complete(messages, tools=tools)
            if response.status == "tool_calls" and response.tool_calls[0].name == ADDITIONAL_TOOL_NAME:
                original = response.tool_calls[0]
                return ToolModelResponse("tool_calls", tool_calls=(ToolCall(
                    original.call_id, original.name,
                    {"indicators": [{"kind": "adx"}, {"kind": "obv"}, {"kind": "vwma"}]}),))
            return response

    case = ResearchWorkflowIntegrationTests()
    case.setUp()
    try:
        case.market.read_daily_bars.return_value = case.artificial_hundred_bars()
        model = AddedIndicatorsModel(("primary", "knowledge", "availability", "default_supplement", "final"),
            date_expression="2026-05-01 至 2026-09-25", indicator_mentions=())
        question = "分析平安银行，2026-05-01 至 2026-09-25" + ("，预测未来十二个月的走势" if outlook else "")
        result = case.turn(model, question=question)
        assert result.status == "completed", (result.code, result.failure_code, model.call_sizes)
        assert (result.model_calls_attempted, result.analyses_used) == (6, 2)
        assert len(result.workflow_observations) == 2
        supplement = result.workflow_observations[1].execution.full_result
        assert {spec.kind for spec in supplement.request.indicators} == {"adx", "obv", "vwma"}
        assert {claim.evidence_id.split(":")[0] for claim in result.explanation_validation.claims} == {"primary", "supplement"}
        assert all(size <= 20000 for size in model.call_sizes)
        case.source_mock.assert_called_once()
        case.market.read_daily_bars.assert_called_once()
    finally:
        case.doCleanups()
