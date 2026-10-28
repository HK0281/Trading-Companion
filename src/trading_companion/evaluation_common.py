"""Shared evaluation accounting; forwarded calls include failures, missing usage stays unknown."""

from threading import Lock
from dataclasses import asdict
import re

from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.tool_model import ToolModelResponse


class EvaluationModelMeter:
    """One allowance shared across delegates/sessions, charged before forwarding."""

    def __init__(self, delegate, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("Use a positive evaluation call allowance")
        self.delegate, self.limit = delegate, limit
        self.usage = []
        self.ledger = []
        self.attempts = self.blocked = 0
        self._lock = Lock()

    @property
    def forwarded(self):
        return len(self.usage)

    def complete(self, messages, *, tools):
        with self._lock:
            self.attempts += 1
            if len(self.usage) >= self.limit:
                self.blocked += 1
                raise ModelCallError("evaluation_budget_exhausted", "Evaluation model budget exhausted")
            index = len(self.usage)
            self.usage.append(None)
            self.ledger.append({"provider": None, "requested_model": None, "model": None,
                                "usage": dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))})
        from trading_companion.turn_trace import trace_model_identity
        trace_model_identity(self.delegate)
        response = self.delegate.complete(messages, tools=tools)
        if type(response) is ToolModelResponse:
            with self._lock:
                self.usage[index] = response.usage
                self.ledger[index] = {name: value if type(value := getattr(response, name)) is str
                                      and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) else None
                                      for name in ("provider", "requested_model", "model")}
                self.ledger[index]["usage"] = asdict(response.usage) if response.usage is not None else dict.fromkeys(
                    ("prompt_tokens", "completion_tokens", "total_tokens"))
        return response


def reported_usage(values):
    """Keep the company evaluator's exact field names and completeness definition."""
    totals = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        reported = [getattr(value, name) for value in values
                    if type(value) is TokenUsage and getattr(value, name) is not None]
        totals["reported_" + name] = sum(reported) if reported else None
    complete = sum(type(value) is TokenUsage
                   and None not in (value.prompt_tokens, value.completion_tokens, value.total_tokens)
                   and value.total_tokens == value.prompt_tokens + value.completion_tokens for value in values)
    return {**totals, "requests_with_complete_usage": complete,
            "requests_without_complete_usage": len(values) - complete,
            "complete_for_forwarded_requests": bool(values) and complete == len(values),
            "scope": "provider_reported_values_only_not_cost_or_total_billed_usage"}
