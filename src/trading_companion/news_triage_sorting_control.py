"""Bounded classifier workers and per-request settlement, separate Jev token budget."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import asdict
from threading import BoundedSemaphore, Event, Lock
from time import monotonic

from trading_companion.model import TokenUsage
from trading_companion.turn_control import TurnStopped
from trading_companion.typesafe_jev import ClassifierError

JEV_TOKEN_LIMIT = 40000
DEEPSEEK_TOKEN_LIMIT = 8000
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="headline-triage")
_SLOTS = BoundedSemaphore(3)


def submit_classifier(function):
    """Never queue behind another turn's unfinished remote work."""
    if not _SLOTS.acquire(blocking=False):
        return None
    context = copy_context()
    def run():
        try:
            return context.run(function)
        finally:
            _SLOTS.release()
    try:
        return _POOL.submit(run)
    except BaseException:
        _SLOTS.release()
        raise


def token_usage(metadata):
    raw = metadata.get("usage", {}) if type(metadata) is dict else {}
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    if (type(raw) is not dict or any(type(raw.get(k)) is not int or not 0 <= raw[k] <= 10**12 for k in names)
            or raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]):
        return None
    return TokenUsage(**{k: raw[k] for k in names})


class SortingRound:
    """One round owns late settlement; it never acquires another turn's control."""

    def __init__(self, control, *, clock=monotonic, sink=None, attempt_guard=None):
        self.control, self.clock, self.sink = control, clock, sink
        self.attempt_guard = attempt_guard
        self.started, self.lock, self.stop = clock(), Lock(), Event()
        self.finished_at = None
        self.calls, self.reason, self.covered, self.candidates = [], "not_run", 0, 0
        self.promoted, self.capacity_fallbacks = 0, 0
        self.deadline = self.started + min(4, control.timeout_for(4))

    def check(self, deadline):
        self.control.checkpoint()
        if self.stop.is_set() or self.clock() >= min(deadline, self.deadline):
            raise ClassifierError("triage_timeout")

    def begin(self, provider, requested_model):
        with self.lock:
            if self.stop.is_set():
                raise ClassifierError("triage_timeout")
            if sum(c["provider"] == provider for c in self.calls) >= (10 if provider == "jev" else 1):
                raise ClassifierError("triage_call_budget")
            if self.attempt_guard is not None:
                self.attempt_guard(provider)
            call = dict(id=len(self.calls)+1, provider=provider, requested_model=requested_model,
                        model=None, usage=None, status="pending", elapsed_ms=None, late=False)
            self.calls.append(call)
            return call

    def settle(self, call, metadata, status, started):
        usage = token_usage(metadata)
        with self.lock:
            call.update(model=metadata.get("model"), usage=asdict(usage) if usage else None,
                        status=status if usage else "usage_unknown", late=self.stop.is_set(),
                        elapsed_ms=round((self.clock()-started)*1000))
            if usage is None:
                self.stop.set()
            total = sum(c["usage"]["total_tokens"] for c in self.calls
                        if c["provider"] == call["provider"] and c["usage"] is not None)
            if total >= (JEV_TOKEN_LIMIT if call["provider"] == "jev" else DEEPSEEK_TOKEN_LIMIT):
                self.stop.set()
        # Jev never contributes to the answer model's 24,000-token counter.
        try:
            if call["provider"] == "deepseek":
                self.control.observe_optional_response(usage)
        finally:
            self.publish()
        if usage is None:
            raise ClassifierError("usage_unknown")
        if self.stop.is_set():
            raise ClassifierError("triage_budget_or_timeout")

    def finish(self, reason):
        with self.lock:
            self.reason = reason
            self.finished_at = self.clock()
            self.stop.set()
        self.publish()

    def snapshot(self):
        from copy import deepcopy
        with self.lock:
            return dict(version="news-triage-sorting-v1", reason=self.reason,
                candidates=self.candidates, covered=self.covered, promoted=self.promoted,
                capacity_fallbacks=self.capacity_fallbacks,
                elapsed_ms=round(((self.finished_at if self.finished_at is not None else self.clock())-self.started)*1000),
                limits=dict(jev_calls=10, jev_tokens=JEV_TOKEN_LIMIT, jev_concurrency=3,
                            deepseek_calls=1, deepseek_tokens=DEEPSEEK_TOKEN_LIMIT),
                calls=deepcopy(self.calls))

    def publish(self):
        if self.sink is not None:
            try:
                self.sink(self.snapshot())
            except Exception:
                pass


class ControlledPost:
    """The frozen adapters still build/parse requests; check streaming boundaries here."""

    def __init__(self, round, deadline, *, provider, estimate=0, answer_reserve=0, post=None):
        self.round, self.deadline, self.provider = round, deadline, provider
        self.estimate, self.answer_reserve, self.post = estimate, answer_reserve, post
        self.call, self.started = None, None

    def __call__(self, endpoint, **kwargs):
        import requests
        r = self.round
        r.check(self.deadline)
        if self.provider == "deepseek" and not r.control.optional_model_available(
                self.estimate, self.answer_reserve, reserve=True):
            raise ClassifierError("triage_budget")
        self.started = r.clock()
        self.call = r.begin(self.provider, kwargs["json"]["model"])
        timeout = min(r.control.timeout_for(2), self.deadline-r.clock(), r.deadline-r.clock())
        if timeout <= 0:
            raise ClassifierError("triage_timeout")
        kwargs["timeout"] = (timeout, timeout)
        if self.provider == "deepseek":
            from trading_companion.turn_trace import trace_provider_request
            trace_provider_request("deepseek", kwargs["json"]["model"])
        response = (requests.post if self.post is None else self.post)(endpoint, **kwargs)
        owner = self
        class CheckedResponse:
            status_code = response.status_code
            def iter_content(self, **options):
                iterator = iter(response.iter_content(**options))
                while True:
                    r.check(owner.deadline)
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        return
                    r.check(owner.deadline)
                    yield chunk
            def close(self):
                response.close()
        return CheckedResponse()


def invoke(round, provider, function, *, deadline, estimate=0, answer_reserve=0, post=None):
    from trading_companion.turn_trace import trace_model_attempt
    with trace_model_attempt() if provider == "deepseek" else nullcontext():
        return _invoke(round, provider, function, deadline=deadline, estimate=estimate,
                       answer_reserve=answer_reserve, post=post)


def _invoke(round, provider, function, *, deadline, estimate=0, answer_reserve=0, post=None):
    transport = ControlledPost(round, deadline, provider=provider, estimate=estimate,
                               answer_reserve=answer_reserve, post=post)
    result, error = None, None
    try:
        result = function(transport)
        return result
    except BaseException as caught:
        error = caught
        raise
    finally:
        if transport.call is not None:
            metadata = result if result is not None else getattr(error, "metadata", {})
            if provider == "deepseek" and metadata.get("model") is not None:
                from trading_companion.tool_model import ToolModelResponse
                from trading_companion.turn_trace import trace_model_response
                trace_model_response(ToolModelResponse(status="complete" if error is None else "refused",
                    text="classification" if error is None else None, provider="deepseek",
                    requested_model=transport.call["requested_model"], model=metadata["model"],
                    usage=token_usage(metadata), is_mock=False))
            # Never swallow a real turn stop under a classification/settlement failure.
            try:
                round.settle(transport.call, metadata, "ok" if error is None else "failed", transport.started)
            except (ClassifierError, TurnStopped):
                if isinstance(error, TurnStopped):
                    raise error
                raise
