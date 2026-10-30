"""Pure ordered OHLCV arithmetic; callers validate identity and real time separately."""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from math import isfinite
from statistics import mean, pstdev
from typing import Literal

@dataclass(frozen=True)
class NumericBar:
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume_shares: int

RelativeVolumeStatus = Literal["available", "insufficient_history", "undefined"]

@dataclass(frozen=True)
class MacdValue:
    """One aligned MACD calculation; unavailable fields remain None."""

    dif: float | None
    dea: float | None
    histogram: float | None


@dataclass(frozen=True)
class BollValue:
    """Population-standard-deviation bands aligned with one input bar."""

    middle: float | None
    upper: float | None
    lower: float | None


@dataclass(frozen=True)
class VolumeValue:
    """Current volume, inclusive MA, and a strictly prior-window baseline."""

    volume_shares: int
    volume_ma: float | None
    previous_volume_ma: float | None
    relative_volume: float | None
    relative_volume_status: RelativeVolumeStatus


@dataclass(frozen=True)
class AdxValue:
    """Wilder ADX and directional indices aligned with one input record."""

    value: float | None
    plus_di: float | None
    minus_di: float | None


@dataclass(frozen=True)
class VwmaValue:
    """Volume-weighted close; a complete zero-volume window is undefined."""

    value: float | None
    status: RelativeVolumeStatus


def sma_close(bars: Sequence[NumericBar], window: int) -> list[float | None]:
    """Return a closing-price SMA aligned with the input bars."""
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")

    averages: list[float | None] = []
    running_total = 0.0

    for index, bar in enumerate(bars):
        running_total += bar.close_price
        if index >= window:
            running_total -= bars[index - window].close_price

        averages.append(running_total / window if index + 1 >= window else None)

    return averages


def _validate_window(window: int) -> None:
    """Reject booleans and non-positive periods before calculation."""
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")


def _close_values(bars: Sequence[NumericBar]) -> list[float]:
    """Validate one ordered instrument and extract closes without sorting."""
    closes = [bar.close_price for bar in bars]

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


def ema_close(bars: Sequence[NumericBar], window: int) -> list[float | None]:
    """Return a closing-price EMA, seeded by the first window closes' mean.

    The first window - 1 values are unavailable. Subsequent values use
    alpha = 2 / (window + 1), without rounding or filling missing dates.
    """
    _validate_window(window)
    return _ema_values(_close_values(bars), window)


def macd_close(
    bars: Sequence[NumericBar], fast: int = 12, slow: int = 26, signal: int = 9,
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


def _wilder_values(values: Sequence[float], window: int) -> list[float | None]:
    """Seed with n non-negative observations, then smooth with weight 1/n."""
    _validate_window(window)
    results: list[float | None] = []
    seed = 0.0
    previous: float | None = None
    for index, value in enumerate(values):
        if not isfinite(value) or value < 0:
            raise ValueError("Wilder values must be finite and non-negative")
        if previous is None:
            seed = _ema_blend(seed, value, 1 / (index + 1))
            if index + 1 < window:
                results.append(None)
                continue
            previous = seed
        else:
            previous = _ema_blend(previous, value, 1 / window)
        results.append(previous)
    return results


def rsi_close(bars: Sequence[NumericBar], window: int = 14) -> list[float | None]:
    """Return Wilder RSI; n changes require n + 1 closing-price records.

    A flat seed is 50; gains without losses yield 100, and the reverse is 0.
    """
    _validate_window(window)
    closes = _close_values(bars)
    if not closes:
        return []
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    gains = _wilder_values([max(change, 0.0) for change in changes], window)
    losses = _wilder_values([max(-change, 0.0) for change in changes], window)
    results: list[float | None] = [None]
    for gain, loss in zip(gains, losses):
        if gain is None or loss is None:
            results.append(None)
        elif gain == 0 and loss == 0:
            results.append(50.0)
        else:
            # Scaling avoids overflow in gain + loss and loss/gain ratios.
            scale = max(gain, loss)
            scaled_gain, scaled_loss = gain / scale, loss / scale
            results.append(100 * scaled_gain / (scaled_gain + scaled_loss))
    return results


def _boll_multiplier(value: float) -> float:
    """Keep product-level limits separate from finite-positive validation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stddev_multiplier must be finite and positive")
    try:
        multiplier = float(value)
    except OverflowError:
        raise ValueError("stddev_multiplier must be finite and positive") from None
    if not isfinite(multiplier) or multiplier <= 0:
        raise ValueError("stddev_multiplier must be finite and positive")
    return multiplier


def boll_close(
    bars: Sequence[NumericBar], window: int = 20, stddev_multiplier: float = 2.0,
) -> list[BollValue]:
    """Return SMA-centered bands using population standard deviation (ddof=0)."""
    _validate_window(window)
    multiplier = _boll_multiplier(stddev_multiplier)
    closes = _close_values(bars)
    results: list[BollValue] = []
    for index in range(len(closes)):
        if index + 1 < window:
            results.append(BollValue(None, None, None))
            continue
        sample = closes[index + 1 - window:index + 1]
        # Recompute each window to avoid cancellation in rolling sum-of-squares.
        middle = mean(sample)
        width = multiplier * pstdev(sample)
        upper, lower = middle + width, middle - width
        if not all(isfinite(value) for value in (middle, upper, lower)):
            raise ValueError("BOLL calculation must remain finite")
        results.append(BollValue(middle, upper, lower))
    return results


def atr(bars: Sequence[NumericBar], window: int = 14) -> list[float | None]:
    """Return Wilder ATR; the first bar has no TR without a previous close."""
    _validate_window(window)
    if not bars:
        return []
    true_ranges = [
        max(current.high_price - current.low_price,
            abs(current.high_price - previous.close_price),
            abs(current.low_price - previous.close_price))
        for previous, current in zip(bars, bars[1:])
    ]
    return [None, *_wilder_values(true_ranges, window)]


def _finite_ratio(numerator: int, denominator: int) -> float:
    """Convert exact integer volume arithmetic to a finite result."""
    try:
        value = numerator / denominator
    except OverflowError:
        raise ValueError("Volume calculation must remain finite") from None
    if not isfinite(value):
        raise ValueError("Volume calculation must remain finite")
    return value


def volume_activity(bars: Sequence[NumericBar], window: int = 20) -> list[VolumeValue]:
    """Return inclusive volume MA and current/prior-n-records volume ratio.

    The ratio needs n + 1 records. A zero prior baseline is undefined,
    while zero current volume with a positive baseline is an available 0.
    """
    _validate_window(window)
    results: list[VolumeValue] = []
    running_total = 0
    for index, bar in enumerate(bars):
        previous_ma = None
        relative = None
        status: RelativeVolumeStatus = "insufficient_history"
        if index >= window:
            previous_ma = _finite_ratio(running_total, window)
            if running_total == 0:
                status = "undefined"
            else:
                relative = _finite_ratio(bar.volume_shares * window, running_total)
                status = "available"
        running_total += bar.volume_shares
        if index >= window:
            running_total -= bars[index - window].volume_shares
        volume_ma = _finite_ratio(running_total, window) if index + 1 >= window else None
        results.append(VolumeValue(bar.volume_shares, volume_ma, previous_ma, relative, status))
    return results


def adx(bars: Sequence[NumericBar], window: int = 14) -> list[AdxValue]:
    """Return Wilder ADX, +DI and -DI using complete change windows.

    The first record has no previous OHLC. DI requires n changes (n + 1
    records); ADX seeds from n available DX values (2n records). A zero
    true-range baseline yields zero directional indices and DX, so a
    completely flat market has ADX zero rather than a fabricated direction.
    """
    _validate_window(window)
    if not bars:
        return []
    true_ranges, positive, negative = [], [], []
    for previous, current in zip(bars, bars[1:]):
        upward = current.high_price - previous.high_price
        downward = previous.low_price - current.low_price
        true_ranges.append(max(current.high_price - current.low_price,
                               abs(current.high_price - previous.close_price),
                               abs(current.low_price - previous.close_price)))
        positive.append(upward if upward > 0 and upward > downward else 0.0)
        negative.append(downward if downward > 0 and downward > upward else 0.0)
    ranges = _wilder_values(true_ranges, window)
    up_values = _wilder_values(positive, window)
    down_values = _wilder_values(negative, window)
    directional = []
    dx_values = []
    for true_range, up, down in zip(ranges, up_values, down_values):
        if true_range is None:
            directional.append((None, None))
            continue
        plus_di = 100 * (up / true_range) if true_range else 0.0
        minus_di = 100 * (down / true_range) if true_range else 0.0
        total = plus_di + minus_di
        dx = 100 * (abs(plus_di - minus_di) / total) if total else 0.0
        if not all(isfinite(value) for value in (plus_di, minus_di, dx)):
            raise ValueError("ADX calculation must remain finite")
        directional.append((plus_di, minus_di))
        dx_values.append(dx)
    smoothed_dx = _wilder_values(dx_values, window)
    # The first valid DX is at source index n, after the first n changes.
    adx_values = [None] * min(window, len(bars)) + smoothed_dx
    di_values = [(None, None), *directional]
    return [AdxValue(value, plus_di, minus_di)
            for value, (plus_di, minus_di) in zip(adx_values, di_values)]


def obv(bars: Sequence[NumericBar]) -> list[int]:
    """Accumulate signed share counts from a zero first-record snapshot anchor."""
    values = []
    current = 0
    previous_close = None
    for bar in bars:
        if previous_close is not None:
            if bar.close_price > previous_close:
                current += bar.volume_shares
            elif bar.close_price < previous_close:
                current -= bar.volume_shares
        values.append(current)
        previous_close = bar.close_price
    return values


def vwma(bars: Sequence[NumericBar], window: int = 20) -> list[VwmaValue]:
    """Return full-window volume-weighted closes without float volume conversion.

    Decimal weighting avoids overflowing price-times-volume products and
    preserves small weights whose large prices can still matter to the mean.
    """
    _validate_window(window)
    results = []
    for index in range(len(bars)):
        if index + 1 < window:
            results.append(VwmaValue(None, "insufficient_history"))
            continue
        sample = bars[index + 1 - window:index + 1]
        total = sum(bar.volume_shares for bar in sample)
        if not total:
            results.append(VwmaValue(None, "undefined"))
            continue
        with localcontext() as context:
            context.prec = 40
            weighted = sum((Decimal(str(bar.close_price)) * bar.volume_shares
                            for bar in sample if bar.volume_shares), Decimal(0))
            average = float(weighted / total)
        if not isfinite(average):
            raise ValueError("VWMA calculation must remain finite")
        results.append(VwmaValue(average, "available"))
    return results
