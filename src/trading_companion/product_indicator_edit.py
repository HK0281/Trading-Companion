"""Apply whole-message indicator edits to trusted shared research plans."""
from dataclasses import dataclass, replace
from datetime import timedelta
import re
import unicodedata

from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest, validate_research_conditions
from trading_companion.research_edit import (
    _EDIT, indicator_candidates, indicator_selection, indicator_selection_label,
    parse_indicator_instance, replace_indicator, window_edit_candidates,
)
from trading_companion.research_intent import ResearchConditions
from trading_companion.research_update import parse_condition_update, update_conditions


@dataclass(frozen=True)
class ProductIndicatorEdit:
    plan: object
    replacement: IndicatorSpec | int
    candidates: tuple[IndicatorSpec, ...]
    question: str


def _issue(code, message):
    raise InputIssue('needs_clarification', code, message)


def _request(plan):
    if len(plan.securities) != 1:
        _issue('research_subject_ambiguous', '请明确要修改哪只股票；没有同时修改多只股票。')
    return MultiIndicatorRequest(plan.securities[0].instrument_id, plan.start_date, plan.end_date,
                                 indicators=plan.indicators, source=plan.source)


def _finish(plan, indicators, question):
    indicators = validate_research_conditions(plan.start_date, plan.end_date, indicators)
    removed = ({spec.kind for spec in plan.indicators} - {spec.kind for spec in indicators}
               if 'technical' in plan.kinds else set())
    scope = plan.scope.edited(indicators, removed=removed)
    kinds = tuple(dict.fromkeys((*plan.kinds, 'technical')))
    if scope.domains and 'technical' not in scope.domains:
        scope = replace(scope, domains=tuple(dict.fromkeys((*scope.domains, 'technical'))))
    scope = replace(scope, excluded=tuple(item for item in scope.excluded if item != 'technical'))
    scope.validate(kinds, indicators)
    return replace(plan, status='ready', code='product_plan_ready', message='指标条件已更新，重新研究。',
                   indicators=indicators, indicators_explicit=True, kinds=kinds, scope=scope,
                   model_calls_attempted=0, model_response=None, context=None,
                   applied_defaults=tuple(item for item in plan.applied_defaults
                                          if item != 'indicator_dependency:atr14_for_risk'),
                   user_question=plan.user_question + '；' + question)


def _replace(plan, target, replacement, question):
    if type(replacement) is int:
        if 'window' not in target.parameters:
            _issue('indicator_edit_ambiguous', '请提供该指标的完整参数。')
        replacement = IndicatorSpec(target.kind, **{**target.parameters, 'window': replacement})
    if replacement.kind != target.kind:
        _issue('indicator_kind_change_not_supported', '请使用增加/删除命令改变指标种类。')
    if replacement == target:
        _issue('indicator_edit_unchanged', '参数没有变化，未重复查询。')
    if replacement in plan.indicators:
        _issue('indicator_edit_duplicate', '修改后会与已有指标重复，未查询。')
    return _finish(plan, tuple(replacement if spec == target else spec for spec in plan.indicators), question)


def _pending(plan, candidates, replacement, question, now, reference_date, development):
    from trading_companion.product_plan import ProductPlanContext
    edit = ProductIndicatorEdit(plan, replacement, candidates, question)
    context = ProductPlanContext(question, now, now + timedelta(minutes=15), reference_date,
        plan.source, development, (), (), '', (), plan.kinds, plan.start_date, plan.end_date,
        plan.explicit_historical, plan.wants_risk, plan.objective, plan.applied_defaults,
        plan.indicators, plan.indicators_explicit, plan.scope, edit)
    choices = '、'.join(indicator_selection_label(spec) for spec in candidates)
    return replace(plan, status='needs_clarification', code='indicator_edit_ambiguous',
        message='请明确要修改哪个指标：' + choices + '。', context=context,
        model_calls_attempted=0, model_response=None)


def edit_product_plan(plan, question, *, now, reference_date, development):
    """No model may reinterpret or silently drop parts of an edit command."""
    try:
        text = unicodedata.normalize('NFKC', question)
        text = re.sub(r'^(\s*(?:(?:请帮我|请|帮我)\s*)?)再加\s*', r'\1添加', text)
        if 'technical' not in plan.kinds:
            # Dormant defaults in a news/fundamentals plan were never researched.
            # An explicit first addition authorizes only the added indicators.
            _request(plan)  # Retain the single checked subject requirement.
            update = parse_condition_update(text)
            if update.kind != 'add':
                _issue('indicator_edit_target_missing', '上一轮未研究技术指标，请先明确增加的指标。')
            return _finish(plan, (parse_indicator_instance(update.value),), question)
        request = _request(plan)
        window = window_edit_candidates(request, text)
        if window is not None:
            replacement, candidates = window
            if len(candidates) > 1:
                return _pending(plan, candidates, replacement, question, now, reference_date, development)
            if not candidates:
                _issue('indicator_edit_target_missing', '上一轮没有对应指标，未查询。')
            return _replace(plan, candidates[0], replacement, question)
        match = _EDIT.fullmatch(text)
        if match is not None:
            candidates = indicator_candidates(request, match[1].strip())
            if len(candidates) > 1:
                return _pending(plan, candidates, parse_indicator_instance(match[2].strip()),
                                question, now, reference_date, development)
            edited = replace_indicator(request, text)
            return _finish(plan, edited.indicators, question)
        update = parse_condition_update(text)
        if update.kind not in ('add', 'remove'):
            _issue('condition_update_not_supported', '本次仅修改指标；请明确增加、删除或替换的指标。')
        conditions = ResearchConditions(plan.start_date, plan.end_date, plan.indicators, plan.source,
                                        reference_date, plan.applied_defaults, plan.objective)
        updated = update_conditions(conditions, update, instrument_id=request.instrument_id, reference_date=reference_date)
        return _finish(plan, updated.indicators, question)
    except (InputIssue, InvalidRequestError, ValueError, OverflowError) as issue:
        return replace(plan, status='needs_clarification', code=getattr(issue, 'code', 'invalid_indicator_edit'),
            message=str(issue) if isinstance(issue, InputIssue) else '指标参数无效，未修改或查询。',
            context=None, model_calls_attempted=0, model_response=None)


def resume_edit(context, question, *, directory, reference_date, now):
    edit = context.edit
    from trading_companion.product_plan import ProductPlan
    if type(edit) is not ProductIndicatorEdit or type(edit.plan) is not ProductPlan:
        return ProductPlan('failed', 'invalid_product_context', '保存的指标修改无效，请重新研究。')
    for security in edit.plan.securities:
        match = directory.lookup(security.instrument_id)
        if match.status != 'resolved' or match.candidates[0] != security:
            return replace(edit.plan, status='needs_clarification', code='product_identity_changed', context=None)
    target = indicator_selection(question)
    if target not in edit.candidates:
        return replace(edit.plan, status='needs_clarification', code='indicator_edit_reply_not_specific',
                       message='请完整选择原候选指标，不附加其他修改。', context=context,
                       model_calls_attempted=0, model_response=None)
    try:
        return _replace(edit.plan, target, edit.replacement, edit.question)
    except (InputIssue, InvalidRequestError, ValueError, OverflowError) as issue:
        return replace(edit.plan, status='needs_clarification', code=getattr(issue, 'code', 'invalid_indicator_edit'),
                       message=str(issue), context=context, model_calls_attempted=0, model_response=None)
