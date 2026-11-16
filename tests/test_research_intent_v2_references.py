"""Research intent v2 references, pending state, revocation and authorization boundaries (formerly v2.6-v2.6.8 files).

Synthetic inputs only; no provider calls or sealed data."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import unicodedata
from unittest.mock import Mock
import unittest

from test_research_intent_v2 import context, draft, response, OfflineTests, measure_request
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.decision_horizon import gate
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.product_agent import SavedProductResearch
from trading_companion.product_plan import ProductPlan
from trading_companion.research_objective import ResearchObjective
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import ToolMessage
from trading_companion.workflow_evaluation_fixtures import NOW, REFERENCE_DATE, SECURITIES


# ---- v2.6: Independent synthetic v2.6 protocol and authorization boundaries; no sealed inputs.

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


class V26Tests(OfflineTests):
    def check(self, question='继续分析', value=None, ctx=None):
        return intent.check_response(question, response(value or previous_value()), context=ctx or context(previous=True))

    def test_version_and_reference_are_required_without_provider_compatibility(self):
        self.assertEqual(intent.VERSION, 'research-intent-v2.6.8')
        self.assertEqual(len(self.check().fields()), 14)
        original = previous_value()
        bad = deepcopy(original)
        del bad['subject']['reference']
        for value in (bad, dict(original, version='research-intent-v2.5.4')):
            with self.assertRaises(InputIssue):
                self.check(value=value)
        for index in (True, False, 1.0, '1', 'null'):
            with self.subTest(index=index), self.assertRaises(InputIssue):
                self.check(value=previous_value(reference('ordinal', index=index)))
        with self.assertRaises(InputIssue):
            self.check(value=previous_value(dict(reference(), extra=True)))
        for ref in (reference('latest', index=1), reference('all', scope=None),
                    reference('none', scope=None), reference('ordinal', index=0), reference('ordinal', index=-1)):
            with self.subTest(ref=ref):
                checked = self.check(value=previous_value(ref))
                self.assertEqual((checked.outcome, checked.bound_subjects), ('clarify_subject', ()))

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

    def test_conflicting_or_consumed_pending_cannot_grant_authority(self):
        ctx = pending_context()
        ctx.session._pending = object()
        self.assertEqual(ctx.model_context()['pending'], dict(valid=False, reason='conflict'))
        self.assertIsNone(intent.local_preflight('未来5天', ctx))
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None))
        result = self.check('研究浦发银行', value, ctx)
        self.assertEqual((result.bound_subjects, result.outcome), (('600000.SH',), 'research'))
        self.assertEqual((result.horizon, result.wants_plan), (None, False))
        self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
        continued = self.check('继续', previous_value(reference(scope='pending')), ctx)
        self.assertEqual((continued.bound_subjects, continued.outcome), ((), 'clarify_conflict'))
        ctx.session.clear_pending()
        self.assertIsNone(intent.local_preflight('中线', ctx))
        self.assertEqual(self.check(value=previous_value(reference(scope='pending')), ctx=ctx).bound_subjects, ())

    def test_positive_authorization_is_not_lent_to_another_task_or_identity(self):
        ctx = pending_context()
        for value, question in ((previous_value(reference(scope='pending'), operation='new', wants_plan=True), '继续'),
                (previous_value(reference(scope='pending'), operation='update', task='index_research', wants_plan=True), '继续')):
            with self.assertRaises(InputIssue):
                self.check(question, value, ctx)
        narrowed = self.check('研究浦发银行', draft(operation='update',
            subject=dict(source='question', mentions=['浦发银行'], selection=None), wants_plan=True, plan_phrase='参考价'), ctx)
        self.assertFalse(narrowed.wants_plan)
        # Local commands take priority over conflicting pending state.
        ctx.session._pending = object()
        for question in ('取消研究。', '查看上次结果'):
            self.assertTrue(intent.local_preflight(question, ctx).zero_intent_calls)


# ---- v2.6.1: Synthetic public-specification v2.6.1 reference and narrowing boundaries.

class V261Tests(OfflineTests):
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
        with self.assertRaises(InputIssue):
            self.check(dict(continued, operation='new'), ctx, '中线')
        value = draft(operation='update', wants_plan=True, plan_phrase='参考价',
                      subject=dict(source='question', mentions=['浦发银行'], selection=None))
        checked, _ = self.check(value, ctx, '研究浦发银行')
        self.assertFalse(checked.wants_plan)
        self.assertFalse(checked.diagnostics['authorization']['same_task_pending'])


# ---- v2.6.2: Synthetic v2.6.2 record existence, recall isolation and expired-pending boundaries.

class V262Tests(OfflineTests):
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

    def test_invalid_pending_never_lends_old_phrases_and_changed_identity_revokes_them(self):
        for ctx, question, value in (
            (replace(pending_context(), now=NOW+timedelta(seconds=900)), '中线',
             previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
                            objective_phrase='参考价', wants_plan=True, plan_phrase='参考价', horizon_expression='中线')),
            (pending_context(), '短线，换成浦发银行',
             draft(operation='update', objective_kind='decision', objective_phrase='参考价', wants_plan=True,
                   plan_phrase='参考价', horizon_expression='短线',
                   subject=dict(source='question', mentions=['浦发银行'], selection=None)))):
            if ctx.model_context()['pending']['valid']:
                result, log = self.check(value, ctx, question)
                self.assertFalse(result.wants_plan)
                self.assertIn('changed_subject_evidence_revoked', [c['kind'] for c in log])
            else:
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

    def test_screening_references_are_cleared_and_updates_still_require_valid_task_scope(self):
        result, log = self.check(previous_value(reference('all', scope='shortlist'), task='screening', operation='update'),
                                 ctx=context(previous=True), question='把名单数量改为五只')
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome), ('none', (), 'clarify_subject'))
        self.assertIn('screening_subject_cleared', [item['kind'] for item in log])
        value = draft(task='screening', operation='update', subject=dict(source='none', mentions=[], selection=None))
        result, _ = self.check(value, question='把名单数量改为五只')
        self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.operation, result.outcome),
                         ('clarify', 'none', (), 'update', 'clarify_subject'))

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


# ---- v2.6.3: Independent synthetic v2.6.3 state, identity and narrowing boundaries; no sealed inputs.

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


# ---- v2.6.4: Independent synthetic v2.6.4 conflict, diagnostic-only and refusal boundaries.

def conflict_context():
    ctx = context(previous=True)
    ctx.session.product_pending = pending_context().session.product_pending
    ctx.session._pending = object()
    return ctx


def horizon_value(**changes):
    return draft(task='clarify', operation='update', clarification_reason='conditions',
        subject=dict(source='none', mentions=[], selection=None), horizon_expression='中期', **changes)


class V264Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_prompt_excludes_complete_questions_from_all_four_public_corpora(self):
        root = Path(__file__).resolve().parents[1]/'data/evaluation'
        corpora = (('research-intent-v2.jsonl', 240), ('research-intent-v2.5-dev.jsonl', 78),
                   ('research-intent-v2.5-holdout.jsonl', 96), ('research-intent-v2.6-dev.jsonl', 64))
        rows = []
        for name, count in corpora:
            public = [json.loads(line) for line in (root/name).read_text(encoding='utf-8').splitlines() if line.strip()]
            self.assertEqual(len(public), count, name)
            rows.extend(public)
        self.assertEqual(len(rows), 478)
        self.assertEqual(len({row['id'] for row in rows}), 478)
        overlaps = []
        for row in rows:
            question = row['question'].strip()
            while question and (question[-1].isspace() or unicodedata.category(question[-1]).startswith('P')):
                question = question[:-1]
            if len(question) >= 5 and question in intent.INSTRUCTION:
                overlaps.append((row['id'], question))
        self.assertEqual(overlaps, [], '提示词不能包含完整公开题干；只读取四份固定公开文件')

    def test_independent_stock_and_index_bypass_conflict_using_only_current_evidence(self):
        for name, task, code in (('浦发银行', 'stock_research', '600000.SH'),
                                  ('沪深300', 'index_research', '000300.SH')):
            ctx = conflict_context()
            state = dict(ctx.session.__dict__)
            value = draft(task=task, subject=dict(source='question', mentions=[name], selection=None),
                          research_kinds=[dict(kind='risk', phrase='风险')])
            before, log = deepcopy(value), []
            result = self.check('请单开研究'+name+'的风险', value, ctx, corrections=log)
            self.assertEqual((result.task, result.operation, result.subject_source, result.bound_subjects, result.outcome),
                             (task, 'new', 'question', (code,), 'research'))
            self.assertEqual((result.horizon, result.wants_plan, result.objective_kind), (None, False, 'analysis'))
            self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
            self.assertIsNone(result.diagnostics['authorization']['plan_source'])
            self.assertNotIn('pending_conflict_priority', [item['kind'] for item in log])
            self.assertEqual(value, before)
            self.assertEqual(ctx.session.__dict__, state)

    def test_new_price_request_keeps_only_its_own_horizon_and_price_authorization(self):
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='未来十二天', horizon_expression='未来十二天',
            wants_plan=True, plan_phrase='入场价')
        result = self.check('浦发银行未来十二天的入场价', value, conflict_context())
        self.assertEqual((result.horizon, result.wants_plan, result.outcome), ('未来12天', True, 'research'))
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'question')
        self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
        value['plan_phrase'] = '参考价'
        with self.assertRaises(InputIssue):
            self.check('浦发银行未来十二天的入场价', value, conflict_context())

    def test_update_refresh_explain_and_previous_references_do_not_bypass(self):
        ctx = conflict_context()
        for operation in ('update', 'refresh', 'explain'):
            value = draft(operation=operation, subject=dict(source='question', mentions=['浦发银行'], selection=None))
            result = self.check('请研究浦发银行', value, ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))
        for ref in (reference(), reference('ordinal', index=1), reference(scope='pending'),
                    reference('ordinal', scope='shortlist', index=1)):
            result = self.check('请继续这项研究', previous_value(ref), ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))

    def test_question_source_cannot_bypass_with_an_ordinal_latest_or_inconsistent_scope(self):
        for ref in (reference(), reference('ordinal', index=1), reference('ordinal', scope='shortlist', index=1),
                    reference('none', scope='pending'), reference('none', scope=None, index=1)):
            value = draft(reference=ref, subject=dict(source='question', mentions=['浦发银行'], selection=None))
            result = self.check('浦发银行的风险', value, conflict_context())
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))

    def test_unverified_or_omitted_identity_never_bypasses_a_conflict(self):
        ctx = conflict_context()
        for name, task in (('示例公司', 'stock_research'), ('示例成长指数', 'index_research')):
            value = draft(task=task, subject=dict(source='question', mentions=[name], selection=None))
            result = self.check('请研究'+name, value, ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None))
        for question in ('研究浦发银行和平安银行', '只研究平安银行'):
            with self.assertRaises(InputIssue):
                self.check(question, value, ctx)
        value = draft(task='unsupported', subject=dict(source='question', mentions=['沪深300ETF'], selection=None))
        self.assertEqual(self.check('研究沪深300ETF', value, ctx).bound_subjects, ())

    def test_single_pending_horizon_normalization_never_recovers_binding_or_authority(self):
        base = pending_context()
        for ctx in (base, replace(base, now=base.now+timedelta(seconds=900)),
                    replace(base, now=base.now+timedelta(seconds=901)),
                    replace(base, now=base.now-timedelta(seconds=1)),
                    replace(base, source='akshare_eastmoney'), replace(base, conversation_memory=False)):
            value, log = horizon_value(), []
            state, before = dict(ctx.session.__dict__), deepcopy(value)
            result = self.check('我打算选中期', value, ctx, corrections=log)
            raw = self.check('我打算选中期', value, ctx, normalize=False)
            self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.wants_plan),
                             ('clarify', 'previous', (), False))
            self.assertEqual(result.horizon, '未来40个交易日')
            self.assertEqual(raw.subject_source, 'none')
            self.assertEqual(result.outcome, raw.outcome)
            self.assertEqual(result.diagnostics['authorization'], raw.diagnostics['authorization'])
            self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
            self.assertIn(('subject', 'previous'), result.provenance)
            self.assertIn('pending_horizon_source', [item['kind'] for item in log])
            self.assertEqual(value, before)
            self.assertEqual(ctx.session.__dict__, state)

    def test_no_record_multiple_records_new_subject_and_extra_fields_do_not_gain_previous(self):
        for ctx in (context(), context(previous=True), conflict_context()):
            result = self.check('我打算选中期', horizon_value(), ctx)
            self.assertEqual((result.subject_source, result.bound_subjects), ('none', ()))
        for changes, question in ((dict(indicator_mentions=['RSI']), '中期用RSI'),
                (dict(research_kinds=[dict(kind='risk', phrase='风险')]), '中期分析风险'),
                (dict(exclusions=[dict(kind='plan', phrase='不要价位')]), '中期不要价位'),
                (dict(date_expression='上个月'), '中期回看上个月'),
                (dict(strict_scope=True, scope_phrase='只看风险'), '中期只看风险')):
            self.assertEqual(self.check(question, horizon_value(**changes), pending_context()).subject_source, 'none')
        self.assertEqual(self.check('浦发银行，中期', horizon_value(), pending_context()).subject_source, 'none')
        value = horizon_value()
        value['horizon_expression'] = '未来5000天'
        self.assertEqual(self.check('未来5000天', value, pending_context()).subject_source, 'none')

    def test_known_unsupported_index_objective_is_neutral_only_in_normalized_view(self):
        for task in ('index_research', 'clarify', 'unsupported'):
            value = draft(task=task, objective_kind='outlook', objective_phrase='后续走向',
                clarification_reason='identity' if task == 'clarify' else None,
                subject=dict(source='question', mentions=['中证500'], selection=None))
            before, log = deepcopy(value), []
            result = self.check('中证500后续走向', value, corrections=log)
            raw = self.check('中证500后续走向', value, normalize=False)
            self.assertEqual((result.task, result.objective_kind, result.outcome, result.bound_subjects),
                             ('unsupported', 'analysis', 'reject', ()))
            self.assertEqual(raw.objective_kind, 'outlook')
            self.assertIn('unsupported_index_objective', [item['kind'] for item in log])
            self.assertEqual(value, before)

    def test_etf_unknown_index_and_stock_background_do_not_get_neutral_index_objective(self):
        for name, task in (('中证500ETF', 'unsupported'), ('示例成长指数', 'index_research'),
                           ('浦发银行', 'stock_research')):
            value = draft(task=task, objective_kind='outlook', objective_phrase='后续走向',
                subject=dict(source='question', mentions=[name], selection=None))
            log = []
            result = self.check(name+'后续走向', value, corrections=log)
            self.assertNotEqual(result.objective_kind, 'analysis')
            self.assertNotIn('unsupported_index_objective', [item['kind'] for item in log])
        value = draft(task='knowledge', subject=dict(source='none', mentions=[], selection=None))
        self.assertEqual(self.check('中证500是什么', value).outcome, 'local')

    def test_price_veto_and_full_indicator_parameters_survive_candidate_checking(self):
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='适合加仓么', wants_plan=True, plan_phrase='适合加仓么')
        result = self.check('浦发银行此时适合加仓么', value)
        self.assertEqual((result.wants_plan, result.outcome), (True, 'clarify_horizon'))
        value.update(wants_plan=False, plan_phrase=None, exclusions=[dict(kind='plan', phrase='别给价位')])
        result = self.check('浦发银行此时适合加仓么，但别给价位', value)
        self.assertEqual((result.wants_plan, result.exclusions), (False, ('plan',)))
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
            indicator_mentions=['SMA3', '均线'], research_kinds=[dict(kind='technical', phrase='均线')])
        self.assertEqual(self.check('沪深300用SMA3与均线研究', value).indicator_mentions, ('SMA3', '均线'))


# ---- v2.6.5: Independent v2.6.5 revocation, goal, task and diagnostic boundaries.

def published_context(original='平安银行未来二十天能建仓吗'):
    ctx = context()
    plan = ProductPlan(status='ready', source='tencent', securities=SECURITIES[:1], kinds=('technical',),
        start_date=ctx.reference_date-timedelta(days=100), end_date=ctx.reference_date-timedelta(days=1),
        objective=ResearchObjective('decision', '未来20天'), user_question=original)
    ctx.session.product_saved = SavedProductResearch(ctx.now, plan, None, None)
    ctx.session.stage_research_subject(plan.securities, kind='company', source='tencent',
        development=False, now=ctx.now, scope=plan)
    return replace(ctx, history=(original,))


class V265Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_repeated_research_kinds_keep_first_grounded_phrase_and_raw_is_unchanged(self):
        for second in ('日线', '技术面'):
            value = draft(research_kinds=[dict(kind='technical', phrase='日线'),
                dict(kind='technical', phrase=second), dict(kind='risk', phrase='风险')])
            before, log = deepcopy(value), []
            result = self.check('平安银行日线技术面与风险分析', value, corrections=log)
            self.assertEqual(result.research_kinds, ('technical', 'risk'))
            correction, = log
            self.assertEqual(correction['kind'], 'research_kinds_deduplicated')
            self.assertEqual(correction['after'], [value['research_kinds'][0], value['research_kinds'][2]])
            self.assertEqual(value, before)
            with self.assertRaises(InputIssue):
                self.check('平安银行日线技术面与风险分析', value, normalize=False)

    def test_deduplication_cannot_hide_invalid_phrases_items_or_exclusions(self):
        first = dict(kind='technical', phrase='技术面')
        for second in (dict(kind='technical', phrase='不存在的原文'),
                       dict(kind='unlisted', phrase='技术面'), dict(first, extra=True)):
            with self.assertRaises(InputIssue):
                self.check('平安银行技术面', draft(research_kinds=[first, second]))
        with self.assertRaises(InputIssue):
            self.check('平安银行不要风险', draft(exclusions=[dict(kind='risk', phrase='不要风险')]*2))
        with self.assertRaises(InputIssue):
            self.check('平安银行技术面', draft(research_kinds=[first]*7))

    def test_published_original_evidence_is_revoked_with_no_history_authority_or_mutation(self):
        ctx, log = published_context(), []
        state = dict(ctx.session.__dict__)
        value = previous_value(operation='update', objective_kind='decision', objective_phrase='未来二十天能建仓吗',
            wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来二十天', indicator_mentions=['RSI'])
        before = deepcopy(value)
        result = self.check('同一对象添加RSI', value, ctx, corrections=log)
        self.assertEqual((result.objective_kind, result.wants_plan, result.horizon), ('analysis', False, None))
        self.assertEqual((result.bound_subjects, result.outcome), (('000001.SZ',), 'research'))
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])
        self.assertIn('published_evidence_revoked', [item['kind'] for item in log])
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)
        with self.assertRaises(InputIssue):
            self.check('同一对象添加RSI', value, ctx, normalize=False)

    def test_current_horizon_survives_old_goal_revocation_but_does_not_authorize_prices(self):
        value = previous_value(operation='update', objective_kind='decision', objective_phrase='能建仓吗',
            wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来十三天')
        result = self.check('同一对象未来十三天', value, published_context())
        self.assertEqual((result.horizon, result.objective_kind, result.wants_plan), ('未来13天', 'decision', False))

    def test_chat_only_stale_mismatched_or_consumed_publications_cannot_sanitize_evidence(self):
        native = published_context()
        chat = replace(context(previous=True), history=native.history)
        mismatch = published_context()
        mismatch.session.product_saved = replace(mismatch.session.product_saved,
            plan=replace(mismatch.session.product_saved.plan, user_question='别的原始研究'))
        consumed = published_context()
        consumed.session._subject_current = False
        for ctx in (chat, mismatch, consumed, replace(native, conversation_memory=False),
                    replace(native, now=native.now+timedelta(seconds=900)),
                    replace(native, now=native.now-timedelta(seconds=1)),
                    replace(native, source='akshare_eastmoney')):
            with self.subTest(ctx=ctx), self.assertRaises(InputIssue):
                self.check('同一对象添加RSI', previous_value(operation='update', indicator_mentions=['RSI'],
                    objective_kind='decision', objective_phrase='能建仓吗', wants_plan=True,
                    plan_phrase='能建仓吗', horizon_expression='未来二十天'), ctx)

    def test_invented_objective_plan_horizon_and_date_remain_invalid(self):
        for key in ('objective_phrase', 'plan_phrase', 'horizon_expression', 'date_expression'):
            value = previous_value(operation='update', objective_kind='decision', objective_phrase='能建仓吗',
                wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来二十天')
            value[key] = '虚构的新依据'
            with self.subTest(key=key), self.assertRaises(InputIssue):
                self.check('同一对象添加RSI', dict(value, indicator_mentions=['RSI']), published_context())

    def test_registered_pending_authorization_is_preserved_even_with_a_matching_publication(self):
        ctx = pending_context()
        published = published_context('平安银行给参考价')
        ctx.session.product_saved = published.session.product_saved
        ctx.session.stage_research_subject(published.session.product_saved.plan.securities, kind='company',
            source='tencent', development=False, now=ctx.now, scope=published.session.product_saved.plan)
        value = previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
            objective_phrase='给参考价', wants_plan=True, plan_phrase='给参考价', horizon_expression='未来十三天')
        result = self.check('未来十三天', value, ctx)
        self.assertTrue(result.wants_plan)
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'program_pending')

    def test_price_goal_uses_extracted_evidence_before_unused_phrase_cleanup_and_never_grants(self):
        for task, mention in (('stock_research', '平安银行'), ('index_research', '沪深300')):
            for field in ('plan_phrase', 'objective_phrase'):
                value = draft(task=task, subject=dict(source='question', mentions=[mention], selection=None),
                    **{field: '给入场价'})
                before, log = deepcopy(value), []
                result = self.check(mention+'给入场价，但别给价位', value, corrections=log)
                self.assertEqual((result.objective_kind, result.wants_plan), ('decision', False))
                self.assertIsNone(result.diagnostics['authorization']['plan_source'])
                self.assertTrue(result.diagnostics['authorization']['plan_veto'])
                self.assertIn('objective_price_request', [item['kind'] for item in log])
                self.assertEqual(value, before)
                raw = self.check(mention+'给入场价，但别给价位', value, normalize=False)
                self.assertEqual(raw.objective_kind, 'analysis')

    def test_pure_negative_rating_direction_and_absent_price_fragments_do_not_change_goal(self):
        for phrase in ('不要价位', '不需要计划', '别给止损位', '不做交易计划', '没有价位',
                       '给个买卖评级', '退出评级', '说出方向'):
            for field in ('objective_phrase', 'plan_phrase'):
                result = self.check('平安银行'+phrase, draft(**{field: phrase}))
                self.assertEqual((result.objective_kind, result.wants_plan), ('analysis', False))
        with self.assertRaises(InputIssue):
            self.check('平安银行说明风险', draft(plan_phrase='给入场价'))

    def test_index_price_goal_with_current_horizon_remains_decision_when_prices_are_vetoed(self):
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
            plan_phrase='给退出点位', horizon_expression='未来十三天', wants_plan=False)
        result = self.check('沪深300未来十三天，给退出点位，但别给价位', value)
        self.assertEqual((result.objective_kind, result.horizon, result.wants_plan), ('decision', '未来13天', False))
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])

    def test_price_goal_does_not_change_explicit_permission_or_missing_identity(self):
        value = draft(task='clarify', clarification_reason='subject',
            subject=dict(source='none', mentions=[], selection=None), wants_plan=True, plan_phrase='给止损参考位')
        result = self.check('给止损参考位', value)
        self.assertEqual((result.task, result.objective_kind, result.wants_plan, result.bound_subjects, result.outcome),
                         ('clarify', 'decision', True, (), 'clarify_subject'))

    def test_recall_knowledge_and_screening_do_not_acquire_decision_objectives(self):
        for task in ('recall', 'knowledge', 'screening'):
            value = draft(task=task, objective_phrase='给过的参考价',
                subject=dict(source='none', mentions=[], selection=None))
            result = self.check('解释给过的参考价', value)
            self.assertEqual((result.objective_kind, result.wants_plan), ('analysis', False))

    def test_unknown_index_keeps_category_without_binding_and_known_alias_resolves_identity_only(self):
        for name, code in (('示例创新指数', None), ('大盘指数', '000001.SH')):
            value = draft(task='clarify', confidence='low', clarification_reason='identity',
                subject=dict(source='question', mentions=[name], selection=None))
            log = []
            result = self.check('考察'+name+'当前走势', value, corrections=log)
            self.assertEqual(result.task, 'index_research')
            self.assertEqual(result.bound_subjects, () if code is None else (code,))
            self.assertEqual(result.outcome, 'clarify_identity' if code is None else 'research')
            self.assertFalse(result.wants_plan)
            self.assertTrue(result.low_confidence)  # The model's original uncertainty remains reported.
        value['clarification_reason'] = 'conditions'
        result = self.check('考察大盘指数当前走势', value)
        self.assertEqual(result.outcome, 'clarify_conditions')

    def test_conflicting_previous_research_keeps_task_without_fixing_latest_or_granting(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for ref, expected in ((reference('all'), tuple(s.instrument_id for s in SECURITIES[:2])),
                              (reference(), ())):
            value = previous_value(ref, task='clarify', clarification_reason='conflict', operation='update',
                research_kinds=[dict(kind='financial', phrase='财务')],
                exclusions=[dict(kind='financial', phrase='不看财务')])
            result = self.check('原对象只看财务但是不看财务', value, ctx)
            self.assertEqual((result.task, result.outcome, result.bound_subjects),
                             ('stock_research', 'clarify_conflict', expected))
            self.assertEqual(result.diagnostics['reference'], ref)
            self.assertFalse(result.wants_plan)
        result = self.check('继续', previous_value(task='clarify', clarification_reason='conflict'), conflict_context())
        self.assertEqual((result.task, result.bound_subjects), ('clarify', ()))

    def test_complete_product_mention_rejects_without_binding_underlying_stock_or_index(self):
        for name in ('那只债券基金', '沪深300ETF'):
            value = draft(task='unsupported', subject=dict(source='question', mentions=[name], selection=None))
            result = self.check('考察'+name+'当前表现', value)
            self.assertEqual((result.task, result.bound_subjects, result.outcome), ('unsupported', (), 'reject'))
        with self.assertRaises(InputIssue):
            self.check('考察沪深300ETF当前表现', draft(task='index_research',
                subject=dict(source='question', mentions=['沪深300'], selection=None)))

    def test_local_binding_proofs_use_validated_pending_and_saved_snapshot_without_session_changes(self):
        for ctx, question, scope in ((pending_context(), '中线', 'pending'),
                                      (published_context(), '查看上次结果', 'checked_saved_result')):
            state = dict(ctx.session.__dict__)
            checked = intent.local_preflight(question, ctx)
            proof = checked.diagnostics['binding']
            self.assertEqual(proof['scope'], scope)
            self.assertIsNone(proof['issue'])
            self.assertEqual(proof['scope_subjects'], list(checked.bound_subjects))
            self.assertEqual(ctx.session.__dict__, state)
        ctx = published_context()
        stale = intent.local_preflight('查看上次结果', replace(ctx, now=ctx.now+timedelta(seconds=900)))
        self.assertEqual(stale.bound_subjects, ())
        self.assertEqual(stale.diagnostics['binding']['scope_subjects'], [])
        self.assertIsNotNone(stale.diagnostics['binding']['issue'])
        self.assertIsNotNone(ctx.session.product_saved)

    def test_full_length_target_uses_unchanged_shared_limit_and_no_model(self):
        question = '平安银行'+'a'*1996
        self.assertEqual(len(question), 2000)
        result = measure_request(question, context())
        self.assertTrue(result['valid'])
        self.assertGreaterEqual(result['headroom'], 0)
        self.assertEqual(result['headroom'], 20000-result['serialized_characters'])


# ---- v2.6.6: Independent v2.6.6 domain, screening and extracted-duration boundaries.

def shortlist_context():
    ctx = replace(context(), quote_source=object())
    request = ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=3)
    ctx.session.screening_saved = SavedScreening(ctx.now, request, SECURITIES[:3], ctx.quote_source)
    return ctx


class V266Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_instruction_stays_within_v266_length_and_declares_current_protocol(self):
        self.assertLessEqual(len(intent.INSTRUCTION), 4014)
        self.assertIn('version='+intent.VERSION, intent.INSTRUCTION)

    def test_supported_and_unsupported_indicators_supply_only_domain_label(self):
        for name, task in (('平安银行', 'stock_research'), ('沪深300', 'index_research')):
            for indicator in ('EMA17', 'RSI9', 'MACD', '成交量', 'CCI', 'TRIX'):
                value = draft(task=task, subject=dict(source='question', mentions=[name], selection=None),
                    indicator_mentions=[indicator], strict_scope=True, scope_phrase='只看')
                original, log = deepcopy(value), []
                result = self.check(name+'只看'+indicator, value, corrections=log)
                raw = self.check(name+'只看'+indicator, value, normalize=False)
                self.assertEqual(result.research_kinds, ('technical',))
                self.assertEqual(result.indicator_mentions, (indicator,))
                self.assertEqual(result.strict_scope, raw.strict_scope)
                self.assertEqual((result.bound_subjects, result.wants_plan, result.outcome, result.plan_blocked),
                                 (raw.bound_subjects, raw.wants_plan, raw.outcome, raw.plan_blocked))
                self.assertEqual(result.outcome, 'clarify_conditions' if indicator in ('CCI', 'TRIX') else 'research')
                self.assertIn('explicit_indicator_domain', [c['kind'] for c in log])
                self.assertEqual(value, original)

    def test_existing_domain_exclusions_and_independent_veto_keep_precedence(self):
        question = '平安银行不用技术面，只看SMA7'
        for exclusions in ([], [dict(kind='technical', phrase='不用技术面')]):
            result = self.check(question, draft(indicator_mentions=['SMA7'], exclusions=exclusions))
            self.assertNotIn('technical', result.research_kinds)
        value = draft(indicator_mentions=['EMA17'], exclusions=[dict(kind='indicator:ema', phrase='不看EMA17')])
        result = self.check('平安银行不看EMA17', value)
        self.assertEqual((result.research_kinds, result.indicator_mentions, result.outcome), ((), (), 'clarify_conflict'))
        log = []
        value = draft(indicator_mentions=['SMA7'], research_kinds=[dict(kind='technical', phrase='SMA7')])
        self.assertEqual(self.check('平安银行SMA7', value, corrections=log).research_kinds, ('technical',))
        self.assertNotIn('explicit_indicator_domain', [c['kind'] for c in log])

    def test_indicator_domain_cannot_unblock_strict_prices_or_bind_unknown_reference(self):
        value = draft(objective_kind='decision', objective_phrase='给参考价', wants_plan=True,
            plan_phrase='给参考价', horizon_expression='短线', strict_scope=True, scope_phrase='只看财务',
            research_kinds=[dict(kind='financial', phrase='财务')], indicator_mentions=['ATR14'])
        question = '平安银行短线只看财务，ATR14，给参考价'
        raw, result = self.check(question, value, normalize=False), self.check(question, value)
        self.assertEqual(result.plan_blocked, 'risk_atr_not_authorized')
        self.assertEqual((result.outcome, result.wants_plan, result.strict_scope, result.bound_subjects),
                         (raw.outcome, raw.wants_plan, raw.strict_scope, raw.bound_subjects))
        missing = previous_value(task='index_research', indicator_mentions=['EMA17'])
        result = self.check('同一对象加EMA17', missing)
        self.assertEqual((result.task, result.outcome, result.bound_subjects, result.research_kinds),
                         ('clarify', 'clarify_subject', (), ('technical',)))

    def test_screening_clears_subject_without_selecting_any_stock_or_mutating_state(self):
        ctx = shortlist_context()
        for source, mention, reference in (
                ('previous', [], dict(kind='latest', index=None, scope='shortlist')),
                ('previous', [], dict(kind='ordinal', index=2, scope='shortlist')),
                ('question', ['平安银行'], dict(kind='none', index=None, scope=None))):
            value = draft(task='screening', operation='update', reference=reference,
                subject=dict(source=source, mentions=mention, selection='第二只' if source == 'previous' else None))
            original, state, log = deepcopy(value), dict(ctx.session.__dict__), []
            result = self.check('平安银行和第二只的名单任务增加价格条件', value, ctx, corrections=log)
            self.assertEqual((result.task, result.operation, result.subject_source, result.bound_subjects, result.outcome),
                             ('screening', 'update', 'none', (), 'research'))
            self.assertEqual(result.diagnostics['reference'], dict(kind='none', index=None, scope=None))
            self.assertIsNone(result.diagnostics['binding']['issue'])
            self.assertIn('screening_subject_cleared', [c['kind'] for c in log])
            self.assertEqual(value, original)
            self.assertEqual(ctx.session.__dict__, state)

    def test_screening_update_retains_list_source_mode_identity_memory_and_time_checks(self):
        ctx = shortlist_context()
        value = draft(task='screening', operation='update', subject=dict(source='previous', mentions=[], selection=None),
            reference=dict(kind='latest', index=None, scope='shortlist'))
        for invalid in (context(), replace(ctx, conversation_memory=False), replace(ctx, source='akshare_eastmoney'),
                replace(ctx, directory=SecurityDirectory(SECURITIES[1:], 'v266-replaced', ctx.now, False)),
                replace(ctx, quote_source=object()), replace(ctx, development=True),
                replace(ctx, now=ctx.now-timedelta(seconds=1)), replace(ctx, now=ctx.now+timedelta(minutes=15))):
            result = self.check('名单加价格条件', value, invalid)
            self.assertEqual((result.subject_source, result.bound_subjects, result.outcome), ('none', (), 'clarify_subject'))
            self.assertIsNotNone(result.diagnostics['binding']['issue'])
        # A fresh screening request has no saved-list dependency.
        fresh = dict(value, operation='new')
        self.assertEqual(self.check('筛选新的名单', fresh).outcome, 'research')

    def test_screening_conditions_are_cleared_only_after_schema_and_grounding_checks(self):
        value = draft(task='screening', indicator_mentions=['换手率大于4%'],
            subject=dict(source='none', mentions=[], selection=None))
        original, log = deepcopy(value), []
        result = self.check('筛选换手率大于4%的股票', value, corrections=log)
        self.assertEqual((result.indicator_mentions, result.research_kinds, result.outcome), ((), (), 'research'))
        self.assertIn('screening_indicators_cleared', [c['kind'] for c in log])
        self.assertEqual(value, original)
        self.assertEqual(self.check('筛选换手率大于4%的股票', value, normalize=False).outcome, 'clarify_conditions')
        for bad in (dict(value, indicator_mentions=['不存在的条件']),
                    dict(value, indicator_mentions=['SMA7']*17),
                    dict(value, subject=dict(source='question', mentions=['不存在的对象'], selection=None))):
            with self.assertRaises(InputIssue):
                self.check('筛选换手率大于4%的股票', bad)
        for indicator in ('CCI', '日线'):
            research = draft(indicator_mentions=[indicator])
            if indicator == '日线':
                with self.assertRaises(InputIssue):
                    self.check('研究平安银行'+indicator, research)
            else:
                self.assertEqual(self.check('研究平安银行'+indicator, research).outcome, 'clarify_conditions')
        with self.assertRaises(InputIssue):
            self.check('筛选给参考价', dict(value, indicator_mentions=[], wants_plan=True, plan_phrase='给参考价'))

    def test_only_whole_extracted_bare_durations_are_accepted_without_pending_authority(self):
        ctx = replace(pending_context(), development=True)
        for expression, expected in (('5周', '未来5周'), ('十三天', '未来13天'), ('3个月', '未来3个月'),
                                     ('１２个交易日', '未来12个交易日')):
            value = previous_value(task='clarify', operation='update', clarification_reason='conditions',
                horizon_expression=expression, ref=dict(kind='latest', index=None, scope='pending'))
            result = self.check('准备持有'+expression, value, ctx)
            self.assertEqual(result.horizon, expected)
            self.assertEqual((result.bound_subjects, result.wants_plan), ((), False))
        value['horizon_expression'] = None
        self.assertIsNone(self.check('准备持有5周', value, ctx).horizon)
        for expression in ('0周', '53周', '5周左右', '5周后', '5周和7周', '-5周', '5.5周'):
            with self.assertRaises(InputIssue):
                intent.normalize_horizon(expression)
        with self.assertRaises(InputIssue):
            self.check('准备持有', dict(value, horizon_expression='5周'), ctx)


# ---- v2.6.7: Independent v2.6.7 invalid-pending diagnosis and entry-decision boundaries.

def expired_context():
    ctx = pending_context()
    return replace(ctx, now=ctx.now+timedelta(minutes=20))


def previous_horizon_value(**changes):
    return draft(task='clarify', operation='update', objective_kind='analysis', clarification_reason='conditions',
        horizon_expression='中期', subject=dict(source='previous', mentions=[], selection=None,
            reference=dict(kind='none', index=None, scope=None)), **changes)


class V267Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_model_conditions_survive_invalid_reference_without_binding_or_mutation(self):
        for change in (dict(now=pending_context().now+timedelta(minutes=20)),
                dict(now=pending_context().now-timedelta(seconds=1)), dict(source='akshare_eastmoney'),
                dict(development=True), dict(conversation_memory=False)):
            ctx = replace(pending_context(), **change)
            state, value, log = dict(ctx.session.__dict__), previous_horizon_value(), []
            original = deepcopy(value)
            result = self.check('我的期限取中期', value, ctx, corrections=log)
            self.assertEqual((result.task, result.operation, result.outcome), ('clarify', 'update', 'clarify_conditions'))
            self.assertEqual((result.bound_subjects, result.wants_plan, result.horizon), ((), False, '未来40个交易日'))
            self.assertEqual(result.diagnostics['binding']['issue'], 'clarify_subject')
            self.assertIsNone(result.diagnostics['authorization']['plan_source'])
            self.assertIn('reference_unbound', [c['kind'] for c in log])
            self.assertIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
            self.assertEqual(value, original)
            self.assertEqual(ctx.session.__dict__, state)
            self.assertEqual(self.check('我的期限取中期', value, ctx, normalize=False).outcome, 'clarify_subject')

    def test_absent_valid_conflicting_or_wrong_stage_pending_cannot_use_priority_repair(self):
        wrong_stage = pending_context()
        wrong_stage.session.product_pending = replace(wrong_stage.session.product_pending, stage='identity')
        for ctx in (context(), pending_context(), conflict_context(), wrong_stage):
            log = []
            result = self.check('我的期限取中期', previous_horizon_value(), ctx, corrections=log)
            self.assertEqual(result.bound_subjects, ())
            self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
            if ctx is not wrong_stage:
                self.assertNotEqual(result.outcome, 'clarify_conditions')

    def test_model_subject_diagnosis_and_unrelated_scope_or_selection_stay_unrepaired(self):
        base = previous_horizon_value()
        changes = (dict(operation='new'), dict(clarification_reason='subject'), dict(horizon_expression=None),
            dict(horizon_expression='以后'), dict(objective_phrase='期限'),
            dict(subject=dict(source='previous', mentions=[], selection='它', reference=dict(kind='none', index=None, scope=None))),
            dict(subject=dict(source='previous', mentions=[], selection=None, reference=dict(kind='latest', index=None, scope='latest_research'))),
            dict(subject=dict(source='previous', mentions=[], selection=None, reference=dict(kind='none', index=1, scope=None))))
        for change in changes:
            log = []
            result = self.check('它的期限是中期，以后再说', dict(base, **change), expired_context(), corrections=log)
            self.assertEqual(result.outcome, 'clarify_subject')
            self.assertEqual(result.bound_subjects, ())
            self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
        with self.assertRaises(InputIssue):
            self.check('没有期限片段', base, expired_context())

    def test_explicit_pending_scope_keeps_existing_conditions_without_new_repair(self):
        value = previous_horizon_value()
        value['subject']['reference'] = dict(kind='latest', index=None, scope='pending')
        log = []
        result = self.check('我的期限取中期', value, expired_context(), corrections=log)
        self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conditions', ()))
        self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])

    def test_entry_question_accepts_its_literal_marker_but_not_ratings_or_fabrication(self):
        phrase = '此时是否适宜介入'
        value = draft(objective_kind='decision', objective_phrase=phrase, wants_plan=True, plan_phrase=phrase)
        result = self.check('研究平安银行，'+phrase, value)
        self.assertTrue(result.wants_plan)
        self.assertEqual(result.outcome, 'clarify_horizon')
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'question')
        self.assertFalse(self.check('研究平安银行，'+phrase, dict(value, wants_plan=False, plan_phrase=None)).wants_plan)
        for phrase in ('给个介入评级', '说说介入方向'):
            with self.assertRaises(InputIssue):
                self.check('平安银行'+phrase, dict(value, objective_phrase=phrase, plan_phrase=phrase))
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value)

    def test_plan_cancellation_and_entry_veto_survive_even_a_positive_request(self):
        for negative in ('介入取消', '不要介入', '计划免了', '删去计划', '不用计划',
                         '不要价位', '不需要止损', '不必给参考价'):
            question = '平安银行'+negative+'，但给我入场参考价'
            value = draft(objective_kind='decision', objective_phrase='给我入场参考价', wants_plan=True,
                plan_phrase='给我入场参考价', exclusions=[dict(kind='plan', phrase=negative)])
            result = self.check(question, value)
            self.assertEqual((result.wants_plan, result.outcome, result.exclusions), (False, 'clarify_conflict', ('plan',)))
            self.assertTrue(result.diagnostics['authorization']['plan_veto'])
            if negative in ('介入取消', '不要介入'):
                without_exclusion = self.check(question, dict(value, exclusions=[]))
                self.assertFalse(without_exclusion.wants_plan)

    def test_prompt_and_full_target_fit_both_v266_size_measures_and_keep_current_version(self):
        self.assertIn('version='+intent.VERSION, intent.INSTRUCTION)
        self.assertLessEqual(len(intent.INSTRUCTION), 4014)
        self.assertLessEqual(len(json.dumps(ToolMessage('system', intent.INSTRUCTION).to_dict(), ensure_ascii=True)), 13980)
        measured = measure_request('平安银行'+'a'*1996, context())
        self.assertTrue(measured['valid'])
        self.assertLessEqual(measured['serialized_characters'], 19996)


# ---- v2.6.8: v2.6.8 prompt scope and existing recall/explanation authority boundaries.

class V268Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_other_prompt_rules_are_byte_identical_and_both_size_limits_are_preserved(self):
        canonical = intent.INSTRUCTION.replace(intent.VERSION, 'research-intent-v2.6.7')
        start, end = canonical.index('recall复述'), canonical.index('recall、knowledge')
        unchanged = canonical[:start]+'<recall-boundary>'+canonical[end:]
        self.assertEqual(sha256(unchanged.encode('utf-8')).hexdigest(),
                         'b8f78cf91d6780086fd64565397dfc95ad03d92a5dd27b790e035ba29a342c68')
        self.assertIn('version='+intent.VERSION, intent.INSTRUCTION)
        self.assertLessEqual(len(intent.INSTRUCTION), 4014)
        self.assertLessEqual(len(json.dumps(ToolMessage('system', intent.INSTRUCTION).to_dict(), ensure_ascii=True)), 13785)
        measured = measure_request('平安银行'+'a'*1996, context())
        self.assertTrue(measured['valid'])
        self.assertLessEqual(measured['serialized_characters'], 19801)

    def test_recall_repeats_content_without_subject_authority_or_mutation(self):
        ctx = context(previous=True)
        value = previous_value(task='recall')
        before, state = deepcopy(value), dict(ctx.session.__dict__)
        result = self.check('把先前给的方向照着念一下', value, ctx)
        self.assertEqual((result.task, result.operation, result.objective_kind, result.wants_plan),
                         ('recall', 'new', 'analysis', False))
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome), ('none', (), 'local'))
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)

    def test_explanation_uses_checked_latest_research_without_new_plan_or_mutation(self):
        ctx, value = context(previous=True), previous_value(operation='explain')
        before, state = deepcopy(value), dict(ctx.session.__dict__)
        result = self.check('帮我讲透旧判断的论证', value, ctx)
        self.assertEqual((result.task, result.operation, result.objective_kind, result.wants_plan),
                         ('stock_research', 'explain', 'analysis', False))
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                         ('previous', ('000001.SZ',), 'research'))
        self.assertEqual(result.diagnostics['reference']['scope'], 'latest_research')
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)

    def test_words_do_not_rewrite_task_or_operation_or_expand_local_grammar(self):
        ctx = context(previous=True)
        for question in ('帮我讲透旧判断的论证', '你为何作出先前那个判断', '请说明旧报告用了哪些依据'):
            self.assertIsNone(intent.local_preflight(question, ctx))
            # A mistaken model recall remains visible; this release only changes its prompt.
            value = draft(task='recall', subject=dict(source='none', mentions=[], selection=None))
            result = self.check(question, value, ctx)
            self.assertEqual((result.task, result.operation), ('recall', 'new'))
        result = self.check('沿用旧对象再作一个新判断', previous_value(operation='new'), ctx)
        self.assertEqual((result.task, result.operation), ('stock_research', 'new'))

    def test_explanation_cannot_recover_missing_or_unusable_latest_binding(self):
        base = context(previous=True)
        for ctx in (context(), context(previous=True, age=-1), replace(base, conversation_memory=False),
                    replace(base, source='akshare_eastmoney'), replace(base, development=True)):
            result = self.check('帮我讲透旧判断的论证', previous_value(operation='explain'), ctx)
            self.assertEqual((result.bound_subjects, result.wants_plan, result.outcome), ((), False, 'clarify_subject'))

    def test_explain_operation_still_revokes_new_price_authority(self):
        value = previous_value(operation='explain', objective_kind='decision', objective_phrase='给参考价',
            wants_plan=True, plan_phrase='给参考价', horizon_expression='中期')
        result = self.check('讲解旧结论，中期给参考价', value, context(previous=True))
        self.assertEqual(result.operation, 'explain')
        self.assertFalse(result.wants_plan)
        self.assertEqual(result.plan_blocked, 'explain_no_new_authorization')


if __name__ == '__main__':
    unittest.main()
