"""Offline tool-message protocol; declarations and proposals grant no execution rights."""

from collections.abc import Sequence
from dataclasses import dataclass, field
import json
from math import isfinite
import re
from typing import Protocol

from trading_companion.model import (
    ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage,
)


MAX_MESSAGES = 20
MAX_USER_CHARACTERS = 2000
MAX_TOTAL_CHARACTERS = 20000
MAX_JSON_CHARACTERS = 10000
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 2000
MAX_TOOLS = 12
MAX_PARALLEL_CALLS = 8
MAX_SCRIPT_RESPONSES = 100
MAX_METADATA_CHARACTERS = 128
_ABSENT = object()


def _identifier(value: object, *, tool_name: bool = False) -> str:
    pattern = r"[A-Za-z_][A-Za-z0-9_-]{0,63}" if tool_name else r"[A-Za-z0-9_-]{1,64}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ModelRequestError("Invalid protocol identifier")
    return value


def _text(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (not isinstance(value, str) or len(value) > MAX_TOTAL_CHARACTERS
            or not value.strip()):
        raise ModelRequestError("Protocol text must be nonempty and within its length limit")
    return value


def _metadata(value: object) -> str:
    """Model/provider identifiers are opaque metadata, not tool-call identifiers."""
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_METADATA_CHARACTERS
            or any(character.isspace() or not character.isprintable() for character in value)):
        raise ModelRequestError("Invalid bounded model metadata")
    return value


def _json_string_length(value: str) -> int:
    if len(value) > MAX_JSON_CHARACTERS:
        raise ModelRequestError("JSON string exceeds its character limit")
    length = 2
    for character in value:
        code = ord(character)
        if character in ('"', "\\", "\b", "\f", "\n", "\r", "\t"):
            length += 2
        elif code < 32 or 127 <= code <= 65535:
            length += 6
        elif code > 65535:
            length += 12
        else:
            length += 1
    return length


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelRequestError("Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_constant(value):
    raise ModelRequestError("Nonfinite JSON numbers are not allowed")


def _canonical_object(payload: object) -> str:
    """Validate bounded JSON and retain an immutable, detached representation."""
    try:
        if isinstance(payload, str):
            if len(payload) > MAX_JSON_CHARACTERS:
                raise ModelRequestError("JSON object exceeds its length limit")
            payload = json.loads(payload, object_pairs_hook=_unique_pairs,
                                 parse_constant=_reject_constant)
        if type(payload) is not dict:
            raise ModelRequestError("Protocol payload must be a JSON object")
        stack = [(payload, 0, frozenset())]
        count = 0
        characters = 0
        while stack:
            value, depth, ancestors = stack.pop()
            count += 1
            if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
                raise ModelRequestError("JSON structure exceeds protocol limits")
            if type(value) in (dict, list):
                if id(value) in ancestors:
                    raise ModelRequestError("Cyclic JSON structures are not allowed")
                descendants = ancestors | {id(value)}
                children = len(value)
                key_nodes = children if type(value) is dict else 0
                if count + len(stack) + children + key_nodes > MAX_JSON_NODES:
                    raise ModelRequestError("JSON structure exceeds protocol limits")
                characters += 2 + max(0, children - 1)
                if type(value) is dict:
                    if any(type(key) is not str for key in value):
                        raise ModelRequestError("JSON object keys must be strings")
                    count += len(value)
                    characters += children
                    for key in value:
                        characters += _json_string_length(key)
                        if characters > MAX_JSON_CHARACTERS:
                            raise ModelRequestError("JSON object exceeds its length limit")
                    stack.extend((item, depth + 1, descendants) for item in value.values())
                else:
                    stack.extend((item, depth + 1, descendants) for item in value)
            elif type(value) is str:
                characters += _json_string_length(value)
            elif value is None or type(value) is bool:
                characters += 4 if value is None or value else 5
            elif type(value) is int:
                if value.bit_length() > MAX_JSON_CHARACTERS * 4:
                    raise ModelRequestError("JSON integer exceeds its character limit")
                characters += len(str(value))
            elif type(value) is float and isfinite(value):
                characters += len(repr(value))
            else:
                raise ModelRequestError("Unsupported JSON value")
            if characters > MAX_JSON_CHARACTERS:
                raise ModelRequestError("JSON object exceeds its length limit")
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                             sort_keys=True, separators=(",", ":"))
        if len(encoded) > MAX_JSON_CHARACTERS:
            raise ModelRequestError("JSON object exceeds its length limit")
        return encoded
    except (ValueError, TypeError, RecursionError, OverflowError):
        # Never forward a parser error or its excerpt of the original payload.
        raise ModelRequestError("Invalid or oversized JSON object payload") from None


def _schema(schema: object) -> str:
    """Check declaration shape only, not instance validity or business permissions."""
    encoded = _canonical_object(schema)
    value = json.loads(encoded)
    allowed = {"type", "properties", "required", "additionalProperties", "description"}
    if value.keys() - allowed or value.get("type") != "object":
        raise ModelRequestError("Tool schema must declare the supported object shape")
    properties = value.get("properties", {})
    required = value.get("required", [])
    if (type(properties) is not dict or type(required) is not list
            or any(type(name) is not str for name in required)
            or len(set(required)) != len(required) or set(required) - properties.keys()
            or any(type(item) is not dict for item in properties.values())):
        raise ModelRequestError("Tool schema properties and required fields are invalid")
    if "additionalProperties" in value and type(value["additionalProperties"]) is not bool:
        raise ModelRequestError("Tool schema additionalProperties must be a boolean")
    if "description" in value:
        _text(value["description"])
    return encoded


@dataclass(frozen=True, init=False)
class ToolDefinition:
    name: str = field(repr=False)
    description: str = field(repr=False)
    _parameters_json: str = field(repr=False)

    def __init__(self, name: str, description: str, parameters: object):
        object.__setattr__(self, "name", _identifier(name, tool_name=True))
        object.__setattr__(self, "description", _text(description))
        object.__setattr__(self, "_parameters_json", _schema(parameters))

    @property
    def parameters(self) -> dict:
        return json.loads(self._parameters_json)

    def to_dict(self) -> dict[str, object]:
        """Explicit serialization includes the supplied description and schema."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass(frozen=True, init=False)
class ToolCall:
    call_id: str = field(repr=False)
    name: str = field(repr=False)
    _arguments_json: str = field(repr=False)

    def __init__(self, call_id: str, name: str, arguments: object):
        object.__setattr__(self, "call_id", _identifier(call_id))
        object.__setattr__(self, "name", _identifier(name, tool_name=True))
        object.__setattr__(self, "_arguments_json", _canonical_object(arguments))

    @property
    def arguments(self) -> dict:
        return json.loads(self._arguments_json)

    def to_dict(self) -> dict[str, object]:
        """Explicit serialization includes the supplied arguments."""
        return {"call_id": self.call_id, "name": self.name, "arguments": self.arguments}


def _calls(calls: object) -> tuple[ToolCall, ...]:
    if not isinstance(calls, (list, tuple)) or len(calls) > MAX_PARALLEL_CALLS:
        raise ModelRequestError("Invalid tool-call collection")
    if any(type(call) is not ToolCall for call in calls):
        raise ModelRequestError("Tool calls must use the protocol type")
    copied = tuple(ToolCall(call.call_id, call.name, call.arguments) for call in calls)
    if len({call.call_id for call in copied}) != len(copied):
        raise ModelRequestError("Tool-call identifiers must be unique")
    return copied


@dataclass(frozen=True, init=False)
class ToolMessage:
    role: str
    content: str | None = field(repr=False)
    tool_calls: tuple[ToolCall, ...] = field(repr=False)
    call_id: str | None = field(repr=False)
    name: str | None = field(repr=False)
    _result_json: str | None = field(repr=False)

    def __init__(self, role: str, content: str | None = None, *, tool_calls=(),
                 call_id: str | None = None, name: str | None = None, result=_ABSENT):
        if role not in ("system", "user", "assistant", "tool"):
            raise ModelRequestError("Unsupported tool-protocol message role")
        calls = _calls(tool_calls)
        if role == "tool":
            if content is not None or calls or result is _ABSENT:
                raise ModelRequestError("Tool results require structured payloads only")
            call_id, name = _identifier(call_id), _identifier(name, tool_name=True)
            result_json = _canonical_object(result)
        else:
            if call_id is not None or name is not None or result is not _ABSENT:
                raise ModelRequestError("Only tool-result messages may contain result fields")
            if role != "assistant" and calls:
                raise ModelRequestError("Only assistant messages may propose tool calls")
            content = _text(content, optional=bool(calls))
            if role == "user" and len(content) > MAX_USER_CHARACTERS:
                raise ModelRequestError("User message exceeds its character limit")
            result_json = None
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "tool_calls", calls)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "_result_json", result_json)

    @property
    def result(self) -> dict | None:
        return json.loads(self._result_json) if self._result_json is not None else None

    def to_dict(self) -> dict[str, object]:
        """Explicit serialization includes message text, arguments, and tool results."""
        result = {"role": self.role}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.role == "tool":
            result.update(call_id=self.call_id, name=self.name, result=self.result)
        return result


@dataclass(frozen=True)
class ToolModelResponse:
    status: str
    text: str | None = field(default=None, repr=False)
    tool_calls: tuple[ToolCall, ...] = field(default=(), repr=False)
    provider: str = field(default="fake", repr=False)
    requested_model: str = field(default="fake-tools-v1", repr=False)
    model: str = field(default="fake-tools-v1", repr=False)
    usage: TokenUsage | None = field(default=None, repr=False)
    is_mock: bool = True

    def __post_init__(self):
        try:
            if self.status not in ("complete", "tool_calls", "refused", "incomplete"):
                raise ModelRequestError("Invalid response status")
            calls = _calls(self.tool_calls)
            if self.status == "tool_calls":
                if not calls:
                    raise ModelRequestError("Tool-call responses need proposals")
                _text(self.text, optional=True)
            else:
                if calls:
                    raise ModelRequestError("This response status cannot contain tool calls")
                _text(self.text, optional=self.status == "incomplete")
            for value in (self.provider, self.requested_model, self.model):
                _metadata(value)
            if type(self.is_mock) is not bool or (self.usage is not None and type(self.usage) is not TokenUsage):
                raise ModelRequestError("Invalid response metadata")
            if len(self.text or "") + sum(len(call._arguments_json) + len(call.call_id) + len(call.name)
                                           for call in calls) > MAX_TOTAL_CHARACTERS:
                raise ModelRequestError("Response content exceeds its character limit")
            object.__setattr__(self, "tool_calls", calls)
        except ModelRequestError:
            raise ModelCallError("invalid_response", "Model returned an invalid tool-protocol response") from None

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    def as_message(self) -> ToolMessage:
        if self.status not in ("complete", "tool_calls"):
            raise ModelCallError("noncomplete_response", "Refused or incomplete output cannot become a completed message")
        return ToolMessage("assistant", self.text, tool_calls=self.tool_calls)

    def to_dict(self) -> dict[str, object]:
        """Explicit serialization includes the response text and proposed arguments."""
        return {"status": self.status, "text": self.text,
                "tool_calls": [call.to_dict() for call in self.tool_calls],
                "provider": self.provider, "requested_model": self.requested_model,
                "model": self.model, "is_mock": self.is_mock,
                "usage": ({"prompt_tokens": self.usage.prompt_tokens,
                           "completion_tokens": self.usage.completion_tokens,
                           "total_tokens": self.usage.total_tokens} if self.usage else None)}


def _validate_tools(tools):
    if not isinstance(tools, (list, tuple)) or len(tools) > MAX_TOOLS:
        raise ModelRequestError("Invalid tool-definition collection")
    if any(type(tool) is not ToolDefinition for tool in tools):
        raise ModelRequestError("Tools must use the declaration protocol")
    copied = tuple(ToolDefinition(tool.name, tool.description, tool.parameters) for tool in tools)
    if len({tool.name for tool in copied}) != len(copied):
        raise ModelRequestError("Tool names must be unique")
    return copied


def validate_tool_messages(messages: Sequence[ToolMessage], *, tools: Sequence[ToolDefinition]):
    """Require a complete history, with every proposal answered exactly once by ID."""
    definitions = _validate_tools(tools)
    names = {tool.name for tool in definitions}
    if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ModelRequestError("Provide between 1 and 20 protocol messages")
    if any(type(message) is not ToolMessage for message in messages):
        raise ModelRequestError("Messages must use the tool protocol")
    snapshots = tuple(
        ToolMessage("tool", call_id=message.call_id, name=message.name, result=message.result)
        if message.role == "tool" else ToolMessage(message.role, message.content,
                                                   tool_calls=message.tool_calls)
        for message in messages
    )
    total = sum(len(json.dumps(tool.to_dict(), ensure_ascii=True)) for tool in definitions)
    total += sum(len(json.dumps(message.to_dict(), ensure_ascii=True)) for message in snapshots)
    if total > MAX_TOTAL_CHARACTERS:
        raise ModelRequestError("Complete tool request exceeds its character limit")
    pending = {}
    seen_ids = set()
    expected = "user"
    for index, message in enumerate(snapshots):
        if message.role == "system":
            if index != 0 or pending:
                raise ModelRequestError("System messages are allowed only at the beginning")
            continue
        if pending:
            if message.role != "tool" or message.call_id not in pending:
                raise ModelRequestError("All pending tool calls require their matching results first")
            if pending[message.call_id] != message.name:
                raise ModelRequestError("Tool result name does not match its proposal")
            del pending[message.call_id]
            continue
        if message.role == "tool":
            raise ModelRequestError("Orphan or duplicate tool result")
        if message.role != expected:
            raise ModelRequestError("Invalid user and assistant message order")
        if message.role == "user":
            expected = "assistant"
        elif message.tool_calls:
            for call in message.tool_calls:
                if call.call_id in seen_ids or call.name not in names:
                    raise ModelRequestError("Repeated tool-call ID or undeclared tool")
                seen_ids.add(call.call_id)
                pending[call.call_id] = call.name
            expected = "assistant"
        else:
            expected = "user"
    if pending:
        raise ModelRequestError("Cannot request a model response with unresolved tool calls")
    if snapshots[-1].role not in ("user", "tool"):
        raise ModelRequestError("Model requests must end in a user message or completed tool results")
    return snapshots, definitions, frozenset(seen_ids)


class ToolCapableModel(Protocol):
    def complete(self, messages: Sequence[ToolMessage], *,
                 tools: Sequence[ToolDefinition]) -> ToolModelResponse: ...


@dataclass(init=False)
class FakeToolModel:
    """A finite offline script; it never routes, evaluates, or executes a proposal."""

    _responses: tuple = field(repr=False)
    _position: int = 0

    def __init__(self, responses):
        if not isinstance(responses, (list, tuple)) or len(responses) > MAX_SCRIPT_RESPONSES:
            raise ModelConfigurationError("Fake tool model needs a finite response script")
        self._responses = tuple(responses)
        self._position = 0

    @property
    def responses_consumed(self) -> int:
        return self._position

    def complete(self, messages: Sequence[ToolMessage], *,
                 tools: Sequence[ToolDefinition]) -> ToolModelResponse:
        _, definitions, seen_ids = validate_tool_messages(messages, tools=tools)
        if self._position >= len(self._responses):
            raise ModelCallError("script_exhausted", "Offline fake response script is exhausted")
        proposed = self._responses[self._position]
        if type(proposed) is not ToolModelResponse:
            raise ModelCallError("invalid_response", "Fake script contains an invalid protocol response")
        response = ToolModelResponse(proposed.status, proposed.text, proposed.tool_calls,
                                     provider="fake", requested_model="fake-tools-v1",
                                     model="fake-tools-v1", usage=None, is_mock=True)
        allowed = {tool.name for tool in definitions}
        if any(call.name not in allowed for call in response.tool_calls):
            raise ModelCallError("unknown_tool", "Model proposed a tool outside the declared set")
        if any(call.call_id in seen_ids for call in response.tool_calls):
            raise ModelCallError("invalid_response", "Model reused a previous tool-call identifier")
        self._position += 1
        return response
