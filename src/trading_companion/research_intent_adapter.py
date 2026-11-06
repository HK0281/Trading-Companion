"""Translate checked candidate fields into existing requests, without a second interpreter."""

from copy import copy
from dataclasses import dataclass, replace
from datetime import date
import re

from trading_companion.agent_input import InputIssue
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.model import ModelConfigurationError
from trading_companion.multi_contracts import IndicatorSpec, validate_research_conditions
from trading_companion.product_plan import ProductPlan, ProductPlanContext, RESEARCH_KINDS, product_defaults
from trading_companion.product_scope import ProductScope, _indicator
from trading_companion.research_intent import PreparedResearchInput, ResearchConditions
from trading_companion.research_intent_v2 import CheckedIntent, _candidate_dates, _pending
from trading_companion.research_objective import ResearchObjective
from trading_companion.technical_selection import select_complementary_indicators


def _issue(code, message='研究条件不适用于当前状态，请重新明确；未查询数据。'):
    raise InputIssue('needs_clarification', code, message)


def _dates(checked, question, context):
    # Reuse the candidate's date parser only for default provenance, never override its checked window.
    from trading_companion.research_objective import mask_outlook_expressions
    from trading_companion.research_intent_v2 import _VAGUE_PERIOD, _VAGUE_DATES
    text = mask_outlook_expressions(question)
    text = _VAGUE_PERIOD.sub(lambda m: ' ' * len(m[0]) if m[0] in _VAGUE_DATES else m[0], text)
    start, end, defaults = _candidate_dates(checked.date_expression, text, context.reference_date)
    if checked.historical_window:
        start, end = map(date.fromisoformat, checked.historical_window)
    if not start <= end < context.reference_date or (end - start).days >= 366:
        _issue('intent_historical_window_invalid', '历史区间无效，请明确上海当日之前、最多366个自然日的日期范围。')
    explicit = bool(checked.date_expression and re.search(r'\d{4}-\d{2}-\d{2}', checked.date_expression))
    return start, end, defaults, explicit


def _native_pending(checked, context):
    if not checked.diagnostics.get('authorization', {}).get('same_task_pending'):
        return None
    binding = _pending(context)
    if (checked.operation != 'update' or not binding or not binding['valid']
            or binding['kind'] != checked.task or tuple(binding['subjects']) != checked.bound_subjects):
        _issue('intent_pending_expired', '原待确认的研究已过期或配置变化，请重新提出完整问题。')
    return context.session.product_pending if checked.task == 'stock_research' else context.session.index_pending


def _saved_scope(checked, context):
    """Validate caches on an isolated copy; identity alone is insufficient for scope inheritance."""
    if checked.operation not in ('update', 'explain'):
        return None
    session = copy(context.session)
    session._turn_parent = None
    if checked.task == 'index_research':
        from trading_companion.index_agent import _saved
        original = session.index_saved
        saved = _saved(session, source=context.source,
            development_source=original.development_source if original is not None else None,
            now=context.now, history=False)
        if saved is not None and (saved.result.request.instrument_id,) == checked.bound_subjects:
            return saved.result.request
    elif context.session._subject_current:
        from trading_companion.product_agent import saved_product
        saved = saved_product(session, directory=context.directory, now=context.now, source=context.source,
            development=context.development, reference_date=context.reference_date)
        if (saved is not None and tuple(s.instrument_id for s in saved.plan.securities) == checked.bound_subjects
                and context.session.latest_research is not None
                and context.session.latest_research.scope == saved.plan):
            return saved.plan
        saved = session._last_result
        if saved is not None and (saved.security.instrument_id,) == checked.bound_subjects:
            lookup = session.saved_result(directory=context.directory, source=context.source,
                development_source=saved.development_source, reference_date=context.reference_date,
                now=context.now)
            if lookup.status == 'ready':
                return context.session.confirmed.conditions if context.session.confirmed else None
    return None


def _delta(question, previous, checked, context):
    """Merge checked scope fields; only parameter replacements use the existing editor."""
    from trading_companion.research_edit import replace_indicator
    from trading_companion.multi_contracts import MultiIndicatorRequest
    excluded = set(checked.exclusions)
    mentions = tuple(s for phrase in checked.indicator_mentions for s in _indicator(phrase)
                     if 'indicator:' + s.kind not in excluded)
    indicators = tuple(dict.fromkeys((*previous.indicators, *mentions)))
    try:
        request = MultiIndicatorRequest(checked.bound_subjects[0], previous.start_date,
            getattr(previous, 'end_date', None) or previous.as_of_date, previous.indicators, source=context.source)
        indicators = replace_indicator(request, question).indicators
    except (InputIssue, InvalidRequestError):
        pass
    added = set(checked.research_kinds)
    if checked.indicator_mentions:
        added.add('technical')
    return (tuple(s for s in indicators if 'indicator:' + s.kind not in excluded),
            added, excluded.intersection(RESEARCH_KINDS),
            {v for v in excluded if v.startswith('indicator:')})


def merge_non_authorizing_scope(plan, previous, checked, question, context):
    """Only dates, indicators, domains and exclusions cross an ordinary update."""
    if previous is None or checked.operation != 'update':
        return plan
    old_end = getattr(previous, 'end_date', None) or previous.as_of_date
    old_defaults = getattr(previous, 'applied_defaults', ())
    if not checked.date_expression:
        plan = replace(plan, start_date=previous.start_date, end_date=old_end, as_of=old_end,
            explicit_historical=getattr(previous, 'explicit_historical', getattr(previous, 'historical_explicit', False)),
            applied_defaults=tuple(v for v in plan.applied_defaults if not v.startswith(('cutoff:', 'range:', 'relative_unit:')))
                + tuple(v for v in old_defaults if v.startswith(('cutoff:', 'range:', 'relative_unit:'))))
    if checked.strict_scope:
        return plan
    if not checked.indicator_mentions and not checked.research_kinds and not checked.exclusions:
        indicators = previous.indicators
        kinds = getattr(previous, 'kinds', ('technical',))
        excluded = getattr(previous, 'scope', ProductScope()).excluded
    else:
        indicators, added, removed, removed_indicators = _delta(question, previous, checked, context)
        kinds = tuple(k for k in RESEARCH_KINDS if k in (set(getattr(previous, 'kinds', ('technical',))) | added) - removed)
        excluded = tuple(sorted((set(getattr(previous, 'scope', ProductScope()).excluded) - added
            - {'indicator:' + s.kind for s in indicators}) | set(checked.exclusions) | removed | removed_indicators))
    kinds = tuple(k for k in kinds if k not in excluded)
    indicators = tuple(s for s in indicators if 'indicator:' + s.kind not in excluded)
    if not kinds and not plan.descriptive_risk:
        _issue('intent_empty_scope', '请至少保留一个研究领域。')
    scope = ProductScope(excluded=excluded)
    scope.validate(kinds, indicators)
    return replace(plan, kinds=kinds, indicators=indicators, scope=scope,
        indicators_explicit=bool(checked.indicator_mentions) or getattr(previous, 'indicators_explicit', False),
        applied_defaults=(*plan.applied_defaults, 'scope:previous_research'))


def build_product_plan(checked, question, context):
    if type(checked) is not CheckedIntent or checked.task != 'stock_research':
        raise ModelConfigurationError('Use a checked stock intent')
    context.directory.assert_usable(context.reference_date, development=context.development)
    securities = []
    for identifier in checked.bound_subjects:
        match = context.directory.lookup(identifier)
        if match.status != 'resolved' or match.candidates[0].instrument_id != identifier:
            _issue('intent_identity_changed', '研究对象在当前目录中已变化，请重新明确名称或代码。')
        securities.append(match.candidates[0])
    if not 1 <= len(securities) <= 3:
        _issue('intent_subject_required', '请明确一至三只股票。')
    start, end, defaults, explicit = _dates(checked, question, context)
    pending = _native_pending(checked, context)
    exclusions = set(checked.exclusions) | set(checked.diagnostics.get('authorization', {}).get('permission_vetoes', ()))
    kinds = tuple(k for k in RESEARCH_KINDS if k in checked.research_kinds and k not in exclusions)
    risk = 'risk' in checked.research_kinds and not checked.wants_plan and 'risk' not in exclusions
    if not kinds and not checked.strict_scope and not risk:
        kinds = tuple(k for k in RESEARCH_KINDS if k not in exclusions)
    selection = select_complementary_indicators(question)
    indicators = (tuple(s for phrase in checked.indicator_mentions for s in _indicator(phrase))
                  if checked.indicator_mentions else selection.indicators)
    indicators = tuple(dict.fromkeys(s for s in indicators if 'indicator:' + s.kind not in exclusions))
    objective = ResearchObjective(checked.objective_kind, checked.horizon)
    prices = checked.wants_plan
    indicators, defaults = product_defaults(indicators, objective=objective, defaults=defaults,
        indicators_explicit=bool(checked.indicator_mentions), indicator_profile=selection.profile,
        prices=prices and checked.plan_blocked is None and not exclusions.intersection(('technical', 'risk')),
        strict=checked.strict_scope, exclusions=exclusions)
    dependency_only = risk and 'technical' not in kinds
    if dependency_only:
        indicators = tuple(s for s in indicators if s.kind in ('atr', 'boll')) if 'technical' not in exclusions else ()
    plan = ProductPlan(status='ready', code='product_plan_ready', message='研究身份、范围和许可已校验。',
        securities=tuple(securities), kinds=kinds, as_of=end, start_date=start, end_date=end,
        explicit_historical=explicit, wants_risk=prices and objective.kind != 'decision', objective=objective,
        source=context.source, applied_defaults=defaults, user_question=question, indicators=indicators,
        indicators_explicit=bool(checked.indicator_mentions) or any(v.startswith('indicator:') for v in exclusions),
        checked_prices=prices, descriptive_risk=risk, risk_dependencies_only=dependency_only,
        scope=ProductScope(kinds if checked.strict_scope else (),
            tuple(s.indicator_id for s in indicators) if checked.strict_scope and checked.indicator_mentions else (),
            tuple(sorted(exclusions))))
    if pending is not None:
        if not isinstance(pending, ProductPlanContext):
            _issue('intent_pending_mismatch')
        if not checked.date_expression:
            plan = replace(plan, start_date=pending.start_date, end_date=pending.end_date, as_of=pending.end_date,
                explicit_historical=pending.explicit_historical, applied_defaults=pending.applied_defaults)
        if not checked.indicator_mentions and not checked.strict_scope:
            plan = replace(plan, indicators=pending.indicators, indicators_explicit=pending.indicators_explicit)
        if not checked.research_kinds and not checked.strict_scope:
            plan = replace(plan, kinds=pending.kinds, scope=pending.scope)
        plan = replace(plan, context_created_at=pending.created_at, context_expires_at=pending.expires_at,
            user_question=pending.original_question + '；' + question,
            applied_defaults=tuple(v for v in plan.applied_defaults if not v.startswith(
                ('horizon:', 'horizon_source:', 'horizon_profile:'))) + ('horizon_source:clarification',))
    else:
        plan = merge_non_authorizing_scope(plan, _saved_scope(checked, context), checked, question, context)
    if checked.diagnostics.get('reference', {}).get('scope') == 'shortlist':
        saved = context.session.screening_saved
        if saved is None or saved.invalid_reason(directory=context.directory, quote_source=context.quote_source,
                source=context.source, development=context.development, now=context.now):
            _issue('intent_screening_expired', '原筛选名单已失效，请重新筛选或明确股票。')
        if any(s not in saved.securities for s in plan.securities):
            _issue('intent_screening_mismatch')
        plan = replace(plan, applied_defaults=(*plan.applied_defaults, 'scope:screening_list'))
    if plan.indicators:
        validate_research_conditions(plan.start_date, plan.end_date, plan.indicators)
    plan.scope.validate(plan.kinds, plan.indicators)
    return plan


def build_technical_input(checked, question, context):
    # Independent technical tools retain their own default indicator contract.
    execution_intent = replace(checked, research_kinds=('technical',)) if not checked.research_kinds else checked
    plan = build_product_plan(execution_intent, question, context)
    if len(plan.securities) != 1 or plan.kinds != ('technical',) or checked.wants_plan or plan.descriptive_risk:
        _issue('intent_company_capability_required', '当前实例只支持单只股票的技术工具研究，请明确技术指标范围。')
    from trading_companion.research_intent import _indicators
    indicators, defaults = _indicators(list(checked.indicator_mentions), ''.join(checked.indicator_mentions))
    if checked.operation == 'update' or any(v.startswith('indicator:') for v in checked.exclusions):
        indicators, defaults = plan.indicators, ()
    date_defaults = tuple(v for v in plan.applied_defaults if not v.startswith(('plan_method:', 'indicators:profile:')))
    conditions = ResearchConditions(plan.start_date, plan.end_date, indicators, context.source,
        context.reference_date, (*date_defaults, *defaults), plan.objective)
    security = plan.securities[0]
    return PreparedResearchInput(status='ready', code='research_input_ready', message='研究条件已校验。',
        stage='parameters', intent='research', request=conditions.for_security(security.instrument_id),
        security=security, directory=context.directory, applied_defaults=conditions.applied_defaults,
        conditions=conditions, user_question=question)


@dataclass(frozen=True)
class PreparedIndexInput:
    request: object
    exclusions: tuple = ()


def build_index_input(checked, question, context):
    return build_index_plan(checked, question, context).request


def build_index_plan(checked, question, context):
    from trading_companion.index_agent import build_index_request
    from trading_companion.index_contracts import DEFAULTS
    if type(checked) is not CheckedIntent or checked.task != 'index_research' or len(checked.bound_subjects) != 1:
        raise ModelConfigurationError('Use a checked single index intent')
    context.directory.assert_usable(context.reference_date, development=context.development)
    start, end, defaults, explicit = _dates(checked, question, context)
    pending = _native_pending(checked, context)
    specs = tuple(s for phrase in checked.indicator_mentions for s in _indicator(phrase)) or DEFAULTS
    specs = tuple(s for s in specs if 'indicator:' + s.kind not in checked.exclusions)
    if 'technical' in checked.exclusions:
        _issue('index_technical_excluded', '本批只支持指数技术研究，已按要求排除技术面，未查询行情。')
    previous = _saved_scope(checked, context)
    excluded = set(checked.exclusions)
    if previous is not None:
        old_scope = ProductPlan(start_date=previous.start_date, end_date=previous.as_of_date,
            as_of=previous.as_of_date, kinds=('technical',), indicators=previous.indicators,
            scope=ProductScope(excluded=context.session.index_saved.scope_exclusions),
            explicit_historical=previous.historical_explicit)
        base = ProductPlan(start_date=start, end_date=end, as_of=end, kinds=('technical',), indicators=specs,
            applied_defaults=defaults, scope=ProductScope(excluded=checked.exclusions))
        merged = merge_non_authorizing_scope(base, old_scope, checked, question, context)
        start, end, specs, explicit = merged.start_date, merged.end_date, merged.indicators, merged.explicit_historical
        excluded.update(merged.scope.excluded)
    if pending is not None:
        excluded.update(pending.scope_exclusions)
        old = pending.base
        old = getattr(getattr(old, 'result', None), 'request', old)
        if old is not None:
            if not checked.date_expression:
                start, end, explicit = old.start_date, old.as_of_date, old.historical_explicit
            if not checked.indicator_mentions and not checked.strict_scope:
                specs = old.indicators
    prices = checked.wants_plan
    blocked = 'risk_atr_not_requested' if checked.plan_blocked or 'indicator:atr' in excluded else (
        'plan_excluded' if 'risk' in excluded else None)
    if prices and not blocked and not any(s.kind == 'atr' and s.parameters['window'] == 14 for s in specs):
        if checked.strict_scope or 'indicator:atr' in checked.exclusions:
            blocked = 'risk_atr_not_requested'
        else:
            specs += (IndicatorSpec('atr'),)
    from trading_companion.market_scope import market_selection
    selected, defaulted, _ = market_selection(question, directory=context.directory)
    benchmark_defaulted = bool(selected is not None and selected.instrument_id == checked.bound_subjects[0] and defaulted)
    if pending is not None:
        benchmark_defaulted = pending.benchmark_defaulted
    elif previous is not None:
        benchmark_defaulted = previous.benchmark_defaulted
    request = build_index_request(checked.bound_subjects[0], start, end, specs, context.source,
        horizon=checked.horizon, scenario_requested=prices, historical_explicit=explicit,
        indicators_strict=checked.strict_scope, scenario_code=blocked, benchmark_defaulted=benchmark_defaulted)
    return PreparedIndexInput(request, tuple(sorted(excluded)))


def _local(checked, question):
    from trading_companion.agent import AgentTurnResult
    if ('preflight', 'existing_execution_guard') in checked.provenance:
        return AgentTurnResult(status='out_of_scope', code='intent_execution_not_supported',
            message='不能执行下单、委托或账户操作；可以继续研究。', stage='clarification',
            input_mode='natural', user_question=question)
    messages = {'reject': '当前只支持只读研究或知识说明，请明确受支持的研究任务。',
                'clarify_subject': '请明确本次研究的股票或指数名称。',
                'clarify_identity': '研究对象尚未唯一确认，请提供完整名称或代码。',
                'clarify_conditions': '请明确研究日期、指标或修改条件；未查询数据。',
                'clarify_conflict': '当前条件或待确认请求有冲突，请明确本次范围，或回复“取消”。',
                'clarify_horizon': '请明确短线、中线或长线期限；未查询数据。'}
    return AgentTurnResult(status='out_of_scope' if checked.outcome == 'reject' else 'needs_clarification',
        code='intent_v2_' + checked.outcome, message=messages.get(checked.outcome, messages['clarify_conditions']),
        stage='clarification', input_mode='natural', user_question=question)


def _explain(checked, question, context, *, model, session, development_source):
    from trading_companion.agent import AgentTurnResult
    saved_scope = _saved_scope(checked, context)
    if saved_scope is None:
        _issue('intent_saved_scope_unavailable', '当前没有同一对象、来源和有效期内的保存证据，请重新研究。')
    if checked.task == 'index_research':
        from trading_companion.index_agent import _outcome
        from trading_companion.index_answer import synthesize_index_answer
        saved = session.index_saved
        answer, responses = synthesize_index_answer(question, saved.result, model=model, directory=context.directory)
        return _outcome(saved.result, answer, responses=responses, saved_at=saved.saved_at)
    if isinstance(saved_scope, ProductPlan):
        from trading_companion.product_answer import synthesize_product_answer
        saved = session.product_saved
        run = synthesize_product_answer('基于刚才保存的研究资料，未重新取数。' + question,
                                        saved.bundle, model=model, repair_invalid=True)
        return AgentTurnResult(status='completed' if run.answer.status == 'accepted' else 'limited',
            code='product_followup_answered', message='基于已保存的公司研究结果，未重新取数。',
            stage='company_research', input_mode='natural', product_plan=saved.plan,
            product_bundle=saved.bundle, product_answer=run.answer, model_responses=run.responses,
            failure_code=run.failure_code, session_event='company_followup', user_question=question)
    from trading_companion.research_followup import render_saved_answer
    saved = session._last_result
    answer = render_saved_answer(saved, action='explain', mentions=checked.indicator_mentions)
    return AgentTurnResult(status='completed' if answer.status == 'accepted' else answer.status,
        code=answer.code, message=answer.text, followup_answer=answer, stage='conversation',
        input_mode='natural', user_question=question)


def execute_checked_intent(checked, question, *, context, model, session, control,
                           development_source=None, data_service=None, market_background_source=None,
                           clock=None, digests=()):
    """One shared controlled executor; errors here never re-enter legacy interpretation."""
    from trading_companion.agent import _controlled, AgentTurnResult
    from trading_companion.turn_control import checkpoint
    from trading_companion.decision_horizon import valid_delivery, remember_delivery
    if type(checked) is not CheckedIntent or not callable(clock):
        raise ModelConfigurationError('Use checked intent and an application clock')

    def action(checked_model, working):
        try:
            if working._delivered_prices is not None and valid_delivery(working, directory=context.directory,
                    now=context.now, reference_date=context.reference_date, source=context.source,
                    development=context.development) is None:
                working._delivered_prices = None
            if checked.outcome not in ('research', 'local', 'clarify_horizon'):
                return _local(checked, question)
            if checked.task == 'knowledge':
                from trading_companion.indicator_knowledge import parse_knowledge_question
                from trading_companion.agent import _run_general_knowledge
                definition = parse_knowledge_question(question)
                if definition is not None:
                    return AgentTurnResult(status='completed', code='indicator_knowledge_answered', stage='knowledge',
                        input_mode='natural', message='仅解释项目维护的指标定义；未查询行情。', knowledge_answer=definition)
                if not context.workflow_enabled:
                    _issue('general_knowledge_disabled', '当前未开启一般知识解释功能。')
                return _run_general_knowledge(question, model=checked_model, directory=context.directory)
            if checked.task == 'recall':
                from trading_companion.conversation_memory import recall_answer
                return AgentTurnResult(status='completed', code='conversation_recall_answered',
                    stage='conversation', input_mode='natural', message=recall_answer(digests), session_event='routing_reply')
            if checked.task == 'screening':
                from trading_companion.screening_agent import run_screening_turn, run_screening_edit_turn
                common = dict(model=checked_model, session=working, directory=context.directory,
                    source=context.quote_source, now=context.now, control=control, clock=clock,
                    research_source=context.source)
                if checked.operation == 'update':
                    return run_screening_edit_turn(question, development=context.development, **common)
                return run_screening_turn(question, **common)
            if checked.operation == 'explain':
                return _explain(checked, question, context, model=checked_model, session=working,
                    development_source=development_source)
            if checked.task == 'stock_research':
                if data_service is not None:
                    from trading_companion.product_agent import run_product_research_turn
                    plan = build_product_plan(checked, question, context)
                    result = run_product_research_turn(question, prepared=plan, model=checked_model,
                        session=working, directory=context.directory, now=context.now, source=context.source,
                        reference_date=context.reference_date, development_source=development_source,
                        control=control, data_service=data_service, market_background_source=market_background_source,
                        background_clock=clock, replace_existing=checked.operation in ('new', 'refresh'))
                else:
                    from trading_companion.agent import run_natural_research_turn
                    prepared = build_technical_input(checked, question, context)
                    result = run_natural_research_turn(question, prepared=prepared, model=checked_model,
                        session=working, directory=context.directory, current_time=context.now, source=context.source,
                        reference_date=context.reference_date, development_source=development_source,
                        control=control, workflow=context.workflow_enabled,
                        replace_existing=checked.operation in ('new', 'refresh'))
            elif checked.task == 'index_research':
                from trading_companion.index_agent import run_index_research_turn, PendingIndexResearch, _horizon_reply
                prepared = build_index_plan(checked, question, context)
                request = prepared.request
                if request.scenario_requested and request.scenario_code is None and request.horizon is None:
                    original = _native_pending(checked, context)
                    created = original.created_at if original is not None else context.now
                    working.begin_research()
                    working.index_pending = PendingIndexResearch(question, created, context.source,
                        development_source, stage='horizon', instrument_id=request.instrument_id,
                        benchmark_defaulted=request.benchmark_defaulted, reference_date=context.reference_date,
                        base=request, scope_exclusions=prepared.exclusions)
                    return _horizon_reply(working.index_pending)
                result = run_index_research_turn(question, prepared=request, model=checked_model,
                    session=working, directory=context.directory, now=context.now, source=context.source,
                    reference_date=context.reference_date, development_source=development_source, control=control,
                    replace_existing=checked.operation in ('new', 'refresh'), scope_exclusions=prepared.exclusions)
            else:
                return _local(checked, question)
            checkpoint()
            if result.session_event in ('company_research', 'index_research'):
                remember_delivery(working, result, now=clock(), reference_date=context.reference_date,
                    development=context.development)
            if result.product_plan is not None and result.product_plan.descriptive_risk:
                messages = [s['message'] for c in result.product_bundle.companies for s in c.coverage
                            if s['kind'] == 'risk'] if result.product_bundle is not None else []
                if messages:
                    result = replace(result, program_notice='\n'.join(dict.fromkeys(messages)))
            return result
        except InputIssue as issue:
            return AgentTurnResult(status=issue.status, code=issue.code, message=str(issue),
                stage='clarification', input_mode='natural', user_question=question)
        except (DataValidationError, InvalidRequestError, ValueError):
            return AgentTurnResult(status='needs_clarification', code='intent_execution_conditions_invalid',
                message='身份、来源、日期或研究范围不符合当前条件，请重新明确；没有自动换源。',
                stage='clarification', input_mode='natural', user_question=question)
    return _controlled(action, model=model, session=session, control=control, default_calls=6, input_mode='natural')
