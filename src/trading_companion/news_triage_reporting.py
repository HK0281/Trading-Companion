"""Write a single experiment and its annotation checklist without overwriting artifacts."""

from html import escape
import json
from pathlib import Path
from uuid import uuid4


def cell(value):
    if value is None:
        return "未知/不适用"
    return escape(str(value), quote=True).replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;")


def rate(value):
    return f"{value['numerator']}/{value['denominator']}" + (f" ({value['value']:.1%})" if value['value'] is not None else "")


def annotation_review(rows, *, status="draft"):
    unique = {r["id"]: r for r in rows}
    label = "已复核" if status == "reviewed" else "草稿，待 Claude 逐条复核"
    lines = ["# 合成标题标注复核", "", f"状态：{label}；语气不是交易信号。", "",
             "| 编号/目标 | 标题 | relevance | event_type | tone | 标注理由 | Claude复核意见 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in unique.values():
        lines.append("| " + " | ".join(cell(v) for v in (f"{row['id']} {row['company_name']} {row['company_code']}",
                     row["title"], *(row["gold"][q] for q in ("relevance", "event_type", "tone")), row["reason"],
                     "已复核" if status == "reviewed" else "待复核")) + " |")
    return "\n".join(lines)+"\n"


def format_report(report):
    plan, providers = report["plan"], report["metrics"]["providers"]
    lines = ["# 新闻标题分诊评估", "", f"合成题库 {plan['planned_titles']} 条观测；{report['evaluated_at']}；模式 {plan['mode']}；题目/题库 {plan['task_version']}；标注 {plan['corpus']['annotation_status']}。",
             "仅说明本次合成题库；离线模拟不是模型效果。语气不是交易信号。",
             plan["rule_baseline_caveat"],
             "", plan["timeout_scope"], "",
             "| 分类器/实际模型 | 三题合计准确率 | 三题全对 | 14类宏F1 | Jev合计ECE/覆盖 | 成功调用延迟p50/p95 ms | 费用/币种 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for c, values in providers.items():
        latency, cost = values["latency"]["ok"], values["cost"]
        calibration = values["combined_calibration"]
        lines.append("| " + " | ".join(cell(v) for v in (
            c+" / "+", ".join(values["actual_models"]), rate(values["combined_accuracy"]), rate(values["joint_accuracy"]),
            f"{values['macro_f1_all_14_labels']:.4f}",
            f"{calibration['ece']} / {rate(calibration['coverage'])}" if calibration else None,
            f"{latency['p50_ms']}/{latency['p95_ms']} (n={latency['n']})",
            f"{cost.get('estimated_total')} / {cost.get('currency')}；未知请求 {cost.get('unknown_requests')}" if c != "rules" else "无供应商调用费用")) + " |")
    for c, values in providers.items():
        lines += ["", f"## {c} 分题", "", "| 问题 | 准确率 | 有效覆盖 | macro-F1 |", "| --- | --- | --- | --- |"]
        for q, v in values["questions"].items():
            lines.append(f"| {q} | {cell(rate(v['accuracy']))} | {cell(rate(v['coverage']))} | {v['macro_f1']:.4f} |")
    lines += ["", "## 级联模拟", "", "使用同一次运行的结果，不追加请求；模拟费用与实验实际两家调用费用分开，币种不合并。",
              "| 门槛 | 三题合计准确率 | 三题全对 | DeepSeek需求比例 | 缺少所需结果 | Jev费用/币种 | DeepSeek费用/币种 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for point in report["metrics"]["cascade"] or []:
        costs = point["cost_by_provider"]
        lines.append("| " + " | ".join(cell(v) for v in (point["threshold"], rate(point["scores"]["combined_accuracy"]),
            rate(point["scores"]["joint_accuracy"]), rate(point["deepseek_demand"]), point["missing_required_results"],
            f"{costs['jev']['estimated_total']} {costs['jev']['currency']}",
            f"{costs['deepseek']['estimated_total']} {costs['deepseek']['currency']}")) + " |")
    lines += ["", "## 完整指标与逐条结果", "", "以下包含计划、混淆矩阵、置信度分桶、选择性曲线、费用覆盖、失败与逐条分类。",
              "模型原始文本与错误正文不保留。", "", "```json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).replace("<", "\\u003c").replace("`", "\\u0060"), "```", ""]
    return "\n".join(lines)


def write_report(report, root):
    directory = Path(root).absolute() / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    report["artifacts"] = {k: str(directory / name) for k, name in
                           (("json", "report.json"), ("markdown", "report.md"), ("annotations", "annotation-review.md"))}
    contents = {"json": json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)+"\n",
                "markdown": format_report(report), "annotations": annotation_review(report["rows"], status=report["plan"]["corpus"]["annotation_status"])}
    for key, content in contents.items():
        with Path(report["artifacts"][key]).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return report["artifacts"]
