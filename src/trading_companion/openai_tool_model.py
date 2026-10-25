"""Fixed-endpoint OpenAI Chat Completions adapter for the existing read-only tool protocol.

The configuration owner must select a model supporting Chat Completions,
developer messages and function tools. A model name alone does not establish
those capabilities. ``max_completion_tokens`` includes reasoning tokens when
the selected model uses them; reaching the cap produces an incomplete result.
No tool execution, source selection, credentials or conversation state is
controlled by provider output. ChatGPT subscriptions are separate from API access.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import isfinite
import os

from trading_companion.deepseek_tool_model import _parse_response, _wire_message
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.tool_model import (
    MAX_METADATA_CHARACTERS, ToolDefinition, ToolMessage, ToolModelResponse, validate_tool_messages,
)
from trading_companion.turn_control import checkpoint, request_timeout


OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    max_output_tokens: int = 2048

    def __post_init__(self):
        if (type(self.model) is not str or not 1 <= len(self.model) <= MAX_METADATA_CHARACTERS
                or not self.model.isascii() or not self.model.isprintable()
                or any(character.isspace() for character in self.model)):
            raise ModelConfigurationError("OPENAI_MODEL must be an explicit bounded model identifier without whitespace")
        if (type(self.api_key) is not str or not self.api_key or not self.api_key.isascii()
                or not self.api_key.isprintable() or any(character.isspace() for character in self.api_key)):
            raise ModelConfigurationError("OPENAI_API_KEY must be a non-empty ASCII credential without whitespace")
        if (type(self.timeout_seconds) not in (int, float) or not isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 30):
            raise ModelConfigurationError("timeout_seconds must be finite and between 0 and 30 seconds")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 4096:
            raise ModelConfigurationError("max_output_tokens must be an integer between 1 and 4096")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "OpenAIConfig":
        """Read only the two documented variables, only upon an explicit call."""
        values = os.environ if environ is None else environ
        return cls(model=values.get("OPENAI_MODEL", ""), api_key=values.get("OPENAI_API_KEY", ""))


def _post_chat(config, body):
    checkpoint()
    try:
        import requests
    except ImportError:
        raise ModelConfigurationError("Install the model extra to use OpenAI") from None
    try:
        # requests.post has no automatic retries; redirecting bearer credentials is forbidden.
        response = requests.post(OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {config.api_key}"}, json=body,
            timeout=request_timeout(config.timeout_seconds), allow_redirects=False)
        try:
            status = response.status_code
            if status in (401, 403):
                raise ModelCallError("authentication_error", "Model authentication or access failed; check local credentials")
            if status == 429:
                raise ModelCallError("rate_limited", "Model request was rate-limited; retry later if appropriate")
            if type(status) is not int or status != 200:
                raise ModelCallError("provider_error", "Model provider did not complete the request; no automatic retry was made")
            try:
                return response.json()
            except (ValueError, RecursionError, OverflowError):
                raise ModelCallError("invalid_response", "Model provider returned invalid JSON") from None
        finally:
            response.close()
    except requests.exceptions.Timeout:
        raise ModelCallError("timeout", "Model request timed out; no automatic retry was made") from None
    except requests.exceptions.RequestException:
        raise ModelCallError("connection_error", "Could not complete the model request; check the connection") from None


@dataclass(frozen=True)
class OpenAIToolModel:
    """Translate provider wire format while preserving the application's existing limits.

    Provider responses share the strict validated Chat Completions subset with
    the DeepSeek adapter. The returned metadata always identifies OpenAI.
    Late-response metering and cancellation are handled by ControlledToolModel.
    """

    config: OpenAIConfig

    def __post_init__(self):
        if type(self.config) is not OpenAIConfig:
            raise ModelConfigurationError("Tool model requires validated OpenAI configuration")

    def complete(self, messages: Sequence[ToolMessage], *,
                 tools: Sequence[ToolDefinition]) -> ToolModelResponse:
        history, definitions, seen_ids = validate_tool_messages(messages, tools=tools)
        wire_messages = []
        for message in history:
            wire = _wire_message(message)
            if message.role == "system":
                wire["role"] = "developer"
            wire_messages.append(wire)
        body = {"model": self.config.model, "messages": wire_messages,
                "max_completion_tokens": self.config.max_output_tokens, "store": False, "stream": False}
        if definitions:
            body["tools"] = [{"type": "function", "function": tool.to_dict()} for tool in definitions]
            body["tool_choice"] = "auto"
        payload = _post_chat(self.config, body)
        parsed = _parse_response(payload, self.config.model,
                                 frozenset(tool.name for tool in definitions), seen_ids)
        return replace(parsed, provider="openai")
