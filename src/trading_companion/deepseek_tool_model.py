"""Non-streaming DeepSeek tool transport; proposals never execute tools here."""

from collections.abc import Sequence
from dataclasses import dataclass
import json
from math import isfinite

from trading_companion.deepseek_model import TOOL_OUTPUT_TOKENS, DeepSeekConfig, _post_chat
from trading_companion.model import (
    ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage,
)
from trading_companion.tool_model import (
    MAX_JSON_CHARACTERS, MAX_METADATA_CHARACTERS, MAX_PARALLEL_CALLS, MAX_TOTAL_CHARACTERS,
    ToolCall, ToolDefinition, ToolMessage, ToolModelResponse, validate_tool_messages,
)


def _json_text(value: dict) -> str:
    """Encode validated protocol objects as provider-required JSON strings."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _wire_call(call: ToolCall) -> dict[str, object]:
    return {"id": call.call_id, "type": "function", "function": {
        "name": call.name, "arguments": _json_text(call.arguments),
    }}


def _wire_message(message: ToolMessage) -> dict[str, object]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.call_id,
                "content": _json_text(message.result)}
    result = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [_wire_call(call) for call in message.tool_calls]
    return result


def _parse_response(payload: object, requested_model: str,
                    allowed_names: frozenset[str], seen_ids: frozenset[str], *,
                    response_diagnostics: bool = False) -> ToolModelResponse:
    invalid = ModelCallError("response_shape_invalid" if response_diagnostics else "invalid_response",
                             "Model returned an invalid tool response")
    if type(payload) is not dict:
        raise invalid
    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise invalid
    choice = choices[0]
    message = choice.get("message")
    if type(message) is not dict or message.get("role") != "assistant":
        raise invalid
    content = message.get("content")
    if content is not None and (type(content) is not str or len(content) > MAX_TOTAL_CHARACTERS):
        raise invalid
    text = content if content is not None and content.strip() else None
    refusal = message.get("refusal")
    if refusal is not None and (type(refusal) is not str or len(refusal) > MAX_TOTAL_CHARACTERS):
        raise invalid
    usage = None
    if payload.get("usage") is not None:
        raw_usage = payload["usage"]
        if type(raw_usage) is not dict:
            raise invalid
        usage = TokenUsage(**{
            key: raw_usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        })
    metadata = {"provider": "deepseek", "requested_model": requested_model,
                "model": payload.get("model"), "usage": usage, "is_mock": False}
    finish_reason = choice.get("finish_reason")
    if finish_reason in ("stop", "tool_calls", "length", "content_filter", "insufficient_system_resource", "aborted"):
        metadata["finish_reason"] = finish_reason

    # Never parse or forward actionable proposals from refused or interrupted output.
    if finish_reason == "content_filter" or (refusal is not None and refusal.strip()):
        return ToolModelResponse("refused", "The model did not provide an answer to this request.", **metadata)
    if finish_reason in ("length", "insufficient_system_resource", "aborted"):
        return ToolModelResponse("incomplete", text, **metadata)
    if message.get("function_call") is not None:
        raise ModelCallError("unsupported_response", "Legacy function-call responses are not supported")
    if finish_reason not in ("stop", "tool_calls"):
        raise ModelCallError("unsupported_response", "Model returned an unsupported completion state")

    raw_calls = message.get("tool_calls")
    if raw_calls is not None and type(raw_calls) is not list:
        raise invalid
    if finish_reason == "stop":
        if raw_calls:
            raise invalid
        return ToolModelResponse("complete", text, **metadata)
    if not raw_calls:
        if response_diagnostics:
            raise ModelCallError("tool_calls_missing", "Model returned no tool call")
        raise invalid
    if len(raw_calls) > MAX_PARALLEL_CALLS:
        raise invalid

    calls = []
    try:
        for raw in raw_calls:
            if type(raw) is not dict or raw.get("type") != "function":
                raise invalid
            function = raw.get("function")
            if type(function) is not dict or type(function.get("arguments")) is not str:
                raise invalid
            if response_diagnostics and len(function['arguments']) <= MAX_JSON_CHARACTERS:
                try:
                    json.loads(function['arguments'])
                except json.JSONDecodeError:
                    raise ModelCallError('tool_arguments_json_invalid', 'Model tool arguments were not complete JSON') from None
                except (ValueError, RecursionError):
                    raise ModelCallError('tool_call_invalid', 'Model tool call failed protocol validation') from None
            # ToolCall validates bounded object JSON, identifiers and duplicate keys.
            call = ToolCall(raw.get("id"), function.get("name"), function["arguments"])
            if call.name not in allowed_names:
                raise ModelCallError("unknown_tool", "Model proposed a tool outside the declared set")
            if call.call_id in seen_ids:
                raise invalid
            calls.append(call)
    except ModelRequestError:
        if response_diagnostics:
            raise ModelCallError('tool_call_invalid', 'Model tool call failed protocol validation') from None
        raise invalid from None
    return ToolModelResponse("tool_calls", text, tuple(calls), **metadata)


@dataclass(frozen=True)
class DeepSeekToolModel:
    """Implement ToolCapableModel without owning execution or conversation state."""

    config: DeepSeekConfig
    temperature: float | None = None
    response_diagnostics: bool = False

    def __post_init__(self) -> None:
        if type(self.config) is not DeepSeekConfig:
            raise ModelConfigurationError("Tool model requires validated DeepSeek configuration")
        if (self.temperature is not None and (type(self.temperature) not in (int, float)
                or not isfinite(self.temperature) or not 0 <= self.temperature <= 2)):
            raise ModelConfigurationError("Tool model temperature must be finite and between 0 and 2")
        if type(self.response_diagnostics) is not bool:
            raise ModelConfigurationError("Tool response diagnostics must be an explicit boolean")
        if (len(self.config.model) > MAX_METADATA_CHARACTERS
                or any(not character.isprintable() for character in self.config.model)):
            raise ModelConfigurationError("Tool model identifier exceeds protocol metadata limits")

    def complete(self, messages: Sequence[ToolMessage], *,
                 tools: Sequence[ToolDefinition]) -> ToolModelResponse:
        history, definitions, seen_ids = validate_tool_messages(messages, tools=tools)
        body = {
            "model": self.config.model,
            "messages": [_wire_message(message) for message in history],
            "max_tokens": min(self.config.max_output_tokens, TOOL_OUTPUT_TOKENS) if definitions else self.config.max_output_tokens,
            "stream": False,
            # Thinking mode needs reasoning history absent from this protocol.
            "thinking": {"type": "disabled"},
        }
        if self.temperature is not None:
            body['temperature'] = self.temperature
        if definitions:
            body["tools"] = [{"type": "function", "function": tool.to_dict()} for tool in definitions]
            body["tool_choice"] = "auto"
        else:
            # Every no-tool caller of this protocol requires a JSON object.
            # Plain text callers use DeepSeekTextModel, which never enables JSON mode.
            body["response_format"] = {"type": "json_object"}
        try:
            payload = _post_chat(self.config, body)
        except ModelCallError as error:
            # This transport's invalid_response is exclusively an HTTP JSON
            # decode failure. Do not inspect exception text or retain its body.
            if self.response_diagnostics and error.code == 'invalid_response':
                raise ModelCallError('response_json_invalid', 'Model response body was not JSON') from None
            raise
        return _parse_response(payload, self.config.model,
                               frozenset(tool.name for tool in definitions), seen_ids,
                               response_diagnostics=self.response_diagnostics)
