from dataclasses import dataclass
from datetime import date
from math import isfinite


@dataclass(frozen=True)
class DailyBar:
    """A completed daily OHLCV bar for one instrument."""

    instrument_id: str
    trade_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume_shares: int
    amount_cny: float

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("instrument_id must not be empty")
        if type(self.trade_date) is not date:
            raise ValueError("trade_date must be a date")

        prices = (
            self.open_price,
            self.high_price,
            self.low_price,
            self.close_price,
        )
        if any(
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not isfinite(price)
            or price <= 0
            for price in prices
        ):
            raise ValueError("OHLC prices must be finite and positive")
        if self.low_price > min(prices) or self.high_price < max(prices):
            raise ValueError("OHLC prices must fall between low_price and high_price")

        if isinstance(self.volume_shares, bool) or not isinstance(self.volume_shares, int):
            raise ValueError("volume_shares must be an integer")
        if self.volume_shares < 0:
            raise ValueError("volume_shares must not be negative")
        if (
            isinstance(self.amount_cny, bool)
            or not isinstance(self.amount_cny, (int, float))
            or not isfinite(self.amount_cny)
            or self.amount_cny < 0
        ):
            raise ValueError("amount_cny must be finite and non-negative")
