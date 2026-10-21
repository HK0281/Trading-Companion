"""Offline two-turn identity clarification with scripted responses and synthetic CSV."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from trading_companion.agent import run_natural_research_turn
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.security_directory import load_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


def run_demo() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    directory = load_security_directory(root / "data" / "security_directory_fixture.json")
    source = CsvDailyBarSource(root / "data" / "sample_daily_bars_synthetic.csv", is_synthetic=True)
    now = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
    session = ResearchSession()
    model = FakeToolModel([
        ToolModelResponse("tool_calls", tool_calls=(ToolCall("interpret_1", INTENT_TOOL_NAME, {
            "intent": "research", "instrument_mentions": ["平安"], "date_expression": "最近一个月",
            "indicator_mentions": ["SMA3", "RSI2"], "clarification_reason": None,
        }),)),
        ToolModelResponse("tool_calls", tool_calls=(ToolCall("research_1", TOOL_NAME, {
            "instrument_id": "000001.SZ", "start_date": "2026-08-27", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "rsi", "window": 2}],
        }),)),
        ToolModelResponse("complete", "离线脚本草稿：保留原30自然日区间，只计算SMA3和RSI2。"
                          "实际CSV只有5条人工日线，本演示不代表真实行情或模型理解能力。"),
    ])
    configuration = {"model": model, "directory": directory, "development_source": source, "session": session}
    first = run_natural_research_turn("看看平安最近一个月的走势，只看SMA3和RSI2", current_time=now, **configuration)
    second = run_natural_research_turn("我指的是平安银行", current_time=now + timedelta(minutes=1), **configuration)
    repeated = run_natural_research_turn("平安银行", current_time=now + timedelta(minutes=2), **configuration)
    return {"mode": "offline_identity_clarification_demo", "is_mock": True,
            "model_network_used": False, "market_network_used": False, "directory_is_fixture": True,
            "first_turn": first.to_dict(), "selection_turn": second.to_dict(),
            "repeated_selection": repeated.to_dict(), "total_model_responses": model.responses_consumed,
            "final_session_state": session.to_dict()}


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
