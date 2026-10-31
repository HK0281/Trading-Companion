"""V2 annotations and review admission; reuse the v1 JSON/digest primitives."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from hashlib import sha256
from pathlib import Path
import json
import re

from trading_companion.intent_router_task import digest, read_json
from trading_companion.intent_rule_baseline import current_baseline
from trading_companion.news_triage_reporting import cell
from trading_companion.typesafe_jev import decode
from trading_companion.workflow_evaluation_fixtures import PHASES, SCRIPT_KEYS

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data/evaluation/research-intent-v2.jsonl"
MANIFEST = CORPUS.with_suffix(".manifest.json")
VERSION = "research-intent-evaluation-v2"
SPLITS = {"dev": 155, "holdout": 85}
BASE_COMMIT = "bb6bc34cd5ad2b5f8cd0f02ed851c5e8bb369297"
GOLD_FIELDS = ("task", "operation", "objective_kind", "wants_plan", "subject_source", "bound_subjects",
               "horizon", "date_expression", "indicator_mentions", "exclusions", "research_kinds",
               "strict_scope", "outcome", "zero_intent_calls")
CHOICES = {
    "task": ("stock_research", "index_research", "screening", "knowledge", "recall",
             "unsupported", "clarify", "command", "history"),
    "operation": ("new", "explain", "update", "refresh", "local"),
    "objective_kind": ("analysis", "outlook", "decision"),
    "subject_source": ("question", "previous", "none"),
    "outcome": ("research", "clarify_subject", "clarify_identity", "clarify_horizon", "clarify_conditions",
                "clarify_conflict", "local", "reject"),
}
BOOL_CONTEXT = ("has_company", "has_technical", "has_index", "company_enabled", "workflow_enabled",
                "development", "conversation_memory")
PENDING = ("none", "company_identity", "company_horizon", "technical", "indicator", "index_identity",
           "index_horizon", "conflict")
IMPLEMENTATION = (
    "src/trading_companion/research_intent_evaluation_cases.py",
    "src/trading_companion/research_intent_evaluation_baseline.py",
    "src/trading_companion/research_intent_evaluation_fixtures.py",
    "src/trading_companion/research_intent_evaluation.py",
    "scripts/evaluate_research_intents.py",
    "src/trading_companion/intent_router_task.py", "src/trading_companion/news_triage_reporting.py",
    "src/trading_companion/classification_metrics.py", "src/trading_companion/typesafe_jev.py",
    "src/trading_companion/workflow_evaluation_fixtures.py", "src/trading_companion/decision_demo.py",
    "src/trading_companion/workflow_evaluation.py",
    "scripts/verify.py",
)


def require(condition, message="Invalid v2 evaluation corpus"):
    if not condition:
        raise ValueError(message)


def strings(value, maximum=20):
    require(type(value) is list and len(value) <= maximum
            and all(type(s) is str and 0 < len(s) <= 2000 and s.isprintable() for s in value)
            and len(set(value)) == len(value))


def valid_context(value):
    require(type(value) is dict and value.keys() == set(BOOL_CONTEXT) | {
        "latest", "source", "pending", "pending_age_seconds", "pending_binding", "shortlist", "history"})
    require(all(type(value[k]) is bool for k in BOOL_CONTEXT) and value["pending"] in PENDING
            and value["source"] in ("tencent", "akshare_eastmoney"))
    strings(value["shortlist"])
    strings(value["history"], 6)
    if value["pending"] == "none":
        require(value["pending_age_seconds"] is None and value["pending_binding"] is None)
    else:
        binding = value["pending_binding"]
        require(type(value["pending_age_seconds"]) is int and -3600 <= value["pending_age_seconds"] <= 86400
                and type(binding) is dict and binding.keys() == {"session_matches", "turn_matches"}
                and all(type(v) is bool for v in binding.values()))
    latest = value["latest"]
    if latest is not None:
        require(type(latest) is dict and latest.keys() == {"kind", "subjects", "age_seconds", "source", "development", "current", "horizon"}
                and latest["kind"] in ("company", "technical", "index") and type(latest["age_seconds"]) is int
                and -3600 <= latest["age_seconds"] <= 86400 and type(latest["development"]) is bool
                and type(latest["current"]) is bool and latest["source"] in ("tencent", "akshare_eastmoney"))
        strings(latest["subjects"], 3)
        require(latest["horizon"] is None or type(latest["horizon"]) is str and 0 < len(latest["horizon"]) <= 120)
        cache = value["has_company"] or value["has_technical"] if latest["kind"] == "technical" else value["has_" + latest["kind"]]
        require(bool(latest["subjects"]) and cache)


def input_state(row):
    """The only input to rule/model adapters. No IDs, split, gold or fixture scripts."""
    return deepcopy({"question": row["question"], "context": row["context"]})


def valid_row(row):
    require(type(row) is dict and row.keys() == {"id", "family_id", "split", "question", "context", "gold",
            "evidence", "reason", "tags", "source", "probe", "fault"})
    require(type(row["id"]) is str and re.fullmatch(r"V2[0-9]{3}", row["id"]) is not None
            and row["split"] in ("dev", "holdout"))
    for key, maximum in (("family_id", 80), ("question", 2000), ("reason", 1000)):
        require(type(row[key]) is str and 0 < len(row[key]) <= maximum and row[key].isprintable())
    valid_context(row["context"])
    gold = row["gold"]
    require(type(gold) is dict and gold.keys() == set(GOLD_FIELDS))
    for key, values in CHOICES.items():
        require(type(gold[key]) is str and gold[key] in values)
    for key in ("wants_plan", "strict_scope", "zero_intent_calls"):
        require(type(gold[key]) is bool)
    for key in ("bound_subjects", "indicator_mentions", "exclusions", "research_kinds"):
        strings(gold[key])
    require(set(gold["research_kinds"]) <= {"profile", "financial", "news", "announcements", "technical", "risk"})
    for key in ("horizon", "date_expression"):
        require(gold[key] is None or type(gold[key]) is str and 0 < len(gold[key]) <= 120)
    require(not (gold["subject_source"] == "none" and gold["bound_subjects"]))
    evidence = row["evidence"]
    require(type(evidence) is dict and evidence.keys() == {"mentions", "horizon", "vague_horizon", "dates", "indicators", "exclusions"})
    for values in evidence.values():
        strings(values)
        require(all(v in row["question"] for v in values))
    require(not set(evidence["horizon"]) & set(evidence["vague_horizon"]), "A horizon cannot also be vague evidence")
    source = row["source"]
    require(type(source) is dict and source.keys() == {"kind", "reference"}
            and source["kind"] in ("reported_failure", "synthetic", "legacy_mapping")
            and type(source["reference"]) is str and 0 < len(source["reference"]) <= 300)
    strings(row["tags"])
    require(row["fault"] in (None, "low_confidence", "malformed", "inconsistent", "timeout", "unavailable"))
    probe = row["probe"]
    require(type(probe) is dict and probe.keys() == {"steps", "unmeasured_reason"})
    steps = probe["steps"]
    require(type(steps) is list and len(steps) <= 7 and type(probe["unmeasured_reason"]) is str
            and bool(steps) != bool(probe["unmeasured_reason"]))
    if steps:
        require([s["question"] for s in steps] == [*row["context"]["history"], row["question"]])
    for step in steps:
        require(type(step) is dict and step.keys() == {"question", "script"}
                and type(step["question"]) is str and 0 < len(step["question"]) <= 2000)
        script = step["script"]
        require(type(script) is dict and script.keys() <= SCRIPT_KEYS and "phases" in script
                and type(script["phases"]) is list and len(script["phases"]) <= 6
                and all(p in PHASES for p in script["phases"]))
        for key in ("mentions", "kinds", "indicators"):
            if key in script:
                strings(script[key])
        require("date_expression" not in script or script["date_expression"] is None
                or type(script["date_expression"]) is str and len(script["date_expression"]) <= 120)


def implementation_hashes():
    return {name: sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in IMPLEMENTATION}


def load_corpus(path=CORPUS, manifest_path=MANIFEST):
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    require(0 < len(raw) <= 2_000_000 and len(raw.splitlines()) == sum(SPLITS.values()))
    rows = [decode(line) for line in raw.splitlines()]
    manifest = read_json(manifest_path)
    require(type(manifest) is dict and manifest.keys() == {"version", "author", "annotation_status", "reviewer", "reviewed_at",
            "count", "splits", "corpus_sha256", "split_sha256", "baseline", "reviews"})
    require(manifest["version"] == VERSION and manifest["author"] == "codex" and manifest["count"] == sum(SPLITS.values())
            and manifest["annotation_status"] in ("draft", "reviewed")
            and manifest["splits"] == SPLITS)
    families, seen = defaultdict(set), set()
    for row in rows:
        valid_row(row)
        key = digest(input_state(row))
        require(key not in seen, "Duplicate v2 input")
        seen.add(key)
        families[row["family_id"]].add(row["split"])
    require(len({r["id"] for r in rows}) == len(rows) and all(len(v) == 1 for v in families.values()))
    require(dict(Counter(r["split"] for r in rows)) == manifest["splits"])
    require(manifest["corpus_sha256"] == sha256(raw).hexdigest()
            and manifest["split_sha256"] == digest([(r["id"], r["family_id"], r["split"]) for r in rows]))
    baseline = current_baseline()
    require(manifest["baseline"] == {"commit": BASE_COMMIT, "rule_baseline_version": baseline["rule_baseline_version"],
                                    "source_digest": baseline["source_digest"]}, "V2 production baseline mismatch")
    reviews = manifest["reviews"]
    require(type(reviews) is dict and reviews.keys() == {r["id"] for r in rows})
    for row in rows:
        review = reviews[row["id"]]
        require(type(review) is dict and review.keys() == {"status", "row_sha256", "reviewer", "note"}
                and review["row_sha256"] == digest(row) and review["status"] in ("pending", "approved", "ambiguous")
                and review["reviewer"] in (None, "claude") and type(review["note"]) is str and len(review["note"]) <= 1000)
    if manifest["annotation_status"] == "reviewed":
        require(manifest["reviewer"] == "claude" and type(manifest["reviewed_at"]) is str
                and date.fromisoformat(manifest["reviewed_at"]) <= date.today()
                and all(r["status"] == "approved" and r["reviewer"] == "claude" for r in reviews.values()),
                "Every v2 row needs Claude review")
    else:
        require(manifest["reviewer"] is None and manifest["reviewed_at"] is None)
    return rows, manifest


def context_summary(value):
    latest = value["latest"]
    subject = "无" if latest is None else (f"{latest['kind']}:{','.join(latest['subjects'])}，距发布{latest['age_seconds']}秒，"
        f"{latest['source']}，{'当前有效' if latest['current'] else '已失效'}，{'演示' if latest['development'] else '真实模式'}，原期限{latest['horizon'] or '无'}")
    pending = value["pending"]
    if pending != "none":
        binding = value["pending_binding"]
        pending += (f"/{value['pending_age_seconds']}秒/会话{'匹配' if binding['session_matches'] else '不匹配'}"
                    f"/轮次{'匹配' if binding['turn_matches'] else '不匹配'}")
    return (f"最近：{subject}；缓存公司/技术/指数：{value['has_company']}/{value['has_technical']}/{value['has_index']}；"
            f"当前：{value['source']}/{'演示' if value['development'] else '真实模式'}；待办：{pending}；"
            f"名单：{','.join(value['shortlist']) or '无'}；前置：{' → '.join(value['history']) or '无'}；"
            f"记忆：{value['conversation_memory']}；公司/工作流启用：{value['company_enabled']}/{value['workflow_enabled']}")


def annotation_review(rows, manifest):
    lines = ["# 研究意图 v2：逐题复核清单", "", f"状态：{manifest['annotation_status']}；{manifest['splits']['dev']}开发 / {manifest['splits']['holdout']}留出。第1步不跑留出。",
             "真实故障仅指已公开脱敏原句；变体均标为合成。按家庭复核，修改后重算行指纹并重审。",
             "每题是一轮计分输入，history为前置步骤，不另算题数。bound_subjects为空表示未授权绑定。",
             "字段：objective_kind=分析/方向/评级；wants_plan=具体价位；subject_source=本句/上轮/无；outcome=研究/澄清/本地/拒绝。",
             "B层未测不等于通过；固定模拟是程序行为观测，不是真实模型成绩。", "",
             "| 题号/家庭/划分 | 前置状态 | 问题 | 各期望字段 | 原文依据/来源 | 标注理由 | B层/复核 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda r: (r['split'] != 'dev', r['family_id'], r['id'])):
        values = (f"{row['id']} / {row['family_id']} / {row['split']}", context_summary(row["context"]), row["question"], row["gold"],
                  {"evidence": row["evidence"], "source": row["source"], "fault": row["fault"]}, row["reason"],
                  {"B": "固定模拟" if row["probe"]["steps"] else row["probe"]["unmeasured_reason"],
                   "review": manifest["reviews"][row["id"]]["status"]})
        lines.append("| " + " | ".join(cell(json.dumps(v, ensure_ascii=False) if type(v) is dict else v) for v in values) + " |")
    faults = [r for r in rows if r["fault"] is not None]
    if faults:
        lines += ["", "## B层故障处理契约（另表，不改变上述正常意图答案）", "",
                  "A层按正常问题评分；B层故障不进入正常字段准确率分母。本步未运行新协议故障，不计通过。", "",
                  "| 故障题号 | 注入 | 处理要求 | 状态 |", "| --- | --- | --- | --- |"]
        for row in faults:
            lines.append("| 故障 " + row["id"] + " | " + cell(row["fault"]) + " | "
                         + cell(fault_expectation(row["fault"])) + " | 未测 |")
    return "\n".join(lines) + "\n"


def fault_expectation(fault):
    return {"allow_research": False, "allow_plan": False,
            "handling": "clarify" if fault in ("low_confidence", "malformed", "inconsistent") else "failure"}
