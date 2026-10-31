"""Observe production rules without manufacturing a full semantic intent sheet."""

from trading_companion import request_routing as routing
from trading_companion.agent_input import InputIssue
from trading_companion.conversation_memory import is_recall_question
from trading_companion.decision_horizon import canonical
from trading_companion.product_plan import _execution_requested, _INJECTION
from trading_companion.research_decision import wants_decision, wants_prices, excludes_plan
from trading_companion.research_objective import extract_outlook
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.workflow_evaluation_fixtures import SECURITIES, NOW
from trading_companion.security_directory import SecurityDirectory
from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS, valid_context

UNKNOWN = "not_determined"


def rule_observation(state):
    """Accept only question/context, never row labels or execution scripts."""
    if state.keys() != {"question", "context"}:
        raise ValueError("Rule adapter accepts input state only")
    value, question = state["context"], state["question"]
    valid_context(value)
    observed = dict.fromkeys(GOLD_FIELDS, UNKNOWN)
    if value["conversation_memory"] and value["history"] and is_recall_question(question):
        return {"status": "ok", "fields": {**observed, "task": "recall"}, "raw": {"branch": "conversation_recall"}}
    directory = SecurityDirectory(SECURITIES, "v2-artificial-directory", NOW)
    quotes = object()
    saved = SavedScreening(NOW, ScreeningRequest((ScreeningCondition("price", "gt", 0),), limit=3),
                           tuple(s for s in SECURITIES if s.instrument_id in value["shortlist"]), quotes) if value["shortlist"] else None
    latest, pending = value["latest"], value["pending"]
    view = routing.RoutingSessionView(
        product_saved=True if value["has_company"] else None, confirmed=True if value["has_technical"] else None,
        screening_saved=saved, latest_kind=latest["kind"] if latest else None,
        latest_current=latest["current"] if latest else True,
        latest_shared=bool(latest and value["has_company"] and latest["kind"] in ("company", "technical")),
        product_pending=True if pending in ("company_identity", "company_horizon", "conflict") else None,
        product_horizon_pending=pending == "company_horizon",
        pending=True if pending in ("technical", "conflict") else None,
        pending_edit=True if pending == "indicator" else None,
        index_pending=True if pending in ("index_identity", "index_horizon") else None,
        index_horizon_pending=pending == "index_horizon")
    probe = routing.probe_general_knowledge(question, workflow=value["workflow_enabled"], history=False, session=view)
    decision = routing.resolve_general_knowledge(question, candidate=probe, directory=directory)
    if decision is None:
        decision = routing.route_research_request(question, context=routing.RoutingContext(view, directory,
            company_enabled=value["company_enabled"], quote_source=quotes, research_source=value["source"],
            development=value["development"], workflow=value["workflow_enabled"]), clock=lambda: NOW)
    raw = {"intent": decision.intent.value, "route": decision.kind.value, "rule_id": decision.rule_id,
           "wants_decision": wants_decision(question), "wants_prices": wants_prices(question),
           "excludes_plan": excludes_plan(question), "is_product_followup": routing.is_product_followup(question),
           "latest_age_seconds": latest["age_seconds"] if latest else None,
           "note": "分流使用现有状态视图；有效期、身份和最终发布由执行器检查，未从分流推断绑定。"}
    task = {"market_research": "index_research", "general_knowledge": "knowledge", "indicator_knowledge": "knowledge",
            "screening_edit": "screening", "screening_followup": "stock_research",
            "state_conflict": "clarify", "knowledge_disabled": "unsupported",
            "company_research": "stock_research", "technical_research": "stock_research",
            "company_clarification": "stock_research", "technical_clarification": "stock_research",
            "indicator_clarification": "stock_research"}.get(decision.intent.value, decision.intent.value)
    observed["task"] = task
    if _execution_requested(question) or _INJECTION.search(question):
        raw["execution_guard"] = True  # The route itself is deliberately not rewritten as a rejection.
    if task in ("stock_research", "index_research"):
        try:
            outlook = extract_outlook(question)
            observed["objective_kind"] = "decision" if wants_decision(question) else outlook.kind if outlook else "analysis"
            observed["horizon"] = canonical(outlook.horizon) if outlook else None
        except InputIssue as error:
            raw["horizon_error"] = error.code
        observed["wants_plan"] = wants_prices(question) and not excludes_plan(question)
    return {"status": "ok", "fields": observed, "raw": raw}
