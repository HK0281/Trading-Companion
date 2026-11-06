"""Application-owned horizon selection, scoped to one expiring research task."""

from dataclasses import dataclass, replace
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
RECALC_NOTICE = "如需按新期限重算价位，请明确提出。"
# This grammar gates a read-only permission projection, never a new intent route.
RECALC_PREFIXES = ('那', '那么', '如果', '要是', '按', '用', '看', '换', '换成', '改', '改成', '改为', '调成')
RECALC_SUFFIXES = ('呢', '吧', '的话', '看看', '试试', '怎么样', '如何', '会怎样')


def recalculation_horizon(question):
    from trading_companion.research_objective import mask_outlook_expressions
    if type(question) is not str:
        return None
    try:
        outlook = extract_outlook(question)
        if outlook is None or outlook.horizon is None:
            return None
        rest = mask_outlook_expressions(question)
        words = (*RECALC_PREFIXES, *RECALC_SUFFIXES, '是')
        grammar = '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        if re.fullmatch(r'(?:\s|[，,。.!！?？:：;；]|' + grammar + r')*', rest) is None:
            return None
        return canonical(outlook.horizon)
    except (InputIssue, ValueError):
        return None


@dataclass(frozen=True)
class DeliveredPrices:
    """A successful program delivery, separate from unanswered clarification slots."""
    task: str
    subjects: tuple
    source: str
    development: bool
    reference_date: object
    delivered_at: object
    native_plan: object

    @property
    def expires_at(self):
        return self.delivered_at + timedelta(minutes=15)


def valid_delivery(session, *, directory, now, reference_date, source, development):
    record = session._delivered_prices
    if (type(record) is not DeliveredPrices or record.source != source or record.development != development
            or record.reference_date != reference_date or not record.delivered_at <= now < record.expires_at):
        return None
    from trading_companion.product_plan import ProductPlan
    from trading_companion.index_contracts import IndexResearchRequest, identity
    from trading_companion.errors import DataValidationError
    try:
        directory.assert_usable(reference_date, development=development)
    except DataValidationError:
        return None
    if record.task == 'stock_research' and type(record.native_plan) is ProductPlan:
        plan = record.native_plan
        if (not requires_prices(plan) or not permits_prices(plan)
                or tuple(s.instrument_id for s in plan.securities) != record.subjects
                or any(directory.lookup(s.instrument_id).candidates != (s,) for s in plan.securities)):
            return None
    elif record.task == 'index_research' and type(record.native_plan) is IndexResearchRequest:
        if not record.native_plan.scenario_requested or record.subjects != (identity(record.subjects[0]).instrument_id,):
            return None
    else:
        return None
    return record


def project_delivery(context, question, *, development_source=None):
    """Only a detached context copy sees this synthetic native horizon pending."""
    from copy import copy
    session = context.session
    if any(v is not None for v in (session.pending, session.pending_edit, session.product_pending, session.index_pending)):
        return context
    record = valid_delivery(session, directory=context.directory, now=context.now,
        reference_date=context.reference_date, source=context.source, development=context.development)
    if record is None or recalculation_horizon(question) is None:
        return context
    projected = copy(session)
    projected._turn_parent = None
    if record.task == 'stock_research':
        from trading_companion.product_plan import ProductPlanContext
        p = record.native_plan
        projected.product_pending = ProductPlanContext(p.user_question, record.delivered_at, record.expires_at,
            record.reference_date, record.source, record.development, record.subjects,
            tuple((s, s) for s in record.subjects), '', (), p.kinds, p.start_date, p.end_date,
            p.explicit_historical, p.wants_risk, replace(p.objective, horizon=None), p.applied_defaults,
            p.indicators, p.indicators_explicit, p.scope, stage='horizon', securities=p.securities,
            checked_prices=True)
    else:
        from trading_companion.index_agent import PendingIndexResearch
        p = record.native_plan
        projected.index_pending = PendingIndexResearch('请给出建仓价位', record.delivered_at, record.source,
            development_source, stage='horizon', instrument_id=p.instrument_id,
            benchmark_defaulted=p.benchmark_defaulted, reference_date=record.reference_date,
            base=replace(p, horizon=None))
    return replace(context, session=projected)


def remember_delivery(session, result, *, now, reference_date, development):
    """Call only from checked execution, after answer validation, before atomic publication."""
    if result.status not in ('completed', 'limited') or result._is_stopped:
        return
    plan, bundle, answer = result.product_plan, result.product_bundle, result.product_answer
    if (plan is not None and bundle is not None and answer is not None and answer.status == 'accepted'
            and plan.checked_prices is True and permits_prices(plan)
            and any(any(p.status == 'available' for p in c.trade_plans)
                    or c.risk is not None and c.risk.status == 'available' for c in bundle.companies)):
        session._delivered_prices = DeliveredPrices('stock_research', tuple(s.instrument_id for s in plan.securities),
            plan.source, development, reference_date, now, plan)
    elif (result.index_result is not None and result.index_research is not None
            and result.index_result.request.scenario_requested
            and result.index_research.get('scenario_plan', {}).get('status') == 'available'):
        p = result.index_result.request
        session._delivered_prices = DeliveredPrices('index_research', (p.instrument_id,), p.source,
            development, reference_date, now, p)


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
    if getattr(plan, 'checked_prices', None) is not None:
        return plan.checked_prices
    return (plan.wants_risk or plan.objective.kind == "decision" and wants_prices(plan.user_question))


def permits_prices(plan):
    return (getattr(plan, 'checked_prices', None) is not False and not excludes_plan(plan.user_question) and "technical" in plan.kinds
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
        stage="horizon", securities=plan.securities, checked_prices=plan.checked_prices,
        descriptive_risk=plan.descriptive_risk)
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
