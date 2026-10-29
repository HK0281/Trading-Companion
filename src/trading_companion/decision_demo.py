"""One deliberately scripted verdict for the offline UI; all data and views are artificial."""

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from trading_companion.company_research_contracts import ResearchDataBatch, ResearchDataSection, SHANGHAI
from trading_companion.followup_demo import ConversationDemoModel
from trading_companion.model import ModelCallError
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse

QUESTION = "平安银行现在能买吗"


def load_decision_demo_source():
    from trading_companion.data_source import CsvDailyBarSource
    from trading_companion.errors import DataValidationError
    path = Path(__file__).resolve().parents[2] / "data/decision_daily_bars_synthetic.csv"
    if sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != "e3c17413ebd7aa41d771ee35cdc62fe5e16b0541a78f62af05be55c0552e067b":
        raise DataValidationError("Decision demo fixture version differs")
    return CsvDailyBarSource(path, is_synthetic=True)


class DemoCompanyData:
    def collect(self, request, *, kinds):
        now = datetime(2026, 9, 26, 12, tzinfo=SHANGHAI)
        return ResearchDataBatch(request, tuple(ResearchDataSection(kind, "synthetic_company_demo", "unavailable", (),
            "demo_domain_unavailable", "固定离线演示未提供此领域资料。", now) for kind in kinds))


def scripted_decision(context):
    """Fixed bullish example, not a rule deriving market direction from ATR."""
    decisions, paragraphs = [], []
    for security in context["securities"]:
        instrument = security["instrument_id"]
        records = [e for e in context["evidence"] if e["instrument_id"] == instrument]
        plan = next((p for p in context["trade_plans"] if p["instrument_id"] == instrument
                     and p["purpose"] == "conditional_entry" and p["status"] == "available"), None)
        if not records:
            raise ModelCallError("demo_evidence_missing", "Artificial demo needs supplied evidence")
        ids = [records[0]["id"]]
        claim = lambda text: {"text": text, "evidence_ids": ids}
        reasons = [claim("人工演示固定采用偏多研判，仅用于展示参考结论和计划。"),
                   claim("观察与计划均基于本轮资料，波动数据本身不能证明未来上涨。")]
        decisions.append(dict(instrument_id=instrument, status="formed", rating="买入", direction="偏多",
            horizon=context["objective"]["horizon"], conviction="低", reasons=reasons,
            invalidation=claim("后续价格若不支持本轮观察，或补充证据与其冲突，应重新研究。"),
            plan_id=plan["plan_id"] if plan else None))
        paragraphs.append(claim(security["name"] + "的本轮结论为固定人工示意。参考计划只展示应用计算方式，不构成真实行情研判。"))
    if len(paragraphs) == 1:
        paragraphs.append({"text": "实际使用应核对行情日期、相反证据与计划失效条件，不能仅凭波动幅度确定未来方向。",
                           "evidence_ids": paragraphs[0]["evidence_ids"]})
    return {"version": "product-research-answer-v2", "paragraphs": paragraphs, "decisions": decisions}


def scripted_narrative(context):
    """Complete v3 demo from supplied evidence; the verdict remains deliberately artificial."""
    narratives = []
    for security in context["securities"]:
        instrument = security["instrument_id"]
        records = [e for e in context["evidence"] if e["instrument_id"] == instrument]
        def price(label):
            record = next((e for e in records if e["label"] == label and e["temporal_scope"] == "daily_price_context"), None)
            if record is None:
                raise ModelCallError("demo_evidence_missing", "Artificial narrative needs its supplied price facts")
            return record
        close, change = price("最新收盘价"), price("近20日涨跌幅")
        low, mean, high = price("近20日最低价"), price("MA20"), price("近20日最高价")
        def claim(text, *facts):
            return {"text": text, "evidence_ids": [r["id"] for r in facts]}
        levels = {r["id"] for r in context["price_levels"] if r["instrument_id"] == instrument}
        if not {low["id"], mean["id"], high["id"]} <= levels:
            raise ModelCallError("demo_evidence_missing", "Artificial narrative needs its supplied level references")
        narratives.append({"instrument_id": instrument,
            "summary": claim(f'**{context["objective"]["horizon"]}固定演示偏多，倾向买入，但仍存在回落风险。**'
                "这是人工示意，不构成真实行情研判。"),
            "current": claim(f'{close["date"]}已完成日线收盘{close["display"]}，近20日涨跌幅{change["display"]}。', close, change),
            "drivers": [
                {"title": "均价对照", **claim(f'MA20为{mean["display"]}，可用来对照收盘与近期均价的位置，不能单独证明趋势延续。', mean)},
                {"title": "波动边界", **claim("近期高低点提供价格观察范围，触及边界后需要结合后续收盘重新判断。", low, high)},
                {"title": "相反证据与局限", **claim("人工演示固定采用偏多研判；现有技术资料不能证明未来上涨，仍需补充经营和事件证据。")},
            ],
            "levels": [
                {"lower_id": low["id"], "upper_id": low["id"], **claim("若接近或跌破近期低点，应重新评估弱势延续的风险。", low)},
                {"lower_id": mean["id"], "upper_id": mean["id"], **claim("观察收盘能否站稳近期均价，不把均线本身当成买入指令。", mean)},
                {"lower_id": high["id"], "upper_id": high["id"], **claim("若接近近期高点，观察突破是否得到后续收盘确认。", high)},
            ],
            "scenarios": {
                "base": claim("若价格围绕现有区间震荡，继续观察收盘与均线的相对位置。"),
                "better": claim("若后续收盘持续站稳MA20并接近近20日高点，可重新核对偏多依据是否增强。", mean, high),
                "worse": claim("若后续收盘跌破计划中的止损价，或新增经营证据转弱，应重新评估原偏多假设。"),
            },
            "stance": claim("倾向买入，但只作为固定人工示意；实际研究仍需核对相反证据，并遵守计划中的失效条件。"),
        })
    return {"version": "product-research-answer-v3", "narratives": narratives,
            "decisions": scripted_decision(context)["decisions"]}


class DecisionDemoModel:
    def __init__(self):
        self.legacy = ConversationDemoModel()

    def complete(self, messages, *, tools):
        if tools and tools[0].name == PRODUCT_INTENT_TOOL_NAME:
            if messages[-1].content.strip() != QUESTION:
                raise ModelCallError("demo_question_unsupported", "Use the fixed offline decision example")
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("demo_decision_intent", PRODUCT_INTENT_TOOL_NAME,
                dict(intent="research", instrument_mentions=["平安银行"], research_kinds=["technical"],
                     date_expression=None, outlook_expression=None, wants_risk=False, clarification_reason=None)),))
        if not tools:
            context = next((m.content for m in messages if m.role == "assistant" and m.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n")), None)
            if context:
                data = json.loads(context.split("\n", 1)[1])
                script = scripted_narrative if "product-research-answer-v3" in messages[0].content else scripted_decision
                return ToolModelResponse("complete", text=json.dumps(script(data), ensure_ascii=False))
        return self.legacy.complete(messages, tools=tools)
