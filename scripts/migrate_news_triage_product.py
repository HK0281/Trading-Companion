"""迁移精确登记的个人表，或重绑定定义不变的v3/v4定稿；默认预览，不改答案。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.news_triage_product_migration import migrate, migrate_finalized


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Registered original dataset.json")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet", help="Only your own rules-v2 codex.csv or claude.csv")
    source.add_argument("--annotations", help="Matching finalized rules-v3/v4/current annotations; definitions must be identical")
    parser.add_argument("--output-root", default=str(ROOT / ".agentbus/tmp/news-triage-product-migrated"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (migrate(args.dataset, args.sheet, args.output_root, write=args.write) if args.sheet is not None
                  else migrate_finalized(args.dataset, args.annotations, args.output_root, write=args.write))
    except Exception:
        print(json.dumps({"status": "invalid_request", "detail": "旧规则登记、数据完整性、个人标注或输出路径无效；未修改原文件。"},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
