"""Bounded Tencent daily transport for explicit, read-only research reviews."""

from datetime import date, datetime, timedelta, timezone
from http.client import HTTPSConnection
import json
from math import isfinite
import re
from time import monotonic
from urllib.parse import urlencode

from trading_companion.chart_projection import MAX_ABSOLUTE_VALUE
from trading_companion.tencent_adapter import (
    MAX_RECORDS, _daily_rows, tencent_row_to_daily_bar, to_tencent_symbol,
)


HOST = "proxy.finance.qq.com"
PATH = "/ifzqgtimg/appstock/app/newfqkline/get"
SHANGHAI = timezone(timedelta(hours=8))
MAX_RESPONSE_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_DAYS = 366
MAX_ATTEMPTS = 2
TOTAL_SECONDS = 8.0
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ReviewPriceError(ValueError):
    """Expose fixed codes only, never transport errors or provider payloads."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Budget:
    def __init__(self, deadline, cancelled, clock):
        self.cancelled, self.clock = cancelled, clock
        started = clock()
        if type(started) not in (int, float) or not isfinite(started):
            raise ReviewPriceError("review_prices_invalid_request")
        self.deadline = min(deadline, started + TOTAL_SECONDS)
        self.previous = started

    def remaining(self):
        if self.cancelled():
            raise ReviewPriceError("review_prices_cancelled")
        stamp = self.clock()
        if type(stamp) not in (int, float) or not isfinite(stamp) or stamp < self.previous:
            raise ReviewPriceError("review_prices_invalid_request")
        self.previous = stamp
        remaining = self.deadline - stamp
        if remaining <= 0:
            raise ReviewPriceError("review_prices_timeout")
        return remaining


def _read_slice(symbol, start_date, end_date, *, budget, connection_factory):
    connection = response = None
    try:
        connection = connection_factory(HOST, timeout=budget.remaining())
        connection.timeout = budget.remaining()
        connection.connect()
        sock = connection.sock
        sock.settimeout(budget.remaining())
        connection.request("GET", PATH + "?" + urlencode({
            "param": f"{symbol},day,{start_date.isoformat()},{end_date.isoformat()},{MAX_RECORDS},",
        }), headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        sock.settimeout(budget.remaining())
        response = connection.getresponse()
        budget.remaining()
        encoding = response.getheader("Content-Encoding", "identity")
        if (type(response.status) is not int or response.status != 200
                or type(encoding) is not str or encoding.strip().lower() != "identity"):
            raise ReviewPriceError("review_prices_transport")
        length = response.getheader("Content-Length")
        if length is not None and (type(length) is not str or not length.isascii()
                or not length.isdigit() or len(length) > 7 or int(length) > MAX_RESPONSE_BYTES):
            raise ReviewPriceError("review_prices_limit")
        content = bytearray()
        while True:
            sock.settimeout(budget.remaining())
            chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - len(content)))
            budget.remaining()
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ReviewPriceError("review_prices_limit")
        if length is not None and len(content) != int(length):
            raise ReviewPriceError("review_prices_invalid_data")
        return bytes(content)
    finally:
        # Always attempt both closes, including when a response close itself fails.
        try:
            if response is not None:
                response.close()
        finally:
            if connection is not None:
                connection.close()


def _parse_slice(content, instrument_id, symbol, start_date, end_date, budget):
    try:
        rows = _daily_rows(json.loads(content), symbol)
        if len(rows) > MAX_RECORDS:
            raise ReviewPriceError("review_prices_limit")
        output, previous = [], None
        for row in rows:
            budget.remaining()
            if (type(row) is not list or len(row) < 9 or type(row[0]) is not str
                    or _DATE.fullmatch(row[0]) is None):
                raise ValueError()
            bar = tencent_row_to_daily_bar(row, instrument_id)
            if ((previous is not None and bar.trade_date <= previous) or bar.trade_date > end_date
                    or any(value > MAX_ABSOLUTE_VALUE for value in (
                        bar.open_price, bar.high_price, bar.low_price, bar.close_price))):
                raise ValueError()
            previous = bar.trade_date
            # The provider can ignore its lower date bound. Validate every row,
            # then discard earlier bars without fabricating missing trading days.
            if bar.trade_date >= start_date:
                output.append({"date": bar.trade_date.isoformat(), "close": bar.close_price})
        return output
    except ReviewPriceError:
        raise
    except Exception:
        raise ReviewPriceError("review_prices_invalid_data") from None


def read_tencent_review_days(instrument_id, *, start_date: date, end_date: date,
                             now: datetime, deadline: float, cancelled=lambda: False,
                             clock=monotonic, connection_factory=HTTPSConnection) -> list[dict]:
    """Read up to two yearly slices within an eight-second cooperative budget.

    The caller derives the dates and original-price eligibility from the saved
    research. This transport accepts no current-day bars, retries or redirects.
    Cancellation/deadlines are checked between blocking operations; they cannot
    forcibly interrupt a call already in progress.
    """
    try:
        if (type(start_date) is not date or type(end_date) is not date or start_date > end_date
                or (end_date - start_date).days + 1 > MAX_DAYS
                or end_date.year - start_date.year + 1 > MAX_ATTEMPTS
                or type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None
                or end_date >= now.astimezone(SHANGHAI).date()
                or type(deadline) not in (int, float) or not isfinite(deadline)
                or not all(callable(value) for value in (cancelled, clock, connection_factory))):
            raise ReviewPriceError("review_prices_invalid_request")
        try:
            symbol = to_tencent_symbol(instrument_id)
        except Exception:
            raise ReviewPriceError("review_prices_instrument_unsupported") from None
        budget = _Budget(deadline, cancelled, clock)
        output = []
        for year in range(start_date.year, end_date.year + 1):
            chunk_start = max(start_date, date(year, 1, 1))
            chunk_end = min(end_date, date(year, 12, 31))
            content = _read_slice(symbol, chunk_start, chunk_end,
                                  budget=budget, connection_factory=connection_factory)
            budget.remaining()
            output.extend(_parse_slice(content, instrument_id, symbol, chunk_start, chunk_end, budget))
        if (len(output) > MAX_DAYS or len(json.dumps(output, allow_nan=False).encode()) > MAX_OUTPUT_BYTES):
            raise ReviewPriceError("review_prices_limit")
        budget.remaining()
        return output
    except ReviewPriceError:
        raise
    except TimeoutError:
        raise ReviewPriceError("review_prices_timeout") from None
    except Exception:
        raise ReviewPriceError("review_prices_transport") from None
