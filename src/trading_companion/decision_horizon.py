"""Application-owned horizon selection, scoped to one expiring research task."""

from dataclasses import replace
from datetime import timedelta
import re

from trading_companion.agent_input import InputIssue
from trading_companion.research_objective import ResearchObjective, extract_outlook
from trading_companion.research_decision import excludes_plan, wants_prices

OPTIONS = (("short", "短线（1-2周）", "短线"), ("medium", "中线（1-3个月）", "中线"),
           ("long", "长线（半年以上）", "长线"))
REPRESENTATIVES = {"短线": "未来10个交易日", "中线": "未来40个交易日", "长线": "未来120个交易日"}
PARAMETERS = {"short": ("daily", "1.5", ("1.5", "2.5")),
              "medium": ("daily", "2.5", ("2", "3")),
              "long": ("weekly", "2", ("2", "3.5"))}
PROMPT = "你打算短线（1-2周）、中线（1-3个月）还是长线（半年以上）？"


def is_horizon_reply(question, directory=None):
    """Only a whole selection; educational questions such as 中线是什么 keep their route."""
    if type(question) is not str:
        return False
    text = question
    if directory is not None:
        from trading_companion.research_instruments import research_spans
        for start, end in reversed(research_spans(text, directory)):
            text = text[:start] + ' ' + text[end:]
    text = re.sub(r'还是|或者|我|打算|准备|选择|选|按|做|是|的|期限|大概|大约|就|用|看|或', '', text)
    text = text.strip(' ，,。.!！?？:：;；（）()')
    return bool(re.fullmatch(r'(?:(?:短|中|长)(?:线|期)|[1一]([-～~至到])[23二三](?:周|个月)|半年以上|'
                             r'(?:未来|持有|拿)[0-9一二两三四五六七八九十百半]+(?:个交易日|交易日|天|周|个月|年))+', text))


def canonical(horizon):
    return REPRESENTATIVES.get(horizon, horizon)


def profile(horizon):
    """Select formula without changing the user's explicit horizon or fetching future bars."""
    horizon = canonical(horizon)
    if horizon is None:
        raise ValueError("A confirmed horizon is required for prices")
    if horizon in ("明天", "后天", "下周"):
        return "short"
    if horizon == "下个月":
        return "medium"
    match = re.fullmatch(r"未来([1-9][0-9]{0,2})(个交易日|天|周|个月|年)", horizon)
    if not match:
        raise ValueError("Unsupported plan horizon")
    value, unit = int(match[1]), match[2]
    short, long = {"个交易日": (10, 120), "天": (14, 180), "周": (2, 26),
                   "个月": (0, 6), "年": (0, 1)}[unit]
    return "short" if value <= short else "long" if value >= long else "medium"


def requires_prices(plan):
    return (plan.wants_risk or plan.objective.kind == "decision" and wants_prices(plan.user_question))


def permits_prices(plan):
    return (not excludes_plan(plan.user_question) and "technical" in plan.kinds
            and "risk" not in plan.scope.excluded
            and any(spec.kind == "atr" and spec.parameters['window'] == 14 for spec in plan.indicators))


def select(plan, horizon, source):
    horizon = canonical(horizon)
    selected_profile = profile(horizon)
    defaults = tuple(item for item in plan.applied_defaults if not item.startswith(
        ("horizon:", "horizon_source:", "horizon_profile:")))
    updates = dict(objective=ResearchObjective(plan.objective.kind if plan.objective.kind != "analysis" else "outlook", horizon),
                   applied_defaults=(*defaults, "horizon_source:" + source, "horizon_profile:" + selected_profile))
    if selected_profile == "long" and "range:365_calendar_days" in defaults:
        updates["start_date"] = plan.end_date - timedelta(days=365)
        updates["applied_defaults"] = tuple("range:366_calendar_days" if v == "range:365_calendar_days" else v
                                           for v in updates["applied_defaults"])
    return replace(plan, **updates)


def gate(plan, *, now, reference_date, development):
    """Run after identity and scope inheritance, before all source calls."""
    if plan.status != "ready" or not requires_prices(plan) or not permits_prices(plan):
        return plan
    if plan.objective.horizon is not None:
        source = next((v.split(":", 1)[1] for v in plan.applied_defaults if v.startswith("horizon_source:")), "explicit")
        return select(plan, plan.objective.horizon, source)
    from trading_companion.product_plan import ProductPlanContext, CONTEXT_TTL
    created = plan.context_created_at or now
    expires = plan.context_expires_at or created + CONTEXT_TTL
    context = ProductPlanContext(plan.user_question, created, expires, reference_date,
        plan.source, development, tuple(s.instrument_id for s in plan.securities),
        tuple((s.instrument_id, s.instrument_id) for s in plan.securities), "", (), plan.kinds,
        plan.start_date, plan.end_date, plan.explicit_historical, plan.wants_risk, plan.objective,
        plan.applied_defaults, plan.indicators, plan.indicators_explicit, plan.scope,
        stage="horizon", securities=plan.securities)
    return replace(plan, status="needs_clarification", code="decision_horizon_required", message=PROMPT,
                   candidates=(), context=context)


def resume(context, base, question, *, directory):
    by_id = {item.instrument_id: item for item in directory.securities}
    if not context.securities or any(by_id.get(s.instrument_id) != s for s in context.securities):
        return replace(base, status="needs_clarification", code="product_identity_changed",
                       message="原研究股票的目录信息已变化，请重新提出完整问题。")
    from trading_companion.research_instruments import research_spans
    spans = research_spans(question, directory)
    text = question
    for start, end in reversed(spans):
        match = directory.lookup(question[start:end])
        if match.status != "resolved" or match.candidates[0] not in context.securities:
            return replace(base, status="needs_clarification", code="decision_horizon_instrument_mismatch",
                           message="期限回复中的股票与原研究不一致，请重新提出完整问题。", context=context)
        text = text[:start] + " " + text[end:]
    try:
        outlook = extract_outlook(text)
        if outlook is None or outlook.horizon is None:
            raise ValueError("Missing horizon")
        # Do not quietly discard additional scope/date/source instructions in a reply.
        from trading_companion.research_objective import mask_outlook_expressions
        rest = mask_outlook_expressions(text)
        rest = re.sub(r"我|打算|准备|选择|选|按|做|是|的|持有|拿|期限|大概|大约|就|用|看", "", rest)
        if rest.strip(" ，,。.!！?？:：;；（）()"):
            raise ValueError("Additional conditions require a complete question")
        selected = select(replace(base, status="ready", code="product_plan_ready", message="持有期限已确认。",
            securities=context.securities, context=None, context_created_at=context.created_at,
            context_expires_at=context.expires_at), outlook.horizon, "clarification")
        return selected
    except (InputIssue, ValueError) as error:
        code = error.code if isinstance(error, InputIssue) else "decision_horizon_required"
        return replace(base, status="needs_clarification", code=code, message=PROMPT, context=context,
                       securities=context.securities)
