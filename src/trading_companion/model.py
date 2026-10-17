from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol


class ModelConfigurationError(ValueError):
    """Local model configuration must be corrected before sending a request."""


class ModelRequestError(ValueError):
    """The caller supplied an invalid text request."""


class ModelCallError(Exception):
    """A sanitized model failure; never include a provider body or credential."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ModelRequestError("Only system, user and assistant text messages are supported")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ModelRequestError("Message content must be non-empty text")


def validate_messages(messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    """Apply the same local limits to fake and remote providers."""
    if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 20:
        raise ModelRequestError("Provide between 1 and 20 messages")
    if any(not isinstance(message, ChatMessage) for message in messages):
        raise ModelRequestError("Every message must be a ChatMessage")
    if messages[-1].role != "user":
        raise ModelRequestError("The last message must be a user message")
    if any(len(message.content) > 2000 for message in messages if message.role == "user"):
        raise ModelRequestError("Each user message must contain at most 2000 characters")
    if sum(len(message.content) for message in messages) > 20000:
        raise ModelRequestError("Total message content exceeds 20000 characters")
    return tuple(messages)


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ModelCallError("invalid_response", "Model returned invalid token usage")


@dataclass(frozen=True)
class ModelResponse:
    provider: str
    requested_model: str
    model: str
    text: str = field(repr=False)
    usage: TokenUsage | None = None
    is_mock: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TextModel(Protocol):
    """Return a complete text answer or raise a classified failure."""

    def complete(self, messages: Sequence[ChatMessage]) -> ModelResponse: ...


@dataclass(frozen=True)
class FakeTextModel:
    """A deterministic offline test double, not an actual language model."""

    response_text: str = "Offline mock response. No model or market data was requested."

    def complete(self, messages: Sequence[ChatMessage]) -> ModelResponse:
        validate_messages(messages)
        if not isinstance(self.response_text, str) or not self.response_text.strip():
            raise ModelConfigurationError("Fake model response must be non-empty text")
        return ModelResponse(
            provider="fake", requested_model="fake-v1", model="fake-v1",
            text=self.response_text, is_mock=True,
        )
