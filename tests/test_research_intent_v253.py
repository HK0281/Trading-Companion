"""Offline format retries, bounded evaluation diagnostics and clarification priorities."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from evaluation_tools import research_intent_v25_evaluation as experiment
from evaluation_tools import replay_research_intents_v25 as replay
from test_deepseek_tool_model import provider_call, provider_payload
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.tool_model import ToolDefinition, ToolMessage, ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits, request_timeout


def payload(arguments, finish='tool_calls'):
    return provider_payload(finish=finish, content=None,
        calls=[provider_call(name=intent.TOOL_NAME, arguments=arguments)])


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
    def evaluate(self, outputs, *, maximum=2):
        events = []
        model = DeepSeekToolModel(DeepSeekConfig('deepseek-flash', 'synthetic-only'),
            response_diagnostics=True, repair_tool_arguments=True,
            argument_diagnostic_sink=experiment._argument_diagnostic,
            evaluation_scope='public', save_failed_arguments=True)
        budget = experiment.AttemptBudget(maximum, events.append)
        with patch('trading_companion.deepseek_tool_model._post_chat', side_effect=outputs) as post:
            result = experiment.predict(sample(), prefixes={}, setups={}, model=model, budget=budget)
        return result, events, budget, post

    def test_single_format_retry_uses_identical_request_and_preserves_first_failure_and_usage_gap(self):
        result, events, budget, post = self.evaluate([payload('{"x":oops}'), payload(json.dumps(draft()))])
        self.assertEqual((result['status'], result['attempts'], budget.attempts), ('ok', 2, 2))
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertEqual(result['runtime']['model_calls_attempted'], 2)
        self.assertFalse(result['runtime']['usage_complete_for_attempts'])
        self.assertEqual(result['raw']['status'], 'failed')
        self.assertEqual(result['format_retry'], dict(attempted=True, successful=True))
        self.assertEqual([e['phase'] for e in events], ['reserved', 'argument_diagnostic', 'finished', 'reserved', 'finished'])
        self.assertEqual([e['attempt_number'] for e in events if e['phase'] == 'reserved'], [1, 2])
        self.assertEqual([e['target_attempt'] for e in events if e['phase'] == 'reserved'], [1, 2])
        self.assertEqual(result['attempt_history'][0]['response'], None)
        row = dict(sample(), V2=result, A=dict(status='ok', fields=sample()['gold']), effective_gold=sample()['gold'])
        report = experiment.report_results([row], {row['id']: row['gold']}, {})
        self.assertEqual(report['format_retries'], dict(total=1, successful=1, ids=[row['id']], successful_ids=[row['id']]))
        self.assertEqual(report['telemetry']['attempts'], 2)
        self.assertEqual(report['telemetry']['missing_responses'], 1)
        self.assertEqual(report['telemetry']['tokens']['total_tokens']['unknown_attempts'], 1)
        self.assertIsNone(report['telemetry']['tokens']['total_tokens']['complete_total'])
        self.assertEqual(report['score_views']['raw_original']['all']['joint_all_fields']['numerator'], 0)
        self.assertEqual(report['score_views']['normalized_approved']['all']['joint_all_fields']['numerator'], 1)
        self.assertIn(experiment.FAILED_ARGUMENT_NOTICE, experiment.markdown(report))
        self.assertEqual(report['failed_arguments']['saved_events'], 1)

    def test_incomplete_json_with_tool_calls_retries_once_but_never_repairs_second_response(self):
        for second in ('{"x":', '```json\n'+json.dumps(draft())+'\n```'):
            result, _, _, post = self.evaluate([payload('{"x":'), payload(second)])
            self.assertEqual((result['status'], result['attempts']), ('failed', 2))
            self.assertEqual(post.call_count, 2)
            self.assertFalse(result['format_retry']['successful'])
            self.assertEqual(result['argument_diagnostics'][0]['category'], 'incomplete_json')
            self.assertFalse(any(e['correction'] for e in result['argument_diagnostics']))

    def test_second_semantic_failure_is_not_success_and_never_gets_a_third_request(self):
        result, _, _, post = self.evaluate([payload('{"x":'), payload(json.dumps(draft(objective_phrase='fabricated')))])
        self.assertEqual((result['status'], result['attempts']), ('invalid', 2))
        self.assertFalse(result['format_retry']['successful'])
        self.assertEqual(post.call_count, 2)

    def test_strict_second_attempt_does_not_disable_repairs_for_the_next_target(self):
        wrapped = '```json\n'+json.dumps(draft())+'\n```'
        failed, _, _, _ = self.evaluate([payload('{"x":'), payload(wrapped)])
        self.assertEqual(failed['status'], 'failed')
        fresh, _, _, post = self.evaluate([payload(wrapped)])
        self.assertEqual((fresh['status'], fresh['attempts']), ('ok', 1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(fresh['response_origin'], 'repaired_tool_arguments')

    def test_network_timeout_http_json_refusal_interruption_and_semantic_errors_never_retry(self):
        cases = [ModelCallError(code, 'must-not-serialize') for code in
                 ('timeout', 'connection_error', 'invalid_response', 'rate_limited', 'provider_error')]
        cases += [payload('{"x":', finish=reason) for reason in ('length', 'aborted', 'content_filter')]
        cases += [payload(json.dumps(draft(objective_phrase='fabricated'))), payload('[1]')]
        for item in cases:
            with self.subTest(kind=type(item).__name__):
                result, _, _, post = self.evaluate([item])
                self.assertEqual(result['attempts'], 1)
                self.assertEqual(post.call_count, 1)
                self.assertFalse(result['format_retry']['attempted'])
                self.assertNotIn('must-not-serialize', json.dumps(result))

    def test_budget_one_suppresses_retry_and_no_budget_can_be_refunded(self):
        result, events, budget, post = self.evaluate([payload('{"x":')], maximum=1)
        self.assertEqual((result['status'], result['attempts']), ('failed', 1))
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result['format_retry']['attempted'])
        model = Mock()
        row = dict(sample(), id='another')
        other = experiment.predict(row, prefixes={}, setups={}, model=model, budget=budget)
        self.assertEqual((other['status'], other['attempts']), ('not_measured', 0))
        model.complete.assert_not_called()
        self.assertEqual(len([e for e in events if e['phase']=='reserved']), 1)

    def test_target_cannot_retry_success_change_input_repeat_wrapper_or_exceed_two_attempts(self):
        delegate = Mock()
        delegate.complete.side_effect = [ModelCallError('tool_arguments_json_invalid', 'safe'), response()]
        budget = experiment.AttemptBudget(3)
        target = budget.target(delegate, 'one')
        with self.assertRaises(ModelCallError):
            target.complete((), tools=())
        with self.assertRaises(ModelConfigurationError):
            target.complete((ToolMessage('user', 'changed'),), tools=())
        self.assertEqual(target.attempts, 1)
        target.complete((), tools=())
        with self.assertRaises(ModelConfigurationError):
            target.complete((), tools=())
        with self.assertRaises(ValueError):
            budget.target(delegate, 'one')
        self.assertEqual(delegate.complete.call_count, 2)

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
    def test_unknown_index_task_recovers_without_any_identity_binding_and_raw_stays_original(self):
        for name in ('那个科技指数', '某个制造业指数'):
            question = '研究一下' + name + '的日线'
            value = draft(task='clarify', clarification_reason='subject', confidence='low',
                          subject=dict(source='question', mentions=[name], selection=None))
            original, log = deepcopy(value), []
            checked = intent.check_response(question, response(value), context=context(), corrections=log)
            self.assertEqual((checked.task, checked.bound_subjects, checked.outcome, checked.research_kinds),
                             ('index_research', (), 'clarify_identity', ('technical',)))
            self.assertIn('clarification_task', [e['kind'] for e in log])
            self.assertEqual(value, original)
            self.assertEqual(intent.check_response(question, response(value), context=context(), normalize=False).task, 'clarify')

    def test_unknown_task_recovery_never_grants_fund_identity_or_unsupported_index_domains(self):
        name = '某个制造业指数基金'
        value = draft(task='clarify', clarification_reason='subject', subject=dict(source='question', mentions=[name], selection=None))
        result = intent.check_response('研究'+name+'的日线', response(value), context=context())
        self.assertEqual((result.task, result.outcome, result.bound_subjects), ('unsupported', 'reject', ()))
        value = draft(task='clarify', clarification_reason='subject', subject=dict(source='question', mentions=['某个科技指数'], selection=None),
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
            self.assertIn('invalid_horizon', [e['kind'] for e in log])
            self.assertEqual(value['horizon_expression'], expression)
            self.assertEqual(intent.check_response(question, response(value), context=context(), normalize=False).outcome, 'clarify_horizon')

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


class ReplayTests(OfflineTests):
    def test_current_archive_replay_never_simulates_missing_retry_and_marks_every_table(self):
        row, missing = sample(), dict(sample(), id='missing')
        old = response(draft(version='research-intent-v2.5.2')).to_dict()
        reports = [dict(rows=[dict(row, V2=dict(response=old)), dict(missing, V2=dict(response=None))])]
        with patch.object(experiment, 'populations', return_value=([row, missing],
                {r['id']:r['gold'] for r in (row, missing)}, {}, {})):
            report = replay.replay_reports(reports)
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual(report['format_retries']['total'], 0)
        self.assertIsNone(report['passed'])
        self.assertFalse(report['new_model_results'])
        self.assertEqual(report['paid_calls'], 0)
        lines = experiment.markdown(report).splitlines()
        for i, line in enumerate(lines):
            if line.startswith('| ---'):
                self.assertIn(experiment.REPLAY_253_NOTICE, lines[i-1])


if __name__ == '__main__':
    unittest.main()
