"""Independent v2.5.1 boundary and transport fixtures; no provider or sealed data."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v25_evaluation as experiment
from evaluation_tools import replay_research_intents_v25 as replay
from scripts.verify import clean_environment, offline_network
from test_deepseek_tool_model import provider_call, provider_payload
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import sample
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.tool_model import ToolDefinition, ToolMessage
from trading_companion.workflow_evaluation_fixtures import SECURITIES


class OfflineTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())


class TechnicalAndSubjectTests(OfflineTests):
    def check(self, question, value=None, ctx=None, **kwargs):
        return intent.check_response(question, response(value or draft()), context=ctx or context(), **kwargs)

    def test_explicit_technical_cues_normalize_stock_and_index_and_leave_raw_unchanged(self):
        for mention, task in (('平安银行', 'stock_research'), ('沪深300', 'index_research')):
            for cue in ('完整日线', '周线', 'K线', 'BOLL'):
                question = '研究' + mention + '的' + cue
                value = draft(task=task, subject=dict(source='question', mentions=[mention], selection=None),
                              indicator_mentions=['BOLL'] if cue == 'BOLL' else [])
                original, log = deepcopy(value), []
                with self.subTest(task=task, cue=cue):
                    result = self.check(question, value, corrections=log)
                    self.assertEqual(result.research_kinds, ('technical',))
                    self.assertIn('explicit_technical_cue', [c['kind'] for c in log])
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
            self.assertEqual(self.check('研究平安银行，' + cue, value).research_kinds, ('technical',))

    def test_unknown_indicators_still_clarify_and_grounding_scope_failures_still_reject(self):
        value = draft(indicator_mentions=['神奇指标'])
        result = self.check('研究平安银行的神奇指标', value)
        self.assertEqual((result.research_kinds, result.outcome), (('technical',), 'clarify_conditions'))
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value)
        with self.assertRaises(InputIssue):
            self.check('研究平安银行，只用BOLL', draft(indicator_mentions=['BOLL']))
        conflict = self.check('研究平安银行，不用BOLL', draft(indicator_mentions=['BOLL']))
        self.assertEqual((conflict.outcome, conflict.indicator_mentions, conflict.exclusions),
                         ('clarify_conflict', (), ('indicator:boll',)))

    def test_whole_group_reference_precedes_ordinals_and_checks_counts(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for selection in ('这两只', '前面这两只', '它们', '这些', '这2只股票', '前面的这二只'):
            value = draft(subject=dict(source='previous', mentions=[], selection=selection))
            result = self.check('比较' + selection + '的情况', value, ctx)
            self.assertEqual(result.bound_subjects, tuple(s.instrument_id for s in SECURITIES[:2]))
        for selection in ('这三只', '这零只', '这两两只', '前面这1只', '这些'):
            value = draft(subject=dict(source='previous', mentions=[], selection=selection))
            target = context(previous=True) if selection == '这些' else ctx
            self.assertEqual(self.check('研究' + selection, value, target).bound_subjects, ())
        value = draft(subject=dict(source='previous', mentions=[], selection='第二只'))
        self.assertEqual(self.check('研究第二只', value, ctx).bound_subjects, (SECURITIES[1].instrument_id,))

    def test_group_reference_cannot_escape_invalid_context_or_consume_an_explicit_object(self):
        value = draft(subject=dict(source='previous', mentions=[], selection='前面这两只'))
        base = context(previous=True, securities=SECURITIES[:2])
        for ctx in (context(), replace(base, conversation_memory=False), replace(base, source='akshare_eastmoney'),
                    replace(base, development=True), replace(base, now=base.now-timedelta(seconds=1))):
            self.assertEqual(self.check('研究前面这两只', value, ctx).bound_subjects, ())
        value = draft(subject=dict(source='previous', mentions=[], selection='这些'))
        with self.assertRaises(InputIssue):
            self.check('这些和平安银行比较', value, base)

    def test_redundant_explicit_selection_only_removes_exact_mention_and_preserves_raw(self):
        value = draft(subject=dict(source='question', mentions=['平安银行'], selection='平安银行'))
        original, log = deepcopy(value), []
        result = self.check('研究平安银行', value, corrections=log)
        self.assertEqual(result.bound_subjects, ('000001.SZ',))
        self.assertIn('redundant_question_selection', [c['kind'] for c in log])
        self.assertEqual(value, original)
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value, normalize=False)
        for selection in ('平安', '第二只', '不存在'):
            with self.assertRaises(InputIssue):
                self.check('研究平安银行', draft(subject=dict(source='question', mentions=['平安银行'], selection=selection)))
        with self.assertRaises(InputIssue):
            self.check('平安银行和浦发银行比较', value)

    def test_grounded_unknown_index_still_clarifies_after_redundant_selection_is_removed(self):
        name = '那个科技指数'
        value = draft(task='index_research', confidence='low', clarification_reason='identity',
                      subject=dict(source='question', mentions=[name], selection=name))
        result = self.check('研究' + name + '的日线', value)
        self.assertEqual((result.task, result.bound_subjects, result.outcome, result.research_kinds),
                         ('index_research', (), 'clarify_identity', ('technical',)))
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

    def test_metered_evaluation_records_one_repair_and_keeps_raw_failure_denominator(self):
        payload = provider_payload(finish='tool_calls', content=None, calls=[provider_call(name=intent.TOOL_NAME,
            arguments='```json\n' + json.dumps(draft()) + '\n```')])
        events = []
        model = DeepSeekToolModel(self.config, response_diagnostics=True, repair_tool_arguments=True,
                                  argument_diagnostic_sink=experiment._argument_diagnostic)
        budget = experiment.AttemptBudget(1, events.append)
        with patch('trading_companion.deepseek_tool_model._post_chat', return_value=payload) as post:
            result = experiment.predict(sample(), prefixes={}, setups={}, model=model, budget=budget)
        post.assert_called_once()
        self.assertEqual((result['status'], result['attempts'], result['raw']['status']), ('ok', 1, 'failed'))
        self.assertEqual([e['phase'] for e in events], ['reserved', 'argument_diagnostic', 'finished'])
        self.assertEqual(events[1]['id'], sample()['id'])
        row = dict(sample(), V2=result, A=dict(status='ok', fields=sample()['gold']))
        report = experiment.report_results([row], {row['id']: row['gold']}, {})
        self.assertEqual(report['tool_arguments_repairs'], dict(total=1,
            by_category=dict(code_fence=dict(count=1, ids=[row['id']]))))
        self.assertEqual(report['score_views']['raw_original']['all']['joint_all_fields']['numerator'], 0)
        self.assertEqual(report['score_views']['normalized_approved']['all']['joint_all_fields']['numerator'], 1)
        self.assertIn('tool_arguments_repaired', report['corrections'])

    def test_failed_argument_diagnostic_is_durable_without_retry_when_global_budget_is_one(self):
        payload = provider_payload(finish='tool_calls', content=None,
            calls=[provider_call(name=intent.TOOL_NAME, arguments='{"private-sentinel":')])
        events = []
        with patch('trading_companion.local_env.load_evaluation_key_environment',
                   return_value={'DEEPSEEK_API_KEY': 'synthetic-fixture-credential'}) as key_loader:
            model = experiment._live_model(10)
        key_loader.assert_called_once_with('DEEPSEEK_API_KEY')
        self.assertTrue(model.response_diagnostics)
        self.assertTrue(model.repair_tool_arguments)
        with patch('trading_companion.deepseek_tool_model._post_chat', return_value=payload) as post:
            result = experiment.predict(sample(), prefixes={}, setups={}, model=model,
                budget=experiment.AttemptBudget(1, events.append))
        post.assert_called_once()
        self.assertEqual((result['status'], result['attempts'], result['response']), ('failed', 1, None))
        self.assertEqual(result['argument_diagnostics'][0]['category'], 'incomplete_json')
        self.assertEqual([e['phase'] for e in events], ['reserved', 'argument_diagnostic', 'finished'])
        self.assertEqual(result['argument_diagnostics'][0]['arguments_raw'], '{"private-sentinel":')
        self.assertNotIn('synthetic-fixture-credential', json.dumps(dict(events=events, result=result)))


class AnnotationAndReplayTests(OfflineTests):
    def test_only_one_approved_field_changes_and_frozen_records_are_still_valid(self):
        rows, effective, _, _ = experiment.populations()
        original = next(r for r in rows if r['id'] == 'V2162')
        self.assertIsNone(original['gold']['date_expression'])
        self.assertEqual(effective['V2162']['date_expression'], '今天')
        _, _, approved = experiment.cases.load_regression_amendments()
        parent = {r['id']: r['gold'] for r in approved}
        differences = [(r['id'], key) for r in approved for key in r['gold'] if effective[r['id']][key] != parent[r['id']][key]]
        self.assertEqual(differences, [('V2162', 'date_expression')])
        self.assertEqual(experiment.annotation_completion()['changes'][0]['approval_message'], 404)
        self.assertFalse(experiment.annotation_completion()['model_improvement'])
        self.assertTrue(experiment.cases.validate_freeze(require_review=True))

    def test_addendum_rejects_drift_extra_fields_unapproved_change_or_wrong_parent(self):
        rows, _ = experiment.load_corpus()
        _, _, approved = experiment.cases.load_regression_amendments()
        parent = {r['id']: r['gold'] for r in approved}
        valid = json.loads(experiment.ADDENDUM.read_text(encoding='utf-8'))
        updated, record = experiment.apply_annotation_completion(rows, parent)
        self.assertEqual(updated['V2162']['date_expression'], '今天')
        self.assertIsNone(parent['V2162']['date_expression'])
        self.assertEqual(record['changes'], valid['changes'])
        variants = []
        for key, value in (('approval_message', 403), ('field', 'operation'), ('after', '明天'),
                           ('parent_row_sha256', '0'*64), ('extra', True)):
            bad = deepcopy(valid)
            bad['changes'][0][key] = value
            variants.append(bad)
        variants += [dict(valid, parent_amendments_sha256='0'*64), dict(valid, changes=valid['changes']*2)]
        with TemporaryDirectory() as folder:
            file = Path(folder)/'addendum.json'
            for bad in variants:
                file.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
                with self.assertRaises(ValueError):
                    experiment.apply_annotation_completion(rows, parent, file)

    def test_v25_replay_keeps_new_cases_missing_json_and_marks_all_tables(self):
        row = dict(sample(), setup_id='independent')
        other = dict(deepcopy(row), id='missing-json')
        raw = response(draft(version='research-intent-v2.5')).to_dict()
        archived = dict(row, V2=dict(status='ok', attempts=1, response=raw, fields=row['gold']))
        failed = dict(other, V2=dict(status='failed', reason='tool_arguments_json_invalid', attempts=1, response=None))
        original = deepcopy([archived, failed])
        with patch.object(experiment, 'populations', return_value=([row, other],
                {r['id']: r['gold'] for r in (row, other)}, {}, {})), \
                patch.object(experiment, 'build_context', return_value=(context(), 0)):
            report = replay.replay_reports([dict(rows=[archived, failed])], 'v2.5')
        self.assertEqual([archived, failed], original)
        self.assertEqual(len(report['rows']), 2)
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual(report['authenticity'], 'archived_response_replay')
        self.assertFalse(report['new_model_results'])
        self.assertIsNone(report['passed'])
        self.assertIsNone(report['thresholds'])
        self.assertEqual(report['paid_calls'], 0)
        lines = experiment.markdown(report).splitlines()
        self.assertIn(experiment.REPLAY_251_NOTICE, lines[0])
        for index, line in enumerate(lines):
            if line.startswith('| ---'):
                self.assertIn(experiment.REPLAY_251_NOTICE, lines[index-1])
        self.assertEqual(replay.migrate_response(raw, 'v2.5').tool_calls[0].arguments['version'], intent.VERSION)
        self.assertEqual(raw['tool_calls'][0]['arguments']['version'], 'research-intent-v2.5')


if __name__ == '__main__':
    unittest.main()
