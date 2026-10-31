"""Identity-neutral weekly grouping; public callers retain their own type checks."""
from datetime import date, timedelta

from trading_companion._indicator_values import PriceValues


def group_weeks(bars, *, start_date, as_of_date):
    if type(start_date) is not date or type(as_of_date) is not date or start_date > as_of_date:
        raise ValueError('Valid date boundaries are required')
    groups = {}
    for bar in bars:
        if not start_date <= bar.trade_date <= as_of_date:
            continue
        monday = bar.trade_date - timedelta(days=bar.trade_date.weekday())
        if monday >= start_date:
            groups.setdefault(monday, []).append(bar)
    return tuple((monday, tuple(rows)) for monday, rows in groups.items())


def aggregate_prices(rows):
    return PriceValues(rows[0].open_price, max(row.high_price for row in rows),
                       min(row.low_price for row in rows), rows[-1].close_price)
