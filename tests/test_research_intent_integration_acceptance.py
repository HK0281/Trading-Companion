"""Offline safety and production-route checks for the independent acceptance runner."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from evaluation_tools import research_intent_integration_acceptance as acceptance
from scripts.evaluate_research_intent_integration import main
from trading_companion.api import create_app
from trading_companion.api_service import ResearchApiService
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import ModelCallError
from trading_companion.tool_model import ToolMessage
from trading_companion.turn_control import ControlledToolModel, TurnControl
from trading_companion.turn_trace import JsonlTraceSink
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory
from tests.test_research_intent_adapter import IntentFixture
from tests.test_research_intent_v2 import draft
from tests.test_research_intent_candidate_identity import saved_response


class AcceptancePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = acceptance.build_plan()

    def test_default_cli_has_no_configuration_network_store_or_server_side_effects(self):
        out = io.StringIO()
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')), \
                patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
                patch.object(Path, 'mkdir', side_effect=AssertionError('No writes')), \
                patch.object(acceptance, 'load_live_models', side_effect=AssertionError('No models')), \
                patch.object(acceptance, 'loopback_client', side_effect=AssertionError('No server')), \
                redirect_stdout(out):
            self.assertEqual(main([]), 0)
        text = out.getvalue()
        for script in self.plan['product_scripts']:
            for step in script['steps']:
                if step.get('question'):
                    self.assertIn(step['question'], text)
        self.assertIn('公开题：64', text)
        self.assertEqual(self.plan['historical_reference']['scores']['joint_all_fields']['numerator'], 62)
        self.assertEqual(self.plan['max_calls'], None)
        self.assertFalse(self.plan['eligible_for_switch'])

    def test_live_requires_positive_explicit_global_limit_before_loading_configuration(self):
        with patch.object(acceptance, 'load_live_models', side_effect=AssertionError('No credentials')), \
                redirect_stderr(io.StringIO()):
            for args in (['--live'], ['--live', '--max-calls', '0'], ['--max-calls', '-1']):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    main(args)

    def test_input_builder_and_model_context_never_receive_gold_or_case_ids(self):
        from evaluation_tools import research_intent_v26_cases as cases
        rows, prefixes, setups = acceptance.public_inputs()
        original = cases.build_context

        def checked(state, *args):
            self.assertEqual(set(state), {'question', 'context', 'setup_id'})
            self.assertNotIn(state['question'], [r['question'] for r in rows])
            return original(state, *args)

        with patch.object(cases, 'build_context', side_effect=checked):
            for row in rows:
                request = acceptance.production_request(row, prefixes, setups)
                messages = json.dumps([m.to_dict() for m in request.messages], ensure_ascii=False)
                self.assertNotIn(row['id'], messages)
                self.assertNotIn('"gold"', messages)
                self.assertNotIn('reference_expectation', messages)
        self.assertEqual(sum(r['max_calls'] for r in self.plan['dev']), 57)

    def test_missing_historical_report_uses_labelled_saved_response_reference(self):
        rows, _, _ = acceptance.public_inputs()
        ref = acceptance.historical_reference(rows, history=acceptance.TEMP / (uuid4().hex + '.missing'))
        self.assertEqual(ref['kind'], 'saved_response_reference')
        self.assertEqual(ref['scores']['joint_all_fields']['numerator'], 62)

    def test_saved_public_replay_runs_the_production_core_without_format_retries(self):
        corpus = json.loads(acceptance.REFERENCE.read_text(encoding='utf-8'))
        by_question = {r['input']['question']: r for r in corpus['rows']}
        seen = []

        class Replay:
            def complete(self, messages, *, tools):
                question = next(m.content for m in messages if m.role == 'user')
                seen.append(question)
                return saved_response(by_question[question]['response'])

        observer = acceptance.TransportObserver(1)
        result = acceptance.replay_dev(self.plan, Replay(), observer)
        self.assertEqual(len(result['rows']), 64)
        self.assertEqual(result['scores']['joint_all_fields']['numerator'], 62)
        self.assertEqual(len(seen), 57)
        self.assertEqual(len(set(seen)), 57)
        self.assertTrue(all(r['V2'].get('runtime', {}).get('limits', {}).get('model_calls', 1) == 1
                            for r in result['rows']))

    def test_exhausted_budget_and_oversize_context_keep_the_fixed_denominator(self):
        observer = acceptance.TransportObserver(1)
        observer.events.append(dict(attempt=1))
        result = acceptance.replay_dev(self.plan, Mock(), observer)
        self.assertEqual(result['scores']['n'], 64)
        self.assertEqual(result['scores']['statuses']['not_measured'], 57)
        from trading_companion.research_intent_context import IntentContextError
        with patch.object(acceptance, 'production_request', side_effect=IntentContextError('context_request_too_large')):
            result = acceptance.replay_dev(self.plan, Mock(), observer)
        self.assertEqual(result['scores']['n'], 64)
        self.assertEqual(result['scores']['statuses']['context_fallback'], 64)
        self.assertEqual(result['scores']['joint_all_fields']['numerator'], 0)

    def test_output_refuses_external_paths_symlink_escape_and_reuse(self):
        with self.assertRaises(ValueError):
            acceptance.isolated_path(acceptance.ROOT / 'data/conversations.sqlite3')
        with TemporaryDirectory(dir=acceptance.TEMP) as tmp:
            path = Path(tmp)
            with self.assertRaises(FileExistsError):
                acceptance.new_run_directory(path)
            # Resolve detects junctions/symlinks as well as plain '..', without OS link privileges.
            with patch.object(Path, 'resolve', return_value=acceptance.ROOT / 'outside'):
                with self.assertRaises(ValueError):
                    acceptance.isolated_path(path / 'link')

    def test_source_or_script_startup_failure_retains_every_planned_product_step(self):
        plan = self.plan
        order = []
        @contextmanager
        def fail(*args, **kwargs):
            order.append(Path(args[0]).parent.name)
            raise RuntimeError('synthetic failure')
            yield
        result = acceptance.run_products(plan, folder=acceptance.TEMP / uuid4().hex,
            answer_factory=Mock(), intent_factory=Mock(), observer=acceptance.TransportObserver(1), service_factory=fail)
        self.assertEqual(len(result), 2 * len(plan['product_scripts']))
        self.assertEqual(sum(len(s['rows']) for s in result), 2 * sum(len(s['steps']) for s in plan['product_scripts']))
        self.assertTrue(all(r['status'] == 'not_measured' for s in result for r in s['rows']))
        self.assertEqual(order, ['legacy', 'intent_v2'] * len(plan['product_scripts']))


class TransportAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.config = DeepSeekConfig(api_key='synthetic-key-not-real', model='synthetic-answer')

    def payload(self):
        return dict(model='synthetic-answer', choices=[dict(finish_reason='stop',
            message=dict(role='assistant', content='{}'))], usage=dict(prompt_tokens=30, completion_tokens=10,
            total_tokens=40, prompt_cache_hit_tokens=20, prompt_cache_miss_tokens=10))

    def test_global_transport_cap_includes_retry_and_restores_imported_aliases(self):
        import requests
        from trading_companion import deepseek_model, deepseek_tool_model
        original, alias = deepseek_model._post_chat, deepseek_tool_model._post_chat
        observer = acceptance.TransportObserver(1)
        control = TurnControl()
        control.start()
        with patch('requests.post', side_effect=requests.exceptions.Timeout('synthetic secret')) as post:
            with observer.installed():
                model = ControlledToolModel(DeepSeekToolModel(self.config), control)
                with self.assertRaises(acceptance.CallLimitReached):
                    model.complete((ToolMessage('user', 'offline'),), tools=())
        control.finish()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(observer.events), 1)
        self.assertTrue(observer.events[0]['wire_started'])
        self.assertIs(deepseek_model._post_chat, original)
        self.assertIs(deepseek_tool_model._post_chat, alias)
        self.assertNotIn('synthetic secret', json.dumps(observer.events))

    def test_one_cap_covers_tool_text_and_concurrent_transport_calls(self):
        observer = acceptance.TransportObserver(3)
        def invoke(_):
            try:
                observer.call(lambda *_: self.payload(), self.config, {})
                return True
            except acceptance.CallLimitReached:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(30)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(len(observer.events), 3)
        observer = acceptance.TransportObserver(2)
        reply = Mock(status_code=200)
        reply.json.side_effect = lambda: self.payload()
        from trading_companion import deepseek_model, deepseek_tool_model
        with patch('requests.post', return_value=reply) as post, observer.installed():
            deepseek_tool_model._post_chat(self.config, {})
            deepseek_model._post_chat(self.config, {})
            with self.assertRaises(acceptance.CallLimitReached):
                deepseek_model._post_chat(self.config, {})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sum(e['wire_started'] for e in observer.events), 2)

    def test_cached_usage_unknown_and_inconsistent_totals_are_never_zero_filled(self):
        result = acceptance.usage_observation(self.payload())
        self.assertTrue(result['cache_consistent'])
        self.assertEqual(result['prompt_cache_hit_tokens'], 20)
        result = acceptance.usage_observation(dict(usage=dict(prompt_tokens=1, completion_tokens=2, total_tokens=4)))
        self.assertFalse(result['complete'])
        self.assertIsNone(result['prompt_cache_hit_tokens'])
        self.assertFalse(result['cache_consistent'])
        result = acceptance.usage_observation(dict(usage=dict(prompt_tokens=True, completion_tokens=-1)))
        self.assertIsNone(result['prompt_tokens'])
        self.assertIsNone(result['completion_tokens'])
        self.assertIsNone(acceptance.token_summary(None)['margin'])
        self.assertFalse(acceptance.token_summary(dict(observed_tokens=24001))['reliable'])
        self.assertEqual(acceptance.token_summary(dict(observed_tokens=24001))['margin'], -1)

    def test_live_configuration_reads_local_env_but_errors_and_reports_never_print_secrets(self):
        secret = 'synthetic-credential-not-real'
        values = dict(DEEPSEEK_API_KEY=secret, DEEPSEEK_MODEL='synthetic-answer')
        with patch('trading_companion.local_env._read_local_values', return_value=values) as read, \
                patch.dict('os.environ', {}, clear=True):
            answer, intent = acceptance.load_live_models()
        read.assert_called_once()
        self.assertEqual(answer().config.api_key, secret)
        self.assertEqual(intent().config.model, 'deepseek-flash')
        with TemporaryDirectory(dir=acceptance.TEMP) as tmp:
            output = Path(tmp) / 'run'
            out, err = io.StringIO(), io.StringIO()
            with patch.object(acceptance, 'load_live_models', side_effect=RuntimeError(secret)), \
                    redirect_stdout(out), redirect_stderr(err):
                report = acceptance.run_live(acceptance.build_plan(suite='dev', max_calls=1), output=output)
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret, path.read_text(encoding='utf-8'))
            self.assertEqual(report['dev']['scores']['statuses']['not_measured'], 64)
            self.assertEqual(report['status'], 'incomplete')

    def test_explicit_prices_only_and_unknown_usage_has_unknown_cost(self):
        event = dict(requested_model='synthetic-answer', usage=acceptance.usage_observation(self.payload()))
        self.assertIsNone(acceptance._cost(event, None))
        prices = {'synthetic-answer': dict(currency='CNY', input_hit=1, input_miss=2, output=3)}
        self.assertAlmostEqual(acceptance._cost(event, prices)['amount'], 70 / 1_000_000)
        event['usage']['complete'] = False
        self.assertIsNone(acceptance._cost(event, prices))

    def test_actual_source_request_parameters_are_captured_without_source_configuration(self):
        from trading_companion.index_contracts import IndexResearchRequest
        from trading_companion.index_source import TencentIndexSource
        from trading_companion.turn_trace import trace_call, trace_message
        request = IndexResearchRequest('000300.SH', NOW.date() - timedelta(days=30), NOW.date() - timedelta(days=1))
        source = TencentIndexSource(request)
        observer = acceptance.TransportObserver(1)
        with TemporaryDirectory(dir=acceptance.TEMP) as tmp:
            sink = JsonlTraceSink(tmp)
            with observer.installed(), patch.object(TencentIndexSource, 'read_daily_bars', new=lambda self: []), \
                    trace_message(sink, mode='normal', provider='deepseek', history=False):
                trace_call('source', 'index_daily', source.read_daily_bars)
        self.assertEqual(len(observer.data_requests), 1)
        actual = observer.data_requests[0]['request']
        self.assertEqual(actual['instrument_id'], '000300.SH')
        self.assertEqual(actual['start_date'], request.start_date.isoformat())
        self.assertEqual(actual['as_of_date'], request.as_of_date.isoformat())
        self.assertTrue(actual['indicators'])
        self.assertIsNone(acceptance.request_scope(self.config))

    def test_partial_round_usage_and_paired_differences_remain_unknown(self):
        events = [dict(usage=acceptance.usage_observation(self.payload())), dict(usage=None)]
        usage = acceptance.transmission_totals(events)
        self.assertIsNone(usage['prompt_tokens']['total'])
        self.assertEqual(usage['prompt_tokens']['known_partial'], 30)
        self.assertEqual(usage['prompt_tokens']['unknown_attempts'], 1)
        paired = acceptance.product_comparison([
            dict(id='example', entry='legacy', rows=[dict(status='observed', tokens=dict(observed_tokens=200, reliable=True))]),
            dict(id='example', entry='intent_v2', rows=[dict(status='not_measured')])])
        self.assertIsNone(paired[0]['observed_token_delta'])
        self.assertFalse(paired[0]['usage_reliable'])

    def test_knowledge_checks_detect_a_real_market_data_event(self):
        item = dict(http_status=200, result=dict(status='completed'), transmissions=[], trace=[
            dict(events=[dict(kind='source', operation='daily_bars', entered=True)])])
        checks = acceptance.product_checks('knowledge', dict(action='message'), item, enabled=True)
        self.assertFalse(checks['no_market_data'])
        item['trace'][0]['truncated'] = True
        checks = acceptance.product_checks('knowledge', dict(action='message'), item, enabled=True)
        self.assertIsNone(checks['no_market_data'])


class HttpAcceptanceTests(unittest.TestCase):
    def test_medium_recalculation_requires_current_available_prices_and_authority(self):
        for identifier in ('company_prices', 'index_prices'):
            def item(plan, **changes):
                scope = dict(horizon='未来40个交易日', checked_prices=True)
                scope.update(changes)
                result = dict(status='limited')
                if identifier == 'company_prices':
                    result['company_research'] = dict(trade_plans=[plan] if plan is not None else [])
                else:
                    result['index_research'] = dict(scenario_plan=plan or {})
                return dict(http_status=200, actual_scope=scope, result=result)
            available = dict(status='available', horizon_profile='medium', horizon='未来40个交易日')
            for current, expected in (
                (item(None), False),
                (item(available), True),
                (item({**available, 'status': 'unavailable'}), False),
                (item({**available, 'horizon_profile': 'short'}), False),
                (item({**available, 'horizon': '未来10个交易日'}), False),
                (item(available, checked_prices=False), False),
                (item(available, horizon='未来10个交易日'), False),
            ):
                with self.subTest(identifier=identifier, current=current):
                    checks = acceptance.product_checks(identifier,
                        dict(action='message', needs='prices'), current, enabled=True)
                    self.assertIs(checks['medium_recalculation'], expected)

    def test_screening_report_keeps_executed_conditions_and_shortlist_scope(self):
        from dataclasses import replace
        from fastapi.testclient import TestClient
        from trading_companion.screening import demo_screening_snapshot
        from tests.test_screening_agent import Model as ScreeningModel, Source
        snapshot = demo_screening_snapshot(clock=lambda: NOW)
        snapshot = replace(snapshot, directory=replace(snapshot.directory, is_fixture=False), is_fixture=False)
        source = Source()
        source.read_snapshot = lambda _: snapshot
        model = ScreeningModel((dict(field='pe_dynamic', operator='lt', value=20, phrase='PE低于20'),
                                dict(field='turnover_percent', operator='gt', value=2, phrase='换手率大于2%')))
        service = ResearchApiService(model_factory=lambda: model, provider='deepseek', data_mode='tencent',
            directory_loader=lambda _: snapshot.directory, wall_clock=lambda: NOW, screening_source=source)
        script = dict(id='screening', steps=[acceptance._message('筛选PE低于20且换手率大于2%，按成交额降序选5只')])
        with TestClient(create_app(service=service), base_url='http://localhost') as client:
            result = acceptance.run_script(script, client=client, service=service, clock=[timedelta()],
                captured=[], observer=acceptance.TransportObserver(1), enabled=False)
        row = result['rows'][0]
        self.assertEqual(row['scope_status'], 'new_screening_result', row)
        self.assertEqual(row['actual_scope']['request']['sort_by'], 'amount_cny')
        self.assertEqual(row['actual_scope']['request']['limit'], 5)
        self.assertTrue(row['actual_scope']['subjects'])
        self.assertIn('coverage', row['result']['screening'])

    def test_production_routes_injected_fallback_and_idempotency_with_isolated_state(self):
        from fastapi.testclient import TestClient
        for enabled in (False, True):
            with self.subTest(enabled=enabled), TemporaryDirectory(dir=acceptance.TEMP) as tmp:
                trace, sink = [], JsonlTraceSink(Path(tmp) / 'trace')
                write = sink.write
                sink.write = lambda record: (trace.append(deepcopy(record)), write(record))[-1]
                answer, data, bars = ProductModelFixture(), CompanyDataFixture(), MemorySourceFactory()
                service = ResearchApiService(model_factory=lambda: answer, provider='deepseek',
                    data_mode='tencent', directory_loader=lambda _: directory(), wall_clock=lambda: NOW,
                    reference_date=NOW.date(), product_data_service=data, trace_sink=sink,
                    research_intent_v2_enabled=enabled, intent_model_factory=lambda: IntentFixture([draft()]))
                script = dict(id='fallback_company', injection='format', steps=[
                    acceptance._message('分析平安银行', fault=True), dict(action='replay'), dict(action='result')])
                with patch('trading_companion.product_research._build_source', side_effect=bars), \
                        TestClient(create_app(service=service), base_url='http://localhost') as client:
                    result = acceptance.run_script(script, client=client, service=service, clock=[timedelta()],
                        captured=trace, observer=acceptance.TransportObserver(1), enabled=enabled)
                first, duplicate, history = result['rows']
                self.assertEqual(first['status'], 'observed', first)
                self.assertEqual(first['result']['status'], 'completed')
                self.assertIsNotNone(first['actual_scope'])
                self.assertEqual(first['injection_exercised'], True if enabled else None)
                if enabled:
                    self.assertTrue(first['checks']['legacy_fallback_once'])
                    self.assertEqual(first['result']['model_calls_attempted'], 3)
                self.assertTrue(duplicate['replayed'])
                self.assertEqual(duplicate['transmissions'], [])
                self.assertEqual(history['result']['model_calls_attempted'], 0)
                self.assertNotIn('access_token', json.dumps(result))
                self.assertEqual(len(data.calls), 1)

    def test_precondition_failure_does_not_spend_a_target_model_call(self):
        from fastapi.testclient import TestClient
        model = ProductModelFixture()
        service = ResearchApiService(model_factory=lambda: model, provider='deepseek', data_mode='tencent',
            directory_loader=lambda _: directory(), wall_clock=lambda: NOW)
        script = dict(id='profile_indicator', steps=[acceptance._message('上一份补EMA13', needs='saved')])
        with TestClient(create_app(service=service), base_url='http://localhost') as client:
            result = acceptance.run_script(script, client=client, service=service, clock=[timedelta()],
                captured=[], observer=acceptance.TransportObserver(1), enabled=False)
        self.assertEqual(result['rows'][0]['reason'], 'prerequisite_failed')
        self.assertEqual(model.calls, [])


if __name__ == '__main__':
    unittest.main()
