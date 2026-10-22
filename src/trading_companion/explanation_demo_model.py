"""Finite demo-only model: script proposals and copy claims from the supplied view."""

import json

from trading_companion.model import ModelCallError
from trading_companion.relation_explanation import EXPLANATION_VERSION, MAX_CLAIMS
from trading_companion.tool_model import FakeToolModel, ToolModelResponse, validate_tool_messages


class ExplanationDemoModel:
    """Not an LLM or evaluation: generate one bounded fixture response without new data reads."""

    def __init__(self, responses, *, max_claims=MAX_CLAIMS):
        if type(max_claims) is not int or not 1 <= max_claims <= MAX_CLAIMS:
            raise ValueError("Demo claim count must be bounded")
        self._script = FakeToolModel(responses)
        self._script_length = len(responses)
        self._max_claims = max_claims
        self._final_used = False

    @property
    def responses_consumed(self):
        return self._script.responses_consumed + int(self._final_used)

    def complete(self, messages, *, tools):
        if self._script.responses_consumed < self._script_length:
            return self._script.complete(messages, tools=tools)
        validate_tool_messages(messages, tools=tools)
        if self._final_used or messages[-1].role != "tool":
            raise ModelCallError("script_exhausted", "Offline explanation demo has no further responses")
        self._final_used = True
        view = messages[-1].result
        if view.get("version") != "research-view-v1":
            raise ModelCallError("invalid_response", "Demo requires a compact research result")
        states = {row[0]: row[1] for row in view["tables"]["states"]["rows"]}
        claims = [{"evidence_id": row[0], "relation": row[2], "value": row[4]}
                  for row in view["tables"]["relations"]["rows"]
                  if row[0] is not None and states[row[3]] == "available"][:self._max_claims]
        payload = {"version": EXPLANATION_VERSION, "request_id": view["request_id"],
                   "instrument_id": view["instrument_id"], "trade_date": view["dates"][0], "claims": claims}
        return ToolModelResponse("complete", json.dumps(payload, separators=(",", ":")),
                                 provider="fake", requested_model="fake-explanation-demo-v1",
                                 model="fake-explanation-demo-v1", is_mock=True)
