"""默认离线预览；显式一次性付费验收及人工盲审工具。"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from trading_companion.news_triage_sorting_replay import load_manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--live",action="store_true")
    modes.add_argument("--make-blind",action="store_true")
    modes.add_argument("--summarize-blind",action="store_true")
    for name in ("manifest","manifest-sha256","prices","annotations-codex","annotations-claude",
                 "output","report","mapping","table","reviews","rerun-interrupted"):
        parser.add_argument("--"+name)
    parser.add_argument("--max-requests",type=int)
    args=parser.parse_args(argv)
    try:
        live_options=(args.manifest_sha256,args.prices,args.annotations_codex,args.annotations_claude,
                      args.max_requests is not None,args.rerun_interrupted)
        blind_options=(args.report,args.mapping,args.table,args.reviews)
        if args.live:
            if (not all((args.manifest,args.manifest_sha256,args.prices,args.annotations_codex,
                         args.annotations_claude,args.output,args.max_requests)) or any(blind_options)):
                raise ValueError("Supply every explicit live allowance and artifact")
            from trading_companion.news_triage_sorting_live import run_live
            report=run_live(args.manifest,args.manifest_sha256,args.prices,args.annotations_codex,
                            args.annotations_claude,args.max_requests,args.output,
                            rerun_interrupted=args.rerun_interrupted)
            print(json.dumps(dict(status=report["status"],attempts=report["attempts"],
                                  stop_reason=report["stop_reason"],enablement=False),ensure_ascii=False,indent=2))
            return 0 if report["status"]=="awaiting_blind_review" else 1
        if args.make_blind or args.summarize_blind:
            if args.manifest or any(live_options) or not args.report or not args.output:
                raise ValueError("Blind modes require a report and new output only")
            from trading_companion.news_triage_sorting_annotations import write_blind, summarize_blind
            if args.make_blind:
                if any((args.mapping,args.table,args.reviews)):raise ValueError("Unexpected blind inputs")
                result=dict(table_sha256=write_blind(args.report,args.output),enablement=False)
            else:
                if not all((args.mapping,args.table,args.reviews)):raise ValueError("Complete blind inputs")
                result=summarize_blind(args.report,args.mapping,args.table,args.reviews)
                from trading_companion.news_triage_sorting_artifacts import write_json
                write_json(args.output,result)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if any(live_options) or any(blind_options) or args.output:
            raise ValueError("Execution arguments require an explicit mode")
        if args.manifest:
            _,plan=load_manifest(args.manifest)
        else:
            plan=dict(mode="plan_only",pairs=30,turns=60,max_calls=dict(jev=300,deepseek=270,total=570),
                      source_requests=0,intent_requests=0,model_requests=0,enablement=False,
                      notice="Live requires v2 manifest/hash, both sealed human files, prices, cap and new output")
    except Exception:
        print("[invalid_sorting_replay] 参数、资料、人工清单或运行未通过核对；保留已有记录，不自动重跑。")
        return 2
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":raise SystemExit(main())
