import argparse
import json
from datetime import date
from pathlib import Path

from trading_companion.akshare_adapter import AkshareDailyBarSource
from trading_companion.analysis import AnalysisResult, analyze_daily_bars
from trading_companion.data_source import CsvDailyBarSource, DailyBarSource
from trading_companion.errors import InvalidRequestError
from trading_companion.tencent_adapter import TencentDailyBarSource


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def print_analysis(result: AnalysisResult) -> None:
    print(f"Status: {result.status} | {result.message}")
    print(f"Source: {result.source} | Price adjustment: {result.price_adjustment}")
    print(f"{result.instrument_id or 'Unspecified instrument'} as of {result.requested_as_of_date}")
    print(f"Latest trade date: {result.latest_trade_date or 'N/A'}")
    if result.rows:
        print(f"Trade date    Close    SMA{result.window}")
        for row in result.rows:
            average = "N/A" if row.sma_close is None else f"{row.sma_close:.2f}"
            print(f"{row.bar.trade_date}    {row.bar.close_price:.2f}    {average}")
    for warning in result.warnings:
        print(f"Note: {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trading-companion",
        description="Inspect an end-of-day SMA snapshot.",
    )
    parser.add_argument("csv_path", type=Path, nargs="?", help="CSV file containing daily bars")
    parser.add_argument("--source", choices=("csv", "tencent", "akshare"), default="csv")
    parser.add_argument("--symbol", help="A-share identifier, e.g. 000001.SZ")
    parser.add_argument("--start", type=parse_date, help="First included date (YYYY-MM-DD)")
    parser.add_argument(
        "--as-of", type=parse_date, required=True, help="Analysis date (YYYY-MM-DD)"
    )
    parser.add_argument("--window", type=int, default=3, help="SMA window (default: 3)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--timeout", type=float, help="Online source connect/read timeout in seconds (default: 15)")
    args = parser.parse_args(argv)

    try:
        source: DailyBarSource
        if args.source == "csv":
            if args.csv_path is None:
                raise InvalidRequestError("CSV mode requires a csv_path")
            if args.timeout is not None:
                raise InvalidRequestError("--timeout requires --source tencent or --source akshare")
            source = CsvDailyBarSource(args.csv_path)
        else:
            if args.csv_path is not None:
                raise InvalidRequestError(f"Do not supply a csv_path with --source {args.source}")
            if args.symbol is None or args.start is None:
                raise InvalidRequestError(f"{args.source} mode requires --symbol and --start")
            source_class = TencentDailyBarSource if args.source == "tencent" else AkshareDailyBarSource
            source = source_class(
                args.symbol, args.start, args.as_of,
                timeout_seconds=args.timeout if args.timeout is not None else 15.0,
            )
        result = analyze_daily_bars(
            source, as_of_date=args.as_of, window=args.window,
            instrument_id=args.symbol, start_date=args.start,
        )
    except InvalidRequestError as error:
        parser.error(str(error))

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2))
    else:
        print_analysis(result)
    return 1 if result.status in ("source_error", "invalid_data") else 0


if __name__ == "__main__":
    raise SystemExit(main())
