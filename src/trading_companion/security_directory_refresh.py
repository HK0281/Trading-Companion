"""Opt-in directory refresh, separate from model calls and market analysis."""

import argparse
import json
from pathlib import Path

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.exchange_directory import ExchangeDirectorySource
from trading_companion.security_directory import (
    SecurityDirectory, check_directory_destination, save_security_directory,
)


def refresh_security_directory(path: str | Path, *, timeout_seconds: float = 15.0) -> SecurityDirectory:
    """Only a fully validated fetch may replace the configured local snapshot."""
    source = ExchangeDirectorySource(timeout_seconds=timeout_seconds)
    selected = check_directory_destination(path)
    directory = source.read_directory()
    save_security_directory(directory, selected)
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicitly refresh current SSE/SZSE security names")
    parser.add_argument("--live", action="store_true", help="Enable three public exchange HTTP requests")
    parser.add_argument("--output", default="data/security_directory_live.json", help="Trusted snapshot path")
    parser.add_argument("--timeout", type=float, default=15.0, help="Each connect/read wait, not a total deadline")
    arguments = parser.parse_args(argv)
    if not arguments.live:
        print(json.dumps({"status": "not_requested", "code": "live_refresh_not_enabled",
                          "network_requested": False, "snapshot_replaced": False}))
        return 0
    try:
        directory = refresh_security_directory(arguments.output, timeout_seconds=arguments.timeout)
    except (DataSourceError, DataValidationError, InvalidRequestError) as error:
        print(json.dumps({"status": "failed", "code": {
            DataSourceError: "directory_source_error", DataValidationError: "directory_validation_error",
            InvalidRequestError: "invalid_refresh_configuration",
        }[type(error)], "snapshot_replaced": False, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "status": "ok", "code": "directory_refreshed", "snapshot_replaced": True,
        "directory": directory.metadata(), "coverage": "SSE_main_STAR_including_CDR_and_SZSE_A_export_not_BSE",
        "analysis_scope_unchanged": True,
        "completeness": {"sse_declared_totals_checked": True, "szse_export_rows_validated": True,
                         "independent_full_market_completeness_proven": False},
        "model_calls": 0, "market_bar_requests": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
