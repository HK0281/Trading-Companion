"""Synthetic headline classification; offline fixtures by default."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.news_triage_evaluation import build_plan, evaluate
from trading_companion.news_triage_reporting import write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--classifier", action="append", choices=("jev", "deepseek"), dest="classifiers")
    parser.add_argument("--item", action="append", dest="selected_items")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--call-limit", type=int)
    parser.add_argument("--prices")
    parser.add_argument("--deepseek-model")
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--assumed-input-tokens", type=int)
    parser.add_argument("--assumed-output-tokens", type=int)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--report-dir")
    options = vars(parser.parse_args(argv))
    preview = options.pop("plan_only")
    result = build_plan(**options) if preview else evaluate(**options)
    if not preview and options["report_dir"] is not None and "metrics" in result:
        try:
            write_report(result, options["report_dir"])
        except (OSError, ValueError):
            result.update(status="partial", code="triage_report_write_error")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] in ("planned", "completed") else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
