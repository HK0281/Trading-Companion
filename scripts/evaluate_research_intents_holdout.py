"""Read-only locked holdout plan; --live is reserved for the reviewed one-use run."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.research_intent_v2_holdout import evaluate_holdout
from trading_companion.typesafe_jev import ClassifierError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--plan-only', action='store_true', help='只核对版本锁、可用性及逐题上下文，不理解目标、不评分、不占用目录')
    actions.add_argument('--live', action='store_true', help='Claude审核提交后显式运行一次；最多85次，无重试')
    options = parser.parse_args(argv)
    try:
        report = evaluate_holdout(live=options.live)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ClassifierError, RecursionError):
        report = dict(status='invalid_request', code='intent_input_invalid')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['status'] in ('planned', 'completed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
