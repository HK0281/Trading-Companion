"""Shared lifecycle and G fallback under injected failures, with no real I/O."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from tests.test_research_intent_v2 import context, draft, response
from tests.test_research_intent_context import configuration
from trading_companion.agent import AgentTurnResult, _controlled, _TURN_SCOPE
from trading_companion.api_service import _SessionModel
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_intent_context import IntentContextError, prepare_intent_request
from trading_companion.research_intent_dispatch import run_intent_dispatch_turn
from trading_companion.research_intent_v2 import CheckedIntent, understand
from trading_companion.research_session import ResearchSession
from trading_companion.tool_model import ToolMessage, ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.turn_trace import JsonlTraceSink, TraceLimits, trace_message, trace_intent_stage


MESSAGES = (ToolMessage('user', 'synthetic answer request'),)


class CountingLock:
    def __init__(self):
        self.lock, self.acquired, self.released = Lock(), 0, 0

    def acquire(self, **kwargs):
        self.acquired += 1
        return self.lock.acquire(**kwargs)

    def release(self):
        self.released += 1
        self.lock.release()


class SharedScopeTests(unittest.TestCase):
    def test_one_lock_start_fork_finish_publish_and_one_model_wrapper(self):
        session, control = ResearchSession(), TurnControl(clock=lambda: 0)
        session._turn_lock = CountingLock()
        model = Mock()
        model.complete.return_value = ToolModelResponse('complete', '{}', usage=TokenUsage(10, 5, 15))
        counts = dict(start=0, finish=0, fork=0, publish=0)
        originals = TurnControl.start, TurnControl.finish, ResearchSession._fork_for_turn, ResearchSession._publish_turn

        def counted(key, original):
            def invoke(*args, **kwargs):
                counts[key] += 1
                return original(*args, **kwargs)
            return invoke

        def outer(checked, working):
            def inner(nested, shared):
                self.assertIs(nested, checked)
                self.assertIs(shared, working)
                nested.complete(MESSAGES, tools=())
                shared._publication_sequence = 1
                return AgentTurnResult(status='completed', code='research_completed')
            first = _controlled(inner, model=model, session=working, control=control,
                                default_calls=3, input_mode='natural')
            self.assertIsNone(first.runtime)
            self.assertEqual(control.to_dict()['state'], 'active')
            self.assertEqual(session._publication_sequence, 0)
            return _controlled(inner, model=checked, session=session, control=control,
                               default_calls=3, input_mode='natural')

        with patch.object(TurnControl, 'start', counted('start', originals[0])), patch.object(
                TurnControl, 'finish', counted('finish', originals[1])), patch.object(
                ResearchSession, '_fork_for_turn', counted('fork', originals[2])), patch.object(
                ResearchSession, '_publish_turn', counted('publish', originals[3])):
            result = _controlled(outer, model=model, session=session, control=control,
                                 default_calls=3, input_mode='natural', share_nested=True)
        self.assertEqual(counts, dict(start=1, finish=1, fork=1, publish=1))
        self.assertEqual((session._turn_lock.acquired, session._turn_lock.released), (1, 1))
        self.assertEqual(result.runtime['state'], 'finished')
        self.assertEqual((result.model_calls_attempted, result.runtime['responses_received'],
                          result.runtime['observed_tokens']), (2, 2, 30))
        self.assertEqual(session._publication_sequence, 1)
        self.assertIsNone(_TURN_SCOPE.get())

    def test_mismatched_session_model_or_control_is_rejected_and_scope_cleans_up(self):
        for mismatch in ('session', 'model', 'control'):
            session, model, control = ResearchSession(), Mock(), TurnControl(clock=lambda: 0)
            def outer(checked, working):
                args = dict(session=working, model=model, control=control)
                args[mismatch] = dict(session=ResearchSession(), model=Mock(), control=TurnControl())[mismatch]
                return _controlled(lambda *_: self.fail('Must reject'), **args,
                                   default_calls=3, input_mode='natural')
            with self.subTest(mismatch=mismatch), self.assertRaises(ModelConfigurationError):
                _controlled(outer, model=model, session=session, control=control,
                            default_calls=3, input_mode='natural', share_nested=True)
            self.assertIsNone(_TURN_SCOPE.get())
            self.assertEqual(control.to_dict()['stop_code'], 'turn_cancelled')
            self.assertTrue(session._turn_lock.acquire(blocking=False))
            session._turn_lock.release()

    def test_replacement_pending_publishes_only_a_success_or_new_clarification(self):
        for status, new_pending, stop in (('completed', False, False), ('failed', False, False),
                ('needs_clarification', False, False), ('needs_clarification', True, False),
                ('completed', True, True)):
            session, control = ResearchSession(), TurnControl(clock=lambda: 0)
            old = SimpleNamespace(stage='horizon', created_at=context().now)
            replacement = SimpleNamespace(stage='horizon', created_at=context().now)
            session.index_pending, model = old, Mock()
            def outer(checked, working):
                def replace_action(nested, shared):
                    self.assertIsNone(shared.index_pending)
                    self.assertIs(session.index_pending, old)
                    if new_pending:
                        shared.index_pending = replacement
                    if stop:
                        control.cancel()
                    return AgentTurnResult(status=status)
                return _controlled(replace_action, model=model, session=working, control=control,
                    default_calls=3, input_mode='natural', replace_pending=True)
            with self.subTest(status=status, new=new_pending, stop=stop):
                result = _controlled(outer, model=model, session=session, control=control,
                    default_calls=3, input_mode='natural', share_nested=True)
                expected = old if stop or status == 'failed' or status == 'needs_clarification' and not new_pending else replacement if new_pending else None
                self.assertIs(session.index_pending, expected)
                if stop:
                    self.assertEqual(result.runtime['stop_code'], 'turn_cancelled')

    def test_keyboard_interrupt_releases_scope_and_does_not_publish_new_state(self):
        session, control = ResearchSession(), TurnControl(clock=lambda: 0)
        def action(checked, working):
            working._publication_sequence = 99
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            _controlled(action, model=Mock(), session=session, control=control,
                        default_calls=3, input_mode='natural', share_nested=True)
        self.assertEqual(session._publication_sequence, 0)
        self.assertEqual(control.to_dict()['state'], 'finished')
        self.assertIsNone(_TURN_SCOPE.get())


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context(previous=True)
        self.session, self.control = self.ctx.session, TurnControl(clock=lambda: 0)
        self.model, self.intent = Mock(), Mock()
        self.model.complete.return_value = ToolModelResponse('complete', '{}', usage=TokenUsage(2, 1, 3))
        self.intent.complete.return_value = response(usage=TokenUsage(4, 2, 6))
        self.factory = Mock(side_effect=lambda q, s: prepare_intent_request(q,
            **configuration(replace(self.ctx, session=s))))
        self.visits = []
        self.execute = Mock(side_effect=self.executed)
        self.legacy = Mock(side_effect=self.old_path)

    def old_path(self, question, *, model, session, control):
        self.visits.append((question, session.latest_research, session._publication_sequence))
        def action(checked, working):
            checked.complete(MESSAGES, tools=())
            working._publication_sequence += 1
            return AgentTurnResult(status='completed', code='research_completed')
        return _controlled(action, model=model, session=session, control=control,
                           default_calls=3, input_mode='natural')

    def executed(self, checked, *, model, session, control):
        return AgentTurnResult(status='needs_clarification' if checked.outcome != 'research' else 'completed')

    def run_dispatch(self, question='研究平安银行', **options):
        args = dict(model=self.model, intent_model=self.intent, session=self.session,
                    control=self.control, request_factory=self.factory, legacy=self.legacy,
                    execute=self.execute, clock=lambda: 0)
        args.update(options)
        return run_intent_dispatch_turn(question, **args)

    def test_invalid_understanding_falls_back_once_with_original_text_and_unchanged_state(self):
        for failure in (ModelCallError('timeout', 'private error'), ModelCallError('connection_error', 'private'),
                        ModelCallError('tool_arguments_json_invalid', 'private'),
                        ToolModelResponse('complete', '{}'), ToolModelResponse('refused', 'private'),
                        ToolModelResponse('incomplete', 'private', finish_reason='length'),
                        response(draft(subject=dict(source='question', mentions=['不存在'], selection=None)))):
            self.setUp()
            before = self.session.latest_research, self.session._publication_sequence
            self.intent.complete.side_effect = failure if isinstance(failure, Exception) else None
            if not isinstance(failure, Exception):
                self.intent.complete.return_value = failure
            with self.subTest(failure=type(failure).__name__):
                result = self.run_dispatch('  研究平安银行  ')
                self.assertEqual(self.visits, [('  研究平安银行  ', *before)])
                self.assertEqual(self.legacy.call_count, 1)
                self.assertEqual(self.intent.complete.call_count, 1)
                self.execute.assert_not_called()
                self.assertEqual(result.runtime['model_calls_attempted'], 2)
                self.assertEqual(result.runtime['state'], 'finished')

    def test_valid_rejection_clarification_and_execution_failure_never_fall_back(self):
        for outcome in ('reject', 'clarify_horizon', 'research'):
            self.setUp()
            checked = CheckedIntent('stock_research', outcome=outcome)
            self.execute.return_value = AgentTurnResult(status='failed')
            self.execute.side_effect = None
            with self.subTest(outcome=outcome), patch(
                    'trading_companion.research_intent_dispatch.understand_active',
                    return_value=SimpleNamespace(status='ok', code='intent_v2_checked', intent=checked)):
                result = self.run_dispatch()
            self.legacy.assert_not_called()
            self.execute.assert_called_once()
            self.assertEqual(result.status, 'failed')

    def test_context_failure_uses_zero_intent_calls_then_the_old_path(self):
        self.factory.side_effect = IntentContextError('context_request_too_large')
        result = self.run_dispatch()
        self.intent.complete.assert_not_called()
        self.execute.assert_not_called()
        self.assertEqual(self.legacy.call_count, 1)
        self.assertEqual(result.model_calls_attempted, 1)

    def test_commands_and_trusted_history_bypass_the_context_and_intent(self):
        for question, history in (('取消', False), ('查看上次结果', False), ('arbitrary', True)):
            self.setUp()
            with self.subTest(question=question):
                self.run_dispatch(question, history=history)
                self.factory.assert_not_called()
                self.intent.complete.assert_not_called()

    def test_cancel_deadline_tokens_and_last_call_stop_before_fallback(self):
        for kind in ('cancel', 'deadline', 'tokens', 'last_call'):
            self.setUp()
            clock = [0]
            self.control = TurnControl(TurnLimits(model_calls=1 if kind == 'last_call' else 3,
                observed_tokens=6 if kind == 'tokens' else 24000), clock=lambda: clock[0])
            def failure(*args, **kwargs):
                if kind == 'cancel':
                    self.control.cancel()
                if kind == 'deadline':
                    clock[0] = 91
                if kind == 'tokens':
                    return response(usage=TokenUsage(4, 2, 6))
                raise ModelCallError('timeout', 'private')
            self.intent.complete.side_effect = failure
            with self.subTest(kind=kind):
                result = self.run_dispatch()
                self.legacy.assert_not_called()
                self.execute.assert_not_called()
                self.assertEqual(result.model_calls_attempted, 1)
                self.assertEqual(result.runtime['stop_code'], dict(cancel='turn_cancelled',
                    deadline='turn_deadline_exceeded', tokens='token_budget_exhausted',
                    last_call='model_call_budget_exhausted')[kind])
                self.assertEqual(self.session._publication_sequence, 1)

    def test_shared_session_counter_includes_failed_intent_and_fallback(self):
        owner = _SessionModel(self.model, 400)
        self.intent.complete.side_effect = ModelCallError('timeout', 'private')
        result = self.run_dispatch(model=owner, intent_model=owner.intent_view(self.intent))
        self.assertEqual((owner.forwarded, result.model_calls_attempted), (2, 2))

    def test_dispatch_reentry_is_rejected_and_reset_after_failure(self):
        self.execute.side_effect = lambda *a, **k: self.run_dispatch()
        with self.assertRaises(ModelConfigurationError):
            self.run_dispatch()
        self.setUp()
        self.assertEqual(self.run_dispatch().status, 'completed')

    def test_stage_trace_records_only_fixed_codes_counts_and_unknown_usage(self):
        with TemporaryDirectory() as folder:
            sink = JsonlTraceSink(Path(folder))
            self.intent.complete.return_value = ToolModelResponse('complete', 'private-body')
            with trace_message(sink, mode='normal', provider='fake', history=False):
                result = self.run_dispatch('研究平安银行 private-question')
            raw = (Path(folder)/'turns.jsonl').read_text('utf-8')
            record = json.loads(raw)
            self.assertNotIn('private', raw)
            stages = record['intent_stages']
            self.assertEqual([s['stage'] for s in stages], ['context', 'understanding', 'legacy'])
            self.assertEqual([s['model_calls_attempted'] for s in stages], [0, 1, 1])
            self.assertFalse(stages[1]['usage_complete_for_attempts'])
            self.assertTrue(stages[2]['usage_complete_for_attempts'])
            self.assertEqual(result.runtime['observed_tokens'], 3)

    def test_stage_trace_is_bounded_sanitized_and_respects_record_bytes(self):
        with TemporaryDirectory() as folder:
            sink = JsonlTraceSink(Path(folder), limits=TraceLimits(record_bytes=2048))
            before = TurnControl().to_dict()
            with trace_message(sink, mode='normal', provider='fake', history=False):
                for _ in range(100):
                    trace_intent_stage('understanding', 'private-provider-message', before, before, 2)
            raw = (Path(folder)/'turns.jsonl').read_bytes()
            self.assertLessEqual(len(raw), 2048)
            record = json.loads(raw)
            self.assertLessEqual(len(record.get('intent_stages', [])), 4)
            self.assertNotIn('private', raw.decode())

    def test_standalone_candidate_counts_late_usage_and_discards_actionable_response(self):
        control = TurnControl(TurnLimits(model_calls=1, observed_tokens=6), clock=lambda: 0)
        self.intent.complete.return_value = response(usage=TokenUsage(4, 2, 6))
        run = understand('研究平安银行', model=self.intent, context=self.ctx, control=control)
        self.assertEqual(run.code, 'token_budget_exhausted')
        self.assertIsNone(run.response)
        self.assertEqual(run.runtime['responses_received'], 1)
        self.assertEqual(run.runtime['observed_tokens'], 6)
        self.assertEqual(run.runtime['state'], 'finished')

