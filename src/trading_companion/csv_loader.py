import csv
from datetime import date
from pathlib import Path

from trading_companion.daily_bar import DailyBar


REQUIRED_COLUMNS = {
    "instrument_id",
    "trade_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume_shares",
    "amount_cny",
}


def load_daily_bars(path: str | Path) -> list[DailyBar]:
    """Read daily bars from a CSV file and validate each row."""
    bars: list[DailyBar] = []

    with Path(path).open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file, strict=True)
        if reader.fieldnames is None:
            raise ValueError("CSV header is missing")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV header contains duplicate columns")

        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV row width does not match the header")
                bar = DailyBar(
                    instrument_id=row["instrument_id"],
                    trade_date=date.fromisoformat(row["trade_date"]),
                    open_price=float(row["open_price"]),
                    high_price=float(row["high_price"]),
                    low_price=float(row["low_price"]),
                    close_price=float(row["close_price"]),
                    volume_shares=int(row["volume_shares"]),
                    amount_cny=float(row["amount_cny"]),
                )
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid CSV row {reader.line_num}: {error}") from error
            bars.append(bar)

    return bars
