"""Independent v2.6.6 domain, screening and extracted-duration boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests
from test_research_intent_v26 import pending_context, previous_value
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from trading_companion.security_directory import SecurityDirectory
from trading_companion.workflow_evaluation_fixtures import SECURITIES


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
