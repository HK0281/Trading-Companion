"""Review and evaluate the v2 research-intent corpus, offline only."""

import argparse
import json
from pathlib import Path
import sys

# Direct script startup must also find the shared offline guards in scripts.verify.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.research_intent_evaluation_cases import load_corpus, annotation_review
from trading_companion.research_intent_evaluation import build_plan, build_v2_plan, diagnose_baseline, evaluate, write_report
from trading_companion.typesafe_jev import ClassifierError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--plan-only", action="store_true")
    actions.add_argument("--annotation-review", action="store_true")
    actions.add_argument("--v2-plan-only", action="store_true", help="查看新协议指纹和调用上限；不调用模型")
    actions.add_argument("--diagnose-baseline", action="store_true", help="仅离线复现五道开发集失败题")
    parser.add_argument("--split", choices=("dev",), default="dev")
    parser.add_argument("--report-dir")
    options = parser.parse_args(argv)
    try:
        if options.annotation_review:
            print(annotation_review(*load_corpus()), end="")
            return 0
        if options.v2_plan_only:
            report = build_v2_plan()
        elif options.diagnose_baseline:
            report = diagnose_baseline()
        else:
            report = build_plan(split=options.split) if options.plan_only else evaluate(split=options.split)
        if options.report_dir and report["status"] == "completed":
            report["artifacts"] = write_report(report, options.report_dir)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ClassifierError, RecursionError):
        report = {"status": "invalid_request", "code": "intent_input_invalid"}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] in ("planned", "completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
