"""Disabled public service parity and the deliberately unavailable 5A switch."""

from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock, patch
import unittest

from tests import routing_baseline_support as old
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
                    old.compare_observations(scenario, observer(scenario, **options))

    def test_true_and_non_boolean_fail_before_any_dependency(self):
        factory = Mock(side_effect=AssertionError('No model or data construction'))
        for value in (True, 1, 0, None, 'false'):
            with self.subTest(value=value), self.assertRaises(ModelConfigurationError):
                ResearchApiService(model_factory=factory, directory_loader=factory,
                                   research_intent_v2_enabled=value)
        factory.assert_not_called()

    def test_cli_switch_is_unavailable_even_in_demo_before_startup_io(self):
        for extra in ([], ['--demo']):
            with self.subTest(extra=extra), redirect_stderr(StringIO()) as output, patch(
                    'trading_companion.api_service.ResearchApiService',
                    side_effect=AssertionError('No service')), patch(
                    'trading_companion.local_env._read_local_values',
                    side_effect=AssertionError('No credentials')):
                self.assertEqual(main(['--research-intent-v2', *extra]), 2)
            self.assertIn('research_intent_v2_not_ready', output.getvalue())

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

