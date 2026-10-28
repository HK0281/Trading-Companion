"""Reports, reproducible human-review queues and read-only aligned comparisons."""

from html import escape as html_escape
import json
from pathlib import Path
import random
import re
from uuid import uuid4

from trading_companion.evaluation_pricing import unique_object


def cell(value):
    text = "未知" if value is None else str(value)
    text = html_escape(text, quote=True).replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "|", "#"):
        text = text.replace(marker, "\\" + marker)
    return text.replace("\r", "").replace("\n", "<br>")


def rate_text(item):
    return f"{item['numerator']}/{item['denominator']}" + (f" ({item['value']:.1%})" if item["value"] is not None else " (不适用)")


def manual_review(report, *, seed=42, limit=10):
    if type(seed) is not int or type(limit) is not int or not 0 <= limit <= 10:
        raise ValueError("Use bounded deterministic review settings")
    eligible = sorted((step for step in report["steps"] if (step.get("observed") or {}).get("answer")),
                      key=lambda step: (step["case_id"], step["repeat"], step["step"]))
    chosen = random.Random(seed).sample(eligible, min(limit, len(eligible)))
    rows = []
    for step in chosen:
        observed = step["observed"]
        company = observed.get("company_research") or {}
        evidence = company.get("evidence") or (observed.get("knowledge") or {}).get("citations") or []
        rows.append({"case_id": step["case_id"], "repeat": step["repeat"], "step": step["step"],
                     "question": step["question"], "answer": observed["answer"],
                     "run_id": (step.get("trace") or {}).get("run_id"),
                     "evidence_index": evidence[:64], "evidence_index_truncated": len(evidence) > 64,
                     "public_facts": observed.get("report"), "coverage": observed.get("coverage", []),
                     "mechanical_checks": [event for event in (step.get("trace") or {}).get("event_records", [])
                                           if event["kind"] == "check"],
                     "human": {"relevance": None, "claim_support": None, "coverage": None, "notes": None}})
    return {"seed": seed, "eligible_answers": len(eligible), "sampled": len(rows), "items": rows,
            "empty_reason": None if rows else "no_generated_answers",
            "failures": [{key: step[key] for key in ("case_id", "repeat", "step", "status")}
                         for step in report["steps"] if step["status"] != "passed"],
            "scope": "human_judgment_required_citation_ids_do_not_prove_claim_support"}


def verified_facts(report):
    metrics = report["metrics"]["overall"]
    trace = metrics["trace"]
    latency = trace["overall"]["total_elapsed_ms"]
    cost = report["cost"]
    label = {"offline": "Offline contract evaluation", "live-model": "Live-model evaluation", "live": "Live end-to-end evaluation"}[report["mode"]]
    scope = f"{label} (corpus {report['corpus']['version']}, {len(report['selected_cases'])} scenarios / {report['planned_steps']} steps)"
    values = (f"run {report['status']}; steps {rate_text(metrics['step_success'])}; scenarios {rate_text(metrics['scenario_success'])}; "
              f"route {rate_text(metrics['route_accuracy'])}; intent {rate_text(metrics['intent_accuracy'])}; "
              f"hard/range/quality {metrics['hard_requirement_violations']}/{metrics['outside_acceptable']}/{metrics['quality_requirement_failures']}; "
              f"p50/p95 {latency['p50_ms']}/{latency['p95_ms']} ms (n={latency['n']}); "
              f"provider requests {trace['models']['provider_requests_started']}; "
              f"estimated total {cost['estimated_total'] or '未知'} {cost['currency'] or ''}")
    return "| Verified facts | Result |\n| --- | --- |\n| " + cell(scope) + " | " + cell(values) + " |"


def format_report(report):
    metrics, plan = report["metrics"]["overall"], report["plan"]
    lines = ["# 聊天工作流评估", "", "范围：" + cell(report["evaluation_scope"]) + "；数据：" + cell(report["data_authenticity"]),
             "", verified_facts(report), "", "## 分母与覆盖", "",
             "失败及未运行保留在所选兼容步骤的分母中；不适用场景单列，标签分组不可相加。",
             "假模型只验证框架契约；引用和数字是联合机械检查，观点支持必须人工判断。", "",
             "| 项目 | 结果 |", "| --- | --- |",
             f"| 执行覆盖 | {cell(rate_text(metrics['execution_coverage']))} |",
             f"| 延迟覆盖 | {cell(rate_text(metrics['latency_coverage']))} |",
             f"| 不适用场景/步骤 | {plan['not_applicable_to_mode']}/{plan['not_applicable_steps']} |",
             f"| 联合检查（通过/适用） | {cell(rate_text(metrics['joint_evidence_checks']['pass_rate']))} |",
             f"| 规则纠正（步骤/全部） | {cell(rate_text(metrics['repairs']['rule_correction']['all_planned']))} |",
             f"| 模型重写（步骤/全部） | {cell(rate_text(metrics['repairs']['model_rewrite']['all_planned']))} |",
             f"| 应用降级（步骤/全部） | {cell(rate_text(metrics['fallback']['all_planned']))} |",
             f"| 截止部分覆盖（含人工注入，步骤/计划公司研究） | {cell(rate_text(metrics['source_deadline']['partial_per_planned_company']))} |",
             f"| 用量完整轮次（含零尝试） | {metrics['trace']['models']['complete_usage_turns']} |",
             f"| 有尝试且用量完整轮次 | {metrics['trace']['models']['attempted_complete_usage_turns']} |",
             "", "## 因模式不适用而排除", "", "| 场景 | 步数 | 原因 |", "| --- | ---: | --- |"]
    lines.extend(f"| {cell(item['case_id'])} | {item['steps']} | {cell(item['reason'])} |" for item in report["excluded_cases"])
    lines += ["", "## 结果类别混淆表", "", "预期行使用静态主目标；合法备选另计，不根据观测改写预期。",
              "", "| 预期/实际 | 完成 | 澄清 | 拒绝 | 错误 |", "| --- | ---: | ---: | ---: | ---: |"]
    lines.extend("| " + cell(expected) + " | " + " | ".join(str(n) for n in row.values()) + " |"
                 for expected, row in metrics["result_categories"]["expected_by_actual"].items())
    lines += ["", "未运行：" + str(metrics["result_categories"]["not_run"]), "", "## 按标签分组", "",
              "| 标签 | 步骤成功 | 场景成功 | 路由正确 | 质量失败 |", "| --- | --- | --- | --- | ---: |"]
    lines.extend(f"| {cell(tag)} | {cell(rate_text(item['step_success']))} | {cell(rate_text(item['scenario_success']))} | "
                 f"{cell(rate_text(item['route_accuracy']))} | {item['quality_requirement_failures']} |"
                 for tag, item in report["metrics"]["by_tag"].items())
    lines += ["", "## 费用与未知项", "", "费用仅为已报告用量的价格估算，不是账单；空价格、缺失用量和缓存拆分未知不按零计算。",
              "", "| 项目 | 结果 |", "| --- | --- |"]
    lines.extend(f"| {cell(key)} | {cell(json.dumps(value, ensure_ascii=False))} |" for key, value in report["cost"].items())
    lines += ["", "## 逐步结果", "", "| 场景/重复/步骤 | 状态 | 结果码 | 安全/质量失败 | run_id |",
              "| --- | --- | --- | --- | --- |"]
    for step in report["steps"]:
        lines.append(f"| {cell(str((step['case_id'],step['repeat'],step['step'])))} | {cell(step['status'])} | "
                     f"{cell((step.get('observed') or {}).get('code', step.get('code')))} | "
                     f"{cell(str(step['hard_requirement_violations']) + '/' + str(step['quality_requirement_failures']))} | "
                     f"{cell((step.get('trace') or {}).get('run_id'))} |")
    lines += ["", "## 人工抽查清单", "", "固定种子 42，最多 10 条；以下人工栏尚未填写，不能当作质量通过结论。"]
    for item in report["manual_review"]["items"]:
        lines += ["", "### " + cell(f"{item['case_id']}/{item['repeat']}/{item['step']}"), "",
                  "问题：" + cell(item["question"]), "", "显示回答：" + cell(item["answer"]), "",
                  "证据索引：" + cell(json.dumps(item["evidence_index"], ensure_ascii=False)), "",
                  "公开事实：" + cell(json.dumps(item["public_facts"], ensure_ascii=False)), "",
                  "人工填写：相关性 __；观点支持 __；覆盖 __；备注 __。"]
    lines += ["", "失败及未运行清单：" + cell(json.dumps(report["manual_review"]["failures"], ensure_ascii=False)), "",
              "配置、全部检查明细、来源版本及未知细项见同目录 report.json。人工故障不代表真实来源超时比例。", ""]
    return "\n".join(lines)


def write_report(report, root):
    directory = Path(root).absolute() / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    report["artifacts"] = {name: str(directory / filename) for name, filename in
                           (("json", "report.json"), ("markdown", "report.md"), ("manual_review", "manual-review.json"))}
    for name, content in (("json", json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"),
                          ("markdown", format_report(report)),
                          ("manual_review", json.dumps(report["manual_review"], ensure_ascii=False, allow_nan=False, indent=2) + "\n")):
        with Path(report["artifacts"][name]).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return report["artifacts"]


def load_report(path):
    path = Path(path)
    if path.stat().st_size > 20_000_000:
        raise ValueError("Evaluation report exceeds limit")
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite report")))
    validate_report(data)
    return data


def validate_report(report):
    if type(report) is not dict or report.get("version") != "research-workflow-evaluation-v2" or report.get("mode") not in ("offline", "live-model", "live"):
        raise ValueError("Unsupported evaluation report")
    if type(report.get("steps")) is not list or len(report["steps"]) > 1500:
        raise ValueError("Invalid evaluation steps")
    seen = set()
    for step in report["steps"]:
        key = (step["case_id"], step["repeat"], step["step"])
        if (type(key[0]) is not str or re.fullmatch(r"[A-Z][0-9]{2}", key[0]) is None
                or any(type(n) is not int or not 1 <= n <= 10 for n in key[1:]) or key in seen
                or step["status"] not in ("passed", "failed", "not_run")):
            raise ValueError("Invalid or duplicate evaluation step")
        seen.add(key)
        trace = step.get("trace")
        if trace is not None:
            elapsed = trace["total_elapsed_ms"]
            if elapsed is not None and (type(elapsed) is not int or elapsed < 0):
                raise ValueError("Invalid elapsed time")


def compare_reports(left, right):
    for report in (left, right):
        validate_report(report)
    fields = ("mode", "corpus", "selected_cases", "repeat", "provider", "model", "data_source", "data_authenticity",
              "price_config_sha256", "model_call_limit", "directory_sha256", "price", "excluded_cases")
    differences = [name for name in fields if left["plan"][name] != right["plan"][name]]
    for name in ("fixtures", "code_revision", "evaluation_implementation_sha256"):
        if left[name] != right[name]:
            differences.append(name)
    if left["plan"]["cost_preview"]["assumptions"] != right["plan"]["cost_preview"]["assumptions"]:
        differences.append("cost_assumptions")
    def keyed(report):
        return {(step["case_id"], step["repeat"], step["step"]): step for step in report["steps"]}
    a, b = keyed(left), keyed(right)
    if a.keys() != b.keys():
        differences.append("step_set")
    def project(step):
        if step is None:
            return None
        return {"status": step["status"], "category": (step.get("observed") or {}).get("category"),
                "code": (step.get("observed") or {}).get("code", step.get("code")),
                "coverage": (step.get("observed") or {}).get("coverage"),
                "hard_requirement_violations": step["hard_requirement_violations"],
                "quality_requirement_failures": step["quality_requirement_failures"],
                "elapsed_ms": (step.get("trace") or {}).get("total_elapsed_ms"),
                "model_attempts": (step.get("accounting") or {}).get("attempts")}
    rows = [{"case_id": key[0], "repeat": key[1], "step": key[2], "left": project(a.get(key)), "right": project(b.get(key))}
            for key in sorted(a.keys() | b.keys())]
    complete = bool(rows) and all(row[side] is not None and row[side]["elapsed_ms"] is not None for row in rows for side in ("left", "right"))
    controlled = not differences and left["mode"] != "live" and complete
    latency = {}
    for name in ("p50", "p95"):
        x, y = (r["metrics"]["overall"]["trace"]["overall"]["total_elapsed_ms"][name + "_ms"] for r in (left, right))
        latency[name] = {"left_ms": x, "right_ms": y,
                         "change_percent": (y - x) / x * 100 if controlled and x else None}
    return {"version": "workflow-comparison-v1", "status": "comparable" if not differences else "incompatible",
            "incompatible_fields": differences, "allowed_config_differences": ["collection_workers"],
            "configurations": [r["plan"] for r in (left, right)], "rows": rows, "controlled_latency_comparison": controlled,
            "latency": latency, "summaries": [r["metrics"]["overall"] for r in (left, right)],
            "evaluated_at": [r["evaluated_at"] for r in (left, right)],
            "caveat": "人工等待不是来源性能；真实来源有时间与顺序干扰；失败保留，未知不补零。"}


def format_comparison(report):
    lines = ["# 工作流并排比较", "", "结论：" + ("可比较配置" if report["status"] == "comparable" else "不可直接比较"), "",
             "条件差异：" + cell(report["incompatible_fields"]), "", "| 场景/重复/步骤 | 左状态 | 右状态 | 左耗时 ms | 右耗时 ms |",
             "| --- | --- | --- | ---: | ---: |"]
    for row in report["rows"]:
        a, b = row["left"] or {}, row["right"] or {}
        lines.append(f"| {row['case_id']}/{row['repeat']}/{row['step']} | {cell(a.get('status'))} | {cell(b.get('status'))} | "
                     f"{cell(a.get('elapsed_ms'))} | {cell(b.get('elapsed_ms'))} |")
    lines += ["", "p50/p95：" + cell(json.dumps(report["latency"], ensure_ascii=False)), "", cell(report["caveat"]), ""]
    return "\n".join(lines)
