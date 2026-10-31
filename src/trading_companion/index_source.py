"""Fixed-origin Tencent index daily reads, without equity quantity conversions."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.index_contracts import IndexBar, identity
from trading_companion.tencent_adapter import TENCENT_DAILY_URL, MAX_RECORDS, _daily_rows, _shanghai_today
from trading_companion.turn_control import checkpoint, request_timeout

MAX_BYTES = 512 * 1024


def parse_daily(payload, instrument_id, start, end):
    rows = _daily_rows(payload, identity(instrument_id).symbol)
    if len(rows) > MAX_RECORDS:
        raise DataValidationError('Index source row limit exceeded')
    output, seen, previous = [], set(), None
    for row in rows:
        try:
            if type(row) is not list or len(row) < 5 or type(row[0]) is not str:
                raise ValueError()
            day = date.fromisoformat(row[0])
            if day in seen or day > end or previous is not None and day <= previous:
                raise ValueError()
            seen.add(day)
            previous = day
            if day < start:
                continue
            if any(type(v) not in (str, int, float) for v in row[1:5]):
                raise ValueError()
            output.append(IndexBar(instrument_id, day, float(row[1]), float(row[3]), float(row[4]), float(row[2])))
        except (TypeError, ValueError, OverflowError):
            raise DataValidationError('Invalid index daily row') from None
    return output


@dataclass(frozen=True)
class TencentIndexSource:
    request: object

    def read_daily_bars(self):
        import json
        import requests
        request = self.request
        request.__post_init__()
        if request.source != 'tencent' or request.as_of_date >= _shanghai_today():
            raise InvalidRequestError('Index daily cutoff must precede Shanghai today')
        output = []
        for year in range(request.start_date.year, request.as_of_date.year + 1):
            checkpoint()
            start, end = max(request.start_date, date(year, 1, 1)), min(request.as_of_date, date(year, 12, 31))
            try:
                response = requests.get(TENCENT_DAILY_URL, params={'param':
                    f'{request.index.symbol},day,{start.isoformat()},{end.isoformat()},{MAX_RECORDS},'},
                    timeout=request_timeout(15), stream=True, allow_redirects=False)
                try:
                    if response.status_code != 200:
                        raise DataSourceError('Index daily source unavailable')
                    raw = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        checkpoint()
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise DataValidationError('Index response limit exceeded')
                    payload = json.loads(raw)
                finally:
                    response.close()
            except requests.RequestException:
                raise DataSourceError('Index daily source unavailable') from None
            except (ValueError, UnicodeError):
                raise DataValidationError('Invalid index source JSON') from None
            checkpoint()
            output.extend(parse_daily(payload, request.instrument_id, start, end))
        return output


def synthetic_index_bars(request):
    """Independent, explicitly artificial index fixture; never reads an equity CSV."""
    output, day = [], request.start_date
    while day <= request.as_of_date:
        if day.weekday() < 5:
            price = 1000 + (day - date(2020, 1, 1)).days * .2 + (day.toordinal() % 9)
            output.append(IndexBar(request.instrument_id, day, price, price + 9, price - 8, price + 2))
        day += timedelta(days=1)
    return output
