"""Preview the separately frozen thirty-pair replay, with zero model/source calls."""

import argparse
import json

from trading_companion.news_triage_sorting_replay import load_manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",required=True)
    args=parser.parse_args(argv)
    try:
        _,plan=load_manifest(args.manifest)
    except (ValueError,OSError,KeyError,TypeError,AttributeError):
        print("[invalid_sorting_replay] 固定资料包或清单未通过核对，未调用模型。")
        return 2
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":raise SystemExit(main())
