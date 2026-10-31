"""Independent fixed protocol responses; these tests do not predict corpus questions."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest

from trading_companion.agent_input import InputIssue
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_intent_v2 import (
    IntentContext, SCHEMA, TOOL_NAME, VERSION, check_response, understand,
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


def draft(**changes):
    value = dict(version=VERSION, task='stock_research', operation='new', objective_kind='analysis',
        objective_phrase=None, wants_plan=False, plan_phrase=None,
        subject=dict(source='question', mentions=['平安银行'], selection=None),
        horizon_expression=None, date_expression=None, indicator_mentions=[], research_kinds=[],
        exclusions=[], strict_scope=False, scope_phrase=None, confidence='high', clarification_reason=None)
    value.update(changes)
    return value


def response(value=None, **changes):
    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('v2-fixed', TOOL_NAME, value or draft()),), **changes)


def checked(question, value=None, ctx=None):
    return check_response(question, response(value), context=ctx or context())


class ProtocolTests(unittest.TestCase):
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

    def test_wrong_tool_multiple_calls_text_only_and_low_confidence(self):
        for payload in (ToolModelResponse('complete', text='直接回答'),
                        ToolModelResponse('tool_calls', tool_calls=(ToolCall('x', 'other', {}),)),
                        ToolModelResponse('tool_calls', tool_calls=(ToolCall('x', TOOL_NAME, draft()), ToolCall('y', TOOL_NAME, draft()))),
                        response(draft(confidence='low'))):
            with self.subTest(payload=payload), self.assertRaises(InputIssue):
                check_response('分析平安银行', payload, context=context())

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
            self.assertIn('plan', plain.exclusions)
            contradictory = checked('平安银行给价位，'+phrase,
                draft(objective_kind='decision', objective_phrase='给价位', wants_plan=True, plan_phrase='给价位'))
            self.assertEqual(contradictory.outcome, 'clarify_conflict')
            self.assertFalse(contradictory.wants_plan)
        with self.assertRaises(InputIssue):
            checked('分析平安银行的ATR', draft(exclusions=[dict(kind='indicator:atr', phrase='ATR')]))

    def test_positive_indicators_cannot_override_exclusions_or_drop_strict_scope(self):
        value = draft(indicator_mentions=['SMA3'], strict_scope=True, scope_phrase='只用SMA3',
                      research_kinds=[dict(kind='technical', phrase='SMA3')])
        intent = checked('平安银行只用SMA3，禁用ATR', value)
        self.assertEqual(intent.indicator_mentions, ('SMA3',))
        self.assertEqual(intent.exclusions, ('indicator:atr',))
        for bad in (draft(), draft(strict_scope=True, scope_phrase='只用SMA3'),
                    draft(indicator_mentions=['SMA3', 'ATR'], strict_scope=True, scope_phrase='只用SMA3')):
            with self.subTest(bad=bad), self.assertRaises(InputIssue):
                checked('平安银行只用SMA3，禁用ATR', bad)

    def test_strict_scope_blocks_implicit_atr_dependency(self):
        intent = checked('平安银行短线给价位，只用SMA3', draft(objective_kind='decision', objective_phrase='给价位',
            wants_plan=True, plan_phrase='给价位', horizon_expression='短线', indicator_mentions=['SMA3'],
            strict_scope=True, scope_phrase='只用SMA3'))
        self.assertEqual(intent.plan_blocked, 'risk_atr_not_authorized')
        self.assertEqual(intent.indicator_mentions, ('SMA3',))
        self.assertEqual(intent.horizon, '未来10个交易日')

    def test_historical_dates_and_future_horizons_are_separate(self):
        question = '平安银行用2026-08-01至2026-09-20的资料判断未来两三个月'
        intent = checked(question, draft(objective_kind='decision', objective_phrase='判断',
            horizon_expression='未来两三个月', date_expression='2026-08-01至2026-09-20'))
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
    def previous(self, question='它给参考价', ctx=None, **changes):
        return checked(question, draft(subject=dict(source='previous', mentions=[], selection='它'),
            objective_kind='decision', objective_phrase='参考价', wants_plan=True, plan_phrase='参考价', **changes),
            ctx or context(previous=True))

    def test_901_seconds_keeps_identity_but_drops_old_horizon(self):
        for age, expected in ((899, '未来40个交易日'), (900, None), (901, None)):
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
        value = draft(subject=dict(source='previous', mentions=[], selection='第二只'))
        self.assertEqual(checked('研究第二只', value, ctx).bound_subjects, ('600000.SH',))
        value['subject']['selection'] = '它们'
        self.assertEqual(checked('研究它们', value, ctx).bound_subjects, ('000001.SZ', '600000.SH'))
        value['subject']['selection'] = '第三只'
        self.assertEqual(checked('研究第三只', value, ctx).bound_subjects, ())
        value['subject']['selection'] = '第十只'
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
        value = draft(subject=dict(source='previous', mentions=[], selection=None), objective_kind='decision',
                      wants_plan=True, horizon_expression='中线')
        intent = checked('中线，禁用ATR', value, ctx)
        self.assertEqual(intent.bound_subjects, ('000001.SZ',))
        self.assertEqual(intent.horizon, '未来40个交易日')
        self.assertEqual(intent.plan_blocked, 'risk_atr_not_authorized')
        self.assertIs(ctx.session.product_pending, pending)
        wrong = draft(subject=dict(source='previous', mentions=[], selection='第二只'))
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
        value = draft(subject=dict(source='previous', mentions=[], selection='第二只'),
                      research_kinds=[dict(kind='technical', phrase='技术面')])
        intent = checked('研究第二只的技术面', value, ctx)
        self.assertEqual(intent.bound_subjects, ('600000.SH',))
        for changed in (replace(ctx, quote_source=object()), replace(ctx, now=NOW+timedelta(minutes=15))):
            self.assertEqual(checked('研究第二只的技术面', value, changed).bound_subjects, ())
        self.assertEqual(ctx.model_context()['shortlist']['names'], [s.name for s in SECURITIES[:3]])


class AttemptTests(unittest.TestCase):
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
        self.assertEqual(run.status, 'invalid')
        self.assertIsNone(run.intent)

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
