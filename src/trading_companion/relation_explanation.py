"""Check bounded, request-local relation claims, then render application-owned text."""

from dataclasses import asdict, dataclass
from itertools import combinations
import json
from math import isfinite
import re

from trading_companion.multi_contracts import Fact, MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import (
    build_research_report, indicator_field_units, indicator_label,
)
from trading_companion.research_tool import ToolExecution


EXPLANATION_VERSION = "indicator-explanation-v1"
MAX_EXPLANATION_CHARACTERS = 4000
MAX_CLAIMS = 8
EXPLANATION_INSTRUCTION = (
    'After the tool result, return ONLY a JSON object, without markdown or extra prose: '
    '{"version":"indicator-explanation-v1","request_id":"<tool request_id>",'
    '"instrument_id":"<tool instrument_id>","trade_date":"<tool dates[0]>",'
    '"claims":[{"evidence_id":"<relation fact alias>","relation":"<relation field>",'
    '"value":"<relation value>"}]}. Select 1 to 8 distinct available rows from '
    'tables.relations; copy their fact, field and value exactly. Prefer coverage of '
    'different requested indicator families over redundant claims. Use this result only; '
    'do not cite numeric_facts as relations. Never invent unavailable relations, add '
    'free text, targets, forecasts or trading instructions. Do not make another tool call.'
)


@dataclass(frozen=True)
class CheckedClaim:
    evidence_id: str
    fact_id: str
    indicator_id: str
    relation: str
    value: str
    dependencies: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ExplanationValidation:
    status: str = "not_performed"
    code: str = "no_structured_explanation"
    request_id: str | None = None
    claims: tuple[CheckedClaim, ...] = ()

    @property
    def text(self) -> str | None:
        if self.status != "accepted":
            return None
        return "\n".join([
            "已核对的指标关系（实际最新交易日，按原始精度比较）：",
            *(item.text for item in self.claims),
            "这些关系不构成独立投票、买卖信号或未来收益预测。",
        ])

    def to_dict(self) -> dict[str, object]:
        return {
            "version": EXPLANATION_VERSION, "origin": "application",
            "validation_scope": "request_local_indicator_relations_only_not_predictions",
            "status": self.status, "code": self.code, "request_id": self.request_id,
            "claims": [asdict(item) for item in self.claims], "text": self.text,
        }


class _Rejected(ValueError):
    pass


def _require(condition: bool, code: str = "explanation_evidence_mismatch") -> None:
    if not condition:
        raise _Rejected(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "invalid_explanation_json")
        result[key] = value
    return result


def _reject_number(value):
    # This version has no numeric JSON fields, including NaN/Infinity tokens.
    raise _Rejected("invalid_explanation_json")


def _parse(text: str) -> dict:
    _require(type(text) is str and 0 < len(text) <= MAX_EXPLANATION_CHARACTERS,
             "invalid_explanation_json")
    try:
        payload = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_number,
                             parse_float=_reject_number, parse_int=_reject_number)
    except (ValueError, RecursionError) as error:
        raise _Rejected("invalid_explanation_json") from error
    _require(type(payload) is dict and payload.keys() == {
        "version", "request_id", "instrument_id", "trade_date", "claims",
    }, "invalid_explanation_shape")
    _require(payload["version"] == EXPLANATION_VERSION
             and all(type(payload[key]) is str for key in (
                 "version", "request_id", "instrument_id", "trade_date"))
             and type(payload["claims"]) is list and 1 <= len(payload["claims"]) <= MAX_CLAIMS,
             "invalid_explanation_shape")
    seen = set()
    for claim in payload["claims"]:
        _require(type(claim) is dict and claim.keys() == {"evidence_id", "relation", "value"}
                 and all(type(value) is str and 0 < len(value) <= 100 for value in claim.values()),
                 "invalid_explanation_shape")
        alias = claim["evidence_id"]
        _require(re.fullmatch(r"f[0-9]+", alias) is not None and alias not in seen,
                 "invalid_explanation_shape")
        seen.add(alias)
    return payload


def _compare(left, right):
    return "above" if left > right else "below" if left < right else "equal"


def _change(current, previous):
    return {"above": "increasing", "below": "decreasing", "equal": "unchanged"}[_compare(current, previous)]


def _cross(pl, pr, cl, cr):
    if pl <= pr and cl > cr:
        return "cross_up"
    if pl >= pr and cl < cr:
        return "cross_down"
    return "no_cross"


def _boll_position(close, middle, upper, lower):
    _require(lower <= middle <= upper)
    if upper == lower:
        return {"above": "above_coincident_bands", "below": "below_coincident_bands",
                "equal": "at_coincident_bands"}[_compare(close, middle)]
    for boundary, above, equal in (
        (upper, "above_upper", "at_upper"),
        (middle, "between_middle_and_upper", "at_middle"),
        (lower, "between_lower_and_middle", "at_lower"),
    ):
        if close > boundary:
            return above
        if close == boundary:
            return equal
    return "below_lower"


def _definitions(result):
    """Bind exact fields, dates and operand order; never infer a pair by substrings."""
    definitions = {}
    latest = result.latest_trade_date
    previous = result.bars[-2].trade_date if len(result.bars) >= 2 else None
    specs = {spec.indicator_id: spec for spec in result.request.indicators}
    indicators = {item.indicator_id: item for item in result.indicators}

    def ref(identifier, name="value", *, prior=False):
        return identifier, name, previous if prior else latest

    def add(identifier, field, references, evaluate, label):
        definitions[(identifier, field)] = references, evaluate, label

    close = ref("daily_bar", "close_price")
    prior_close = ref("daily_bar", "close_price", prior=True)
    for identifier, spec in specs.items():
        label = indicator_label(spec)
        current, prior = ref(identifier), ref(identifier, prior=True)
        if spec.kind in ("sma", "ema"):
            add(identifier, "price_position", (close, current), _compare, label)
            add(identifier, "mean_change", (current, prior), _change, label)
            add(identifier, "price_cross", (prior_close, prior, close, current), _cross, label)
        elif spec.kind == "macd":
            dif, dea = ref(identifier, "dif"), ref(identifier, "dea")
            add(identifier, "dif_dea_position", (dif, dea), _compare, label)
            add(identifier, "dif_dea_cross", (ref(identifier, "dif", prior=True),
                ref(identifier, "dea", prior=True), dif, dea), _cross, label)
            add(identifier, "histogram_change", (ref(identifier, "histogram"),
                ref(identifier, "histogram", prior=True)), _change, label)
        elif spec.kind == "rsi":
            add(identifier, "reference_zone", (current,),
                lambda value: "below_30" if value < 30 else "above_70" if value > 70 else "neutral", label)
        elif spec.kind == "boll":
            add(identifier, "price_position", (close, ref(identifier, "middle"),
                ref(identifier, "upper"), ref(identifier, "lower")), _boll_position, label)
            add(identifier, "bands_state", (ref(identifier, "upper"), ref(identifier, "lower")),
                lambda upper, lower: "coincident" if upper == lower else "distinct", label)
        elif spec.kind == "atr":
            add(identifier, "atr_change", (current, prior), _change, label)
        elif spec.kind == "volume":
            add(identifier, "relative_volume_position", (ref(identifier, "relative_volume"),),
                lambda value: _compare(value, 1), label)
            add(identifier, "current_vs_prior_mean", (ref(identifier, "volume_shares"),
                ref(identifier, "previous_volume_ma")), _compare, label)
    means = [spec for spec in specs.values() if spec.kind in ("sma", "ema")]
    for left, right in combinations(means, 2):
        identifier = f"ma_pair[{left.indicator_id};{right.indicator_id}]"
        label = (indicator_label(left), indicator_label(right))
        add(identifier, "mean_position", (ref(left.indicator_id), ref(right.indicator_id)), _compare, label)
        add(identifier, "mean_cross", (ref(left.indicator_id, prior=True), ref(right.indicator_id, prior=True),
            ref(left.indicator_id), ref(right.indicator_id)), _cross, label)
    return definitions, specs, indicators


def _numeric_fact(result, reference, specs, indicators, index):
    identifier, name, trade_date = reference
    _require(trade_date is not None)
    if identifier == "daily_bar":
        bar = next((bar for bar in result.bars[-2:] if bar.trade_date == trade_date), None)
        _require(bar is not None and name in ("close_price", "volume_shares"))
        value, unit, parameters, required = getattr(bar, name), ("shares" if name == "volume_shares"
                                                               else "CNY/share"), {}, 1
        matching = [fact for fact in index.values() if (fact.indicator_id, fact.field, fact.trade_date) == reference]
        _require(len(matching) == 1)
        fact = matching[0]
    else:
        spec = specs[identifier]
        indicator = indicators[identifier]
        rows = [row for row in indicator.series[-2:] if row.trade_date == trade_date]
        _require(len(rows) == 1 and name in rows[0].fields)
        field = rows[0].fields[name]
        unit, parameters = indicator_field_units(spec.kind)[name], spec.parameters
        actual = len(result.bars) - (trade_date != result.latest_trade_date)
        _require(field.status == "available" and field.trade_date == trade_date
                 and field.unit == unit and type(field.actual_bars) is int and field.actual_bars == actual
                 and type(field.missing_bars) is int and field.missing_bars == 0
                 and type(field.required_bars) is int and 0 < field.required_bars <= actual)
        value, required = field.value, field.required_bars
        fact = index.get(field.fact_id)
        _require(fact is not None)
    _require((type(value) is int or type(value) is float and isfinite(value))
             and fact.indicator_id == identifier and fact.field == name and fact.trade_date == trade_date
             and fact.parameters == parameters and fact.unit == unit and type(fact.value) is type(value)
             and fact.value == value and not fact.dependencies)
    return fact, required + (trade_date != result.latest_trade_date)


def _render(label, kind, relation, value):
    position = {"above": "高于", "below": "低于", "equal": "等于"}
    change = {"increasing": "增加", "decreasing": "减少", "unchanged": "不变"}
    cross = {"cross_up": "向上穿越", "cross_down": "向下穿越", "no_cross": "未发生穿越"}
    if relation in ("mean_position", "mean_cross"):
        left, right = label
        return (f"{left}{position[value]}{right}。" if relation == "mean_position" else
                f"相较紧邻前一条记录，{left}相对{right}{cross[value]}。")
    if relation == "price_position" and kind in ("sma", "ema"):
        return f"收盘价{position[value]}{label}。"
    if relation == "price_cross":
        return f"相较紧邻前一条记录，收盘价相对{label}{cross[value]}。"
    if relation == "mean_change":
        return f"{label}数值相较紧邻前一条记录{change[value]}。"
    if relation == "dif_dea_position":
        return f"{label}中，DIF{position[value]}DEA。"
    if relation == "dif_dea_cross":
        return f"{label}中，相较紧邻前一条记录，DIF相对DEA{cross[value]}。"
    if relation == "histogram_change":
        return f"{label}柱值相较紧邻前一条记录{change[value]}；这只比较柱值，不推断上涨或下跌动量。"
    if relation == "reference_zone":
        zone = {"below_30": "低于30", "above_70": "高于70", "neutral": "位于30至70之间（含边界）"}[value]
        return f"{label}{zone}；阈值仅作参考，不是买卖信号。"
    if relation == "price_position" and kind == "boll":
        zone = {"above_upper": "高于上轨", "at_upper": "等于上轨",
                "between_middle_and_upper": "位于中轨与上轨之间", "at_middle": "等于中轨",
                "between_lower_and_middle": "位于下轨与中轨之间", "at_lower": "等于下轨",
                "below_lower": "低于下轨", "above_coincident_bands": "高于重合轨道",
                "below_coincident_bands": "低于重合轨道", "at_coincident_bands": "等于重合轨道"}[value]
        return f"{label}中，收盘价{zone}。"
    if relation == "bands_state":
        return f"{label}上下轨{'重合' if value == 'coincident' else '不重合'}。"
    if relation == "atr_change":
        return f"{label}相较紧邻前一条记录{change[value]}；ATR只反映波动尺度，不表示价格方向。"
    if relation == "relative_volume_position":
        return f"{label}中，相对此前窗口均量的倍率{position[value]}1。"
    if relation == "current_vs_prior_mean":
        return f"{label}中，当日成交量{position[value]}严格此前窗口均量。"
    raise _Rejected("explanation_relation_mismatch")


def validate_indicator_explanation(text: str, *, execution: ToolExecution,
                                   expected_request: MultiIndicatorRequest | None) -> ExplanationValidation:
    """Accept all selected claims or none, without model calls or data acquisition."""
    request_id = expected_request.request_id if type(expected_request) is MultiIndicatorRequest else None
    try:
        payload = _parse(text)
        _require(type(execution) is ToolExecution and execution.status == "executed"
                 and type(execution.full_result) is MultiIndicatorResult, "explanation_not_ready")
        result = execution.full_result
        report = build_research_report(result, expected_request=expected_request)
        _require(report.status in ("complete", "limited") and result.synthesis_allowed,
                 "explanation_not_ready")
        _require(payload["request_id"] == request_id
                 and payload["instrument_id"] == expected_request.instrument_id
                 and payload["trade_date"] == result.latest_trade_date.isoformat(),
                 "explanation_context_mismatch")
        index = {fact.fact_id: fact for fact in result.facts}
        aliases = execution.fact_aliases
        _require(aliases == {f"f{number}": fact for number, fact in enumerate(result.facts)})
        _require(all(type(fact) is Fact for fact in result.facts))
        definitions, specs, indicators = _definitions(result)
        relations = {(item.indicator_id, item.field): item for item in result.relations}
        _require(len(relations) == len(result.relations))
        checked = []
        for claim in payload["claims"]:
            fact = aliases.get(claim["evidence_id"])
            _require(fact is not None and fact.unit == "relation")
            key = fact.indicator_id, claim["relation"]
            relation, definition = relations.get(key), definitions.get(key)
            _require(relation is not None and definition is not None, "explanation_relation_mismatch")
            references, evaluate, label = definition
            operands = [_numeric_fact(result, ref, specs, indicators, index) for ref in references]
            dependencies = tuple(item.fact_id for item, _ in operands)
            required = max(count for _, count in operands)
            _require(relation.status == "available" and relation.reason == "ready"
                     and relation.relation_id == f"{fact.indicator_id}:{claim['relation']}"
                     and relation.fact_id == fact.fact_id and fact.field == claim["relation"]
                     and fact.parameters == (specs[fact.indicator_id].parameters if fact.indicator_id in specs else {})
                     and fact.trade_date == relation.trade_date == result.latest_trade_date
                     and type(relation.required_bars) is int and relation.required_bars == required
                     and type(relation.actual_bars) is int and relation.actual_bars == len(result.bars)
                     and type(relation.missing_bars) is int and relation.missing_bars == 0
                     and fact.dependencies == relation.dependencies == dependencies)
            value = evaluate(*(item.value for item, _ in operands))
            _require(claim["value"] == fact.value == relation.value == value, "explanation_relation_mismatch")
            kind = specs[fact.indicator_id].kind if fact.indicator_id in specs else "ma_pair"
            checked.append(CheckedClaim(claim["evidence_id"], fact.fact_id, fact.indicator_id,
                claim["relation"], value, dependencies, _render(label, kind, claim["relation"], value)))
        return ExplanationValidation("accepted", "indicator_relations_checked", request_id, tuple(checked))
    except _Rejected as error:
        return ExplanationValidation("rejected", str(error), request_id)
