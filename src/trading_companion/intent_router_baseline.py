"""Call the frozen production router with fixed synthetic state, never duplicate its semantic rules."""

from datetime import datetime, timezone
import re

from trading_companion import request_routing as routing
from trading_companion.product_plan import _execution_requested, _INJECTION
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.intent_router_task import LABELS, valid_context

NOW = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
SECURITIES = (Security("000001.SZ", "平安银行", ("平安",)), Security("600000.SH", "浦发银行"),
              Security("601318.SH", "中国平安", ("平安",)), Security("600036.SH", "招商银行"))


def protected_reason(row):
    """Evaluation admission only; no new product router or execution permission."""
    question, guard = row["question"], row["guard"]
    if guard != "none":
        return guard
    text = routing._command_text(question)
    if text in routing._COMMANDS:
        return "command"
    if text in routing._HISTORY:
        return "history"
    if re.match(r"\s*新问题\s*[:：]", question):
        return "new_question"
    if _execution_requested(question) or _INJECTION.search(question):
        return "execution_or_injection"
    if not row["context"]["company_enabled"] or not row["context"]["workflow_enabled"]:
        return "feature_disabled"
    return None


def rule_classify(row):
    value = row["context"]
    valid_context(value)
    quotes = object()
    directory = SecurityDirectory(SECURITIES, "intent-evaluation-artificial", NOW)
    saved = SavedScreening(NOW, ScreeningRequest((ScreeningCondition("price", "gt", 0),), limit=3),
                           SECURITIES[:2], quotes) if value["has_shortlist"] else None
    view = routing.RoutingSessionView(product_saved=True if value["has_saved_company"] else None,
        confirmed=True if value["has_saved_technical"] else None, screening_saved=saved,
        product_pending=True if row["guard"] in ("company_pending", "state_conflict") else None,
        pending=True if row["guard"] in ("technical_pending", "state_conflict") else None,
        pending_edit=True if row["guard"] == "indicator_pending" else None)
    from dataclasses import replace
    view = replace(view, latest_kind=value["latest_valid_research_kind"]
                   if value["latest_valid_research_kind"] in ("company", "technical") else None)
    question = row["question"]
    probe = routing.probe_general_knowledge(question, workflow=value["workflow_enabled"], history=row["guard"] == "history", session=view)
    result = routing.resolve_general_knowledge(question, candidate=probe, directory=directory)
    if result is None:
        result = routing.route_research_request(question, context=routing.RoutingContext(view, directory,
            history=row["guard"] == "history", company_enabled=value["company_enabled"], quote_source=quotes,
            workflow=value["workflow_enabled"]), clock=lambda: NOW)
    return {"status": "ok", "code": "ok", "prediction": result.intent.value if result.intent.value in LABELS else None,
            "raw_intent": result.intent.value, "rule_id": result.rule_id,
            "executor_clarification": "not_determined", "attempted": False, "model": "frozen_rules",
            "confidence": None, "usage": None}
