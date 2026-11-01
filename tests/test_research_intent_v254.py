"""Independent scalar repair, unused evidence, date-prefix and saved-argument replay boundaries."""

from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v25_evaluation as experiment
from evaluation_tools import replay_research_intents_v25 as replay
from test_deepseek_tool_model import provider_call, provider_payload
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel, retry_tool_arguments
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.tool_model import ToolDefinition, ToolMessage


def payload(text, **changes):
    return provider_payload(finish='tool_calls', content=None,
        calls=[provider_call(name=intent.TOOL_NAME, arguments=text)], **changes)


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
    def test_false_plan_phrase_is_cleared_with_exact_veto_or_unused_reason_and_original_preserved(self):
        for phrase, exclusions, kind in (
                ('不用价位', [dict(kind='plan', phrase='不用价位')], 'vetoed_plan_phrase'),
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
            self.assertFalse(any(e['kind'] in ('vetoed_plan_phrase','unused_plan_phrase') for e in raw_log))

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


class MeteredTests(OfflineTests):
    def evaluate(self, outputs, maximum=2, row=None):
        model = DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only'),
            response_diagnostics=True, repair_tool_arguments=True, argument_diagnostic_sink=experiment._argument_diagnostic,
            evaluation_scope='public', save_failed_arguments=True)
        budget = experiment.AttemptBudget(maximum)
        with patch('trading_companion.deepseek_tool_model._post_chat', side_effect=outputs) as post:
            result = experiment.predict(row or sample(), prefixes={}, setups={}, model=model, budget=budget)
        return result, budget, post

    def test_first_repair_is_one_request_and_second_repair_is_two_identical_requests(self):
        row = dict(sample(), question='研究平安银行，不用价位')
        for outputs, expected in (([payload(bare())], 1), ([payload('{"x":'), payload(bare())], 2)):
            result, budget, post = self.evaluate(outputs, row=row)
            self.assertEqual((result['status'], result['attempts'], budget.attempts, post.call_count), ('ok', expected, expected, expected))
            self.assertEqual(result['raw']['status'], 'failed')
            self.assertFalse(result['fields']['wants_plan'])
            self.assertEqual(result['argument_diagnostics'][-1]['repair'], 'unquoted_scalar')
            self.assertEqual(result['format_retry']['attempted'], expected == 2)
            if expected == 2:
                self.assertEqual(post.call_args_list[0], post.call_args_list[1])
                self.assertFalse(result['runtime']['usage_complete_for_attempts'])

    def test_repaired_semantic_or_protocol_failure_does_not_retry(self):
        for text in (bare(draft(plan_phrase='不用价位', objective_phrase='fabricated')),
                     '{"a":unused,"a":1}'):
            result, budget, post = self.evaluate([payload(text)])
            self.assertIn(result['status'], ('invalid', 'failed'))
            self.assertEqual((result['attempts'], budget.attempts, post.call_count), (1,1,1))
            self.assertFalse(result['format_retry']['attempted'])

    def test_plan_has_same_global_budget_and_explicit_retry_repair_allowlist(self):
        plan = experiment.build_plan('dev')
        self.assertEqual((plan['max_calls'], plan['max_attempts_per_target']), (233,2))
        self.assertEqual(plan['tool_arguments_policy']['retry_repairs'], ['unquoted_scalar'])
        self.assertIn('unquoted_scalar', plan['tool_arguments_policy']['allowed_repairs'])
        self.assertTrue(plan['tool_arguments_policy']['global_budget_includes_retries'])


def archived_failure(text, number=1):
    try:
        json.loads(text)
    except json.JSONDecodeError as error:
        event = dict(call_index=0, arguments_length=len(text), finish_reason='tool_calls', category='invalid_syntax',
            position=error.pos, line=error.lineno, column=error.colno, repair=None, correction=None,
            arguments_raw=text, arguments_raw_truncated=False, target_attempt=number)
    return dict(target_attempt=number, response=None, failure='intent_v2_model_tool_arguments_json_invalid',
                call_ms=None, argument_diagnostics=[event])


class ReplayTests(OfflineTests):
    def run_replay(self, history, row=None):
        row = row or dict(sample(), question='研究平安银行，不用价位')
        original = dict(attempts=len(history), response=history[-1].get('response'), fields={},
            attempt_history=history, argument_diagnostics=[e for h in history for e in h['argument_diagnostics']],
            format_retry=dict(attempted=len(history)==2, successful=history[-1].get('response') is not None))
        archive = dict(row, V2=original)
        before = deepcopy(archive)
        with patch.object(experiment, 'populations', return_value=([row], {row['id']:row['gold']}, {}, {})), \
                patch.object(experiment, 'build_context', return_value=(context(), 0)):
            report = replay.replay_reports([dict(rows=[archive])])
        self.assertEqual(archive, before)
        return report

    def test_all_saved_attempts_are_separate_first_is_selected_and_carrier_is_disclosed(self):
        text = bare(version='v2.5.3')
        report = self.run_replay([archived_failure(text), archived_failure(text, 2)])
        self.assertEqual(report['saved_argument_replay_summary'], dict(saved=2, repaired=2, selected=1))
        self.assertEqual([e['selected'] for e in report['saved_argument_replays']], [True,False])
        row = report['rows'][0]['V2']
        self.assertEqual((row['status'], row['selected_archived_attempt']), ('ok',1))
        self.assertEqual(row['raw']['status'], 'failed')
        self.assertIsNone(row['response'])
        self.assertTrue(row['validation_carrier']['synthetic'])
        self.assertIsNone(row['validation_carrier']['original_call_id'])
        self.assertIsNone(row['validation_carrier']['original_usage'])
        self.assertEqual(report['historical_format_retries']['total'], 1)
        self.assertEqual((report['paid_calls'], report['format_retries']['total']), (0,0))
        self.assertEqual(report['telemetry']['attempts'], 0)
        self.assertIsNone(report['passed'])
        self.assertIsNone(report['thresholds'])
        self.assertFalse(report['new_model_results'])
        lines = experiment.markdown(report).splitlines()
        for i,line in enumerate(lines):
            if line.startswith('| ---'):
                self.assertIn(experiment.REPLAY_254_NOTICE, lines[i-1])

    def test_first_checked_result_wins_without_using_gold_or_choosing_later_success(self):
        # The first saved proposal is syntactically repairable but semantically invalid.
        # It still ends interpretation; a later recorded response cannot conceal that.
        text = bare(draft(plan_phrase='不用价位', objective_phrase='fabricated'), version='v2.5.3')
        later = dict(target_attempt=2, response=response(draft(version='research-intent-v2.5.3')).to_dict(),
                     failure=None, call_ms=None, argument_diagnostics=[])
        report = self.run_replay([archived_failure(text), later])
        self.assertEqual(report['rows'][0]['V2']['status'], 'invalid')
        self.assertEqual(report['rows'][0]['V2']['selected_archived_attempt'], 1)

    def test_missing_or_invalid_raw_stays_unmeasured_and_no_retry_is_fabricated(self):
        text = bare(version='v2.5.3')
        valid = archived_failure(text)
        variants = []
        for key,value in (('arguments_raw_truncated',True), ('arguments_length',len(text)+1),
                          ('position',0), ('call_index',1), ('target_attempt',2), ('finish_reason','length')):
            bad = deepcopy(valid)
            bad['argument_diagnostics'][0][key] = value
            variants.append(bad)
        bad = deepcopy(valid)
        bad['argument_diagnostics'][0].pop('arguments_raw')
        variants.append(bad)
        for attempt in variants:
            report = self.run_replay([attempt])
            self.assertEqual(report['rows'][0]['V2']['status'], 'not_measured')
            self.assertEqual(report['format_retries']['total'], 0)
            self.assertEqual(report['saved_argument_replay_summary']['selected'], 0)

    def test_fixed_public_sources_only_and_current_default(self):
        self.assertEqual(set(replay.SOURCES_BY_VERSION), {'v2.4','v2.5','v2.5.1','v2.5.2','v2.5.3'})
        self.assertIn('3c0ba9d', str(replay.SOURCES_BY_VERSION['v2.5.3']['dev']))
        self.assertIn('30339be', str(replay.SOURCES_BY_VERSION['v2.5.2']['regression']))


if __name__ == '__main__':
    unittest.main()
