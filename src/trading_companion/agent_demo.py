"""Offline single-turn demonstration; scripted model, real synthetic CSV calculations."""

import json
from pathlib import Path

from trading_companion.agent import run_research_turn
from trading_companion.agent_input import QUESTION_EXAMPLE
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


def run_demo() -> dict[str, object]:
    source = CsvDailyBarSource(
        Path(__file__).resolve().parents[2] / "data" / "sample_daily_bars_synthetic.csv", is_synthetic=True)
    model = FakeToolModel([
        ToolModelResponse("tool_calls", tool_calls=(ToolCall("agent_demo_1", TOOL_NAME, {
            "instrument_id": "000001.SZ", "start_date": "2026-09-21", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                           {"kind": "volume", "window": 3}],
        }),)),
        ToolModelResponse("complete", "离线脚本草稿：SMA3与量能3可用，SMA20历史不足。真实模型理解与回答检查不在本演示内。"),
    ])
    result = run_research_turn(QUESTION_EXAMPLE, model=model, development_source=source)
    return {"mode": "offline_synthetic_agent_demo", "is_mock": True,
            "model_network_used": False, "market_network_used": False,
            "question": QUESTION_EXAMPLE, "result": result.to_dict()}


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
