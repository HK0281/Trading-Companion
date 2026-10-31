"""Chart-only time series. No research, evidence, session memory or model calls."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from math import isfinite

from trading_companion import _indicator_values as calculations
from trading_companion.chart_projection import MAX_JSON_BYTES, MAX_ABSOLUTE_VALUE, _required_bars
from trading_companion.daily_bar import DailyBar
from trading_companion.multi_contracts import ALGORITHM_VERSIONS, IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_report import FIELD_LABELS, indicator_field_units, indicator_label
from trading_companion.technical_insights import group_daily_weeks
from trading_companion.ohlc_weeks import aggregate_prices
from trading_companion.tencent_adapter import to_tencent_symbol

VERSION = "research-chart-interval-v1"
SHANGHAI = timezone(timedelta(hours=8))
INTERVALS = ("1w", "60m", "120m")


class ChartUnavailable(ValueError):
    """Fixed reason only; no upstream payload or exception text crosses the API."""

    def __init__(self, code="chart_data_unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChartBar:
    start: datetime
    end: datetime
    values: calculations.NumericBar
    complete: bool = True


@dataclass(frozen=True)
class ChartBinding:
    request: MultiIndicatorRequest
    snapshot: dict
    bars: tuple[DailyBar, ...]


def bind_chart(turn, data_mode):
    """Resolve only a validated single-stock projection from a completed local record."""
    if turn.get('index_research') is not None:
        from trading_companion.index_chart import bind_index_chart
        return bind_index_chart(turn, data_mode)
    try:
        if turn.get("status") not in ("completed", "limited"):
            raise ValueError()
        chart, parameters, report = turn["chart"], turn["parameters"], turn["report"]
        company = turn.get("company_research")
        if company is not None and (type(company) is not dict or type(company.get("securities")) is not list
                or len(company["securities"]) != 1 or company["securities"][0].get("instrument_id") != chart["instrument_id"]):
            raise ValueError()
        if (chart["version"] != "research-chart-v1" or chart["status"] != "available"
                or chart["code"] != "aligned_research_series"
                or chart["request_id"] != report["request_id"]
                or chart["source"] != parameters["source"]
                or any(chart[key] != parameters[key] for key in ("instrument_id", "start_date", "as_of_date"))
                or chart["source"] not in ("csv", "tencent", "akshare_eastmoney")
                or (data_mode == "synthetic") != (chart["source"] == "csv")
                or data_mode != "synthetic" and chart["source"] != data_mode
                or not 1 <= len(chart["bars"]) <= 366 or not 1 <= len(chart["series"]) <= 48):
            raise ValueError()
        if chart["source"] == "csv":
            if chart["price_adjustment"] != "unknown" or chart["data_authenticity"] not in ("synthetic", "unverified_csv"):
                raise ValueError()
        elif chart["price_adjustment"] != "unadjusted" or chart["data_authenticity"] != "provider_reported":
            raise ValueError()
        specs = tuple(IndicatorSpec(item["kind"], **{k: v for k, v in item.items() if k != "kind"})
                      for item in parameters["indicators"])
        request = MultiIndicatorRequest(chart["instrument_id"], date.fromisoformat(chart["start_date"]),
            date.fromisoformat(chart["as_of_date"]), specs, chart["source"], chart["request_id"])
        expected = {(spec.indicator_id, name): spec for spec in specs for name in indicator_field_units(spec.kind)}
        seen = set()
        for series in chart["series"]:
            key = (series["indicator_id"], series["field"])
            spec = expected[key]
            if (key in seen or series["kind"] != spec.kind or series["parameters"] != spec.parameters
                    or series["algorithm_version"] != ALGORITHM_VERSIONS[spec.kind]
                    or series["unit"] != indicator_field_units(spec.kind)[series["field"]]
                    or len(series["values"]) != len(chart["bars"]) or len(series["statuses"]) != len(chart["bars"])):
                raise ValueError()
            seen.add(key)
        if seen != set(expected) or len(specs) > 12:
            raise ValueError()
        bars = tuple(DailyBar(request.instrument_id, date.fromisoformat(row["date"]), row["open"], row["high"],
                              row["low"], row["close"], row["volume"], 0) for row in chart["bars"])
        previous = None
        for bar in bars:
            if (not request.start_date <= bar.trade_date <= request.as_of_date
                    or previous is not None and previous >= bar.trade_date
                    or any(abs(value) > MAX_ABSOLUTE_VALUE for value in
                           (bar.open_price, bar.high_price, bar.low_price, bar.close_price, bar.volume_shares))):
                raise ValueError()
            previous = bar.trade_date
        if len(json.dumps(chart, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError()
        return ChartBinding(request, deepcopy(chart), bars)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise ChartUnavailable("chart_binding_invalid") from None


def capabilities(binding, data_mode):
    from trading_companion.index_chart import IndexChartBinding, index_capabilities
    if type(binding) is IndexChartBinding:
        return index_capabilities(binding)
    code = "ready"
    try:
        to_tencent_symbol(binding.request.instrument_id)
    except ValueError:
        code = "chart_market_unsupported"
    if data_mode != "tencent" or binding.snapshot["source"] != "tencent":
        code = "chart_hourly_source_unsupported"
    return {"version": "research-chart-capabilities-v1", "research_id": binding.request.request_id,
            "intervals": {"1w": {"enabled": True, "code": "ready"},
                          **{interval: {"enabled": code == "ready", "code": code} for interval in ("60m", "120m")}}}


def weekly_bars(binding):
    output = []
    request = binding.request
    for monday, rows in group_daily_weeks(binding.bars, start_date=request.start_date, as_of_date=request.as_of_date):
        prices = aggregate_prices(rows)
        output.append(ChartBar(datetime.combine(rows[0].trade_date, time(9, 30), SHANGHAI),
            datetime.combine(rows[-1].trade_date, time(15), SHANGHAI),
            calculations.NumericBar(prices.open_price, prices.high_price,
                prices.low_price, prices.close_price, sum(row.volume_shares for row in rows)),
            monday + timedelta(days=6) <= request.as_of_date))
    if not output:
        raise ChartUnavailable("chart_insufficient_history")
    return tuple(output)


def two_hour_bars(bars):
    output = []
    if len(bars) % 2:
        raise ChartUnavailable("chart_hourly_pair_missing")
    for first, last in zip(bars[::2], bars[1::2]):
        if (first.end.date() != last.end.date()
                or (first.end.time(), last.end.time()) not in ((time(10, 30), time(11, 30)), (time(14), time(15)))):
            raise ChartUnavailable("chart_hourly_pair_missing")
        a, b = first.values, last.values
        output.append(ChartBar(first.start, last.end, calculations.NumericBar(
            a.open_price, max(a.high_price, b.high_price), min(a.low_price, b.low_price), b.close_price,
            a.volume_shares + b.volume_shares)))
    return tuple(output)


def chart_series(bars, specs):
    functions = {"sma": calculations.sma_close, "ema": calculations.ema_close, "macd": calculations.macd_close,
        "rsi": calculations.rsi_close, "boll": calculations.boll_close, "atr": calculations.atr,
        "volume": calculations.volume_activity, "adx": calculations.adx, "obv": calculations.obv, "vwma": calculations.vwma}
    numeric = tuple(bar.values for bar in bars)
    output = []
    for spec in specs:
        failed = False
        try:
            values = functions[spec.kind](numeric, **spec.parameters)
        except (ValueError, ArithmeticError):
            failed, values = True, [None] * len(bars)
        for name, unit in indicator_field_units(spec.kind).items():
            numbers, statuses = [], []
            required = _required_bars(spec, name)
            for index, result in enumerate(values):
                value = None if failed else (result if name == "value" and spec.kind not in ("adx", "vwma") else getattr(result, name))
                status = ("calculation_error" if failed else "insufficient_history" if index + 1 < required
                          else "undefined" if value is None else "available")
                if value is not None and (type(value) not in (int, float) or abs(value) > MAX_ABSOLUTE_VALUE or not isfinite(value)):
                    raise ChartUnavailable("chart_payload_limit")
                numbers.append(value)
                statuses.append(status)
            output.append({"id": spec.indicator_id + ":" + name, "indicator_id": spec.indicator_id,
                "kind": spec.kind, "parameters": spec.parameters, "algorithm_version": ALGORITHM_VERSIONS[spec.kind],
                "field": name, "label": indicator_label(spec) + ("" if name == "value" else " " + FIELD_LABELS[name]),
                "unit": unit, "start_effect_warning": not failed and spec.kind in ("ema", "macd", "rsi", "atr", "adx")
                    and required <= len(bars) < required + 3 * max(spec.parameters.values()),
                "values": numbers, "statuses": statuses})
    if len(output) > 48:
        raise ChartUnavailable("chart_payload_limit")
    return output


def build_interval_chart(binding, interval, bars, retrieved_at):
    for bar in bars:
        numeric = bar.values
        if any(type(value) not in (int, float) or not isfinite(value) or abs(value) > MAX_ABSOLUTE_VALUE for value in
               (numeric.open_price, numeric.high_price, numeric.low_price, numeric.close_price, numeric.volume_shares)):
            raise ChartUnavailable("chart_payload_limit")
    snapshot = binding.snapshot
    payload = {key: snapshot[key] for key in ("request_id", "instrument_id", "start_date", "as_of_date", "source",
        "price_adjustment", "data_authenticity", "context", "saved_at")}
    payload.update(version=VERSION, status="available", code="chart_only_series", interval=interval,
        timezone="Asia/Shanghai", retrieved_at=retrieved_at.isoformat(),
        coverage={"start_at": bars[0].start.isoformat(), "end_at": bars[-1].end.isoformat(), "bar_count": len(bars)},
        bars=[{"date": bar.end.date().isoformat() if interval == "1w" else bar.end.isoformat(),
               "start_at": bar.start.isoformat(), "end_at": bar.end.isoformat(), "complete": bar.complete,
               "open": bar.values.open_price, "high": bar.values.high_price, "low": bar.values.low_price,
               "close": bar.values.close_price, "volume": bar.values.volume_shares} for bar in bars],
        series=chart_series(bars, binding.request.indicators))
    if (len(bars) > 366 or len(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                        separators=(",", ":")).encode("utf-8")) > MAX_JSON_BYTES):
        raise ChartUnavailable("chart_payload_limit")
    return payload
