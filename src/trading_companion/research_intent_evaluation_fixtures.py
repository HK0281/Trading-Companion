"""Small B-layer adapter over existing fixed sources and finite workflow scripts."""

from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from trading_companion.api_service import ResearchApiService, ApiProblem
from trading_companion.decision_demo import DecisionDemoModel
from trading_companion.research_intent_evaluation_cases import valid_context
from trading_companion.decision_horizon import canonical
from trading_companion.request_routing import RoutingSessionView
from trading_companion.workflow_evaluation import _fixed_authenticity
from trading_companion.workflow_evaluation_fixtures import FixedSources, ScriptedWorkflowModel, NOW, REFERENCE_DATE


class FixedModel(ScriptedWorkflowModel):
    """Reuse the existing v3 artificial answer, with no new classifier or adaptive intent."""

    def __init__(self):
        super().__init__()
        self.calls, self.intent_calls = 0, 0
        self.last_tools, self.error_type = [], None

    def complete(self, messages, *, tools):
        self.calls += 1
        self.last_tools = [t.name for t in tools]
        if tools and any("interpret" in t.name for t in tools):
            self.intent_calls += 1
        try:
            return super().complete(messages, tools=tools)
        except Exception as error:
            # Fixed synthetic diagnostics only; never save provider text or traceback.
            self.error_type = type(error).__name__
            raise

    def _respond(self, phase, messages, tools):
        if phase == "company_answer":
            return DecisionDemoModel().complete(messages, tools=tools)
        return super()._respond(phase, messages, tools)


def _outcome(result):
    code = result.get("code", "other")
    status = result.get("status")
    if status in ("completed", "limited"):
        return "research" if result.get("company_research") or result.get("parameters") or result.get("index_research") else "local"
    if status == "cancelled":
        return "local"
    if status in ("out_of_scope", "invalid_request"):
        return "reject"
    if status != "needs_clarification":
        return "error"
    if "horizon" in code:
        return "clarify_horizon"
    if "conflict" in code:
        return "clarify_conflict"
    if "identity" in code or "ambiguous" in code:
        return "clarify_identity"
    if any(t in code for t in ("instrument", "subject", "ungrounded")):
        return "clarify_subject"
    return "clarify_conditions"


def fixed_execution(state, probe):
    from scripts.verify import clean_environment, offline_network, VerificationError
    try:
        with clean_environment(), offline_network(), \
                patch("trading_companion.local_env.load_model_environment", side_effect=RuntimeError("offline credentials forbidden")), \
                patch("trading_companion.local_env._read_local_values", side_effect=RuntimeError("offline credentials forbidden")):
            return _fixed_execution(state, probe)
    except (VerificationError, RuntimeError):
        return {"status": "failed", "reason": "离线边界拒绝了联网或凭据读取，不能计为正确澄清", "steps": [], "fields": {}}


def _fixed_execution(state, probe):
    """No gold accepted. Unscripted cases remain unmeasured, never default to success."""
    if not probe["steps"]:
        return {"status": "not_measured", "reason": probe["unmeasured_reason"], "steps": [], "fields": {}}
    # Complex state construction is intentionally outside this small adapter.
    context = state["context"]
    valid_context(context)
    if (context["development"] or context["source"] != "tencent" or not context["company_enabled"]
            or not context["workflow_enabled"] or context["shortlist"]
            or context["latest"] and context["latest"]["age_seconds"] != 0
            or context["pending"] != "none" and (context["pending_age_seconds"] != 0
                or not all(context["pending_binding"].values()))):
        return {"status": "not_measured", "reason": "现有固定会话不模拟来源/模式/时钟/名单/待办绑定切换", "steps": [], "fields": {}}
    model, observations = FixedModel(), []
    with FixedSources("normal", 1) as sources:
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda now: sources.directory,
            provider="fake", data_mode="tencent", reference_date=REFERENCE_DATE,
            product_data_service=sources.company, screening_source=sources.quotes,
            wall_clock=lambda: NOW, conversation_context=context["conversation_memory"])
        try:
            credentials = service.create_session()
            for index, step in enumerate(probe["steps"]):
                if index == len(probe["steps"]) - 1:
                    entry = service._entry(credentials["session_id"], credentials["access_token"])
                    latest = entry.session.latest_research
                    expected = context["latest"]
                    actual = None if latest is None else dict(kind=latest.kind,
                        subjects=[s.instrument_id for s in latest.securities], age_seconds=int((NOW - latest.published_at).total_seconds()),
                        source=latest.source, development=latest.development, current=entry.session._subject_current,
                        horizon=canonical(getattr(getattr(latest.scope, "objective", None), "horizon", None)))
                    if actual != expected:
                        return {"status": "not_measured", "reason": "固定前置步骤未形成声明的最近研究状态", "steps": observations, "fields": {}}
                    view = RoutingSessionView.capture(entry.session)
                    pending = ("conflict" if view.pending_count > 1 else "company_horizon" if view.product_horizon_pending
                        else "company_identity" if view.product_pending else "technical" if view.pending
                        else "indicator" if view.pending_edit else "index_horizon" if view.index_horizon_pending
                        else "index_identity" if view.index_pending else "none")
                    caches = (entry.session.product_saved is not None, entry.session.confirmed is not None,
                              entry.session.index_saved is not None)
                    if pending != context["pending"] or caches != tuple(context[k] for k in ("has_company", "has_technical", "has_index")):
                        return {"status": "not_measured", "reason": "固定前置步骤未形成声明的待办/缓存状态", "steps": observations, "fields": {}}
                model.start_step(deepcopy(step["script"]))
                before, before_intent = model.calls, model.intent_calls
                try:
                    result = service.message(credentials["session_id"], credentials["access_token"], uuid4().hex,
                                             step["question"])["result"]
                except ApiProblem as error:
                    return {"status": "failed", "reason": error.code, "steps": observations, "fields": {},
                            "failed_step": {"question": step["question"], "model_calls": model.calls - before,
                                "intent_calls": model.intent_calls - before_intent, "tools": model.last_tools,
                                "fixture_error": model.failed, "error_type": model.error_type,
                                "script_phases": step["script"]["phases"]}}
                # Inspect program-owned checked state, not text guessed from the answer.
                entry = service._entry(credentials["session_id"], credentials["access_token"])
                session = entry.session
                company = result.get("company_research") or {}
                pending, saved = session.product_pending, session.product_saved
                plan = saved.plan if saved is not None and company else None
                if pending is not None and pending.stage == "horizon" and _outcome(result) == "clarify_horizon":
                    plan = pending
                public_plan = company.get("plan") or {}
                actual_plans = _fixed_authenticity(company.get("trade_plans", []))
                subjects = [s["instrument_id"] for s in company.get("securities", [])]
                if not subjects and plan is not None and _outcome(result) in ("research", "clarify_horizon"):
                    subjects = [s.instrument_id for s in plan.securities]
                observations.append({"question": step["question"], "status": result.get("status"), "code": result.get("code"),
                    "outcome": _outcome(result), "bound_subjects": subjects, "model_calls": model.calls - before,
                    "intent_calls": model.intent_calls - before_intent,
                    "objective_kind": plan.objective.kind if plan is not None else None,
                    "horizon": plan.objective.horizon if plan is not None else None,
                    "indicators": [s.kind for s in plan.indicators] if plan is not None else [],
                    "indicator_specs": [{"kind": s.kind, **s.parameters} for s in plan.indicators] if plan is not None else [],
                    "research_kinds": list(plan.kinds) if plan is not None else [],
                    "trade_plans": actual_plans, "public_plan": public_plan,
                    "fixture_error": bool(model.failed or model.remaining)})
        finally:
            service.close()
    final = observations[-1]
    if any(s["fixture_error"] for s in observations):
        return {"status": "failed", "reason": "既有固定模拟响应与实际调用不一致；未扩建模拟层", "steps": observations, "fields": {}}
    fields = {k: final[k] for k in ("outcome", "bound_subjects")}
    fields["zero_intent_calls"] = final["intent_calls"] == 0
    if final["objective_kind"] is not None:
        fields.update(objective_kind=final["objective_kind"], horizon=canonical(final["horizon"]))
    return {"status": "ok", "reason": None, "data_authenticity": "fixed_synthetic", "steps": observations, "fields": fields}
