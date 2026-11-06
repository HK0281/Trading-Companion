"""Independent synthetic v2.6.3 state, identity and narrowing boundaries; no sealed inputs."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests
from test_research_intent_v26 import pending_context, previous_value, reference
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue


class V263Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def conflict_context(self):
        ctx = context(previous=True)
        ctx.session.product_pending = pending_context().session.product_pending
        ctx.session._pending = object()
        return ctx

    def test_conflict_precedes_continuation_references_but_not_a_checked_independent_task(self):
        ctx = self.conflict_context()
        proposals = [draft(task='clarify', clarification_reason='subject',
                          subject=dict(source='none', mentions=[], selection=None))]
        proposals += [previous_value(ref, task=task, clarification_reason='subject')
            for task in ('clarify', 'stock_research') for ref in
            (reference(), reference(scope='pending'), reference('none', scope=None), reference('ordinal', index=0))]
        for value in proposals:
            with self.subTest(subject=value['subject'], task=value['task']):
                before = deepcopy(value)
                result = self.check('继续吧', value, ctx)
                self.assertEqual((result.task, result.outcome, result.bound_subjects, result.subject_source),
                                 ('clarify', 'clarify_conflict', (), 'none'))
                self.assertFalse(result.wants_plan)
                self.assertEqual(value, before)
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None))
        result = self.check('研究浦发银行', value, ctx)
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                         ('question', ('600000.SH',), 'research'))
        value = previous_value(task='stock_research')
        result = self.check('研究浦发银行', value, ctx)
        self.assertEqual((result.bound_subjects, result.outcome), ((), 'clarify_conflict'))
        value = draft(subject=dict(source='question', mentions=[], selection=None))
        result = self.check('继续', value, ctx)
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome), ('none', (), 'clarify_conflict'))

    def test_conflicting_pending_source_requires_object_reference_not_a_horizon(self):
        ctx = self.conflict_context()
        for question, source in (('第一只', 'previous'), ('选它', 'previous'), ('就是那个', 'previous'),
                                 ('我选第一个', 'previous'), ('继续', 'none'), ('那就继续吧', 'none'),
                                 ('我准备做短期', 'none'), ('两个待办里我还没决定', 'none')):
            for proposed in ('none', 'previous'):
                value = draft(task='clarify', clarification_reason='subject',
                    subject=dict(source=proposed, mentions=[], selection=None))
                result = self.check(question, value, ctx)
                self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                                 (source, (), 'clarify_conflict'))
        value = previous_value(reference(scope='pending'), task='clarify', operation='update',
                               clarification_reason='conditions', horizon_expression='短期')
        result = self.check('我准备做短期', value, ctx)
        self.assertEqual((result.operation, result.horizon, result.subject_source, result.outcome),
                         ('update', '未来10个交易日', 'none', 'clarify_conflict'))
        self.assertEqual(result.diagnostics['reference'], reference('none', scope=None))

    def test_conflict_does_not_expand_local_grammar_or_mutate_pending(self):
        ctx = self.conflict_context()
        pending, product = ctx.session.pending, ctx.session.product_pending
        for question in ('取消研究。', '查看上次结果'):
            self.assertTrue(intent.local_preflight(question, ctx).zero_intent_calls)
        for question in ('继续', '我准备做短期', '第一只'):
            self.assertIsNone(intent.local_preflight(question, ctx))
        value = previous_value(reference('none', scope=None), task='clarify', clarification_reason='subject')
        raw = self.check('选它', value, ctx, normalize=False)
        self.assertEqual(raw.outcome, 'clarify_subject')
        self.assertIs(ctx.session.pending, pending)
        self.assertIs(ctx.session.product_pending, product)

    def test_known_unsupported_indices_are_rejected_without_identity_or_domain_execution(self):
        for name in ('中证500', '中证1000指数', '中证2000', '上证50', '上证180指数', '上证380',
                     '深证100', '北证50', '恒生指数', '恒生科技指数', '恐慌指数'):
            for task in ('index_research', 'clarify'):
                value = draft(task=task, clarification_reason='identity', confidence='low',
                    subject=dict(source='question', mentions=[name], selection=None),
                    research_kinds=[dict(kind='financial', phrase='财务')])
                result = self.check('研究'+name+'的财务', value)
                self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
        value = draft(task='index_research', subject=dict(source='question', mentions=['中证500'], selection=None))
        raw = self.check('研究中证500走势', value, normalize=False)
        self.assertEqual((raw.task, raw.outcome), ('index_research', 'clarify_identity'))

    def test_unknown_ambiguous_and_fund_indices_do_not_gain_supported_identity(self):
        for name in ('科创板指数', '示例成长指数'):
            result = self.check('研究'+name, draft(task='index_research',
                subject=dict(source='question', mentions=[name], selection=None)))
            self.assertEqual((result.task, result.outcome, result.bound_subjects), ('index_research', 'clarify_identity', ()))
        value = draft(task='unsupported', subject=dict(source='question', mentions=['中证500ETF'], selection=None))
        log = []
        result = self.check('研究中证500ETF', value, corrections=log)
        self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
        self.assertNotIn('known_index_unsupported', [c['kind'] for c in log])
        knowledge = draft(task='knowledge', subject=dict(source='none', mentions=[], selection=None))
        self.assertEqual(self.check('中证500指数是什么', knowledge).outcome, 'local')

    def test_unavailable_fact_preserves_checked_research_task_and_removes_future_horizon(self):
        for name, task, code in (('浦发银行', 'stock_research', '600000.SH'), ('沪深300', 'index_research', '000300.SH')):
            question = name+'未来五天的实际收盘价是多少'
            value = draft(task='clarify', clarification_reason='conditions', horizon_expression='未来五天',
                subject=dict(source='question', mentions=[name], selection=None))
            before, log = deepcopy(value), []
            result = self.check(question, value, corrections=log)
            self.assertEqual((result.task, result.objective_kind, result.wants_plan, result.horizon, result.outcome),
                             (task, 'analysis', False, None, 'clarify_conditions'))
            self.assertEqual(result.bound_subjects, (code,))
            self.assertEqual(result.historical_window, ())
            self.assertIn('research_task_preserved', [c['kind'] for c in log])
            self.assertEqual(value, before)
            self.assertEqual(self.check(question, value, normalize=False).task, 'clarify')

    def test_unfinished_today_bars_keep_explicit_technical_without_restoring_an_exclusion(self):
        for name, task in (('浦发银行', 'stock_research'), ('沪深300', 'index_research')):
            question = name+'今天完整日线的收盘价给我'
            value = draft(task='clarify', clarification_reason='conditions', date_expression='今天',
                subject=dict(source='question', mentions=[name], selection=None))
            result = self.check(question, value)
            self.assertEqual((result.task, result.date_expression, result.research_kinds, result.outcome),
                             (task, '今天', ('technical',), 'clarify_conditions'))
            self.assertEqual((result.horizon, result.wants_plan, result.historical_window), (None, False, ()))
            excluded = deepcopy(value)
            excluded['exclusions'] = [dict(kind='technical', phrase='不要技术面')]
            result = self.check(question+'，不要技术面', excluded)
            self.assertEqual(result.research_kinds, ())
            self.assertEqual(result.exclusions, ('technical',))
            result = self.check(question+'，不看技术面', value)
            self.assertEqual(result.research_kinds, ())
            restricted = dict(value, strict_scope=True, scope_phrase='只看风险',
                              research_kinds=[dict(kind='risk', phrase='风险')])
            result = self.check(question+'，只看风险', restricted)
            self.assertEqual(result.research_kinds, ('risk',))

    def test_unknown_identity_and_non_fact_clarification_never_acquire_a_task_from_history(self):
        value = draft(task='clarify', clarification_reason='conditions',
            subject=dict(source='question', mentions=['示例公司'], selection=None))
        result = self.check('示例公司今天完整日线', value, context(previous=True))
        self.assertEqual((result.task, result.bound_subjects, result.outcome), ('clarify', (), 'clarify_identity'))
        value = draft(task='clarify', clarification_reason='conditions',
                      subject=dict(source='none', mentions=[], selection=None))
        result = self.check('今天的完整日线', value, context(previous=True))
        self.assertEqual((result.task, result.bound_subjects), ('clarify', ()))

    def test_conditional_future_price_scenario_is_not_rejected_as_realized_fact(self):
        value = draft(objective_kind='decision', objective_phrase='未来五天', horizon_expression='未来五天',
            wants_plan=True, plan_phrase='止损参考价')
        result = self.check('平安银行未来五天若跌破支撑，止损参考价怎么设', value)
        self.assertEqual((result.objective_kind, result.wants_plan, result.outcome), ('decision', True, 'research'))

    def borrowed_value(self, **changes):
        return draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价', **changes)

    def test_changed_stock_revokes_old_evidence_and_keeps_current_horizon_scope_and_operation(self):
        for operation in ('new', 'update', 'refresh', 'explain'):
            value = self.borrowed_value(operation=operation, horizon_expression='未来16天', strict_scope=True,
                scope_phrase='只看财务', research_kinds=[dict(kind='financial', phrase='财务')])
            before, log = deepcopy(value), []
            result = self.check('未来16天，改看浦发银行并只看财务', value, pending_context(), corrections=log)
            self.assertEqual((result.objective_kind, result.wants_plan, result.operation, result.bound_subjects,
                              result.research_kinds, result.strict_scope, result.outcome),
                             ('decision', False, operation, ('600000.SH',), ('financial',), True, 'research'))
            self.assertEqual(result.horizon, '未来16天')
            self.assertEqual(result.diagnostics['authorization']['plan_source'], None)
            self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
            self.assertIn('changed_subject_evidence_revoked', [c['kind'] for c in log])
            self.assertEqual(value, before)
            with self.assertRaises(InputIssue):
                self.check('未来16天，改看浦发银行并只看财务', value, pending_context(), normalize=False)

    def test_changed_stock_without_horizon_is_analysis_and_never_recovers_model_false(self):
        for wants_plan in (True, False):
            value = self.borrowed_value()
            value['wants_plan'] = wants_plan
            result = self.check('换成浦发银行', value, pending_context())
            self.assertEqual((result.objective_kind, result.wants_plan, result.horizon, result.outcome),
                             ('analysis', False, None, 'research'))

    def test_current_stock_evidence_is_not_discarded_when_object_changes(self):
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            operation='update', objective_kind='decision', objective_phrase='未来五天', horizon_expression='未来五天',
            wants_plan=True, plan_phrase='买入价位')
        log = []
        result = self.check('改看浦发银行未来五天的买入价位', value, pending_context(), corrections=log)
        self.assertTrue(result.wants_plan)
        self.assertNotIn('changed_subject_evidence_revoked', [c['kind'] for c in log])

    def test_invented_stale_same_object_and_cross_task_evidence_remain_invalid(self):
        ctx = pending_context()
        stale = replace(ctx, now=ctx.now+timedelta(seconds=900))
        invented = self.borrowed_value()
        invented['objective_phrase'] = '凭空的预测'
        same = self.borrowed_value(operation='new')
        same['subject']['mentions'] = ['平安银行']
        cross = self.borrowed_value(task='index_research')
        cross['subject']['mentions'] = ['沪深300']
        for question, value, state in (
                ('换成浦发银行', invented, ctx), ('换成浦发银行', self.borrowed_value(), stale),
                ('换成浦发银行', self.borrowed_value(), context(previous=True)),
                ('研究平安银行', same, ctx), ('改看沪深300', cross, ctx)):
            with self.assertRaises(InputIssue) as caught:
                self.check(question, value, state)
            self.assertEqual(caught.exception.code, 'ungrounded_intent_v2')

    def test_unmarked_exclusions_are_not_removed_by_narrowing_or_conflict(self):
        for phrase in ('先放下个股', '指数先放一边'):
            value = self.borrowed_value(exclusions=[dict(kind='financial', phrase=phrase)])
            for ctx in (pending_context(), self.conflict_context()):
                with self.assertRaises(InputIssue) as caught:
                    self.check(phrase+'，换成浦发银行', value, ctx)
                self.assertEqual(caught.exception.code, 'ungrounded_exclusion')

    def test_operation_and_strict_scope_still_require_model_semantics(self):
        question = '浦发银行未来走势如何，用均线作分析'
        for operation in ('new', 'update', 'refresh', 'explain'):
            for strict in (True, False):
                value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
                    operation=operation, strict_scope=strict, scope_phrase='用均线' if strict else None)
                result = self.check(question, value)
                self.assertEqual((result.operation, result.strict_scope, result.objective_kind), (operation, strict, 'analysis'))
