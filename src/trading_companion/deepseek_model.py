import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite

from trading_companion.model import (
    ChatMessage,
    ModelCallError,
    ModelConfigurationError,
    ModelResponse,
    TokenUsage,
    validate_messages,
)
from trading_companion.turn_control import request_timeout
from trading_companion.turn_trace import trace_provider_request


DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
ANSWER_OUTPUT_TOKENS = 4096
TOOL_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 45.0
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ModelConfigurationError("DEEPSEEK_MODEL must be a non-empty model identifier without whitespace")
        if (
            not isinstance(self.api_key, str) or not self.api_key.strip()
            or any(c.isspace() for c in self.api_key)
            or not self.api_key.isascii() or not self.api_key.isprintable()
        ):
            raise ModelConfigurationError("DEEPSEEK_API_KEY must be a non-empty ASCII credential without whitespace")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 120 or not isfinite(self.timeout_seconds)
        ):
            raise ModelConfigurationError("timeout_seconds must be finite and between 0 and 120 seconds")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 8192:
            raise ModelConfigurationError("max_output_tokens must be an integer between 1 and 8192")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DeepSeekConfig":
        """Read only the documented variables when explicitly requested."""
        values = os.environ if environ is None else environ
        return cls(model=values.get("DEEPSEEK_MODEL", ""), api_key=values.get("DEEPSEEK_API_KEY", ""))


def parse_chat_response(payload: object, requested_model: str) -> ModelResponse:
    """Validate the text-only subset of the compatible Chat Completions shape."""
    invalid = ModelCallError("invalid_response", "Model returned an invalid text response")
    if not isinstance(payload, dict):
        raise invalid
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise invalid
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise invalid
    if message.get("tool_calls") or message.get("function_call"):
        raise ModelCallError("unsupported_response", "Tool calls are not supported by this text-only adapter yet")
    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise ModelCallError("refused", "The model did not provide an answer to this request")
    if choice.get("finish_reason") == "length":
        raise ModelCallError("incomplete_response", "Model output reached its limit; no complete answer is available")
    if choice.get("finish_reason") != "stop":
        raise ModelCallError("unsupported_response", "Model did not finish with a supported text response")
    content = message.get("content")
    model = payload.get("model")
    if not isinstance(content, str) or not content.strip() or not isinstance(model, str) or not model.strip():
        raise invalid
    usage = None
    if payload.get("usage") is not None:
        raw_usage = payload["usage"]
        if not isinstance(raw_usage, dict):
            raise invalid
        usage = TokenUsage(**{
            key: raw_usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        })
    return ModelResponse(
        provider="deepseek", requested_model=requested_model, model=model, text=content, usage=usage,
    )


def _post_chat(config: DeepSeekConfig, body: dict[str, object]) -> object:
    """Share fixed-endpoint transport without mixing text and tool protocols."""
    try:
        import requests
    except ImportError:
        raise ModelConfigurationError("Install the model extra to use DeepSeek") from None

    try:
        # Requests has no automatic retries by default; redirects are refused.
        timeout = request_timeout(config.timeout_seconds)
        trace_provider_request("deepseek", config.model)
        response = requests.post(
            DEEPSEEK_CHAT_URL,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
        try:
            status = response.status_code
            if status in (401, 403):
                raise ModelCallError("authentication_error", "Model authentication or access failed; check local credentials")
            if status == 429:
                raise ModelCallError("rate_limited", "Model request was rate-limited; retry later if appropriate")
            if status != 200:
                raise ModelCallError("provider_error", f"Model provider returned HTTP {status}; no automatic retry was made")
            payload = response.json()
        finally:
            response.close()
        return payload
    except requests.exceptions.Timeout:
        raise ModelCallError("timeout", "Model request timed out") from None
    except requests.exceptions.JSONDecodeError:
        raise ModelCallError("invalid_response", "Model provider returned invalid JSON") from None
    except requests.exceptions.RequestException:
        raise ModelCallError("connection_error", "Could not complete the model request; check the connection") from None


@dataclass(frozen=True)
class DeepSeekTextModel:
    config: DeepSeekConfig

    def complete(self, messages: Sequence[ChatMessage]) -> ModelResponse:
        validated = validate_messages(messages)
        payload = _post_chat(self.config, {
            "model": self.config.model,
            "messages": [{"role": message.role, "content": message.content} for message in validated],
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            # Keep this small text-only layer independent of thinking defaults.
            "thinking": {"type": "disabled"},
        })
        return parse_chat_response(payload, self.config.model)
