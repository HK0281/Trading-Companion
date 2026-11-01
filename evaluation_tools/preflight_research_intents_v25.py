"""Context-only v2.5 authoring preflight; never imports a live model factory."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools import research_intent_v25_cases as cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev', 'holdout'), default='dev')
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--plan-only', action='store_true', required=True)
    args = parser.parse_args()
    if args.split == 'holdout' and (args.corpus is None or args.expected_sha256 is None):
        parser.error('holdout requires an explicit --corpus and --expected-sha256')
    lock = cases.validate_freeze()
    expected = lock['dev_sha256'] if args.split == 'dev' else lock['holdout']['sha256']
    if args.expected_sha256 is not None and args.expected_sha256 != expected:
        parser.error('supplied SHA256 does not match the public freeze record')
    rows = cases.load_cases(args.corpus or cases.DEV, split=args.split, expected_sha256=expected)
    old, _ = cases.load_corpus()
    result = cases.preflight(rows, cases.load_setups(), cases.core.prefix_library(old))
    # Public CLI output contains only counts; detailed contexts stay in the author review packet.
    print(json.dumps(dict(version=cases.VERSION, split=args.split, freeze_status=lock['status'],
        **{key: value for key, value in result.items() if key != 'rows'}), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
