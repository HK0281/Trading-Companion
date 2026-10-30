"""Validated daily-bar entry points over the shared numerical kernel."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from trading_companion.daily_bar import DailyBar
from trading_companion import _indicator_values as _values
from trading_companion._indicator_values import (
    MacdValue, BollValue, VolumeValue, AdxValue, VwmaValue, RelativeVolumeStatus,
    _ema_values,
)

@dataclass(frozen=True)
class SmaPoint:
    """One SMA result tied to its instrument and trade date."""

    instrument_id: str
    trade_date: date
    window: int
    value: float | None


@dataclass(frozen=True)
class EmaPoint:
    """One SMA-seeded EMA result tied to its instrument and trade date."""

    instrument_id: str
    trade_date: date
    window: int
    value: float | None


@dataclass(frozen=True)
class MacdPoint:
    """One MACD result tied to its instrument, date, and periods."""

    instrument_id: str
    trade_date: date
    fast: int
    slow: int
    signal: int
    dif: float | None
    dea: float | None
    histogram: float | None


@dataclass(frozen=True)
class RsiPoint:
    """One Wilder-smoothed RSI tied to its instrument and trade date."""

    instrument_id: str
    trade_date: date
    window: int
    value: float | None


@dataclass(frozen=True)
class BollPoint:
    """One set of Bollinger bands with its date and calculation parameters."""

    instrument_id: str
    trade_date: date
    window: int
    stddev_multiplier: float
    middle: float | None
    upper: float | None
    lower: float | None


@dataclass(frozen=True)
class AtrPoint:
    """One Wilder-smoothed ATR in price units, not percentage units."""

    instrument_id: str
    trade_date: date
    window: int
    value: float | None


@dataclass(frozen=True)
class VolumePoint:
    """Volume activity tied to its instrument, date, and record window."""

    instrument_id: str
    trade_date: date
    window: int
    volume_shares: int
    volume_ma: float | None
    previous_volume_ma: float | None
    relative_volume: float | None
    relative_volume_status: RelativeVolumeStatus


def sma_close(bars: Sequence[DailyBar], window: int) -> list[float | None]:
    _validate_bars(bars)
    return _values.sma_close(bars, window)


def sma_close_points(bars: Sequence[DailyBar], window: int) -> list[SmaPoint]:
    """Attach instrument and date information to closing-price SMA values."""
    averages = sma_close(bars, window)
    return [
        SmaPoint(bar.instrument_id, bar.trade_date, window, average)
        for bar, average in zip(bars, averages)
    ]


def _validate_bars(bars: Sequence[DailyBar]) -> None:
    """Validate one ordered instrument without sorting or filling dates."""
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes, bytearray)):
        raise ValueError("bars must be a sequence of DailyBar records")

    instrument_id: str | None = None
    previous_date = None

    for bar in bars:
        if not isinstance(bar, DailyBar):
            raise ValueError("bars must contain DailyBar records")
        if instrument_id is None:
            instrument_id = bar.instrument_id
        elif bar.instrument_id != instrument_id:
            raise ValueError("bars must belong to one instrument")

        if previous_date is not None and bar.trade_date <= previous_date:
            raise ValueError("bars must have strictly increasing trade dates")
        previous_date = bar.trade_date


def ema_close(bars: Sequence[DailyBar], window: int) -> list[float | None]:
    _validate_bars(bars)
    return _values.ema_close(bars, window)


def ema_close_points(bars: Sequence[DailyBar], window: int) -> list[EmaPoint]:
    """Attach instrument and date information to closing-price EMA values."""
    averages = ema_close(bars, window)
    return [
        EmaPoint(bar.instrument_id, bar.trade_date, window, average)
        for bar, average in zip(bars, averages)
    ]


def macd_close(
    bars: Sequence[DailyBar], fast: int = 12, slow: int = 26, signal: int = 9,
) -> list[MacdValue]:
    _validate_bars(bars)
    return _values.macd_close(bars, fast, slow, signal)


def macd_close_points(
    bars: Sequence[DailyBar], fast: int = 12, slow: int = 26, signal: int = 9,
) -> list[MacdPoint]:
    """Attach instrument, dates, and periods to aligned MACD values."""
    values = macd_close(bars, fast, slow, signal)
    return [
        MacdPoint(bar.instrument_id, bar.trade_date, fast, slow, signal,
                  value.dif, value.dea, value.histogram)
        for bar, value in zip(bars, values)
    ]


def rsi_close(bars: Sequence[DailyBar], window: int = 14) -> list[float | None]:
    _validate_bars(bars)
    return _values.rsi_close(bars, window)


def rsi_close_points(bars: Sequence[DailyBar], window: int = 14) -> list[RsiPoint]:
    """Attach instrument, date, and period to aligned RSI values."""
    return [
        RsiPoint(bar.instrument_id, bar.trade_date, window, value)
        for bar, value in zip(bars, rsi_close(bars, window))
    ]


def boll_close(
    bars: Sequence[DailyBar], window: int = 20, stddev_multiplier: float = 2.0,
) -> list[BollValue]:
    _validate_bars(bars)
    return _values.boll_close(bars, window, stddev_multiplier)


def boll_close_points(
    bars: Sequence[DailyBar], window: int = 20, stddev_multiplier: float = 2.0,
) -> list[BollPoint]:
    """Attach instrument, dates, and parameters to aligned Bollinger bands."""
    values = boll_close(bars, window, stddev_multiplier)
    return [
        BollPoint(bar.instrument_id, bar.trade_date, window, float(stddev_multiplier),
                  value.middle, value.upper, value.lower)
        for bar, value in zip(bars, values)
    ]


def atr(bars: Sequence[DailyBar], window: int = 14) -> list[float | None]:
    _validate_bars(bars)
    return _values.atr(bars, window)


def atr_points(bars: Sequence[DailyBar], window: int = 14) -> list[AtrPoint]:
    """Attach instrument, date, and period to ATR in CNY per share."""
    return [
        AtrPoint(bar.instrument_id, bar.trade_date, window, value)
        for bar, value in zip(bars, atr(bars, window))
    ]


def volume_activity(bars: Sequence[DailyBar], window: int = 20) -> list[VolumeValue]:
    _validate_bars(bars)
    return _values.volume_activity(bars, window)


def volume_activity_points(bars: Sequence[DailyBar], window: int = 20) -> list[VolumePoint]:
    """Attach instrument, date, and period to volume fields and ratio status."""
    return [
        VolumePoint(bar.instrument_id, bar.trade_date, window, value.volume_shares,
                    value.volume_ma, value.previous_volume_ma, value.relative_volume,
                    value.relative_volume_status)
        for bar, value in zip(bars, volume_activity(bars, window))
    ]


def adx(bars: Sequence[DailyBar], window: int = 14) -> list[AdxValue]:
    _validate_bars(bars)
    return _values.adx(bars, window)


def obv(bars: Sequence[DailyBar]) -> list[int]:
    _validate_bars(bars)
    return _values.obv(bars)


def vwma(bars: Sequence[DailyBar], window: int = 20) -> list[VwmaValue]:
    _validate_bars(bars)
    return _values.vwma(bars, window)
