"""Replay saved public responses: identity evidence, never a fresh evaluation."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from evaluation_tools import research_intent_v26_evaluation as evaluation
from trading_companion.agent_input import InputIssue
from trading_companion.intent_router_task import digest
from trading_companion.model import TokenUsage, ModelCallError
from trading_companion.model_registry import configured_intent_model
from trading_companion import research_intent_v2 as intent
from trading_companion.tool_model import ToolCall, ToolModelResponse
from trading_companion.turn_control import TurnControl, bind_turn
from tests.test_deepseek_tool_model import provider_payload
from tests.test_research_intent_context import configuration, public_replay_inputs
from trading_companion.research_intent_context import build_intent_context


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'tests/fixtures/research-intent-v268-public-replay.json'


def saved_response(value):
    value = deepcopy(value)
    value['tool_calls'] = tuple(ToolCall(**call) for call in value['tool_calls'])
    value['usage'] = TokenUsage(**value['usage']) if value['usage'] else None
    return ToolModelResponse(**value)


class CandidateIdentityTests(unittest.TestCase):
    def test_prompt_tool_and_guard_still_match_the_historical_lock(self):
        ref = json.loads(REFERENCE.read_text('utf-8'))['metadata']
        lock = json.loads((ROOT/'data/evaluation/research-intent-v2.6-lock.json').read_text('utf-8'))
        self.assertEqual(ref['source_commit'], 'a11323834b6d0b0d615b39d896bb34c83ba905e4')
        self.assertTrue(ref['historical_lock_validated'])
        self.assertEqual(ref['candidate_sha256'], digest(lock['candidate']))
        self.assertEqual(ref['prompt'], dict(text=intent.INSTRUCTION,
            sha256_utf8=sha256(intent.INSTRUCTION.encode()).hexdigest()))
        self.assertEqual(ref['tool'], dict(definition=intent.TOOL.to_dict(), sha256=digest(intent.TOOL.to_dict())))
        self.assertEqual(intent.GUARD_VERSION, lock['candidate']['guard_version'])

    def test_all_64_saved_responses_match_the_pre_change_checker_with_production_context(self):
        corpus = json.loads(REFERENCE.read_text('utf-8'))
        rows, prefixes, setups = public_replay_inputs()
        self.assertEqual(len(corpus['rows']), 64)
        for row, saved in zip(rows, corpus['rows'], strict=True):
            with self.subTest(row=saved['id']):
                self.assertEqual(row, saved['input'])
                original, _ = evaluation.build_context(row, prefixes, setups)
                ctx = build_intent_context(**configuration(original))
                self.assertEqual(ctx.model_context(), saved['model_context'])
                corrections = []
                local = intent.local_preflight(row['question'], ctx)
                if local is not None:
                    actual = dict(status='local', checked=asdict(local), corrections=[])
                else:
                    try:
                        result = intent.check_response(row['question'], saved_response(saved['response']),
                                                       context=ctx, corrections=corrections)
                        actual = dict(status='checked', checked=asdict(result), corrections=corrections)
                    except InputIssue as error:
                        actual = dict(status='invalid', code=error.code, corrections=corrections)
                self.assertEqual(json.loads(json.dumps(actual)), saved['reference'])

    def test_production_factory_preserves_wire_semantics_and_tightens_only_resources(self):
        loader = Mock(return_value=dict(DEEPSEEK_MODEL='synthetic-answer',
                                        DEEPSEEK_API_KEY='synthetic-key-not-real'))
        factory = configured_intent_model(loader=loader)
        loader.assert_called_once_with(provider='deepseek')
        model = factory()
        self.assertIsNot(model, factory())
        self.assertTrue(model.response_diagnostics)
        self.assertTrue(model.repair_tool_arguments)
        self.assertFalse(model.save_failed_arguments)
        reply = Mock(status_code=200)
        reply.json.return_value = provider_payload()
        control = TurnControl(clock=lambda: 0)
        control.start()
        with patch('requests.post', return_value=reply) as post, bind_turn(control):
            model.complete((intent.ToolMessage('user', '问题'),), tools=(intent.TOOL,))
        body = post.call_args.kwargs['json']
        policy = json.loads(REFERENCE.read_text('utf-8'))['metadata']['model']
        for name, value in dict(model=policy['requested_model'], temperature=policy['temperature'],
                                thinking=dict(type=policy['thinking']), max_tokens=policy['max_output_tokens']).items():
            self.assertEqual(body[name], value)
        self.assertEqual(body['tools'][0]['function'], intent.TOOL.to_dict())
        self.assertEqual(post.call_args.kwargs['timeout'], 20)
        self.assertEqual(policy['timeout_seconds'], 45)
        self.assertEqual(policy['max_attempts_per_target'], 2)
        control.finish()

    def test_understanding_transport_never_retries_and_uses_remaining_deadline(self):
        from tests.test_research_intent_v2 import context
        factory = configured_intent_model(loader=lambda **_:dict(
            DEEPSEEK_MODEL='synthetic', DEEPSEEK_API_KEY='synthetic-key-not-real'))
        clock = [0]
        control = TurnControl(clock=lambda: clock[0])
        control.start()
        clock[0] = 85
        with patch('requests.post', side_effect=__import__('requests').Timeout('private')) as post:
            result = intent.understand_active('研究平安银行', model=factory(), context=context(), control=control)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'], 5)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(control.to_dict()['model_calls_attempted'], 1)
        control.finish()

