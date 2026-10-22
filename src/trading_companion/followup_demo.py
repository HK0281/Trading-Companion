"""Fixed offline conversation, not a general-purpose fake language model."""

from datetime import datetime, timedelta, timezone
import json

from trading_companion.agent_cli import ConversationData, run_conversation
from trading_companion.agent_input import request_arguments
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_followup import FOLLOWUP_TOOL_NAME
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse


QUESTION = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
FOLLOWUP = "这里的RSI为什么偏高"


class ConversationDemoModel:
    """Accept only the finite research/follow-up/edit sequence used by this demo."""

    def __init__(self):
        from datetime import date
        request = MultiIndicatorRequest("000001.SZ", date(2026, 9, 21), date(2026, 9, 25),
            (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2)), source="csv")
        interpretation = ToolModelResponse("tool_calls", tool_calls=(ToolCall("demo_intent", INTENT_TOOL_NAME, {
            "intent": "research", "instrument_mentions": ["平安银行"], "date_expression": "2026-09-21 至 2026-09-25",
            "indicator_mentions": ["SMA3", "RSI2"], "clarification_reason": None}),))
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("demo_analysis", TOOL_NAME, request_arguments(request)),))
        edited = {**request_arguments(request), "indicators": [{"kind": "sma", "window": 5}, {"kind": "rsi", "window": 2}]}
        amendment = ToolModelResponse("tool_calls", tool_calls=(ToolCall("demo_edit", TOOL_NAME, edited),))
        self.initial = ExplanationDemoModel([interpretation, proposal])
        self.amendment = ExplanationDemoModel([amendment])
        self.calls = 0

    def complete(self, messages, *, tools):
        self.calls += 1
        if self.calls <= 3:
            return self.initial.complete(messages, tools=tools)
        if self.calls == 4 and messages[-1].content == FOLLOWUP and tools[0].name == FOLLOWUP_TOOL_NAME:
            scope = json.loads(messages[0].content.split("Cached scope: ", 1)[1])
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("demo_followup", FOLLOWUP_TOOL_NAME,
                {"request_id": scope["request_id"], "action": "explain", "topic_mentions": ["RSI"]}),))
        if self.calls in (5, 6):
            return self.amendment.complete(messages, tools=tools)
        raise ModelCallError("script_exhausted", "Fixed conversation demo has no further responses")


def main() -> int:
    data = ConversationData.from_options("synthetic")
    questions = iter((QUESTION, FOLLOWUP, "解释一下MACD里的DIF和DEA", "把SMA3改成5日，其他不变", "/last", "/exit"))
    def read_demo(prompt):
        question = next(questions)
        print(prompt + question)
        return question
    now = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
    return run_conversation(model=ConversationDemoModel(), data=data, provider="fake",
                            read_line=read_demo, clock=lambda: now)


if __name__ == "__main__":
    raise SystemExit(main())
