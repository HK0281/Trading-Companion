"""Default-off library parity and default-on DeepSeek command-line startup."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from inspect import signature
from unittest.mock import Mock, patch
import unittest

from scripts.verify import clean_environment, offline_network
from tests import routing_baseline_support as old
from tests.knowledge_diagnostics_baseline_support import compare_observations
from tests.intent_integration_support import BASELINE, observe
from tests.intent_historical_observer import (
    fixed_index_observe, historical_index_observation, require_historical_revision,
)
from trading_companion.api_service import ResearchApiService, _SessionModel
from trading_companion.api_server import main
from trading_companion.conversation_memory import TurnDigest
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.tool_model import ToolMessage, ToolModelResponse


class DisabledServiceTests(unittest.TestCase):
    def test_library_constructor_keeps_an_explicit_false_default(self):
        self.assertIs(signature(ResearchApiService).parameters['research_intent_v2_enabled'].default, False)

    def test_missing_historical_commit_fails_explicitly(self):
        with patch('tests.intent_historical_observer.subprocess.run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(AssertionError, 'Required historical commit .* is unavailable'):
                require_historical_revision()

    def test_default_and_explicit_false_match_the_pre_edit_public_baseline(self):
        corpus = old.read_baseline(BASELINE)
        self.assertEqual(len(corpus['scenarios']), 38)
        self.assertEqual(sum(len(s['steps']) for s in corpus['scenarios']), 113)
        for scenario in corpus['scenarios']:
            observer = observe
            if scenario['id'] == 'intent-index-horizon':
                # Its scenario snapshot hashes the raw random request ID before
                # normalization. Preserve the original capture and compare fresh
                # pinned source and current source with deterministic IDs instead.
                scenario = {**scenario, 'observed': historical_index_observation(scenario)}
                observer = fixed_index_observe
            for options in ({}, dict(research_intent_v2_enabled=False)):
                with self.subTest(scenario=scenario['id'], options=options), patch(
                        'trading_companion.model_registry.configured_intent_model',
                        side_effect=AssertionError('No extra configuration')):
                    compare_observations(scenario, observer(scenario, **options))

    def test_non_boolean_switch_fails_before_any_dependency(self):
        factory = Mock(side_effect=AssertionError('No model or data construction'))
        for value in (1, 0, None, 'false'):
            with self.subTest(value=value), self.assertRaises(ModelConfigurationError):
                ResearchApiService(model_factory=factory, directory_loader=factory,
                                   research_intent_v2_enabled=value)
        factory.assert_not_called()

    def test_switch_does_not_construct_intent_factory_for_demo_or_openai(self):
        from trading_companion.api import create_demo_service
        from trading_companion.development_fixtures import load_development_fixtures, REFERENCE_DATE
        source, directory = load_development_fixtures()
        with patch('trading_companion.model_registry.configured_intent_model',
                   side_effect=AssertionError('No dedicated model for unrelated providers')):
            for provider in ('fake', 'openai'):
                service = ResearchApiService(model_factory=Mock, directory_loader=lambda _: directory,
                    provider=provider, data_mode='synthetic', development_source=source,
                    reference_date=REFERENCE_DATE, research_intent_v2_enabled=True)
                self.addCleanup(service.close)
                self.assertIsNone(service._intent_model_factory)

    def test_intent_view_shares_session_allowance_without_answer_history(self):
        answer, intent = Mock(), Mock()
        answer.complete.return_value = intent.complete.return_value = ToolModelResponse('complete', '{}')
        owner = _SessionModel(answer, 400)
        owner.history = (TurnDigest('先前问题', 'knowledge', 'completed'),)
        view = owner.intent_view(intent)
        messages = (ToolMessage('user', '本句'),)
        for _ in range(399):
            view.complete(messages, tools=())
        self.assertEqual(intent.complete.call_args.args[0], messages)
        owner.complete(messages, tools=())
        self.assertEqual(owner.forwarded, 400)
        self.assertIn('先前问题', answer.complete.call_args.args[0][0].content)
        for delegate in (view, owner):
            with self.assertRaises(ModelCallError) as caught:
                delegate.complete(messages, tools=())
            self.assertEqual(caught.exception.code, 'api_session_call_limit')
        self.assertEqual(intent.complete.call_count, 399)
        self.assertEqual(answer.complete.call_count, 1)

    def test_failed_intent_transport_still_uses_the_shared_session_allowance(self):
        intent = Mock()
        intent.complete.side_effect = ModelCallError('timeout', 'private')
        owner = _SessionModel(Mock(), 1)
        with self.assertRaises(ModelCallError):
            owner.intent_view(intent).complete((ToolMessage('user', '本句'),), tools=())
        self.assertEqual(owner.forwarded, 1)


class IntentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for guard in (
            clean_environment(), offline_network(),
            patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('No .env reads')),
        ):
            self.stack.enter_context(guard)
        self.models = self.stack.enter_context(patch('trading_companion.model_registry.configured_models'))
        self.intent_config = self.stack.enter_context(patch('trading_companion.model_registry.configured_intent_model'))
        self.app = self.stack.enter_context(patch('trading_companion.api.create_app', return_value='isolated-app'))
        self.server = self.stack.enter_context(patch('uvicorn.run'))

    def exercise(self, flags=(), *, providers=('deepseek',), selected=None, enabled=True):
        from tests.test_research_intent_v2 import draft, response
        answer = Mock(complete=Mock(return_value=ToolModelResponse('complete', '固定离线回答')))
        self.models.return_value = {provider: lambda: answer for provider in providers}
        intent = Mock(complete=Mock(return_value=response(draft(task='knowledge',
            subject=dict(source='none', mentions=[], selection=None)))))
        intent_factory = Mock(return_value=intent)
        self.intent_config.return_value = intent_factory

        def serve(app, **options):
            self.assertEqual(app, 'isolated-app')
            service = self.app.call_args.kwargs['service']
            development = self.app.call_args.kwargs.get('development_service')
            if development is not None:
                self.assertIsNone(development._intent_model_factory)
                self.assertFalse(development._research_intent_v2_enabled)
            count_before_session = intent_factory.call_count
            credentials = service.create_session(provider=selected)
            entry = service._entries[credentials['session_id']]
            uses_intent = enabled and entry.provider == 'deepseek'
            self.assertEqual(intent_factory.call_count - count_before_session, int(uses_intent))
            self.assertIs(entry.intent_model, intent if uses_intent else None)
            with patch.object(service, '_intent_turn', wraps=service._intent_turn) as new, \
                    patch.object(service, '_legacy_turn', wraps=service._legacy_turn) as legacy:
                service.message(credentials['session_id'], credentials['access_token'], 'launcher-test', 'MACD怎么计算')
                self.assertEqual(new.call_count, int(uses_intent))
                self.assertEqual(legacy.call_count, int(not uses_intent))
            self.assertEqual(intent.complete.call_count, int(uses_intent))

        self.server.side_effect = serve
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            status = main(list(flags))
        self.assertEqual(status, 0)
        self.server.assert_called_once()
        if enabled and 'deepseek' in providers:
            self.intent_config.assert_called_once_with()
        else:
            self.intent_config.assert_not_called()

    def test_cli_default_enables_deepseek_interpretation(self):
        self.exercise(['--data', 'synthetic'])

    def test_cli_explicit_enable_retains_deepseek_interpretation(self):
        self.exercise(['--data', 'synthetic', '--research-intent-v2'])

    def test_cli_explicit_disable_avoids_dedicated_configuration(self):
        self.exercise(['--data', 'synthetic', '--no-research-intent-v2'], enabled=False)

    def test_demo_default_keeps_legacy_entry_without_model_configuration(self):
        self.exercise(['--demo'], providers=('fake',), enabled=False)
        self.models.assert_not_called()

    def test_openai_default_keeps_legacy_entry_without_intent_configuration(self):
        self.exercise(['--provider', 'openai', '--data', 'synthetic'], providers=('openai',))

    def test_openai_session_in_a_mixed_service_does_not_create_an_intent_instance(self):
        self.exercise(['--data', 'synthetic'], providers=('deepseek', 'openai'), selected='openai')

    def test_intent_startup_failure_stops_without_retrying_the_legacy_entry(self):
        self.models.return_value = {'deepseek': Mock()}
        self.intent_config.side_effect = ModelConfigurationError('artificial-private-diagnostic')
        with patch('trading_companion.api_service.ResearchApiService', wraps=ResearchApiService) as service, \
                patch('trading_companion.api.create_demo_service') as demo, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            self.assertEqual(main(['--data', 'synthetic']), 2)
        service.assert_called_once()
        self.assertIs(service.call_args.kwargs['research_intent_v2_enabled'], True)
        self.assertIn('[model_configuration_error]', errors.getvalue())
        self.assertNotIn('artificial-private-diagnostic', errors.getvalue())
        self.app.assert_not_called()
        self.server.assert_not_called()
        demo.assert_not_called()

    def test_help_explains_default_and_start_cmd_rollback_without_configuration(self):
        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as caught:
            main(['--help'])
        self.assertEqual(caught.exception.code, 0)
        help_text = ' '.join(output.getvalue().split())
        self.assertIn('default: enabled', help_text)
        self.assertIn('start.cmd --no-research-intent-v2', help_text)
        self.assertIn('OpenAI and demo sessions keep their existing entry', help_text)
        self.models.assert_not_called()
        self.intent_config.assert_not_called()
        self.app.assert_not_called()
        self.server.assert_not_called()

