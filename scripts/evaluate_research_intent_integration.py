"""Default read-only acceptance plan; only --live with --max-calls can spend calls."""

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    from evaluation_tools import research_intent_integration_acceptance as acceptance
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('all', 'dev', 'products'), default='all')
    parser.add_argument('--max-calls', type=int, help='Explicit global limit, including both entries and retries')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, help='New live run directory strictly below .agentbus/tmp')
    parser.add_argument('--prices', type=Path, help='Optional JSON: requested model -> currency/input_hit/input_miss/output per million')
    parser.add_argument('--json', action='store_true', help='Print the full read-only plan as JSON')
    args = parser.parse_args(argv)
    if args.max_calls is not None and args.max_calls < 1:
        parser.error('--max-calls must be positive')
    if args.live and args.max_calls is None:
        parser.error('--live requires an explicit --max-calls')
    try:
        plan = acceptance.build_plan(suite=args.suite, max_calls=args.max_calls)
        if not args.live:
            print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else acceptance.markdown(plan))
            return 0
        output = args.output or acceptance.TEMP / ('integration-acceptance-' + uuid4().hex)
        prices = json.loads(args.prices.read_text(encoding='utf-8')) if args.prices else None
        report = acceptance.run_live(plan, output=output, prices=prices)
        print(json.dumps(dict(status=report['status'], summary=report['summary'], report=str(output / 'report.json')),
                         ensure_ascii=False, indent=2))
        return 0 if report['status'] == 'observations_recorded' else 1
    except Exception:
        # Configuration/provider errors may contain credentials; never print their values.
        print('验收工具无法完成：请检查公开输入、依赖、隔离目录及服务端模型配置。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
