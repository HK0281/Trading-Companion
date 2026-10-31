"""Company-research orchestration layered on the existing session and turn control."""

from dataclasses import dataclass, replace
from datetime import timedelta, timezone
import re

from trading_companion.agent import AgentTurnResult, _controlled, _market_reply
from trading_companion.model import ModelConfigurationError
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.product_plan import ProductPlan, prepare_product_plan, _execution_requested, _INJECTION
from trading_companion.product_scope import (
    ProductScope, _positive_scope, _ONLY, has_company_domain, is_product_indicator_edit,
)
from trading_companion.product_research import collect_product_research
from trading_companion.research_decision import wants_decision, excludes_plan, wants_prices
from trading_companion.turn_control import checkpoint, company_turn_control
from trading_companion.routing_context import (
    checked_subject, continuation_question, latest_subject, new_question, plural_subject, replaces_pending,
)


@dataclass(frozen=True)
class SavedProductResearch:
    saved_at: object
    plan: object
    bundle: object
    answer: object


def is_product_followup(question):
    return type(question) is str and (is_product_indicator_edit(question) or wants_decision(question) or bool(re.search(
        r"回测|历史回放|策略回放|周线|日周|多周期|背离|支撑|压力位|阻力|量价|放量|缩量|趋势强度|趋势强不强|"
        r"那.*(?:面|呢|如何)|为什么.*(?:判断|结论|看|说)|为何.*(?:判断|结论)|"
        r"(?:它|这只|这家|这两只|这三只).*(?:涨|跌|走势|方向|业务|盈利|风险)|"
        r"(?:解释|说明|详细|展开).*(?:判断|结论|依据|分析)|(?:还有|哪些).*(?:依据|风险)|"
        r"查看上次结果|上次研究结果|^(?:改成|改为|换成|再看|重新分析|继续分析|刷新)|"
        r"^(?:只|仅)?(?:看|分析|研究)?(?:技术面|基本面|新闻|公告|风险|止盈止损)(?:呢|如何|怎么样)?[。?？]?$", question)))


def _inherit_scope(plan, saved_plan, question):
    """A change of research domain does not change the historical cutoff or horizon."""
    if saved_plan is None or plan.end_date is None:
        return plan
    updates = {}
    refresh = re.search(r"最新|刷新|重新|今天|当前|现在", question)
    date_defaults = {"cutoff:previous_shanghai_day", "range:365_calendar_days"}
    if not refresh and date_defaults <= set(plan.applied_defaults):
        updates.update(start_date=saved_plan.start_date, end_date=saved_plan.end_date,
                       as_of=saved_plan.as_of, explicit_historical=saved_plan.explicit_historical,
                       applied_defaults=("scope:previous_research", *(
                           item for item in plan.applied_defaults if item not in date_defaults)))
    if (plan.objective.horizon is None and saved_plan.objective.horizon is not None
            and any(item.startswith('horizon_source:') for item in saved_plan.applied_defaults)):
        updates["objective"] = (saved_plan.objective if plan.objective.kind == "analysis"
                                else replace(plan.objective, horizon=saved_plan.objective.horizon))
        if any(item.startswith('horizon_source:') for item in saved_plan.applied_defaults):
            updates['applied_defaults'] = (*updates.get('applied_defaults', plan.applied_defaults), 'horizon_source:inherited')
    selected_profile = any(item.startswith("indicators:profile:") for item in plan.applied_defaults)
    if not plan.indicators_explicit and (saved_plan.indicators_explicit or not selected_profile):
        updates.update(indicators=saved_plan.indicators, indicators_explicit=saved_plan.indicators_explicit)
    indicators = updates.get('indicators', plan.indicators)
    positive, excluded = _positive_scope(question)
    changes_domain = has_company_domain(question) or bool(re.search(r'技术面|技术指标|技术分析', positive)) or _ONLY.search(positive)
    domains = plan.scope.domains if changes_domain else saved_plan.scope.domains or plan.scope.domains
    exclusions = set(saved_plan.scope.excluded) | set(plan.scope.excluded)
    if changes_domain:
        exclusions.difference_update(plan.kinds)
    if plan.indicators_explicit:
        exclusions.difference_update('indicator:' + spec.kind for spec in indicators)
    scope = ProductScope(domains, tuple(spec.indicator_id for spec in indicators)
        if saved_plan.scope.indicator_ids or plan.scope.indicator_ids else (), tuple(sorted(exclusions)))
    if ((plan.wants_risk or wants_prices(plan.user_question)) and not excludes_plan(plan.user_question)
            and scope.permits_atr_dependency and not any(spec.kind == 'atr' for spec in indicators)):
        indicators += (IndicatorSpec('atr', window=14),)
        defaults = updates.get('applied_defaults', plan.applied_defaults)
        updates['applied_defaults'] = tuple(dict.fromkeys((*defaults, 'indicator_dependency:atr14_for_risk')))
    kinds = tuple(kind for kind in plan.kinds if (not domains or kind in domains) and kind not in exclusions)
    updates.update(scope=scope, indicators=indicators, kinds=kinds)
    if plan.context is not None:
        context_updates = {key: value for key, value in updates.items() if key != "as_of"}
        updates["context"] = replace(plan.context, **context_updates)
    return replace(plan, **updates)


def saved_product(session, *, directory, now, source, development, reference_date=None):
    saved = session.product_saved
    if saved is None:
        return None
    if (not saved.saved_at <= now < saved.saved_at + timedelta(minutes=15)
            or saved.plan.source != ("csv" if development else source)):
        session.product_saved = None
        return None
    try:
        reference = reference_date or now.astimezone(timezone(timedelta(hours=8))).date()
        directory.assert_usable(reference, development=development)
        for security in saved.plan.securities:
            match = directory.lookup(security.instrument_id)
            if match.status != "resolved" or match.candidates[0] != security:
                session.product_saved = None
                return None
    except Exception:
        session.product_saved = None
        return None
    return saved


def _execute(question, *, session, model, directory, now, reference_date, source,
             development_source, data_service, source_factory, market_background_source, background_clock,
             selected_securities=(), original_question=None):
    from trading_companion.product_answer import synthesize_product_answer
    checkpoint()
    reference = reference_date or now.astimezone(timezone(timedelta(hours=8))).date()
    command = question.strip().rstrip("。.!！")
    if command in ("新对话", "重新开始", "清空会话", "取消", "取消研究", "取消分析", "算了"):
        reset = command in ("新对话", "重新开始", "清空会话")
        session.clear() if reset else session.clear_pending()
        return AgentTurnResult(status="cancelled", code="session_reset" if reset else "research_cancelled",
            stage="clarification", input_mode="natural", message="会话已清空。" if reset else "已取消待澄清的研究。")
    market = _market_reply(question, directory)
    if market is not None:
        return market
    saved = saved_product(session, directory=directory, now=now, source=source,
                          development=development_source is not None, reference_date=reference)
    from trading_companion.research_instruments import research_spans
    securities, subject_error = (), None
    if (not research_spans(question, directory) and not new_question(question)
            and session.product_pending is None and continuation_question(question)):
        try:
            securities, subject_error = checked_subject(session, directory=directory, reference_date=reference,
                development=development_source is not None, now=now)
        except DataValidationError:
            subject_error = "research_subject_directory_unusable"
        if len(securities) > 1 and not plural_subject(question):
            securities, subject_error = (), "research_subject_ambiguous"
    if (wants_decision(question) and not research_spans(question, directory)
            and not _execution_requested(question) and not _INJECTION.search(question)):
        if new_question(question) or session.product_pending is None and not securities:
            return AgentTurnResult(status="needs_clarification", code="decision_instrument_required",
                stage="clarification", input_mode="natural", message="请明确要研判的股票名称或代码。",
                user_question=question)
    if subject_error and continuation_question(question) and not research_spans(question, directory):
        return AgentTurnResult(status="needs_clarification", code=subject_error,
            stage="clarification", input_mode="natural", message="请明确要继续研究的股票；原资料未被当作最新使用。",
            user_question=question)
    history = command in ("查看上次结果", "查看上一次结果", "上次研究结果")
    if history:
        if saved is None:
            return AgentTurnResult(status="needs_clarification", code="product_result_unavailable",
                message="当前没有仍在有效期内的公司研究结果，请提供股票名称重新研究。", stage="clarification", input_mode="natural")
        return AgentTurnResult(status="completed", code="product_saved_result", message="基于已保存的公司研究结果。",
            stage="company_research", input_mode="natural", product_answer=saved.answer,
            product_bundle=saved.bundle, product_plan=saved.plan, session_event="history")
    if (saved is not None and securities == saved.plan.securities and session.product_pending is None
            and not directory.mentioned_spans(question)
            and re.search(r"为什么|为何|依据|解释.*(?:结论|判断)|详细.*(?:结论|判断)", question)
            and not wants_decision(question)
            and not re.search(r"最新|刷新|重新|新闻|公告|财报|财务|基本面|技术面|止损|止盈", question)):
        run = synthesize_product_answer("基于刚才保存的研究资料，未重新取数。" + question, saved.bundle,
                                        model=model, repair_invalid=True)
        checkpoint()
        return AgentTurnResult(status="completed" if run.answer.status == "accepted" else "limited",
            code="product_followup_answered", stage="company_research", input_mode="natural",
            message="基于已保存的公司研究结果，未重新取数。", product_answer=run.answer,
            product_bundle=saved.bundle, product_plan=saved.plan,
            model_calls_attempted=run.model_calls_attempted,
            model_responses=run.responses, failure_code=run.failure_code,
            session_event="company_followup", user_question=question)
    effective_question = question
    continuation = None
    # A short new-domain follow-up may reuse identities, never silently reuse current market data.
    if securities and session.product_pending is None and not research_spans(question, directory):
        names = "、".join(security.name for security in securities)
        effective_question = f"分析{names}：{question}"
        subject = latest_subject(session)
        if (saved is not None and subject is not None and type(subject.scope) is ProductPlan
                and saved.plan.securities == securities):
            continuation = saved.plan
        elif (subject is not None and type(subject.scope) is ProductPlan
                and subject.source == ("csv" if development_source is not None else source)
                and subject.development == (development_source is not None)):
            continuation = subject.scope
            if not subject.published_at <= now < subject.published_at + timedelta(minutes=15):
                continuation = replace(continuation, objective=replace(continuation.objective, horizon=None),
                    applied_defaults=tuple(v for v in continuation.applied_defaults if not v.startswith('horizon_source:')))
        if re.search(r"还会涨|还会跌|会上涨|会下跌", question) and not re.search(r"未来|后续|接下来", question):
            effective_question += "，讨论后续走势的条件情景"
    if session.product_pending is None and is_product_indicator_edit(question):
        from trading_companion.product_indicator_edit import edit_product_plan
        if continuation is None:
            return AgentTurnResult(status='needs_clarification', code='no_current_research_subject',
                stage='clarification', input_mode='natural', message='请明确要修改的当前研究对象。', user_question=question)
        plan = edit_product_plan(continuation, question, now=now, reference_date=reference,
                                 development=development_source is not None)
    else:
        plan = prepare_product_plan(effective_question, model=model, directory=directory, reference_date=reference,
            now=now, source="csv" if development_source is not None else source,
            development=development_source is not None, context=session.product_pending,
            allow_context_research=bool(directory.mentioned_spans(effective_question)))
        plan = _inherit_scope(plan, continuation, question)
    from trading_companion.decision_horizon import gate
    if selected_securities and plan.status == 'ready' and plan.securities == selected_securities:
        plan = replace(plan, user_question=original_question)
    if (plan.status == 'ready' and plan.objective.horizon is None and saved is not None
            and plan.securities == saved.plan.securities
            and any(v.startswith('horizon_source:') for v in saved.plan.applied_defaults)):
        plan = replace(plan, objective=replace(plan.objective, kind='outlook' if plan.objective.kind == 'analysis' else plan.objective.kind,
            horizon=saved.plan.objective.horizon), applied_defaults=(*plan.applied_defaults, 'horizon_source:inherited'))
    plan = gate(plan, now=now, reference_date=reference, development=development_source is not None)
    responses = (plan.model_response,) if plan.model_response is not None else ()
    if selected_securities:
        if plan.status == "ready" and plan.securities != selected_securities:
            return AgentTurnResult(status="needs_clarification", code="screening_selection_plan_mismatch",
                stage="clarification", input_mode="natural", message="研究计划与选择的名单对象不一致，未查询研究数据。",
                model_calls_attempted=plan.model_calls_attempted, model_responses=responses,
                user_question=original_question)
        if plan.status == "ready":
            plan = replace(plan, user_question=original_question)
    if plan.status != "ready":
        session.product_pending = (plan.context if not selected_securities
            or plan.context is not None and plan.context.stage == 'horizon' else None)
        return AgentTurnResult(status="needs_clarification" if plan.status in ("needs_clarification", "not_applicable")
            else "failed", code=plan.code, message=plan.message, stage="clarification", input_mode="natural",
            model_calls_attempted=plan.model_calls_attempted, model_responses=responses, product_plan=plan,
            session_event="company_clarification", user_question=question)
    # New research consumes the old base before any external reads, including failed or cancelled work.
    session.begin_research(preserve_screening=bool(selected_securities) or continuation is not None)
    try:
        bundle = collect_product_research(plan, data_service=data_service, now=now,
            development_source=development_source, source_factory=source_factory,
            market_background_source=market_background_source, directory=directory, background_clock=background_clock)
    except (DataSourceError, DataValidationError, InvalidRequestError, ValueError, OSError):
        return AgentTurnResult(status="failed", code="company_evidence_invalid", stage="company_research",
            input_mode="natural", message="本轮资料未通过来源或时间校验，请稍后重试。", product_plan=plan,
            model_responses=responses, model_calls_attempted=plan.model_calls_attempted,
            failure_code="company_evidence_invalid", user_question=question)
    checkpoint()
    run = synthesize_product_answer(plan.user_question or question, bundle, model=model, repair_invalid=True)
    checkpoint()
    responses += run.responses
    answer = run.answer
    has_data = bool(bundle.evidence)
    if has_data:
        session.product_saved = SavedProductResearch(now, plan, bundle, answer)
        session.stage_research_subject(plan.securities, kind="technical" if plan.kinds == ('technical',) else "company", source=plan.source,
                                      development=development_source is not None, now=now, scope=plan)
    status = "completed" if has_data and answer.status == "accepted" and not bundle.partial else "limited" if has_data else "failed"
    return AgentTurnResult(status=status, code="company_research_completed" if status == "completed" else
        "company_research_partial" if has_data else "company_research_unavailable",
        message=("技术分析已完成。" if plan.kinds == ('technical',) else "公司研究已完成。") if has_data else "本轮公开数据暂不可用。", stage="company_research",
        input_mode="natural", product_answer=answer, product_bundle=bundle, product_plan=plan,
        model_responses=responses, model_calls_attempted=plan.model_calls_attempted + run.model_calls_attempted,
        failure_code=run.failure_code, user_question=question, session_event="company_research")


def run_product_research_turn(question, *, session, model, directory, now, source, data_service,
                              reference_date=None, development_source=None, control=None, source_factory=None,
                              market_background_source=None, background_clock=None):
    from trading_companion.index_agent import owns_index_request, run_index_research_turn
    if owns_index_request(question, session, directory):
        return run_index_research_turn(question, model=model, directory=directory, session=session, now=now,
            source=source, development_source=development_source, reference_date=reference_date, control=control)
    if data_service is None:
        raise ModelConfigurationError("Company research requires an explicit trusted data service")
    return _controlled(lambda controlled, working: _execute(question, session=working, model=controlled,
        directory=directory, now=now, reference_date=reference_date, source=source,
        development_source=development_source, data_service=data_service, source_factory=source_factory,
        market_background_source=market_background_source, background_clock=background_clock),
        model=model, session=session, control=control or company_turn_control(), default_calls=6, input_mode="natural",
        replace_pending=replaces_pending(question, directory))
