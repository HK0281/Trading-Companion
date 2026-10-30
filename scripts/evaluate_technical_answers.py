"""10题技术回答评估：草稿题库、需人工复核；默认离线。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.technical_answer_evaluation import build_plan, evaluate, write_report
from trading_companion.workflow_evaluation_runtime import check_output_root, PlanError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--mode', choices=('offline', 'live-model', 'live'), default='offline')
    parser.add_argument('--provider', choices=('deepseek',))
    parser.add_argument('--model')
    parser.add_argument('--model-call-limit', type=int)
    parser.add_argument('--prices')
    parser.add_argument('--directory')
    parser.add_argument('--data-source', choices=('tencent', 'akshare_eastmoney'), default='tencent')
    parser.add_argument('--collection-workers', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--assumed-input-tokens', type=int)
    parser.add_argument('--assumed-output-tokens', type=int)
    parser.add_argument('--trace-dir')
    parser.add_argument('--diagnostics-dir', help='默认关闭；仅允许.agentbus/tmp/下的本地诊断')
    parser.add_argument('--report-dir')
    parser.add_argument('--plan-only', action='store_true')
    options = vars(parser.parse_args(argv))
    preview, root = options.pop('plan_only'), options.pop('report_dir')
    try:
        check_output_root(root)
    except (PlanError, ValueError, OSError):
        parser.error('report-dir必须是有效目录。')
    result = build_plan(**options) if preview else evaluate(**options)
    if root is not None and not preview and 'metrics' in result:
        try:
            result['report_directory'] = write_report(result, root)
        except (OSError, ValueError):
            result.update(status='failed', code='technical_report_write_error')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result['status'] in ('passed', 'planned') else 2 if result['status'] == 'invalid_request' else 1


if __name__ == '__main__':
    raise SystemExit(main())
