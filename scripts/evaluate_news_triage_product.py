"""新三题对照评估：默认只预览；--live、显式预算及人工复核齐全才请求模型。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.news_triage_product_evaluation import ARMS, evaluate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", dest="dataset_path", required=True)
    parser.add_argument("--annotations", dest="annotations_path")
    parser.add_argument("--split", choices=("debug", "sealed"), default="debug")
    parser.add_argument("--arm", dest="arms", action="append", choices=ARMS)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-calls", dest="call_limit", type=int)
    parser.add_argument("--deepseek-model")
    parser.add_argument("--prices", dest="prices_path")
    parser.add_argument("--timeout-seconds", type=float, default=6)
    parser.add_argument("--report-dir", default=str(ROOT / ".agentbus/tmp/news-triage-product-evaluations"))
    args = parser.parse_args(argv)
    try:
        result = evaluate(**vars(args))
    except Exception:
        print(json.dumps({"status": "invalid_request", "detail": "输入、任务版本、人工复核或额度未通过预检，已停止。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] in ("planned", "completed") else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
