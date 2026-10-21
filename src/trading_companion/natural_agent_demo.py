"""Scripted natural-input examples, never a live model or a market directory."""

from datetime import date
import json
from pathlib import Path

from trading_companion.agent import run_natural_research_turn
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.security_directory import load_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


def _intent(mention: str, *, indicators: list[str]) -> ToolModelResponse:
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("interpret_demo_1", INTENT_TOOL_NAME, {
        "intent": "research", "instrument_mentions": [mention], "date_expression": "最近",
        "indicator_mentions": indicators, "clarification_reason": None,
    }),))


def run_demo() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    directory = load_security_directory(root / "data" / "security_directory_fixture.json")
    source = CsvDailyBarSource(root / "data" / "sample_daily_bars_synthetic.csv", is_synthetic=True)
    question = "看看平安银行最近的走势，关注3日均线和量能"
    model = FakeToolModel([
        _intent("平安银行", indicators=["3日均线", "量能"]),
        ToolModelResponse("tool_calls", tool_calls=(ToolCall("research_demo_1", TOOL_NAME, {
            "instrument_id": "000001.SZ", "start_date": "2025-09-26", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "volume", "window": 20}],
        }),)),
        ToolModelResponse("complete", "脚本草稿：3日均线可用，20日量能历史不足；人工数据不能用于市场判断。"),
    ])
    result = run_natural_research_turn(question, model=model, directory=directory,
                                      development_source=source, reference_date=date(2026, 9, 26))
    ambiguous_question = "看看平安最近的走势"
    ambiguous = run_natural_research_turn(
        ambiguous_question, model=FakeToolModel([_intent("平安", indicators=[])]), directory=directory,
        development_source=source, reference_date=date(2026, 9, 26))
    return {"mode": "offline_natural_agent_demo", "is_mock": True, "model_network_used": False,
            "market_network_used": False, "directory_is_fixture": True,
            "reference_date": "2026-09-26", "question": question, "result": result.to_dict(),
            "clarification_example": {"question": ambiguous_question, "result": ambiguous.to_dict()}}


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
