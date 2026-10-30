"""Exact, snapshot-bound ATR reference plans. No order or predicted future price."""

from dataclasses import dataclass, asdict
from decimal import Decimal, DecimalException, ROUND_FLOOR, ROUND_HALF_UP, localcontext

from trading_companion.risk_research import build_risk_scenario, PRICE_GRID

METHOD = "atr_reference_plan_v1"
HORIZON_METHOD = "atr_reference_plan_v2"
PURPOSES = ("conditional_entry", "holding_exit", "wait")
ATR_DISPLAY_GRID = Decimal("0.0001")


def method_for_plan(plan):
    """Old trusted snapshots retain v1; newly interpreted plans explicitly declare v2."""
    return HORIZON_METHOD if 'plan_method:' + HORIZON_METHOD in plan.applied_defaults else METHOD


def unavailable_code_for_plan(plan):
    from trading_companion.decision_horizon import requires_prices
    from trading_companion.research_decision import excludes_plan
    if excludes_plan(plan.user_question) or 'technical' not in plan.kinds or 'risk' in plan.scope.excluded:
        return 'plan_excluded'
    if method_for_plan(plan) == HORIZON_METHOD:
        if not requires_prices(plan):
            return 'plan_not_requested'
        if not any(s.kind == 'atr' and s.parameters['window'] == 14 for s in plan.indicators):
            return 'risk_atr_not_requested'
    return None


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    instrument_id: str
    purpose: str
    status: str
    code: str
    request_id: str | None = None
    source_date: str | None = None
    source: str | None = None
    price_adjustment: str | None = None
    data_authenticity: str | None = None
    atr_indicator_id: str | None = None
    values: tuple[tuple[str, str], ...] = ()
    evidence_ids: tuple[str, ...] = ()
    method: str = METHOD
    horizon: str | None = None
    horizon_profile: str | None = None
    atr_timeframe: str | None = None
    atr_source_date: str | None = None
    atr_multiple: str | None = None
    target_multiples: tuple[str, ...] = ()

    def to_dict(self):
        result = asdict(self)
        if self.method == METHOD:
            for key in ('horizon', 'horizon_profile', 'atr_timeframe', 'atr_source_date', 'atr_multiple', 'target_multiples'):
                result.pop(key)
        else:
            result['target_multiples'] = list(self.target_multiples)
        result.update(version="trade-plan-v1" if self.method == METHOD else "trade-plan-v2", method=self.method, origin="application",
                      execution_allowed=False, values=dict(self.values) if self.values else None,
                      evidence_ids=list(self.evidence_ids), condition=self.condition,
                      assumptions=["历史收盘参考；条件价位由规则计算，不是预测目标。",
                                   "未计费用、滑点、跳空及交易限制，不保证按参考价成交。"])
        return result

    @property
    def condition(self):
        value = dict(self.values)
        if not value:
            return None
        if self.purpose == "conditional_entry":
            return {"field": "daily_close", "operator": "<=", "price": value["reference_stop"],
                    "effect": "invalidate_and_review"}
        if self.purpose == "holding_exit":
            return {"field": "daily_close", "operator": "<", "price": value["exit_below"], "effect": "review"}
        return {"any": [{"field": "daily_close", "operator": "<=", "price": value["observe_below"]},
                        {"field": "daily_close", "operator": ">=", "price": value["observe_above"]}], "effect": "review"}


def build_trade_plans(result, *, expected_request, unavailable_code=None, horizon=None, method=METHOD):
    if method == HORIZON_METHOD:
        from trading_companion.horizon_trade_plan import build_horizon_plans
        return build_horizon_plans(result, expected_request=expected_request, horizon=horizon, unavailable_code=unavailable_code)
    if method != METHOD:
        raise ValueError('Unknown reference plan method')
    instrument = expected_request.instrument_id if expected_request is not None else result
    request_id = expected_request.request_id if expected_request is not None else None
    common = dict(instrument_id=instrument, request_id=request_id)
    code = unavailable_code or "risk_market_data_unavailable"
    scenario = None
    if expected_request is not None and unavailable_code is None:
        scenario = build_risk_scenario(result, expected_request=expected_request)
        code = scenario.code
        common.update(source_date=scenario.source_date, source=scenario.source,
                      price_adjustment=scenario.price_adjustment, data_authenticity=scenario.data_authenticity,
                      atr_indicator_id=scenario.atr_indicator_id, evidence_ids=scenario.evidence_ids)
    core = None
    if scenario is not None and scenario.status == "available":
        try:
            with localcontext() as context:
                context.prec = 80
                entry, stop = Decimal(scenario.values.reference_entry), Decimal(scenario.values.reference_stop)
                risk = entry - stop
                first = (entry + Decimal("1.5") * risk).quantize(PRICE_GRID, rounding=ROUND_FLOOR)
                second = (entry + Decimal("2.5") * risk).quantize(PRICE_GRID, rounding=ROUND_FLOOR)
                if not 0 < stop < entry < first < second:
                    raise ValueError("price_grid")
                ratio = lambda target: format(((target-entry)/risk).quantize(Decimal(".000001"), rounding=ROUND_FLOOR), "f")
                # Format only after all calculations. Preserve a sub-cent source
                # close rather than changing E and its actual reward/risk ratios.
                entry_text = format(entry, ".2f") if entry == entry.quantize(PRICE_GRID) else format(entry, "f")
                atr_text = format(Decimal(scenario.values.atr).quantize(ATR_DISPLAY_GRID, rounding=ROUND_HALF_UP), ".4f")
                core = dict(reference_entry=entry_text, reference_stop=format(stop, ".2f"),
                    target_1=format(first, ".2f"), target_2=format(second, ".2f"),
                    reward_risk_1=ratio(first), reward_risk_2=ratio(second),
                    atr=atr_text, atr_multiple="1.5")
        except (DecimalException, ValueError, OverflowError):
            code = "plan_price_grid_unavailable"
    plans = []
    for purpose in PURPOSES:
        values = None
        if core is not None:
            values = (core if purpose == "conditional_entry" else
                      {"reference_close": core["reference_entry"], "exit_below": core["reference_stop"]}
                      if purpose == "holding_exit" else
                      {"observe_below": core["reference_stop"], "observe_above": core["target_1"]})
        plans.append(TradePlan(plan_id=f"{request_id or instrument}|{METHOD}:{purpose}", purpose=purpose,
            status="available" if values else "unavailable", code="plan_ready" if values else code,
            values=tuple(values.items()) if values else (), **common))
    return tuple(plans)


def plan_display(plan):
    labels = {"reference_entry": "入场参考", "reference_stop": "止损", "target_1": "止盈1",
              "target_2": "止盈2", "reward_risk_1": "盈亏比1", "reward_risk_2": "盈亏比2",
              "atr": "ATR", "atr_multiple": "ATR倍数", "reference_close": "收盘参考",
              "exit_below": "退出观察位", "observe_below": "下方观察位", "observe_above": "上方观察位"}
    return "；".join(labels[key] + value + ("元" if key not in (
        "reward_risk_1", "reward_risk_2", "atr_multiple") else "倍") for key, value in plan.values)
