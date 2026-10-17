"""An offline message round trip with explicitly injected artificial data."""

import json

from trading_companion.tool_model import (
    FakeToolModel, ToolCall, ToolDefinition, ToolMessage, ToolModelResponse,
)


def run_demo() -> dict[str, object]:
    tool = ToolDefinition("demo_lookup", "仅描述固定人工样例，不执行任何工具。", {
        "type": "object", "properties": {"label": {"type": "string"}},
        "required": ["label"], "additionalProperties": False,
    })
    model = FakeToolModel([
        ToolModelResponse("tool_calls", tool_calls=(ToolCall(
            "demo_call_1", "demo_lookup", {"label": "artificial_fixture"}),)),
        ToolModelResponse("complete", "模拟往返完成：程序显式注入的人工固定值为7；这不是行情分析。"),
    ])
    messages = [ToolMessage("system", "仅演示离线协议，不调用真实模型，不执行工具。"),
                ToolMessage("user", "查看固定人工样例。")]
    proposal = model.complete(messages, tools=(tool,))
    injected = ToolMessage("tool", call_id="demo_call_1", name="demo_lookup", result={
        "is_synthetic": True, "value": 7, "source": "explicit_artificial_fixture",
    })
    messages.extend((proposal.as_message(), injected))
    completed = model.complete(messages, tools=(tool,))
    return {"status": "offline_mock_demo", "is_mock": True,
            "description": "人工固定结果由演示程序显式填写；未执行工具、未联网、未进行行情或Agent分析。",
            "executed_tools": False, "network_used": False,
            "proposal": proposal.to_dict(), "injected_result": injected.to_dict(),
            "final_response": completed.to_dict()}


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
