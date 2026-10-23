"""Deterministic, evidence-bound reference comparisons, never a trading forecast."""

from dataclasses import asdict, dataclass
from datetime import date
import json
from math import isfinite

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_contracts import (
    ALGORITHM_VERSIONS, ComponentMetadata, Fact, FieldResult, IndicatorResult,
    IndicatorRow, MultiIndicatorRequest, MultiIndicatorResult,
)
from trading_companion.research_report import build_research_report, indicator_field_units, indicator_label


VERSION = "research-synthesis-v1"
VALIDATION_SCOPE = "request_local_numeric_reference_comparisons_not_predictions"
MAX_PUBLIC_BYTES = 128 * 1024
_TITLES = {"trend": "趋势参考", "momentum": "动量参考", "volatility": "波动尺度", "volume": "量能参考"}
_STATUS_TEXT = {"insufficient_history": "历史记录不足", "undefined": "零基线导致未定义",
                "calculation_error": "计算失败", "previous_record_unavailable": "没有可核对的紧邻前一条记录"}
_REASONS = {"available": "ready", "insufficient_history": "insufficient_records",
            "undefined": "zero_prior_volume_baseline", "calculation_error": "numeric_calculation_failed"}


@dataclass(frozen=True)
class SynthesisObservation:
    observation_id: str
    dimension: str
    indicator_id: str
    relation: str
    value: str
    trade_date: str
    dependencies: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class SynthesisMissing:
    indicator_id: str | None
    kind: str
    field: str
    status: str
    trade_date: str | None
    required_bars: int | None = None
    actual_bars: int | None = None
    missing_bars: int | None = None


@dataclass(frozen=True)
class SynthesisDimension:
    dimension: str
    status: str
    state: str
    text: str
    observations: tuple[SynthesisObservation, ...] = ()
    missing: tuple[SynthesisMissing, ...] = ()


@dataclass(frozen=True)
class SynthesisAlignment:
    state: str
    text: str
    dimensions: tuple[str, ...] = ("trend", "momentum")
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class SynthesisCondition:
    condition_id: str
    dimension: str
    text: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class ResearchSynthesis:
    status: str
    code: str
    text: str
    request_id: str | None = None
    instrument_id: str | None = None
    trade_date: str | None = None
    dimensions: tuple[SynthesisDimension, ...] = ()
    cross_dimension: SynthesisAlignment | None = None
    conditions: tuple[SynthesisCondition, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        dependencies = dict.fromkeys(
            dependency for dimension in self.dimensions for item in dimension.observations
            for dependency in item.dependencies
        )
        # Every exported collection is detached from both this object and the analysis.
        return {"version": VERSION, "origin": "application", "validation_scope": VALIDATION_SCOPE,
                "status": self.status, "code": self.code, "text": self.text,
                "request_id": self.request_id, "instrument_id": self.instrument_id,
                "trade_date": self.trade_date, "dimensions": [asdict(item) for item in self.dimensions],
                "cross_dimension": asdict(self.cross_dimension) if self.cross_dimension is not None else None,
                "conditions": [asdict(item) for item in self.conditions],
                "limitations": list(self.limitations), "evidence_ids": list(dependencies)}


class _IntegrityError(ValueError):
    pass


def _require(condition: bool) -> None:
    if not condition:
        raise _IntegrityError("Synthesis evidence is inconsistent")


def _finite(value) -> bool:
    return type(value) is int or type(value) is float and isfinite(value)


def _required(spec, name):
    if spec.kind == "macd":
        return spec.parameters["slow"] + (0 if name == "dif" else spec.parameters["signal"] - 1)
    window = spec.parameters["window"]
    if spec.kind == "volume":
        return 1 if name == "volume_shares" else window + (name in ("previous_volume_ma", "relative_volume"))
    return window + (spec.kind in ("rsi", "atr"))


def _compare(left, right):
    return "upper_reference" if left > right else "lower_reference" if left < right else "at_reference"


def _change(current, previous):
    return "increasing" if current > previous else "decreasing" if current < previous else "unchanged"


def _reference_state(states):
    unique = set(states)
    if len(unique) <= 1:
        return next(iter(unique)) if unique else "unknown"
    if "at_reference" in unique and not {"upper_reference", "lower_reference"} <= unique:
        return "boundary_mixed"
    return "mixed_reference"


class _CheckedNumbers:
    def __init__(self, result, request):
        self.result, self.request = result, request
        _require(type(result.facts) is tuple and all(type(item) is Fact for item in result.facts))
        self.facts = {item.fact_id: item for item in result.facts}
        _require(len(self.facts) == len(result.facts))
        self.indicators = {item.indicator_id: item for item in result.indicators}
        for spec in request.indicators:
            indicator = self.indicators[spec.indicator_id]
            _require(type(indicator) is IndicatorResult and type(indicator.series) is tuple
                     and len(indicator.series) == len(result.bars)
                     and indicator.kind == spec.kind and indicator.parameters == spec.parameters
                     and all(type(value) is type(spec.parameters[name]) for name, value in indicator.parameters.items())
                     and indicator.algorithm_version == ALGORITHM_VERSIONS[spec.kind]
                     and type(indicator.components) is tuple
                     and all(type(item) is ComponentMetadata and type(item.start_effect_warning) is bool
                             for item in indicator.components))
            for offset, row in enumerate(reversed(indicator.series[-2:])):
                _require(type(row) is IndicatorRow and type(row.trade_date) is date
                         and row.trade_date == result.bars[-1 - offset].trade_date
                         and type(row.fields) is dict and row.fields.keys() == indicator_field_units(spec.kind).keys())
            for name in indicator_field_units(spec.kind):
                self.operand(spec, name)

    def operand(self, spec, name, *, prior=False):
        offset = int(prior)
        if len(self.result.bars) <= offset:
            return None, SynthesisMissing(spec.indicator_id, spec.kind, name, "previous_record_unavailable", None)
        row = self.indicators[spec.indicator_id].series[-1 - offset]
        field = row.fields[name]
        required, actual = _required(spec, name), len(self.result.bars) - offset
        _require(type(field) is FieldResult and type(field.trade_date) is date and field.trade_date == row.trade_date
                 and field.unit == indicator_field_units(spec.kind)[name]
                 and type(field.required_bars) is int and field.required_bars == required
                 and type(field.actual_bars) is int and field.actual_bars == actual
                 and type(field.missing_bars) is int and field.missing_bars == max(0, required - actual)
                 and type(field.status) is str and field.status in _REASONS
                 and field.reason == _REASONS[field.status])
        matching = [fact for fact in self.facts.values() if
                    (fact.indicator_id, fact.field, fact.trade_date) == (spec.indicator_id, name, row.trade_date)]
        if field.status != "available":
            _require(field.value is None and field.fact_id is None and not matching)
            _require(field.status != "insufficient_history" or field.missing_bars > 0)
            _require(field.status != "undefined" or spec.kind == "volume"
                     and name == "relative_volume" and field.missing_bars == 0)
            return None, SynthesisMissing(spec.indicator_id, spec.kind, name, field.status,
                                         row.trade_date.isoformat(), required, actual, field.missing_bars)
        _require(_finite(field.value) and field.missing_bars == 0 and len(matching) == 1)
        fact = matching[0]
        canonical = (f"{self.request.request_id}|{self.request.instrument_id}|{row.trade_date.isoformat()}|"
                     f"{spec.indicator_id}|{name}")
        _require(type(fact.fact_id) is str and fact.fact_id == field.fact_id == canonical
                 and fact.request_id == self.request.request_id and fact.instrument_id == self.request.instrument_id
                 and fact.parameters == spec.parameters and fact.unit == field.unit
                 and type(fact.value) is type(field.value) and fact.value == field.value and not fact.dependencies)
        if spec.kind in ("sma", "ema"):
            _require(field.value > 0)
        if spec.kind == "rsi":
            _require(0 <= field.value <= 100)
        if spec.kind in ("atr", "volume"):
            _require(field.value >= 0)
        if name == "volume_shares":
            _require(type(field.value) is int and field.value == self.result.bars[-1 - offset].volume_shares)
        return fact, None

    def close(self):
        bar = self.result.bars[-1]
        canonical = (f"{self.request.request_id}|{self.request.instrument_id}|{bar.trade_date.isoformat()}|"
                     "daily_bar|close_price")
        fact = self.facts.get(canonical)
        _require(fact is not None and fact.field == "close_price" and fact.indicator_id == "daily_bar"
                 and fact.trade_date == bar.trade_date and fact.request_id == self.request.request_id
                 and fact.instrument_id == self.request.instrument_id and fact.parameters == {}
                 and type(fact.value) is type(bar.close_price) and fact.value == bar.close_price
                 and fact.unit == "CNY/share" and not fact.dependencies)
        return fact


def _observation(dimension, spec, relation, value, operands, text):
    return SynthesisObservation(f"{spec.indicator_id}:{relation}", dimension, spec.indicator_id,
                                relation, value, operands[0].trade_date.isoformat(),
                                tuple(item.fact_id for item in operands), text)


def _dimension(name, state, observations, missing):
    available = bool(observations)
    status = "available" if available and not missing else "limited" if available else "unknown"
    descriptions = {
        "upper_reference": "已知参考位置在上侧", "lower_reference": "已知参考位置在下侧",
        "at_reference": "已知参考位置等于参考值", "mixed_reference": "不同实例或参考关系存在分歧",
        "boundary_mixed": "部分关系位于参考边界，不能称为全部一致，也不把中性边界判作相反信号",
        "increasing": "已知波动尺度描述增加", "decreasing": "已知波动尺度描述减少",
        "unchanged": "已知波动尺度描述不变", "mixed_scale": "不同波动尺度描述不一致",
        "unknown": "证据不足，不能形成该维度的完整描述",
    }
    lines = [f"{_TITLES[name]}：{descriptions[state]}。", *(item.text for item in observations)]
    seen = set()
    for item in missing:
        key = (item.indicator_id, item.kind, item.field, item.status, item.trade_date)
        if key in seen:
            continue
        seen.add(key)
        if item.status == "not_requested":
            lines.append(f"未请求{item.kind.upper()}，对应信息未知，不补造结论。")
        else:
            label = item.indicator_id or item.kind.upper()
            stamp = f"（{item.trade_date}）" if item.trade_date is not None else ""
            shortage = f"，还缺{item.missing_bars}条记录" if item.missing_bars else ""
            lines.append(f"{label} {item.field}{stamp}：{_STATUS_TEXT[item.status]}{shortage}，该项保留未知。")
    return SynthesisDimension(name, status, state, "\n".join(lines), tuple(observations), tuple(missing))


def _build_dimensions(numbers, request):
    observations = {key: [] for key in _TITLES}
    missing = {key: [] for key in _TITLES}
    states = {key: [] for key in _TITLES}
    specs = request.indicators
    families = {spec.kind for spec in specs}
    close = numbers.close()

    def get(dimension, spec, name, prior=False):
        fact, absent = numbers.operand(spec, name, prior=prior)
        if absent is not None:
            missing[dimension].append(absent)
        return fact

    def add(dimension, spec, relation, value, facts, text):
        observations[dimension].append(_observation(dimension, spec, relation, value, facts, text))

    for spec in specs:
        label = indicator_label(spec)
        if spec.kind in ("sma", "ema"):
            current, previous = get("trend", spec, "value"), get("trend", spec, "value", True)
            if current is not None:
                state = _compare(close.value, current.value)
                states["trend"].append(state)
                word = {"upper_reference": "高于", "lower_reference": "低于", "at_reference": "等于"}[state]
                add("trend", spec, "price_position", state, (close, current), f"收盘价{word}{label}；这里只比较当前价格与均线。")
            if current is not None and previous is not None:
                change = _change(current.value, previous.value)
                word = {"increasing": "增加", "decreasing": "减少", "unchanged": "不变"}[change]
                add("trend", spec, "mean_change", change, (current, previous), f"{label}相较紧邻前一条记录{word}，不外推后续方向。")
        elif spec.kind == "macd":
            dif, dea = get("momentum", spec, "dif"), get("momentum", spec, "dea")
            current, previous = get("momentum", spec, "histogram"), get("momentum", spec, "histogram", True)
            if dif is not None and dea is not None:
                state = _compare(dif.value, dea.value)
                states["momentum"].append(state)
                word = {"upper_reference": "高于", "lower_reference": "低于", "at_reference": "等于"}[state]
                add("momentum", spec, "dif_dea_position", state, (dif, dea), f"{label}的DIF{word}DEA，仅表示两条序列的相对位置。")
            if current is not None and previous is not None:
                change = _change(current.value, previous.value)
                word = {"increasing": "增加", "decreasing": "减少", "unchanged": "不变"}[change]
                add("momentum", spec, "histogram_change", change, (current, previous),
                    f"{label}柱值相较紧邻前一条记录{word}；柱值增加不等于价格将上涨。")
        elif spec.kind == "rsi":
            current = get("momentum", spec, "value")
            if current is not None:
                state = _compare(current.value, 50)
                states["momentum"].append(state)
                word = {"upper_reference": "高于", "lower_reference": "低于", "at_reference": "等于"}[state]
                zone = "below_30" if current.value < 30 else "above_70" if current.value > 70 else "between_30_and_70"
                zone_text = {"below_30": "低于30", "above_70": "高于70", "between_30_and_70": "位于30至70之间（含边界）"}[zone]
                add("momentum", spec, "rsi_midpoint", state, (current,), f"{label}{word}50参考中线；该中线不代表胜率或买卖触发。")
                add("momentum", spec, "rsi_zone", zone, (current,), f"{label}{zone_text}；30/70只是参考阈值，不推断反转。")
        elif spec.kind == "boll":
            current = [get("volatility", spec, name) for name in ("lower", "middle", "upper")]
            previous = [get("volatility", spec, name, True) for name in ("lower", "middle", "upper")]
            for operands in (current, previous):
                if all(item is not None for item in operands):
                    _require(operands[0].value <= operands[1].value <= operands[2].value)
            if all(item is not None for item in current):
                coincident = current[0].value == current[2].value
                add("volatility", spec, "bands_state", "coincident" if coincident else "distinct", tuple(current),
                    f"{label}上下轨{'重合' if coincident else '不重合'}；轨道宽度不赋予价格方向。")
            if all(item is not None for item in (*current, *previous)):
                current_width = current[2].value - current[0].value
                previous_width = previous[2].value - previous[0].value
                _require(_finite(current_width) and _finite(previous_width))
                change = _change(current_width, previous_width)
                states["volatility"].append(change)
                word = {"increasing": "变宽", "decreasing": "变窄", "unchanged": "不变"}[change]
                add("volatility", spec, "band_width_change", change, (current[2], current[0], previous[2], previous[0]),
                    f"{label}轨道相较紧邻前一条记录{word}；这是波动尺度变化，不是涨跌判断。")
        elif spec.kind == "atr":
            current, previous = get("volatility", spec, "value"), get("volatility", spec, "value", True)
            if current is not None:
                add("volatility", spec, "atr_scale", "zero" if current.value == 0 else "nonzero", (current,),
                    f"{label}波动尺度{'为零' if current.value == 0 else '非零'}；ATR本身没有价格方向。")
            if current is not None and previous is not None:
                change = _change(current.value, previous.value)
                states["volatility"].append(change)
                word = {"increasing": "增加", "decreasing": "减少", "unchanged": "不变"}[change]
                add("volatility", spec, "atr_change", change, (current, previous),
                    f"{label}相较紧邻前一条记录{word}；不把波动增加解释为上涨或下跌。")
        elif spec.kind == "volume":
            current = get("volume", spec, "volume_shares")
            baseline = get("volume", spec, "previous_volume_ma")
            ratio = get("volume", spec, "relative_volume")
            if baseline is not None and baseline.value == 0:
                field = numbers.indicators[spec.indicator_id].latest["relative_volume"]
                _require(field.status == "undefined")
                add("volume", spec, "zero_prior_baseline", "undefined", (baseline,),
                    f"{label}此前窗口均量为零，量比未定义；不能当作零倍或无限放量。")
            elif ratio is not None:
                _require(current is not None and baseline is not None and baseline.value > 0)
                state = _compare(ratio.value, 1)
                _require(state == _compare(current.value, baseline.value))
                states["volume"].append(state)
                word = {"upper_reference": "高于", "lower_reference": "低于", "at_reference": "等于"}[state]
                add("volume", spec, "relative_volume_position", state, (ratio, baseline, current),
                    f"{label}量比{word}1（相对严格此前窗口均量）；不能独立确认价格方向。")
    if not families & {"sma", "ema"}:
        missing["trend"].append(SynthesisMissing(None, "sma/ema", "value", "not_requested", None))
    for dimension, required_families in (("momentum", ("macd", "rsi")), ("volatility", ("boll", "atr")),
                                          ("volume", ("volume",))):
        for kind in required_families:
            if kind not in families:
                missing[dimension].append(SynthesisMissing(None, kind, "value", "not_requested", None))
    dimensions = []
    for name in _TITLES:
        state = _reference_state(states[name])
        if name == "volatility" and state == "mixed_reference":
            state = "mixed_scale"
        dimensions.append(_dimension(name, state, observations[name], missing[name]))
    return tuple(dimensions)


def _alignment(dimensions):
    trend, momentum = dimensions[:2]
    dependencies = tuple(dict.fromkeys(dependency for item in (trend, momentum)
        for observation in item.observations if observation.relation in ("price_position", "dif_dea_position", "rsi_midpoint")
        for dependency in observation.dependencies))
    if "mixed_reference" in (trend.state, momentum.state):
        state, text = "conflicting", "均线或动量参考内部存在分歧，不能合并成单一方向判断；缺失项仍然未知。"
    elif trend.status != "available" or momentum.status != "available":
        state, text = "incomplete", "趋势或动量证据不完整，跨维度一致性未知；不把未请求或不可用的指标当作支持。"
    elif trend.state in ("upper_reference", "lower_reference") and trend.state == momentum.state:
        state = "aligned_" + trend.state
        word = "上侧" if trend.state == "upper_reference" else "下侧"
        text = f"均线价格位置与动量参考位置同处{word}，仅说明本次参考关系相容，不代表独立确认或未来走势。"
    elif {trend.state, momentum.state} == {"upper_reference", "lower_reference"}:
        state, text = "conflicting", "均线价格位置与动量参考位置不一致，不能合并为单一方向判断。"
    else:
        state, text = "indeterminate", "至少一类关系恰好位于参考边界，不能据此声称跨维度同向。"
    return SynthesisAlignment(state, "跨维度核对：" + text, dependencies=dependencies)


def _conditions(dimensions):
    texts = {
        "trend": "若后续收盘价跨到当日同参数均线的另一侧，需用新记录重新核对各实例；本轮不预判穿越，也不把当前均线固定为支撑或止损价。",
        "momentum": "若后续DIF/DEA相对位置或RSI参考区域变化，需重新核对二者是否仍相容；满足参考条件不意味着收益或反转。",
        "volatility": "若后续BOLL轨道变宽且ATR增加，只能更新为波动尺度描述同时增加，仍不能据此判断涨跌。",
        "volume": "若后续此前均量基线仍非零，可重新核对量比与1的关系；基线为零时继续保留量比未知，不独立确认价格方向。",
    }
    output = []
    for item in dimensions:
        dependencies = tuple(dict.fromkeys(dependency for observation in item.observations
                                           for dependency in observation.dependencies))
        text = texts[item.dimension] if item.observations else f"先补齐{_TITLES[item.dimension]}所需记录和指标，再核对条件；本轮该维度未知。"
        output.append(SynthesisCondition(item.dimension + "_recheck", item.dimension, text, dependencies))
    return tuple(output)


def build_research_synthesis(result: MultiIndicatorResult, *,
                             expected_request: MultiIndicatorRequest | None) -> ResearchSynthesis:
    """Use numerical facts only, ignoring all unverified relation labels and free text."""
    blocked = ResearchSynthesis("blocked", "synthesis_evidence_mismatch",
                               "研究综合解读未通过请求或证据检查，未展示综合结论；请重新研究。")
    try:
        _require(type(expected_request) is MultiIndicatorRequest)
        request = MultiIndicatorRequest(
            expected_request.instrument_id, expected_request.start_date, expected_request.as_of_date,
            expected_request.indicators, expected_request.source, expected_request.request_id,
        )
        _require(request == expected_request and type(result) is MultiIndicatorResult
                 and type(result.bars) is tuple and len(result.bars) <= 366
                 and type(result.indicators) is tuple and len(result.indicators) <= 12)
        report = build_research_report(result, expected_request=request)
        if report.status == "unavailable":
            return ResearchSynthesis("unavailable", "synthesis_data_unavailable",
                                     "没有可核对的有效行情，无法进行跨指标解读；不据此推断停牌、退市或缺失原因。",
                                     request.request_id, request.instrument_id)
        _require(report.status in ("complete", "limited") and bool(result.bars))
        for bar in result.bars:
            _require(type(bar) is DailyBar)
            DailyBar.__post_init__(bar)
        dimensions = _build_dimensions(_CheckedNumbers(result, request), request)
        alignment = _alignment(dimensions)
        conditions = _conditions(dimensions)
        limitations = (*report.limitations,
            "综合解读只核对最新及紧邻前一条记录的数值依赖，不验证全部历史指标或预测质量。",
            "同族多参数实例分别展示但不计票；跨维度只核对均线价格位置与MACD/RSI参考位置，不计算分数或胜率。",
            "RSI的50是本项目显式参考中线；30/70含边界规则不构成买卖信号。",
            "EMA/MACD/RSI/ATR的递推初始化依赖计算起点；数值可用不证明初始化影响已充分消除。",
            "波动与量能不参与价格方向表决；观察条件不是交易触发器，不提供目标价或收益概率。")
        banner = ("人工样例 / 非真实行情。" if result.data_authenticity == "synthetic" else
                  "开发CSV / 真实性未经验证。" if result.source == "csv" else
                  "供应商历史日线 / 未经独立认证，不是实时行情。")
        lines = [banner, "跨指标现状解读（程序生成，不含模型自由文本）。",
                 f"研究对象：{request.instrument_id}；区间：{request.start_date.isoformat()} 至 {request.as_of_date.isoformat()}；"
                 f"实际最新交易日：{result.latest_trade_date.isoformat()}。",
                 *(item.text for item in dimensions), alignment.text, "条件化观察（需要新的记录核对）：",
                 *(item.text for item in conditions), "限制：", *limitations]
        status = "complete" if report.status == "complete" and all(item.status == "available" for item in dimensions) else "limited"
        synthesis = ResearchSynthesis(status, "checked_reference_synthesis", "\n".join(lines),
                                       request.request_id, request.instrument_id, result.latest_trade_date.isoformat(),
                                       dimensions, alignment, conditions, limitations)
        if len(json.dumps(synthesis.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_PUBLIC_BYTES:
            return ResearchSynthesis("unavailable", "synthesis_payload_limit", "综合解读超出安全展示范围，未截断或展示部分结论。")
        return synthesis
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError, RecursionError):
        return blocked
