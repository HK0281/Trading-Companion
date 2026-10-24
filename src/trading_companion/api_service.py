"""Local single-process HTTP application service; no web-framework dependency."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
from importlib.util import find_spec
from math import isfinite
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from uuid import uuid4

from trading_companion.agent import AgentTurnResult, run_natural_research_turn
from trading_companion.answer_presentation import build_answer_presentation
from trading_companion.agent_input import request_arguments
from trading_companion.chart_projection import build_chart_projection
from trading_companion.market_context import build_market_context
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.research_session import ResearchSession
from trading_companion.research_memory import SavedResearch
from trading_companion.research_report import build_research_report
from trading_companion.turn_control import TurnControl, TurnLimits


DATA_MODES = ("synthetic", "tencent", "akshare_eastmoney")


class DataModeDependencyError(ModelConfigurationError):
    """A fixed startup error without import paths or underlying dependency details."""


def ensure_data_mode_dependencies(data_mode: str) -> None:
    """Inspect optional package availability without importing it or making a request."""
    if data_mode not in DATA_MODES:
        raise ModelConfigurationError("Choose an explicit supported data mode")
    if data_mode == "akshare_eastmoney":
        try:
            available = find_spec("akshare") is not None
        except (ImportError, AttributeError, ValueError):
            available = False
        if not available:
            raise DataModeDependencyError("Install the data extra to enable AKShare") from None


class ApiProblem(Exception):
    """A fixed public problem, never a provider error or request dump."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class ApiLimits:
    sessions: int = 32
    messages: int = 20
    model_calls: int = 60
    concurrent_turns: int = 2
    lifetime_seconds: float = 1800

    def __post_init__(self):
        for value, maximum in ((self.sessions, 32), (self.messages, 20), (self.model_calls, 60), (self.concurrent_turns, 2)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ModelConfigurationError("API limits may only tighten positive local-development bounds")
        if (type(self.lifetime_seconds) not in (int, float) or not isfinite(self.lifetime_seconds)
                or not 0 < self.lifetime_seconds <= 1800):
            raise ModelConfigurationError("Session lifetime must be positive and at most 1800 seconds")


class _SessionModel:
    def __init__(self, delegate, limit):
        self.delegate, self.limit, self.forwarded = delegate, limit, 0
        self._lock = Lock()

    def complete(self, messages, *, tools):
        with self._lock:
            if self.forwarded >= self.limit:
                raise ModelCallError("api_session_call_limit", "Session model allowance exhausted")
            self.forwarded += 1
        return self.delegate.complete(messages, tools=tools)


@dataclass
class _Record:
    digest: str
    response: dict | None = None
    problem: tuple[int, str, str] | None = None
    retired: bool = False


@dataclass
class _Entry:
    token: str = field(repr=False)
    created: float
    model: _SessionModel = field(repr=False)
    session: ResearchSession = field(default_factory=ResearchSession, repr=False)
    records: dict[str, _Record] = field(default_factory=dict, repr=False)
    market_contexts: dict[str, dict] = field(default_factory=dict, repr=False)
    active_id: str | None = None
    control: TurnControl | None = field(default=None, repr=False)
    messages_used: int = 0


def public_turn(result: AgentTurnResult, *, saved: SavedResearch | None = None) -> dict:
    """Explicit allowlist; never use the Agent's debug-oriented to_dict() here."""
    report = result.safe_answer
    request = result.confirmed_request
    analysis = result.research_result
    context, saved_at = "current", None
    followup = result.followup_answer
    chart = ({"version": "research-chart-v1", "status": "unavailable", "code": "chart_data_unavailable"}
             if followup is not None and followup.status == "accepted" else None)
    # A saved chart belongs only to the accepted answer that named this snapshot.
    if (type(saved) is SavedResearch and result.status == "completed"
            and followup is not None and followup.status == "accepted"
            and followup.request_id == saved.request.request_id):
        try:
            candidate = saved.result
            checked = build_research_report(candidate, expected_request=saved.request)
            if checked.status in ("complete", "limited"):
                report, request, analysis = checked, saved.request, candidate
                context, saved_at = "saved", saved.saved_at
        except Exception:
            # The optional snapshot view must not discard an already accepted answer.
            pass
    if analysis is not None:
        try:
            chart = build_chart_projection(analysis, expected_request=request, context=context, saved_at=saved_at)
        except Exception:
            # Optional visualization cannot turn a confirmed text response into a transport failure.
            chart = {"version": "research-chart-v1", "status": "unavailable", "code": "chart_integrity_mismatch"}
    understanding = result.understanding
    try:
        presentation = build_answer_presentation(result, report=report, request=request, analysis=analysis)
    except Exception:
        # An optional UI view must not discard an accepted answer or leak exception details.
        presentation = None
    return {
        "status": result.status, "code": result.code, "message": result.message, "stage": result.stage,
        "answer": result.display_answer, "report": report.to_dict() if report is not None else None,
        "presentation": presentation,
        "parameters": {**request_arguments(request), "source": request.source} if request else None,
        "chart": chart,
        "market_context": None,
        "candidates": [candidate.to_dict() for candidate in understanding.candidates[:5]] if understanding else [],
        "applied_defaults": list(understanding.applied_defaults) if understanding else [],
        "model_calls_attempted": result.model_calls_attempted, "analyses_used": result.analyses_used,
        "runtime": deepcopy(result.runtime),
        "validation_scope": "application_facts_and_checked_relations_not_arbitrary_model_prose",
    }


class ResearchApiService:
    """Own isolated capability-token sessions, bounded work and idempotent message results."""

    def __init__(self, *, model_factory, directory_loader, provider="fake", data_mode="synthetic",
                 development_source=None, reference_date=None, limits=None, market_context_loader=None,
                 clock: Callable[[], float] = monotonic, wall_clock=None, control_factory=TurnControl):
        self.limits = ApiLimits() if limits is None else limits
        if (type(self.limits) is not ApiLimits or provider not in ("fake", "deepseek")
                or data_mode not in DATA_MODES or not callable(model_factory)
                or not callable(directory_loader) or not callable(clock) or not callable(control_factory)
                or market_context_loader is not None and not callable(market_context_loader)):
            raise ModelConfigurationError("Use trusted API application configuration")
        if (data_mode == "synthetic") != (development_source is not None):
            raise ModelConfigurationError("Synthetic mode requires explicit trusted development data")
        ensure_data_mode_dependencies(data_mode)
        self.provider, self.data_mode = provider, data_mode
        # Development CSV overrides the online choice inside the Agent; never select CSV through chat input.
        self._research_source = "tencent" if data_mode == "synthetic" else data_mode
        self._model_factory, self._directory_loader = model_factory, directory_loader
        self._market_context_loader = market_context_loader
        self._development_source, self._reference_date = development_source, reference_date
        self._clock, self._wall_clock, self._control_factory = clock, wall_clock or (lambda: datetime.now(timezone.utc)), control_factory
        self._entries: dict[str, _Entry] = {}
        self._lock = Lock()
        self._active = 0
        self._closed = False

    def _prune(self):
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if self._closed or now < entry.created or now - entry.created >= self.limits.lifetime_seconds:
                if entry.control is not None:
                    entry.control.cancel()
                else:
                    entry.session.clear()
                    entry.market_contexts.clear()
                    del self._entries[key]

    def _entry(self, session_id, token):
        self._prune()
        if self._closed:
            raise ApiProblem(503, "service_closed", "服务正在关闭，请稍后重试。")
        entry = self._entries.get(session_id)
        valid_token = type(token) is str and token.isascii() and 1 <= len(token) <= 128
        if (entry is None or not valid_token or not compare_digest(entry.token, token)
                or self._clock() - entry.created >= self.limits.lifetime_seconds):
            raise ApiProblem(404, "session_not_found", "会话不存在、已失效或访问凭据不匹配。")
        return entry

    def create_session(self):
        with self._lock:
            self._prune()
            if self._closed:
                raise ApiProblem(503, "service_closed", "服务正在关闭。")
            if len(self._entries) >= self.limits.sessions:
                raise ApiProblem(429, "session_capacity", "本机服务会话数已满，请删除不用的会话。")
            try:
                model = self._model_factory()
            except Exception:
                raise ApiProblem(503, "model_configuration_error", "服务端模型配置不可用，请在服务端检查。") from None
            session_id, token = uuid4().hex, token_urlsafe(32)
            self._entries[session_id] = _Entry(token, self._clock(), _SessionModel(model, self.limits.model_calls))
            return {"session_id": session_id, "access_token": token, "token_type": "bearer",
                    "expires_in_seconds": self.limits.lifetime_seconds,
                    "provider": self.provider, "data_mode": self.data_mode,
                    "limits": {"messages": self.limits.messages, "model_calls": self.limits.model_calls}}

    def _view(self, entry):
        return {"state": "busy" if entry.control is not None else entry.session.to_dict()["state"],
                "active_request_id": entry.active_id,
                "messages_used": entry.messages_used, "model_calls_forwarded": entry.model.forwarded,
                "research": None if entry.control is not None else entry.session.to_dict()}

    def inspect(self, session_id, token):
        with self._lock:
            return self._view(self._entry(session_id, token))

    def _admit(self, session_id, token, request_id, question, *, history=False):
        digest = sha256(question.encode("utf-8")).hexdigest()
        with self._lock:
            entry = self._entry(session_id, token)
            record = entry.records.get(request_id)
            if record is not None:
                if record.digest != digest:
                    raise ApiProblem(409, "request_id_conflict", "同一请求编号不能用于不同问题。")
                if record.retired:
                    raise ApiProblem(409, "request_id_retired", "会话已重置，请使用新的请求编号。")
                if record.response is not None:
                    return entry, None, {**deepcopy(record.response), "replayed": True}
                if record.problem is not None:
                    raise ApiProblem(*record.problem)
                raise ApiProblem(409, "request_in_progress", "该请求仍在执行，没有再次调用模型。")
            if entry.control is not None:
                raise ApiProblem(409, "session_busy", "会话正在处理另一条请求，请等待结束或取消对应请求。")
            if self._active >= self.limits.concurrent_turns:
                raise ApiProblem(429, "service_busy", "本机并行研究数已满，请稍后使用同一请求编号重试。")
            if not history and entry.messages_used >= self.limits.messages:
                raise ApiProblem(429, "session_message_limit", "本会话问题次数已用完；仍可查看已有结果。")
            control = (TurnControl(TurnLimits(model_calls=0, analyses=0)) if history else self._control_factory())
            if type(control) is not TurnControl:
                raise ModelConfigurationError("Control factory must create a fresh trusted TurnControl")
            if not history:
                entry.messages_used += 1
                entry.records[request_id] = _Record(digest)
            entry.active_id, entry.control = request_id, control
            self._active += 1
            return entry, control, None

    def _saved_for_view(self, entry, result, directory, timestamp):
        """Recheck committed research, never promote stopped-but-retained facts to memory."""
        if result.runtime is not None and result.runtime.get("stop_code") is not None:
            return None
        followup = result.followup_answer
        accepted_followup = followup is not None and followup.status == "accepted"
        research = result.research_result is not None and result.confirmed_request is not None
        if result.status not in ("completed", "limited") or not (accepted_followup or research):
            return None
        reference = (self._reference_date if self._reference_date is not None else
                     timestamp.astimezone(timezone(timedelta(hours=8))).date())
        try:
            lookup = entry.session.saved_result(directory=directory,
                source="csv" if self._development_source is not None else self._research_source,
                development_source=self._development_source, reference_date=reference,
                now=timestamp, allow_history=accepted_followup and followup.historical)
            expected = followup.request_id if accepted_followup else result.confirmed_request.request_id
            if lookup.status == "ready" and lookup.snapshot is not None and lookup.snapshot.request.request_id == expected:
                return lookup.snapshot
        except Exception:
            # Optional views cannot discard an already confirmed core answer.
            pass
        return None

    def _market_context_view(self, entry, result, saved, timestamp):
        """Prepare bounded local context; history only reads the originally frozen projection."""
        followup = result.followup_answer
        if saved is None:
            return None, None
        key = saved.request.request_id
        if followup is not None:
            value = entry.market_contexts.get(key, {
                "version": "research-market-context-v1", "status": "unavailable", "code": "context_cache_miss"})
            return deepcopy(value), None
        if key in entry.market_contexts:
            return deepcopy(entry.market_contexts[key]), None
        if len(entry.market_contexts) >= self.limits.messages:
            return {"version": "research-market-context-v1", "status": "unavailable", "code": "context_cache_limit"}, None
        try:
            snapshot = self._market_context_loader(timestamp) if self._market_context_loader is not None else None
            value = build_market_context(saved.result, expected_request=saved.request,
                                         snapshot=snapshot, reference_at=timestamp)
        except Exception:
            value = {"version": "research-market-context-v1", "status": "unavailable", "code": "context_unusable"}
        return deepcopy(value), (key, deepcopy(value))

    def message(self, session_id, token, request_id, question, *, history=False):
        entry, control, replay = self._admit(session_id, token, request_id, question, history=history)
        if replay is not None:
            return replay
        problem, response, staged_context = None, None, None
        reset_contexts = False
        try:
            try:
                timestamp = self._wall_clock()
                directory = self._directory_loader(timestamp)
            except (DataSourceError, DataValidationError, InvalidRequestError, OSError):
                raise ApiProblem(503, "directory_unusable", "当前本地证券目录或夹具不可用，未自动刷新或调用模型。") from None
            result = run_natural_research_turn(question, model=entry.model, directory=directory,
                source=self._research_source,
                development_source=self._development_source, reference_date=self._reference_date,
                session=entry.session, current_time=timestamp, control=control)
            saved = self._saved_for_view(entry, result, directory, timestamp)
            projected = public_turn(result, saved=saved)
            projected["market_context"], staged_context = self._market_context_view(entry, result, saved, timestamp)
            reset_contexts = result.code == "session_reset"
            response = {"request_id": request_id, "replayed": False, "result": projected}
        except ApiProblem as error:
            problem = error
        except Exception:
            control.cancel()
            # No provider exception, traceback or request data is returned or logged.
            problem = ApiProblem(500, "research_internal_error", "本轮服务处理失败；没有自动重试，请检查服务端。")
        finally:
            with self._lock:
                if reset_contexts:
                    entry.market_contexts.clear()
                if problem is None and staged_context is not None:
                    key, value = staged_context
                    if len(entry.market_contexts) < self.limits.messages or key in entry.market_contexts:
                        entry.market_contexts[key] = deepcopy(value)
                entry.active_id, entry.control = None, None
                self._active -= 1
                if not history:
                    record = entry.records[request_id]
                    record.response = deepcopy(response)
                    record.problem = (problem.status, problem.code, problem.message) if problem else None
                self._prune()
        if problem is not None:
            raise problem
        return response

    def history(self, session_id, token):
        # History lookup never calls the model or consumes a message allowance.
        return self.message(session_id, token, uuid4().hex, "查看上次结果", history=True)

    def cancel(self, session_id, token, request_id):
        with self._lock:
            entry = self._entry(session_id, token)
            if entry.control is None or entry.active_id != request_id or not entry.control.cancel():
                raise ApiProblem(409, "request_not_cancellable", "该编号不是当前可取消请求；未取消其他请求。")
            return {"status": "cancellation_requested", "request_id": request_id,
                    "message": "已发出取消信号，不保证远程请求已中断；请等待原请求返回。"}

    def reset(self, session_id, token):
        with self._lock:
            entry = self._entry(session_id, token)
            if entry.control is not None:
                raise ApiProblem(409, "session_busy", "请先取消并等待当前请求结束，再重置会话。")
            entry.session.clear()
            entry.market_contexts.clear()
            for record in entry.records.values():
                record.response, record.problem, record.retired = None, None, True
            return {"status": "reset", "session": self._view(entry)}

    def delete(self, session_id, token):
        with self._lock:
            entry = self._entry(session_id, token)
            if entry.control is not None:
                raise ApiProblem(409, "session_busy", "请先取消并等待当前请求结束，再删除会话。")
            entry.session.clear()
            entry.market_contexts.clear()
            del self._entries[session_id]

    def close(self):
        with self._lock:
            self._closed = True
            for entry in self._entries.values():
                if entry.control is not None:
                    entry.control.cancel()
                else:
                    entry.session.clear()
                    entry.market_contexts.clear()
            self._entries = {key: entry for key, entry in self._entries.items() if entry.control is not None}
