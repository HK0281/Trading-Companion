"""合并两份独立人工标注，生成分歧、待裁定表及完整性状态，不调用模型。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.news_triage_product_annotations import write_merge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--adjudications")
    parser.add_argument("--output-root", default=str(ROOT / ".agentbus/tmp/news-triage-product-annotations"))
    args = parser.parse_args(argv)
    try:
        result = write_merge(args.dataset, args.codex, args.claude, args.output_root, adjudications=args.adjudications)
    except Exception:
        print(json.dumps({"status": "invalid_request", "detail": "标注字段、原始资料、规则版本或裁定无效。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "reviewed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
