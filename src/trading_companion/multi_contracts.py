"""Validated requests and serializable contracts for one multi-indicator query."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from math import isfinite
import re
from typing import Any
from uuid import uuid4

from trading_companion.akshare_adapter import to_akshare_symbol
from trading_companion.daily_bar import DailyBar
from trading_companion.errors import InvalidRequestError
from trading_companion.tencent_adapter import to_tencent_symbol


PARAMETER_DEFAULTS = {
    "sma": {"window": 20}, "ema": {"window": 20},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "rsi": {"window": 14}, "boll": {"window": 20, "stddev_multiplier": 2.0},
    "atr": {"window": 14}, "volume": {"window": 20},
    "adx": {"window": 14}, "obv": {}, "vwma": {"window": 20},
}
ALGORITHM_VERSIONS = {
    "sma": "sma-close-v1", "ema": "ema-sma-seed-v1",
    "macd": "macd-sma-seed-double-histogram-v1", "rsi": "rsi-wilder-flat50-v1",
    "boll": "boll-population-ddof0-v1", "atr": "atr-wilder-prior-close-v1",
    "volume": "volume-prior-records-ratio-v1",
    "adx": "adx-wilder-double-smoothed-v1",
    "obv": "obv-snapshot-zero-seed-v1", "vwma": "vwma-close-volume-weighted-v1",
}


@dataclass(frozen=True, init=False)
class IndicatorSpec:
    """A canonical indicator instance; reject unknown parameters, never ignore them."""

    kind: str
    _parameters: tuple[tuple[str, int | float], ...]

    def __init__(self, kind: str, **parameters: int | float):
        if not isinstance(kind, str) or kind not in PARAMETER_DEFAULTS:
            raise InvalidRequestError("Unsupported indicator kind")
        defaults = PARAMETER_DEFAULTS[kind]
        if parameters.keys() - defaults.keys():
            raise InvalidRequestError("Unsupported indicator parameter")
        normalized = {**defaults, **parameters}
        for name, value in normalized.items():
            if name == "stddev_multiplier":
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not 0 < value <= 4):
                    raise InvalidRequestError("BOLL multiplier must be finite and in (0, 4]")
                normalized[name] = float(value)
            else:
                lower = 1 if kind in ("sma", "ema", "volume", "vwma") else 2
                upper = 60 if name == "signal" else 120
                if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                    raise InvalidRequestError("Indicator period is outside the product range")
        if kind == "macd" and normalized["fast"] >= normalized["slow"]:
            raise InvalidRequestError("MACD fast must be less than slow")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_parameters", tuple(normalized.items()))

    @property
    def parameters(self) -> dict[str, int | float]:
        return dict(self._parameters)

    @property
    def indicator_id(self) -> str:
        # repr preserves distinct finite float parameters; :g would truncate them.
        values = ",".join(f"{name}={value!r}" for name, value in self._parameters)
        return f"{self.kind}({values})"


DEFAULT_INDICATORS = (
    IndicatorSpec("sma", window=20), IndicatorSpec("sma", window=60),
    IndicatorSpec("ema"), IndicatorSpec("macd"), IndicatorSpec("rsi"),
    IndicatorSpec("boll"), IndicatorSpec("atr"), IndicatorSpec("volume"),
)


def validate_research_conditions(start_date: date, as_of_date: date,
                                 indicators: Sequence[IndicatorSpec]) -> tuple[IndicatorSpec, ...]:
    """Validate stock-independent conditions, including requests awaiting an identity."""
    if type(start_date) is not date or type(as_of_date) is not date:
        raise InvalidRequestError("Request boundaries must be dates")
    if not 1 <= (as_of_date - start_date).days + 1 <= 366:
        raise InvalidRequestError("Request range must contain between 1 and 366 calendar days")
    if (not isinstance(indicators, Sequence) or isinstance(indicators, (str, bytes)) or not indicators
            or any(type(spec) is not IndicatorSpec for spec in indicators)):
        raise InvalidRequestError("At least one validated indicator instance is required")
    canonical = []
    for spec in indicators:
        # Reconstruct so malformed trusted objects cannot bypass validation.
        validated = IndicatorSpec(spec.kind, **spec.parameters)
        if validated not in canonical:
            canonical.append(validated)
    if len(canonical) > 12:
        raise InvalidRequestError("At most 12 distinct indicator instances are allowed")
    return tuple(canonical)


@dataclass(frozen=True)
class MultiIndicatorRequest:
    instrument_id: str
    start_date: date
    as_of_date: date
    indicators: tuple[IndicatorSpec, ...] = DEFAULT_INDICATORS
    source: str = "tencent"
    request_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or re.fullmatch(
                r"[0-9]{6}\.(SH|SZ|BJ)", self.instrument_id) is None:
            raise InvalidRequestError("A complete SH/SZ/BJ instrument identifier is required")
        if self.source not in ("tencent", "akshare_eastmoney", "csv"):
            raise InvalidRequestError("Unsupported data source")
        if self.source == "tencent":
            to_tencent_symbol(self.instrument_id)
        else:
            to_akshare_symbol(self.instrument_id)
        object.__setattr__(self, "indicators", validate_research_conditions(
            self.start_date, self.as_of_date, self.indicators))
        if (not isinstance(self.request_id, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.request_id) is None):
            raise InvalidRequestError("request_id must be a bounded identifier")


@dataclass(frozen=True)
class FieldResult:
    trade_date: date
    status: str
    value: int | float | None
    reason: str
    unit: str
    required_bars: int
    actual_bars: int
    missing_bars: int
    fact_id: str | None = None


@dataclass(frozen=True)
class ComponentMetadata:
    name: str
    period: int
    required_bars: int
    calculation_start_date: date
    seed_method: str
    first_available_date: date | None
    recursive_updates: int
    related_periods: tuple[int, ...]
    start_effect_warning: bool
    warning_threshold_updates: int | None


@dataclass(frozen=True)
class IndicatorRow:
    trade_date: date
    fields: dict[str, FieldResult]


@dataclass(frozen=True)
class IndicatorResult:
    indicator_id: str
    kind: str
    parameters: dict[str, int | float]
    algorithm_version: str
    components: tuple[ComponentMetadata, ...]
    series: tuple[IndicatorRow, ...]

    @property
    def latest(self) -> dict[str, FieldResult]:
        return self.series[-1].fields


@dataclass(frozen=True)
class RelationResult:
    relation_id: str
    indicator_id: str
    field: str
    trade_date: date
    status: str
    value: str | None
    reason: str
    required_bars: int
    actual_bars: int
    missing_bars: int
    dependencies: tuple[str, ...]
    rule: str
    fact_id: str | None


@dataclass(frozen=True)
class Fact:
    fact_id: str
    request_id: str
    instrument_id: str
    trade_date: date
    indicator_id: str
    parameters: dict[str, int | float]
    field: str
    value: int | float | str
    unit: str
    dependencies: tuple[str, ...] = ()


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, IndicatorSpec):
        return {"kind": value.kind, "indicator_id": value.indicator_id,
                "parameters": value.parameters}
    if is_dataclass(value):
        result = {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, IndicatorResult):
            result["latest"] = _json_value(value.latest)
        return result
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("Serialized analysis values must remain finite")
    return value


@dataclass(frozen=True)
class MultiIndicatorResult:
    request: MultiIndicatorRequest
    data_status: str
    calculation_status: str
    reason: str
    message: str
    source: str
    price_adjustment: str
    data_authenticity: str
    retrieved_at: datetime | None
    bars: tuple[DailyBar, ...] = ()
    indicators: tuple[IndicatorResult, ...] = ()
    relations: tuple[RelationResult, ...] = ()
    facts: tuple[Fact, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def latest_trade_date(self) -> date | None:
        return self.bars[-1].trade_date if self.bars else None

    @property
    def synthesis_allowed(self) -> bool:
        return any(value.status == "available" for indicator in self.indicators
                   for name, value in indicator.latest.items() if name != "volume_shares")

    def to_dict(self) -> dict[str, object]:
        result = _json_value(self)
        result.update(latest_trade_date=_json_value(self.latest_trade_date),
                      bars_used=len(self.bars), synthesis_allowed=self.synthesis_allowed,
                      evidence_scope="latest_and_immediately_previous_record")
        return result
