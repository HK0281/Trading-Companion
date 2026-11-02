"""Plan public v2.6.1 development/regression on the frozen v2.6 populations; live runs by Claude."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools import research_intent_v26_evaluation as evaluation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--split', choices=('dev', 'regression'), default='dev')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan-only', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--max-calls', type=int)
    parser.add_argument('--timeout-seconds', type=float, default=45)
    args = parser.parse_args(argv)
    result = evaluation.evaluate(split=args.split, live=args.live, maximum=args.max_calls,
                                 timeout_seconds=args.timeout_seconds)
    output = deepcopy({k: v for k, v in result.items() if k not in ('rows', 'source_sha256_lf')})
    if 'context_preflight' in output:
        output['context_preflight'].pop('rows', None)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
