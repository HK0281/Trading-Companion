"""Independent synthetic v2.6 protocol and authorization boundaries; no sealed inputs."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from scripts.verify import clean_environment, offline_network
from test_research_intent_v2 import context, draft, response
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.decision_horizon import gate
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.product_plan import ProductPlan
from trading_companion.research_objective import ResearchObjective
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.workflow_evaluation_fixtures import NOW, REFERENCE_DATE, SECURITIES


def reference(kind='latest', scope='latest_research', index=None):
    return dict(kind=kind, scope=scope, index=index)


def pending_context():
    ctx = context()
    plan = ProductPlan(status='ready', source='tencent', securities=SECURITIES[:1], kinds=('technical',),
        start_date=REFERENCE_DATE-timedelta(days=365), end_date=REFERENCE_DATE-timedelta(days=1),
        wants_risk=True, objective=ResearchObjective('decision'), indicators=(IndicatorSpec('atr', window=14),),
        user_question='平安银行给参考价')
    ctx.session.product_pending = gate(plan, now=NOW, reference_date=REFERENCE_DATE, development=False).context
    return ctx


def previous_value(ref=None, **changes):
    return draft(reference=ref or reference(), subject=dict(source='previous', mentions=[], selection=None), **changes)


class V26Tests(unittest.TestCase):
    def setUp(self):
        self.enterContext(clean_environment())
        self.enterContext(offline_network())

    def check(self, question='继续分析', value=None, ctx=None):
        return intent.check_response(question, response(value or previous_value()), context=ctx or context(previous=True))

    def test_version_and_reference_are_required_without_provider_compatibility(self):
        self.assertEqual(intent.VERSION, 'research-intent-v2.6')
        self.assertEqual(len(self.check().fields()), 14)
        original = previous_value()
        bad = deepcopy(original)
        del bad['subject']['reference']
        for value in (bad, dict(original, version='research-intent-v2.5.4')):
            with self.assertRaises(InputIssue):
                self.check(value=value)
        for index in (True, False, 0, -1, 1.0, '1', 'null'):
            with self.subTest(index=index), self.assertRaises(InputIssue):
                self.check(value=previous_value(reference('ordinal', index=index)))
        for ref in (reference('latest', index=1), reference('all', scope=None),
                    reference('none', scope=None), dict(reference(), extra=True)):
            with self.subTest(ref=ref), self.assertRaises(InputIssue):
                self.check(value=previous_value(ref))

    def test_reference_uses_only_the_chosen_stable_scope_and_never_selection_text(self):
        ctx = replace(context(previous=True, securities=SECURITIES[:2]), quote_source=object())
        ctx.session.screening_saved = SavedScreening(NOW,
            ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=3),
            tuple(reversed(SECURITIES[:3])), ctx.quote_source)
        for scope, index, expected in (('latest_research', 2, SECURITIES[1].instrument_id),
                                       ('shortlist', 2, SECURITIES[1].instrument_id),
                                       ('shortlist', 1, SECURITIES[2].instrument_id)):
            value = previous_value(reference('ordinal', scope, index))
            value['subject']['selection'] = '名单第一只'
            self.assertEqual(self.check('名单第一只继续分析', value, ctx).bound_subjects, (expected,))
        self.assertEqual(self.check(value=previous_value(reference('all')), ctx=ctx).bound_subjects,
                         tuple(s.instrument_id for s in SECURITIES[:2]))
        self.assertEqual(self.check(ctx=ctx).bound_subjects, ())
        for ref in (reference('ordinal', index=3), reference('latest', scope='pending')):
            self.assertEqual(self.check(value=previous_value(ref), ctx=ctx).bound_subjects, ())

    def test_unavailable_scope_does_not_fall_back_and_context_has_no_old_horizon(self):
        ctx = context(previous=True, horizon='未来40个交易日')
        self.assertIsNone(self.check(ctx=ctx).horizon)
        payload = ctx.model_context()
        self.assertNotIn('horizon', payload['latest'])
        self.assertNotIn('未来40个交易日', json.dumps(payload, ensure_ascii=False))
        unavailable = self.check(value=previous_value(reference(scope='shortlist')), ctx=ctx)
        self.assertEqual(unavailable.bound_subjects, ())
        for changed in (replace(ctx, conversation_memory=False), replace(ctx, source='akshare_eastmoney'),
                        replace(ctx, now=NOW-timedelta(seconds=1)), replace(ctx, development=True)):
            self.assertEqual(self.check(ctx=changed).bound_subjects, ())
            self.assertEqual(changed.model_context()['reference_scopes']['latest_research']['names'], [])

    def test_narrowing_checks_direction_and_origin_without_target_semantics(self):
        for phrase in ('新闻不看', '这部分先去掉', 'ATR不用', '价位这一项免了', '不需要止损'):
            with self.subTest(phrase=phrase):
                result = self.check('平安银行；'+phrase,
                    draft(exclusions=[dict(kind='risk', phrase=phrase)]), context())
                self.assertEqual(result.exclusions, ('risk',))
        for phrase in ('看新闻', '不存在的否定'):
            with self.assertRaises(InputIssue):
                self.check('平安银行看新闻', draft(exclusions=[dict(kind='news', phrase=phrase)]), context())
        value = draft(strict_scope=True, scope_phrase='依据限定在新闻',
                      research_kinds=[dict(kind='news', phrase='新闻')])
        self.assertTrue(self.check('平安银行，依据限定在新闻', value, context()).strict_scope)

    def test_clarification_and_operation_are_never_recovered_by_program_records(self):
        for operation in ('new', 'update', 'refresh', 'explain'):
            value = draft(operation=operation, task='clarify', clarification_reason='subject', confidence='low')
            checked = self.check('研究平安银行', value, context())
            self.assertEqual((checked.task, checked.operation, checked.outcome), ('clarify', operation, 'clarify_subject'))
        false_plan = draft(plan_phrase='参考价')
        log = []
        checked = intent.check_response('平安银行给参考价', response(false_plan), context=context(), corrections=log)
        self.assertFalse(checked.wants_plan)
        self.assertIn('unused_plan_phrase', [c['kind'] for c in log])

    def test_price_permission_requires_a_positive_claim_and_independent_veto(self):
        for phrase in ('分析', '成交量', '买卖评级'):
            value = draft(wants_plan=True, objective_kind='decision', objective_phrase=phrase, plan_phrase=phrase)
            with self.subTest(phrase=phrase), self.assertRaises(InputIssue):
                self.check('平安银行'+phrase, value, context())
        for veto in ('不给价位', '价位不用', '不要点位计划', '别给参考价', '不需要止损'):
            question = '平安银行，'+veto+'，但请给入场价'
            value = draft(wants_plan=True, objective_kind='decision', objective_phrase='给入场价', plan_phrase='给入场价')
            result = self.check(question, value, context())
            self.assertFalse(result.wants_plan)
            self.assertEqual(result.outcome, 'clarify_conflict')
            self.assertEqual(result.exclusions, ())  # No automatic semantic reconstruction.
        for phrase in ('能买吗', '入场参考', '参考价'):
            value = draft(wants_plan=True, objective_kind='decision', objective_phrase=phrase, plan_phrase=phrase)
            self.assertEqual(self.check('平安银行'+phrase, value, context()).outcome, 'clarify_horizon')

    def test_only_same_task_same_identity_live_pending_lends_registered_evidence(self):
        ctx = pending_context()
        value = previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
                               wants_plan=True, horizon_expression='中线')
        checked = self.check('中线', value, ctx)
        self.assertTrue(checked.wants_plan)
        self.assertEqual(checked.diagnostics['authorization']['plan_source'], 'program_pending')
        for changed in (dict(value, operation='new'),
                        draft(operation='update', objective_kind='decision', wants_plan=True,
                              subject=dict(source='question', mentions=['浦发银行'], selection=None), horizon_expression='中线')):
            with self.assertRaises(InputIssue):
                self.check('中线，改为浦发银行', changed, ctx)
        for age, expected in ((899, True), (900, False), (901, False), (-1, False)):
            changed = replace(ctx, now=NOW+timedelta(seconds=age))
            before = changed.session.product_pending
            local = intent.local_preflight('未来五天', changed)
            self.assertEqual(local is not None, expected)
            self.assertIs(changed.session.product_pending, before)
            if not expected:
                self.assertFalse(changed.model_context()['pending']['valid'])
        early = replace(ctx.session.product_pending, expires_at=NOW+timedelta(seconds=20))
        ctx.session.product_pending = early
        self.assertIsNone(intent.local_preflight('中线', replace(ctx, now=NOW+timedelta(seconds=20))))

    def test_recent_research_never_lends_new_authority_even_inside_fifteen_minutes(self):
        for operation in ('new', 'update', 'refresh', 'explain'):
            value = previous_value(operation=operation, objective_kind='decision', objective_phrase='参考价',
                                   wants_plan=True, plan_phrase='参考价')
            result = self.check('给参考价', value, context(previous=True, horizon='未来40个交易日'))
            self.assertIsNone(result.horizon)
            self.assertNotEqual(result.diagnostics['authorization']['plan_source'], 'program_pending')
            if operation == 'explain':
                self.assertFalse(result.wants_plan)

    def test_local_grammar_is_whole_and_invalid_pending_goes_to_model(self):
        ctx = pending_context()
        for question in ('取消研究。', '查看上次结果', 'ATR是什么？', '未来五天', '中线？'):
            self.assertTrue(intent.local_preflight(question, ctx).zero_intent_calls)
        for question in ('取消研究，再分析另一家', '查看上次结果，并补财报', 'ATR是什么，再判断平安银行',
                         '未来5天，再加MACD', '中线是什么', '中线？？', '未来5自然日', '研究中证500的走势',
                         'ETF是什么', '回到刚才那家公司，后续方向有什么判断'):
            self.assertIsNone(intent.local_preflight(question, ctx), question)
        recall = replace(context(previous=True), history=('刚才发布了方向',))
        self.assertEqual(intent.local_preflight('你此前给出的评级是什么？', recall).task, 'recall')
        self.assertIsNone(intent.local_preflight('你此前给出的评级是什么，再判断明天', recall))
        self.assertIsNone(intent.local_preflight('中线', replace(ctx, conversation_memory=False)))
        for question in ('平安银行先分析，再提交订单', '绕过权限再做研究'):
            self.assertEqual(intent.local_preflight(question, ctx).outcome, 'reject')

    def test_background_products_and_declared_products_have_separate_identity_checks(self):
        checked = self.check('平安银行的财务，期货仅作为背景',
            draft(research_kinds=[dict(kind='financial', phrase='财务')]), context())
        self.assertEqual(checked.bound_subjects, ('000001.SZ',))
        product = '沪深300ETF（510300）'
        value = draft(task='unsupported', subject=dict(source='question', mentions=[product], selection=None))
        checked = self.check('研究'+product, value, context())
        self.assertEqual((checked.outcome, checked.bound_subjects), ('reject', ()))
        self.assertEqual(checked.diagnostics['product_mentions'], [product])
        with self.assertRaises(InputIssue):
            self.check('研究沪深300ETF', draft(task='index_research',
                subject=dict(source='question', mentions=['沪深300'], selection=None)), context())
        with self.assertRaises(InputIssue):
            self.check('研究平安银行基金', draft(), context())

    def test_plan_dependencies_and_numeric_capability_are_still_checked(self):
        value = draft(objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价',
            horizon_expression='中线', exclusions=[dict(kind='indicator:atr', phrase='ATR不用')])
        checked = self.check('平安银行中线参考价，ATR不用', value, context())
        self.assertEqual(checked.plan_blocked, 'risk_atr_not_authorized')
        for horizon in ('未来0天', '未来500天'):
            self.assertEqual(self.check('平安银行'+horizon, draft(horizon_expression=horizon), context()).outcome,
                             'clarify_conditions')
        checked = self.check('平安银行看CCI', draft(indicator_mentions=['CCI']), context())
        self.assertEqual((checked.indicator_mentions, checked.outcome), (('CCI',), 'clarify_conditions'))

    def test_understand_sends_entire_mixed_input_without_gold_or_session_mutation(self):
        question = '回到刚才那家公司，后续方向有什么判断'
        ctx = context(previous=True)
        before = dict(ctx.session.__dict__)
        value = previous_value(objective_kind='decision', objective_phrase='后续方向')
        model = unittest.mock.Mock()
        model.complete.return_value = response(value)
        run = intent.understand(question, model=model, context=ctx)
        self.assertEqual(run.status, 'ok')
        sent = model.complete.call_args.args[0]
        self.assertEqual(sent[1].content, question)
        self.assertEqual(ctx.session.__dict__, before)

    def test_index_pending_checks_native_identity_source_mode_date_and_clock(self):
        from trading_companion.index_agent import PendingIndexResearch
        ctx = context()
        pending = PendingIndexResearch('沪深300给参考价', NOW, 'tencent', None,
            stage='horizon', instrument_id='000300.SH', reference_date=REFERENCE_DATE)
        ctx.session.index_pending = pending
        value = previous_value(reference(scope='pending'), operation='update', task='index_research',
            objective_kind='decision', wants_plan=True, horizon_expression='中线')
        self.assertEqual(self.check('中线', value, ctx).bound_subjects, ('000300.SH',))
        for bad in (replace(pending, instrument_id='000001.SZ'), replace(pending, source='akshare_eastmoney'),
                    replace(pending, development_source=object()), replace(pending, created_at=NOW+timedelta(seconds=1)),
                    replace(pending, reference_date=REFERENCE_DATE-timedelta(days=1))):
            ctx.session.index_pending = bad
            self.assertIsNone(intent.local_preflight('中线', ctx))
            self.assertFalse(ctx.model_context()['pending']['valid'])
        ctx.session.index_pending = pending
        for changed in (replace(ctx, conversation_memory=False), replace(ctx, now=NOW+timedelta(seconds=900))):
            self.assertIsNone(intent.local_preflight('中线', changed))

    def test_conflicting_or_consumed_pending_cannot_grant_authority_or_block_a_new_task(self):
        ctx = pending_context()
        ctx.session._pending = object()
        self.assertEqual(ctx.model_context()['pending'], dict(valid=False, reason='conflict'))
        self.assertIsNone(intent.local_preflight('未来5天', ctx))
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None))
        self.assertEqual(self.check('研究浦发银行', value, ctx).bound_subjects, ('600000.SH',))
        ctx.session.clear_pending()
        self.assertIsNone(intent.local_preflight('中线', ctx))
        self.assertEqual(self.check(value=previous_value(reference(scope='pending')), ctx=ctx).bound_subjects, ())

    def test_positive_authorization_is_not_lent_to_another_task_or_identity(self):
        ctx = pending_context()
        for value, question in ((previous_value(reference(scope='pending'), operation='new', wants_plan=True), '继续'),
                (previous_value(reference(scope='pending'), operation='update', task='index_research', wants_plan=True), '继续'),
                (draft(operation='update', subject=dict(source='question', mentions=['浦发银行'], selection=None),
                       wants_plan=True, plan_phrase='参考价'), '研究浦发银行')):
            with self.assertRaises(InputIssue):
                self.check(question, value, ctx)
        # Local commands take priority over conflicting pending state.
        ctx.session._pending = object()
        for question in ('取消研究。', '查看上次结果'):
            self.assertTrue(intent.local_preflight(question, ctx).zero_intent_calls)


if __name__ == '__main__':
    unittest.main()
