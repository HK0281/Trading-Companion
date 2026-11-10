"""预览真实标题分组；显式 --write 才生成独立空白标注表，不联网。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_companion.news_triage_product_data import build_dataset, prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", required=True, action="append", help="Repeat for multiple capture directories")
    parser.add_argument("--directory", default=str(ROOT / "data/security_directory_live.json"))
    parser.add_argument("--output-root", default=str(ROOT / ".agentbus/tmp/news-triage-product"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.write:
            result = prepare(args.captures, args.directory, args.output_root)
        else:
            dataset = build_dataset(args.captures, args.directory)
            result = {"status": "planned" if dataset["summary"]["balance"]["passed"] else "imbalanced",
                      "task": dataset["task"], "summary": dataset["summary"],
                      "model_calls": 0, "annotations_filled": 0, "output_written": False}
    except Exception:
        print(json.dumps({"status": "invalid_request", "detail": "采样、目录或输出无效；请检查本地输入和任务版本。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 1 if result["status"] == "imbalanced" else 0


if __name__ == "__main__":
    raise SystemExit(main())
