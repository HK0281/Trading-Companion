"""Bounded, fixed-origin Tencent m60 transport, exclusively for chart display."""

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
import json
import re
from time import monotonic
from urllib.parse import urlencode

from trading_companion._indicator_values import NumericBar
from trading_companion.chart_intervals import ChartBar, ChartUnavailable, SHANGHAI
from trading_companion.chart_projection import MAX_ABSOLUTE_VALUE
from trading_companion.tencent_adapter import to_tencent_symbol

HOST = "ifzq.gtimg.cn"
PATH = "/appstock/app/kline/mkline"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_BASE_BARS = 320
STARTS = {time(10, 30): time(9, 30), time(11, 30): time(10, 30),
          time(14): time(13), time(15): time(14)}


def remaining(deadline, clock):
    seconds = deadline - clock()
    if seconds <= 0:
        raise ChartUnavailable("chart_timeout")
    return seconds


def parse_tencent_hours(payload, instrument_id, *, now):
    """Reject an invalid whole batch before taking the saved research window."""
    try:
        symbol = to_tencent_symbol(instrument_id)
        if type(payload) is not dict or type(payload.get("code")) is not int or payload["code"] != 0:
            raise ValueError()
        rows = payload["data"][symbol]["m60"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_BASE_BARS or now.utcoffset() is None:
            raise ValueError()
        output, previous = [], None
        for row in rows:
            if type(row) is not list or len(row) != 8 or type(row[0]) is not str or not re.fullmatch(r"[0-9]{12}", row[0]):
                raise ValueError()
            end = datetime.strptime(row[0], "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
            if (end.time() not in STARTS or end.weekday() >= 5 or end > now
                    or previous is not None and end <= previous):
                raise ValueError()
            prices = []
            for value in row[1:5]:
                if type(value) not in (str, int, float):
                    raise ValueError()
                number = Decimal(str(value))
                if not number.is_finite() or not 0 < number <= MAX_ABSOLUTE_VALUE:
                    raise ValueError()
                prices.append(float(number))
            opening, close, high, low = prices
            if low > min(prices) or high < max(prices) or type(row[5]) not in (str, int, float):
                raise ValueError()
            volume = Decimal(str(row[5])) * (1 if symbol.startswith("sh688") else 100)
            if (not volume.is_finite() or not 0 <= volume <= MAX_ABSOLUTE_VALUE
                    or volume != volume.to_integral_value()):
                raise ValueError()
            output.append(ChartBar(datetime.combine(end.date(), STARTS[end.time()], SHANGHAI), end,
                NumericBar(opening, high, low, close, int(volume))))
            previous = end
        return tuple(output)
    except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation, OverflowError):
        raise ChartUnavailable("chart_hourly_invalid") from None


def read_tencent_hours(instrument_id, *, deadline, now, clock=monotonic, connection_factory=HTTPSConnection):
    """An eight-second cooperative budget, reset before every blocking read; no redirects/retries."""
    connection = response = None
    try:
        symbol = to_tencent_symbol(instrument_id)
        connection = connection_factory(HOST, timeout=remaining(deadline, clock))
        connection.connect()
        sock = connection.sock
        sock.settimeout(remaining(deadline, clock))
        connection.request("GET", PATH + "?" + urlencode({"param": f"{symbol},m60,,320"}),
                           headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        sock.settimeout(remaining(deadline, clock))
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise ChartUnavailable("chart_hourly_transport")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 7
                                   or int(length) > MAX_RESPONSE_BYTES):
            raise ChartUnavailable("chart_hourly_limit")
        content = bytearray()
        while True:
            sock.settimeout(remaining(deadline, clock))
            chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - len(content)))
            remaining(deadline, clock)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ChartUnavailable("chart_hourly_limit")
        payload = json.loads(content)
        bars = parse_tencent_hours(payload, instrument_id, now=now)
        remaining(deadline, clock)
        return bars
    except ChartUnavailable:
        raise
    except TimeoutError:
        raise ChartUnavailable("chart_timeout") from None
    except Exception:
        raise ChartUnavailable("chart_hourly_transport") from None
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
