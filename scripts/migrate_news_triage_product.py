"""将一份独立标注从精确登记的rules-v2迁移到rules-v3；默认仅预览，不改答案。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.news_triage_product_migration import migrate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Original rules-v2 dataset.json")
    parser.add_argument("--sheet", required=True, help="Only your own codex.csv or claude.csv")
    parser.add_argument("--output-root", default=str(ROOT / ".agentbus/tmp/news-triage-product-migrated"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.dataset, args.sheet, args.output_root, write=args.write)
    except Exception:
        print(json.dumps({"status": "invalid_request", "detail": "旧规则登记、数据完整性、个人标注或输出路径无效；未修改原文件。"},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
