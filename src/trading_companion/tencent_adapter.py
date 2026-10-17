import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import ClassVar

from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError


TENCENT_DAILY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
MAX_RECORDS = 640


def _shanghai_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def to_tencent_symbol(instrument_id: str) -> str:
    """Accept supported mainland equity prefixes, not funds or indices."""
    match = re.fullmatch(r"([0-9]{6})\.(SH|SZ)", instrument_id) if isinstance(instrument_id, str) else None
    if match is None:
        raise InvalidRequestError("Tencent requires a supported SH/SZ A-share identifier")
    code, exchange = match.groups()
    prefixes = {
        "SH": ("600", "601", "603", "605", "688"),
        "SZ": ("000", "001", "002", "003", "300", "301"),
    }
    if not code.startswith(prefixes[exchange]):
        raise InvalidRequestError("Unsupported Tencent equity prefix or exchange mismatch")
    return exchange.lower() + code


def tencent_row_to_daily_bar(row: list, instrument_id: str) -> DailyBar:
    """Normalize raw Tencent daily fields, never already-normalized SDK rows."""
    symbol = to_tencent_symbol(instrument_id)
    try:
        if not isinstance(row, list) or len(row) < 9:
            raise ValueError("daily row must contain at least nine fields")
        # Raw STAR volume is shares; main-board and ChiNext volume is hands.
        volume = Decimal(str(row[5])) * (1 if symbol.startswith("sh688") else 100)
        amount = Decimal(str(row[8])) * 10000
        if not volume.is_finite() or volume < 0 or volume != volume.to_integral_value():
            raise ValueError("volume must convert to a finite non-negative whole share count")
        if not amount.is_finite() or amount < 0:
            raise ValueError("amount must convert to finite non-negative CNY")
        if any(isinstance(row[index], bool) for index in (1, 2, 3, 4)):
            raise ValueError("prices must not be booleans")
        return DailyBar(
            instrument_id=instrument_id,
            trade_date=date.fromisoformat(row[0]),
            open_price=float(row[1]), high_price=float(row[3]),
            low_price=float(row[4]), close_price=float(row[2]),
            volume_shares=int(volume), amount_cny=float(amount),
        )
    except (TypeError, ValueError, InvalidOperation, OverflowError) as error:
        raise DataValidationError(f"Invalid Tencent daily row: {error}") from error


def _daily_rows(payload: object, symbol: str) -> list:
    """Require an explicit successful, unadjusted response for this symbol."""
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise DataValidationError("Tencent response is missing a numeric result code")
    if payload["code"] != 0:
        raise DataSourceError(f"Tencent returned error code {payload['code']} for {symbol}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DataValidationError("Tencent response has invalid data")
    entry = data.get(symbol)
    if not isinstance(entry, dict) or not isinstance(entry.get("day"), list):
        raise DataValidationError("Tencent response is missing the requested unadjusted daily series")
    return entry["day"]


@dataclass(frozen=True)
class TencentDailyBarSource:
    source_name: ClassVar[str] = "tencent"
    adjustment: ClassVar[str] = "unadjusted"
    instrument_id: str
    start_date: date
    end_date: date
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        to_tencent_symbol(self.instrument_id)
        if type(self.start_date) is not date or type(self.end_date) is not date:
            raise InvalidRequestError("start_date and end_date must be dates")
        if self.start_date > self.end_date:
            raise InvalidRequestError("start_date must not be after end_date")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise InvalidRequestError("timeout_seconds must be a finite positive number")

    def read_daily_bars(self) -> list[DailyBar]:
        """Fetch bounded yearly slices; fail the whole query if any slice fails."""
        if self.end_date >= _shanghai_today():
            raise InvalidRequestError("end_date must be before today in Shanghai; current-day bars are not supported")
        try:
            import requests
        except ImportError as error:
            raise DataSourceError("Install the data extra to use Tencent") from error

        symbol = to_tencent_symbol(self.instrument_id)
        bars: list[DailyBar] = []
        for year in range(self.start_date.year, self.end_date.year + 1):
            chunk_start = max(self.start_date, date(year, 1, 1))
            chunk_end = min(self.end_date, date(year, 12, 31))
            try:
                response = requests.get(
                    TENCENT_DAILY_URL,
                    params={"param": f"{symbol},day,{chunk_start.isoformat()},{chunk_end.isoformat()},{MAX_RECORDS},"},
                    timeout=self.timeout_seconds,
                )
                try:
                    response.raise_for_status()
                    payload = response.json()
                finally:
                    response.close()
            except requests.exceptions.JSONDecodeError as error:
                raise DataValidationError("Tencent returned invalid JSON") from error
            except requests.exceptions.RequestException as error:
                raise DataSourceError(
                    f"Tencent request failed for {self.instrument_id}; check the connection or retry later"
                ) from error

            raw_rows = _daily_rows(payload, symbol)
            dates: set[date] = set()
            for row in raw_rows:
                # Tencent may ignore the lower date bound and return older bars.
                try:
                    if not isinstance(row, list) or len(row) < 9:
                        raise ValueError("daily row must contain at least nine fields")
                    trade_date = date.fromisoformat(row[0])
                except (TypeError, ValueError) as error:
                    raise DataValidationError(f"Invalid Tencent daily record: {error}") from error
                if trade_date in dates:
                    raise DataValidationError(f"Tencent returned a duplicate bar for {trade_date}")
                dates.add(trade_date)
                if trade_date > chunk_end:
                    raise DataValidationError("Tencent returned a bar after the requested end date")
                if trade_date >= chunk_start:
                    bars.append(tencent_row_to_daily_bar(row, self.instrument_id))

        bars.sort(key=lambda bar: bar.trade_date)
        return bars
