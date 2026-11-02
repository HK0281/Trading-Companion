"""Independent fixed protocol responses; these tests do not predict corpus questions."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from copy import deepcopy
import unittest

from trading_companion.agent_input import InputIssue
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_intent_v2 import (
    IntentContext, SCHEMA, TOOL_NAME, VERSION, check_response, normalize_horizon, understand,
)
from trading_companion.research_objective import ResearchObjective
from trading_companion.research_session import ResearchSession
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.workflow_evaluation_fixtures import NOW, REFERENCE_DATE, SECURITIES


def context(*, previous=False, age=0, horizon=None, securities=SECURITIES[:1], kind='company', **changes):
    session = ResearchSession()
    if previous:
        scope = SimpleNamespace(objective=ResearchObjective('decision' if horizon else 'analysis', horizon),
                                instrument_id='000300.SH' if kind == 'index' else securities[0].instrument_id,
                                horizon=horizon)
        session.stage_research_subject(() if kind == 'index' else securities, kind=kind,
            source='tencent', development=False, now=NOW-timedelta(seconds=age), scope=scope)
    value = IntentContext(session, SecurityDirectory(SECURITIES, 'v2-fixed', NOW, False), NOW, REFERENCE_DATE)
    return replace(value, **changes)


def draft(*, reference=None, **changes):
    """Construct current-protocol synthetic inputs; never adapt provider responses."""
    value = dict(version=VERSION, task='stock_research', operation='new', objective_kind='analysis',
        objective_phrase=None, wants_plan=False, plan_phrase=None,
        subject=dict(source='question', mentions=['平安银行'], selection=None),
        horizon_expression=None, date_expression=None, indicator_mentions=[], research_kinds=[],
        exclusions=[], strict_scope=False, scope_phrase=None, confidence='high', clarification_reason=None)
    value.update(changes)
    value['subject'] = deepcopy(value['subject'])
    value['subject'].setdefault('reference', reference or dict(
        kind='latest' if value['subject']['source'] == 'previous' else 'none', index=None,
        scope='latest_research' if value['subject']['source'] == 'previous' else None))
    return value


def response(value=None, **changes):
    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('v2-fixed', TOOL_NAME, value or draft()),), **changes)


def checked(question, value=None, ctx=None):
    return check_response(question, response(value), context=ctx or context())


class ProtocolTests(unittest.TestCase):
    def test_index_mentions_may_be_contained_in_one_trusted_span_only(self):
        log = []
        value = draft(task='index_research', subject=dict(source='question', mentions=['A股'], selection=None))
        result = check_response('A股整体的走势', response(value), context=context(), corrections=log)
        self.assertEqual(result.bound_subjects, ('000001.SH',))
        self.assertIn('index_alias', [c['kind'] for c in log])
        for question, mentions in (('研究沪深300和科创50', ['沪深300和科创50']),
                                   ('研究科创板指数', ['科创板']),
                                   ('研究沪深300ETF', ['沪深300']),
                                   ('研究沪深300和沪深300ETF', ['沪深300']),
                                   ('研究上证指数和沪深300指数', ['指数'])):
            with self.subTest(question=question):
                value = draft(task='index_research', subject=dict(source='question', mentions=mentions, selection=None))
                try:
                    result = checked(question, value)
                except InputIssue:
                    continue
                self.assertEqual(result.bound_subjects, ())
                self.assertEqual(result.outcome, 'reject' if 'ETF' in question else 'clarify_identity')

    def test_future_horizon_repairs_analysis_without_adding_prices_or_reusing_old_horizon(self):
        for mention, task, wants_plan, objective in (('平安银行', 'stock_research', False, 'decision'),
                ('沪深300', 'index_research', False, 'outlook'), ('沪深300', 'index_research', True, 'decision')):
            log = []
            value = draft(task=task, subject=dict(source='question', mentions=[mention], selection=None),
                          horizon_expression='中线', wants_plan=wants_plan, plan_phrase='参考价' if wants_plan else None)
            question = mention+'中线研究'+('给参考价' if wants_plan else '，不要计划')
            result = check_response(question, response(value), context=context(), corrections=log)
            self.assertEqual(result.objective_kind, objective)
            self.assertEqual(result.wants_plan, wants_plan)
            self.assertEqual(result.horizon, '未来40个交易日')
            self.assertEqual(result.outcome, 'research')
            self.assertIn('objective_horizon_future', [c['kind'] for c in log])
            raw = check_response(question, response(value), context=context(), normalize=False)
            self.assertEqual(raw.objective_kind, 'analysis')
        for phrase in ('后面', '未来999天', '近三个月'):
            result = checked('平安银行'+phrase+'研究', draft(horizon_expression=phrase))
            self.assertEqual(result.objective_kind, 'analysis')
        result = checked('继续研究它', draft(subject=dict(source='previous', mentions=[], selection='它')),
                         context(previous=True, horizon='未来40个交易日'))
        self.assertEqual(result.objective_kind, 'analysis')

    def test_vague_date_window_is_null_but_quantified_or_uncovered_dates_survive(self):
        for phrase in ('最近', '近期', '近来', '近日'):
            log = []
            value = draft(date_expression=phrase)
            result = check_response('平安银行'+phrase+'表现', response(value), context=context(), corrections=log)
            self.assertIsNone(result.date_expression)
            self.assertEqual(result.outcome, 'research')
            self.assertEqual(result.historical_window, checked('研究平安银行').historical_window)
            self.assertIn('vague_date_window', [c['kind'] for c in log])
            self.assertEqual(checked('平安银行'+phrase+'表现', draft()).outcome, 'research')
            future = checked('平安银行'+phrase+'表现和未来方向', draft(objective_kind='decision', objective_phrase='未来方向'))
            self.assertEqual(future.outcome, 'research')
        value = draft(date_expression='最近一个月')
        self.assertEqual(checked('研究平安银行最近一个月', value).date_expression, '最近一个月')
        self.assertEqual(checked('研究平安银行最近未来999天', draft(date_expression='最近')).outcome, 'clarify_conditions')
        with self.assertRaises(InputIssue):
            checked('研究平安银行', draft(date_expression='近日'))

    def test_update_without_record_keeps_operation_but_never_borrows_history(self):
        value = draft(task='clarify', operation='update', clarification_reason='subject', confidence='low',
                      subject=dict(source='none', mentions=[], selection=None))
        log = []
        result = check_response('取消之后继续研究', response(value), context=context(history=('研究浦发银行',)), corrections=log)
        self.assertEqual(result.operation, 'update')
        self.assertEqual(result.outcome, 'clarify_subject')
        self.assertEqual(result.bound_subjects, ())
        self.assertNotIn('update_without_record', [c['kind'] for c in log])
        for ctx in (context(previous=True), context(previous=True, age=901), context(previous=True, source='akshare_eastmoney')):
            self.assertEqual(checked('取消之后继续研究', value, ctx).operation, 'update')
        for field in ('_pending', '_pending_edit', 'product_pending', 'index_pending', 'screening_saved'):
            ctx = context()
            setattr(ctx.session, field, SimpleNamespace(stage='invalid'))
            self.assertEqual(checked('取消之后继续研究', value, ctx).operation, 'update')
        with self.assertRaises(InputIssue):
            checked('取消之后继续研究', dict(value, objective_phrase='历史里才有的依据'))

    def test_non_index_outlook_uses_stock_decision_without_adding_prices(self):
        for task, subject in (('stock_research', dict(source='question', mentions=['浦发银行'], selection=None)),
                              ('clarify', dict(source='none', mentions=[], selection=None))):
            question = '浦发银行后续方向' if task == 'stock_research' else '后续方向'
            log, value = [], draft(task=task, subject=subject, objective_kind='outlook',
                objective_phrase='后续方向', clarification_reason='subject' if task == 'clarify' else None)
            result = check_response(question, response(value), context=context(), corrections=log)
            self.assertEqual(result.objective_kind, 'decision' if task == 'stock_research' else 'outlook')
            self.assertFalse(result.wants_plan)
            self.assertEqual(result.outcome, 'research' if task == 'stock_research' else 'clarify_subject')
            self.assertEqual('objective_outlook_non_index' in [c['kind'] for c in log], task == 'stock_research')
            raw = check_response(question, response(value), context=context(), normalize=False)
            self.assertEqual(raw.objective_kind, 'outlook')
        index = checked('沪深300后续方向', draft(task='index_research', objective_kind='outlook',
            objective_phrase='后续方向', subject=dict(source='question', mentions=['沪深300'], selection=None)))
        self.assertEqual(index.objective_kind, 'outlook')

    def test_generic_future_objective_is_not_a_historical_date_or_atr_request(self):
        for objective in ('未来方向', '未来的走势', '未来前景'):
            log = []
            value = draft(objective_kind='decision', objective_phrase=objective,
                          exclusions=[dict(kind='indicator:atr', phrase='不用ATR')])
            result = check_response('平安银行'+objective+'，不用ATR', response(value), context=context(), corrections=log)
            self.assertEqual(result.outcome, 'research')
            self.assertFalse(result.wants_plan)
            self.assertIsNone(result.plan_blocked)
            self.assertEqual(result.exclusions, ('indicator:atr',))
            self.assertEqual(len(result.historical_window), 2)
            self.assertIn('future_objective_date_cue', [c['kind'] for c in log])
            raw = check_response('平安银行'+objective+'，不用ATR', response(value), context=context(), normalize=False)
            self.assertEqual(raw.outcome, 'clarify_conditions')
        # Neither a missing time extraction nor an invalid historical window is erased.
        for extra in ('未来999天', '未来若干天', '2027-01-01的数据', '今天的数据'):
            value = draft(objective_kind='decision', objective_phrase='未来方向')
            self.assertEqual(checked('平安银行未来方向，参考'+extra, value).outcome, 'clarify_conditions')
        self.assertEqual(checked('平安银行未来方向', draft()).outcome, 'clarify_conditions')

    def test_model_fundamentals_domains_are_not_automatically_expanded(self):
        log = []
        value = draft(research_kinds=[dict(kind='financial', phrase='基本面')])
        result = check_response('平安银行基本面', response(value), context=context(), corrections=log)
        self.assertEqual(result.research_kinds, ('financial',))
        self.assertEqual(log, [])
        raw = check_response('平安银行基本面', response(value), context=context(), normalize=False)
        self.assertEqual(raw.research_kinds, ('financial',))
        self.assertEqual(checked('平安银行财报', draft(research_kinds=[dict(kind='financial', phrase='财报')])).research_kinds,
                         ('financial',))
        conflict = checked('平安银行基本面，不看公司资料', dict(value, exclusions=[dict(kind='profile', phrase='不看公司资料')]))
        self.assertEqual(conflict.outcome, 'research')
        self.assertEqual(conflict.research_kinds, ('financial',))
        self.assertEqual(conflict.exclusions, ('profile',))
        with self.assertRaises(InputIssue):
            checked('平安银行基本面', draft(research_kinds=[dict(kind='financial', phrase='虚构基本面')]))

    def test_grounded_indicator_fragments_are_preserved_without_semantic_repair(self):
        value = draft(indicator_mentions=['SMA5', 'SMA'])
        original, log = deepcopy(value), []
        result = check_response('平安银行参考SMA5', response(value), context=context(), corrections=log)
        self.assertEqual(result.indicator_mentions, ('SMA5', 'SMA'))
        self.assertEqual(value, original)
        self.assertEqual(log, [])
        raw = check_response('平安银行参考SMA5', response(value), context=context(), normalize=False)
        self.assertEqual(raw.indicator_mentions, ('SMA5', 'SMA'))
        result = checked('平安银行参考SMA5和默认SMA', value)
        self.assertEqual(result.indicator_mentions, ('SMA5', 'SMA'))
        with self.assertRaises(InputIssue):
            checked('平安银行参考SMA5', draft(indicator_mentions=['SMA5', 'RSI']))

    def test_clarify_task_retains_conflict_and_never_recovers_research(self):
        for mention, task in (('平安银行', 'stock_research'), ('沪深300', 'index_research')):
            value = draft(task='clarify', clarification_reason='conflict', confidence='low',
                subject=dict(source='question', mentions=[mention], selection=None))
            log = []
            result = check_response(mention+'要求有冲突', response(value), context=context(), corrections=log)
            self.assertEqual(result.task, task if task == 'stock_research' else 'clarify')
            self.assertEqual(result.outcome, 'clarify_conflict')
            self.assertTrue(result.low_confidence)
            self.assertNotIn('clarification_task', [c['kind'] for c in log])
            raw = check_response(mention+'要求有冲突', response(value), context=context(), normalize=False)
            self.assertEqual(raw.task, 'clarify')
        with self.assertRaises(InputIssue):
            checked('沪深300财报有冲突', draft(task='index_research', clarification_reason='conflict',
                subject=dict(source='question', mentions=['沪深300'], selection=None),
                research_kinds=[dict(kind='financial', phrase='财报')]))

    def test_board_shorthand_requires_index_context_and_excludes_funds_and_stocks(self):
        for suffix in ('中线参考计划', '止盈止损方案', '价位如何', '计划如何', '走势怎样'):
            value = draft(task='index_research', subject=dict(source='question', mentions=['创业板'], selection=None))
            result = checked('创业板'+suffix, value)
            self.assertEqual(result.bound_subjects, ('399006.SZ',))
        for suffix in ('ETF走势', '基金走势', '股票中线计划', '个股价位', '短线ETF走势', '上市规则'):
            value = draft(task='index_research', subject=dict(source='none', mentions=[], selection=None))
            result = checked('创业板'+suffix, value)
            self.assertEqual(result.bound_subjects, ())
            self.assertEqual(result.outcome, 'clarify_subject')

    def test_nullable_corrections_are_audited_and_raw_response_is_immutable(self):
        value = draft(date_expression='null', horizon_expression=' NONE ', objective_phrase='无')
        original = deepcopy(value)
        log = []
        result = check_response('分析平安银行', response(value), context=context(), corrections=log)
        self.assertEqual(result.outcome, 'research')
        self.assertEqual(value, original)
        self.assertEqual(len(log), 3)
        self.assertTrue(all(c['kind'] == 'nullable_null' for c in log))
        with self.assertRaises(InputIssue):
            check_response('分析平安银行', response(value), context=context(), normalize=False)
        with self.assertRaises(InputIssue):
            checked('分析平安银行', draft(operation='recall', date_expression='null'))

    def test_low_confidence_preserves_specific_conflict_identity_and_veto(self):
        conflict = checked('平安银行给价位，不要价位', draft(confidence='low', wants_plan=True,
            plan_phrase='给价位', clarification_reason='conflict'))
        self.assertEqual(conflict.outcome, 'clarify_conflict')
        self.assertFalse(conflict.wants_plan)
        ambiguous = checked('研究平安', draft(confidence='low', subject=dict(source='question', mentions=['平安'], selection=None)))
        self.assertEqual(ambiguous.outcome, 'clarify_identity')
        self.assertEqual(checked('分析平安银行', draft(confidence='low', clarification_reason='horizon')).outcome, 'clarify_horizon')

    def test_unquantified_time_does_not_select_horizon_or_hide_invalid_quantities(self):
        for phrase in ('后面', '现在', '接下来'):
            for wants_plan in (False, True):
                result = checked('平安银行'+phrase+'给参考价', draft(horizon_expression=phrase,
                    wants_plan=wants_plan, plan_phrase='参考价' if wants_plan else None))
                self.assertIsNone(result.horizon)
                self.assertEqual(result.outcome, 'clarify_horizon' if wants_plan else 'research')
        bad = checked('平安银行未来999天给参考价', draft(horizon_expression='未来999天', wants_plan=True, plan_phrase='参考价'))
        self.assertEqual(bad.outcome, 'clarify_conditions')

    def test_index_aliases_require_same_identity_and_all_explicit_objects(self):
        for question, mention, expected in (('大盘指数有何变化', '大盘指数', '000001.SH'),
                                             ('创业板后续表现如何', '创业板', '399006.SZ')):
            log = []
            value = draft(task='index_research', subject=dict(source='question', mentions=[mention], selection=None))
            result = check_response(question, response(value), context=context(), corrections=log)
            self.assertEqual(result.bound_subjects, (expected,))
            self.assertEqual(log[0]['kind'], 'index_alias')
            with self.assertRaises(InputIssue):
                check_response(question, response(value), context=context(), normalize=False)
        broad = checked('A股后市怎样', draft(task='index_research', subject=dict(source='question', mentions=['A股'], selection=None)))
        self.assertEqual(broad.bound_subjects, ('000001.SH',))
        for question, mentions in (('研究大盘指数和沪深300', ['大盘指数']),
                                   ('研究大盘指数和平安银行', ['大盘指数'])):
            with self.subTest(question=question), self.assertRaises(InputIssue):
                checked(question, draft(task='index_research', subject=dict(source='question', mentions=mentions, selection=None)))
        for question in ('研究大盘指数ETF', '研究沪深300指数基金', '研究创业板期权'):
            result = checked(question, draft(task='index_research', subject=dict(source='none', mentions=[], selection=None)))
            self.assertEqual(result.bound_subjects, ())
            self.assertEqual(result.outcome, 'clarify_subject')

    def test_index_domain_normalization_cannot_erase_financial_or_strict_exclusion(self):
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
                      indicator_mentions=['均线'], research_kinds=[dict(kind='technical', phrase='均线')])
        result = checked('研究沪深300的均线', value)
        self.assertEqual(result.research_kinds, ('technical',))
        self.assertEqual(result.indicator_mentions, ('均线',))
        with self.assertRaises(InputIssue):
            checked('研究沪深300的财报，不要技术面', dict(value, indicator_mentions=[],
                research_kinds=[dict(kind='technical', phrase='技术面'), dict(kind='financial', phrase='财报')]))
        conflict = checked('研究沪深300的财报，不要技术面', dict(value, indicator_mentions=[],
            research_kinds=[dict(kind='technical', phrase='技术面')], exclusions=[dict(kind='technical', phrase='不要技术面')]))
        self.assertEqual(conflict.outcome, 'clarify_conflict')
        self.assertEqual(conflict.research_kinds, ())
        self.assertEqual(conflict.exclusions, ('technical',))

    def test_schema_and_checker_reject_extra_missing_and_wrong_types(self):
        self.assertEqual(set(SCHEMA['required']), set(draft()))
        bad = []
        for key in draft():
            missing = draft()
            del missing[key]
            bad.append(missing)
        bad += [draft(ticker='600000.SH'), draft(wants_plan=1), draft(strict_scope='true'),
                draft(task='company_research'), draft(confidence='medium'),
                draft(subject=dict(source='previous', mentions=[], selection=None, code='000001.SZ')),
                draft(indicator_mentions=['SMA3', 'SMA3'])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(InputIssue):
                checked('分析平安银行', value)

    def test_wrong_tool_multiple_calls_and_text_only(self):
        for payload in (ToolModelResponse('complete', text='直接回答'),
                        ToolModelResponse('tool_calls', tool_calls=(ToolCall('x', 'other', {}),)),
                        ToolModelResponse('tool_calls', tool_calls=(ToolCall('x', TOOL_NAME, draft()), ToolCall('y', TOOL_NAME, draft())))):
            with self.subTest(payload=payload), self.assertRaises(InputIssue):
                check_response('分析平安银行', payload, context=context())

    def test_low_confidence_keeps_checked_fields_and_requests_confirmation(self):
        intent = checked('分析平安银行', draft(confidence='low'))
        self.assertTrue(intent.low_confidence)
        self.assertEqual(intent.bound_subjects, ('000001.SZ',))
        self.assertEqual(intent.outcome, 'clarify_conditions')
        self.assertNotIn('low_confidence', intent.fields())
        missing = checked('分析它', draft(confidence='low', subject=dict(source='previous', mentions=[], selection='它')))
        self.assertEqual(missing.outcome, 'clarify_subject')
        with self.assertRaises(InputIssue):
            checked('分析平安银行', draft(confidence='low', date_expression='不存在的片段'))

    def test_general_horizon_intervals_midpoints_and_boundaries(self):
        for expression, expected in (('未来2~4个月', '未来40个交易日'), ('接下来一到二周', '未来10个交易日'),
                ('持有十至二十天', '未来40个交易日'), ('三四周', '未来40个交易日'),
                ('往后六至八个月', '未来120个交易日'), ('未来10～18天', '未来10个交易日'),
                ('未来11-18天', '未来40个交易日'), ('未来2到3个交易日', '未来10个交易日')):
            with self.subTest(expression=expression):
                self.assertEqual(normalize_horizon(expression), expected)
        for expression in ('未来4到2个月', '未来0到2周', '未来1至13个月', '三五个月', '未来2到3个月左右'):
            with self.subTest(expression=expression), self.assertRaises(InputIssue):
                normalize_horizon(expression)

    def test_every_evidence_field_must_come_from_current_question(self):
        for value in (draft(subject=dict(source='question', mentions=['浦发银行'], selection=None)),
                      draft(horizon_expression='短线'), draft(date_expression='最近30天'),
                      draft(indicator_mentions=['SMA3']), draft(scope_phrase='只看技术面'),
                      draft(research_kinds=[dict(kind='financial', phrase='财报')]),
                      draft(exclusions=[dict(kind='plan', phrase='不要价位')])):
            with self.subTest(value=value), self.assertRaises(InputIssue):
                checked('分析平安银行', value)

    def test_direction_rating_and_price_plan_are_distinct(self):
        rating = checked('平安银行未来走势怎样', draft(objective_kind='decision', objective_phrase='未来走势'))
        self.assertEqual(rating.objective_kind, 'decision')
        self.assertFalse(rating.wants_plan)
        price = checked('平安银行给建仓参考价', draft(objective_kind='decision', objective_phrase='建仓参考价',
                         wants_plan=True, plan_phrase='建仓参考价'))
        self.assertEqual(price.outcome, 'clarify_horizon')
        self.assertTrue(price.wants_plan)

    def test_veto_and_conflict_never_authorize_prices(self):
        for phrase in ('不要价位', '禁用计划', '不需要风险计划'):
            plain = checked('分析平安银行，'+phrase)
            self.assertFalse(plain.wants_plan)
            self.assertEqual(plain.exclusions, ())
            contradictory = checked('平安银行给价位，'+phrase,
                draft(objective_kind='decision', objective_phrase='给价位', wants_plan=True, plan_phrase='给价位'))
            self.assertEqual(contradictory.outcome, 'clarify_conflict')
            self.assertFalse(contradictory.wants_plan)
        with self.assertRaises(InputIssue):
            checked('分析平安银行的ATR', draft(exclusions=[dict(kind='indicator:atr', phrase='ATR')]))

    def test_positive_indicators_cannot_override_exclusions_or_drop_strict_scope(self):
        value = draft(indicator_mentions=['SMA3'], strict_scope=True, scope_phrase='只用SMA3',
                      research_kinds=[dict(kind='technical', phrase='SMA3')], exclusions=[dict(kind='indicator:atr', phrase='禁用ATR')])
        intent = checked('平安银行只用SMA3，禁用ATR', value)
        self.assertEqual(intent.indicator_mentions, ('SMA3',))
        self.assertEqual(intent.exclusions, ('indicator:atr',))
        for bad in (draft(), draft(strict_scope=True, scope_phrase='只用SMA3')):
            with self.subTest(bad=bad):
                result = checked('平安银行只用SMA3，禁用ATR', bad)
                self.assertFalse(result.wants_plan)
                self.assertEqual(result.strict_scope, bad['strict_scope'])
        conflict = checked('平安银行只用SMA3，禁用ATR',
            draft(indicator_mentions=['SMA3', 'ATR'], strict_scope=True, scope_phrase='只用SMA3', exclusions=[dict(kind='indicator:atr', phrase='禁用ATR')]))
        self.assertEqual(conflict.outcome, 'clarify_conflict')
        self.assertEqual(conflict.indicator_mentions, ('SMA3',))
        self.assertEqual(conflict.exclusions, ('indicator:atr',))

    def test_strict_scope_blocks_implicit_atr_dependency(self):
        intent = checked('平安银行短线给价位，只用SMA3', draft(objective_kind='decision', objective_phrase='给价位',
            wants_plan=True, plan_phrase='给价位', horizon_expression='短线', indicator_mentions=['SMA3'],
            strict_scope=True, scope_phrase='只用SMA3'))
        self.assertEqual(intent.plan_blocked, 'risk_atr_not_authorized')
        self.assertEqual(intent.indicator_mentions, ('SMA3',))
        self.assertEqual(intent.horizon, '未来10个交易日')

    def test_historical_dates_and_future_horizons_are_separate(self):
        question = '平安银行用2026-08-01至2026-09-20的资料判断未来2至4个月'
        intent = checked(question, draft(objective_kind='decision', objective_phrase='判断',
            horizon_expression='未来2至4个月', date_expression='2026-08-01至2026-09-20'))
        self.assertEqual(intent.horizon, '未来40个交易日')
        self.assertEqual(intent.historical_window, ('2026-08-01', '2026-09-20'))
        for phrase in ('近期', '下下个月', '明年'):
            intent = checked('平安银行'+phrase+'给价位', draft(objective_kind='decision', objective_phrase='给价位',
                wants_plan=True, plan_phrase='给价位', horizon_expression=phrase))
            self.assertTrue(intent.outcome.startswith('clarify'))
            self.assertIsNone(intent.horizon)
        future = checked('平安银行用2027-01-01的数据', draft(date_expression='2027-01-01'))
        self.assertEqual(future.outcome, 'clarify_conditions')

    def test_knowledge_is_not_a_generated_answer_or_execution(self):
        intent = checked('什么是市净率', draft(task='knowledge', subject=dict(source='none', mentions=[], selection=None)))
        self.assertEqual(intent.outcome, 'local')
        self.assertEqual(intent.bound_subjects, ())
        self.assertFalse(intent.zero_intent_calls)
        self.assertNotIn('answer', intent.fields())


class BindingTests(unittest.TestCase):
    def test_missing_program_records_only_reduce_previous_claims_and_preserve_clarification(self):
        value = draft(task='clarify', clarification_reason='subject', confidence='low',
                      subject=dict(source='previous', mentions=[], selection='它'))
        log = []
        result = check_response('分析它', response(value), context=context(history=('研究浦发银行',)), corrections=log)
        self.assertEqual(result.subject_source, 'none')
        self.assertEqual(result.bound_subjects, ())
        self.assertEqual(result.outcome, 'clarify_subject')
        self.assertEqual({entry['kind'] for entry in log}, {'reference_unbound', 'previous_without_record'})
        raw = check_response('分析它', response(value), context=context(), normalize=False)
        self.assertEqual(raw.subject_source, 'previous')
        for ctx, expected in ((context(previous=True), 'previous'),
                              (context(previous=True, source='akshare_eastmoney'), 'none')):
            result = checked('分析它', value, ctx)
            self.assertEqual(result.subject_source, expected)
        with self.assertRaises(InputIssue):
            checked('分析它', dict(value, subject=dict(source='previous', mentions=[], selection='不存在的对象')))

    def test_none_source_selection_does_not_bind_even_with_available_history(self):
        for selection in ('它', '上一只'):
            value = draft(task='clarify', clarification_reason='subject', confidence='low',
                          subject=dict(source='none', mentions=[], selection=selection))
            for normalize in (True, False):
                with self.subTest(selection=selection, normalize=normalize), self.assertRaises(InputIssue):
                    check_response('分析'+selection, response(value), context=context(previous=True), normalize=normalize)
        with self.assertRaises(InputIssue):
            checked('研究浦发银行', draft(subject=dict(source='none', mentions=['浦发银行'], selection='它')))

    def test_latest_single_reference_keeps_identity_without_reviving_expired_conditions(self):
        for selection in ('上一只', '上一个', '刚才那只'):
            value = draft(subject=dict(source='previous', mentions=[], selection=selection),
                objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价')
            log = []
            result = check_response(selection+'给参考价', response(value),
                context=context(previous=True, age=901, horizon='未来40个交易日'), corrections=log)
            self.assertEqual(result.bound_subjects, ('000001.SZ',))
            self.assertIsNone(result.horizon)
            self.assertEqual(result.outcome, 'clarify_horizon')
            self.assertEqual(log, [])
            raw = check_response(selection+'给参考价', response(value), context=context(previous=True), normalize=False)
            self.assertEqual(raw.outcome, 'clarify_horizon')
            for ctx in (context(previous=True, securities=SECURITIES[:2]), context(previous=True, source='akshare_eastmoney')):
                self.assertEqual(checked(selection+'给参考价', value, ctx).bound_subjects, ())

    def test_checked_identity_never_retires_model_clarification(self):
        for reason in ('subject', 'conflict', 'identity'):
            value = draft(clarification_reason=reason, confidence='low', subject=dict(source='none', mentions=[], selection=None))
            with self.assertRaises(InputIssue):
                checked('研究平安银行', value)
            explicit = draft(clarification_reason=reason, confidence='low')
            self.assertEqual(checked('研究平安银行', explicit).outcome, 'clarify_'+reason)
        with self.assertRaises(InputIssue):
            checked('研究平安', value)
        old_log = [dict(kind='explicit_subject')]
        result = check_response('研究它', response(draft(confidence='low', clarification_reason='subject',
            subject=dict(source='previous', mentions=[], selection='它'))), context=context(previous=True), corrections=old_log)
        self.assertEqual(result.outcome, 'clarify_subject')

    def test_subject_clarification_keeps_task_and_checked_identity_separate(self):
        for mention in ('大盘指数', '沪深300', '创业板'):
            question = mention+'中线参考计划'
            value = draft(task='clarify', clarification_reason='subject', confidence='low', objective_kind='decision',
                objective_phrase='参考计划', wants_plan=True, plan_phrase='参考计划', horizon_expression='中线',
                subject=dict(source='question', mentions=[mention], selection=None))
            result = checked(question, value)
            self.assertEqual(result.task, 'clarify')
            self.assertEqual(result.outcome, 'clarify_subject')
            self.assertTrue(result.low_confidence)
        for question, mentions in (('研究平安', ['平安']), ('比较平安银行和浦发银行', ['平安银行', '浦发银行']),
                                   ('它和浦发银行比较', ['浦发银行'])):
            result = checked(question, draft(clarification_reason='subject', confidence='low',
                subject=dict(source='question', mentions=mentions, selection=None)), context(previous=True))
            self.assertTrue(result.outcome.startswith('clarify_'))


    def test_explicit_source_correction_never_drops_a_referenced_object(self):
        cases = (
            ('它和浦发银行比哪个好', '它'),
            ('它和浦发银行比哪个更值得研究', '它'),
            ('把它换成浦发银行再分析', '它'),
            ('它们和浦发银行比较', '它们'),
            ('这只和浦发银行比较', '这只'),
            ('这只股和浦发银行比较', '这只股'),
            ('该股和浦发银行比较', '该股'),
            ('这家公司和浦发银行比较', '这家公司'),
            ('该公司和浦发银行比较', '该公司'),
            ('上一只和浦发银行比较', '上一只'),
            ('上一个和浦发银行比较', '上一个'),
            ('刚才那只和浦发银行比较', '刚才那只'),
            ('第二只和浦发银行比较', '第二只'),
            ('第2只和浦发银行比较', '第2只'),
            ('第２只和浦发银行比较', '第２只'),
            ('第 二 只和浦发银行比较', '第 二 只'),
        )
        for question, reference in cases:
            for selection in (reference, None):
                for source in ('previous', 'none'):
                    with self.subTest(question=question, selection=selection, source=source):
                        value = draft(subject=dict(source=source, mentions=[], selection=selection))
                        original, log = deepcopy(value), []
                        with self.assertRaises(InputIssue):
                            check_response(question, response(value), context=context(previous=True), corrections=log)
                        self.assertFalse(any(c['kind'] == 'explicit_subject' for c in log))
                        self.assertEqual(value, original)
        for selection in ('它', None):
            value = draft(task='index_research', subject=dict(source='previous', mentions=[], selection=selection))
            with self.subTest(index_selection=selection), self.assertRaises(InputIssue):
                checked('它和上证指数比较', value, context(previous=True, kind='index'))

    def test_explicit_source_correction_never_repairs_model_semantics_or_fabricated_selection(self):
        ctx = context(previous=True, horizon='未来40个交易日')
        value = draft(subject=dict(source='previous', mentions=[], selection='浦发银行'), operation='refresh',
                      objective_kind='decision', objective_phrase='给方向')
        log = []
        with self.assertRaises(InputIssue):
            check_response('浦发银行给方向', response(value), context=ctx, corrections=log)
        self.assertEqual(log, [])
        with self.assertRaises(InputIssue):
            check_response('浦发银行给方向', response(value), context=ctx, normalize=False)
        for selection in ('第二只', '招商银行'):
            with self.assertRaises(InputIssue):
                checked('浦发银行给方向', dict(value, subject=dict(source='previous', mentions=[], selection=selection)), ctx)

    def test_unwritten_selection_rejects_and_implicit_latest_never_selects_multiple(self):
        value = draft(subject=dict(source='previous', mentions=[], selection='它'))
        with self.assertRaises(InputIssue):
            checked('继续分析', value, context(previous=True, securities=SECURITIES[:2]))
        value['subject']['selection'] = None
        result = checked('继续分析', value, context(previous=True, securities=SECURITIES[:2]))
        self.assertEqual(result.bound_subjects, ())
        self.assertEqual(result.outcome, 'clarify_subject')
        with self.assertRaises(InputIssue):
            checked('继续分析', dict(value, subject=dict(source='previous', mentions=[], selection='第二只')), context(previous=True))

    def test_pending_evidence_only_lends_two_fields_to_valid_updates(self):
        from trading_companion.decision_horizon import gate
        from trading_companion.product_plan import ProductPlan
        from trading_companion.multi_contracts import IndicatorSpec
        ctx = context()
        plan = ProductPlan(status='ready', source='tencent', securities=SECURITIES[:1], kinds=('technical',),
            start_date=REFERENCE_DATE-timedelta(days=30), end_date=REFERENCE_DATE-timedelta(days=1),
            indicators=(IndicatorSpec('atr', window=14),),
            objective=ResearchObjective('decision'), wants_risk=True, user_question='平安银行能买吗，参考最近一个月')
        ctx.session.product_pending = gate(plan, now=NOW, reference_date=REFERENCE_DATE, development=False).context
        self.assertIsNotNone(ctx.session.product_pending)
        pending = ctx.session.product_pending
        value = draft(operation='update', objective_kind='decision', objective_phrase='能买吗',
            wants_plan=True, plan_phrase='能买吗', horizon_expression='中线',
            reference=dict(kind='latest', index=None, scope='pending'),
            subject=dict(source='previous', mentions=[], selection=None))
        log = []
        result = check_response('中线', response(value), context=ctx, corrections=log)
        self.assertEqual(result.bound_subjects, ('000001.SZ',))
        self.assertEqual(result.horizon, '未来40个交易日')
        self.assertNotEqual(result.historical_window[0], plan.start_date.isoformat())
        self.assertIn(('objective_phrase', 'program_pending'), result.provenance)
        self.assertEqual(log, [])
        invalid_contexts = (context(history=(pending.original_question,)), replace(ctx, now=NOW+timedelta(minutes=15)),
                            replace(ctx, source='akshare_eastmoney'), replace(ctx, development=True))
        for invalid in invalid_contexts:
            with self.assertRaises(InputIssue):
                checked('中线', value, invalid)
        for changes in (dict(operation='new'), dict(subject=dict(source='question', mentions=['浦发银行'], selection=None, reference=dict(kind='none', index=None, scope=None))), dict(date_expression='最近一个月'),
                        dict(indicator_mentions=['ATR']), dict(scope_phrase='只看技术面')):
            with self.assertRaises(InputIssue):
                checked('中线，改为浦发银行', dict(value, **changes), ctx)
        # A pending has its own conditions; even a fresh older publication cannot lend a horizon.
        ctx.session.stage_research_subject(SECURITIES[1:2], kind='company', source='tencent', development=False,
            now=NOW, scope=SimpleNamespace(objective=ResearchObjective('decision', '未来40个交易日')))
        ctx.session.product_pending = pending
        continued = draft(reference=dict(kind='latest', index=None, scope='pending'), operation='update', objective_kind='decision', wants_plan=True,
                          subject=dict(source='previous', mentions=[], selection=None))
        result = checked('继续', continued, ctx)
        self.assertEqual(result.bound_subjects, ('000001.SZ',))
        self.assertIsNone(result.horizon)
        self.assertEqual(result.outcome, 'clarify_horizon')
        self.assertIs(ctx.session.product_pending, pending)

    def previous(self, question='它给参考价', ctx=None, **changes):
        return checked(question, draft(subject=dict(source='previous', mentions=[], selection='它'),
            objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价', **changes),
            ctx or context(previous=True))

    def test_901_seconds_keeps_identity_but_drops_old_horizon(self):
        for age, expected in ((899, None), (900, None), (901, None)):
            with self.subTest(age=age):
                ctx = context(previous=True, age=age, horizon='未来40个交易日')
                intent = self.previous(ctx=ctx)
                self.assertEqual(intent.bound_subjects, ('000001.SZ',))
                self.assertEqual(intent.horizon, expected)
                self.assertEqual(intent.outcome, 'research' if expected else 'clarify_horizon')

    def test_explicit_new_horizon_wins_and_new_subject_inherits_nothing(self):
        ctx = context(previous=True, horizon='未来40个交易日')
        intent = self.previous('它短线给参考价', ctx, horizon_expression='短线')
        self.assertEqual(intent.horizon, '未来10个交易日')
        new = checked('浦发银行给参考价', draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价'), ctx)
        self.assertEqual(new.bound_subjects, ('600000.SH',))
        self.assertIsNone(new.horizon)
        self.assertEqual(new.outcome, 'clarify_horizon')

    def test_no_fallback_after_invalidation_reset_source_mode_or_clock_change(self):
        cases = [context(), context(previous=True, age=-1), context(previous=True, source='akshare_eastmoney'),
                 context(previous=True, development=True)]
        invalid = context(previous=True)
        invalid.session._subject_current = False
        cases.append(invalid)
        reset = context(previous=True)
        reset.session.clear()
        cases.append(reset)
        for ctx in cases:
            with self.subTest(ctx=ctx):
                intent = self.previous(ctx=ctx)
                self.assertEqual(intent.bound_subjects, ())
                self.assertEqual(intent.outcome, 'clarify_subject')

    def test_directory_identity_change_is_not_silently_accepted(self):
        ctx = context(previous=True)
        changed = tuple(Security(s.instrument_id, '新名称') if i == 0 else s for i, s in enumerate(SECURITIES))
        ctx = replace(ctx, directory=SecurityDirectory(changed, 'changed', NOW, False))
        self.assertEqual(self.previous(ctx=ctx).bound_subjects, ())

    def test_ambiguous_and_omitted_explicit_objects_require_clarification(self):
        ambiguous = checked('研究平安', draft(subject=dict(source='question', mentions=['平安'], selection=None)))
        self.assertEqual(ambiguous.outcome, 'clarify_identity')
        with self.assertRaises(InputIssue):
            checked('比较平安银行和浦发银行', draft())
        with self.assertRaises(InputIssue):
            self.previous('浦发银行，给它参考价')
        with self.assertRaises(InputIssue):
            checked('浦发银行，给它参考价', draft(task='clarify', clarification_reason='subject',
                subject=dict(source='previous', mentions=[], selection='它')), context(previous=True))

    def test_multiple_previous_objects_need_explicit_same_publication_selection(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        self.assertEqual(self.previous(ctx=ctx).outcome, 'clarify_subject')
        value = draft(reference=dict(kind='ordinal', index=2, scope='latest_research'), subject=dict(source='previous', mentions=[], selection='第二只'))
        self.assertEqual(checked('研究第二只', value, ctx).bound_subjects, ('600000.SH',))
        value['subject']['selection'] = '它们'
        value['subject']['reference'] = dict(kind='all', index=None, scope='latest_research')
        self.assertEqual(checked('研究它们', value, ctx).bound_subjects, ('000001.SZ', '600000.SH'))
        value['subject']['selection'] = '第三只'
        value['subject']['reference'] = dict(kind='ordinal', index=3, scope='latest_research')
        self.assertEqual(checked('研究第三只', value, ctx).bound_subjects, ())
        value['subject']['selection'] = '第十只'
        value['subject']['reference']['index'] = 10
        self.assertEqual(checked('研究第十只', value, context(previous=True)).bound_subjects, ())

    def test_stocks_indices_and_etfs_do_not_share_identities(self):
        index_ctx = context(previous=True, kind='index')
        self.assertEqual(self.previous(ctx=index_ctx).bound_subjects, ())
        value = draft(task='index_research', subject=dict(source='previous', mentions=[], selection='它'))
        self.assertEqual(checked('研究它', value, index_ctx).bound_subjects, ('000300.SH',))
        index = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None))
        self.assertEqual(checked('研究沪深300', index).bound_subjects, ('000300.SH',))
        with self.assertRaises(InputIssue):
            checked('研究沪深300ETF', index)
        product = checked('研究沪深300ETF', draft(task='unsupported', subject=dict(source='question', mentions=['沪深300ETF'], selection=None)))
        self.assertEqual(product.task, 'unsupported')
        self.assertEqual(product.outcome, 'reject')
        self.assertEqual(product.bound_subjects, ())

    def test_binding_checks_do_not_mutate_session_or_borrow_other_session(self):
        ctx = context(previous=True)
        original = dict(ctx.session.__dict__)
        self.previous(ctx=ctx)
        self.assertEqual(ctx.session.__dict__, original)
        self.assertEqual(self.previous(ctx=context()).bound_subjects, ())

    def test_pending_keeps_original_deadline_and_binds_only_its_securities(self):
        from trading_companion.decision_horizon import gate
        from trading_companion.product_plan import ProductPlan
        from trading_companion.multi_contracts import IndicatorSpec
        ctx = context()
        plan = ProductPlan(status='ready', source='tencent', securities=SECURITIES[:1], kinds=('technical',),
            start_date=REFERENCE_DATE-timedelta(days=365), end_date=REFERENCE_DATE-timedelta(days=1),
            wants_risk=True, objective=ResearchObjective('decision'), indicators=(IndicatorSpec('atr', window=14),),
            user_question='平安银行给参考价')
        pending = gate(plan, now=NOW, reference_date=REFERENCE_DATE, development=False).context
        self.assertIsNotNone(pending)
        ctx.session.product_pending = pending
        value = draft(reference=dict(kind='latest', index=None, scope='pending'), operation='update', subject=dict(source='previous', mentions=[], selection=None), objective_kind='decision',
                      wants_plan=True, horizon_expression='中线')
        intent = checked('中线，禁用ATR', value, ctx)
        self.assertEqual(intent.bound_subjects, ('000001.SZ',))
        self.assertEqual(intent.horizon, '未来40个交易日')
        self.assertEqual(intent.plan_blocked, 'risk_atr_not_authorized')
        self.assertIs(ctx.session.product_pending, pending)
        wrong = draft(reference=dict(kind='ordinal', index=2, scope='pending'), subject=dict(source='previous', mentions=[], selection='第二只'))
        self.assertEqual(checked('研究第二只', wrong, ctx).bound_subjects, ())
        expired = replace(ctx, now=NOW+timedelta(minutes=15))
        self.assertEqual(self.previous(ctx=expired).bound_subjects, ())
        self.assertEqual(ctx.session.product_pending.expires_at, pending.expires_at)
        other_session = context()
        self.assertEqual(self.previous(ctx=other_session).bound_subjects, ())

    def test_shortlist_selection_uses_checked_source_order_and_expiry(self):
        from trading_companion.screening import ScreeningCondition, ScreeningRequest
        from trading_companion.screening_context import SavedScreening
        quote_source = object()
        ctx = replace(context(), quote_source=quote_source)
        request = ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=3)
        ctx.session.screening_saved = SavedScreening(NOW, request, SECURITIES[:3], quote_source)
        value = draft(reference=dict(kind='ordinal', index=2, scope='shortlist'), subject=dict(source='previous', mentions=[], selection='第二只'),
                      research_kinds=[dict(kind='technical', phrase='技术面')])
        intent = checked('研究第二只的技术面', value, ctx)
        self.assertEqual(intent.bound_subjects, ('600000.SH',))
        for changed in (replace(ctx, quote_source=object()), replace(ctx, now=NOW+timedelta(minutes=15))):
            self.assertEqual(checked('研究第二只的技术面', value, changed).bound_subjects, ())
        self.assertEqual(ctx.model_context()['shortlist']['names'], [s.name for s in SECURITIES[:3]])

    def test_screening_update_binds_task_not_stock_and_preserves_grounding(self):
        from trading_companion.screening import ScreeningCondition, ScreeningRequest
        from trading_companion.screening_context import SavedScreening
        ctx = replace(context(), quote_source=object())
        request = ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=3)
        ctx.session.screening_saved = SavedScreening(NOW, request, SECURITIES[:3], ctx.quote_source)
        value = draft(task='screening', operation='update', subject=dict(source='none', mentions=[], selection=None), scope_phrase='名单')
        result = checked('名单改为8只', value, ctx)
        self.assertEqual(result.subject_source, 'none')
        self.assertEqual(result.bound_subjects, ())
        self.assertEqual(result.operation, 'update')
        for bad in (dict(value, objective_phrase='新增一个条件'),
                    dict(value, subject=dict(source='previous', mentions=[], selection='第二只'))):
            with self.assertRaises(InputIssue):
                checked('名单改为8只', bad, ctx)
        with self.assertRaises(InputIssue):
            checked('改为8只', value, ctx)  # A fabricated list phrase is not silently discarded.
        for invalid in (replace(ctx, now=NOW+timedelta(minutes=15)), replace(ctx, quote_source=object()),
                        replace(ctx, source='akshare_eastmoney')):
            result = checked('名单改为8只', value, invalid)
            self.assertEqual((result.operation, result.bound_subjects), ('update', ()))


class AttemptTests(unittest.TestCase):
    def test_missing_tool_limit_and_refusal_are_distinct_failures_without_retry(self):
        for payload, code in ((ToolModelResponse('complete', text='固定样例', finish_reason='stop'), 'tool_calls_missing'),
                (ToolModelResponse('incomplete', finish_reason='length'), 'output_limit'),
                (ToolModelResponse('incomplete', finish_reason='aborted'), 'incomplete_response'),
                (ToolModelResponse('refused', text='固定拒绝样例'), 'refused')):
            run, model = self.run_model(lambda *_: payload)
            self.assertEqual(run.status, 'failed')
            self.assertEqual(run.code, 'intent_v2_model_'+code)
            self.assertIsNone(run.intent)
            self.assertEqual(model.calls, 1)

    def run_model(self, action=None, ctx=None, control=None):
        class Fixed:
            calls = 0
            def complete(self, messages, *, tools):
                self.calls += 1
                validate_tool_messages(messages, tools=tools)
                if action:
                    return action(messages, tools)
                return response(model='fixed-returned-model', usage=TokenUsage(100, 20, 120))
        model = Fixed()
        run = understand('分析平安银行', model=model, context=ctx or context(), control=control)
        return run, model

    def test_one_attempt_single_system_message_and_returned_model_usage(self):
        def observe(messages, tools):
            self.assertEqual(sum(m.role == 'system' for m in messages), 1)
            self.assertNotIn('gold', ''.join(m.content or '' for m in messages))
            return response(model='fixed-returned-model', usage=TokenUsage(100, 20, 120))
        run, model = self.run_model(observe)
        self.assertEqual(run.status, 'ok', run)
        self.assertEqual(model.calls, 1)
        self.assertEqual(run.response.model, 'fixed-returned-model')
        self.assertEqual(run.runtime['model_calls_attempted'], 1)
        self.assertEqual(run.runtime['reported_usage']['total_tokens'], 120)
        self.assertEqual(run.runtime['analyses_used'], 0)

    def test_timeout_unavailable_and_malformed_are_not_success_or_retry(self):
        for error in ('timeout', 'connection_error', 'unavailable'):
            def fail(messages, tools):
                raise ModelCallError(error, 'sanitized fixed error')
            run, model = self.run_model(fail)
            self.assertEqual(run.status, 'failed')
            self.assertIsNone(run.intent)
            self.assertEqual(model.calls, 1)
        run, model = self.run_model(lambda *args: response(draft(confidence='low')))
        self.assertEqual(run.status, 'ok')
        self.assertEqual(run.intent.outcome, 'clarify_conditions')
        self.assertTrue(run.intent.low_confidence)

    def test_cancellation_and_deadline_discard_returned_interpretation(self):
        control = TurnControl(TurnLimits(model_calls=1))
        def cancelled(messages, tools):
            control.cancel()
            return response()
        run, _ = self.run_model(cancelled, control=control)
        self.assertEqual(run.code, 'turn_cancelled')
        self.assertIsNone(run.intent)
        tick = [0.0]
        control = TurnControl(TurnLimits(model_calls=1), clock=lambda: tick[0])
        def late(messages, tools):
            tick[0] = 91.0
            return response()
        run, _ = self.run_model(late, control=control)
        self.assertEqual(run.code, 'turn_deadline_exceeded')
        self.assertIsNone(run.intent)

    def test_more_than_one_attempt_budget_is_rejected(self):
        with self.assertRaises(ModelConfigurationError):
            self.run_model(control=TurnControl())

    def test_invalid_input_or_stale_directory_is_rejected_before_model_attempt(self):
        class Forbidden:
            def complete(self, *args, **kwargs):
                raise AssertionError('model must not be called')
        for question in ('', 'x'*2001, '分析\x00平安银行'):
            run = understand(question, model=Forbidden(), context=context())
            self.assertEqual(run.status, 'invalid')
            self.assertEqual(run.runtime['model_calls_attempted'], 0)
        stale = replace(context(), directory=SecurityDirectory(SECURITIES, 'stale', NOW-timedelta(days=8), False))
        run = understand('分析平安银行', model=Forbidden(), context=stale)
        self.assertEqual(run.status, 'invalid')
        self.assertEqual(run.runtime['model_calls_attempted'], 0)


if __name__ == '__main__':
    unittest.main()
