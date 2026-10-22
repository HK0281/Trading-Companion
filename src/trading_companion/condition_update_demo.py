"""Finite offline update examples, including deliberate cross-stock fixture rejection."""

from datetime import date, datetime, timedelta, timezone

from trading_companion.agent import run_natural_research_turn
from trading_companion.agent_cli import _display_turn
from trading_companion.agent_input import request_arguments
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


def _proposal(specs, *, instrument="000001.SZ", cutoff=date(2026, 9, 25)):
    request = MultiIndicatorRequest(instrument, date(2026, 9, 21), cutoff, specs, source="csv")
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("fixed_update_analysis", TOOL_NAME,
                                                              request_arguments(request)),))


def main() -> int:
    source, directory = load_development_fixtures()
    specs = (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2))
    expanded = (*specs, IndicatorSpec("atr", window=2))
    reduced = (specs[0], expanded[-1])
    interpretation = ToolModelResponse("tool_calls", tool_calls=(ToolCall("fixed_update_intent", INTENT_TOOL_NAME, {
        "intent": "research", "instrument_mentions": ["平安银行"], "date_expression": "2026-09-21 至 2026-09-25",
        "indicator_mentions": ["SMA3", "RSI2"], "clarification_reason": None}),))
    steps = (
        ("分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2",
         ExplanationDemoModel([interpretation, _proposal(specs)]), "research_completed"),
        ("添加ATR2，其他不变", ExplanationDemoModel([_proposal(expanded)]), "research_completed"),
        ("删除RSI2", ExplanationDemoModel([_proposal(reduced)]), "research_completed"),
        ("把截止日期改成2026-09-24", ExplanationDemoModel([
            _proposal(reduced, cutoff=date(2026, 9, 24))]), "research_completed"),
        ("换成平安", FakeToolModel([]), "condition_update_instrument_unresolved"),
        ("中国平安", FakeToolModel([_proposal(reduced, instrument="601318.SH", cutoff=date(2026, 9, 24))]),
         "research_data_failed"),
        ("查看上次结果", FakeToolModel([]), "saved_result_shown"),
    )
    session = ResearchSession()
    calls = analyses = 0
    now = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
    print("固定离线教学演示：人工CSV、Fake模型、无凭据或网络请求；不是自由交互终端。")
    print("将展示换股后拒绝原股票数据：人工样例只有平安银行，不伪造中国平安行情。")
    try:
        for question, model, expected_code in steps:
            print("你> " + question)
            result = run_natural_research_turn(question, model=model, directory=directory,
                session=session, development_source=source, current_time=now)
            calls += result.model_calls_attempted
            analyses += result.analyses_used
            _display_turn(result, print)
            if result.code != expected_code:
                print("演示未符合固定预期，已停止；没有自动重试。")
                return 1
        print(f"演示统计：Fake模型调用{calls}次，分析尝试{analyses}次；预期的跨股票样例拒绝已发生。")
        return 0
    finally:
        session.clear()


if __name__ == "__main__":
    raise SystemExit(main())
