"""Read two evaluation reports without models or sources, retaining all aligned outcomes."""

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.workflow_evaluation_reporting import load_report, compare_reports, format_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument("--report-dir")
    args = parser.parse_args(argv)
    try:
        result = compare_reports(load_report(args.left), load_report(args.right))
        if args.report_dir:
            directory = Path(args.report_dir).absolute() / uuid4().hex
            directory.mkdir(parents=True, exist_ok=False)
            result["artifacts"] = {"json": str(directory / "comparison.json"), "markdown": str(directory / "comparison.md")}
            with (directory / "comparison.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            with (directory / "comparison.md").open("x", encoding="utf-8") as stream:
                stream.write(format_comparison(result))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        result = {"status": "invalid_request", "code": "evaluation_comparison_input_or_output_invalid"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "comparable" else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
