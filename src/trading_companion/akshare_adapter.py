import re
from math import isfinite
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError


SHARES_PER_HAND = 100
REQUIRED_COLUMNS = {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"}


def _shanghai_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def to_akshare_symbol(instrument_id: str) -> str:
    """Convert a supported A-share identifier to AKShare's six-digit symbol."""
    if not isinstance(instrument_id, str):
        raise InvalidRequestError("instrument_id must look like 000001.SZ")
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", instrument_id)
    if match is None:
        raise InvalidRequestError("instrument_id must look like 000001.SZ")

    code, exchange = match.groups()
    prefixes = {"SH": ("6",), "SZ": ("0", "3"), "BJ": ("4", "8", "9")}
    if not code.startswith(prefixes[exchange]):
        raise InvalidRequestError("instrument_id code does not match its exchange")
    return code


def akshare_row_to_daily_bar(
    row: Mapping[str, object], instrument_id: str
) -> DailyBar:
    """Convert an unadjusted AKShare row; volume hands become shares."""
    to_akshare_symbol(instrument_id)

    try:
        raw_date = row["日期"]
        if isinstance(raw_date, datetime):
            trade_date = raw_date.date()
        elif isinstance(raw_date, date):
            trade_date = raw_date
        else:
            trade_date = date.fromisoformat(str(raw_date))

        volume_shares = Decimal(str(row["成交量"])) * SHARES_PER_HAND
        if volume_shares != volume_shares.to_integral_value():
            raise ValueError("成交量 must convert to a whole number of shares")

        return DailyBar(
            instrument_id=instrument_id,
            trade_date=trade_date,
            open_price=float(row["开盘"]),
            high_price=float(row["最高"]),
            low_price=float(row["最低"]),
            close_price=float(row["收盘"]),
            volume_shares=int(volume_shares),
            amount_cny=float(row["成交额"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as error:
        raise DataValidationError(f"Invalid AKShare daily row: {error}") from error


@dataclass(frozen=True)
class AkshareDailyBarSource:
    source_name: ClassVar[str] = "akshare_eastmoney"
    adjustment: ClassVar[str] = "unadjusted"
    instrument_id: str
    start_date: date
    end_date: date
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        to_akshare_symbol(self.instrument_id)
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
        """Fetch unadjusted daily bars and convert them to canonical fields."""
        # Temporary conservative boundary until exchange session handling exists.
        if self.end_date >= _shanghai_today():
            raise InvalidRequestError("end_date must be before today in Shanghai; current-day bars are not supported")
        try:
            import akshare
            from requests.exceptions import JSONDecodeError, RequestException
        except ImportError as error:
            raise DataSourceError("Install the data extra to use AKShare") from error

        try:
            # This bounds connection/read waits, not the total request duration.
            frame = akshare.stock_zh_a_hist(
                symbol=to_akshare_symbol(self.instrument_id),
                period="daily",
                start_date=self.start_date.strftime("%Y%m%d"),
                end_date=self.end_date.strftime("%Y%m%d"),
                adjust="",
                timeout=self.timeout_seconds,
            )
        except JSONDecodeError as error:
            raise DataValidationError("AKShare returned invalid JSON") from error
        except RequestException as error:
            raise DataSourceError(
                f"AKShare request failed for {self.instrument_id}; check the connection or retry later"
            ) from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise DataValidationError("AKShare returned an invalid response") from error

        try:
            columns = list(frame.columns)
            records = frame.to_dict(orient="records")
        except (AttributeError, TypeError, ValueError) as error:
            raise DataValidationError("AKShare returned an invalid table") from error
        if not isinstance(records, list) or any(
            not isinstance(row, Mapping) for row in records
        ):
            raise DataValidationError("AKShare returned invalid daily records")
        if any(not isinstance(column, str) for column in columns):
            raise DataValidationError("AKShare returned invalid column names")
        if len(columns) != len(set(columns)):
            raise DataValidationError("AKShare returned duplicate columns")
        # AKShare returns a columnless empty table when the upstream has no data.
        if (records or columns) and not REQUIRED_COLUMNS.issubset(columns):
            raise DataValidationError("AKShare response is missing required daily columns")
        bars = [
            akshare_row_to_daily_bar(row, self.instrument_id)
            for row in records
        ]
        bars.sort(key=lambda bar: bar.trade_date)

        previous_date: date | None = None
        for bar in bars:
            if not self.start_date <= bar.trade_date <= self.end_date:
                raise DataValidationError("AKShare returned a bar outside the requested date range")
            if bar.trade_date == previous_date:
                raise DataValidationError(f"AKShare returned a duplicate bar for {bar.trade_date}")
            previous_date = bar.trade_date

        return bars
