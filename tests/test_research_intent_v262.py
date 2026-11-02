"""Synthetic v2.6.2 record existence, recall isolation and expired-pending boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from scripts.verify import clean_environment, offline_network
from test_research_intent_v2 import context, draft, response
from test_research_intent_v26 import pending_context, previous_value, reference
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.workflow_evaluation_fixtures import NOW, SECURITIES


class V262Tests(unittest.TestCase):
    def setUp(self):
        self.enterContext(clean_environment())
        self.enterContext(offline_network())

    def check(self, value=None, ctx=None, question='继续研究它'):
        log = []
        result = intent.check_response(question, response(value or previous_value()),
                                       context=ctx or context(), corrections=log)
        return result, log

    def test_only_complete_absence_normalizes_previous_to_none(self):
        empty = replace(context(), history=('之前谈过平安银行',))
        result, log = self.check(ctx=empty)
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                         ('none', (), 'clarify_subject'))
        self.assertIn('previous_without_record', [c['kind'] for c in log])
        self.assertFalse(any(empty.model_context()['reference_records'].values()))
        for name in ('_pending', '_pending_edit', 'product_pending', 'index_pending'):
            with self.subTest(name=name):
                ctx = context()
                setattr(ctx.session, name, object())
                result, log = self.check(ctx=ctx)
                self.assertEqual((result.subject_source, result.bound_subjects), ('previous', ()))
                self.assertNotIn('previous_without_record', [c['kind'] for c in log])
                self.assertTrue(ctx.model_context()['reference_records']['pending'])

    def test_invalid_latest_records_never_grant_identity_or_disappear(self):
        consumed = context(previous=True)
        consumed.session._subject_current = False
        variants = (context(previous=True, age=-1), consumed,
                    context(previous=True, source='akshare_eastmoney'), context(previous=True, development=True),
                    context(previous=True, conversation_memory=False))
        for ctx in variants:
            with self.subTest(ctx=ctx):
                before = ctx.session.latest_research
                result, log = self.check(ctx=ctx)
                self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                                 ('previous', (), 'clarify_subject'))
                self.assertNotIn('previous_without_record', [c['kind'] for c in log])
                self.assertIs(ctx.session.latest_research, before)
                payload = ctx.model_context()
                self.assertTrue(payload['reference_records']['latest_research'])
                self.assertFalse(payload['reference_scopes']['latest_research']['available'])
                self.assertEqual(payload['reference_scopes']['latest_research']['names'], [])

    def test_expired_or_empty_shortlist_counts_as_record_without_fallback(self):
        for securities in ((), SECURITIES[:2]):
            ctx = replace(context(), quote_source=object())
            ctx.session.screening_saved = SavedScreening(NOW-timedelta(seconds=900),
                ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=3), securities, ctx.quote_source)
            result, log = self.check(previous_value(reference('all', scope='shortlist')), ctx)
            self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                             ('previous', (), 'clarify_subject'))
            self.assertNotIn('previous_without_record', [c['kind'] for c in log])
            self.assertTrue(ctx.model_context()['reference_records']['shortlist'])

    def test_pending_expiration_preserves_current_horizon_and_conditions_issue(self):
        for age in (900, 901, -1):
            for phrase, horizon in (('短线', '未来10个交易日'), ('中线', '未来40个交易日')):
                with self.subTest(age=age, phrase=phrase):
                    ctx = replace(pending_context(), now=NOW+timedelta(seconds=age))
                    before = ctx.session.product_pending
                    value = previous_value(reference(scope='pending'), task='clarify', operation='update',
                        horizon_expression=phrase, clarification_reason='conditions', confidence='low')
                    self.assertIsNone(intent.local_preflight(phrase+'？', ctx))
                    result, log = self.check(value, ctx, phrase+'？')
                    self.assertEqual((result.task, result.operation, result.objective_kind, result.wants_plan,
                                      result.subject_source, result.bound_subjects, result.horizon, result.outcome),
                                     ('clarify', 'update', 'analysis', False, 'previous', (), horizon, 'clarify_conditions'))
                    self.assertNotIn('previous_without_record', [c['kind'] for c in log])
                    self.assertIs(ctx.session.product_pending, before)
                    pending = ctx.model_context()['pending']
                    self.assertEqual(pending, dict(valid=False, reason='conditions', stage='horizon'))
                    self.assertNotIn('original_question', pending)
                    self.assertNotIn('inheritable_fields', pending)

    def test_semantic_recall_removes_previous_or_explicit_identity_with_audit(self):
        for ctx in (context(), context(previous=True), context(previous=True, source='akshare_eastmoney'),
                    context(previous=True, securities=SECURITIES[:2]), pending_context()):
            for subject in (dict(source='previous', mentions=[], selection=None),
                            dict(source='question', mentions=['平安银行'], selection=None)):
                value = draft(task='recall', subject=subject)
                before = deepcopy(value)
                result, log = self.check(value, ctx, '你刚才给平安银行的评级是什么')
                self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.outcome),
                                 ('recall', 'none', (), 'local'))
                self.assertEqual(result.diagnostics['binding']['scope_subjects'], [])
                self.assertEqual(result.diagnostics['binding']['issue'], None)
                self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
                self.assertIn('recall_no_binding', [c['kind'] for c in log])
                self.assertEqual(value, before)
        raw_value = previous_value(task='recall')
        raw = intent.check_response('刚才的评级是什么', response(raw_value), context=context(previous=True), normalize=False)
        self.assertEqual((raw.subject_source, raw.bound_subjects), ('previous', ('000001.SZ',)))

    def test_local_recall_remains_unbound_and_does_not_expand_grammar(self):
        ctx = replace(context(previous=True), history=('已发布评级',))
        result = intent.local_preflight('你刚才给出的评级是什么？', ctx)
        self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.zero_intent_calls),
                         ('recall', 'none', (), True))
        self.assertIsNone(intent.local_preflight('你刚才给的评级是什么？', ctx))

    def test_invalid_pending_and_changed_identity_never_lend_old_phrases(self):
        for ctx, question, value in (
            (replace(pending_context(), now=NOW+timedelta(seconds=900)), '中线',
             previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
                            objective_phrase='参考价', wants_plan=True, plan_phrase='参考价', horizon_expression='中线')),
            (pending_context(), '短线，换成浦发银行',
             draft(operation='update', objective_kind='decision', objective_phrase='参考价', wants_plan=True,
                   plan_phrase='参考价', horizon_expression='短线',
                   subject=dict(source='question', mentions=['浦发银行'], selection=None)))):
            with self.assertRaises(InputIssue) as caught:
                self.check(value, ctx, question)
            self.assertEqual(caught.exception.code, 'ungrounded_intent_v2')

    def test_operation_and_strict_scope_are_not_keyword_rewritten(self):
        question = '单独研究平安银行的财务，财务部分用更新的数据重算'
        for operation in ('new', 'update', 'refresh', 'explain'):
            for strict in (True, False):
                value = draft(operation=operation, strict_scope=strict, scope_phrase='财务部分' if strict else None,
                              research_kinds=[dict(kind='financial', phrase='财务')])
                result, _ = self.check(value, question=question)
                self.assertEqual((result.operation, result.strict_scope), (operation, strict))

    def test_screening_references_remain_invalid_until_model_uses_task_scope(self):
        with self.assertRaises(InputIssue) as caught:
            self.check(previous_value(reference('all', scope='shortlist'), task='screening'),
                       ctx=context(previous=True), question='把名单数量改为五只')
        self.assertEqual(caught.exception.code, 'task_fields_conflict')
        value = draft(task='screening', operation='update', subject=dict(source='none', mentions=[], selection=None))
        result, _ = self.check(value, question='把名单数量改为五只')
        self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.operation, result.outcome),
                         ('screening', 'none', (), 'update', 'research'))

    def test_exit_synonym_authorizes_prices_but_not_ratings(self):
        for phrase, allowed in (('在哪里退出合适', True), ('退出评级', False)):
            with self.subTest(phrase=phrase):
                self.assertEqual(intent._positive_price_evidence(phrase, phrase), allowed)
                value = draft(wants_plan=True, plan_phrase=phrase)
                if allowed:
                    result, _ = self.check(value, question='平安银行'+phrase)
                    self.assertTrue(result.wants_plan)
                    self.assertEqual(result.outcome, 'clarify_horizon')
                else:
                    with self.assertRaises(InputIssue) as caught:
                        self.check(value, question='平安银行'+phrase)
                    self.assertEqual(caught.exception.code, 'ungrounded_plan_authorization')
        result, _ = self.check(draft(wants_plan=True, plan_phrase='在哪里退出合适'),
                               question='平安银行在哪里退出合适，不要价位')
        self.assertEqual((result.wants_plan, result.outcome), (False, 'clarify_conflict'))
        result, _ = self.check(draft(wants_plan=False, plan_phrase='在哪里退出合适'),
                               question='平安银行在哪里退出合适')
        self.assertFalse(result.wants_plan)


if __name__ == '__main__':
    unittest.main()
