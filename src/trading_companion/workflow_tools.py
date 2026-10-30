"""Bounded, read-only research tools owned by one trusted workflow turn."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from threading import RLock
from uuid import uuid4

from trading_companion.agent_input import proposal_matches_request
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.indicator_knowledge import DEFINITIONS, KnowledgeAnswer
from trading_companion.model import ModelConfigurationError, ModelRequestError
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_tool import (
    RESEARCH_TOOL_DEFINITION, SEED_METHOD_CATALOG, TOOL_NAME, ResearchToolExecutor, ToolExecution,
    ToolExecutionError, _metrics, _project,
)
from trading_companion.tool_model import (
    ToolCall, ToolDefinition, ToolMessage, ToolModelResponse, validate_tool_messages,
)
from trading_companion.turn_control import TurnStopped, checkpoint, reserve_analysis
from trading_companion.turn_trace import trace_call, trace_event


KNOWLEDGE_TOOL_NAME = "get_indicator_knowledge"
SNAPSHOT_TOOL_NAME = "inspect_research_snapshot"
ADDITIONAL_TOOL_NAME = "analyze_additional_indicators"
MAX_WORKFLOW_TOOL_CALLS = 6

KNOWLEDGE_TOOL_DEFINITION = ToolDefinition(
    KNOWLEDGE_TOOL_NAME,
    "Project indicator definitions (1-3 distinct topics); no market data.",
    {"type": "object", "properties": {"topics": {"type": "array", "items": {
        "type": "string", "enum": list(DEFINITIONS)}, "minItems": 1, "maxItems": 3}},
     "required": ["topics"], "additionalProperties": False},
)
SNAPSHOT_TOOL_DEFINITION = ToolDefinition(
    SNAPSHOT_TOOL_NAME,
    "Read primary overview or field availability; no market fetch.",
    {"type": "object", "properties": {"section": {"type": "string",
        "enum": ["overview", "availability"]}}, "required": ["section"],
     "additionalProperties": False},
)
ADDITIONAL_TOOL_DEFINITION = ToolDefinition(
    ADDITIONAL_TOOL_NAME,
    "Once: 1-3 new indicators on primary frozen bars, fixed scope, no fetch; never shorten "
    "periods. Aliases local to supplement.",
    {"type": "object", "properties": {"indicators": {
        **RESEARCH_TOOL_DEFINITION.parameters["properties"]["indicators"],
        "minItems": 1, "maxItems": 3}}, "required": ["indicators"],
     "additionalProperties": False},
)
# Field descriptions duplicate the trusted scope; keep every schema constraint and rule catalogue.
_PRIMARY_PARAMETERS = RESEARCH_TOOL_DEFINITION.parameters
for _property in _PRIMARY_PARAMETERS["properties"].values():
    _property.pop("description", None)
_WORKFLOW_RELATION_RULES = {
    "price_position": "close vs MA/BOLL; equality/coincident bands explicit",
    "mean_change": "current vs prior mean",
    "price_cross": "up:prior close<=MA,now>MA; down:prior>=MA,now<MA",
    "mean_position": "left vs right named mean",
    "mean_cross": "up:prior left<=right,now>; down:prior>=,now<",
    "dif_dea_position": "DIF vs DEA",
    "dif_dea_cross": "up:prior DIF<=DEA,now>; down:prior>=,now<",
    "histogram_change": "current vs prior 2*(DIF-DEA); not strength alone",
    "reference_zone": "RSI<30 below_30; >70 above_70; else neutral, incl 30/70",
    "bands_state": "upper=lower coincident; else distinct",
    "atr_change": "current vs prior ATR; volatility, not direction",
    "relative_volume_position": "today/strictly prior N mean vs 1",
    "current_vs_prior_mean": "today vs prior N share mean; zero baseline explicit",
    "strength_zone": "ADX<25 below_25; else at_or_above_25; strength only",
    "di_position": "+DI vs -DI: above/below/equal",
    "obv_change": "current vs prior OBV; zero seed, not currency flow",
}
WORKFLOW_PRIMARY_DEFINITION = ToolDefinition(
    TOOL_NAME, "Read-only. Full precision, adjacent records. research-rules-v1:\n"
    + "\n".join(f"{name}={rule}" for name, rule in _WORKFLOW_RELATION_RULES.items())
    + "\nresearch-seeds-v1:\n"
    + "\n".join(f"{code}={method}" for code, method in SEED_METHOD_CATALOG.items()),
    _PRIMARY_PARAMETERS,
)
WORKFLOW_TOOL_DEFINITIONS = (
    WORKFLOW_PRIMARY_DEFINITION, KNOWLEDGE_TOOL_DEFINITION,
    SNAPSHOT_TOOL_DEFINITION, ADDITIONAL_TOOL_DEFINITION,
)


@dataclass(frozen=True)
class WorkflowObservation:
    """Keep each evidence registry attached to its own immutable snapshot key."""

    key: str
    execution: ToolExecution


@dataclass(frozen=True)
class _SnapshotSource:
    """A detached read-only source; read_daily_bars never performs external IO."""

    bars: tuple[DailyBar, ...]
    source_name: str
    adjustment: str
    is_synthetic: bool
    instrument_id: str
    start_date: date
    end_date: date

    def read_daily_bars(self):
        return list(self.bars)


def _message(call, status, code, **details):
    return ToolMessage("tool", call_id=call.call_id, name=call.name, result={
        "status": status, "code": code, "synthesis_allowed": False, **details,
    })


def _limited(call, key):
    return _message(call, "result_too_large", "tool_result_too_large",
                    snapshot_key=key, full_result_retained=True)


class WorkflowToolExecutor:
    """Authorize proposals, preserve primary facts and limit local supplementary work."""

    def __init__(self, confirmed_request: MultiIndicatorRequest, *,
                 development_source: CsvDailyBarSource | None = None):
        if type(confirmed_request) is not MultiIndicatorRequest:
            raise ModelConfigurationError("Workflow tools require a trusted confirmed request")
        try:
            self._request = MultiIndicatorRequest(
                confirmed_request.instrument_id, confirmed_request.start_date,
                confirmed_request.as_of_date, confirmed_request.indicators,
                confirmed_request.source, confirmed_request.request_id)
        except (InvalidRequestError, TypeError, ValueError):
            raise ModelConfigurationError("Workflow request failed validation") from None
        # Let the existing boundary validate the trusted development configuration.
        self._primary_executor = ResearchToolExecutor(
            development_source=development_source, request_id=self._request.request_id)
        if (self._request.source == "csv") != (development_source is not None):
            raise ModelConfigurationError("The trusted source and confirmed request must agree")
        self._development_source = development_source
        self._observations = []
        self._steps = []
        self._used = 0
        self._seen_ids = set()
        self._signatures = set()
        self._primary_attempted = False
        self._supplement_attempted = False
        self._lock = RLock()

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return WORKFLOW_TOOL_DEFINITIONS

    @property
    def observations(self) -> tuple[WorkflowObservation, ...]:
        with self._lock:
            return deepcopy(tuple(self._observations))

    @property
    def steps(self) -> tuple[dict, ...]:
        with self._lock:
            return deepcopy(tuple(self._steps))

    @property
    def tool_calls_used(self) -> int:
        with self._lock:
            return self._used

    @property
    def primary_execution(self) -> ToolExecution | None:
        with self._lock:
            return deepcopy(self._observations[0].execution) if self._observations else None

    def _record(self, tool, status, code, *, call_id=None):
        self._steps.append({"tool": tool, "status": status, "code": code,
                            **({"call_id": call_id} if call_id is not None else {})})

    def execute_batch(self, response, *, messages, tool_limit=MAX_WORKFLOW_TOOL_CALLS,
                      allow_execution=True, stop_after=None):
        """Keep the original batch paired, reserving bounded results before every IO.

        The executor still authorizes/counts each call independently. Reserved results
        are internal capacity checks only; they are never supplied as observations.
        """
        with self._lock:
            history, _, seen = validate_tool_messages(messages, tools=self.definitions)
            calls = response.tool_calls
            allowed = {d.name for d in self.definitions}
            if any(c.name not in allowed for c in calls):
                raise ToolExecutionError('unknown_tool')
            if any(c.call_id in seen or c.call_id in self._seen_ids for c in calls):
                raise ToolExecutionError('reused_call_id')
            proposal = response.as_message()
            reserved = [_message(c, 'not_executed', 'indicator_period_reduction_not_allowed',
                                 snapshot_key='supplement', full_result_retained=True) for c in calls]
            results, stop = [], None

            def check(outputs):
                validate_tool_messages([*history, proposal, *outputs], tools=self.definitions)

            def skipped(call, code):
                self._record(call.name, 'not_executed', code, call_id=call.call_id)
                with trace_event('tool', 'workflow') as event:
                    event.status, event.code = 'skipped', code
                return _message(call, 'not_executed', code)

            try:
                check(reserved)
            except ModelRequestError:
                for call in calls:
                    skipped(call, 'workflow_context_limit')
                raise
            for index, call in enumerate(calls):
                if stop is not None:
                    results.append(skipped(call, stop))
                    continue
                if not allow_execution:
                    stop = 'workflow_iteration_limit'
                    results.append(skipped(call, stop))
                    continue
                if self._used >= min(tool_limit, MAX_WORKFLOW_TOOL_CALLS):
                    results.append(skipped(call, 'tool_call_budget_exhausted'))
                    continue
                used_before = self._used
                try:
                    checkpoint()
                    output = self.execute(call, messages=history,
                        _batch_check=lambda value: check([*results, value, *reserved[index+1:]]),
                        _record_call_id=len(calls)>1)
                except TurnStopped as error:
                    self._record(call.name, 'interrupted' if self._used>used_before else 'not_executed',
                                 error.code, call_id=call.call_id)
                    for remaining in calls[index+1:]:
                        skipped(remaining, error.code)
                    raise
                except ModelRequestError:
                    stop = 'workflow_context_limit'
                    results.append(skipped(call, stop))
                    continue
                results.append(output)
                if output.result.get('code') == 'tool_result_too_large':
                    stop = 'workflow_context_limit'
                elif stop_after is not None:
                    stop = stop_after()
            check(results)
            return tuple(results), stop

    @trace_call("tool", "workflow")
    def execute(self, call: ToolCall, *, messages, _batch_check=None, _record_call_id=False) -> ToolMessage:
        """Process one call; the runner owns batching, history and model-call limits."""
        with self._lock:
            checkpoint()
            if type(call) is not ToolCall:
                raise ToolExecutionError("not_a_tool_proposal")
            history, _, seen_ids = trace_call("check", "tool_history", validate_tool_messages, messages, tools=self.definitions)
            if call.call_id in seen_ids or call.call_id in self._seen_ids:
                raise ToolExecutionError("reused_call_id")
            if call.name not in {definition.name for definition in self.definitions}:
                raise ToolExecutionError("unknown_tool")
            proposal = ToolMessage("assistant", tool_calls=(call,))

            def check(output):
                if _batch_check is not None:
                    _batch_check(output)
                else:
                    trace_call("check", "tool_history", validate_tool_messages, [*history, proposal, output], tools=self.definitions)

            # Reserve a closed error and a transport-limit envelope before any IO or budget use.
            check(_message(call, "rejected", "indicator_period_reduction_not_allowed"))
            check(_limited(call, "supplement" if call.name == ADDITIONAL_TOOL_NAME else "primary"))
            self._seen_ids.add(call.call_id)
            if self._used >= MAX_WORKFLOW_TOOL_CALLS:
                output = _message(call, "rejected", "tool_call_budget_exhausted")
                check(output)
                self._record(call.name, "rejected", "tool_call_budget_exhausted",
                             call_id=call.call_id if _record_call_id else None)
                return output
            self._used += 1
            try:
                output, status, code, observation = self._dispatch(call)
            except TurnStopped:
                raise
            except (ModelConfigurationError, InvalidRequestError, DataSourceError, DataValidationError):
                output, status, code, observation = (
                    _message(call, "failed", "tool_execution_failed"),
                    "failed", "tool_execution_failed", None)
            except ToolExecutionError:
                output, status, code, observation = (
                    _message(call, "failed", "tool_execution_failed"),
                    "failed", "tool_execution_failed", None)
            except Exception:
                # A programming or provider exception must never become model-visible text.
                output, status, code, observation = (
                    _message(call, "failed", "tool_internal_error"),
                    "failed", "tool_internal_error", None)
            checkpoint()
            try:
                check(output)
            except ModelRequestError:
                key = observation.key if observation is not None else "primary"
                output, status, code = _limited(call, key), "limited", "tool_result_too_large"
                check(output)
            if observation is not None:
                observation = replace(observation, execution=replace(
                    observation.execution, messages=(output,),
                    status="limited" if status == "limited" else observation.execution.status))
                self._observations.append(observation)
            self._record(call.name, status, code, call_id=call.call_id if _record_call_id else None)
            return output

    def _dispatch(self, call):
        if call.name == TOOL_NAME:
            return self._primary(call)
        if call.name == KNOWLEDGE_TOOL_NAME:
            return self._knowledge(call)
        if call.name == SNAPSHOT_TOOL_NAME:
            return self._snapshot(call)
        return self._additional(call)

    @staticmethod
    def _reject(call, code):
        return _message(call, "rejected", code), "rejected", code, None

    def _primary(self, call):
        if not trace_call("check", "tool_scope", proposal_matches_request, call, self._request):
            return self._reject(call, "proposal_request_mismatch")
        if self._primary_attempted:
            return self._reject(call, "duplicate_request")
        self._primary_attempted = True
        response = ToolModelResponse("tool_calls", tool_calls=(call,))
        # The legacy executor knows only its single tool; validate actual workflow history outside it.
        execution = self._primary_executor.execute(
            response, messages=[ToolMessage("user", "Execute the confirmed read-only research request.")])
        payload = {**execution.messages[0].result, "snapshot_key": "primary"}
        try:
            message = ToolMessage("tool", call_id=call.call_id, name=call.name, result=payload)
        except ModelRequestError:
            message = _limited(call, "primary")
            execution = replace(execution, status="limited")
        observation = WorkflowObservation("primary", replace(execution, messages=(message,)))
        status = execution.status if execution.status in ("executed", "rejected", "failed", "limited") else "failed"
        code = {"executed": "primary_analysis_completed", "limited": "tool_result_too_large"}.get(
            status, payload.get("code", "tool_execution_failed"))
        return message, status, code, observation

    def _knowledge(self, call):
        arguments = call.arguments
        topics = arguments.get("topics")
        if (arguments.keys() != {"topics"} or type(topics) is not list or not 1 <= len(topics) <= 3
                or any(type(topic) is not str or topic not in DEFINITIONS for topic in topics)
                or len(set(topics)) != len(topics)):
            return self._reject(call, "invalid_arguments")
        signature = (KNOWLEDGE_TOOL_NAME, tuple(sorted(topics)))
        if signature in self._signatures:
            return self._reject(call, "duplicate_request")
        self._signatures.add(signature)
        with trace_event("tool", "execute"):
            payload = {**KnowledgeAnswer(tuple(topics)).to_dict(), "status": "knowledge_result"}
            return (ToolMessage("tool", call_id=call.call_id, name=call.name, result=payload),
                    "executed", "indicator_knowledge_returned", None)

    def _snapshot(self, call):
        arguments = call.arguments
        if (arguments.keys() != {"section"} or type(arguments.get("section")) is not str
                or arguments["section"] not in ("overview", "availability")):
            return self._reject(call, "invalid_arguments")
        primary = self._observations[0].execution if self._observations else None
        if primary is None or primary.full_result is None:
            return self._reject(call, "primary_snapshot_unavailable")
        signature = (SNAPSHOT_TOOL_NAME, arguments["section"])
        if signature in self._signatures:
            return self._reject(call, "duplicate_request")
        self._signatures.add(signature)
        with trace_event("tool", "execute"):
            result = primary.full_result
            payload = {"status": "snapshot_summary", "snapshot_key": "primary",
                       "section": arguments["section"], "market_data_requested": False,
                       "request_id": result.request.request_id, "instrument_id": result.request.instrument_id,
                       "source": result.source, "data_authenticity": result.data_authenticity,
                       "price_adjustment": result.price_adjustment,
                       "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at else None,
                       "data_status": result.data_status, "calculation_status": result.calculation_status,
                       "synthesis_allowed": result.synthesis_allowed, "bars_used": len(result.bars)}
            if arguments["section"] == "overview":
                payload.update(range=[result.request.start_date.isoformat(), result.request.as_of_date.isoformat()],
                               latest_trade_date=result.latest_trade_date.isoformat() if result.latest_trade_date else None,
                               indicator_count=len(result.request.indicators))
            else:
                payload["fields"] = [
                    [indicator.indicator_id, field, value.status, value.missing_bars]
                    for indicator in result.indicators for field, value in indicator.latest.items()]
            return (ToolMessage("tool", call_id=call.call_id, name=call.name, result=payload),
                    "executed", "snapshot_summary_returned", None)

    def _additional(self, call):
        arguments = call.arguments
        raw = arguments.get("indicators")
        if arguments.keys() != {"indicators"} or type(raw) is not list or not 1 <= len(raw) <= 3:
            return self._reject(call, "invalid_arguments")
        try:
            specs = tuple(IndicatorSpec(item["kind"], **{
                key: value for key, value in item.items() if key != "kind"})
                for item in raw if type(item) is dict and "kind" in item)
            if len(specs) != len(raw) or len(set(specs)) != len(specs):
                return self._reject(call, "invalid_arguments")
        except (InvalidRequestError, TypeError, ValueError):
            return self._reject(call, "invalid_arguments")
        if any(spec in self._request.indicators for spec in specs):
            return self._reject(call, "duplicate_indicator")
        for spec in specs:
            baselines = [item for item in self._request.indicators if item.kind == spec.kind]
            periods = tuple(key for key in spec.parameters if key != "stddev_multiplier")
            if baselines and not any(all(spec.parameters[key] >= baseline.parameters[key]
                                         for key in periods) for baseline in baselines):
                return self._reject(call, "indicator_period_reduction_not_allowed")
        primary = self._observations[0].execution if self._observations else None
        if (primary is None or primary.full_result is None
                or primary.full_result.data_status != "ok" or not primary.full_result.bars):
            return self._reject(call, "primary_snapshot_unavailable")
        if self._supplement_attempted:
            return self._reject(call, "additional_analysis_budget_exhausted")
        reserve_analysis()
        with trace_event("tool", "execute"):
            self._supplement_attempted = True
            original = primary.full_result
            request = replace(self._request, indicators=specs, request_id=uuid4().hex)
            source = _SnapshotSource(tuple(original.bars), original.source, original.price_adjustment,
                                     original.data_authenticity == "synthetic", request.instrument_id,
                                     request.start_date, request.as_of_date)
            result = analyze_indicators(source, request, allow_development_csv=request.source == "csv")
            result = replace(result, retrieved_at=original.retrieved_at,
                             data_authenticity=original.data_authenticity,
                             price_adjustment=original.price_adjustment)
            payload, aliases = _project(result, development=request.source == "csv")
            payload.update(snapshot_key="supplement", based_on_request_id=original.request.request_id,
                           market_data_requested=False)
            metrics = _metrics(payload)
            try:
                message = ToolMessage("tool", call_id=call.call_id, name=call.name, result=payload)
                status, code = "executed", "additional_analysis_completed"
            except ModelRequestError:
                message, status, code = _limited(call, "supplement"), "limited", "tool_result_too_large"
            execution = ToolExecution(status, (message,), result, aliases, metrics)
            return message, status, code, WorkflowObservation("supplement", execution)
