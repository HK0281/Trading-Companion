"""Deterministic user-facing facts; never promote model prose to a verified answer."""

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_contracts import (
    ALGORITHM_VERSIONS, FieldResult, MultiIndicatorRequest, MultiIndicatorResult,
)


FIELD_LABELS = {
    "value": "数值", "dif": "DIF", "dea": "DEA", "histogram": "柱值",
    "middle": "中轨", "upper": "上轨", "lower": "下轨",
    "volume_shares": "当日成交量", "volume_ma": "含当日均量",
    "previous_volume_ma": "此前窗口均量", "relative_volume": "相对此前均量倍率",
}
UNITS = {"CNY/share": "元/股", "shares": "股", "multiple": "倍", "index_points": "点"}
UNAVAILABLE = {"insufficient_history": "历史记录不足", "undefined": "未定义（此前均量为零）",
               "calculation_error": "数值计算失败"}
SOURCE_LABELS = {"csv": "开发CSV", "tencent": "腾讯", "akshare_eastmoney": "AKShare/东方财富"}


@dataclass(frozen=True)
class ReportField:
    indicator_id: str
    field: str
    label: str
    trade_date: str
    status: str
    value: int | float | None
    unit: str
    required_bars: int
    actual_bars: int
    missing_bars: int
    fact_id: str | None


@dataclass(frozen=True)
class ResearchReport:
    status: str
    code: str
    text: str
    request_id: str | None
    fields: tuple[ReportField, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "research-report-v1", "origin": "application",
            "validation_scope": "deterministic_report_only_not_model_prose",
            "status": self.status, "code": self.code, "text": self.text,
            "request_id": self.request_id,
            "fields": [asdict(item) for item in self.fields],
            "evidence_ids": list(dict.fromkeys(item.fact_id for item in self.fields if item.fact_id)),
            "limitations": list(self.limitations),
        }


class _ReportIntegrityError(ValueError):
    pass


def _require(condition: bool) -> None:
    if not condition:
        raise _ReportIntegrityError("Report evidence is inconsistent")


def _number(value: int | float) -> str:
    # Display rounding never changes the structured value or derives a relation.
    return str(value) if type(value) is int else format(value, ".8g")


def _finite(value: object) -> bool:
    return type(value) is int or type(value) is float and isfinite(value)


def _same_request(left: MultiIndicatorRequest, right: MultiIndicatorRequest) -> bool:
    # Proposals may reorder the same canonical indicator instances, but no other scope may differ.
    return (left.request_id == right.request_id and left.instrument_id == right.instrument_id
            and left.start_date == right.start_date and left.as_of_date == right.as_of_date
            and left.source == right.source and set(left.indicators) == set(right.indicators))


def indicator_label(spec) -> str:
    """Use the same application-owned labels in fact reports and checked relations."""
    parameters = spec.parameters
    if spec.kind == "macd":
        return "MACD({fast},{slow},{signal})".format(**parameters)
    if spec.kind == "boll":
        return f"BOLL({parameters['window']},{_number(parameters['stddev_multiplier'])})"
    name = "量能" if spec.kind == "volume" else spec.kind.upper()
    return f"{name}({parameters['window']})"


def render_report_fields(fields: tuple[ReportField, ...]) -> tuple[str, ...]:
    """Render fields already checked by build_research_report, without deriving new claims."""
    lines = []
    for item in fields:
        if item.status == "available":
            lines.append(f"{item.label}：{_number(item.value)} {UNITS[item.unit]}。")
        else:
            shortage = f"，还缺{item.missing_bars}条记录" if item.status == "insufficient_history" else ""
            lines.append(f"{item.label}：{UNAVAILABLE[item.status]}{shortage}。")
    return tuple(lines)


def indicator_field_units(kind: str) -> dict[str, str]:
    """Describe the numerical fields emitted by each supported indicator family."""
    if kind == "macd":
        return dict.fromkeys(("dif", "dea", "histogram"), "CNY/share")
    if kind == "boll":
        return dict.fromkeys(("middle", "upper", "lower"), "CNY/share")
    if kind == "volume":
        return {"volume_shares": "shares", "volume_ma": "shares",
                "previous_volume_ma": "shares", "relative_volume": "multiple"}
    return {"value": "index_points" if kind == "rsi" else "CNY/share"}


def _checked_fields(result: MultiIndicatorResult, request: MultiIndicatorRequest) -> tuple[ReportField, ...]:
    _require(bool(result.bars))
    _require(all(type(bar) is DailyBar and bar.instrument_id == request.instrument_id
                 and request.start_date <= bar.trade_date <= request.as_of_date for bar in result.bars))
    _require(all(left.trade_date < right.trade_date for left, right in zip(result.bars, result.bars[1:])))
    latest = result.bars[-1]
    _require(all(type(fact.fact_id) is str and bool(fact.fact_id) for fact in result.facts))
    index = {fact.fact_id: fact for fact in result.facts}
    _require(len(index) == len(result.facts))
    _require(all(fact.request_id == request.request_id and fact.instrument_id == request.instrument_id
                 for fact in result.facts))

    def check_fact(identifier, indicator_id, parameters, name, value, unit):
        fact = index.get(identifier)
        _require(fact is not None and fact.trade_date == latest.trade_date
                 and fact.indicator_id == indicator_id and fact.parameters == parameters
                 and fact.field == name and _finite(fact.value) and fact.value == value
                 and fact.unit == unit and not fact.dependencies)

    output = []
    for name, value, unit, label in (
        ("close_price", latest.close_price, "CNY/share", "收盘价"),
        ("volume_shares", latest.volume_shares, "shares", "当日成交量"),
    ):
        matches = [fact for fact in result.facts if fact.indicator_id == "daily_bar"
                   and fact.field == name and fact.trade_date == latest.trade_date]
        _require(len(matches) == 1 and _finite(value))
        check_fact(matches[0].fact_id, "daily_bar", {}, name, value, unit)
        output.append(ReportField("daily_bar", name, label, latest.trade_date.isoformat(),
                                  "available", value, unit, 1, len(result.bars), 0, matches[0].fact_id))
    _require(len(result.indicators) == len(request.indicators))
    by_indicator = {indicator.indicator_id: indicator for indicator in result.indicators}
    _require(len(by_indicator) == len(result.indicators)
             and by_indicator.keys() == {spec.indicator_id for spec in request.indicators})
    for spec in request.indicators:
        indicator = by_indicator[spec.indicator_id]
        _require(indicator.indicator_id == spec.indicator_id and indicator.kind == spec.kind
                 and indicator.parameters == spec.parameters
                 and indicator.algorithm_version == ALGORITHM_VERSIONS[spec.kind]
                 and bool(indicator.series) and indicator.series[-1].trade_date == latest.trade_date)
        definitions = indicator_field_units(spec.kind)
        _require(indicator.latest.keys() == definitions.keys())
        for name, unit in definitions.items():
            item = indicator.latest[name]
            _require(type(item) is FieldResult and item.trade_date == latest.trade_date and item.unit == unit
                     and type(item.required_bars) is int and item.required_bars > 0
                     and type(item.actual_bars) is int and item.actual_bars == len(result.bars)
                     and type(item.missing_bars) is int
                     and item.missing_bars == max(0, item.required_bars - item.actual_bars))
            if item.status == "available":
                _require(_finite(item.value) and item.missing_bars == 0)
                check_fact(item.fact_id, spec.indicator_id, spec.parameters, name, item.value, unit)
            else:
                _require(item.status in UNAVAILABLE and item.value is None and item.fact_id is None)
                _require(item.status != "undefined" or spec.kind == "volume" and name == "relative_volume")
                _require(item.status != "undefined" or item.missing_bars == 0)
                _require(item.status != "insufficient_history" or item.missing_bars > 0)
            label = indicator_label(spec) + ("" if name == "value" else " " + FIELD_LABELS[name])
            output.append(ReportField(spec.indicator_id, name, label, item.trade_date.isoformat(),
                                      item.status, item.value, item.unit, item.required_bars,
                                      item.actual_bars, item.missing_bars, item.fact_id))
    return tuple(output)


def _limitations(result: MultiIndicatorResult, fields: tuple[ReportField, ...]) -> tuple[str, ...]:
    notes = [
        "仅按交易日过滤，未验证历史发布时间及修订版本，不等于严格point-in-time数据。",
        "未使用新闻或基本面，未验证历史ST、上市时长、停复牌或可交易性。",
        "指标可能相关，不能视为独立投票或胜率；本摘要不预测收益或提供买卖目标。",
    ]
    if result.price_adjustment == "unknown":
        notes.append("复权口径未知；CSV真实性未经独立验证。")
    else:
        notes.append("使用不复权价格，公司行动可能影响价格和指标；未核查具体公司行动。")
    if result.source != "csv":
        notes.append("量额单位按适配器转换，覆盖与精度未经全面验证。")
        if result.source == "tencent":
            notes.append("腾讯源端量额可能已舍入，单位转换不能恢复丢失的精度。")
    if result.latest_trade_date and result.latest_trade_date < result.request.as_of_date:
        notes.append("实际最新交易日早于请求截止日，不能据此认定休市或停牌。")
    if any(component.start_effect_warning for indicator in result.indicators for component in indicator.components):
        notes.append("部分递推指标可能受计算起点影响；初始化提示不代表已收敛或预测可靠。")
    if any(item.status == "calculation_error" for item in fields):
        notes.append("部分指标数值计算失败，仅展示其余有效事实。")
    if fields and not result.synthesis_allowed:
        notes.append("请求的计算字段全部不可用，不生成综合走势判断。")
    notes.append("文本数字仅作显示舍入，原始精度及证据标识见结构化字段；未加入模型草稿。")
    return tuple(notes)


def build_research_report(result: MultiIndicatorResult, *,
                          expected_request: MultiIndicatorRequest | None) -> ResearchReport:
    """Project this request's facts only; a report is not a validation of arbitrary prose."""
    request_id = expected_request.request_id if type(expected_request) is MultiIndicatorRequest else None
    blocked = ResearchReport("blocked", "report_evidence_mismatch",
                             "研究结果与本轮请求或证据不一致，未展示数值；请重新发起明确研究。", request_id)
    if (type(result) is not MultiIndicatorResult or type(expected_request) is not MultiIndicatorRequest
            or type(result.request) is not MultiIndicatorRequest
            or not _same_request(result.request, expected_request) or result.source != expected_request.source):
        return blocked
    expected_authenticity = (("synthetic", "unverified_csv") if result.source == "csv"
                             else ("provider_reported",))
    if (result.data_authenticity not in expected_authenticity
            or result.price_adjustment != ("unknown" if result.source == "csv" else "unadjusted")
            or result.retrieved_at is not None and (type(result.retrieved_at) is not datetime
                or result.retrieved_at.tzinfo is None or result.retrieved_at.utcoffset() is None)):
        return blocked
    banner = ("人工样例 / 非真实行情，仅供开发验证。" if result.data_authenticity == "synthetic" else
              "开发CSV / 真实性未经验证，不作为真实市场研究。" if result.source == "csv" else
              "供应商提供的历史日线，不是实时行情或经独立认证的数据。")
    request = expected_request
    lines = [banner, f"研究对象：{request.instrument_id}；来源：{SOURCE_LABELS[request.source]}。",
             f"请求区间：{request.start_date.isoformat()} 至 {request.as_of_date.isoformat()}。",
             "指标：" + "、".join(indicator_label(spec) for spec in request.indicators) + "。"]
    if result.data_status != "ok":
        messages = {"source_error": "行情源读取失败，没有可展示的有效行情；未自动换源或重试。",
                    "invalid_data": "行情数据未通过校验，没有展示行情数值。",
                    "no_data": "本次没有匹配日线，不能据此判断停牌、退市或股票不存在。"}
        if result.data_status not in messages:
            return blocked
        lines.append(messages[result.data_status])
        notes = _limitations(result, ())
        return ResearchReport("unavailable", result.data_status,
                              "\n".join([*lines, "限制：", *notes]), request_id, limitations=notes)
    try:
        fields = _checked_fields(result, expected_request)
    except _ReportIntegrityError:
        return blocked
    notes = _limitations(result, fields)
    lines.append(f"实际最新交易日：{result.latest_trade_date.isoformat()}；区间内共{len(result.bars)}条日线。")
    lines.append("采集时间：" + (result.retrieved_at.isoformat() if result.retrieved_at else "未知") + "。")
    lines.extend(render_report_fields(fields))
    status = "complete" if all(item.status == "available" for item in fields) else "limited"
    return ResearchReport(status, "deterministic_facts", "\n".join([*lines, "限制：", *notes]),
                          request_id, fields, notes)
