"""Explicit sealed path and SHA required; default byte-only plan never decodes questions."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools.research_intent_v25_holdout import RunInvalidated, evaluate_holdout
from trading_companion.typesafe_jev import ClassifierError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--sealed-corpus', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan-only', action='store_true', help='仅校验精确字节、行数、锁和可用性，不解码题目或占用')
    mode.add_argument('--live', action='store_true', help='Claude审核提交后执行一次；先完成全部前置检查，再占用')
    args = parser.parse_args(argv)
    try:
        result = evaluate_holdout(sealed_corpus=args.sealed_corpus, expected_sha256=args.expected_sha256, live=args.live)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ClassifierError, RecursionError,
            subprocess.SubprocessError, RunInvalidated):
        result = dict(status='invalid_request', code='holdout_admission_invalid',
            paid_calls=None if args.live else 0, eligible_for_switch=False,
            note='live异常的实际请求以一次性目录账本为准；不得补跑。' if args.live else '未运行模型。')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result['status'] in ('planned', 'completed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
