"""Review checklists and explicit dev/holdout tables; artifacts never overwrite."""

import json
from pathlib import Path
from uuid import uuid4

from trading_companion.news_triage_reporting import cell, rate
from trading_companion.intent_router_task import load_corpus


def annotation_review(rows,manifest):
    pairs={p['id']:p['kind'] for p in manifest['ablation_design']['pairs']}
    lines=["# 意图题库标注复核", "",f"状态：{manifest['annotation_status']}；全部为人工合成；待Claude逐条复核。",
        "同一语义家庭不跨划分；标准答案独立于冻结规则。复核结果同时写回manifest对应行的状态、摘要、reviewer和note。",
        "", "| ID/家庭/划分/消融配对 | 问题 | 允许上下文 | 标准意图/保护归属 | 理由/禁止误触发 | 复核状态 |",
        "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append("| "+" | ".join(cell(v) for v in (f"{r['id']} / {r['family_id']} / {r['split']} / {r['ablation_pair'] or '-'} {pairs.get(r['ablation_pair'],'')}",r["question"],
            json.dumps(r["context"],ensure_ascii=False),r["gold"] or r["guard"],r["reason"]+" 禁止："+",".join(r["forbidden_intents"]),
            manifest["reviews"][r["id"]]["status"]))+" |")
    return "\n".join(lines)+"\n"


def format_report(report):
    p=report["plan"]
    lines=["# 意图换说法评估", "",f"合成题库；{p['unique_questions']}条输入案例 / {p['planned_questions']}条观测；{report['evaluated_at']}；{p['suite']} / {p['split']}；{p['mode']}。",
        "留出集才是正式效果口径；开发集仅用于选阈值。offline是假响应框架验证，不是模型准确率。",
        "语义路由标签不等于最终用户结果；规则执行器后续能否澄清未判定。",
        f"题库摘要：{p['corpus']['corpus_sha256']}；定义摘要：{p['definition_sha256']}；状态：{report['status']}。",
        "旧59步/105轮一致率套件已后置，本报告不包含该项结果。",
        "", "| 分类器/返回模型 | 全部准确率 | 全部macro-F1 | 非反问题准确率 | 非反问题macro-F1 | 误路由率 | 反问率 | 反例误触发 | 已尝试p50/p95 ms | 费用/覆盖 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name,m in report["metrics"]["providers"].items():
        latency=m["latency"]["attempted"] if name!="rules" else m["latency"]["ok"]
        cost=m["cost"]
        lines.append("| "+" | ".join(cell(v) for v in (name+" / 请求:"+p['models'].get(name,'frozen_rules')+" / 返回:"+",".join(m["actual_models"]),rate(m["all"]["accuracy"]),
            m["all"]["macro_f1"],rate(m["non_clarify"]["accuracy"]),m["non_clarify"]["macro_f1"],rate(m["misroute_rate"]),
            rate(m["ask_rate"]),rate(m["false_trigger_rate"]),f"{latency['p50_ms']} / {latency['p95_ms']} (n={latency['n']})",
            f"{cost['estimated_total']} {cost['currency']}；完整{cost['requests_with_complete_cost']}/{cost['requests']}；未知{cost['unknown_requests']} {cost['unknown_reasons']}" if cost else "无供应商调用"))+" |")
    lines += ["", "## gold=clarify 单独报告", "", "| 分类器 | 正确反问 | 缺信息仍强行路由 | 执行器后续澄清 |", "| --- | --- | --- | --- |"]
    for name,m in report["metrics"]["providers"].items():
        lines.append("| "+" | ".join(cell(v) for v in (name,rate(m["correct_ask"]),rate(m["forced_route"]),"未判定" if name=="rules" else "本实验不执行"))+" |")
    lines += ["", "费用为供应商报告用量×价格的估算；模拟用量不是花费。币种分列不相加；未知用量/模型身份不补零。",
        "延迟只包含分类请求；级联是保存结果的串行估算，未部署，不代表整轮研究耗时。",
        "", "## 全部指标、失败和分歧", "", "```json",
        json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False).replace("<","\\u003c").replace("`","\\u0060"), "```", ""]
    return "\n".join(lines)


def write_report(report,root):
    folder=Path(root).absolute()/uuid4().hex
    folder.mkdir(parents=True,exist_ok=False)
    corpus,manifest=load_corpus()
    paths={k:str(folder/name) for k,name in (("json","report.json"),("markdown","report.md"),("annotations","annotation-review.md"))}
    report["artifacts"]=paths
    for name,body in (("json",json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n"),
                      ("markdown",format_report(report)),("annotations",annotation_review(corpus,manifest))):
        with Path(paths[name]).open("x",encoding="utf-8") as stream:
            stream.write(body)
    return paths
