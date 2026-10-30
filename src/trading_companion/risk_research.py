"""Deterministic historical long scenarios; this module cannot execute trades."""

from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Literal

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report


METHOD = "atr_multiple_historical_long_v1"
PRICE_GRID = Decimal("0.01")
MAX_ATR_MULTIPLE = Decimal("10")
MAX_REWARD_RISK = Decimal("10")


@dataclass(frozen=True)
class RiskValues:
    """Exact decimal strings keep price rounding explicit at the JSON boundary."""

    reference_entry: str
    atr: str
    atr_multiple: str
    reward_risk: str
    reference_stop: str
    reference_target: str
    risk_per_share: str
    reward_per_share: str
    risk_pct: str
    effective_reward_risk: str
    price_unit: str = "CNY/share"


@dataclass(frozen=True)
class RiskScenario:
    status: Literal["available", "unavailable", "invalid_request"]
    code: str
    request_id: str | None = None
    instrument_id: str | None = None
    source_date: str | None = None
    source: str | None = None
    data_authenticity: str | None = None
    price_adjustment: str | None = None
    atr_indicator_id: str | None = None
    values: RiskValues | None = None
    evidence_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    method: str = METHOD
    horizon: str | None = None
    atr_source_date: str | None = None
    atr_timeframe: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = {
            "version": "risk-scenario-v1" if self.method == METHOD else "risk-scenario-v2", "origin": "application",
            "validation_scope": "historical_long_scenario_not_execution_or_forecast",
            "execution_allowed": False,
            "status": self.status, "code": self.code, "method": self.method,
            "request_id": self.request_id, "instrument_id": self.instrument_id,
            "source_date": self.source_date, "source": self.source,
            "data_authenticity": self.data_authenticity,
            "price_adjustment": self.price_adjustment,
            "atr_indicator_id": self.atr_indicator_id,
            "values": asdict(self.values) if self.values is not None else None,
            "evidence_ids": list(self.evidence_ids), "assumptions": list(self.assumptions),
        }
        if self.method != METHOD:
            result.update(horizon=self.horizon, atr_source_date=self.atr_source_date, atr_timeframe=self.atr_timeframe)
        return result


def _parameter(value: object, maximum: Decimal) -> Decimal:
    if type(value) not in (int, float, Decimal):
        raise ValueError("Risk parameters must be finite positive numbers")
    converted = Decimal(str(value))
    if not converted.is_finite() or not 0 < converted <= maximum:
        raise ValueError("Risk parameters are outside the research range")
    return converted


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def build_risk_scenario(result: MultiIndicatorResult, *,
                        expected_request: MultiIndicatorRequest,
                        atr_multiple: int | float | Decimal = 1.5,
                        reward_risk: int | float | Decimal = 2.0) -> RiskScenario:
    """Derive an observation scenario only from this request's latest close and ATR.

    ATR(14) is preferred when requested. Otherwise one explicitly requested ATR
    period is required. An unavailable preferred field never falls back to an
    older point, shorter period, fixed percentage, or another market request.
    """
    try:
        multiple = _parameter(atr_multiple, MAX_ATR_MULTIPLE)
        ratio = _parameter(reward_risk, MAX_REWARD_RISK)
    except (ValueError, DecimalException):
        return RiskScenario("invalid_request", "invalid_risk_parameters")

    if type(expected_request) is not MultiIndicatorRequest or type(result) is not MultiIndicatorResult:
        return RiskScenario("unavailable", "risk_evidence_mismatch")
    try:
        # Reconstruct trusted request objects before indexing externally supplied evidence.
        MultiIndicatorRequest(expected_request.instrument_id, expected_request.start_date,
                              expected_request.as_of_date, expected_request.indicators,
                              expected_request.source, expected_request.request_id)
        report = build_research_report(result, expected_request=expected_request)
        if report.status not in ("complete", "limited"):
            code = "risk_evidence_mismatch" if report.status == "blocked" else "risk_market_data_unavailable"
            return RiskScenario("unavailable", code, request_id=expected_request.request_id,
                                instrument_id=expected_request.instrument_id)
        # Report checks bind facts to request/date/unit; bar validation also rejects invalid OHLCV.
        for bar in result.bars:
            DailyBar.__post_init__(bar)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
        return RiskScenario("unavailable", "risk_evidence_mismatch")

    common = dict(request_id=expected_request.request_id, instrument_id=expected_request.instrument_id,
                  source_date=result.latest_trade_date.isoformat(), source=result.source,
                  data_authenticity=result.data_authenticity, price_adjustment=result.price_adjustment)

    def unavailable(code: str, *, indicator_id: str | None = None) -> RiskScenario:
        return RiskScenario("unavailable", code, **common, atr_indicator_id=indicator_id)

    atr_specs = [spec for spec in expected_request.indicators if spec.kind == "atr"]
    preferred = next((spec for spec in atr_specs if spec.parameters["window"] == 14), None)
    if preferred is None:
        if not atr_specs:
            return unavailable("risk_atr_not_requested")
        if len(atr_specs) != 1:
            return unavailable("risk_atr_selection_ambiguous")
        preferred = atr_specs[0]

    fields = {(item.indicator_id, item.field): item for item in report.fields}
    close_field = fields.get(("daily_bar", "close_price"))
    atr_field = fields.get((preferred.indicator_id, "value"))
    latest = common["source_date"]
    if (close_field is None or close_field.status != "available" or close_field.trade_date != latest
            or close_field.unit != "CNY/share" or not close_field.fact_id):
        return unavailable("risk_close_unavailable", indicator_id=preferred.indicator_id)
    if (atr_field is None or atr_field.status != "available" or atr_field.trade_date != latest
            or atr_field.unit != "CNY/share" or not atr_field.fact_id):
        return unavailable("risk_atr_unavailable", indicator_id=preferred.indicator_id)
    if atr_field.required_bars != preferred.parameters["window"] + 1:
        return unavailable("risk_evidence_mismatch", indicator_id=preferred.indicator_id)

    # These inputs are numerical leaf facts, not relations or model-generated dependencies.
    facts = {fact.fact_id: fact for fact in result.facts}
    evidence = (close_field.fact_id, atr_field.fact_id)
    if any(identifier not in facts or facts[identifier].dependencies for identifier in evidence):
        return unavailable("risk_evidence_mismatch", indicator_id=preferred.indicator_id)

    try:
        with localcontext() as context:
            context.prec = 80
            entry = Decimal(str(close_field.value))
            atr = Decimal(str(atr_field.value))
            if not entry.is_finite() or entry <= 0 or not atr.is_finite() or atr <= 0:
                return unavailable("risk_nonpositive_reference", indicator_id=preferred.indicator_id)
            distance = multiple * atr
            # For a long scenario, a closer stop limits modelled loss; a lower target avoids exaggeration.
            stop = (entry - distance).quantize(PRICE_GRID, rounding=ROUND_CEILING)
            target = (entry + ratio * distance).quantize(PRICE_GRID, rounding=ROUND_FLOOR)
            if not 0 < stop < entry < target:
                return unavailable("risk_price_grid_unavailable", indicator_id=preferred.indicator_id)
            risk = entry - stop
            reward = target - entry
            risk_pct = (risk / entry * 100).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
            effective_ratio = (reward / risk).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
            values = RiskValues(_text(entry), _text(atr), _text(multiple), _text(ratio),
                                format(stop, ".2f"), format(target, ".2f"), _text(risk),
                                _text(reward), format(risk_pct, ".4f"), format(effective_ratio, ".6f"))
    except (DecimalException, ValueError, OverflowError):
        return unavailable("risk_calculation_unavailable", indicator_id=preferred.indicator_id)

    assumptions = (
        f"以{latest}历史收盘价作为假设入场参考，仅构建做多观察情景，不代表真实持仓或交易指令。",
        f"初始风险距离为{values.atr_multiple}倍ATR({preferred.parameters['window']})，目标距离为初始风险距离的{values.reward_risk}倍。",
        "按0.01元研究网格将止损向上、目标向下取整；风险比例和实际收益风险比按舍入后的距离计算。",
        "参考价不代表实时可成交价；未计手续费、滑点、跳空、涨跌停、T+1或停牌影响，不保证止损成交或目标收益。",
        "未核验该证券历史交易规则、公司行动和价格修订；仅按日线日期过滤，不等于严格历史时点可得。",
        "ATR衡量历史波动，不能说明未来涨跌方向、目标触达概率或最大实际亏损。",
    )
    if result.data_authenticity == "synthetic":
        assumptions += ("输入为人工样例，所列价格仅用于开发验证。",)
    elif result.source == "csv":
        assumptions += ("输入CSV真实性与复权口径未经验证。",)
    if result.latest_trade_date < expected_request.as_of_date:
        assumptions += ("实际最新交易日早于请求截止日，未将旧日参考价视为截止日行情。",)
    return RiskScenario("available", "historical_long_scenario_ready", **common,
                        atr_indicator_id=preferred.indicator_id, values=values,
                        evidence_ids=evidence, assumptions=assumptions)
