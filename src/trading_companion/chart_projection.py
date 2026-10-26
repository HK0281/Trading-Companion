"""Bounded chart DTOs from an existing, request-bound analysis; never fetch or calculate."""

from datetime import date, datetime
import json
from math import isfinite

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_contracts import (
    ALGORITHM_VERSIONS, ComponentMetadata, FieldResult, IndicatorResult, IndicatorRow,
    IndicatorSpec, MultiIndicatorRequest, MultiIndicatorResult,
)
from trading_companion.research_report import (
    FIELD_LABELS, build_research_report, indicator_field_units, indicator_label,
)


VERSION = "research-chart-v1"
VALIDATION_SCOPE = "request_bound_aligned_series_not_full_historical_evidence"
MAX_BARS = 366
MAX_INDICATORS = 12
MAX_SERIES = 48
MAX_JSON_BYTES = 256 * 1024
MAX_ABSOLUTE_VALUE = 10 ** 15
STATUSES = {"available", "insufficient_history", "undefined", "calculation_error"}
REASONS = {
    "available": "ready", "insufficient_history": "insufficient_records",
    "undefined": "zero_prior_volume_baseline", "calculation_error": "numeric_calculation_failed",
}


class _IntegrityError(ValueError):
    pass


class _LimitError(ValueError):
    pass


def _require(condition: bool) -> None:
    if not condition:
        raise _IntegrityError("Chart inputs are inconsistent")


def _number(value: object) -> bool:
    return (type(value) in (int, float) and abs(value) <= MAX_ABSOLUTE_VALUE
            and (type(value) is int or isfinite(value)))


def _timestamp(value: object) -> bool:
    return (type(value) is datetime and value.tzinfo is not None
            and value.utcoffset() is not None)


def _unavailable(code: str) -> dict[str, object]:
    # Do not echo exceptions, source paths, model output, or unvalidated identifiers.
    return {"version": VERSION, "status": "unavailable", "code": code}


def _validated_request(request: object) -> MultiIndicatorRequest:
    _require(type(request) is MultiIndicatorRequest)
    _require(type(request.indicators) is tuple and type(request.request_id) is str
             and type(request.instrument_id) is str and type(request.source) is str)
    _require(all(type(spec) is IndicatorSpec and type(spec.kind) is str
                 and all(_number(value) for value in spec.parameters.values())
                 for spec in request.indicators))
    # Frozen dataclasses still contain mutable data and can be tampered with internally.
    reconstructed = MultiIndicatorRequest(
        request.instrument_id, request.start_date, request.as_of_date,
        request.indicators, request.source, request.request_id,
    )
    _require(reconstructed == request)
    return reconstructed


def _required_bars(spec: IndicatorSpec, field: str) -> int:
    """Check record thresholds, without recalculating any numerical indicator."""
    parameters = spec.parameters
    if spec.kind == "macd":
        return parameters["slow"] + (0 if field == "dif" else parameters["signal"] - 1)
    if spec.kind == "obv":
        return 1
    if spec.kind == "adx":
        return 2 * parameters["window"] if field == "value" else parameters["window"] + 1
    if spec.kind == "volume":
        return (1 if field == "volume_shares" else parameters["window"]
                + (field in ("previous_volume_ma", "relative_volume")))
    return parameters["window"] + (spec.kind in ("rsi", "atr"))


def _bars(result: MultiIndicatorResult, request: MultiIndicatorRequest) -> list[dict[str, object]]:
    output = []
    previous = None
    for bar in result.bars:
        _require(type(bar) is DailyBar)
        DailyBar.__post_init__(bar)
        _require(type(bar.instrument_id) is str and bar.instrument_id == request.instrument_id
                 and type(bar.trade_date) is date
                 and request.start_date <= bar.trade_date <= request.as_of_date
                 and (previous is None or previous < bar.trade_date))
        _require(all(_number(value) for value in (
            bar.open_price, bar.high_price, bar.low_price, bar.close_price, bar.volume_shares)))
        _require(type(bar.volume_shares) is int)
        previous = bar.trade_date
        output.append({"date": bar.trade_date.isoformat(), "open": bar.open_price,
                       "high": bar.high_price, "low": bar.low_price,
                       "close": bar.close_price, "volume": bar.volume_shares})
    return output


def _series(result: MultiIndicatorResult, request: MultiIndicatorRequest) -> list[dict[str, object]]:
    by_id = {item.indicator_id: item for item in result.indicators}
    output = []
    for spec in request.indicators:
        indicator = by_id[spec.indicator_id]
        definitions = indicator_field_units(spec.kind)
        _require(type(indicator) is IndicatorResult and indicator.kind == spec.kind
                 and type(indicator.parameters) is dict and indicator.parameters == spec.parameters
                 and all(_number(value) and type(value) is type(spec.parameters[name])
                         for name, value in indicator.parameters.items())
                 and type(indicator.algorithm_version) is str
                 and indicator.algorithm_version == ALGORITHM_VERSIONS[spec.kind]
                 and type(indicator.series) is tuple and len(indicator.series) == len(result.bars))
        _require(type(indicator.components) is tuple
                 and all(type(item) is ComponentMetadata
                         and type(item.name) is str and type(item.start_effect_warning) is bool
                         for item in indicator.components))
        components = {item.name: item for item in indicator.components}
        expected_components = set(definitions) | ({"fast_ema", "slow_ema"} if spec.kind == "macd" else set())
        _require(len(components) == len(indicator.components) and set(components) == expected_components)
        values = {name: [] for name in definitions}
        statuses = {name: [] for name in definitions}
        for index, (row, bar) in enumerate(zip(indicator.series, result.bars)):
            _require(type(row) is IndicatorRow and type(row.trade_date) is date
                     and row.trade_date == bar.trade_date and type(row.fields) is dict
                     and row.fields.keys() == definitions.keys())
            for name, unit in definitions.items():
                field = row.fields[name]
                required = _required_bars(spec, name)
                _require(type(field) is FieldResult and type(field.trade_date) is date
                         and field.trade_date == bar.trade_date and field.unit == unit
                         and type(field.required_bars) is int and field.required_bars == required
                         and type(field.actual_bars) is int and field.actual_bars == index + 1
                         and type(field.missing_bars) is int
                         and field.missing_bars == max(0, required - index - 1)
                         and type(field.status) is str and field.status in STATUSES
                         and field.reason == ("zero_window_volume" if spec.kind == "vwma"
                             and field.status == "undefined" else REASONS[field.status]))
                if field.status == "available":
                    _require(_number(field.value) and field.missing_bars == 0
                             and (field.fact_id is None
                                  or type(field.fact_id) is str and bool(field.fact_id)))
                    if spec.kind in ("rsi", "adx"):
                        _require(0 <= field.value <= 100)
                    if spec.kind == "obv":
                        _require(type(field.value) is int)
                    if spec.kind in ("atr", "volume"):
                        _require(field.value >= 0)
                    if name == "volume_shares":
                        _require(type(field.value) is int and field.value == bar.volume_shares)
                else:
                    _require(field.value is None and field.fact_id is None)
                    if field.status == "insufficient_history":
                        _require(field.missing_bars > 0)
                    elif field.status == "undefined":
                        _require(field.missing_bars == 0)
                        if spec.kind == "vwma":
                            _require(name == "value" and all(
                                item.volume_shares == 0 for item in result.bars[index + 1 - required:index + 1]))
                        else:
                            _require(spec.kind == "volume" and name == "relative_volume")
                            baseline = row.fields["previous_volume_ma"]
                            _require(type(baseline) is FieldResult and baseline.status == "available"
                                     and baseline.value == 0)
                values[name].append(field.value)
                statuses[name].append(field.status)
        for name, unit in definitions.items():
            label = indicator_label(spec) + ("" if name == "value" else " " + FIELD_LABELS[name])
            output.append({"id": spec.indicator_id + ":" + name,
                           "indicator_id": spec.indicator_id, "kind": spec.kind,
                           "parameters": spec.parameters, "algorithm_version": indicator.algorithm_version,
                           "field": name, "label": label, "unit": unit,
                           "start_effect_warning": components[name].start_effect_warning,
                           "values": values[name], "statuses": statuses[name]})
    if len(output) > MAX_SERIES:
        raise _LimitError("Chart series count exceeds its bound")
    return output


def build_chart_projection(result: MultiIndicatorResult, *,
                           expected_request: MultiIndicatorRequest | None,
                           context: str = "current", saved_at: datetime | None = None) -> dict[str, object]:
    """Expose aligned chart data, not a full historical evidence or investment-quality claim."""
    try:
        if (type(context) is not str or context not in ("current", "saved")
                or context == "current" and saved_at is not None
                or context == "saved" and not _timestamp(saved_at)):
            return _unavailable("chart_context_invalid")
        request = _validated_request(expected_request)
        _require(type(result) is MultiIndicatorResult and type(result.bars) is tuple
                 and type(result.indicators) is tuple and type(result.source) is str
                 and type(result.price_adjustment) is str and type(result.data_authenticity) is str)
        if len(result.bars) > MAX_BARS or len(result.indicators) > MAX_INDICATORS:
            raise _LimitError("Chart input count exceeds its bound")
        report = build_research_report(result, expected_request=request)
        if report.status == "unavailable":
            return _unavailable("chart_data_unavailable")
        _require(report.status in ("complete", "limited"))
        payload = {
            "version": VERSION, "status": "available", "code": "aligned_research_series",
            "validation_scope": VALIDATION_SCOPE, "request_id": request.request_id,
            "instrument_id": request.instrument_id, "start_date": request.start_date.isoformat(),
            "as_of_date": request.as_of_date.isoformat(), "source": result.source,
            "price_adjustment": result.price_adjustment, "data_authenticity": result.data_authenticity,
            "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at is not None else None,
            "context": context, "saved_at": saved_at.isoformat() if saved_at is not None else None,
            "bars": _bars(result, request), "series": _series(result, request),
        }
        # Measure the entire compact UTF-8 DTO, not only its numerical arrays.
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_JSON_BYTES:
            raise _LimitError("Chart JSON exceeds its byte bound")
        return payload
    except _LimitError:
        return _unavailable("chart_payload_limit")
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError, RecursionError):
        return _unavailable("chart_integrity_mismatch")
