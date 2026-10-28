"""Read-only selection of existing API executors; routing is not authorization."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trading_companion.indicator_knowledge import parse_knowledge_question
from trading_companion.open_knowledge import is_general_knowledge_question
from trading_companion.product_agent import is_product_followup
from trading_companion.product_plan import is_explicit_technical_request, wants_product_research
from trading_companion.research_session import ResearchSession
from trading_companion.screening_context import SavedScreening, is_screening_followup
from trading_companion.screening_intent import is_screening_edit, is_screening_request
from trading_companion.security_directory import SecurityDirectory


class RouteKind(str, Enum):
    GENERAL_KNOWLEDGE = "general_knowledge"
    SCREENING_EDIT = "screening_edit"
    SCREENING = "screening"
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

    @classmethod
    def capture(cls, session: ResearchSession) -> "RoutingSessionView":
        return cls(product_pending=True if session.product_pending is not None else None,
            pending=True if session.pending is not None else None,
            pending_edit=True if session.pending_edit is not None else None,
            product_saved=True if session.product_saved is not None else None,
            confirmed=True if session.confirmed is not None else None,
            screening_saved=session.screening_saved)


@dataclass(frozen=True)
class RoutingContext:
    session: RoutingSessionView
    directory: SecurityDirectory
    history: bool = False
    company_enabled: bool = False
    quote_source: object | None = None
    research_source: str = "tencent"
    development: bool = False


@dataclass(frozen=True)
class RouteDecision:
    kind: RouteKind
    rule_id: str


@dataclass(frozen=True)
class _MatchInput:
    question: str
    # Knowledge is resolved before mandatory directory admission. The remaining
    # rules always have a research context and the original trusted wall clock.
    directory: SecurityDirectory | None = None
    context: RoutingContext | None = None
    product: bool = False
    clock: Callable[[], datetime] | None = None


@dataclass(frozen=True)
class RouteSpec:
    kind: RouteKind
    priority: int
    rule_id: str
    matches: Callable[[_MatchInput], bool]

    def decision(self) -> RouteDecision:
        return RouteDecision(self.kind, self.rule_id)


def probe_general_knowledge(question: str, *, workflow: bool, history: bool) -> bool:
    """Whether the API should attempt the optional local identity hint first."""
    return (workflow and not history and parse_knowledge_question(question) is None
            and is_general_knowledge_question(question))


def _knowledge(value: _MatchInput) -> bool:
    return is_general_knowledge_question(value.question, directory=value.directory)


def _screening_edit(value: _MatchInput) -> bool:
    context = value.context
    return (not context.history and parse_knowledge_question(value.question) is None
            and context.session.product_pending is None and context.session.pending is None
            and context.session.pending_edit is None
            and is_screening_edit(value.question, session=context.session))


def _screening(value: _MatchInput) -> bool:
    return is_screening_request(value.question)


def _screening_followup(value: _MatchInput) -> bool:
    context = value.context
    return (not context.history and parse_knowledge_question(value.question) is None
            and context.session.product_pending is None and context.session.pending is None
            and context.session.pending_edit is None
            and is_screening_followup(value.question, session=context.session, directory=context.directory,
                now=value.clock(), quote_source=context.quote_source, source=context.research_source,
                development=context.development))


def _company(value: _MatchInput) -> bool:
    return value.product


def _technical(value: _MatchInput) -> bool:
    return True


def _ordered_specs(specs: tuple[RouteSpec, ...]) -> tuple[RouteSpec, ...]:
    if (len({spec.priority for spec in specs}) != len(specs)
            or len({spec.kind for spec in specs}) != len(specs)
            or len({spec.rule_id for spec in specs}) != len(specs)):
        raise ValueError("Route priorities, paths and reason codes must be unique")
    return tuple(sorted(specs, key=lambda spec: spec.priority))


# This table describes legacy precedence, including its existing inconsistencies.
# It is static application code, not a handler registry supplied by a client/model.
ROUTE_SPECS = _ordered_specs((
    RouteSpec(RouteKind.GENERAL_KNOWLEDGE, 10, "legacy.general_knowledge", _knowledge),
    RouteSpec(RouteKind.SCREENING_EDIT, 20, "legacy.screening_edit", _screening_edit),
    RouteSpec(RouteKind.SCREENING, 30, "legacy.screening", _screening),
    RouteSpec(RouteKind.SCREENING_FOLLOWUP, 40, "legacy.screening_followup", _screening_followup),
    RouteSpec(RouteKind.COMPANY_RESEARCH, 50, "legacy.company_research", _company),
    RouteSpec(RouteKind.TECHNICAL_RESEARCH, 60, "legacy.technical_research", _technical),
))
_KNOWLEDGE_SPEC = ROUTE_SPECS[0]
_RESEARCH_SPECS = ROUTE_SPECS[1:]


def resolve_general_knowledge(question: str, *, candidate: bool,
                              directory: SecurityDirectory | None) -> RouteDecision | None:
    """Resolve only a probed candidate; directory hint failure is handled by API."""
    if candidate and _KNOWLEDGE_SPEC.matches(_MatchInput(question, directory=directory)):
        return _KNOWLEDGE_SPEC.decision()
    return None


def route_research_request(question: str, *, context: RoutingContext,
                           clock: Callable[[], datetime]) -> RouteDecision:
    """Select after directory loading; retain short circuiting and lazy clock reads."""
    # Deliberately evaluated before screening rules, just as in the original API.
    product = context.company_enabled and (
        context.session.product_pending is not None or wants_product_research(question)
        or bool(context.directory.mentioned_spans(question)) and not is_explicit_technical_request(question)
        or context.session.product_saved is not None and is_product_followup(question)
        or context.history and context.session.product_saved is not None)
    value = _MatchInput(question, context=context, product=product, clock=clock)
    for spec in _RESEARCH_SPECS:
        if spec.matches(value):
            return spec.decision()
    raise AssertionError("The static technical fallback must always select a route")
