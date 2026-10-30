"""Fixed synthetic workflows; live model access is explicit and never requests live market data."""

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
import sys
from time import monotonic

from trading_companion.company_research_contracts import ResearchDataBatch, ResearchDataSection, ResearchEvidence
from trading_companion.daily_bar import DailyBar
from trading_companion.evaluation_common import EvaluationModelMeter, reported_usage
from trading_companion.open_knowledge import (
    GENERAL_KNOWLEDGE_INSTRUCTION, GENERAL_KNOWLEDGE_VERSION, answer_general_knowledge,
)
from trading_companion.product_answer import PRODUCT_ANSWER_VERSION, synthesize_product_answer
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME, prepare_product_plan
from trading_companion.product_research import ProductResearchBundle, collect_product_research
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn


VERSION = "company-research-evaluation-v1"
REFERENCE_DATE = date(2026, 9, 26)
NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
MAX_MODEL_CALLS = 10
SCOPE = "fixed_synthetic_product_workflows_and_output_contracts_not_language_or_investment_quality"


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    question: str
    calls: int
    analyses: int
    mentions: tuple[str, ...]
    kinds: tuple[str, ...]


CASES = (
    EvaluationCase("pe_knowledge", "PE和PB有什么区别？", 1, 0, (), ()),
    EvaluationCase("company_fundamentals", "分析平安银行的公司业务和基本面", 2, 0, ("平安银行",), ("profile", "financial")),
    EvaluationCase("two_stock_comparison", "比较平安银行与浦发银行的基本面和技术面", 2, 2,
                   ("平安银行", "浦发银行"), ("profile", "financial", "technical")),
    EvaluationCase("announcements_news", "分析平安银行最近的公告和新闻", 2, 0, ("平安银行",), ("news", "announcements")),
    EvaluationCase("risk_scenario", "分析平安银行的止盈止损风险情景，中线", 2, 1, ("平安银行",), ("technical",)),
)


class _SyntheticCompanyData:
    """Fixed evidence at the data-service boundary; no endpoint or network implementation."""

    def __init__(self):
        self.collections = 0

    def collect(self, request, kinds):
        self.collections += 1
        sections = []
        factor = 1.0 if request.instrument_id == "000001.SZ" else .8
        values = {
            "profile": (("company_name", request.security_name + "（合成样例）", "text"),
                        ("main_business", "用于流程验收的合成银行业务资料", "text")),
            "financial": (("revenue", 10_000_000_000.0 * factor, "cny"),
                          ("parent_net_profit", 2_000_000_000.0 * factor, "cny"),
                          ("revenue_yoy", 5.0, "percent"), ("parent_net_profit_yoy", 3.0, "percent"),
                          ("weighted_roe", 8.0, "percent")),
            "news": (("news_excerpt", "合成新闻样例：公司介绍业务进展，不代表真实新闻或利好判断。", "text"),),
            "announcements": (("announcement_title", "合成公告样例：经营安排说明", "text"),),
        }
        for kind in kinds:
            items = []
            for metric, value, unit in values[kind]:
                background = kind == "profile"
                publication = None if background else datetime(2026, 8, 20, tzinfo=timezone.utc)
                items.append(ResearchEvidence(
                    f"synthetic:{request.instrument_id}:{kind}:{metric}", request.instrument_id,
                    kind, "synthetic_company_evaluation", metric, value, unit,
                    "https://synthetic.example.invalid/fixture", publication,
                    publication.date() if publication else None,
                    "unknown" if background else "datetime", NOW,
                    report_period=date(2026, 6, 30) if kind == "financial" else None,
                    content_kind="background" if background else "snippet" if kind == "news" else "title" if kind == "announcements" else "fact",
                    temporal_scope="current_context" if background else "published_before_cutoff",
                    warnings=("Synthetic evaluation fixture; never real company data.",)))
            sections.append(ResearchDataSection(kind, "synthetic_company_evaluation", "available", tuple(items),
                "synthetic_fixture_available", "固定合成样例，不是真实公司资料。", NOW))
        return ResearchDataBatch(request, tuple(sections))


class _SyntheticBars:
    source_name, adjustment, is_synthetic = "csv", "unknown", True

    def __init__(self, instrument_id):
        self.instrument_id = instrument_id
        self.reads = 0

    def read_daily_bars(self):
        self.reads += 1
        days, cursor = [], date(2026, 9, 25)
        while len(days) < 100:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        factor = 1.0 if self.instrument_id == "000001.SZ" else 1.2
        bars = []
        for index, day in enumerate(reversed(days)):
            close = (10 + index * .02 + (.08 if index % 7 >= 3 else -.08)) * factor
            volume = 100000 + index * 1000
            bars.append(DailyBar(self.instrument_id, day, close - .03, close + .2, close - .2,
                                 close, volume, close * volume))
        return bars


def _blocked_source(request):
    raise AssertionError("Evaluation cannot construct a real market source")


def _synthetic_bundle(plan, service):
    companies, reads = [], 0
    # The collector accepts one explicit development source; bind one source per company.
    for security in plan.securities:
        bars = _SyntheticBars(security.instrument_id)
        partial = collect_product_research(replace(plan, securities=(security,)), data_service=service,
            now=NOW, development_source=bars, source_factory=_blocked_source)
        reads += bars.reads
        companies.extend(replace(company, evidence=tuple(replace(item, data_authenticity="synthetic")
            for item in company.evidence)) for company in partial.companies)
    return ProductResearchBundle(plan, tuple(companies)), reads


class _FixedModel:
    """Deterministic offline responses; these are fixtures, not scored language generation."""

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        question = messages[1].content
        case = next(item for item in CASES if item.question == question)
        if tools:
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("eval_" + case.id, PRODUCT_INTENT_TOOL_NAME, {
                "intent": "research", "instrument_mentions": list(case.mentions), "research_kinds": list(case.kinds),
                "date_expression": None, "outlook_expression": None, "wants_risk": case.id == "risk_scenario",
                "clarification_reason": None}),))
        if messages[0].content == GENERAL_KNOWLEDGE_INSTRUCTION:
            payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": [
                "PE比较股价与每股收益，PB比较股价与每股净资产。它们从盈利和资产两个角度观察估值。",
                "低估值本身不等于便宜，还要比较行业、盈利持续性和资产质量；这里解释的是概念。"],
                "citations": [{"paragraph": 0, "sources": ["K1", "K2"]},
                              {"paragraph": 1, "sources": ["K1", "K2"]}]}
        else:
            context = json.loads(messages[2].content.split("\n", 1)[1])
            records = context["evidence"]
            def find(kind, *, instrument="000001.SZ", label=None):
                return next(item for item in records if item["kind"] == kind and item["instrument_id"] == instrument
                            and (label is None or item["label"] == label))
            if case.id == "risk_scenario":
                risk = find("risk")
                levels = "；".join(part for part in risk["display"].split("；")
                                   if part.startswith(("历史参考价", "参考止损", "参考目标")))
                paragraphs = [
                    {"text": "可以基于已计算的历史ATR情景讨论参考目标，后续是否采用要观察价格变化。", "evidence_ids": [risk["id"]]},
                    {"text": "在合成历史做多假设下，" + levels + "。未计费用、滑点或跳空。", "evidence_ids": [risk["id"]]},
                ]
            elif case.id == "announcements_news":
                news, announcement = find("news"), find("announcements")
                paragraphs = [
                    {"text": "合成公告与新闻提供的是事件线索，不能仅据标题断言未来方向。", "evidence_ids": [news["id"], announcement["id"]]},
                    {"text": "公告资料为“" + announcement["display"] + "”；新闻资料为“" + news["display"] + "”。", "evidence_ids": [news["id"], announcement["id"]]},
                ]
            elif case.id == "two_stock_comparison":
                first, second = find("financial", label="营业收入"), find("financial", instrument="600000.SH", label="营业收入")
                technical = find("technical")
                paragraphs = [
                    {"text": "平安银行与浦发银行可以按财务口径和技术关系分别比较，不能用单项读数直接选出赢家。", "evidence_ids": [first["id"], second["id"]]},
                    {"text": "平安银行样例营业收入为" + first["display"] + "，浦发银行为" + second["display"] + "；仍需核对盈利质量和口径。", "evidence_ids": [first["id"], second["id"]]},
                    {"text": "技术面资料中已计算的关系是：" + technical["display"], "evidence_ids": [technical["id"]]},
                ]
            else:
                revenue, profit = find("financial", label="营业收入"), find("financial", label="归母净利润")
                paragraphs = [
                    {"text": "平安银行的样例财务提供了经营规模的线索，但还不足以直接推断股价方向。", "evidence_ids": [revenue["id"], profit["id"]]},
                    {"text": "样例营业收入为" + revenue["display"] + "，归母净利润为" + profit["display"] + "。还应结合同比与现金流判断盈利质量。", "evidence_ids": [revenue["id"], profit["id"]]},
                ]
            payload = {"version": PRODUCT_ANSWER_VERSION, "paragraphs": paragraphs}
            if case.id == "two_stock_comparison":
                # Current protocol keeps each company's interpretation with its own evidence.
                payload = {"version": "product-research-answer-v3", "decisions": [], "narratives": [
                    {"instrument_id": s["instrument_id"], "summary": {"text": s["name"] + "需分别核对经营与走势。"},
                     "current": {"text": "样例营业收入为" + find("financial", instrument=s["instrument_id"], label="营业收入")["display"] + "。"},
                     "drivers": [], "scenarios": {}, "levels": [], "stance": None}
                    for s in context["securities"]]}
        return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))


class _SuiteModel(EvaluationModelMeter):
    """Compatibility name for the company evaluator's shared allowance."""


def _usage(values):
    return reported_usage(values)


def _build_model(provider):
    if provider == "fake":
        return _FixedModel()
    from trading_companion.deepseek_model import DeepSeekConfig
    from trading_companion.deepseek_tool_model import DeepSeekToolModel
    from trading_companion.local_env import load_model_environment
    configuration = replace(DeepSeekConfig.from_env(load_model_environment()), max_output_tokens=1200)
    return DeepSeekToolModel(configuration)


def evaluate_company_research(*, provider="fake", model_call_budget=10, selected_cases=None):
    requested = tuple(item.id for item in CASES) if selected_cases is None else selected_cases
    valid_cases = (type(requested) in (tuple, list) and bool(requested)
                   and all(type(name) is str and name in {case.id for case in CASES} for name in requested)
                   and len(set(requested)) == len(requested))
    selected = tuple(item for item in CASES if valid_cases and item.id in requested)
    required = sum(item.calls for item in selected)
    base = {"version": VERSION, "provider": provider, "evaluation_scope": SCOPE,
            "fixture_data_authenticity": "synthetic", "live_provider_selected": provider == "deepseek",
            "real_market_data_requested": False, "news_network_requested": False, "directory_network_requested": False,
            "reference_date": REFERENCE_DATE.isoformat(), "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "selected_cases": [item.id for item in selected], "model_call_budget": model_call_budget,
            "required_model_call_budget": required, "language_quality": "requires_manual_review",
            "answer_display_policy": "checked_application_display_only_not_raw_model_responses", "cases": []}
    invalid = ("evaluation_cases_invalid" if not valid_cases else "evaluation_provider_invalid" if provider not in ("fake", "deepseek")
               else "evaluation_budget_invalid" if type(model_call_budget) is not int or not 1 <= model_call_budget <= MAX_MODEL_CALLS
               else "evaluation_budget_too_small" if model_call_budget < required else None)
    if invalid:
        return {**base, "status": "invalid_request", "code": invalid, "summary": {"executed": 0, "model_calls": 0, "analyses": 0}}
    try:
        model = _SuiteModel(_build_model(provider), model_call_budget)
    except Exception:
        return {**base, "status": "invalid_request", "code": "model_configuration_error",
                "summary": {"executed": 0, "model_calls": 0, "analyses": 0}}
    directory = SecurityDirectory((Security("000001.SZ", "平安银行"), Security("600000.SH", "浦发银行")),
                                  "synthetic_company_evaluation", NOW, True)
    cases = []
    for case in selected:
        control = TurnControl(TurnLimits(model_calls=case.calls, analyses=case.analyses, profile="company_research"))
        control.start()
        start, before = monotonic(), len(model.usage)
        answer, plan, collections, reads, bundle = None, None, 0, 0, None
        code = "evaluation_case_not_completed"
        try:
            with bind_turn(control):
                checked_model = ControlledToolModel(model, control)
                if case.id == "pe_knowledge":
                    answer = answer_general_knowledge(case.question, model=checked_model).answer
                else:
                    plan = prepare_product_plan(case.question, model=checked_model, directory=directory,
                        reference_date=REFERENCE_DATE, now=NOW, source="csv", development=True)
                    if plan.status == "ready":
                        service = _SyntheticCompanyData()
                        bundle, reads = _synthetic_bundle(plan, service)
                        collections = service.collections
                        answer = synthesize_product_answer(case.question, bundle, model=checked_model).answer
                    else:
                        code = plan.code
        except TurnStopped as stop:
            code = stop.code
        except Exception:
            code = "evaluation_case_error"
        stop_code = control.finish()
        runtime = control.to_dict()
        accepted = answer is not None and answer.status == "accepted" and stop_code is None
        fallback = answer is not None and answer.status == "fallback" and stop_code is None
        if answer is not None and stop_code is None:
            code = answer.code
        elif stop_code is not None:
            code = stop_code
        authentic = bundle is None or all(item.data_authenticity == "synthetic" for item in bundle.evidence)
        checks = {"answer_accepted": accepted, "synthetic_evidence": authentic,
                  "model_budget": runtime["model_calls_attempted"] <= case.calls,
                  "model_calls": runtime["model_calls_attempted"] == case.calls,
                  "analysis_budget": runtime["analyses_used"] == case.analyses,
                  "plan_ready": plan is None or plan.status == "ready"}
        cases.append({"case": case.id, "question": case.question, "status": "passed" if all(checks.values()) else "failed",
            "code": code, "checks": checks, "model_calls": runtime["model_calls_attempted"],
            "provider_calls_forwarded": len(model.usage) - before, "analyses": runtime["analyses_used"],
            "answer_origin": getattr(answer, "origin", None) or ("model" if accepted else "application"),
            "accepted": accepted, "fallback": fallback,
            "answer": answer.text if answer is not None and stop_code is None else None,
            "answer_validation_scope": answer.to_dict().get("validation_scope") if answer is not None else None,
            "synthetic_company_collections": collections, "synthetic_daily_reads": reads,
            "elapsed_ms": round((monotonic() - start) * 1000), "token_usage": _usage(model.usage[before:])})
    passed = sum(item["status"] == "passed" for item in cases)
    return {**base, "status": "passed" if passed == len(cases) else "failed",
            "code": "scenario_expectations_met" if passed == len(cases) else "scenario_expectations_not_met", "cases": cases,
            "summary": {"selected": len(selected), "executed": len(cases), "passed": passed, "failed": len(cases) - passed,
                "model_calls": sum(item["model_calls"] for item in cases), "provider_calls_forwarded": len(model.usage),
                "analyses": sum(item["analyses"] for item in cases), "token_usage": _usage(model.usage)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--model-call-budget", type=int, default=10)
    parser.add_argument("--case", choices=tuple(item.id for item in CASES), action="append", dest="selected_cases")
    args = parser.parse_args(argv)
    result = evaluate_company_research(**vars(args))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
