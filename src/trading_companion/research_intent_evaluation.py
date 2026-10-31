"""Offline v2 plans, field scoring and artifacts; no paid/live entry point."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from trading_companion.classification_metrics import ratio
from trading_companion.news_triage_reporting import cell, rate
from trading_companion.research_intent_evaluation_cases import (
    VERSION, GOLD_FIELDS, load_corpus, input_state, implementation_hashes, fault_expectation,
)
from trading_companion.research_intent_evaluation_baseline import UNKNOWN, rule_observation
from trading_companion.research_intent_evaluation_fixtures import fixed_execution
from trading_companion.product_scope import _indicator
from trading_companion.workflow_evaluation import _revision, _dirty


def _plan(rows, manifest, split):
    if split != "dev":
        raise ValueError("第1步不运行留出集；只接受dev")
    selected = [row for row in rows if row["split"] == split]
    return {"version": VERSION, "status": "planned", "split": split, "annotation_status": manifest["annotation_status"],
            "corpus_sha256": manifest["corpus_sha256"], "split_sha256": manifest["split_sha256"],
            "baseline": manifest["baseline"], "implementation_sha256_lf": implementation_hashes(),
            "evaluation_revision": _revision(), "evaluation_workspace_dirty": _dirty(),
            "scenarios": len(selected), "planned_A": len(selected),
            "planned_B": sum(bool(r["probe"]["steps"]) for r in selected),
            "planned_B_turns": sum(len(r["probe"]["steps"]) for r in selected),
            "paid_calls": 0, "eligible_for_switch": False}


def build_plan(*, split="dev"):
    return _plan(*load_corpus(), split)


def same(actual, expected):
    return sorted(actual) == sorted(expected) if type(actual) is list and type(expected) is list else actual == expected


def field_scores(rows, layer):
    if layer == "B":
        rows = [r for r in rows if r.get("fault") is None]
    def correct(row, field):
        result = row[layer]
        return result["status"] == "ok" and field in result["fields"] and same(result["fields"][field], row["gold"][field])
    fields = {}
    for field in GOLD_FIELDS:
        measured = sum(r[layer]["status"] == "ok" and r[layer]["fields"].get(field, UNKNOWN) != UNKNOWN for r in rows)
        fields[field] = {"accuracy": ratio(sum(correct(r, field) for r in rows), len(rows)), "measured": measured}
    return {"n": len(rows), "fields": fields,
            "joint_all_fields": ratio(sum(all(correct(r, f) for f in GOLD_FIELDS) for r in rows), len(rows)),
            "statuses": dict(Counter(r[layer]["status"] for r in rows)),
            "correct_clarification": ratio(sum(correct(r, "outcome") for r in rows if r["gold"]["outcome"].startswith("clarify")),
                sum(r["gold"]["outcome"].startswith("clarify") for r in rows)),
            "excess_clarification": ratio(sum(r[layer]["status"] == "ok" and str(r[layer]["fields"].get("outcome", "")).startswith("clarify")
                and not r["gold"]["outcome"].startswith("clarify") for r in rows), len(rows)),
            "direct_correct": ratio(sum(correct(r, "outcome") and r["gold"]["outcome"] == "research" for r in rows), len(rows))}


def safety_checks(row):
    """Observed errors and unmeasured checks are separate; zero errors alone is not a pass."""
    names = ("vetoed_plan", "wrong_subject", "cross_object", "strict_scope", "local_intent_call")
    checks = dict.fromkeys(names, None)
    result, gold = row["B"], row["gold"]
    if row.get("fault") is not None or result["status"] != "ok":
        return checks
    final = result["steps"][-1]
    if "plan" in gold["exclusions"]:
        checks["vetoed_plan"] = any(p.get("status") == "available" for p in final["trade_plans"])
    if final["bound_subjects"]:
        checks["wrong_subject"] = not same(final["bound_subjects"], gold["bound_subjects"])
    if gold["zero_intent_calls"]:
        checks["local_intent_call"] = final["intent_calls"] != 0
    if "indicator:atr" in gold["exclusions"]:
        checks["strict_scope"] = "atr" in final["indicators"]
    if gold["strict_scope"] and gold["indicator_mentions"]:
        allowed = [dict(kind=s.kind, **s.parameters) for mention in gold["indicator_mentions"] for s in _indicator(mention)]
        checks["strict_scope"] = bool(checks["strict_scope"]) or any(s not in allowed for s in final["indicator_specs"])
    if gold["strict_scope"] and gold["research_kinds"]:
        checks["strict_scope"] = bool(checks["strict_scope"]) or bool(set(final["research_kinds"]) - set(gold["research_kinds"]))
    # Origin of inherited evidence/conditions is not inferred from a correct identity.
    # A general cross-object or full strict-scope audit remains unmeasured in this step.
    return checks


def metrics(rows):
    subsets = {"all": rows, "semantic": [r for r in rows if "local" not in r["tags"]],
               "local": [r for r in rows if "local" in r["tags"]],
               "reported_failure": [r for r in rows if r["source"]["kind"] == "reported_failure"],
               "multi_turn": [r for r in rows if r["context"]["history"]],
               "negative": [r for r in rows if "negative" in r["tags"]]}
    safety = [safety_checks(r) for r in rows]
    return {"layers": {layer: {name: field_scores(group, layer) for name, group in subsets.items()} for layer in ("A", "B")},
            "zero_tolerance": {key: {"violations": sum(s[key] is True for s in safety),
                                     "measured": sum(s[key] is not None for s in safety),
                                     "unmeasured": sum(s[key] is None for s in safety)} for key in safety[0]} if safety else {},
            "fault_handling": [{"id": r["id"], "fault": r["fault"], "expected": fault_expectation(r["fault"]),
                "status": "not_measured", "reason": "第1步未实现新理解契约的故障注入，未计通过"}
                for r in rows if r.get("fault") is not None],
            "eligible_for_switch": False}


def evaluate(*, split="dev"):
    rows, manifest = load_corpus()
    plan = _plan(rows, manifest, split)
    if manifest["annotation_status"] != "reviewed":
        return {"status": "invalid_request", "code": "intent_annotations_not_reviewed", "plan": plan}
    selected, results = [r for r in rows if r["split"] == split], []
    for row in selected:
        result = {k: row[k] for k in ("id", "family_id", "question", "context", "gold", "tags", "source", "fault")}
        state = input_state(row)
        for layer, function in (("A", lambda: rule_observation(state)), ("B", lambda: fixed_execution(state, row["probe"]))):
            if layer == "B" and row["fault"] is not None:
                result[layer] = {"status": "not_measured", "reason": "新协议故障处理另表，不改正常意图答案", "steps": [], "fields": {}}
                continue
            try:
                result[layer] = function()
            except Exception as error:
                result[layer] = {"status": "failed", "reason": type(error).__name__, "fields": {}, "steps": []}
        results.append(result)
    return {"status": "completed", "evaluated_at": datetime.now(timezone.utc).isoformat(), "plan": plan,
            "scope": "A为规则字段观测；B为固定模拟下的程序执行，均不是真实模型成绩；未观测字段保留在分母中。",
            "rows": results, "metrics": metrics(results)}


def format_report(report):
    lines = ["# 研究意图 v2：开发集基线", "", report["scope"],
             "本报告不批准产品切换；没有留出成绩。完整意图字段尚不能从旧规则全部观测，联合全对率据实计分。", "",
             "| 层/子集 | 样本数 | 全字段全对 | 观测状态 |", "| --- | --- | --- | --- |"]
    for layer, groups in report["metrics"]["layers"].items():
        for name, value in groups.items():
            lines.append("| " + " | ".join(cell(v) for v in (layer + "/" + name, value["n"], rate(value["joint_all_fields"]), value["statuses"])) + " |")
    lines += ["", "## B层故障处理（与意图准确率分开）", "", "| 题号 | 注入 | 处理要求 | 状态 |", "| --- | --- | --- | --- |"]
    for row in report["metrics"]["fault_handling"]:
        lines.append("| " + " | ".join(cell(row[k]) for k in ("id", "fault", "expected", "status")) + " |")
    lines += ["", "## 完整字段、逐题结果和未测范围", "", "```json",
              json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).replace("<", "\\u003c").replace("`", "\\u0060"), "```", ""]
    return "\n".join(lines)


def write_report(report, root):
    if report["status"] != "completed":
        raise ValueError("Only completed reviewed runs have score artifacts")
    folder = Path(root).absolute() / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    for name, body in (("report.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"),
                       ("report.md", format_report(report))):
        with (folder / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
    return str(folder)
