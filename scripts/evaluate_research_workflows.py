"""Evaluate real chat workflows; offline by default, real modes require explicit configuration."""

import argparse
import json
from pathlib import Path
import sys

# The executable lives in scripts/, while the shared offline guards live in scripts.verify.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.workflow_evaluation import evaluate_workflows
from trading_companion.workflow_evaluation_runtime import PlanError, build_plan, check_output_root
from trading_companion.workflow_evaluation_reporting import write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("offline", "live-model", "live"), default="offline")
    parser.add_argument("--case", action="append", dest="selected_cases")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--collection-workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--model-call-limit", type=int)
    parser.add_argument("--trace-dir", help="Retain trace-v2 records in a new run subdirectory")
    parser.add_argument("--provider", choices=("deepseek", "openai"))
    parser.add_argument("--model")
    parser.add_argument("--prices", help="Versioned, verified price file; empty prices remain unknown")
    parser.add_argument("--directory", help="Existing trusted security directory; live only, never refreshed")
    parser.add_argument("--data-source", choices=("tencent", "akshare_eastmoney"), default="tencent")
    parser.add_argument("--assumed-input-tokens", type=int)
    parser.add_argument("--assumed-output-tokens", type=int)
    parser.add_argument("--plan-only", action="store_true", help="Preview without credentials, clients or network")
    parser.add_argument("--report-dir", help="Write JSON/Markdown/review artifacts to a new unique subdirectory")
    options = vars(parser.parse_args(argv))
    preview, report_root = options.pop("plan_only"), options.pop("report_dir")
    try:
        check_output_root(report_root)
    except PlanError as error:
        parser.error(error.detail)
    except (ValueError, OSError):
        parser.error("Use a valid report directory")
    result = build_plan(**options) if preview else evaluate_workflows(**options)
    if report_root is not None and not preview and "metrics" in result:
        try:
            write_report(result, report_root)
        except (OSError, ValueError):
            result.update(status="failed", code="evaluation_report_write_error")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] in ("passed", "planned") else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
