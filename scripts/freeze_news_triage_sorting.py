"""固定30组资料：默认预览；真实采集只由Claude执行一次。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"src"))

from scripts.collect_news_titles import BATCHES
from trading_companion.news_triage_sorting_artifacts import write_json
from trading_companion.news_triage_sorting_freeze import (
    CaseSpec, collection_plan, freeze_batch, SourceTransport, configured_recorder, MAX_SOURCE_REQUESTS,
)
from trading_companion.security_directory import Security, SecurityDirectory, load_security_directory
from trading_companion.company_research_contracts import SHANGHAI


# Approved #579/#580. Select only issuer features and existing public titles, never model results.
CASES = (
    CaseSpec("case01", "single_mixed", ("600079.SH",), "ST名称；2026-11-10药物临床批准标题"),
    CaseSpec("case02", "single_mixed", ("600115.SH",), "航空周期；2026-11-04永续债权融资续期标题"),
    CaseSpec("case03", "single_mixed", ("002129.SZ",), "光伏周期；2026-11-03股份回购进展标题"),
    CaseSpec("case04", "single_mixed", ("600187.SH",), "ST名称；2026-11-09回购报告书标题"),
    CaseSpec("case05", "single_mixed", ("002460.SZ",), "锂盐周期；2026-11-06实控人回购提议标题"),
    CaseSpec("case06", "single_mixed", ("600276.SH",), "创新药；2026-11-09临床批准标题"),
    CaseSpec("case07", "single_mixed", ("002594.SZ",), "汽车制造；2026-11-02月度产销快报标题"),
    CaseSpec("case08", "single_mixed", ("000488.SZ",), "ST名称及造纸周期；2026-11-06股东股份司法拍卖标题"),
    CaseSpec("case09", "single_news", ("300308.SZ",), "电子制造；严格只看新闻", ("news",)),
    CaseSpec("case10", "single_news", ("300364.SZ",), "常用词名称与传媒；严格只看新闻", ("news",)),
    CaseSpec("case11", "single_announcements", ("300763.SZ",), "光伏周期及资金用途公告", ("announcements",)),
    CaseSpec("case12", "single_announcements", ("688223.SH",), "光伏周期及长篇激励计划公告标题", ("announcements",)),
    CaseSpec("case13", "decision", ("300750.SZ",), "电池及员工持股；财务技术决策"),
    CaseSpec("case14", "decision", ("600036.SH",), "银行及招商名称重叠；财务技术决策"),
    CaseSpec("case15", "decision", ("688180.SH",), "研发商业化与业绩沟通；财务技术决策"),
    CaseSpec("case16", "decision", ("600096.SH",), "化肥周期及财报/说明会区别；财务技术决策"),
    CaseSpec("case17", "decision", ("300274.SZ",), "光伏储能与常用名称；财务技术决策"),
    CaseSpec("case18", "decision", ("000333.SZ",), "消费制造及集团名称；财务技术决策"),
    CaseSpec("case19", "multi_two", ("600115.SH", "600029.SH"), "航空同业，不互挪公司证据"),
    CaseSpec("case20", "multi_two", ("300750.SZ", "300014.SZ"), "电池同业，不互挪公司证据"),
    CaseSpec("case21", "multi_two", ("600999.SH", "600036.SH"), "招商简称片段，不同业务主体"),
    CaseSpec("case22", "multi_two", ("688180.SH", "600276.SH"), "临床术语，不同药品与公司"),
    CaseSpec("case23", "multi_three", ("300763.SZ", "688390.SH", "300274.SZ"), "光伏逆变器三公司"),
    CaseSpec("case24", "multi_three", ("600938.SH", "600011.SH", "600096.SH"), "能源成本与产品周期三公司"),
    CaseSpec("case25", "multi_three", ("000001.SZ", "600000.SH", "002142.SZ"), "银行同业三证券"),
    CaseSpec("case26", "multi_three", ("688005.SH", "300073.SZ", "688707.SH"), "电池材料三公司"),
    CaseSpec("case27", "capacity_single", ("600082.SH",), "长公司名及标题；全领域与日周指标容量"),
    CaseSpec("case28", "capacity_single", ("688180.SH",), "研发和财务文字；全领域与日周指标容量"),
    CaseSpec("case29", "capacity_multi", ("600011.SH", "600115.SH", "600096.SH"), "三公司全部领域及日周指标容量"),
    CaseSpec("case30", "capacity_multi", ("600079.SH", "000488.SZ", "600082.SH"), "三公司ST名称及全领域日周指标容量"),
)


def preview_directory(now):
    universe = tuple(row for batch in BATCHES.values() for row in batch)
    if len(universe) != 150 or any(code not in {r[0] for r in universe} for c in CASES for code in c.codes):
        raise ValueError("Keep the approved thirty cases inside the original 150 issuers")
    # Planning names only. Real collection always reloads an explicit trusted snapshot.
    return SecurityDirectory(tuple(Security(r[0], r[1]) for r in universe), "fixed_names_plan_only", now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-source-requests", type=int)
    parser.add_argument("--directory")
    parser.add_argument("--output")
    parser.add_argument("--seal-annotations", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--annotations-codex")
    parser.add_argument("--annotations-claude")
    parser.add_argument("--adjudication")
    args = parser.parse_args(argv)
    try:
        if args.seal_annotations:
            if args.live or not all((args.manifest, args.annotations_codex, args.annotations_claude)):
                raise ValueError("Supply the manifest and both completed human files")
            from trading_companion.news_triage_sorting_annotations import seal_annotations
            seal = seal_annotations(args.manifest, args.annotations_codex, args.annotations_claude, adjudication=args.adjudication)
            print(json.dumps(dict(status="human_annotations_frozen", cases=len(seal["cases"])), ensure_ascii=False))
            return 0
        if any((args.manifest, args.annotations_codex, args.annotations_claude, args.adjudication)):
            raise ValueError("Human inputs require the explicit annotation freeze mode")
        now = datetime.now(timezone.utc)
        if not args.live:
            if any((args.directory, args.output, args.max_source_requests is not None)):
                raise ValueError("Source execution options require --live")
            plan = collection_plan(CASES, preview_directory(now), now)
            plan["directory_scope"] = "planning_names_not_authority_for_live_collection"
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if not all((args.directory, args.output, args.max_source_requests)):
            raise ValueError("Live collection needs an explicit directory, output and source request cap")
        directory = load_security_directory(args.directory)
        directory.assert_usable(now.astimezone(SHANGHAI).date(), development=False)
        plan = collection_plan(CASES, directory, now)
        if not 1 <= args.max_source_requests <= min(MAX_SOURCE_REQUESTS,plan["source_http_upper_bound"]):
            raise ValueError("Public request allowance must not exceed the previewed bound")
        if Path(args.output).exists():raise ValueError("Never replace collection artifacts")
        # One workspace-wide claim, independent of output path and date. Never auto-delete.
        claim = ROOT/".agentbus/tmp/news-triage-sorting-collection.claim.json"
        claim.parent.mkdir(parents=True, exist_ok=True)
        write_json(claim, dict(version="sorting-collection-claim-v1", started_at=now.isoformat(),
                               output=str(Path(args.output).resolve()), plan=plan))
        import requests
        with requests.Session() as client:
            transport = SourceTransport(client, args.max_source_requests)
            report = freeze_batch(CASES, directory, now, args.output,
                                  recorder=configured_recorder(transport), allowance=transport)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "completed" else 1
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RuntimeError):
        print("[sorting_freeze_invalid] 采集或人工冻结未完成；保留已有文件，不自动补取。")
        return 2


if __name__ == "__main__":raise SystemExit(main())
