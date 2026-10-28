"""Opt-in, bounded metadata only. Tracing never owns budgets or session publication."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from uuid import uuid4

from trading_companion.model import TokenUsage


SCHEMA_VERSION = "trace-v1"
_PROVIDERS = frozenset(("fake", "deepseek", "openai"))
_PHASES = frozenset(("admission", "directory", "routing", "execution", "projection"))
_STATUSES = {"completed": "completed", "needs_clarification": "clarification", "limited": "limited",
             "failed": "error", "invalid_request": "rejected", "out_of_scope": "rejected", "cancelled": "stopped"}
_STOPS = frozenset(("turn_cancelled", "turn_deadline_exceeded", "model_call_budget_exhausted",
                    "analysis_budget_exhausted", "token_budget_exhausted"))
# Exact application codes, never arbitrary exception/response strings or a prefix match.
# Source-code coverage and reasoned non-turn exclusions live in test_turn_trace.py.
# Unknown external values still become "other"; source discovery never runs in production.
_CODES = _STOPS | frozenset("""
api_session_call_limit authentication_error connection_error invalid_response provider_error rate_limited timeout
invalid_input invalid_product_input invalid_product_parameters invalid_research_parameters invalid_research_context
invalid_condition_update invalid_request model_configuration_error product_configuration_error unknown_tool
model_unavailable explanation_unavailable
date_not_grounded research_data_failed saved_result_shown screening_list_configuration_changed
screening_list_expired screening_selection_needs_clarification ungrounded_intent
unsupported_response incomplete_response noncomplete_response refused final_refused final_incomplete script_exhausted
directory_unusable research_internal_error session_not_found session_token_required session_busy service_busy
service_closed session_message_limit request_id_conflict request_id_retired request_in_progress
research_completed partial_research explanation_rejected company_research_completed company_research_limited
general_knowledge_answered general_knowledge_disabled indicator_knowledge_answered routing_state_conflict
session_reset session_cancelled research_cancelled no_saved_result saved_result saved_question_answered
no_confirmed_research no_pending_clarification no_pending_indicator_edit
clarification_configuration_changed clarification_directory_changed clarification_expired
clarification_reply_not_specific clarification_resolved instrument_needs_clarification
indicator_edit_ambiguous indicator_edit_context_changed indicator_edit_expired indicator_edit_ready
indicator_edit_reply_not_specific indicator_edit_target_missing research_context_configuration_changed
research_context_expired research_context_identity_changed condition_update_instrument_unresolved
intent_model_unavailable missing_product_instrument product_directory_unavailable product_directory_unusable
product_explicit_selection_required product_followup_answered product_instrument_needs_clarification
product_model_unavailable product_plan_cancelled product_plan_ready product_request_out_of_scope
product_research_not_requested product_result_unavailable product_saved_result product_selection_outside_candidates
product_trade_execution_forbidden product_unsafe_request company_evidence_invalid
screening_condition_not_supported screening_data_unavailable screening_edit_invalid screening_edit_target_missing
screening_intent_invalid screening_list_unavailable screening_needs_conditions screening_request_forbidden
screening_research_unavailable screening_selection_plan_mismatch screening_source_unavailable
screening_universe_not_supported sample_conditions_evaluated screening_conditions_updated
unsupported_intent workflow_research_required workflow_single_call_required workflow_budget_exhausted
candidate_selection_mismatch company_research_partial company_research_unavailable completed_daily_bars_required
condition_update_ambiguous condition_update_duplicate condition_update_empty_indicators condition_update_not_supported
condition_update_target_missing condition_update_unchanged conflicting_dates conflicting_outlook_horizons explicit_request_required
followup_indicator_ambiguous followup_model_unavailable followup_needs_clarification followup_scope_not_supported
followup_topic_not_supported followup_topic_required general_knowledge_incomplete general_knowledge_invalid_input
general_knowledge_invalid_response general_knowledge_model_unavailable general_knowledge_output_invalid
general_knowledge_refused general_knowledge_retrieval_unavailable general_knowledge_scope_required
indicator_edit_duplicate indicator_edit_not_supported indicator_edit_unchanged indicator_kind_change_not_supported
indicator_not_grounded indicator_not_in_saved_result instrument_not_grounded intent_needs_clarification
invalid_followup_response invalid_indicator_edit invalid_intent_response invalid_product_context invalid_product_intent
knowledge_entry_not_connected missing_instrument model_request_invalid model_request_or_configuration_error
multiple_calls_not_allowed no_age_eligible_verified_quotes no_current_result product_answer_context_unusable
product_answer_invalid_input product_answer_model_unavailable product_answer_no_context_evidence product_answer_no_evidence
product_answer_output_invalid product_clarification_expired product_comparison_needs_two product_conflicting_dates
product_historical_date_required product_identity_changed product_identity_conflict product_instrument_not_grounded
product_no_research_domain product_security_limit product_source_mismatch proposal_request_mismatch provider_unavailable
request_not_cancellable research_not_ready saved_relation_integrity_failed saved_result_configuration_changed
saved_result_expired saved_result_identity_changed saved_result_integrity_failed screening_list_directory_unusable
screening_list_empty screening_list_identity_changed screening_selection_invalid session_capacity source_selection_mismatch
tool_call_not_proposed tool_execution_failed tool_view_unavailable trading_not_supported unexpected_followup_call
ungrounded_followup_topics ungrounded_product_intent unsupported_date_expression unsupported_instrument
unsupported_outlook_horizon workflow_context_limit workflow_iteration_limit workflow_tool_rejected workflow_tool_unavailable
interrupted internal_error other
""".split())
_WORKFLOW_REASONS = frozenset(("answered", "invalid_answer", "model_error", "tool_error", "budget_exhausted",
                               "cancelled", "budget_or_deadline"))
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_CURRENT = ContextVar("research_turn_trace", default=None)
_MODEL = ContextVar("research_trace_model_attempt", default=None)


def _code(value):
    return value if type(value) is str and value in _CODES else "other"


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _model_name(value):
    return value if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) else None


def _usage(value):
    usage = {key: _count(getattr(value, key)) if type(value) is TokenUsage else None for key in _USAGE_KEYS}
    complete = (all(item is not None for item in usage.values())
                and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
    return usage, complete


def _line(record):
    return (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class TraceLimits:
    max_events: int = 64
    record_bytes: int = 64 * 1024
    file_bytes: int = 5 * 1024 * 1024
    file_count: int = 4

    def __post_init__(self):
        for value, low, high in ((self.max_events, 0, 64), (self.record_bytes, 2048, 65536),
                                 (self.file_bytes, self.record_bytes, 5 * 1024 * 1024), (self.file_count, 1, 4)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Trace limits may only tighten the supported bounds")


class JsonlTraceSink:
    """One shared writer per directory/process; no open files or background threads retained."""

    def __init__(self, directory, *, limits=None, clock=monotonic, wall_clock=None, warning=None):
        self.directory = Path(directory)
        self.limits = TraceLimits() if limits is None else limits
        if type(self.limits) is not TraceLimits or not callable(clock) or (warning is not None and not callable(warning)):
            raise ValueError("Use trusted trace configuration")
        self.clock, self.wall_clock = clock, wall_clock or (lambda: datetime.now(timezone.utc))
        self._warning, self._warned, self._lock = warning, False, Lock()

    def _path(self, index):
        return self.directory / ("turns.jsonl" if index == 0 else f"turns.{index}.jsonl")

    def write(self, record):
        """All disk errors stay local, including a short write and rotation failure."""
        try:
            data = _line(record)
            if len(data) > self.limits.record_bytes:
                raise ValueError("Trace record exceeds the configured limit")
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                for index in range(4):
                    path = self._path(index)
                    if path.is_symlink():
                        raise OSError("Trace files must be regular local files")
                    if path.exists():
                        if not path.is_file() or path.stat().st_size > self.limits.file_bytes:
                            raise OSError("Existing trace file exceeds the selected bounds")
                        if index >= self.limits.file_count:
                            path.unlink()
                active = self._path(0)
                if active.exists() and active.stat().st_size + len(data) > self.limits.file_bytes:
                    self._path(self.limits.file_count - 1).unlink(missing_ok=True)
                    for index in range(self.limits.file_count - 2, -1, -1):
                        source = self._path(index)
                        if source.exists():
                            source.replace(self._path(index + 1))
                with active.open("a+b") as handle:
                    position = handle.tell()
                    try:
                        if handle.write(data) != len(data):
                            raise OSError("Incomplete trace write")
                        handle.flush()
                    except Exception:
                        # Best effort: retain earlier complete lines after a partial write.
                        handle.truncate(position)
                        raise
        except Exception:
            self.unavailable()

    def unavailable(self):
        with self._lock:
            first, self._warned = not self._warned, True
        if first and self._warning is not None:
            try:
                self._warning()
            except Exception:
                pass


class TurnTrace:
    """One bounded metadata collector; copied contexts may share it safely across threads."""

    def __init__(self, sink, *, mode, provider, history):
        self.sink, self._clock, self._lock = sink, sink.clock, Lock()
        self.run_id = uuid4().hex
        self._start = self._now()
        self._started_at = sink.wall_clock().astimezone(timezone.utc).isoformat()
        self.mode = mode if mode in ("normal", "development", "demo") else "normal"
        self.provider = provider if provider in _PROVIDERS else None
        self.history = history is True
        self._phase, self._route, self._control = "admission", None, None
        self._outcome = {"category": "error", "code": "internal_error"}
        self._workflow, self._original, self._done = None, None, False
        self._events, self._dropped = [], 0
        self._attempts = self._requests = self._responses = self._complete = self._unknown_transport = 0
        self._totals = dict.fromkeys(_USAGE_KEYS)

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("Trace clock must return finite seconds")
        return value

    def _ms(self, start):
        return min(10**12, max(0, round((self._now() - start) * 1000)))

    def phase(self, phase):
        if phase in _PHASES:
            self._phase = phase

    def control(self, control, provider):
        self._control = control
        self.session(provider)

    def session(self, provider):
        if provider in _PROVIDERS:
            self.provider = provider

    def route(self, decision):
        # Lazy import avoids changing the Agent/control/routing dependency graph.
        from trading_companion.request_routing import RouteDecision, ROUTE_SPECS
        if type(decision) is RouteDecision and any(decision.rule_id == spec.rule_id and decision.intent == spec.intent
                                                  for spec in ROUTE_SPECS):
            self._route = {"kind": decision.kind.value, "intent": decision.intent.value, "rule_id": decision.rule_id}
        self._phase = "execution"

    def replay(self, original_run_id, code):
        if type(original_run_id) is str and re.fullmatch(r"[0-9a-f]{32}", original_run_id):
            self._original = original_run_id
        self._outcome = {"category": "replayed", "code": _code(code)}

    def outcome(self, result):
        self._outcome = {"category": _STATUSES.get(result.status, "error"), "code": _code(result.code)}
        workflow = result.workflow_trace
        if type(workflow) is dict:
            reason = workflow.get("stop_reason")
            self._workflow = {"stop_reason": reason if reason in _WORKFLOW_REASONS else "other",
                              "tool_calls_used": _count(workflow.get("tool_calls_used"))}

    def problem(self, error):
        code = _code(getattr(error, "code", None))
        if self._outcome["category"] == "replayed":
            self._outcome["code"] = code
        else:
            self._outcome = {"category": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit))
                             else "rejected" if self._phase == "admission" else "error",
                             "code": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else code}

    def model_start(self):
        with self._lock:
            self._attempts += 1
            event = {"sequence": self._attempts, "start_ms": self._ms(self._start), "elapsed_ms": None,
                     "provider": self.provider, "requested_model": None, "transport": "unknown",
                     "provider_requests_started": 0, "response_received": False, "response_status": None,
                     "usage": dict.fromkeys(_USAGE_KEYS), "usage_complete": False, "error_code": None}
            if len(self._events) < self.sink.limits.max_events:
                self._events.append(event)
            else:
                self._dropped += 1
            return event, self._now()

    def identity(self, event, model):
        from trading_companion.deepseek_tool_model import DeepSeekToolModel
        from trading_companion.openai_tool_model import OpenAIToolModel
        if type(model) in (DeepSeekToolModel, OpenAIToolModel):
            with self._lock:
                event["provider"] = "deepseek" if type(model) is DeepSeekToolModel else "openai"
                event["requested_model"], event["transport"] = _model_name(model.config.model), "http"

    def request(self, event, provider, model):
        with self._lock:
            event["provider"] = provider if provider in _PROVIDERS else None
            event["requested_model"], event["transport"] = _model_name(model), "http"
            event["provider_requests_started"] += 1
            self._requests += 1

    def response(self, event, response):
        from trading_companion.tool_model import ToolModelResponse
        if type(response) is not ToolModelResponse:
            return
        usage, complete = _usage(response.usage)
        with self._lock:
            if response.is_mock and response.provider == "fake" and event["transport"] == "unknown":
                event["provider"], event["requested_model"], event["transport"] = "fake", "fake-tools-v1", "mock"
            event.update(response_received=True, response_status=response.status, usage=usage, usage_complete=complete)
            self._responses += 1
            self._complete += int(complete)
            for key, value in usage.items():
                if value is not None:
                    self._totals[key] = (self._totals[key] or 0) + value

    def model_end(self, event, start, error):
        with self._lock:
            event["elapsed_ms"] = self._ms(start)
            if error is not None:
                event["error_code"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else _code(
                    getattr(error, "code", None))
            self._unknown_transport += int(event["transport"] == "unknown")

    def finish(self):
        with self._lock:
            if self._done:
                return
            self._done = True
            runtime = None
            if self._control is not None:
                snapshot = self._control.to_dict()
                if snapshot["state"] != "not_started":
                    runtime = {"state": snapshot["state"],
                               "usage_complete_for_attempts": snapshot["usage_complete_for_attempts"] is True}
                    runtime.update({key: _count(snapshot[key]) for key in ("elapsed_ms", "analyses_used", "model_calls_attempted",
                                    "responses_received", "observed_tokens", "responses_with_complete_consistent_usage")})
                    runtime["stop_code"] = snapshot["stop_code"] if snapshot["stop_code"] in _STOPS else None
                    runtime["reported_usage"] = {key: _count(snapshot["reported_usage"][key]) for key in _USAGE_KEYS}
                    runtime["limits"] = {key: snapshot["limits"][key] for key in
                                         ("timeout_seconds", "model_calls", "analyses", "observed_tokens")}
                    if runtime["stop_code"] is not None and self._outcome["code"] in _STOPS:
                        self._outcome = {"category": "stopped", "code": runtime["stop_code"]}
            record = {"schema_version": SCHEMA_VERSION, "run_id": self.run_id, "original_run_id": self._original,
                      "started_at": self._started_at, "entry": "api_message", "mode": self.mode,
                      "provider": self.provider, "history": self.history, "phase": self._phase,
                      "route": self._route, "outcome": dict(self._outcome), "total_elapsed_ms": self._ms(self._start),
                      "execution_started": runtime is not None,
                      "execution_elapsed_ms": runtime["elapsed_ms"] if runtime is not None else None,
                      "control": runtime, "workflow": self._workflow,
                      "models": {"attempted": self._attempts, "provider_requests_started": self._requests,
                                 "responses_received": self._responses,
                                 "reported_usage": {key: _count(value) for key, value in self._totals.items()},
                                 "responses_with_complete_usage": self._complete,
                                 "usage_complete_for_attempts": self._complete == self._attempts,
                                 "attempts_with_unknown_transport": self._unknown_transport,
                                 "events": list(self._events)},
                      "truncated": self._dropped > 0, "dropped_events": self._dropped}
            while len(_line(record)) > self.sink.limits.record_bytes and record["models"]["events"]:
                record["models"]["events"].pop()
                record["dropped_events"] += 1
                record["truncated"] = True
        # Never hold collector, control, API or publication locks during disk I/O.
        self.sink.write(record)


def _update(method, *args):
    trace = _CURRENT.get()
    if trace is not None:
        try:
            return getattr(trace, method)(*args)
        except Exception:
            pass
    return None


def trace_phase(phase):
    _update("phase", phase)


def trace_control(control, provider=None):
    _update("control", control, provider)


def trace_session(provider):
    _update("session", provider)


def trace_route(decision):
    _update("route", decision)


def trace_outcome(result):
    _update("outcome", result)


def trace_replay(original_run_id, code=None):
    _update("replay", original_run_id, code)


def trace_run_id():
    trace = _CURRENT.get()
    return trace.run_id if trace is not None else None


def _model_update(method, *args):
    active = _MODEL.get()
    if active is not None and active[0] is _CURRENT.get():
        _update(method, active[1], *args)


def trace_model_identity(model):
    _model_update("identity", model)


def trace_provider_request(provider, model):
    """Called only after timeout resolution, immediately before requests.post()."""
    _model_update("request", provider, model)


def trace_model_response(response):
    _model_update("response", response)


@contextmanager
def trace_model_attempt():
    trace = _CURRENT.get()
    started = _update("model_start")
    token = _MODEL.set((trace, started[0]) if started is not None else None)
    error = None
    try:
        yield
    except BaseException as caught:
        error = caught
        raise
    finally:
        if started is not None:
            _update("model_end", started[0], started[1], error)
        _MODEL.reset(token)


@contextmanager
def trace_message(sink, *, mode, provider, history):
    trace = None
    if sink is not None:
        try:
            trace = TurnTrace(sink, mode=mode, provider=provider, history=history)
        except Exception:
            sink.unavailable()
    token = _CURRENT.set(trace)
    try:
        yield
    except BaseException as error:
        _update("problem", error)
        raise
    finally:
        try:
            if trace is not None:
                trace.finish()
        except Exception:
            sink.unavailable()
        finally:
            _CURRENT.reset(token)
