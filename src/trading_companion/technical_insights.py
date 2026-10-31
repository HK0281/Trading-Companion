"""Bounded technical observations derived from one checked, frozen daily snapshot."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from math import isfinite
from statistics import mean

from trading_companion.daily_bar import DailyBar
from trading_companion.ohlc_weeks import group_weeks, aggregate_prices
from trading_companion import indicators as calculations
from trading_companion.multi_contracts import Fact, IndicatorSpec, MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report, indicator_label


VERSION = "technical-insights-v1-pivot2-lookback120-age20-cluster1pct-complete-week"
LOOKBACK = 120
PIVOT_NEIGHBOURS = 2
MAX_PIVOT_AGE = 20
CLUSTER_FRACTION = 0.01
LIMITATIONS = (
    "背离、支撑和压力仅是固定规则候选，不能证明反转、突破有效或未来收益。",
    "极值需要右侧记录确认；只使用当前快照中已完成的记录。",
    "周线仅汇总完整日历周，保守排除未结束周及请求起点处的不完整周。",
    "未使用交易所日历补全缺失行情；周线沿用日线复权口径与指标算法。",
)


@dataclass(frozen=True)
class TechnicalInsight:
    key: str
    instrument_id: str
    trade_date: date
    category: str
    status: str
    text: str
    value: int | float | str | None = None
    unit: str = "relation"
    dependencies: tuple[str, ...] = ()
    timeframe: str = "daily"
    indicator_id: str = "technical_insights"
    field: str = "observation"


@dataclass(frozen=True)
class TechnicalInsights:
    status: str
    code: str
    request_id: str | None = None
    instrument_id: str | None = None
    trade_date: date | None = None
    observations: tuple[TechnicalInsight, ...] = ()
    facts: tuple[Fact, ...] = ()
    weekly_bars: tuple[DailyBar, ...] = ()
    limitations: tuple[str, ...] = LIMITATIONS

    def to_dict(self):
        """Return detached, JSON-compatible public data without model prose."""
        def encode(value):
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [encode(item) for item in value]
            return value

        return {"version": VERSION, **encode(asdict(self))}


def _validate_bars(bars, instrument_id=None):
    previous = None
    for bar in bars:
        if type(bar) is not DailyBar:
            raise ValueError("A validated daily bar is required")
        DailyBar.__post_init__(bar)
        if instrument_id is None:
            instrument_id = bar.instrument_id
        if bar.instrument_id != instrument_id or previous is not None and bar.trade_date <= previous:
            raise ValueError("Daily bars must have one identity and strictly increasing dates")
        previous = bar.trade_date


def group_daily_weeks(bars, *, start_date: date, as_of_date: date):
    """Group observed days by Monday, omitting the truncated first week."""
    if type(start_date) is not date or type(as_of_date) is not date or start_date > as_of_date:
        raise ValueError("Valid date boundaries are required")
    bars = tuple(bars)
    _validate_bars(bars)
    return group_weeks(bars, start_date=start_date, as_of_date=as_of_date)


def aggregate_completed_weeks(bars, *, start_date: date, as_of_date: date) -> tuple[DailyBar, ...]:
    """Aggregate observed rows only; a week is complete after its Sunday boundary."""
    output = []
    for monday, rows in group_daily_weeks(bars, start_date=start_date, as_of_date=as_of_date):
        if monday + timedelta(days=6) > as_of_date:
            continue
        prices = aggregate_prices(rows)
        output.append(DailyBar(
            rows[0].instrument_id, rows[-1].trade_date, prices.open_price,
            prices.high_price, prices.low_price,
            prices.close_price, sum(row.volume_shares for row in rows),
            sum(row.amount_cny for row in rows),
        ))
    return tuple(output)


def _pivots(bars, name, side):
    """Use strict extrema so flat plateaus never produce arbitrary pivots."""
    start = max(PIVOT_NEIGHBOURS, len(bars) - LOOKBACK)
    points = []
    for index in range(start, len(bars) - PIVOT_NEIGHBOURS):
        value = getattr(bars[index], name)
        neighbours = [getattr(bars[other], name)
                      for other in range(index - PIVOT_NEIGHBOURS, index + PIVOT_NEIGHBOURS + 1)
                      if other != index]
        if all(value > other if side == "high" else value < other for other in neighbours):
            points.append(index)
    return points


class _Evidence:
    def __init__(self, request, bars):
        self.request, self.bars = request, bars
        self.facts = {}

    def add(self, key, trade_date, value, unit, *, dependencies=(), parameters=None):
        if (type(value) not in (str, int, float)
                or type(value) is float and not isfinite(value)):
            raise ValueError("Technical evidence must contain a finite scalar")
        identifier = f"{self.request.request_id}|{self.request.instrument_id}|{trade_date.isoformat()}|{VERSION}|{key}"
        dependencies = tuple(dict.fromkeys(dependencies))
        # Dependencies must already exist and be known no later than the derived fact.
        # This construction order rules out cycles rather than checking membership alone.
        if (identifier in dependencies or trade_date > self.request.as_of_date
                or any(dependency not in self.facts or self.facts[dependency].trade_date > trade_date
                       for dependency in dependencies)):
            raise ValueError("Technical evidence dependencies must form a chronological DAG")
        fact = Fact(identifier, self.request.request_id, self.request.instrument_id,
                    trade_date, f"technical_insights:{key}", parameters or {}, key,
                    value, unit, dependencies)
        if identifier in self.facts and self.facts[identifier] != fact:
            raise ValueError("Technical evidence identifiers must be unambiguous")
        self.facts[identifier] = fact
        return identifier

    def bar(self, index, name):
        bar = self.bars[index]
        unit = "shares" if name == "volume_shares" else "CNY/share"
        return self.add(f"daily:{name}", bar.trade_date, getattr(bar, name), unit)

    def observe(self, key, category, text, value, *, status="available", unit="relation",
                dependencies=(), timeframe="daily", indicator_id="technical_insights", field="observation",
                trade_date=None):
        trade_date = trade_date or self.bars[-1].trade_date
        if status == "available":
            identifier = self.add(key, trade_date, value, unit, dependencies=dependencies)
            dependencies = (identifier,)
        return TechnicalInsight(key, self.request.instrument_id, trade_date, category,
                                status, text, value, unit, tuple(dependencies), timeframe,
                                indicator_id, field)


def _divergence(evidence, spec):
    bars = evidence.bars
    values = (calculations.rsi_close(bars, **spec.parameters) if spec.kind == "rsi" else
              [point.dif for point in calculations.macd_close(bars, **spec.parameters)])
    observations = []
    for side, name in (("high", "bearish"), ("low", "bullish")):
        key = f"divergence:{spec.indicator_id}:{name}"
        pivots = _pivots(bars, "close_price", side)
        label = "顶背离" if side == "high" else "底背离"
        if len(pivots) < 2 or any(values[index] is None for index in pivots[-2:]):
            observations.append(evidence.observe(key, "divergence", f"已确认极值或指标历史不足，无法核对{label}。",
                                                  None, status="insufficient_history", indicator_id=spec.indicator_id))
            continue
        left, right = pivots[-2:]
        dependencies = []
        for index in (left, right):
            # The confirmation rows are dependencies too, not just the turning-point prices.
            dependencies.extend(evidence.bar(other, "close_price")
                                for other in range(index - PIVOT_NEIGHBOURS, index + PIVOT_NEIGHBOURS + 1))
            dependencies.append(evidence.add(
                f"daily:{spec.indicator_id}:pivot_value", bars[index].trade_date, values[index],
                "index_points" if spec.kind == "rsi" else "CNY/share",
                dependencies=tuple(evidence.bar(other, "close_price") for other in range(index + 1)),
                parameters=spec.parameters,
            ))
        fresh = len(bars) - 1 - right <= MAX_PIVOT_AGE
        candidate = (bars[right].close_price > bars[left].close_price and values[right] < values[left]
                     if side == "high" else
                     bars[right].close_price < bars[left].close_price and values[right] > values[left])
        state = f"{name}_candidate" if fresh and candidate else "not_detected" if fresh else "stale_pivots"
        text = (f"已确认价格极值与{spec.kind.upper()}形成{label}规则候选，仍需后续价格与量能观察。"
                if state.endswith("_candidate") else
                f"最近可核对极值尚未形成{label}规则候选。" if fresh else
                f"可比较极值距离当前较远，不作为当前{label}候选。")
        observations.append(evidence.observe(key, "divergence", text, state,
                                              dependencies=dependencies, indicator_id=spec.indicator_id))
    return observations


def _levels(evidence):
    bars, observations = evidence.bars, []
    close = bars[-1].close_price
    for side, name, field, label in (("low", "support", "low_price", "支撑"),
                                     ("high", "resistance", "high_price", "压力")):
        pivots = _pivots(bars, field, side)
        clusters = []
        for index in sorted(pivots, key=lambda index: getattr(bars[index], field)):
            price = getattr(bars[index], field)
            if not clusters or price / getattr(bars[clusters[-1][0]], field) - 1 > CLUSTER_FRACTION:
                clusters.append([index])
            else:
                clusters[-1].append(index)
        candidates = [(mean(getattr(bars[index], field) for index in cluster), cluster)
                      for cluster in clusters]
        candidates = [(price, cluster) for price, cluster in candidates
                      if price <= close] if side == "low" else [
                          (price, cluster) for price, cluster in candidates if price >= close]
        key = f"levels:{name}"
        if not candidates:
            observations.append(evidence.observe(key, "levels", f"当前价对应一侧没有可用的历史{label}候选。",
                                                  None, status="insufficient_history", field=name))
            continue
        price, cluster = min(candidates, key=lambda pair: abs(pair[0] - close))
        dependencies = [evidence.bar(len(bars) - 1, "close_price")]
        for index in cluster:
            dependencies.extend(evidence.bar(other, field)
                                for other in range(index - PIVOT_NEIGHBOURS, index + PIVOT_NEIGHBOURS + 1))
        observations.append(evidence.observe(
            key, "levels", f"历史确认极值聚类得到的最近{label}候选，仅是价格观察区域。", price,
            unit="CNY/share", dependencies=dependencies, field=name,
        ))
        observations.append(evidence.observe(
            f"levels:{name}:pivot_count", "levels", f"该{label}候选所含的已确认历史极值数量，不等同有效触及次数。",
            len(cluster), unit="count", dependencies=dependencies, field="pivot_count",
        ))
    return observations


def _weekly(evidence):
    request, bars = evidence.request, evidence.bars
    weeks = aggregate_completed_weeks(bars, start_date=request.start_date, as_of_date=request.as_of_date)
    output = []
    specs = tuple(spec for spec in request.indicators if spec.kind in ("sma", "ema", "rsi", "macd"))
    if not weeks:
        return [evidence.observe("weekly:availability", "weekly", "当前快照没有可汇总的完整周线。", None,
                                 status="insufficient_history", timeframe="weekly")], weeks
    by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
    weekly_close_facts = tuple(evidence.add(
        "weekly:close_price", bar.trade_date, bar.close_price, "CNY/share",
        dependencies=(evidence.bar(by_date[bar.trade_date], "close_price"),),
    ) for bar in weeks)
    output.append(evidence.observe(
        "weekly:close_price", "weekly", "最近完整周线的收盘价。", weeks[-1].close_price,
        unit="CNY/share", dependencies=(evidence.bar(by_date[weeks[-1].trade_date], "close_price"),),
        timeframe="weekly", trade_date=weeks[-1].trade_date, field="close_price",
    ))
    latest = {}
    for spec in specs:
        function = {"sma": calculations.sma_close, "ema": calculations.ema_close,
                    "rsi": calculations.rsi_close, "macd": calculations.macd_close}[spec.kind]
        values = function(weeks, **spec.parameters)
        fields = (("dif", values[-1].dif), ("dea", values[-1].dea), ("histogram", values[-1].histogram)) if spec.kind == "macd" else (("value", values[-1]),)
        latest[spec.indicator_id] = dict(fields)
        for field, number in fields:
            key = f"weekly:{spec.indicator_id}:{field}"
            text = f"完整周线上的{indicator_label(spec)}读数。" if number is not None else f"完整周线历史不足，{indicator_label(spec)}尚不可用。"
            output.append(evidence.observe(
                key, "weekly", text, number, status="available" if number is not None else "insufficient_history",
                unit="index_points" if spec.kind == "rsi" else "CNY/share", dependencies=weekly_close_facts if number is not None else (),
                timeframe="weekly", indicator_id=spec.indicator_id, field=field, trade_date=weeks[-1].trade_date,
            ))
    means = sorted((spec for spec in specs if spec.kind in ("sma", "ema")),
                   key=lambda spec: (spec.parameters["window"], spec.kind))
    available = next((spec for spec in means if latest[spec.indicator_id]["value"] is not None), None)
    if available is None:
        output.append(evidence.observe("timeframes:alignment", "alignment", "缺少同参数可用均线，日周方向暂不能比较。",
                                       None, status="insufficient_history", timeframe="cross_timeframe"))
    else:
        function = calculations.sma_close if available.kind == "sma" else calculations.ema_close
        daily_mean = function(bars, **available.parameters)[-1]
        weekly_mean = latest[available.indicator_id]["value"]
        day = "above" if bars[-1].close_price > daily_mean else "below" if bars[-1].close_price < daily_mean else "equal"
        week = "above" if weeks[-1].close_price > weekly_mean else "below" if weeks[-1].close_price < weekly_mean else "equal"
        daily_dependencies = tuple(evidence.bar(index, "close_price") for index in range(len(bars)))
        daily_id = evidence.add(f"daily:{available.indicator_id}:alignment_mean", bars[-1].trade_date,
                                daily_mean, "CNY/share", dependencies=daily_dependencies, parameters=available.parameters)
        weekly_id = next(item.dependencies[0] for item in output
                         if item.key == f"weekly:{available.indicator_id}:value")
        state = "aligned_above" if day == week == "above" else "aligned_below" if day == week == "below" else "boundary" if "equal" in (day, week) else "conflicting"
        text = {"aligned_above": "日线与最近完整周线均位于同参数均线上方，参考方向一致。",
                "aligned_below": "日线与最近完整周线均位于同参数均线下方，参考方向一致。",
                "conflicting": "日线与最近完整周线位于同参数均线不同侧，存在周期分歧。",
                "boundary": "至少一个周期位于均线边界，暂不判断方向一致。"}[state]
        output.append(evidence.observe("timeframes:alignment", "alignment", text, state,
                                       dependencies=(daily_id, weekly_id, evidence.bar(len(bars) - 1, "close_price"), weekly_close_facts[-1]),
                                       timeframe="cross_timeframe", indicator_id=available.indicator_id))
    return output, weeks


def build_technical_insights(result: MultiIndicatorResult, *,
                             expected_request: MultiIndicatorRequest | None = None) -> TechnicalInsights:
    """Reject inconsistent input, then recompute historical dependencies from frozen bars."""
    blocked = TechnicalInsights("blocked", "technical_insights_evidence_mismatch")
    if type(result) is not MultiIndicatorResult or type(expected_request) is not MultiIndicatorRequest:
        return blocked
    try:
        # The report boundary checks request identity, source metadata and registered facts.
        report = build_research_report(result, expected_request=expected_request)
        if report.status == "blocked":
            return blocked
        if report.status == "unavailable":
            return TechnicalInsights("unavailable", "technical_insights_no_data",
                                      expected_request.request_id, expected_request.instrument_id)
        _validate_bars(result.bars, expected_request.instrument_id)
        if any(not expected_request.start_date <= bar.trade_date <= expected_request.as_of_date for bar in result.bars):
            return blocked
        evidence = _Evidence(expected_request, result.bars)
        observations = []
        for spec in expected_request.indicators:
            if spec.kind in ("rsi", "macd"):
                observations.extend(_divergence(evidence, spec))
        observations.extend(_levels(evidence))
        weekly, weeks = _weekly(evidence)
        observations.extend(weekly)
        identifiers = set(evidence.facts)
        if any(dependency not in identifiers for fact in evidence.facts.values() for dependency in fact.dependencies):
            return blocked
        status = "complete" if all(item.status == "available" for item in observations) else "limited"
        return TechnicalInsights(status, "technical_insights_computed", expected_request.request_id,
                                  expected_request.instrument_id, result.latest_trade_date,
                                  tuple(observations), tuple(evidence.facts.values()), weeks)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        return blocked
