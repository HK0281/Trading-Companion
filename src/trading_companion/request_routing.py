"""Read-only selection of existing API executors; routing is not authorization."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import unicodedata

from trading_companion.indicator_knowledge import parse_knowledge_question
from trading_companion.open_knowledge import is_general_knowledge_question
from trading_companion.product_agent import is_product_followup
from trading_companion.product_plan import is_explicit_technical_request, wants_product_research
from trading_companion.research_decision import wants_decision
from trading_companion.research_session import ResearchSession
from trading_companion.screening_context import SavedScreening, is_screening_followup
from trading_companion.screening_intent import is_screening_edit, is_screening_request
from trading_companion.security_directory import SecurityDirectory
from trading_companion.routing_context import independent_task, latest_subject, continuation_question, new_question


class RouteKind(str, Enum):
    GENERAL_KNOWLEDGE = "general_knowledge"
    SCREENING_EDIT = "screening_edit"
    SCREENING = "screening"
    SCREENING_FOLLOWUP = "screening_followup"
    COMPANY_RESEARCH = "company_research"
    TECHNICAL_RESEARCH = "technical_research"
    LOCAL_REPLY = "local_reply"


class IntentKind(str, Enum):
    COMMAND = "command"
    HISTORY = "history"
    STATE_CONFLICT = "state_conflict"
    INDICATOR_KNOWLEDGE = "indicator_knowledge"
    GENERAL_KNOWLEDGE = "general_knowledge"
    KNOWLEDGE_DISABLED = "knowledge_disabled"
    SCREENING = "screening"
    COMPANY_CLARIFICATION = "company_clarification"
    TECHNICAL_CLARIFICATION = "technical_clarification"
    INDICATOR_CLARIFICATION = "indicator_clarification"
    SCREENING_EDIT = "screening_edit"
    SCREENING_FOLLOWUP = "screening_followup"
    COMPANY_RESEARCH = "company_research"
    TECHNICAL_RESEARCH = "technical_research"


@dataclass(frozen=True)
class RoutingSessionView:
    """Presence markers preserve legacy `is None` checks without exposing mutation."""

    product_pending: bool | None = None
    pending: bool | None = None
    pending_edit: bool | None = None
    product_saved: bool | None = None
    confirmed: bool | None = None
    screening_saved: SavedScreening | None = None
    latest_kind: str | None = None
    latest_current: bool = True

    @property
    def pending_count(self) -> int:
        return sum(item is not None for item in (self.product_pending, self.pending, self.pending_edit))

    @classmethod
    def capture(cls, session: ResearchSession) -> "RoutingSessionView":
        return cls(product_pending=True if session.product_pending is not None else None,
            pending=True if session.pending is not None else None,
            pending_edit=True if session.pending_edit is not None else None,
            product_saved=True if session.product_saved is not None else None,
            confirmed=True if session.confirmed is not None else None,
            screening_saved=session.screening_saved,
            latest_kind=getattr(latest_subject(session), "kind", None),
            latest_current=session._subject_current if session.latest_research is not None else True)


@dataclass(frozen=True)
class RoutingContext:
    session: RoutingSessionView
    directory: SecurityDirectory
    history: bool = False
    company_enabled: bool = False
    quote_source: object | None = None
    research_source: str = "tencent"
    development: bool = False
    workflow: bool = True


@dataclass(frozen=True)
class RouteDecision:
    kind: RouteKind
    rule_id: str
    intent: IntentKind


@dataclass(frozen=True)
class _MatchInput:
    question: str
    # Knowledge is resolved before mandatory directory admission. The remaining
    # rules always have a research context and the original trusted wall clock.
    directory: SecurityDirectory | None = None
    context: RoutingContext | None = None
    clock: Callable[[], datetime] | None = None


@dataclass(frozen=True)
class RouteSpec:
    intent: IntentKind
    kind: RouteKind
    priority: int
    rule_id: str
    matches: Callable[[_MatchInput], bool]

    def decision(self) -> RouteDecision:
        return RouteDecision(self.kind, self.rule_id, self.intent)


def probe_general_knowledge(question: str, *, workflow: bool, history: bool,
                            session: RoutingSessionView | None = None) -> bool:
    """Whether the API should attempt the optional local identity hint first."""
    return (workflow and not history
            and parse_knowledge_question(question) is None
            and is_general_knowledge_question(question))


def _knowledge(value: _MatchInput) -> bool:
    directory = value.context.directory if value.context is not None else value.directory
    return is_general_knowledge_question(value.question, directory=directory)


_COMMANDS = ("取消", "取消分析", "取消研究", "算了", "新对话", "重新开始", "清空会话")
_HISTORY = ("查看上次结果", "查看上一次结果", "上次研究结果")


def _command_text(question: str) -> str:
    return unicodedata.normalize("NFKC", question).strip().rstrip("。.!！").strip()


def _command(value: _MatchInput) -> bool:
    return not value.context.history and _command_text(value.question) in _COMMANDS


def _history(value: _MatchInput) -> bool:
    return value.context.history or _command_text(value.question) in _HISTORY


def _conflict(value: _MatchInput) -> bool:
    return value.context.session.pending_count > 1 and not independent_task(value.question, value.context.directory)


def _indicator_knowledge(value: _MatchInput) -> bool:
    return parse_knowledge_question(value.question) is not None


def _general_enabled(value: _MatchInput) -> bool:
    return value.context.workflow and _knowledge(value)


def _general_disabled(value: _MatchInput) -> bool:
    return not value.context.workflow and _knowledge(value)


def _screening_edit(value: _MatchInput) -> bool:
    context = value.context
    return (context.session.pending_count == 0
            and is_screening_edit(value.question, session=context.session))


def _screening(value: _MatchInput) -> bool:
    return (is_screening_request(value.question)
            and not is_screening_edit(value.question, session=value.context.session))


def _company_pending(value: _MatchInput) -> bool:
    return (value.context.company_enabled and value.context.session.product_pending is not None
            and not independent_task(value.question, value.context.directory))


def _technical_pending(value: _MatchInput) -> bool:
    return value.context.session.pending is not None and not independent_task(value.question, value.context.directory)


def _indicator_pending(value: _MatchInput) -> bool:
    return value.context.session.pending_edit is not None and not independent_task(value.question, value.context.directory)


def _screening_followup(value: _MatchInput) -> bool:
    context = value.context
    return (context.session.pending_count == 0
            and is_screening_followup(value.question, session=context.session, directory=context.directory,
                now=value.clock(), quote_source=context.quote_source, source=context.research_source,
                development=context.development))


def _company(value: _MatchInput) -> bool:
    context = value.context
    return context.company_enabled and (
        wants_product_research(value.question)
        or bool(context.directory.mentioned_spans(value.question)) and not is_explicit_technical_request(value.question)
        or not new_question(value.question) and (context.session.latest_kind == "company" and context.session.latest_current
            or context.session.latest_kind is None and context.session.product_saved is not None)
            and (continuation_question(value.question) or is_product_followup(value.question)))


def _technical(value: _MatchInput) -> bool:
    return True


def _ordered_specs(specs: tuple[RouteSpec, ...]) -> tuple[RouteSpec, ...]:
    if (len({spec.priority for spec in specs}) != len(specs)
            or len({spec.intent for spec in specs}) != len(specs)
            or len({spec.rule_id for spec in specs}) != len(specs)):
        raise ValueError("Route priorities, intents and reason codes must be unique")
    return tuple(sorted(specs, key=lambda spec: spec.priority))


# Static application policy, not a registry supplied by a client or a model.
# Several semantic intents deliberately share one existing executor.
ROUTE_SPECS = _ordered_specs((
    RouteSpec(IntentKind.COMMAND, RouteKind.TECHNICAL_RESEARCH, 0, "session.command", _command),
    RouteSpec(IntentKind.HISTORY, RouteKind.TECHNICAL_RESEARCH, 1, "session.history", _history),
    RouteSpec(IntentKind.STATE_CONFLICT, RouteKind.LOCAL_REPLY, 5, "session.pending_conflict", _conflict),
    RouteSpec(IntentKind.INDICATOR_KNOWLEDGE, RouteKind.TECHNICAL_RESEARCH, 10, "knowledge.indicator", _indicator_knowledge),
    RouteSpec(IntentKind.GENERAL_KNOWLEDGE, RouteKind.GENERAL_KNOWLEDGE, 11, "knowledge.general", _general_enabled),
    RouteSpec(IntentKind.KNOWLEDGE_DISABLED, RouteKind.LOCAL_REPLY, 12, "knowledge.disabled", _general_disabled),
    RouteSpec(IntentKind.SCREENING, RouteKind.SCREENING, 20, "screening.new", _screening),
    RouteSpec(IntentKind.COMPANY_CLARIFICATION, RouteKind.COMPANY_RESEARCH, 30, "clarification.company", _company_pending),
    RouteSpec(IntentKind.TECHNICAL_CLARIFICATION, RouteKind.TECHNICAL_RESEARCH, 31, "clarification.technical", _technical_pending),
    RouteSpec(IntentKind.INDICATOR_CLARIFICATION, RouteKind.TECHNICAL_RESEARCH, 32, "clarification.indicator", _indicator_pending),
    RouteSpec(IntentKind.SCREENING_EDIT, RouteKind.SCREENING_EDIT, 40, "screening.edit", _screening_edit),
    RouteSpec(IntentKind.SCREENING_FOLLOWUP, RouteKind.SCREENING_FOLLOWUP, 50, "screening.followup", _screening_followup),
    RouteSpec(IntentKind.COMPANY_RESEARCH, RouteKind.COMPANY_RESEARCH, 60, "research.company", _company),
    RouteSpec(IntentKind.TECHNICAL_RESEARCH, RouteKind.TECHNICAL_RESEARCH, 70, "research.technical", _technical),
))
_KNOWLEDGE_SPEC = next(spec for spec in ROUTE_SPECS if spec.intent == IntentKind.GENERAL_KNOWLEDGE)


def resolve_general_knowledge(question: str, *, candidate: bool,
                              directory: SecurityDirectory | None) -> RouteDecision | None:
    """Resolve only a probed candidate; directory hint failure is handled by API."""
    if candidate and _knowledge(_MatchInput(question, directory=directory)):
        return _KNOWLEDGE_SPEC.decision()
    return None


def route_research_request(question: str, *, context: RoutingContext,
                           clock: Callable[[], datetime]) -> RouteDecision:
    """Read-only policy; identity, source, TTL and publication stay with executors."""
    value = _MatchInput(question, context=context, clock=clock)
    for spec in ROUTE_SPECS:
        if spec.matches(value):
            if spec.intent in (IntentKind.COMMAND, IntentKind.HISTORY):
                # Preserve the old no-pending command's observed clock read. This
                # is not an authorization check and cannot renew a saved list.
                if not context.history and context.session.pending_count == 0:
                    clock()
                company = context.company_enabled and (
                    context.session.product_pending is not None
                    or spec.intent == IntentKind.HISTORY and (_company(value)
                        or context.history and context.session.product_saved is not None))
                if spec.intent == IntentKind.COMMAND and context.session.pending_count > 1:
                    company = False
                return RouteDecision(RouteKind.COMPANY_RESEARCH if company else RouteKind.TECHNICAL_RESEARCH,
                                     spec.rule_id, spec.intent)
            return spec.decision()
    raise AssertionError("The static technical fallback must always select a route")
