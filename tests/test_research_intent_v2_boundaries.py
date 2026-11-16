"""Research intent v2 capability, transport, repair and clarification boundaries (formerly v2.5-v2.5.4 files).

Synthetic inputs only; no provider calls or sealed data."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import patch, Mock
import unittest

from test_deepseek_tool_model import provider_call, provider_payload
from test_research_intent_v2 import context, draft, response, OfflineTests
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel, retry_tool_arguments
from trading_companion.errors import InvalidRequestError
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.research_intent import _dates
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import ToolDefinition, ToolMessage
from trading_companion.turn_control import TurnControl, TurnLimits, request_timeout
from trading_companion.workflow_evaluation_fixtures import SECURITIES


# ---- v2.5: Candidate-only capability, language and state boundaries; no sealed inputs.

class BoundaryTests(OfflineTests):
    def check(self, question, value=None, ctx=None, **kwargs):
        return intent.check_response(question, response(value or draft()), context=ctx or context(), **kwargs)

    def test_products_are_rejected_after_grounding_without_binding_the_index(self):
        for product in ('ETF', '指数基金', '期货', '期权'):
            question = '研究沪深300' + product + '未来2周的走势'
            value = draft(task='unsupported', subject=dict(source='question', mentions=['沪深300'+product], selection=None),
                          horizon_expression='未来2周', objective_kind='outlook', objective_phrase='未来2周的走势')
            log = []
            result = self.check(question, value, corrections=log)
            self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
            self.assertEqual(result.diagnostics['product_mentions'], ['沪深300'+product])
            self.assertIsNone(intent.local_preflight(question, context()))
            with self.assertRaises(InputIssue):
                self.check(question, dict(value, objective_phrase='不存在的依据'))
            with self.assertRaises(InputIssue):
                self.check(question, dict(value, extra=True))

    def test_concepts_background_and_mixed_product_research(self):
        for question in ('ETF是什么', '期货合约和现货的区别是什么？', '基金常见风险有哪些？'):
            local = intent.local_preflight(question, context())
            self.assertIsNone(local)
            proposal = self.check(question, draft(task='knowledge', subject=dict(source='none', mentions=[], selection=None)))
            self.assertEqual((proposal.task, proposal.zero_intent_calls), ('knowledge', False))
        question = '研究平安银行的公司概况，背景里提到期权激励'
        result = self.check(question, draft(research_kinds=[dict(kind='profile', phrase='公司概况')]))
        self.assertEqual((result.task, result.bound_subjects), ('stock_research', ('000001.SZ',)))
        question = '研究沪深300ETF，同时说明ETF是什么'
        result = self.check(question, draft(task='unsupported', subject=dict(source='question', mentions=['沪深300ETF'], selection=None)))
        self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))

    def test_unsupported_index_uses_model_and_unknown_identity_stays_unbound(self):
        result = intent.local_preflight('研究中证500的走势', context())
        self.assertIsNone(result)
        result = self.check('研究中证500的走势', draft(task='unsupported', subject=dict(source='question', mentions=['中证500'], selection=None)))
        self.assertEqual((result.outcome, result.bound_subjects, result.zero_intent_calls), ('reject', (), False))
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

    def test_model_scope_is_not_reclassified_and_narrowing_never_adds_permission(self):
        question = '仅研究平安银行2026-08-10至2026-08-28的历史表现'
        result = self.check(question, draft(date_expression='2026-08-10至2026-08-28',
            strict_scope=True, scope_phrase='仅研究'))
        self.assertTrue(result.strict_scope)
        self.assertEqual(result.outcome, 'research')
        question = '平安银行不能只用BOLL，也结合新闻'
        value = draft(indicator_mentions=['BOLL'], research_kinds=[dict(kind='technical', phrase='BOLL'),
            dict(kind='news', phrase='新闻')], strict_scope=True, scope_phrase='只用BOLL')
        result = self.check(question, value)
        self.assertTrue(result.strict_scope)
        self.assertEqual(result.exclusions, ())
        self.assertEqual(result.indicator_mentions, ('BOLL',))
        self.assertEqual(result.outcome, 'research')
        narrowed = self.check(question, dict(value, exclusions=[dict(kind='indicator:boll', phrase='不能只用BOLL')]))
        self.assertEqual(narrowed.indicator_mentions, ())
        self.assertFalse(narrowed.wants_plan)

    def test_indicator_alias_veto_does_not_hit_a_shorter_indicator_and_repeats_are_masked(self):
        result = self.check('平安银行不用指数移动平均线', draft(exclusions=[dict(kind='indicator:ema', phrase='不用指数移动平均线')]))
        self.assertEqual(result.exclusions, ('indicator:ema',))
        result = self.check('平安银行不看基本面', draft(exclusions=[dict(kind=k, phrase='不看基本面') for k in ('financial', 'profile')]))
        self.assertEqual(set(result.exclusions), {'financial', 'profile'})
        result = self.check('平安银行不要RSI，再次强调不用RSI2，只用EMA5', draft(
            indicator_mentions=['EMA5'], research_kinds=[dict(kind='technical', phrase='EMA5')],
            strict_scope=True, scope_phrase='只用EMA5', exclusions=[dict(kind='indicator:rsi', phrase='不用RSI2')]))
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
                                          wants_plan=True, plan_phrase='实际收盘价'))
        self.assertEqual((result.objective_kind, result.wants_plan, result.horizon, result.outcome),
                         ('analysis', False, None, 'clarify_conditions'))
        result = self.check(question, draft(horizon_expression='未来三天', date_expression='未来三天'))
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
        result = self.check(question, draft(date_expression=date_text))
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


# ---- v2.5.1: Independent v2.5.1 boundary and transport fixtures; no provider or sealed data.

class TechnicalAndSubjectTests(OfflineTests):
    def check(self, question, value=None, ctx=None, **kwargs):
        return intent.check_response(question, response(value or draft()), context=ctx or context(), **kwargs)

    def test_indicators_supply_domain_but_granularity_needs_model_declaration_and_preserves_raw(self):
        for mention, task in (('平安银行', 'stock_research'), ('沪深300', 'index_research')):
            for cue in ('完整日线', '周线', 'K线', 'BOLL'):
                question = '研究' + mention + '的' + cue
                value = draft(task=task, subject=dict(source='question', mentions=[mention], selection=None),
                              indicator_mentions=['BOLL'] if cue == 'BOLL' else [])
                original, log = deepcopy(value), []
                with self.subTest(task=task, cue=cue):
                    result = self.check(question, value, corrections=log)
                    self.assertEqual(result.research_kinds, ('technical',) if cue == 'BOLL' else ())
                    declared = dict(value, research_kinds=[dict(kind='technical', phrase=cue)])
                    self.assertEqual(self.check(question, declared).research_kinds, ('technical',))
                    self.assertNotIn('explicit_technical_cue', [c['kind'] for c in log])
                    self.assertEqual(self.check(question, value, normalize=False).research_kinds, ())
                    self.assertEqual(value, original)

    def test_negation_and_price_dependencies_never_supply_a_positive_technical_cue(self):
        for question in ('研究平安银行的走势', '研究平安银行，不看日线', '研究平安银行，删除周线',
                         '研究平安银行，不用K线', '研究平安银行，不用ATR', '研究平安银行，不看技术面，但看看日线',
                         '研究平安银行，排除完整日线和周线数据', '研究平安银行，日线、周线和K线都不要'):
            with self.subTest(question=question):
                result = self.check(question)
                self.assertNotIn('technical', result.research_kinds)
        value = draft(objective_kind='decision', objective_phrase='能买么', wants_plan=True, plan_phrase='参考价',
                      horizon_expression='短线')
        self.assertEqual(self.check('平安银行短线能买么，给参考价', value).research_kinds, ())
        for cue in ('不能只看日线', '不要只用BOLL'):
            value = draft(indicator_mentions=['BOLL'] if 'BOLL' in cue else [])
            self.assertEqual(self.check('研究平安银行，' + cue, value).research_kinds,
                             ('technical',) if 'BOLL' in cue else ())

    def test_unknown_indicators_still_clarify_and_grounding_scope_failures_still_reject(self):
        value = draft(indicator_mentions=['神奇指标'])
        result = self.check('研究平安银行的神奇指标', value)
        self.assertEqual((result.research_kinds, result.outcome), (('technical',), 'clarify_conditions'))
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value)
        self.assertFalse(self.check('研究平安银行，只用BOLL', draft(indicator_mentions=['BOLL'])).strict_scope)
        conflict = self.check('研究平安银行，不用BOLL', draft(indicator_mentions=['BOLL'], exclusions=[dict(kind='indicator:boll', phrase='不用BOLL')]))
        self.assertEqual((conflict.outcome, conflict.indicator_mentions, conflict.exclusions),
                         ('clarify_conflict', (), ('indicator:boll',)))

    def test_group_and_ordinal_use_structured_reference_without_reparsing_counts(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for selection in ('这两只', '前面这两只', '它们', '这些', '这2只股票', '前面的这二只'):
            value = draft(reference=dict(kind='all', index=None, scope='latest_research'), subject=dict(source='previous', mentions=[], selection=selection))
            result = self.check('比较' + selection + '的情况', value, ctx)
            self.assertEqual(result.bound_subjects, tuple(s.instrument_id for s in SECURITIES[:2]))
        for selection in ('这三只', '这零只', '这两两只', '前面这1只', '这些'):
            value = draft(reference=dict(kind='all', index=None, scope='latest_research'), subject=dict(source='previous', mentions=[], selection=selection))
            target = context(previous=True) if selection == '这些' else ctx
            self.assertEqual(self.check('研究' + selection, value, target).bound_subjects, tuple(s.instrument_id for s in (SECURITIES[:1] if selection == '这些' else SECURITIES[:2])))
        value = draft(reference=dict(kind='ordinal', index=2, scope='latest_research'), subject=dict(source='previous', mentions=[], selection='第二只'))
        self.assertEqual(self.check('研究第二只', value, ctx).bound_subjects, (SECURITIES[1].instrument_id,))

    def test_group_reference_cannot_escape_invalid_context_or_consume_an_explicit_object(self):
        value = draft(reference=dict(kind='all', index=None, scope='latest_research'), subject=dict(source='previous', mentions=[], selection='前面这两只'))
        base = context(previous=True, securities=SECURITIES[:2])
        for ctx in (context(), replace(base, conversation_memory=False), replace(base, source='akshare_eastmoney'),
                    replace(base, development=True), replace(base, now=base.now-timedelta(seconds=1))):
            self.assertEqual(self.check('研究前面这两只', value, ctx).bound_subjects, ())
        value = draft(reference=dict(kind='all', index=None, scope='latest_research'), subject=dict(source='previous', mentions=[], selection='这些'))
        with self.assertRaises(InputIssue):
            self.check('这些和平安银行比较', value, base)

    def test_explicit_selection_rejects_even_redundancy_and_preserves_raw(self):
        value = draft(subject=dict(source='question', mentions=['平安银行'], selection='平安银行'))
        original, log = deepcopy(value), []
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value, corrections=log)
        self.assertEqual(log, [])
        self.assertEqual(value, original)
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value, normalize=False)
        for selection in ('平安', '第二只', '不存在'):
            with self.assertRaises(InputIssue):
                self.check('研究平安银行', draft(subject=dict(source='question', mentions=['平安银行'], selection=selection)))
        with self.assertRaises(InputIssue):
            self.check('平安银行和浦发银行比较', value)

    def test_unknown_index_rejects_selection_and_cannot_gain_technical_default(self):
        name = '那个科技指数'
        value = draft(task='index_research', confidence='low', clarification_reason='identity',
                      subject=dict(source='question', mentions=[name], selection=name))
        with self.assertRaises(InputIssue):
            self.check('研究' + name + '的日线', value)
        value['subject']['selection'] = None
        result = self.check('研究' + name + '的日线', value)
        self.assertEqual((result.task, result.bound_subjects, result.outcome, result.research_kinds),
                         ('index_research', (), 'clarify_identity', ()))
        with self.assertRaises(InputIssue):
            self.check('研究沪深300的日线', value)


class ArgumentRepairTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.config = DeepSeekConfig('deepseek-flash', 'synthetic-not-a-real-credential')
        self.events = []
        self.messages = (ToolMessage('user', '人工边界测试'),)

    def parse(self, arguments, *, enabled=True, finish='tool_calls'):
        payload = provider_payload(finish=finish, content=None,
            calls=[provider_call(name=intent.TOOL_NAME, arguments=arguments)])
        original = deepcopy(payload)
        model = DeepSeekToolModel(self.config, response_diagnostics=True, repair_tool_arguments=enabled,
                                  argument_diagnostic_sink=self.events.append)
        with patch('trading_companion.deepseek_tool_model._post_chat', return_value=payload) as post:
            try:
                return model.complete(self.messages, tools=(intent.TOOL,))
            finally:
                post.assert_called_once()
                self.assertEqual(payload, original)

    def test_strict_first_and_three_bounded_repair_categories(self):
        self.assertEqual(self.parse('{"value":1}').tool_calls[0].arguments, {'value': 1})
        self.assertEqual(self.events, [])
        for text, kind, value in (('{"text":"甲\n乙"}', 'control_char', {'text': '甲\n乙'}),
                                  ('```json\n{"value":1}\n```', 'code_fence', {'value': 1}),
                                  ('{"value":1}\n以上是参数说明。', 'trailing_text', {'value': 1})):
            with self.subTest(kind=kind):
                self.events.clear()
                self.assertEqual(self.parse(text).tool_calls[0].arguments, value)
                event = self.events[0]
                self.assertEqual(event['repair'], kind)
                self.assertEqual(event['correction'], 'tool_arguments_repaired')
                self.assertEqual(event['arguments_length'], len(text))
                self.assertEqual(event['finish_reason'], 'tool_calls')
                if kind == 'control_char':
                    self.assertEqual(event['category'], 'control_char')

    def test_truncated_multiple_duplicate_nonobject_and_oversized_arguments_stay_failed(self):
        cases = ('{"x":', '{"x":"未结束', '{"x":1}{"x":2}', '{"x":1} 说明 {"x":2}',
                 '{"x":1} [2]', '{"x":1} null', '{"x":1},', '{"x":1} :',
                 '前缀说明 {"x":1}', '```json\n{"x":1}',
                 '```json\n{"x":1,"x":2}\n```', '{"x":1,"x":2}\n说明', '{"x":1,"x":2}',
                 '{"x":"甲\n乙","x":2}', '[1,2]', '{"x":NaN}', '{"x":"' + 'a'*10001 + '"}',
                 '```json\n' + '{"x":'*14 + '1' + '}'*14 + '\n```')
        for text in cases:
            self.events.clear()
            with self.subTest(text=text[:25]), self.assertRaises(ModelCallError):
                self.parse(text)
            self.assertFalse(any(e['correction'] == 'tool_arguments_repaired' for e in self.events))

    def test_diagnostics_are_allowlisted_and_never_retain_body_parser_message_or_credentials(self):
        text = '{"private-body": "secret-value"'
        with self.assertRaises(ModelCallError) as caught:
            self.parse(text)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(set(self.events[0]), {'call_index', 'arguments_length', 'finish_reason', 'category',
                                              'position', 'line', 'column', 'repair', 'correction'})
        encoded = json.dumps(self.events) + str(caught.exception)
        for sensitive in ('private-body', 'secret-value', self.config.api_key, 'Expecting'):
            self.assertNotIn(sensitive, encoded)
        self.assertIsInstance(self.events[0]['position'], int)

    def test_interruption_never_parses_or_repairs_even_a_complete_looking_object(self):
        for reason in ('length', 'aborted', 'insufficient_system_resource'):
            self.events.clear()
            result = self.parse('{"x":1}', finish=reason)
            self.assertEqual((result.status, result.tool_calls), ('incomplete', ()))
            self.assertEqual(self.events[0]['category'], 'interrupted')
            self.assertIsNone(self.events[0]['repair'])
            self.assertIsNone(self.events[0]['position'])

    def test_repair_is_disabled_by_default_and_requires_current_candidate_and_diagnostics(self):
        with self.assertRaises(ModelCallError):
            self.parse('```json\n{"x":1}\n```', enabled=False)
        self.assertFalse(DeepSeekToolModel(self.config).repair_tool_arguments)
        for kwargs in (dict(repair_tool_arguments=True), dict(repair_tool_arguments='yes'),
                       dict(argument_diagnostic_sink='not-callable')):
            with self.assertRaises(ModelConfigurationError):
                DeepSeekToolModel(self.config, **kwargs)
        model = DeepSeekToolModel(self.config, response_diagnostics=True, repair_tool_arguments=True,
                                  argument_diagnostic_sink=self.events.append)
        for tool in (ToolDefinition('other', 'fixture', {'type': 'object', 'properties': {}}),
                     ToolDefinition(intent.TOOL_NAME, 'old schema', dict(intent.SCHEMA,
                         properties=dict(intent.SCHEMA['properties'], version={'type': 'string', 'enum': ['research-intent-v2.5']})))):
            with patch('trading_companion.deepseek_tool_model._post_chat', side_effect=AssertionError('No transport')):
                with self.assertRaises(ModelConfigurationError):
                    model.complete(self.messages, tools=(tool,))

    def test_repaired_transport_still_runs_full_intent_semantic_validation(self):
        raw = json.dumps(draft(objective_phrase='不存在的原文'), ensure_ascii=False)
        proposal = self.parse('```json\n' + raw + '\n```')
        with self.assertRaises(InputIssue):
            intent.check_response('研究平安银行', proposal, context=context())


# ---- v2.5.2: Independent same-turn conflict and recent-research ordinal boundaries.

class ConflictTests(OfflineTests):
    def check(self, question, value, ctx=None, **kwargs):
        return intent.check_response(question, response(value), context=ctx or context(), **kwargs)

    def test_domain_overlap_clarifies_and_keeps_strict_scope_veto_subjects_and_original(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        value = draft(reference=dict(kind='all', index=None, scope='latest_research'), subject=dict(source='previous', mentions=[], selection='前面这两只'), operation='update',
            research_kinds=[dict(kind='financial', phrase='只看财务')], strict_scope=True,
            scope_phrase='只看财务', exclusions=[dict(kind='financial', phrase='不要财务')])
        original, log = deepcopy(value), []
        result = self.check('前面这两只的比较只看财务，但不要财务', value, ctx, corrections=log)
        self.assertEqual((result.outcome, result.strict_scope, result.research_kinds, result.exclusions),
                         ('clarify_conflict', True, (), ('financial',)))
        self.assertEqual(result.bound_subjects, tuple(s.instrument_id for s in SECURITIES[:2]))
        self.assertEqual(result.operation, 'update')
        self.assertIn('domain_conflict', [c['kind'] for c in log])
        self.assertEqual(value, original)
        self.assertEqual(self.check('前面这两只的比较只看财务，但不要财务', value, ctx, normalize=False).outcome, 'clarify_conflict')

    def test_domain_overlap_keeps_independent_domains_and_conflict_beats_other_clarification(self):
        for reason in (None, 'horizon', 'conditions', 'conflict'):
            value = draft(research_kinds=[dict(kind='news', phrase='新闻'), dict(kind='financial', phrase='财务')],
                          clarification_reason=reason, exclusions=[dict(kind='financial', phrase='不要财务')])
            result = self.check('平安银行研究新闻和财务，但不要财务', value)
            self.assertEqual((result.outcome, result.research_kinds, result.exclusions),
                             ('clarify_conflict', ('news',), ('financial',)))

    def test_indicator_overlap_removes_all_parameters_of_excluded_kind_without_default(self):
        value = draft(indicator_mentions=['RSI8', 'RSI14'], strict_scope=True, scope_phrase='只用RSI8和RSI14',
                      exclusions=[dict(kind='indicator:rsi', phrase='不要RSI')])
        original, log = deepcopy(value), []
        result = self.check('平安银行只用RSI8和RSI14，但不要RSI', value, corrections=log)
        self.assertEqual((result.outcome, result.indicator_mentions, result.research_kinds, result.strict_scope),
                         ('clarify_conflict', (), (), True))
        self.assertIn('indicator:rsi', result.exclusions)
        self.assertEqual(result.diagnostics['narrowing']['exclusions'][0]['kind'], 'indicator:rsi')
        self.assertEqual(value, original)
        self.assertEqual(self.check('平安银行只用RSI8和RSI14，但不要RSI', value, normalize=False).indicator_mentions, ())

    def test_mixed_indicator_conflict_never_recovers_another_positive_fragment(self):
        value = draft(indicator_mentions=['SMA3,RSI8'], strict_scope=True, scope_phrase='只用SMA3,RSI8',
                      exclusions=[dict(kind='indicator:rsi', phrase='不用RSI')])
        result = self.check('平安银行只用SMA3,RSI8，但不用RSI', value)
        self.assertEqual((result.outcome, result.indicator_mentions, result.strict_scope),
                         ('clarify_conflict', (), True))
        self.assertEqual(result.research_kinds, ())

    def test_overlap_does_not_hide_fabricated_evidence_bad_shape_or_dropped_scope(self):
        question = '平安银行只看财务，但不要财务'
        base = draft(research_kinds=[dict(kind='financial', phrase='财务')],
                     exclusions=[dict(kind='financial', phrase='不要财务')], strict_scope=True, scope_phrase='只看财务')
        for bad in (dict(base, extra=True), dict(base, scope_phrase='不在原文中'),
                    dict(base, exclusions=[dict(kind='financial', phrase='财务')]),
                    dict(base, research_kinds=[dict(kind='financial', phrase='不存在的财务')]),
                    dict(base, strict_scope=True, scope_phrase=None)):
            with self.subTest(bad=bad), self.assertRaises(InputIssue):
                self.check(question, bad)
        self.assertFalse(self.check(question, dict(base, strict_scope=False)).strict_scope)
        with self.assertRaises(InputIssue):
            self.check('平安银行只用SMA3，但不用RSI', draft(indicator_mentions=['RSI8']))

    def test_splitting_a_mixed_phrase_cannot_exceed_the_original_array_limit(self):
        phrase = 'RSI8,' + ','.join('SMA' + str(n) for n in range(3, 16))
        value = draft(indicator_mentions=[phrase], exclusions=[dict(kind='indicator:rsi', phrase='不用RSI')])
        with self.assertRaises(InputIssue):
            self.check('平安银行研究' + phrase + '，但不用RSI', value)


class OrdinalTests(OfflineTests):
    def check(self, selection, ctx=None, *, index=2, scope='latest_research', **kwargs):
        value = draft(reference=dict(kind='ordinal', index=index, scope=scope), operation='update', subject=dict(source='previous', mentions=[], selection=selection),
                      indicator_mentions=['RSI8'])
        return intent.check_response(selection + '，补上RSI8', response(value),
                                     context=ctx or context(previous=True, securities=SECURITIES[:2]), **kwargs)

    def test_neutral_latest_research_prefix_selects_one_checked_member(self):
        for selection in ('上一份比较里的第二只', '上次研究的第2个', '刚才比较中的第二只',
                          '前面的比较结果里的第二只', '上一份报告的第二只', '上次的第二只'):
            log = []
            with self.subTest(selection=selection):
                result = self.check(selection, corrections=log)
                self.assertEqual(result.bound_subjects, (SECURITIES[1].instrument_id,))
                self.assertEqual(result.outcome, 'research')
                self.assertEqual(result.indicator_mentions, ('RSI8',))
                self.assertEqual([item['kind'] for item in log], ['explicit_indicator_domain'])
        self.assertEqual(self.check('上一份比较里的第二只', normalize=False).bound_subjects, (SECURITIES[1].instrument_id,))

    def test_recent_comparison_never_selects_a_coexisting_screening_list(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.screening_saved = object()  # Must not be consulted by this reference.
        with patch.object(intent, '_reference_scope', wraps=intent._reference_scope) as scopes:
            self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, (SECURITIES[1].instrument_id,))
        self.assertEqual({call.args[1] for call in scopes.call_args_list}, {'latest_research'})

    def test_out_of_range_single_subject_invalid_identity_and_context_still_clarify(self):
        result = self.check('上一份比较里的第0只', index=0)
        self.assertEqual((result.outcome, result.bound_subjects), ('clarify_subject', ()))
        for selection, index in (('上一份比较里的第四只', 4), ('上次研究的第十个', 10)):
            self.assertEqual(self.check(selection, index=index).outcome, 'clarify_subject')
        base = context(previous=True, securities=SECURITIES[:2])
        changed = replace(SECURITIES[0], name='已更名的人工样例')
        directory = SecurityDirectory((changed, *SECURITIES[1:]), 'changed', base.now, False)
        invalid = (context(), context(previous=True, kind='index'),
                   replace(base, conversation_memory=False), replace(base, source='akshare_eastmoney'),
                   replace(base, development=True), replace(base, now=base.now-timedelta(seconds=1)),
                   replace(base, directory=directory))
        for ctx in invalid:
            with self.subTest(ctx=ctx):
                self.assertEqual(self.check('上一份比较里的第一只', ctx, index=1).outcome, 'clarify_subject')
        base.session._subject_current = False
        self.assertEqual(self.check('上一份比较里的第一只', base, index=1).outcome, 'clarify_subject')

    def test_conflict_precedes_neutral_prefix_and_missing_research_cannot_fall_back_to_list(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.product_pending, ctx.session.index_pending = object(), object()
        result = self.check('上一份比较里的第二只', ctx)
        self.assertEqual((result.bound_subjects, result.outcome), ((), 'clarify_conflict'))
        ctx.session.clear_pending()
        self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, (SECURITIES[1].instrument_id,))
        ctx = context()
        ctx.session.screening_saved = object()
        with patch.object(intent, '_reference_scope', wraps=intent._reference_scope):
            self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, ())

    def test_selection_never_changes_structured_scope_and_cannot_fabricate_evidence(self):
        # Selection is evidence only; a list reference must explicitly name its scope.
        for selection in ('上次筛选名单里的第二只', '上一份归档报告2024里的第二只'):
            self.assertEqual(self.check(selection, scope='shortlist').bound_subjects, ())
            self.assertEqual(self.check(selection).bound_subjects, (SECURITIES[1].instrument_id,))
        for selection in ('上一份浦发银行比较里的第二只', '刚才沪深300报告里的第二只'):
            with self.assertRaises(InputIssue):
                self.check(selection)
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.screening_saved = object()
        self.assertEqual(self.check('上次筛选名单里的第二只', ctx, scope='shortlist').bound_subjects, ())
        with self.assertRaises(InputIssue):
            intent.check_response('继续研究', response(draft(subject=dict(source='previous', mentions=[], selection='第二只'))), context=ctx)


# ---- v2.5.3: Offline format retries, bounded evaluation diagnostics and clarification priorities.

def payload(arguments, finish='tool_calls', **changes):
    return provider_payload(finish=finish, content=None,
        calls=[provider_call(name=intent.TOOL_NAME, arguments=arguments)], **changes)


class TransportTests(OfflineTests):
    def model(self, sink, **options):
        return DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only'),
            response_diagnostics=True, argument_diagnostic_sink=sink, **options)

    def test_capture_is_opt_in_and_only_saves_failed_arguments_not_response_or_exception(self):
        text = '{"model-only-value":'
        for scope in (None, 'public', 'sealed'):
            events = []
            model = self.model(events.append, evaluation_scope=scope, save_failed_arguments=scope is not None)
            raw = dict(payload(text), unrelated_http_body='must-not-be-saved')
            with patch('trading_companion.deepseek_tool_model._post_chat', return_value=raw):
                with self.assertRaises(ModelCallError) as caught:
                    model.complete((ToolMessage('user', 'synthetic'),), tools=(intent.TOOL,))
            self.assertEqual(caught.exception.code, 'tool_arguments_json_invalid')
            self.assertEqual('arguments_raw' in events[0], scope is not None)
            if scope is not None:
                self.assertEqual(events[0]['arguments_raw'], text)
                self.assertFalse(events[0]['arguments_raw_truncated'])
            encoded = json.dumps(events) + str(caught.exception)
            self.assertNotIn('must-not-be-saved', encoded)
            self.assertNotIn('synthetic-only', encoded)
            self.assertNotIn('Expecting', encoded)

    def test_capture_requires_evaluation_diagnostics_sink_and_current_tool(self):
        config = DeepSeekConfig('deepseek-flash', 'synthetic-only')
        for options in (dict(save_failed_arguments=True),
                        dict(save_failed_arguments=True, evaluation_scope='public', response_diagnostics=True),
                        dict(save_failed_arguments='yes'), dict(evaluation_scope='production')):
            with self.assertRaises(ModelConfigurationError):
                DeepSeekToolModel(config, **options)
        model = self.model(lambda e: None, evaluation_scope='public', save_failed_arguments=True)
        old = ToolDefinition(intent.TOOL_NAME, 'old', dict(intent.SCHEMA,
            properties=dict(intent.SCHEMA['properties'], version={'type': 'string', 'enum': ['research-intent-v2.5.2']})))
        with patch('trading_companion.deepseek_tool_model._post_chat') as post:
            with self.assertRaises(ModelConfigurationError):
                model.complete((ToolMessage('user', 'synthetic'),), tools=(old,))
        post.assert_not_called()
        self.assertFalse(DeepSeekToolModel(config).save_failed_arguments)

    def test_capture_bound_and_no_capture_for_success_repair_size_or_other_protocol_errors(self):
        events = []
        model = self.model(events.append, evaluation_scope='public', save_failed_arguments=True, repair_tool_arguments=True)
        maximum = '{"x":' + ' ' * 9995
        with patch('trading_companion.deepseek_tool_model._post_chat', return_value=payload(maximum)):
            with self.assertRaises(ModelCallError):
                model.complete((ToolMessage('user', 'synthetic'),), tools=(intent.TOOL,))
        self.assertEqual(len(events[0]['arguments_raw']), 10000)
        for text in ('{"x":1}', '```json\n{"x":1}\n```', '[1]', '{"x":1,"x":2}', maximum+' '):
            events.clear()
            with patch('trading_companion.deepseek_tool_model._post_chat', return_value=payload(text)):
                try:
                    model.complete((ToolMessage('user', 'synthetic'),), tools=(intent.TOOL,))
                except ModelCallError as error:
                    self.assertNotEqual(error.code, 'tool_arguments_json_invalid')
            self.assertFalse(any('arguments_raw' in event for event in events))


class RetryTests(OfflineTests):


    def test_cancellation_and_deadline_after_first_failure_prevent_retry_in_same_turn(self):
        for cancel in (False, True):
            clock = [0.0]
            runtime = TurnControl(TurnLimits(model_calls=2, timeout_seconds=1), clock=lambda: clock[0])
            def fail(*args, **kwargs):
                if cancel:
                    runtime.cancel()
                else:
                    clock[0] = 1.0
                raise ModelCallError('tool_arguments_json_invalid', 'safe')
            delegate = Mock(complete=Mock(side_effect=fail))
            run = intent.understand('研究平安银行', model=delegate, context=context(), control=runtime,
                                    retry_json_arguments=True)
            self.assertEqual(run.code, 'turn_cancelled' if cancel else 'turn_deadline_exceeded')
            self.assertEqual(run.runtime['model_calls_attempted'], 1)
            delegate.complete.assert_called_once()

    def test_second_request_uses_remaining_time_and_cannot_publish_after_shared_deadline(self):
        clock, timeouts = [0.0], []
        runtime = TurnControl(TurnLimits(model_calls=2, timeout_seconds=2), clock=lambda: clock[0])
        class TimedModel:
            def complete(self, messages, *, tools):
                timeouts.append(request_timeout(10))
                if len(timeouts) == 1:
                    clock[0] = .5
                    raise ModelCallError('tool_arguments_json_invalid', 'safe')
                clock[0] = 2.0
                return response()
        run = intent.understand('研究平安银行', model=TimedModel(), context=context(), control=runtime,
                                retry_json_arguments=True)
        self.assertEqual(timeouts, [2, 1.5])
        self.assertEqual((run.status, run.code), ('failed', 'turn_deadline_exceeded'))
        self.assertIsNone(run.intent)
        self.assertEqual(run.runtime['model_calls_attempted'], 2)


class ClarificationTests(OfflineTests):
    def test_unknown_index_preserves_category_without_identity_or_domain_repair(self):
        for name in ('那个科技指数', '某个制造业指数'):
            question = '研究一下' + name + '的日线'
            value = draft(task='clarify', clarification_reason='subject', confidence='low',
                          subject=dict(source='question', mentions=[name], selection=None))
            original, log = deepcopy(value), []
            checked = intent.check_response(question, response(value), context=context(), corrections=log)
            self.assertEqual((checked.task, checked.bound_subjects, checked.outcome, checked.research_kinds),
                             ('index_research', (), 'clarify_identity', ()))
            self.assertNotIn('clarification_task', [e['kind'] for e in log])
            self.assertEqual(value, original)
            self.assertEqual(intent.check_response(question, response(value), context=context(), normalize=False).task, 'clarify')

    def test_unknown_task_recovery_never_grants_fund_identity_or_unsupported_index_domains(self):
        name = '某个制造业指数基金'
        value = draft(task='clarify', clarification_reason='subject', subject=dict(source='question', mentions=[name], selection=None))
        with self.assertRaises(InputIssue):
            intent.check_response('研究'+name+'的日线', response(value), context=context())
        value['task'] = 'unsupported'
        result = intent.check_response('研究'+name+'的日线', response(value), context=context())
        self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
        value = draft(task='index_research', clarification_reason='subject', subject=dict(source='question', mentions=['某个科技指数'], selection=None),
                      research_kinds=[dict(kind='financial', phrase='财务')])
        with self.assertRaises(InputIssue):
            intent.check_response('研究某个科技指数的财务', response(value), context=context())
        value = draft(task='clarify', clarification_reason='subject', subject=dict(source='question', mentions=['虚构科技公司'], selection=None))
        self.assertEqual(intent.check_response('研究虚构科技公司', response(value), context=context()).task, 'clarify')
        value['subject']['mentions'] = ['指数科技公司']
        self.assertEqual(intent.check_response('研究指数科技公司', response(value), context=context()).task, 'clarify')

    def test_invalid_quantified_horizon_beats_missing_horizon_without_inheriting_previous(self):
        for expression in ('未来0天', '未来零天', '未来999天', '未来8至2天'):
            question = '平安银行'+expression+'值得买入吗'
            value = draft(objective_kind='decision', objective_phrase='值得买入吗', wants_plan=True,
                          plan_phrase='值得买入吗', horizon_expression=expression, clarification_reason='horizon')
            log = []
            result = intent.check_response(question, response(value), context=context(previous=True, horizon='未来40个交易日'), corrections=log)
            self.assertEqual((result.horizon, result.outcome), (None, 'clarify_conditions'))
            self.assertIsNone(result.horizon)
            self.assertEqual(value['horizon_expression'], expression)
            self.assertEqual(intent.check_response(question, response(value), context=context(), normalize=False).outcome, 'clarify_conditions')

    def test_missing_or_vague_horizon_remains_missing_and_conflict_or_identity_has_priority(self):
        for expression in (None, '以后'):
            value = draft(objective_kind='decision', objective_phrase='值得买入吗', wants_plan=True,
                          plan_phrase='值得买入吗', horizon_expression=expression)
            q = '平安银行'+(expression or '')+'值得买入吗'
            self.assertEqual(intent.check_response(q, response(value), context=context()).outcome, 'clarify_horizon')
        value = draft(objective_kind='decision', objective_phrase='值得买入吗', wants_plan=True, plan_phrase='值得买入吗',
                      horizon_expression='未来0天', clarification_reason='horizon', exclusions=[dict(kind='plan', phrase='不要计划')])
        self.assertEqual(intent.check_response('平安银行未来0天值得买入吗，但不要计划', response(value), context=context()).outcome, 'clarify_conflict')
        value['exclusions'] = []
        value['subject']['mentions'] = ['虚构科技公司']
        self.assertEqual(intent.check_response('虚构科技公司未来0天值得买入吗', response(value), context=context()).outcome, 'clarify_identity')


# ---- v2.5.4: Independent scalar repair, unused evidence and date-prefix boundaries.

def bare(value=None, version=None):
    value = value or draft(plan_phrase='不用价位', exclusions=[dict(kind='plan', phrase='不用价位')])
    if version:
        value = dict(value, version='research-intent-' + version)
    return json.dumps(value, ensure_ascii=False).replace('"plan_phrase": "不用价位"', '"plan_phrase": 不用价位')


class ScalarTests(OfflineTests):
    def parse(self, text, *, enabled=True, calls=None):
        events = []
        model = DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only'),
            response_diagnostics=True, repair_tool_arguments=enabled, argument_diagnostic_sink=events.append)
        self.events = events
        item = payload(text) if calls is None else provider_payload(finish='tool_calls', content=None, calls=calls)
        with patch('trading_companion.deepseek_tool_model._post_chat', return_value=item):
            return model.complete((ToolMessage('user', 'synthetic'),), tools=(intent.TOOL,))

    def test_one_value_and_200_characters_at_an_actual_nested_object_key(self):
        for text, expected in (('{"a":不用价位}', dict(a='不用价位')),
                               ('{"a":{"b": 说明文字 ,"c":true}}', dict(a=dict(b='说明文字', c=True))),
                               ('{"a":' + '甲'*200 + '}', dict(a='甲'*200)),
                               ('{"a":"escaped\\\"string: fake", "b":unused}', dict(a='escaped"string: fake', b='unused'))):
            result = self.parse(text)
            self.assertEqual(result.tool_calls[0].arguments, expected)
            self.assertEqual(len(self.events), 1)
            self.assertEqual((self.events[0]['repair'], self.events[0]['correction']),
                             ('unquoted_scalar', 'tool_arguments_repaired'))
            try:
                json.loads(text)
            except json.JSONDecodeError as error:
                self.assertEqual(self.events[0]['position'], error.pos)

    def test_forbidden_characters_arrays_fake_keys_truncation_and_multiple_errors_never_repair(self):
        cases = ['{"a":' + c + '}' for c in ('甲"乙', "甲'乙", '甲[乙', '甲]乙', '甲{乙', '甲:乙', '甲\\乙',
                    '甲\n乙', '甲\r乙', '甲'*201, '', 'trueX', 'nullX', '+2', '01', '-02', '.5',
                    'TRUE', 'False', 'NULL')]
        cases += ['[unused]', '{"a":[unused]}', '[{"a":unused}]', '{"a":"fake \\"x\\": unused", nope}',
                  '{"a":unused', '{"a":unused,"b":unused}', '{"a":unused}说明',
                  '```json\n{"a":unused}\n```', '{"a":unused,"b":"甲\n乙"}',
                  '{"a":"甲\n乙","b":unused}', '{"a":unused}{"b":1}']
        for text in cases:
            with self.subTest(text=text[:35]), self.assertRaises(ModelCallError):
                self.parse(text)
            self.assertFalse(any(e['correction'] for e in self.events))

    def test_literals_numbers_and_valid_strings_pass_strictly_without_repairs(self):
        for scalar in ('true', 'false', 'null', '12', '-1.2', '1e2', '"unused"'):
            self.assertEqual(self.parse('{"a":' + scalar + '}').tool_calls[0].arguments['a'], json.loads(scalar))
            self.assertEqual(self.events, [])
        self.assertEqual(self.parse('{"fake \\"a\\": unused":1}').tool_calls[0].arguments, {'fake "a": unused':1})
        self.assertEqual(self.events, [])

    def test_protocol_limits_duplicates_nonfinite_and_identifiers_still_reject(self):
        cases = ('{"a":unused,"a":1}', '{"a":unused,"b":NaN}', '{"a":unused,"b":1e999}',
                 '{"a":unused,"b":' + '['*14 + '1' + ']'*14 + '}',
                 '{"a":unused,"b":"' + 'x'*10001 + '"}')
        for text in cases:
            with self.subTest(text=text[:30]), self.assertRaises(ModelCallError):
                self.parse(text)
            self.assertFalse(any(e['correction'] for e in self.events))
        for call in (provider_call(call_id='bad id', name=intent.TOOL_NAME, arguments='{"a":unused}'),
                     provider_call(name='other_tool', arguments='{"a":unused}')):
            with self.assertRaises(ModelCallError):
                self.parse('', calls=[call])
            self.assertFalse(any(e['correction'] for e in self.events))

    def test_disabled_by_default_and_old_version_cannot_enable_transport(self):
        with self.assertRaises(ModelCallError):
            self.parse('{"a":unused}', enabled=False)
        self.assertFalse(DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only')).repair_tool_arguments)
        old = ToolDefinition(intent.TOOL_NAME, 'old candidate', dict(intent.SCHEMA,
            properties=dict(intent.SCHEMA['properties'], version={'type':'string', 'enum':['research-intent-v2.5.3']})))
        model = DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only'),
            response_diagnostics=True, repair_tool_arguments=True, argument_diagnostic_sink=lambda e: None)
        with patch('trading_companion.deepseek_tool_model._post_chat', side_effect=AssertionError('No request')):
            with self.assertRaises(ModelConfigurationError):
                model.complete((ToolMessage('user', 'synthetic'),), tools=(old,))

    def test_second_attempt_allows_only_scalar_and_restores_scope_after_exit(self):
        with retry_tool_arguments():
            self.assertEqual(self.parse('{"a":unused}').tool_calls[0].arguments, dict(a='unused'))
            for text in ('{"a":"甲\n乙"}', '```json\n{"a":1}\n```', '{"a":1}说明'):
                with self.assertRaises(ModelCallError):
                    self.parse(text)
        self.assertEqual(self.parse('```json\n{"a":1}\n```').tool_calls[0].arguments, dict(a=1))


class EvidenceAndDateTests(OfflineTests):
    def test_false_plan_evidence_is_grounded_then_cleared_without_semantic_veto_repair(self):
        for phrase, exclusions, kind in (
                ('不用价位', [dict(kind='plan', phrase='不用价位')], 'unused_plan_phrase'),
                ('价位', [dict(kind='plan', phrase='不用价位')], 'unused_plan_phrase'),
                ('平安银行', [], 'unused_plan_phrase')):
            value = draft(plan_phrase=phrase, exclusions=exclusions)
            original, log = deepcopy(value), []
            result = intent.check_response('研究平安银行，不用价位', response(value), context=context(), corrections=log)
            self.assertFalse(result.wants_plan)
            entry = next(e for e in log if e['kind'] == kind)
            self.assertEqual((entry['before'], entry['after']), (phrase, None))
            self.assertEqual(value, original)
            raw_log = []
            intent.check_response('研究平安银行，不用价位', response(value), context=context(), normalize=False, corrections=raw_log)
            self.assertFalse(any(e['kind'] in ('unused_plan_phrase','unused_plan_phrase') for e in raw_log))

    def test_grounding_bad_veto_and_true_plan_still_obey_existing_checks(self):
        for value in (draft(plan_phrase='fabricated'), draft(plan_phrase=12),
                      draft(plan_phrase='价位', exclusions=[dict(kind='plan', phrase='价位')])):
            with self.assertRaises(InputIssue):
                intent.check_response('研究平安银行，不用价位', response(value), context=context())
        value = draft(wants_plan=True, plan_phrase='给价位', horizon_expression='中线',
                      exclusions=[dict(kind='plan', phrase='不用价位')])
        log = []
        result = intent.check_response('研究平安银行中线，给价位但不用价位', response(value), context=context(), corrections=log)
        self.assertEqual((result.wants_plan, result.outcome), (False, 'clarify_conflict'))
        self.assertFalse(any(e['kind'] in ('vetoed_plan_phrase','unused_plan_phrase') for e in log))

    def test_date_prefix_uses_shared_recognition_verbatim_and_same_window_raw_stays_original(self):
        for expression, expected in (('这三个月的K线', '这三个月'), ('近三个月的日线', '近三个月'),
                                     ('2024-01-01到2024-02-01的数据', '2024-01-01到2024-02-01')):
            question = '研究平安银行' + expression
            value, log = draft(date_expression=expression), []
            result = intent.check_response(question, response(value), context=context(), corrections=log)
            raw = intent.check_response(question, response(value), context=context(), normalize=False)
            self.assertEqual(result.date_expression, expected)
            self.assertEqual(raw.date_expression, expression)
            self.assertEqual(result.historical_window, raw.historical_window)
            self.assertEqual(result.outcome, 'research')
            self.assertIn('date_expression_prefix', [e['kind'] for e in log])
            self.assertIn(expected, expression)

    def test_date_prefix_never_discards_constraints_invalid_future_or_additional_dates(self):
        future = (context().reference_date + timedelta(days=10)).isoformat()
        for expression in ('这三个月', '这三个月以来的K线', '这三个月之前的数据', '这三个月左右的K线',
                           '近三个月到近一个月的K线', '近零个月的K线', '近999个月的K线',
                           '2024-02-31的数据', future+'的数据', '这三个月半的K线', '观察近三个月的K线'):
            log = []
            result = intent.check_response('研究平安银行'+expression, response(draft(date_expression=expression)),
                                            context=context(), corrections=log)
            self.assertEqual(result.date_expression, expression)
            self.assertNotIn('date_expression_prefix', [e['kind'] for e in log])
        for extra in ('，另看近一个月', '，不要丢掉2024-01-01以前的数据', '，还要今天的数据'):
            log = []
            intent.check_response('研究平安银行这三个月的K线'+extra,
                response(draft(date_expression='这三个月的K线')), context=context(), corrections=log)
            self.assertNotIn('date_expression_prefix', [e['kind'] for e in log])


if __name__ == '__main__':
    unittest.main()
