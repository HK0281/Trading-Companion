from collections.abc import Iterable
from datetime import date

from trading_companion.daily_bar import DailyBar


def bars_as_of_close(bars: Iterable[DailyBar], as_of_date: date) -> list[DailyBar]:
    """Select bars through as_of_date for end-of-day analysis."""
    if type(as_of_date) is not date:
        raise ValueError("as_of_date must be a date")

    return [bar for bar in bars if bar.trade_date <= as_of_date]
