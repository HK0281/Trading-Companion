"""Deterministic, next-open exposure replay over one validated research snapshot."""

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
import re

from trading_companion.daily_bar import DailyBar
from trading_companion.indicators import sma_close
from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report


VERSION = "historical-replay-v1-sma-next-open"
METHOD = "normalized_long_or_cash_exposure_not_order_fill_simulation"


@dataclass(frozen=True)
class ReplayConfig:
    window: int = 20
    cost_bps: float = 5.0

    def __post_init__(self):
        if type(self.window) is not int or not 2 <= self.window <= 120:
            raise ValueError("Replay SMA window must be between 2 and 120")
        if type(self.cost_bps) not in (int, float) or not isfinite(self.cost_bps) or not 0 <= self.cost_bps <= 100:
            raise ValueError("Replay turnover cost must be finite and between 0 and 100 basis points")


def replay_config_from_question(question, indicators, *, indicators_explicit=False):
    """Honor one explicit SMA and a bounded numeric cost instead of optimizing them."""
    smas = [spec for spec in indicators if spec.kind == "sma"]
    if indicators_explicit and (len(smas) != 1 or len(indicators) != 1):
        raise ValueError("Replay needs exactly one explicit SMA")
    if re.search(r"做空|空头|双向|杠杆|融资|融券|金叉|死叉|交叉|止盈|止损|周线|周均线|周SMA|日内|分钟|低于均线|均线下方", question, re.I):
        raise ValueError("Custom strategy rules are not part of the fixed SMA long-or-cash replay")
    window = smas[0].parameters["window"] if indicators_explicit else 20
    cost = 5.0
    if re.search(r"手续费|交易成本|费率|成本", question):
        matches = list(re.finditer(r"(?:手续费|交易成本|费率|成本)\s*(?:为|是|[:：])?\s*"
                                   r"([0-9]+(?:\.[0-9]+)?)\s*(%|基点|bps)", question, re.I))
        if len(matches) != 1:
            raise ValueError("Specify a single numeric cost with percent or basis-point units")
        match = matches[0]
        cost = float(match[1]) * (100 if match[2] == "%" else 1)
    return ReplayConfig(window, cost)


@dataclass(frozen=True)
class ReplayStep:
    signal_date: str
    start_date: str
    end_date: str
    signal_close: float
    signal_sma: float
    exposure: int
    changed: bool
    interval_return: float
    turnover_cost: float
    equity: float
    benchmark_equity: float


@dataclass(frozen=True)
class HistoricalReplay:
    status: str
    code: str
    request_id: str
    instrument_id: str
    config: ReplayConfig
    source: str
    price_adjustment: str
    data_authenticity: str
    start_date: str | None = None
    end_date: str | None = None
    total_return_percent: float | None = None
    benchmark_return_percent: float | None = None
    excess_return_percentage_points: float | None = None
    max_drawdown_percent: float | None = None
    benchmark_max_drawdown_percent: float | None = None
    turnover_count: int = 0
    active_intervals: int = 0
    final_liquidation_cost: float = 0.0
    steps: tuple[ReplayStep, ...] = ()
    limitation_codes: tuple[str, ...] = (
        "snapshot_revisions_not_historical_versions",
        "unadjusted_prices_may_include_corporate_action_jumps",
        "no_lot_size_limit_price_liquidity_or_order_fill_model",
        "cost_is_fixed_exposure_turnover_assumption_not_broker_fee_schedule",
        "same_sample_result_not_forecast_or_out_of_sample_validation",
    )

    def to_dict(self):
        result = asdict(self)
        result.update(version=VERSION, method=METHOD, trading_enabled=False)
        result["steps"] = [asdict(step) for step in self.steps]
        result["limitation_codes"] = list(self.limitation_codes)
        return result


def _drawdown(equities):
    peak, maximum = 1.0, 0.0
    for value in equities:
        peak = max(peak, value)
        maximum = max(maximum, 1.0 - value / peak)
    return maximum * 100


def replay_bars(bars, *, request_id, instrument_id, config=None, source="csv",
                price_adjustment="unknown", data_authenticity="unknown") -> HistoricalReplay:
    """Use yesterday's completed SMA signal at today's open, never today's close.

    Both strategy and benchmark use exactly the same open-to-open intervals.
    A target exposure is a mathematical return weight, not a share order or fill.
    The final held exposure is liquidated at the final observed open with cost.
    """
    config = ReplayConfig() if config is None else config
    if type(config) is not ReplayConfig:
        raise ValueError("Use a validated replay configuration")
    ReplayConfig(config.window, config.cost_bps)
    if type(request_id) is not str or not request_id or len(request_id) > 100:
        raise ValueError("A bounded research request identity is required")
    bars = tuple(bars)
    if len(bars) > 366:
        raise ValueError("Replay accepts at most 366 observed daily records")
    previous = None
    for bar in bars:
        if type(bar) is not DailyBar:
            raise ValueError("Replay requires daily records")
        DailyBar.__post_init__(bar)
        if bar.instrument_id != instrument_id or previous is not None and bar.trade_date <= previous:
            raise ValueError("Replay records must have one identity and increasing dates")
        previous = bar.trade_date
    base = dict(request_id=request_id, instrument_id=instrument_id, config=config, source=source,
                price_adjustment=price_adjustment, data_authenticity=data_authenticity)
    if len(bars) < config.window + 2:
        return HistoricalReplay("unavailable", "replay_insufficient_history", **base)
    means = sma_close(bars, config.window)
    rate = config.cost_bps / 10000
    equity, benchmark, exposure, turnovers, active = 1.0, 1.0, 0, 0, 0
    equities, benchmarks, steps = [1.0], [1.0], []
    for index in range(config.window, len(bars) - 1):
        signal, current, following = bars[index - 1], bars[index], bars[index + 1]
        mean = means[index - 1]
        target = int(signal.close_price > mean)
        changed = target != exposure
        fee = rate if changed else 0.0
        exposure = target
        turnovers += int(changed)
        active += exposure
        interval = following.open_price / current.open_price - 1.0
        equity *= 1.0 - fee
        equities.append(equity)
        equity *= 1.0 + exposure * interval
        if not steps:
            benchmark *= 1.0 - rate
            benchmarks.append(benchmark)
        benchmark *= 1.0 + interval
        if not all(isfinite(value) and value > 0 for value in (equity, benchmark)):
            raise ValueError("Replay equity must remain finite and positive")
        equities.append(equity)
        benchmarks.append(benchmark)
        steps.append(ReplayStep(signal.trade_date.isoformat(), current.trade_date.isoformat(),
            following.trade_date.isoformat(), signal.close_price, mean, exposure, changed,
            interval, fee, equity, benchmark))
    liquidation = rate if exposure else 0.0
    equity *= 1.0 - liquidation
    benchmark *= 1.0 - rate
    turnovers += int(bool(exposure))
    equities.append(equity)
    benchmarks.append(benchmark)
    total, baseline = (equity - 1.0) * 100, (benchmark - 1.0) * 100
    return HistoricalReplay("available", "historical_replay_computed", **base,
        start_date=bars[config.window].trade_date.isoformat(), end_date=bars[-1].trade_date.isoformat(),
        total_return_percent=total, benchmark_return_percent=baseline,
        excess_return_percentage_points=total - baseline, max_drawdown_percent=_drawdown(equities),
        benchmark_max_drawdown_percent=_drawdown(benchmarks), turnover_count=turnovers,
        active_intervals=active, final_liquidation_cost=liquidation, steps=tuple(steps))


def build_historical_replay(result, *, expected_request, config=None):
    """Reject mismatched public research snapshots before using their frozen bars."""
    if type(result) is not MultiIndicatorResult or type(expected_request) is not MultiIndicatorRequest:
        raise ValueError("Replay requires a checked research snapshot")
    report = build_research_report(result, expected_request=expected_request)
    if report.status == "blocked":
        raise ValueError("Replay snapshot binding is invalid")
    return replay_bars(result.bars, request_id=expected_request.request_id,
        instrument_id=expected_request.instrument_id, config=config, source=result.source,
        price_adjustment=result.price_adjustment, data_authenticity=result.data_authenticity)


def replay_archived_snapshot(payload, *, instrument_id, config=None):
    """Consume only a payload returned by ResearchSnapshotStore.load().

    The archive's known_at policy is enforced by the caller before this function;
    a local observation timestamp does not restore upstream historical versions.
    """
    if (type(payload) is not dict or payload.get("trading_enabled") is not False
            or type(payload.get("companies")) is not list):
        raise ValueError("A validated archive payload is required")
    matches = [company for company in payload["companies"] if company.get("instrument_id") == instrument_id]
    if len(matches) != 1 or type(matches[0].get("technical")) is not dict:
        raise ValueError("The archive has no unique technical snapshot for this security")
    technical = matches[0]["technical"]
    request = technical["request"]
    if request["instrument_id"] != instrument_id:
        raise ValueError("Archived request identity mismatch")
    bars = [DailyBar(**{**bar, "trade_date": date.fromisoformat(bar["trade_date"])})
            for bar in technical["bars"]]
    start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["as_of_date"])
    if any(not start <= bar.trade_date <= end for bar in bars):
        raise ValueError("Archived records exceed the request range")
    return replay_bars(bars, request_id=request["request_id"], instrument_id=instrument_id, config=config,
        source=technical["source"], price_adjustment=technical["price_adjustment"],
        data_authenticity=technical["data_authenticity"])
