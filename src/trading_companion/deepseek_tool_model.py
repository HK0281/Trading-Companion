"""Non-streaming DeepSeek tool transport; proposals never execute tools here."""

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from math import isfinite
import re

from trading_companion.deepseek_model import TOOL_OUTPUT_TOKENS, DeepSeekConfig, _post_chat
from trading_companion.model import (
    ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage,
)
from trading_companion.tool_model import (
    MAX_JSON_CHARACTERS, MAX_METADATA_CHARACTERS, MAX_PARALLEL_CALLS, MAX_TOTAL_CHARACTERS,
    ToolCall, ToolDefinition, ToolMessage, ToolModelResponse, validate_tool_messages,
    _unique_pairs, _reject_constant,
)


MAX_FAILED_ARGUMENT_CHARACTERS = 10000
_RETRY_ARGUMENTS = ContextVar('retry_candidate_tool_arguments', default=False)


@contextmanager
def retry_tool_arguments():
    """Allow only scalar quoting on a retry, without changing its request."""
    token = _RETRY_ARGUMENTS.set(True)
    try:
        yield
    finally:
        _RETRY_ARGUMENTS.reset(token)


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


def _json_category(error, arguments):
    """Classify without returning parser messages or any part of its document."""
    if error.msg.startswith('Invalid control character'):
        return 'control_char'
    if arguments.lstrip().startswith('```'):
        return 'code_fence'
    if error.msg == 'Extra data':
        return 'trailing_data'
    if error.msg.startswith('Unterminated') or error.pos >= len(arguments.rstrip()):
        return 'incomplete_json'
    return 'invalid_syntax'


def _unwrap_arguments(arguments):
    """Only a complete leading object with unambiguous prose or one code fence."""
    text, kind = arguments.strip(), 'trailing_text'
    if text.startswith('```'):
        fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```', text, re.S | re.I)
        if fence is None:
            raise ValueError('Invalid wrapper')
        text, kind = fence[1].strip(), 'code_fence'
    if not text.startswith('{'):
        raise ValueError('Not a leading object')
    decoder = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    value, end = decoder.raw_decode(text)
    suffix = text[end:].strip()
    if type(value) is not dict or (not suffix and kind != 'code_fence'):
        raise ValueError('Not a wrapped object')
    # Reject another object/value, JSON fragments, or fences even when prose
    # precedes them. This deliberately rejects ambiguous wrappers.
    if suffix:
        if kind == 'code_fence' or not suffix[0].isalpha() or re.search(r'[{}\[\]`"]', suffix):
            raise ValueError('Ambiguous suffix')
        try:
            decoder.raw_decode(suffix)
        except json.JSONDecodeError:
            pass
        else:
            raise ValueError('Multiple JSON values')
    return value, kind


def _quote_unquoted_scalar(arguments, error):
    """Quote exactly one object value at the first strict parser error; never compose repairs."""
    if error.msg != 'Expecting value':
        raise ValueError('Not a value start')
    position = error.pos
    # Scan the already parsed prefix, skipping complete JSON strings. A colon
    # inside a quoted string or an array element must never authorize repair.
    decoder = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    stack, last, index = [], None, 0
    while index < position:
        char = arguments[index]
        if char == '"':
            _, index = decoder.raw_decode(arguments, index)
            last = 'string'
            continue
        if char in '{[':
            stack.append(char)
        elif char in '}]':
            stack.pop()
        if not char.isspace():
            last = char
        index += 1
    if index != position or last != ':' or not stack or stack[-1] != '{':
        raise ValueError('Not an object value')
    boundary = re.search(r'[,}]', arguments[position:])
    if boundary is None:
        raise ValueError('Missing value boundary')
    end = position + boundary.start()
    raw = arguments[position:end]
    scalar = raw.strip()
    if (not 1 <= len(scalar) <= 200 or re.search(r"[\"'\[\]{}:\\\r\n]", raw)
            or scalar.casefold() in ('true', 'false', 'null', 'nan', 'infinity', '-infinity')
            or re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', scalar)):
        raise ValueError('Not a bounded text scalar')
    repaired = arguments[:position] + json.dumps(scalar, ensure_ascii=True) + arguments[end:]
    value = decoder.decode(repaired)
    if type(value) is not dict:
        raise ValueError('Not an object')
    return value


def _tool_arguments(arguments, finish_reason, call_index, *, diagnostics, repair, sink,
                    save_failed_arguments=False, scalar_only=False):
    """Strict first; return a proposal plus safe diagnostic awaiting ToolCall validation."""
    if diagnostics and len(arguments) > MAX_JSON_CHARACTERS and sink is not None:
        sink(dict(call_index=call_index, arguments_length=len(arguments), finish_reason=finish_reason,
                  category='size_limit', position=None, line=None, column=None, repair=None, correction=None))
    if not diagnostics or len(arguments) > MAX_JSON_CHARACTERS:
        return arguments, None
    try:
        json.loads(arguments)
        return arguments, None
    except json.JSONDecodeError as error:
        parse_error = error
        event = dict(call_index=call_index, arguments_length=len(arguments), finish_reason=finish_reason,
                     category=_json_category(error, arguments), position=error.pos,
                     line=error.lineno, column=error.colno, repair=None, correction=None)
    except (ValueError, RecursionError):
        raise ModelCallError('tool_call_invalid', 'Model tool call failed protocol validation') from None
    if repair:
        try:
            value = _quote_unquoted_scalar(arguments, parse_error)
            event['repair'] = 'unquoted_scalar'
            return value, event
        except ModelRequestError:
            if sink is not None:
                sink(event)
            raise ModelCallError('tool_call_invalid', 'Model tool call failed protocol validation') from None
        except (ValueError, IndexError, RecursionError):
            pass
    if repair and not scalar_only:
        try:
            # The sole relaxation is literal control characters in strings.
            value = json.loads(arguments, strict=False, object_pairs_hook=_unique_pairs,
                               parse_constant=_reject_constant)
            event['repair'] = 'control_char'
            return value, event
        except json.JSONDecodeError:
            pass
        except (ValueError, RecursionError):
            if sink is not None:
                sink(event)
            raise ModelCallError('tool_call_invalid', 'Model tool call failed protocol validation') from None
        try:
            value, kind = _unwrap_arguments(arguments)
            event['repair'] = kind
            return value, event
        except (ValueError, RecursionError):
            pass
    if sink is not None:
        if save_failed_arguments:
            event.update(arguments_raw=arguments[:MAX_FAILED_ARGUMENT_CHARACTERS],
                         arguments_raw_truncated=len(arguments) > MAX_FAILED_ARGUMENT_CHARACTERS)
        sink(event)
    raise ModelCallError('tool_arguments_json_invalid', 'Model tool arguments were not complete JSON') from None


def _parse_response(payload: object, requested_model: str,
                    allowed_names: frozenset[str], seen_ids: frozenset[str], *,
                    response_diagnostics: bool = False, repair_tool_arguments: bool = False,
                    argument_diagnostic_sink=None, save_failed_arguments=False,
                    scalar_only=False) -> ToolModelResponse:
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
        if response_diagnostics and argument_diagnostic_sink is not None:
            raw_calls = message.get('tool_calls')
            for index, raw in enumerate(raw_calls[:MAX_PARALLEL_CALLS] if type(raw_calls) is list else []):
                arguments = raw.get('function', {}).get('arguments') if type(raw) is dict and type(raw.get('function')) is dict else None
                if type(arguments) is str:
                    argument_diagnostic_sink(dict(call_index=index, arguments_length=len(arguments), finish_reason=finish_reason,
                        category='interrupted', position=None, line=None, column=None, repair=None, correction=None))
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
        for call_index, raw in enumerate(raw_calls):
            if type(raw) is not dict or raw.get("type") != "function":
                raise invalid
            function = raw.get("function")
            if type(function) is not dict or type(function.get("arguments")) is not str:
                raise invalid
            arguments, diagnostic = _tool_arguments(function['arguments'], finish_reason, call_index,
                diagnostics=response_diagnostics, repair=repair_tool_arguments, sink=argument_diagnostic_sink,
                save_failed_arguments=save_failed_arguments, scalar_only=scalar_only)
            # ToolCall validates bounded object JSON, identifiers and duplicate keys.
            try:
                call = ToolCall(raw.get("id"), function.get("name"), arguments)
            except ModelRequestError:
                if diagnostic is not None and argument_diagnostic_sink is not None:
                    argument_diagnostic_sink(dict(diagnostic, repair=None))
                raise
            if call.name not in allowed_names:
                raise ModelCallError("unknown_tool", "Model proposed a tool outside the declared set")
            if call.call_id in seen_ids:
                raise invalid
            if diagnostic is not None and argument_diagnostic_sink is not None:
                argument_diagnostic_sink(dict(diagnostic, correction='tool_arguments_repaired'))
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
    repair_tool_arguments: bool = False
    argument_diagnostic_sink: Callable[[dict[str, object]], None] | None = field(default=None, repr=False, compare=False)
    evaluation_scope: str | None = None
    save_failed_arguments: bool = False

    def __post_init__(self) -> None:
        if type(self.config) is not DeepSeekConfig:
            raise ModelConfigurationError("Tool model requires validated DeepSeek configuration")
        if (self.temperature is not None and (type(self.temperature) not in (int, float)
                or not isfinite(self.temperature) or not 0 <= self.temperature <= 2)):
            raise ModelConfigurationError("Tool model temperature must be finite and between 0 and 2")
        if type(self.response_diagnostics) is not bool:
            raise ModelConfigurationError("Tool response diagnostics must be an explicit boolean")
        if (self.evaluation_scope not in (None, 'public', 'sealed')
                or type(self.save_failed_arguments) is not bool
                or self.save_failed_arguments and (self.evaluation_scope is None or not self.response_diagnostics
                                                     or self.argument_diagnostic_sink is None)):
            raise ModelConfigurationError('Saving failed arguments requires explicit evaluation diagnostics and a sink')
        if (type(self.repair_tool_arguments) is not bool
                or self.argument_diagnostic_sink is not None and not callable(self.argument_diagnostic_sink)
                or self.repair_tool_arguments and (not self.response_diagnostics or self.argument_diagnostic_sink is None)):
            raise ModelConfigurationError('Argument repair requires explicit diagnostics and a safe sink')
        if (len(self.config.model) > MAX_METADATA_CHARACTERS
                or any(not character.isprintable() for character in self.config.model)):
            raise ModelConfigurationError("Tool model identifier exceeds protocol metadata limits")

    def complete(self, messages: Sequence[ToolMessage], *,
                 tools: Sequence[ToolDefinition]) -> ToolModelResponse:
        history, definitions, seen_ids = validate_tool_messages(messages, tools=tools)
        if (self.repair_tool_arguments or self.save_failed_arguments) and (len(definitions) != 1
                or definitions[0].name != 'interpret_research_intent_v2'
                or definitions[0].parameters.get('properties', {}).get('version', {}).get('enum') != ['research-intent-v2.5.4']):
            raise ModelConfigurationError('Argument repair and capture are restricted to the v2.5.4 intent candidate')
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
                               response_diagnostics=self.response_diagnostics,
                               repair_tool_arguments=self.repair_tool_arguments,
                               scalar_only=_RETRY_ARGUMENTS.get(),
                               argument_diagnostic_sink=self.argument_diagnostic_sink,
                               save_failed_arguments=self.save_failed_arguments)
