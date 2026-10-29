"""Evaluate synthetic intent paraphrases, never change product routing."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from trading_companion.intent_router_evaluation import build_plan, evaluate, export_lock, failure
from trading_companion.intent_router_reporting import write_report, annotation_review
from trading_companion.intent_router_task import load_corpus
from trading_companion.typesafe_jev import ClassifierError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument("--mode",choices=("offline","live"),default="offline")
    parser.add_argument("--classifier",action="append",choices=("jev","deepseek"),dest="classifiers")
    parser.add_argument("--split",choices=("dev","holdout"),default="dev")
    parser.add_argument("--suite",choices=("paraphrase","context-ablation","legacy"),default="paraphrase")
    parser.add_argument("--repeat",type=int,default=1)
    parser.add_argument("--call-limit",type=int)
    parser.add_argument("--prices")
    parser.add_argument("--deepseek-model")
    parser.add_argument("--timeout-seconds",type=float,default=5)
    parser.add_argument("--assumed-input-tokens",type=int)
    parser.add_argument("--assumed-output-tokens",type=int)
    parser.add_argument("--threshold-lock")
    parser.add_argument("--reference-report")
    action=parser.add_mutually_exclusive_group()
    action.add_argument("--plan-only",action="store_true")
    parser.add_argument("--report-dir")
    action.add_argument("--lock-from-report")
    parser.add_argument("--lock-output")
    action.add_argument("--annotation-review",action="store_true")
    options=vars(parser.parse_args(argv))
    preview=options.pop("plan_only")
    source,target,annotations=(options.pop(k) for k in ("lock_from_report","lock_output","annotation_review"))
    if annotations:
        print(annotation_review(*load_corpus()),end="")
        return 0
    if source or target:
        if not source or not target:
            parser.error("lock-from-report和lock-output必须同时提供")
        try:
            result=export_lock(source,target)
        except (OSError,ValueError,TypeError,KeyError,ClassifierError,RecursionError) as error:
            result=failure(error)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if result.get("version")=="intent-router-threshold-lock-v1" else 2
    result=build_plan(**options) if preview else evaluate(**options)
    if not preview and options["report_dir"] is not None and "metrics" in result:
        try:
            write_report(result,options["report_dir"])
        except (OSError,ValueError):
            result.update(status="partial",code="intent_report_write_error")
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    return 0 if result["status"] in ("planned","completed") else 2 if result["status"]=="invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
