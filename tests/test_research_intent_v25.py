"""Candidate-only capability, language and state boundaries; no sealed inputs."""

from contextlib import ExitStack
from datetime import timedelta
import unittest

from scripts.verify import clean_environment, offline_network
from test_research_intent_v2 import context, draft, response
from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError
from trading_companion import research_intent_v2 as intent
from trading_companion.research_intent import _dates
from trading_companion.workflow_evaluation_fixtures import SECURITIES


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())

    def check(self, question, value=None, ctx=None, **kwargs):
        return intent.check_response(question, response(value or draft()), context=ctx or context(), **kwargs)

    def test_products_are_rejected_after_grounding_without_binding_the_index(self):
        for product in ('ETF', '指数基金', '期货', '期权'):
            question = '研究沪深300' + product + '未来2周的走势'
            value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
                          horizon_expression='未来2周', objective_kind='outlook', objective_phrase='未来2周的走势')
            log = []
            result = self.check(question, value, corrections=log)
            self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
            self.assertIn('unsupported_product', [c['kind'] for c in log])
            self.assertIsNone(intent.local_preflight(question, context()))
            with self.assertRaises(InputIssue):
                self.check(question, dict(value, objective_phrase='不存在的依据'))
            with self.assertRaises(InputIssue):
                self.check(question, dict(value, extra=True))

    def test_concepts_background_and_mixed_product_research(self):
        for question in ('ETF是什么', '期货合约和现货的区别是什么？', '基金常见风险有哪些？'):
            local = intent.local_preflight(question, context())
            self.assertEqual((local.task, local.zero_intent_calls), ('knowledge', True))
        question = '研究平安银行的公司概况，背景里提到期权激励'
        result = self.check(question, draft(research_kinds=[dict(kind='profile', phrase='公司概况')]))
        self.assertEqual((result.task, result.bound_subjects), ('stock_research', ('000001.SZ',)))
        question = '研究沪深300ETF，同时说明ETF是什么'
        result = self.check(question, draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None)))
        self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))

    def test_known_unsupported_index_is_local_but_unknown_identity_is_not_rejected(self):
        result = intent.local_preflight('研究中证500的走势', context())
        self.assertEqual((result.task, result.operation, result.outcome, result.zero_intent_calls),
                         ('unsupported', 'local', 'reject', True))
        self.assertIsNone(intent.local_preflight('研究科创板指数的日线', context()))
        for name in ('平安', '远帆科技'):
            result = self.check('研究'+name+'的新闻', draft(subject=dict(source='question', mentions=[name], selection=None),
                research_kinds=[dict(kind='news', phrase='新闻')], confidence='low', clarification_reason='identity'))
            self.assertEqual((result.task, result.outcome, result.bound_subjects), ('stock_research', 'clarify_identity', ()))

    def test_negative_lists_map_domains_and_indicators_and_keep_long_tail(self):
        question = '平安银行删掉主营业务和财务，去掉公告，移除EMA和ATR，' + '请保留其他要求，'*25 + '不再分析新闻，不用止损'
        exclusions = [dict(kind=k, phrase=p) for k, p in (
            ('profile', '删掉主营业务和财务'), ('financial', '删掉主营业务和财务'),
            ('announcements', '去掉公告'), ('indicator:ema', '移除EMA和ATR'),
            ('indicator:atr', '移除EMA和ATR'), ('news', '不再分析新闻'), ('plan', '不用止损'))]
        result = self.check(question, draft(exclusions=exclusions))
        self.assertEqual(set(result.exclusions), {v['kind'] for v in exclusions})
        self.assertFalse(result.wants_plan)
        self.assertEqual(result.outcome, 'research')

    def test_only_time_and_negated_only_do_not_grant_strict_scope_or_veto_the_indicator(self):
        question = '仅研究平安银行2026-08-10至2026-08-28的历史表现'
        result = self.check(question, draft(date_expression='2026-08-10至2026-08-28',
            strict_scope=True, scope_phrase='仅研究'))
        self.assertFalse(result.strict_scope)
        self.assertEqual(result.outcome, 'research')
        question = '平安银行不能只用BOLL，也结合新闻'
        value = draft(indicator_mentions=['BOLL'], research_kinds=[dict(kind='technical', phrase='BOLL'),
            dict(kind='news', phrase='新闻')], strict_scope=True, scope_phrase='只用BOLL')
        result = self.check(question, value)
        self.assertFalse(result.strict_scope)
        self.assertEqual(result.exclusions, ())
        self.assertEqual(result.indicator_mentions, ('BOLL',))
        self.assertEqual(result.outcome, 'research')
        with self.assertRaises(InputIssue):
            self.check(question, dict(value, exclusions=[dict(kind='indicator:boll', phrase='不能只用BOLL')]))

    def test_indicator_alias_veto_does_not_hit_a_shorter_indicator_and_repeats_are_masked(self):
        result = self.check('平安银行不用指数移动平均线', draft())
        self.assertEqual(result.exclusions, ('indicator:ema',))
        result = self.check('平安银行不看基本面', draft())
        self.assertEqual(set(result.exclusions), {'financial', 'profile'})
        result = self.check('平安银行不要RSI，再次强调不用RSI2，只用EMA5', draft(
            indicator_mentions=['EMA5'], research_kinds=[dict(kind='technical', phrase='EMA5')],
            strict_scope=True, scope_phrase='只用EMA5'))
        self.assertEqual(result.exclusions, ('indicator:rsi',))
        self.assertEqual(result.outcome, 'research')
        result = self.check('平安银行移除20日均线与EMA（13）和ATR14', draft(exclusions=[
            dict(kind=kind, phrase='移除20日均线与EMA（13）和ATR14')
            for kind in ('indicator:sma', 'indicator:ema', 'indicator:atr')]))
        self.assertEqual(set(result.exclusions), {'indicator:sma', 'indicator:ema', 'indicator:atr'})

    def test_strict_indicator_fully_removed_is_a_conflict_without_authorizing_a_default(self):
        result = self.check('平安银行只用RSI，但不要RSI', draft(strict_scope=True, scope_phrase='只用RSI',
            exclusions=[dict(kind='indicator:rsi', phrase='不要RSI')], clarification_reason='conflict'))
        self.assertEqual(result.outcome, 'clarify_conflict')
        self.assertEqual(result.indicator_mentions, ())
        self.assertTrue(result.strict_scope)

    def test_explicit_index_technical_survives_but_unsupported_domains_do_not(self):
        question = '沪深300只看日线'
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
                      research_kinds=[dict(kind='technical', phrase='日线')], strict_scope=True, scope_phrase='只看日线')
        log = []
        result = self.check(question, value, corrections=log)
        self.assertEqual(result.research_kinds, ('technical',))
        self.assertNotIn('index_default_domain', [c['kind'] for c in log])
        with self.assertRaises(InputIssue):
            self.check('沪深300财报', dict(value, strict_scope=False, scope_phrase=None,
                research_kinds=[dict(kind='financial', phrase='财报')]))

    def test_relative_this_is_candidate_only_and_not_a_partial_period(self):
        for expression, days in (('这三个月', 90), ('这2周', 14), ('这十五天', 15)):
            result = self.check('平安银行'+expression+'的日线', draft(date_expression=expression,
                research_kinds=[dict(kind='technical', phrase='日线')]))
            end = context().reference_date-timedelta(days=1)
            self.assertEqual(result.historical_window, ((end-timedelta(days=days-1)).isoformat(), end.isoformat()))
            self.assertEqual(result.date_expression, expression)
            with self.assertRaises((InputIssue, InvalidRequestError)):
                _dates(expression, expression, context().reference_date)
        for expression in ('这三个月以来', '本月', '这0天', '这999个月', '这三个月和近2周'):
            self.assertEqual(self.check('平安银行'+expression, draft(date_expression=expression)).outcome, 'clarify_conditions')

    def test_future_actual_data_cannot_become_a_price_scenario(self):
        question = '平安银行未来三天的实际收盘价是多少'
        result = self.check(question, draft(objective_kind='decision', objective_phrase='实际收盘价',
            wants_plan=True, plan_phrase='实际收盘价', horizon_expression='未来三天', date_expression='未来三天'))
        self.assertEqual((result.objective_kind, result.wants_plan, result.horizon, result.outcome),
                         ('analysis', False, None, 'clarify_conditions'))
        self.assertEqual(result.historical_window, ())
        self.assertIsNone(result.date_expression)
        question = '平安银行未来6天若跌破支撑，止损参考怎么设'
        result = self.check(question, draft(objective_kind='decision', objective_phrase='止损参考',
            wants_plan=True, plan_phrase='止损参考', horizon_expression='未来6天'))
        self.assertEqual((result.outcome, result.wants_plan, result.horizon), ('research', True, '未来6天'))
        question = '如果平安银行明天实际收盘价跌破10元，给止损参考'
        result = self.check(question, draft(objective_kind='decision', objective_phrase='止损参考',
            wants_plan=True, plan_phrase='止损参考', horizon_expression='明天'))
        self.assertEqual((result.outcome, result.wants_plan), ('research', True))
        date_text = (context().reference_date + timedelta(days=2)).isoformat()
        question = '平安银行'+date_text+'的实际收盘价'
        result = self.check(question, draft(objective_kind='decision', objective_phrase='实际收盘价',
            wants_plan=True, plan_phrase='实际收盘价', date_expression=date_text))
        self.assertEqual((result.objective_kind, result.wants_plan, result.outcome, result.date_expression),
                         ('analysis', False, 'clarify_conditions', date_text))

    def test_granularity_is_never_an_indicator_and_unknown_indicator_is_clarified(self):
        for phrase in ('日线', '周线', 'K线'):
            with self.assertRaises(InputIssue):
                self.check('平安银行'+phrase, draft(indicator_mentions=[phrase]))
        result = self.check('平安银行加TRIX，不用价位', draft(indicator_mentions=['TRIX'],
            research_kinds=[dict(kind='technical', phrase='TRIX')], exclusions=[dict(kind='plan', phrase='不用价位')]))
        self.assertEqual((result.indicator_mentions, result.outcome), (('TRIX',), 'clarify_conditions'))
        result = self.check('平安银行只用TRIX', draft(indicator_mentions=['TRIX'],
            research_kinds=[dict(kind='technical', phrase='TRIX')], strict_scope=True, scope_phrase='只用TRIX'))
        self.assertTrue(result.strict_scope)
        self.assertEqual(result.outcome, 'clarify_conditions')

    def test_same_single_reference_never_uses_an_ordinal_or_invalid_publication(self):
        for selection in ('同一只', '同一只股票'):
            value = draft(subject=dict(source='previous', mentions=[], selection=selection))
            self.assertEqual(self.check('继续研究'+selection, value, context(previous=True)).bound_subjects, ('000001.SZ',))
            for ctx in (context(), context(previous=True, securities=SECURITIES[:2]), context(previous=True, age=-1),
                        context(previous=True, source='akshare_eastmoney'), context(previous=True, conversation_memory=False)):
                self.assertEqual(self.check('继续研究'+selection, value, ctx).bound_subjects, ())


if __name__ == '__main__':
    unittest.main()
