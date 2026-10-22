"""One-shot cooperative turn limits; never a hard remote-cancellation or billing cap."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Callable

from trading_companion.model import ModelConfigurationError, TokenUsage


@dataclass(frozen=True)
class TurnLimits:
    timeout_seconds: float = 90.0
    model_calls: int = 3
    analyses: int = 1
    observed_tokens: int = 24000

    def __post_init__(self):
        if (type(self.timeout_seconds) not in (int, float) or not isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 90):
            raise ModelConfigurationError("Turn duration must be positive and at most 90 seconds")
        for value, maximum in ((self.model_calls, 3), (self.analyses, 1)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ModelConfigurationError("Turn call limits may tighten, not expand, product limits")
        if type(self.observed_tokens) is not int or not 1 <= self.observed_tokens <= 24000:
            raise ModelConfigurationError("Observed-token threshold must be between 1 and 24000")


class TurnStopped(Exception):
    """Kept separate from model/source errors so inner error handlers cannot swallow it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TurnControl:
    """Trusted application object; cancellation is thread-safe, execution is synchronous."""

    def __init__(self, limits: TurnLimits | None = None, *, clock: Callable[[], float] = monotonic):
        self.limits = TurnLimits() if limits is None else limits
        if type(self.limits) is not TurnLimits or not callable(clock):
            raise ModelConfigurationError("Use trusted validated turn limits and a monotonic clock")
        self._clock = clock
        self._lock = Lock()
        self._started_at = None
        self._last_time = None
        self._finished = False
        self._elapsed = 0.0
        self._stop_code = None
        self._model_calls = self._analyses = self._responses = 0
        self._complete_usage = 0
        self._observed_tokens = 0
        self._reported = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def _now(self):
        value = self._clock()
        if (type(value) not in (int, float) or not isfinite(value)
                or (self._last_time is not None and value < self._last_time)):
            raise ModelConfigurationError("Turn clock must return finite nondecreasing seconds")
        self._last_time = value
        return value

    def start(self):
        with self._lock:
            if self._started_at is not None or self._finished:
                raise ModelConfigurationError("A turn control is single-use; create one per message")
            self._started_at = self._now()

    def _check(self):
        if self._started_at is None or self._finished:
            raise ModelConfigurationError("Turn control is not active")
        self._elapsed = self._now() - self._started_at
        if self._stop_code is None and self._elapsed >= self.limits.timeout_seconds:
            self._stop_code = "turn_deadline_exceeded"
        if self._stop_code is not None:
            raise TurnStopped(self._stop_code)

    def checkpoint(self):
        with self._lock:
            self._check()

    def cancel(self) -> bool:
        """Signal only; cannot terminate an HTTP request already executing remotely."""
        with self._lock:
            if self._finished or self._stop_code is not None:
                return False
            self._stop_code = "turn_cancelled"
            return True

    def timeout_for(self, per_request: float) -> float:
        with self._lock:
            self._check()
            return min(per_request, self.limits.timeout_seconds - self._elapsed)

    def reserve_model(self):
        with self._lock:
            self._check()
            if self._model_calls >= self.limits.model_calls:
                self._stop_code = "model_call_budget_exhausted"
                raise TurnStopped(self._stop_code)
            self._model_calls += 1

    def reserve_analysis(self):
        with self._lock:
            self._check()
            if self._analyses >= self.limits.analyses:
                self._stop_code = "analysis_budget_exhausted"
                raise TurnStopped(self._stop_code)
            self._analyses += 1

    def observe_response(self, usage: TokenUsage | None):
        """Record even late responses, before rejecting their actionable content."""
        with self._lock:
            if self._started_at is None or self._finished:
                raise ModelConfigurationError("Turn control is not active")
            self._responses += 1
            if type(usage) is TokenUsage:
                for name, old in self._reported.items():
                    value = getattr(usage, name)
                    if value is not None:
                        self._reported[name] = (old or 0) + value
                parts = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
                self._observed_tokens += max(usage.total_tokens or 0, parts)
                if (None not in (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
                        and usage.total_tokens == parts):
                    self._complete_usage += 1
            # Cancellation/deadline take precedence over an observed-token threshold.
            self._check()
            if self._observed_tokens >= self.limits.observed_tokens:
                self._stop_code = "token_budget_exhausted"
                raise TurnStopped(self._stop_code)

    def finish(self, commit: Callable[[bool], None] | None = None) -> str | None:
        """Linearize final state publication against cancel(); callback must be bounded local work."""
        with self._lock:
            try:
                self._check()
            except TurnStopped:
                pass
            if commit is not None:
                commit(self._stop_code is not None)
            self._finished = True
            return self._stop_code

    def to_dict(self) -> dict[str, object]:
        """Metadata only: no prompts, credentials, provider errors or report contents."""
        with self._lock:
            return {
                "version": "turn-control-v1",
                "state": "finished" if self._finished else "active" if self._started_at is not None else "not_started",
                "stop_code": self._stop_code, "elapsed_ms": round(self._elapsed * 1000),
                "limits": {"timeout_seconds": self.limits.timeout_seconds, "model_calls": self.limits.model_calls,
                           "analyses": self.limits.analyses, "observed_tokens": self.limits.observed_tokens},
                "model_calls_attempted": self._model_calls, "analyses_used": self._analyses,
                "responses_received": self._responses, "observed_tokens": self._observed_tokens,
                "reported_usage": dict(self._reported),
                "responses_with_complete_consistent_usage": self._complete_usage,
                "usage_complete_for_attempts": self._complete_usage == self._model_calls,
                "token_policy": "observed_threshold_not_preflight_or_billing_cap",
                "deadline_policy": "cooperative_checkpoints_not_hard_interrupt",
            }


_ACTIVE: ContextVar[TurnControl | None] = ContextVar("research_turn_control", default=None)


@contextmanager
def bind_turn(control: TurnControl):
    token = _ACTIVE.set(control)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def checkpoint():
    control = _ACTIVE.get()
    if control is not None:
        control.checkpoint()


def request_timeout(per_request: float) -> float:
    control = _ACTIVE.get()
    return per_request if control is None else control.timeout_for(per_request)


def reserve_analysis():
    control = _ACTIVE.get()
    if control is not None:
        control.reserve_analysis()


class ControlledToolModel:
    """Meter all model paths without changing their protocol or granting tools authority."""

    def __init__(self, delegate, control: TurnControl):
        self.delegate, self.control = delegate, control

    def complete(self, messages, *, tools):
        self.control.reserve_model()
        try:
            response = self.delegate.complete(messages, tools=tools)
        except Exception:
            self.control.checkpoint()
            raise
        self.control.observe_response(response.usage)
        return response
