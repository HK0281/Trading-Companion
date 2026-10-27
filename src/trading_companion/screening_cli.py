"""Windows-friendly offline demo and explicitly enabled live quote screening."""

import argparse
import json
from math import isfinite

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.screening import (
    FIELDS, OPERATORS, ScreeningCondition, ScreeningRequest, ScreeningService,
    demo_screening_snapshot, render_screening_result,
)
from trading_companion.screening_source import configured_quote_source
from trading_companion.security_directory import load_security_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Filter a bounded A-share quote sample, without models or trading")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use five explicitly synthetic records without network")
    mode.add_argument("--live", action="store_true", help="Enable bounded public quote requests")
    parser.add_argument("--directory", help="Trusted fresh nonfixture security directory for live mode")
    parser.add_argument("--source", choices=("tencent", "eastmoney"), default="tencent",
                        help="Explicit provider choice; failures never switch providers")
    parser.add_argument("--screening-coverage", choices=("standard", "expanded"), default="standard",
                        help="Expanded: up to 1000 sampled records; more requests, same deadline")
    parser.add_argument("--per-stratum", type=int, help="Tencent standard profile only: at most 100 IDs per stratum")
    parser.add_argument("--condition", nargs=3, action="append", metavar=("FIELD", "OP", "NUMBER"),
                        help=f"Repeat AND conditions. Fields: {', '.join(FIELDS)}. Operators: {', '.join(OPERATORS)}")
    parser.add_argument("--sort-by", default="amount_cny", choices=FIELDS)
    parser.add_argument("--direction", default="desc", choices=("asc", "desc"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-quote-age-days", type=int, default=7)
    parser.add_argument("--pages-per-stratum", type=int, choices=(1, 2), help="Eastmoney standard profile only")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output the structured result")
    args = parser.parse_args(argv)
    if not args.demo and not args.live:
        parser.print_help()
        return 0
    if args.demo and args.directory:
        parser.error("--demo cannot use a live security directory")
    if args.demo and (args.screening_coverage != "standard" or args.per_stratum is not None or args.pages_per_stratum is not None):
        parser.error("--demo uses fixed synthetic records, not live sample configuration")
    if args.live and (not args.directory or not args.condition):
        parser.error("--live requires --directory and at least one explicit --condition")
    try:
        raw_conditions = args.condition or [("amount_cny", "gt", "0")]
        conditions = []
        for field, operator, raw_value in raw_conditions:
            try:
                value = float(raw_value)
            except ValueError:
                raise InvalidRequestError("Condition threshold must be a finite number") from None
            if not isfinite(value):
                raise InvalidRequestError("Condition threshold must be a finite number")
            conditions.append(ScreeningCondition(field, operator, value))
        request = ScreeningRequest(tuple(conditions), args.sort_by, args.direction, args.limit, args.max_quote_age_days)
        if args.demo:
            snapshot = demo_screening_snapshot()
        else:
            collector = configured_quote_source(args.source, args.screening_coverage,
                per_stratum=args.per_stratum, pages_per_stratum=args.pages_per_stratum)
            snapshot = collector.read_snapshot(load_security_directory(args.directory))
        result = ScreeningService().screen(snapshot, request)
    except (DataSourceError, DataValidationError, InvalidRequestError) as error:
        code = {DataSourceError: "screening_source_error", DataValidationError: "screening_data_validation_error",
                InvalidRequestError: "invalid_screening_request"}[type(error)]
        print(json.dumps({"status": "failed", "code": code, "message": str(error),
                          "model_calls": 0, "trading_enabled": False}, ensure_ascii=False))
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2) if args.json_output
          else render_screening_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
