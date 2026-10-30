"""Opt-in, bounded metadata only. Tracing never owns budgets or session publication."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import json
from math import isfinite
from pathlib import Path
import re
import sys
from trading_companion.answer_validation import CODES as _ANSWER_CODES, SECTIONS as _ANSWER_SECTIONS
from threading import Lock
from time import monotonic
from uuid import uuid4

from trading_companion.model import TokenUsage


SCHEMA_VERSION = "trace-v2"
_PROVIDERS = frozenset(("fake", "deepseek", "openai"))
_PHASES = frozenset(("admission", "directory", "routing", "execution", "projection"))
_STATUSES = {"completed": "completed", "needs_clarification": "clarification", "limited": "limited",
             "failed": "error", "invalid_request": "rejected", "out_of_scope": "rejected", "cancelled": "stopped"}
_STOPS = frozenset(("turn_cancelled", "turn_deadline_exceeded", "model_call_budget_exhausted",
                    "analysis_budget_exhausted", "token_budget_exhausted"))
# Exact application codes, never arbitrary exception/response strings or a prefix match.
# Source-code coverage and reasoned non-turn exclusions live in test_turn_trace.py.
# Unknown external values still become "other"; source discovery never runs in production.
_CODES = _STOPS | _ANSWER_CODES | frozenset("""
decision_context_unusable decision_instrument_required decision_insufficient_evidence decision_model_unavailable
missing_research_subject no_current_research_subject research_subject_time_invalid research_subject_identity_changed
research_subject_directory_unusable research_subject_ambiguous
decision_no_evidence decision_output_invalid decision_ready demo_domain_unavailable demo_evidence_missing
demo_question_unsupported plan_price_grid_unavailable plan_ready plan_excluded
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
_PARENT = ContextVar("research_trace_parent", default=None)
_TOOL = ContextVar("research_trace_tool", default=None)
_KINDS = {"tool": "tools", "source": "sources", "check": "checks", "repair": "repairs"}
_OPERATIONS = frozenset("""
research workflow execute daily_bars company_section company_collect directory market_context_load market_background answer_validation
screening_snapshot knowledge_search tool_history tool_request tool_scope research_response workflow_response
indicator_relations company_batch product_answer knowledge_answer intent_shape intent_rule local_date_expression rewrite_decision model_rewrite
daily_source daily_records
""".split())
_EVENT_STATUSES = ("ok", "limited", "rejected", "failed", "stopped", "skipped", "unfinished")
_TOOLS = {"analyze_daily_indicators": "primary_analysis_completed", "get_indicator_knowledge": "indicator_knowledge_returned",
          "inspect_research_snapshot": "snapshot_summary_returned", "analyze_additional_indicators": "additional_analysis_completed"}
# Event-only codes are deliberately separate from public turn outcome codes.
_EVENT_CODES = frozenset("""
ok empty local_result source_unavailable invalid_source_data invalid_source_shape
bounded_evidence_available no_eligible_evidence_in_bounded_response current_background_not_allowed
additional_analysis_completed additional_analysis_budget_exhausted duplicate_indicator duplicate_request execution_failed
indicator_period_reduction_not_allowed invalid_arguments not_a_tool_proposal primary_analysis_completed
primary_snapshot_unavailable reused_call_id source_configuration_error tool_call_budget_exhausted tool_internal_error
tool_result_too_large indicator_knowledge_returned snapshot_summary_returned
explanation_context_mismatch explanation_evidence_mismatch explanation_not_ready explanation_relation_mismatch
indicator_relations_checked invalid_explanation_json invalid_explanation_shape no_structured_explanation
no_conversational_response response_duplicate_key response_invalid_json response_invalid_shape
response_invalid_paragraphs response_invalid_citations response_numeric_prose_not_supported response_evidence_checked
workflow_response_context_mismatch workflow_response_duplicate_key workflow_response_evidence_checked
workflow_response_invalid_citations workflow_response_invalid_claims workflow_response_invalid_json
workflow_response_invalid_observations workflow_response_invalid_paragraphs workflow_response_invalid_shape
workflow_response_numeric_prose_not_supported workflow_response_snapshot_mismatch
invalid_output citation_association numeric_citation_association comparison_company_coverage
product_research_answered
rewrite_disabled rewrite_not_repairable rewrite_limit_reached rewrite_allowed rule_applied rule_not_needed rule_not_applicable local_date_recognized
research_data_deadline_exceeded source_redirect_blocked source_http_error source_response_too_large
research_request_budget_exhausted research_byte_budget_exhausted
news_callback_mismatch invalid_source_json source_connection_failed source_business_error invalid_source_rows
source_record_limit_exceeded source_profile_not_unique invalid_source_text duplicate_source_json_key invalid_source_date
missing_publication_timestamp invalid_publication_timestamp invalid_source_number source_instrument_mismatch
financial_record_not_available_at_cutoff financial_currency_unknown invalid_news_result news_article_result_missing
invalid_news_document_id news_company_association_unverified news_outside_publication_window
announcement_exchange_mapping_unverified invalid_announcement_result invalid_announcement_document_id
announcement_instrument_mismatch announcement_outside_publication_window cninfo_exchange_mapping_unverified
invalid_cninfo_directory cninfo_company_not_unique invalid_cninfo_announcements cninfo_instrument_or_document_mismatch
source_deadline_exceeded source_request_failed source_http_unavailable invalid_news_html news_response_too_large
news_source_unavailable news_source_invalid news_identity_unverified
news_list_not_unique invalid_news_list_markup sina_news_exchange_unverified news_layout_invalid
news_page_instrument_mismatch news_list_missing_or_incomplete invalid_source_encoding news_title_association_unverified
""".split())
_SAFE_EVENT_CODES = _CODES | _EVENT_CODES


def _event_code(value):
    return value if type(value) is str and value in _SAFE_EVENT_CODES else "other"


def _known(value, module, name):
    """Only exact known types, never class-name strings supplied by an object."""
    loaded = sys.modules.get("trading_companion." + module)
    return loaded is not None and type(value) is getattr(loaded, name, None)


def _source_identity(operation, function, args):
    owner = getattr(function, "__self__", None)
    if operation == "company_section" and args and _known(args[0], "company_research", "ResearchDataService"):
        service, kind = args[0], args[2] if len(args) > 2 else None
        if kind == "news":
            return ("sina_news" if service._news_provider == "sina" else "eastmoney_news"), "network"
        if kind == "announcements":
            return ("cninfo_announcements" if service._announcement_provider == "cninfo" else "eastmoney_announcements"), "network"
        return {"profile": "eastmoney_company", "financial": "eastmoney_financial"}.get(kind), "network"
    for module, name, source, access in (
        ("tencent_adapter", "TencentDailyBarSource", "tencent", "network"),
        ("akshare_adapter", "AkshareDailyBarSource", "akshare_eastmoney", "network"),
        ("data_source", "CsvDailyBarSource", "csv", "local"),
        ("workflow_tools", "_SnapshotSource", "research_snapshot", "snapshot"),
        ("screening_source", "TencentQuoteSnapshotSource", "tencent", "network"),
        ("screening_source", "EastmoneyQuoteSnapshotSource", "eastmoney_quotes", "network"),
        ("market_context_live", "ExchangeMarketContextSource", "exchange_metadata", "network"),
        ("knowledge_retrieval", "KnowledgeRetriever", "local_knowledge", "local"),
    ):
        if _known(owner, module, name):
            return source, access
    # Injectable loaders and test doubles may do arbitrary IO; do not infer from their names.
    return None, "unknown"


def _result_metadata(operation, result):
    if operation == "local_date_expression":
        return ("ok", "local_date_recognized") if result is True else ("rejected", "rule_not_applicable")
    if operation == "intent_shape":
        return ("ok", "ok") if result is True else ("rejected", "rule_not_applicable")
    if operation == "tool_scope":
        return ("ok", "ok") if result is True else ("rejected", "proposal_request_mismatch")
    if operation == "intent_rule":
        if not _known(result, "research_intent", "PreparedResearchInput"):
            return "failed", "other"
        response = result.model_response
        draft = response.tool_calls[0].arguments if response is not None and response.tool_calls else {}
        applied = draft.get("intent") != "research" or draft.get("clarification_reason") is not None
        return ("ok", "rule_applied") if applied else ("skipped", "rule_not_needed")
    if operation in ("daily_bars", "knowledge_search"):
        if operation == "daily_bars" and type(result) in (list, tuple):
            empty = not result
        elif operation == "knowledge_search" and _known(result, "knowledge_retrieval", "KnowledgeRetrieval"):
            empty = not result.hits
        else:
            return "failed", "invalid_source_shape"
        return "ok", "empty" if empty else "ok"
    if operation == "company_section":
        if not _known(result, "company_research_contracts", "ResearchDataSection"):
            return "failed", "invalid_source_shape"
        code = _event_code(result.code)
        if code in ("research_data_deadline_exceeded", "source_deadline_exceeded"):
            return "stopped", code
        status = {"available": "ok", "empty": "ok", "unavailable": "failed"}.get(result.status, "failed")
        return ("skipped" if code == "current_background_not_allowed" else status), code
    if _known(result, "research_tool", "ToolExecution"):
        payload = result.messages[0].result if result.messages else {}
        return {"executed": "ok", "limited": "limited", "rejected": "rejected"}.get(result.status, "failed"), _event_code(payload.get("code", "primary_analysis_completed"))
    if _known(result, "tool_model", "ToolMessage"):
        payload = result.result if type(result.result) is dict else {}
        return {"rejected": "rejected", "failed": "failed", "result_too_large": "limited"}.get(payload.get("status"), "ok"), _event_code(payload.get("code", _TOOLS.get(result.name, "other")))
    if operation in ("research_response", "workflow_response", "indicator_relations"):
        if not (_known(result, "research_response", "ResponseValidation") or _known(result, "relation_explanation", "ExplanationValidation")):
            return "failed", "other"
        return {"accepted": "ok", "rejected": "rejected", "not_performed": "skipped"}.get(result.status, "failed"), _event_code(result.code)
    if operation == "product_answer" and _known(result, "product_answer", "ProductAnswer"):
        return {"accepted": "ok", "limited": "limited", "fallback": "failed"}.get(result.status, "failed"), _event_code(result.code)
    return "ok", "ok"


def _error_metadata(error, operation):
    from trading_companion.errors import DataSourceError, DataValidationError
    from trading_companion.turn_control import TurnStopped
    if isinstance(error, TurnStopped):
        return "stopped", _event_code(error.code)
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "stopped", "interrupted"
    if isinstance(error, DataSourceError):
        return "failed", "source_unavailable"
    if isinstance(error, DataValidationError):
        return "failed", "invalid_source_data"
    if _known(error, "research_tool", "ToolExecutionError"):
        return "rejected", _event_code(error.code)
    if operation == "tool_history":
        return "rejected", "model_request_invalid"
    if operation == "tool_request":
        return "rejected", "invalid_arguments"
    if operation == "company_batch":
        return "rejected", "company_evidence_invalid"
    # Known private validation exceptions carry fixed reasons; never serialize arbitrary str(error).
    for module, name in (("product_answer", "_Invalid"), ("open_knowledge", "_Invalid")):
        if _known(error, module, name):
            reason = getattr(error, "reason", None)
            return "rejected", _event_code(reason) if reason is not None else "invalid_output"
    return "failed", _event_code(getattr(error, "code", None))


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
        self._details, self._sequence = [], 0
        self._summaries = {name: dict.fromkeys(("seen", "entered", *_EVENT_STATUSES), 0) for name in _KINDS.values()}
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
        with self._lock:
            if not self._done and phase in _PHASES:
                self._phase = phase

    def control(self, control, provider):
        with self._lock:
            if not self._done:
                self._control = control
                if provider in _PROVIDERS:
                    self.provider = provider

    def session(self, provider):
        with self._lock:
            if not self._done and provider in _PROVIDERS:
                self.provider = provider

    def route(self, decision):
        # Lazy import avoids changing the Agent/control/routing dependency graph.
        from trading_companion.request_routing import RouteDecision, ROUTE_SPECS
        with self._lock:
            if self._done:
                return
            if type(decision) is RouteDecision and any(decision.rule_id == spec.rule_id and decision.intent == spec.intent
                                                      for spec in ROUTE_SPECS):
                self._route = {"kind": decision.kind.value, "intent": decision.intent.value, "rule_id": decision.rule_id}
            self._phase = "execution"

    def replay(self, original_run_id, code):
        with self._lock:
            if self._done:
                return
            if type(original_run_id) is str and re.fullmatch(r"[0-9a-f]{32}", original_run_id):
                self._original = original_run_id
            self._outcome = {"category": "replayed", "code": _code(code)}

    def outcome(self, result):
        with self._lock:
            if self._done:
                return
            self._outcome = {"category": _STATUSES.get(result.status, "error"), "code": _code(result.code)}
            workflow = result.workflow_trace
            if type(workflow) is dict:
                reason = workflow.get("stop_reason")
                self._workflow = {"stop_reason": reason if reason in _WORKFLOW_REASONS else "other",
                                  "tool_calls_used": _count(workflow.get("tool_calls_used"))}

    def problem(self, error):
        with self._lock:
            if self._done:
                return
            code = _code(getattr(error, "code", None))
            if self._outcome["category"] == "replayed":
                self._outcome["code"] = code
            else:
                self._outcome = {"category": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit))
                                 else "rejected" if self._phase == "admission" else "error",
                                 "code": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else code}

    def _retain(self, event, destination):
        self._sequence += 1
        parent = _PARENT.get()
        event.update(event_id=self._sequence, parent_event_id=parent[1]["event_id"] if parent and parent[0] is self else None)
        if len(self._events) + len(self._details) < self.sink.limits.max_events:
            destination.append(event)
        else:
            self._dropped += 1

    def event_start(self, kind, operation, source, access):
        with self._lock:
            if self._done:
                return None
            started = self._now()
            event = {"kind": kind, "operation": operation, "phase": self._phase,
                     "start_ms": self._ms(self._start), "elapsed_ms": None,
                     "source": source, "access": access, "entered": kind != "tool",
                     "status": "unfinished", "code": "other"}
            self._retain(event, self._details)
            summary = self._summaries[_KINDS[kind]]
            summary["seen"] += 1
            summary["entered"] += int(event["entered"])
            summary["unfinished"] += 1
            return event, started

    def event_enter(self, event):
        with self._lock:
            if not self._done and event["status"] == "unfinished" and not event["entered"]:
                event["entered"] = True
                self._summaries[_KINDS[event["kind"]]]["entered"] += 1

    def event_end(self, event, started, status, code):
        with self._lock:
            if self._done or event["status"] != "unfinished":
                return
            elapsed = self._ms(started)
            status = status if status in _EVENT_STATUSES and status != "unfinished" else "failed"
            entered = event["entered"] and status != "skipped"
            summary = self._summaries[_KINDS[event["kind"]]]
            if event["entered"] and not entered:
                summary["entered"] -= 1
            event.update(status=status, code=_event_code(code), entered=entered, elapsed_ms=elapsed)
            summary["unfinished"] -= 1
            summary[status] += 1

    def model_start(self):
        with self._lock:
            if self._done:
                return None
            self._attempts += 1
            event = {"sequence": self._attempts, "start_ms": self._ms(self._start), "elapsed_ms": None,
                     "provider": self.provider, "requested_model": None, "transport": "unknown",
                     "provider_requests_started": 0, "response_received": False, "response_status": None,
                     "usage": dict.fromkeys(_USAGE_KEYS), "usage_complete": False, "error_code": None}
            self._retain(event, self._events)
            return event, self._now()

    def identity(self, event, model):
        from trading_companion.deepseek_tool_model import DeepSeekToolModel
        from trading_companion.openai_tool_model import OpenAIToolModel
        if type(model) in (DeepSeekToolModel, OpenAIToolModel):
            with self._lock:
                if self._done:
                    return
                event["provider"] = "deepseek" if type(model) is DeepSeekToolModel else "openai"
                event["requested_model"], event["transport"] = _model_name(model.config.model), "http"

    def request(self, event, provider, model):
        with self._lock:
            if self._done:
                return
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
            if self._done:
                return
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
            if self._done:
                return
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
                                 "events": deepcopy(self._events)},
                      **deepcopy(self._summaries), "events": deepcopy(self._details),
                      "truncated": self._dropped > 0, "dropped_events": self._dropped}
            while len(_line(record)) > self.sink.limits.record_bytes and (record["models"]["events"] or record["events"]):
                models, details = record["models"]["events"], record["events"]
                destination = models if models and (not details or models[-1]["event_id"] > details[-1]["event_id"]) else details
                destination.pop()
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


def safe_answer_validation(value):
    """Exact, versioned allowlist. Arbitrary strings and rejected sentences never survive."""
    if type(value) is not dict:
        value = {}
    result = {"version": 1, "answer_version": "product-research-answer-v3",
              "state": value.get("state") if value.get("state") in ("complete", "partial", "fallback") else "fallback",
              "code": value.get("code") if type(value.get("code")) is str and value["code"] in _ANSWER_CODES else "other"}
    for key in ("generated_sentences", "retained_sentences", "deleted_sentences", "retained_paragraphs", "repair_attempts", "structure_failures"):
        number = value.get(key)
        result[key] = min(number, 10000) if type(number) is int and number >= 0 else 0
    for key in ('local_structure_recoveries','partial_structure_recoveries','punctuation_structure_recoveries',
                'output_truncations','initial_output_truncations','repair_output_truncations', 'numeric_tokens',
                'bound_numbers','bound_matched','bound_rejected','unbound_matched','unbound_rejected','unbound_ambiguous'):
        if type(value.get(key)) is int and value[key]>=0:
            result[key] = min(value[key],10000)
    if value.get('numeric_validation_version') == 'source-bindings-v1':
        result['numeric_validation_version'] = 'source-bindings-v1'
    if value.get('output_truncation_version') == 'answer-output-v1':
        result['output_truncation_version'] = 'answer-output-v1'
    for key in ('repair_all_passed','repair_rating_restored','repair_content_added'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    result["reasons"] = []
    reasons = value.get("reasons", [])
    if type(reasons) is list:
        for issue in reasons[:20]:
            if type(issue) is not dict:
                continue
            code, section, index = issue.get("code"), issue.get("section"), issue.get("index")
            result["reasons"].append({"code": code if type(code) is str and code in _ANSWER_CODES else "other",
                "section": section if type(section) is str and section in _ANSWER_SECTIONS else "structure",
                "index": min(index, 100) if type(index) is int and index >= 0 else 0})
    return result


def trace_answer_validation(value):
    with trace_event("check", "answer_validation") as handle:
        if handle.started is not None:
            metadata = safe_answer_validation(value)
            handle.started[0]["validation"] = metadata
            handle.status = {"complete": "ok", "partial": "limited", "fallback": "failed"}[metadata["state"]]
            handle.code = metadata["code"]


class _EventHandle:
    """Only fixed metadata is retained; result objects never outlive result()."""

    def __init__(self, operation, started, trace=None):
        self.operation, self.started = operation, started
        self._trace = trace
        self.status, self.code = "ok", "ok"

    def stop(self, code):
        """End a logically abandoned source from its coordinator; late completion is ignored."""
        if self.started is not None and self._trace is not None:
            try:
                self._trace.event_end(self.started[0], self.started[1], "stopped", _event_code(code))
            except Exception:
                pass  # Trace bookkeeping never owns cancellation or source result acceptance.

    def result(self, value):
        if self.started is not None:
            try:
                self.status, self.code = _result_metadata(self.operation, value)
            except Exception:
                self.status, self.code = "failed", "other"

    def record_failure(self, code):
        if self.started is not None:
            try:
                self.status, self.code = "failed", _event_code(code)
            except Exception:
                self.status, self.code = "failed", "other"


@contextmanager
def trace_event(kind, operation, *, enabled=True, source=None, decision=None):
    """The sole span interface. Neither metadata failures nor disabled spans own business work.

    ``tool/execute`` marks dispatch of the active logical tool without making a second event.
    ``source`` is an internal (callable, positional arguments) descriptor, never serialized.
    """
    trace, started = _CURRENT.get(), None
    parent_token = tool_token = None
    handle = _EventHandle(operation, None, trace)
    try:
        if trace is not None and enabled is True and operation in _OPERATIONS:
            active_tool = _TOOL.get()
            duplicate = (kind == "tool" and active_tool is not None and active_tool[0] is trace
                         and (operation == "execute" or operation == "research"
                              and active_tool[1]["operation"] == "analyze_daily_indicators"))
            if duplicate:
                if operation == "execute":
                    _update("event_enter", active_tool[1])
            elif kind in _KINDS:
                origin, access = _source_identity(operation, *source) if source is not None else (None, "unknown")
                label = operation
                if operation == "workflow" and source is not None and len(source[1]) > 1:
                    call = source[1][1]
                    if _known(call, "tool_model", "ToolCall") and call.name in _TOOLS:
                        label = call.name
                elif operation == "research":
                    label = "analyze_daily_indicators"
                started = _update("event_start", kind, label, origin, access)
                if started is not None:
                    handle.started = started
                    parent_token = _PARENT.set((trace, started[0]))
                    if kind == "tool":
                        tool_token = _TOOL.set((trace, started[0]))
                    if operation == "rewrite_decision":
                        attempt, allowed, repairable = decision
                        handle.code = ("rewrite_limit_reached" if attempt else "rewrite_disabled" if not allowed
                                       else "rewrite_not_repairable" if not repairable else "rewrite_allowed")
                        handle.status = "ok" if handle.code == "rewrite_allowed" else "rejected"
    except Exception:
        # Do not let metadata inspection change whether or how often the callable runs.
        pass
    try:
        yield handle
    except BaseException as error:
        if started is not None and operation != "rewrite_decision":
            try:
                handle.status, handle.code = _error_metadata(error, operation)
            except Exception:
                handle.status, handle.code = "failed", "other"
        raise
    finally:
        try:
            if started is not None:
                _update("event_end", started[0], started[1], handle.status, handle.code)
        finally:
            if tool_token is not None:
                _TOOL.reset(tool_token)
            if parent_token is not None:
                _PARENT.reset(parent_token)


def trace_call(kind, operation, function=None, /, *args, **kwargs):
    """Wrap one existing call, or decorate a boundary; disabled calls go straight through."""
    if function is None:
        def decorate(target):
            @wraps(target)
            def wrapped(*arguments, **keywords):
                return trace_call(kind, operation, target, *arguments, **keywords)
            return wrapped
        return decorate
    if _CURRENT.get() is None:
        return function(*args, **kwargs)
    section_traced = False
    try:
        section_traced = operation == "company_collect" and _known(
            getattr(function, "__self__", None), "company_research", "ResearchDataService")
    except Exception:
        pass
    if section_traced:
        # The built-in service records each section itself. Opaque injected services get one batch event.
        return function(*args, **kwargs)
    with trace_event(kind, operation, source=(function, args)) as event:
        result = function(*args, **kwargs)
        event.result(result)
        return result


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
    parent = _PARENT.set((trace, started[0])) if started is not None else None
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
        if parent is not None:
            _PARENT.reset(parent)


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
