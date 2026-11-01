"""Explicit public v2.5 plans and bounded Claude-run development/regression."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools import research_intent_v25_evaluation as evaluation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('all', 'dev', 'regression'), default='all')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan-only', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--max-calls', type=int)
    parser.add_argument('--timeout-seconds', type=float, default=45)
    args = parser.parse_args(argv)
    if args.live and args.split == 'all':
        parser.error('Live execution requires dev or regression, in separate approved runs')
    result = evaluation.evaluate(split=args.split, live=args.live, maximum=args.max_calls,
                                 timeout_seconds=args.timeout_seconds)
    # Detailed contexts remain in the report; stdout is a compact review summary.
    print(json.dumps({k: v for k, v in result.items() if k not in ('rows', 'source_sha256_lf')},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
