from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite

from trading_companion.daily_bar import DailyBar


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
class MacdValue:
    """One aligned MACD calculation; unavailable fields remain None."""

    dif: float | None
    dea: float | None
    histogram: float | None


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


def sma_close(bars: Sequence[DailyBar], window: int) -> list[float | None]:
    """Return a closing-price SMA aligned with the input bars."""
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")

    averages: list[float | None] = []
    running_total = 0.0
    instrument_id: str | None = None
    previous_date = None

    for index, bar in enumerate(bars):
        if instrument_id is None:
            instrument_id = bar.instrument_id
        elif bar.instrument_id != instrument_id:
            raise ValueError("bars must belong to one instrument")

        if previous_date is not None and bar.trade_date <= previous_date:
            raise ValueError("bars must have strictly increasing trade dates")
        previous_date = bar.trade_date

        running_total += bar.close_price
        if index >= window:
            running_total -= bars[index - window].close_price

        averages.append(running_total / window if index + 1 >= window else None)

    return averages


def sma_close_points(bars: Sequence[DailyBar], window: int) -> list[SmaPoint]:
    """Attach instrument and date information to closing-price SMA values."""
    averages = sma_close(bars, window)
    return [
        SmaPoint(bar.instrument_id, bar.trade_date, window, average)
        for bar, average in zip(bars, averages)
    ]


def _validate_window(window: int) -> None:
    """Reject booleans and non-positive periods before calculation."""
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")


def _close_values(bars: Sequence[DailyBar]) -> list[float]:
    """Validate one ordered instrument and extract closes without sorting."""
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes, bytearray)):
        raise ValueError("bars must be a sequence of DailyBar records")

    closes: list[float] = []
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
        closes.append(bar.close_price)

    return closes


def _ema_blend(previous: float, current: float, weight: float) -> float:
    """Blend finite signed values without overflowing their difference."""
    if weight == 1:
        return current
    difference = current - previous
    if isfinite(difference):
        value = previous + weight * difference
    else:
        # Opposite-signed finite values can have a non-finite difference.
        value = (1 - weight) * previous + weight * current
    if not isfinite(value):
        raise ValueError("EMA calculation must remain finite")
    return value


def _ema_values(values: Sequence[float], window: int) -> list[float | None]:
    """Smooth finite numbers, including negative values and zero, with an SMA seed."""
    _validate_window(window)
    averages: list[float | None] = []
    seed_average = 0.0
    previous_ema: float | None = None
    alpha = 2 / (window + 1)

    for index, raw_value in enumerate(values):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError("EMA values must be finite numbers")
        try:
            value = float(raw_value)
        except OverflowError:
            raise ValueError("EMA values must be finite numbers") from None
        if not isfinite(value):
            raise ValueError("EMA values must be finite numbers")

        if previous_ema is None:
            # Compute the seed mean incrementally to avoid overflowing a sum.
            seed_average = _ema_blend(seed_average, value, 1 / (index + 1))
            if index + 1 < window:
                averages.append(None)
                continue
            previous_ema = seed_average
        else:
            previous_ema = _ema_blend(previous_ema, value, alpha)

        averages.append(previous_ema)

    return averages


def ema_close(bars: Sequence[DailyBar], window: int) -> list[float | None]:
    """Return a closing-price EMA, seeded by the first window closes' mean.

    The first window - 1 values are unavailable. Subsequent values use
    alpha = 2 / (window + 1), without rounding or filling missing dates.
    """
    _validate_window(window)
    return _ema_values(_close_values(bars), window)


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
    """Return aligned DIF, SMA-seeded DEA, and 2 * (DIF - DEA)."""
    for window in (fast, slow, signal):
        _validate_window(window)
    if fast >= slow:
        raise ValueError("fast must be less than slow")

    # Both price EMAs use the same validated input, with no data requests.
    closes = _close_values(bars)
    fast_ema = _ema_values(closes, fast)
    slow_ema = _ema_values(closes, slow)
    dif_values = [
        fast_value - slow_value if fast_value is not None and slow_value is not None else None
        for fast_value, slow_value in zip(fast_ema, slow_ema)
    ]

    # Only the leading warm-up values are missing. Preserve valid zero DIFs.
    valid_dif = [value for value in dif_values if value is not None]
    unavailable_prefix = len(closes) - len(valid_dif)
    dea_values = [None] * unavailable_prefix + _ema_values(valid_dif, signal)

    results: list[MacdValue] = []
    for dif, dea in zip(dif_values, dea_values):
        histogram = None if dif is None or dea is None else 2 * (dif - dea)
        if histogram is not None and not isfinite(histogram):
            raise ValueError("MACD histogram calculation must remain finite")
        results.append(MacdValue(dif, dea, histogram))

    return results


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
