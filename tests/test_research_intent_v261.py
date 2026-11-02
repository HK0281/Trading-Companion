"""Synthetic public-specification v2.6.1 reference and narrowing boundaries."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from scripts.verify import clean_environment, offline_network
from test_research_intent_v2 import context, draft, response
from test_research_intent_v26 import pending_context, previous_value, reference
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.workflow_evaluation_fixtures import SECURITIES


class V261Tests(unittest.TestCase):
    def setUp(self):
        self.enterContext(clean_environment())
        self.enterContext(offline_network())

    def check(self, value, ctx=None, question='它继续研究'):
        log = []
        result = intent.check_response(question, response(value), context=ctx or context(), corrections=log)
        return result, log

    def test_inconsistent_reference_with_no_scope_is_audited_unbound_clarification(self):
        for ref in (reference('none', scope=None), reference(), reference('all'),
                    reference('ordinal', index=99), reference('latest', index=1), reference('ordinal', index=-1)):
            with self.subTest(ref=ref):
                value = previous_value(ref)
                value['subject']['selection'] = '它'
                before = deepcopy(value)
                result, log = self.check(value)
                self.assertEqual((result.task, result.subject_source, result.outcome, result.bound_subjects),
                                 ('clarify', 'none', 'clarify_subject', ()))
                self.assertIn('previous_without_record', [c['kind'] for c in log])
                self.assertEqual(result.diagnostics['reference'], ref)
                self.assertEqual(value, before)
                raw = intent.check_response('它继续研究', response(value), context=context(), normalize=False)
                self.assertEqual(raw.subject_source, 'previous')
        self.assertEqual(intent._reference_scope(context(previous=True), None), ((), None, 'clarify_subject'))

    def test_ambiguous_available_scope_is_not_misreported_as_absent_or_used_as_fallback(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for ref in (reference(), reference('none', scope=None), reference('ordinal', index=3),
                    reference(scope='pending'), reference(scope='shortlist')):
            result, log = self.check(previous_value(ref), ctx)
            self.assertEqual((result.subject_source, result.outcome, result.bound_subjects),
                             ('previous', 'clarify_subject', ()))
            self.assertNotIn('previous_without_record', [c['kind'] for c in log])

    def test_explicit_mentions_and_reference_must_agree_without_selection_reinterpretation(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for index, name, expected in ((1, '平安银行', ('000001.SZ',)), (2, '平安银行', ()),
                                     (3, '平安银行', ()), (2, '浦发银行', ('600000.SH',))):
            value = draft(reference=reference('ordinal', index=index),
                          subject=dict(source='question', mentions=[name], selection=None))
            result, _ = self.check(value, ctx, '研究'+name)
            self.assertEqual(result.bound_subjects, expected)
            self.assertEqual(result.outcome, 'research' if expected else 'clarify_subject')
        value = previous_value(reference('ordinal', index=2))
        value['subject']['selection'] = '第一只'
        result, _ = self.check(value, ctx, '第一只继续研究')
        self.assertEqual(result.bound_subjects, ('600000.SH',))
        value = draft(reference=reference('ordinal', index=1),
                      subject=dict(source='question', mentions=['平安银行'], selection='第一只'))
        result, _ = self.check(value, ctx, '第一只平安银行继续研究')
        self.assertEqual(result.bound_subjects, ('000001.SZ',))

    def test_structural_errors_still_fail_without_nullable_reference_repair(self):
        refs = [reference('ordinal', index=v) for v in (True, False, 1.0, '1', 'null')]
        refs += [reference('unknown'), reference(scope='chat'), dict(reference(), extra=True)]
        for ref in refs:
            with self.subTest(ref=ref), self.assertRaises(InputIssue):
                self.check(previous_value(ref))
        value = previous_value()
        del value['subject']['reference']
        with self.assertRaises(InputIssue):
            self.check(value)

    def test_identity_and_conflict_preserve_task_without_recovering_research(self):
        for name, reason, phrase in (('平安', 'identity', '主营业务'), ('平安银行', 'conflict', '买入价位')):
            question = name+'的'+phrase+('，不需要价位' if reason == 'conflict' else '')
            value = draft(task='clarify', subject=dict(source='question', mentions=[name], selection=None),
                          clarification_reason=reason, confidence='low')
            if reason == 'conflict':
                value['exclusions'] = [dict(kind='plan', phrase='不需要价位')]
            result, _ = self.check(value, question=question)
            self.assertEqual((result.task, result.outcome, result.wants_plan),
                             ('stock_research', 'clarify_'+reason, False))
            raw = intent.check_response(question, response(value), context=context(), normalize=False)
            self.assertEqual(raw.task, 'clarify')
        value = draft(task='clarify', clarification_reason='subject')
        result, _ = self.check(value, question='研究平安银行')
        self.assertEqual((result.task, result.outcome), ('clarify', 'clarify_subject'))

    def test_product_rejection_retains_model_goal_without_binding_the_tracked_index(self):
        value = draft(task='unsupported', objective_kind='decision', objective_phrase='购买参考', wants_plan=True,
                      plan_phrase='购买参考', subject=dict(source='question', mentions=['沪深300ETF'], selection=None))
        result, _ = self.check(value, question='沪深300ETF购买参考')
        self.assertEqual((result.outcome, result.objective_kind, result.wants_plan, result.bound_subjects),
                         ('reject', 'decision', True, ()))

    def test_prompt_distinctions_do_not_expand_local_grammar_or_rewrite_operation(self):
        ctx = replace(context(previous=True), history=('刚才发布了评级',))
        for question in ('你刚才给的评级是什么', '之前说偏多还是偏空'):
            self.assertIsNone(intent.local_preflight(question, ctx))
            value = draft(task='recall', subject=dict(source='none', mentions=[], selection=None))
            result, _ = self.check(value, ctx, question)
            self.assertEqual((result.task, result.operation, result.outcome, result.subject_source),
                             ('recall', 'new', 'local', 'none'))
        for operation in ('new', 'update', 'refresh', 'explain'):
            value = previous_value(operation=operation)
            result, _ = self.check(value, ctx, '沿用它，再新增一个领域')
            self.assertEqual(result.operation, operation)
        model = Mock()
        model.complete.return_value = response()
        # Exercise the actual protocol, including a full-length target question.
        ctx = context()
        question = '平安银行'+('x'*1996)
        run = intent.understand(question, model=model, context=ctx)
        self.assertEqual(run.status, 'ok')
        self.assertEqual(model.complete.call_args.args[0][1].content, question)
        oversized = replace(ctx, history=tuple('普通历史'*400 for _ in range(6)))
        before = model.complete.call_count
        rejected = intent.understand(question, model=model, context=oversized)
        self.assertEqual((rejected.status, rejected.code), ('failed', 'intent_v2_model_request'))
        self.assertEqual(model.complete.call_count, before)

    def test_user_approved_price_markers_and_rating_exception(self):
        for phrase in ('能建仓吗', '给几个买入价位', '止损位在哪', '值得买吗', '我要减仓',
                       '给评级及目标价', '评级附参考计划', '方向及退出价', '计划', '目标'):
            with self.subTest(phrase=phrase):
                value = draft(wants_plan=True, plan_phrase=phrase)
                checked, _ = self.check(value, question='平安银行'+phrase)
                self.assertTrue(checked.wants_plan)
                self.assertEqual(checked.diagnostics['authorization']['plan_source'], 'question')
                self.assertEqual(checked.outcome, 'clarify_horizon')
        for phrase in ('给个买卖评级', '买卖评级', '买卖方向', '分析一下', '成交量', '评级', '入', '出', '止'):
            with self.subTest(phrase=phrase), self.assertRaises(InputIssue) as caught:
                self.check(draft(wants_plan=True, plan_phrase=phrase), question='平安银行'+phrase)
            self.assertEqual(caught.exception.code, 'ungrounded_plan_authorization')

    def test_rating_exception_ignores_common_verbs_and_requires_price_markers(self):
        for phrase, expected in (('给出买卖评级', False), ('请给出一个买入评级', False),
                ('说出你的买卖方向', False), ('给个买卖评级', False), ('评级及目标价', True),
                ('能建仓吗', True), ('给出入场价', True)):
            with self.subTest(phrase=phrase):
                self.assertEqual(intent._positive_price_evidence(phrase, phrase), expected)
                value = draft(wants_plan=True, plan_phrase=phrase)
                if expected:
                    checked, _ = self.check(value, question='平安银行'+phrase)
                    self.assertTrue(checked.wants_plan)
                else:
                    with self.assertRaises(InputIssue) as caught:
                        self.check(value, question='平安银行'+phrase)
                    self.assertEqual(caught.exception.code, 'ungrounded_plan_authorization')

    def test_markers_must_belong_to_claimed_phrase_and_cannot_restore_a_false_request(self):
        for phrase in ('分析一下', '成交量', '给个买卖评级'):
            with self.subTest(phrase=phrase), self.assertRaises(InputIssue):
                self.check(draft(wants_plan=True, plan_phrase=phrase),
                           question='平安银行'+phrase+'，再给几个买入价位')
        for phrase in ('能建仓吗', '给几个买入价位', '止损位在哪'):
            value = draft(wants_plan=False, plan_phrase=phrase)
            checked, log = self.check(value, question='平安银行'+phrase)
            self.assertFalse(checked.wants_plan)
            self.assertIn('unused_plan_phrase', [entry['kind'] for entry in log])
        with self.assertRaises(InputIssue):
            self.check(draft(wants_plan=True, plan_phrase='能建仓吗'), question='平安银行分析一下')

    def test_price_markers_do_not_bypass_veto_dependencies_or_pending_ownership(self):
        for exclusion in ([], [dict(kind='plan', phrase='不要价位')]):
            value = draft(wants_plan=True, plan_phrase='不要价位', exclusions=exclusion)
            checked, _ = self.check(value, question='平安银行不要价位')
            self.assertFalse(checked.wants_plan)
            self.assertEqual((checked.outcome, checked.plan_blocked), ('clarify_conflict', 'plan_veto'))
        for phrase, kind in (('不要风险', 'risk'), ('不用ATR', 'indicator:atr'), ('不看技术面', 'technical')):
            value = draft(wants_plan=True, plan_phrase='能建仓吗', horizon_expression='中线',
                          exclusions=[dict(kind=kind, phrase=phrase)])
            checked, _ = self.check(value, question='平安银行中线能建仓吗，'+phrase)
            self.assertEqual(checked.plan_blocked, 'risk_atr_not_authorized')
        # Ordinary history/recent identity cannot lend old marker-bearing text.
        ctx = replace(context(previous=True), history=('平安银行能建仓吗',))
        value = previous_value(wants_plan=True, plan_phrase='能建仓吗')
        with self.assertRaises(InputIssue):
            self.check(value, ctx, '继续分析')
        ctx = pending_context()
        continued = previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
                                   wants_plan=True, horizon_expression='中线')
        checked, _ = self.check(continued, ctx, '中线')
        self.assertTrue(checked.wants_plan)
        self.assertEqual(checked.diagnostics['authorization']['plan_source'], 'program_pending')
        for value, question in ((dict(continued, operation='new'), '中线'),
                (draft(operation='update', wants_plan=True, plan_phrase='参考价',
                    subject=dict(source='question', mentions=['浦发银行'], selection=None)), '研究浦发银行')):
            with self.assertRaises(InputIssue):
                self.check(value, ctx, question)


if __name__ == '__main__':
    unittest.main()
