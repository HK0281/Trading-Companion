"""Fake proposals backed by actual calculations on a trusted synthetic CSV fixture."""

import json
from pathlib import Path

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.research_tool import RESEARCH_TOOL_DEFINITION, TOOL_NAME, ResearchToolExecutor
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse


def run_demo() -> dict[str, object]:
    source = CsvDailyBarSource(Path(__file__).resolve().parents[2] / "data" / "sample_daily_bars_synthetic.csv",
                              is_synthetic=True)
    executor = ResearchToolExecutor(development_source=source)
    fake = FakeToolModel([
        ToolModelResponse("tool_calls", tool_calls=(ToolCall("research_demo_1", TOOL_NAME, {
            "instrument_id": "000001.SZ", "start_date": "2026-09-21", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                           {"kind": "volume", "window": 3}],
        }),)),
        ToolModelResponse("complete", "离线人工样例指标已计算；历史不足字段保留，本演示未进行真实行情研究或模型语义验证。"),
    ])
    messages = [ToolMessage("system", "Offline synthetic research demonstration only."),
                ToolMessage("user", "Calculate SMA3, SMA20 and volume3 on the trusted artificial fixture.")]
    proposal = fake.complete(messages, tools=(RESEARCH_TOOL_DEFINITION,))
    execution = executor.execute(proposal, messages=messages)
    final = fake.complete([*messages, proposal.as_message(), *execution.messages],
                          tools=(RESEARCH_TOOL_DEFINITION,))
    return {"status": "offline_synthetic_research_demo", "is_mock": True,
            "description": "Fake仅提议；执行器对人工CSV实际计算SMA3/SMA20/量能3。短周期仅用于演示可用与历史不足，不是参数建议；未联网、未验证真实工具模型或自由文本解释。",
            "network_used": False, "executed_tools": True, "real_provider_tool_validated": False,
            "analyses_used": executor.analyses_used, "projection_metrics": execution.projection_metrics,
            "tool_result": execution.messages[0].to_dict(), "final_response": final.to_dict()}


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
