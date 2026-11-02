"""Offline public v2.6 authoring preflight; no sealed or live entry point."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from evaluation_tools import research_intent_v26_cases as cases


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev','regression'),default='dev')
    parser.add_argument('--plan-only',action='store_true',required=True)
    args=parser.parse_args()
    freeze=cases.validate_freeze()
    rows=cases.load_cases() if args.split=='dev' else cases.legacy_rows()[0]
    report=cases.preflight(rows)
    summary={k:v for k,v in report.items() if k!='rows'}
    print(json.dumps(dict(version=cases.VERSION,split=args.split,freeze_status=freeze['status'],
        note='仅检查状态构建及新题本地语法契约；没有模型理解或评分，pending叠加未生效。',**summary),ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
