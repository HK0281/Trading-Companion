"""Deterministic offline checks for cooperative limits, late results and publication."""

from dataclasses import replace
from datetime import date, datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_natural_research_turn, run_research_turn
from trading_companion.agent_cli import ConversationData, _ConversationModel, run_conversation
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.followup_demo import ConversationDemoModel, QUESTION, FOLLOWUP
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_session import ResearchSession
from trading_companion.tencent_adapter import TencentDailyBarSource
from trading_companion.akshare_adapter import AkshareDailyBarSource
from trading_companion.tool_model import ToolMessage, ToolModelResponse
from trading_companion.turn_control import (
    ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn, checkpoint, request_timeout,
)


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
EXPLICIT = "分析000001.SZ，2026-09-21至2026-09-25，指标：SMA3、RSI2"


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class HookModel:
    def __init__(self, *, hook=None, usage=None, delegate=None):
        self.delegate = ConversationDemoModel().initial if delegate is None else delegate
        self.hook, self.usage, self.calls = hook, usage, 0

    def complete(self, messages, *, tools):
        self.calls += 1
        response = self.delegate.complete(messages, tools=tools)
        if self.hook is not None:
            self.hook(self.calls)
        return replace(response, usage=self.usage) if self.usage is not None else response


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.post", side_effect=AssertionError("No unmocked model HTTP")),
            patch("requests.get", side_effect=AssertionError("No unmocked market HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No real credential reads")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.source, self.directory = load_development_fixtures()
        self.session = ResearchSession()
        self.clock = FakeClock()

    def control(self, **limits):
        return TurnControl(TurnLimits(**limits), clock=self.clock)

    def natural(self, question=QUESTION, *, model=None, control=None, **kwargs):
        options = dict(model=HookModel() if model is None else model, directory=self.directory,
                       development_source=self.source, session=self.session, current_time=NOW, control=control)
        options.update(kwargs)
        return run_natural_research_turn(question, **options)

    def explicit_model(self, **kwargs):
        from trading_companion.agent_input import parse_research_question, request_arguments
        from trading_companion.research_tool import TOOL_NAME
        from trading_companion.tool_model import ToolCall
        request = parse_research_question(EXPLICIT, source="csv")
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("control_analysis", TOOL_NAME, request_arguments(request)),))
        return HookModel(delegate=ExplanationDemoModel([proposal]), **kwargs)


class ControlUnitTests(OfflineTests):
    def test_untrusted_control_configuration_is_rejected_without_session_mutation(self):
        for control in ({}, TurnLimits(), "90"):
            with self.subTest(control=control), self.assertRaises(ModelConfigurationError):
                self.natural(control=control)
        self.assertIsNone(self.session.confirmed)

    def test_limits_reject_invalid_and_expanded_product_limits(self):
        for values in ({"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
                       {"timeout_seconds": 91}, {"model_calls": 4}, {"model_calls": -1}, {"model_calls": True},
                       {"analyses": 2}, {"observed_tokens": 0}, {"observed_tokens": 24001}, {"observed_tokens": 2.0}):
            with self.subTest(values=values), self.assertRaises(ModelConfigurationError):
                TurnLimits(**values)

    def test_deadline_uses_monotonic_seconds_and_includes_exact_boundary(self):
        control = self.control(timeout_seconds=2)
        control.start()
        self.clock.advance(1.5)
        self.assertEqual(control.timeout_for(15), .5)
        self.clock.advance(.5)
        with self.assertRaises(TurnStopped) as caught:
            control.checkpoint()
        self.assertEqual(caught.exception.code, "turn_deadline_exceeded")

    def test_invalid_or_backwards_clock_is_not_a_valid_deadline(self):
        for value in (float("nan"), float("inf"), True, "100"):
            with self.subTest(value=value), self.assertRaises(ModelConfigurationError):
                TurnControl(clock=lambda: value).start()
        control = self.control()
        control.start()
        self.clock.advance(-1)
        with self.assertRaises(ModelConfigurationError):
            control.checkpoint()

    def test_finished_control_is_one_shot_and_cannot_be_cancelled(self):
        control = self.control()
        self.assertEqual(control.to_dict()["state"], "not_started")
        control.start()
        commit = Mock()
        self.assertIsNone(control.finish(commit))
        commit.assert_called_once_with(False)
        self.assertFalse(control.cancel())
        for method in (control.start, control.checkpoint, lambda: control.observe_response(None), control.finish):
            with self.assertRaises(ModelConfigurationError):
                method()

    def test_precancelled_control_stops_without_work_and_cancel_is_idempotent(self):
        control = self.control()
        self.assertTrue(control.cancel())
        self.assertFalse(control.cancel())
        control.start()
        commit = Mock()
        self.assertEqual(control.finish(commit), "turn_cancelled")
        commit.assert_called_once_with(True)

    def test_shared_model_budget_is_consumed_before_failure_without_refund(self):
        control = self.control(model_calls=1)
        control.start()
        delegate = Mock(complete=Mock(side_effect=ModelCallError("timeout", "private")))
        model = ControlledToolModel(delegate, control)
        with self.assertRaises(ModelCallError) as failure:
            model.complete([], tools=())
        self.assertEqual(failure.exception.code, "timeout")
        self.assertIsNone(control.to_dict()["stop_code"])
        with self.assertRaises(TurnStopped) as caught:
            model.complete([], tools=())
        self.assertEqual(caught.exception.code, "model_call_budget_exhausted")
        self.assertEqual(control.to_dict()["model_calls_attempted"], 1)
        self.assertFalse(control.to_dict()["usage_complete_for_attempts"])
        delegate.complete.assert_called_once()

    def test_analysis_budget_is_shared_and_never_refunded(self):
        control = self.control()
        control.start()
        control.reserve_analysis()
        with self.assertRaises(TurnStopped) as caught:
            control.reserve_analysis()
        self.assertEqual(caught.exception.code, "analysis_budget_exhausted")
        self.assertEqual(control.to_dict()["analyses_used"], 1)

    def test_missing_usage_is_unknown_not_zero_or_a_reason_to_break_fake_models(self):
        result = self.natural()
        runtime = result.runtime
        self.assertEqual(result.code, "research_completed")
        self.assertEqual(runtime["reported_usage"], {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})
        self.assertEqual(runtime["observed_tokens"], 0)
        self.assertFalse(runtime["usage_complete_for_attempts"])

    def test_partial_and_inconsistent_usage_preserve_available_lower_bounds(self):
        control = self.control()
        control.start()
        for usage in (TokenUsage(prompt_tokens=10), TokenUsage(3, 4, None), TokenUsage(2, 3, 1)):
            control.reserve_model()
            control.observe_response(usage)
        metadata = control.to_dict()
        self.assertEqual(metadata["observed_tokens"], 22)
        self.assertFalse(metadata["usage_complete_for_attempts"])
        self.assertEqual(metadata["responses_with_complete_consistent_usage"], 0)
        self.assertEqual(metadata["reported_usage"]["total_tokens"], 1)

    def test_complete_usage_aggregates_across_all_model_stages(self):
        result = self.natural(model=HookModel(usage=TokenUsage(10, 5, 15)))
        self.assertEqual(result.runtime["observed_tokens"], 45)
        self.assertEqual(result.runtime["reported_usage"], {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45})
        self.assertTrue(result.runtime["usage_complete_for_attempts"])

    def test_context_is_restored_and_outside_transport_limits_are_unchanged(self):
        control = self.control(timeout_seconds=2)
        control.start()
        with bind_turn(control):
            self.assertEqual(request_timeout(15), 2)
            nested = self.control(timeout_seconds=1)
            nested.start()
            with bind_turn(nested):
                self.assertEqual(request_timeout(15), 1)
            self.assertEqual(request_timeout(15), 2)
        control.cancel()
        checkpoint()
        self.assertEqual(request_timeout(15), 15)


class ModelRetryTests(OfflineTests):
    def test_default_network_retry_policy_is_unchanged_when_option_is_omitted(self):
        for code in ('timeout', 'connection_error'):
            control = self.control(model_calls=2)
            control.start()
            result = ToolModelResponse('complete', 'synthetic')
            delegate = Mock(complete=Mock(side_effect=[ModelCallError(code, 'safe'), result]))
            self.assertIs(ControlledToolModel(delegate, control).complete([], tools=()), result)
            self.assertEqual(delegate.complete.call_count, 2)
            self.assertEqual(control.to_dict()['model_calls_attempted'], 2)

    def test_explicitly_disabling_network_retry_preserves_failure_and_unused_budget(self):
        for code in ('timeout', 'connection_error'):
            control = self.control(model_calls=2)
            control.start()
            delegate = Mock(complete=Mock(side_effect=ModelCallError(code, 'safe')))
            with self.assertRaises(ModelCallError) as caught:
                ControlledToolModel(delegate, control, retry_transient_errors=False).complete([], tools=())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(control.to_dict()['model_calls_attempted'], 1)
            delegate.complete.assert_called_once()

    def test_timeout_or_connection_error_retries_once_and_preserves_partial_usage(self):
        response = ToolModelResponse("complete", "offline", usage=TokenUsage(10, 5, 15))
        for code in ("timeout", "connection_error"):
            with self.subTest(code=code):
                control = self.control()
                control.start()
                delegate = Mock(complete=Mock(side_effect=[ModelCallError(code, "private"), response]))
                model = ControlledToolModel(delegate, control)
                messages = [ToolMessage("user", "offline")]
                self.assertIs(model.complete(messages, tools=()), response)
                self.assertEqual(delegate.complete.call_count, 2)
                self.assertEqual(delegate.complete.call_args_list[0], delegate.complete.call_args_list[1])
                runtime = control.to_dict()
                self.assertEqual((runtime["model_calls_attempted"], runtime["responses_received"]), (2, 1))
                self.assertEqual(runtime["reported_usage"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
                self.assertFalse(runtime["usage_complete_for_attempts"])

    def test_two_transport_failures_keep_the_failure_code_without_a_third_attempt(self):
        for code in ("timeout", "connection_error"):
            with self.subTest(code=code):
                control = self.control()
                control.start()
                failure = ModelCallError(code, "private")
                delegate = Mock(complete=Mock(side_effect=[failure, failure, ToolModelResponse("complete", "unused")]))
                with self.assertRaises(ModelCallError) as caught:
                    ControlledToolModel(delegate, control).complete([], tools=())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(delegate.complete.call_count, 2)
                runtime = control.to_dict()
                self.assertEqual(runtime["model_calls_attempted"], 2)
                self.assertEqual(runtime["responses_received"], 0)
                self.assertTrue(all(value is None for value in runtime["reported_usage"].values()))
                self.assertFalse(runtime["usage_complete_for_attempts"])

    def test_non_transport_errors_and_unusable_responses_are_not_retried(self):
        errors = ("invalid_response", "unsupported_response", "unknown_tool", "authentication_error", "rate_limited", "provider_error")
        for code in errors:
            with self.subTest(code=code):
                control = self.control()
                control.start()
                delegate = Mock(complete=Mock(side_effect=ModelCallError(code, "private")))
                with self.assertRaises(ModelCallError):
                    ControlledToolModel(delegate, control).complete([], tools=())
                delegate.complete.assert_called_once()
        for status in ("incomplete", "refused"):
            with self.subTest(status=status):
                control = self.control()
                control.start()
                response = ToolModelResponse(status, "offline")
                delegate = Mock(complete=Mock(return_value=response))
                self.assertIs(ControlledToolModel(delegate, control).complete([], tools=()), response)
                delegate.complete.assert_called_once()

    def test_deadline_or_cancellation_after_failure_prevents_retry(self):
        for stop in ("turn_deadline_exceeded", "turn_cancelled"):
            with self.subTest(stop=stop):
                control = self.control()
                control.start()
                def fail(*args, **kwargs):
                    if stop == "turn_cancelled":
                        control.cancel()
                    else:
                        self.clock.advance(90)
                    raise ModelCallError("timeout", "private")
                delegate = Mock(complete=Mock(side_effect=fail))
                with self.assertRaises(TurnStopped) as caught:
                    ControlledToolModel(delegate, control).complete([], tools=())
                self.assertEqual(caught.exception.code, stop)
                delegate.complete.assert_called_once()
                self.assertEqual(control.to_dict()["model_calls_attempted"], 1)


class TurnIntegrationTests(OfflineTests):
    def test_retried_understanding_counts_both_attempts_without_repeating_analysis(self):
        success = HookModel(usage=TokenUsage(10, 5, 15))
        attempts = 0
        def complete(messages, *, tools):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ModelCallError("timeout", "private")
            return success.complete(messages, tools=tools)
        control = self.control(profile="research_workflow", model_calls=4)
        read = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=read) as fetch:
            result = self.natural(model=Mock(complete=Mock(side_effect=complete)), control=control)
        self.assertEqual(result.code, "research_completed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (4, 1))
        self.assertEqual(result.runtime["responses_received"], 3)
        self.assertEqual(result.runtime["observed_tokens"], 45)
        self.assertFalse(result.runtime["usage_complete_for_attempts"])
        fetch.assert_called_once()

    def test_repeated_understanding_timeouts_keep_the_existing_public_failure(self):
        model = Mock(complete=Mock(side_effect=ModelCallError("timeout", "private")))
        result = self.natural(model=model, control=self.control())
        self.assertEqual((result.code, result.failure_code), ("intent_model_unavailable", "timeout"))
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 0))
        self.assertNotIn("自动重试", result.message)
        self.assertEqual(model.complete.call_count, 2)

    def test_cancel_before_start_makes_no_model_or_market_calls(self):
        control = self.control()
        control.cancel()
        model = Mock()
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No source read")):
            result = self.natural(model=model, control=control)
        self.assertEqual((result.status, result.code), ("cancelled", "turn_cancelled"))
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
        model.complete.assert_not_called()

    def test_zero_model_budget_blocks_understanding_and_explicit_proposal(self):
        for natural in (True, False):
            model = Mock()
            control = self.control(model_calls=0)
            result = (self.natural(model=model, control=control) if natural else
                      run_research_turn(EXPLICIT, model=model, development_source=self.source, control=control))
            self.assertEqual(result.code, "model_call_budget_exhausted")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
            model.complete.assert_not_called()

    def test_understanding_proposal_and_explanation_share_one_call_allowance(self):
        for limit, analyses in ((1, 0), (2, 1), (3, 1)):
            with self.subTest(limit=limit):
                self.session = ResearchSession()
                model = HookModel()
                result = self.natural(model=model, control=self.control(model_calls=limit))
                self.assertEqual((model.calls, result.model_calls_attempted, result.analyses_used), (limit, limit, analyses))
                self.assertEqual(result.code, "research_completed" if limit == 3 else "model_call_budget_exhausted")
                if limit == 2:
                    self.assertIsNotNone(result.safe_answer)
                    self.assertIsNone(self.session.confirmed)

    def test_zero_analysis_budget_blocks_source_before_construction_or_read(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No read")):
            result = self.natural(control=self.control(analyses=0))
        self.assertEqual(result.code, "analysis_budget_exhausted")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 0))

    def test_late_responses_at_every_model_stage_are_discarded(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage):
                self.session = ResearchSession()
                model = HookModel(hook=lambda call: self.clock.advance(90) if call == stage else None,
                                  usage=TokenUsage(10, 5, 15))
                result = self.natural(model=model, control=self.control())
                self.assertEqual(result.code, "turn_deadline_exceeded")
                self.assertEqual((result.model_calls_attempted, result.analyses_used), (stage, int(stage == 3)))
                self.assertEqual(result.runtime["observed_tokens"], stage * 15)
                self.assertIsNone(result.model_draft)
                self.assertEqual(result.research_result is not None, stage == 3)
                self.assertIsNone(self.session.confirmed)
                self.assertIsNone(self.session.to_dict()["saved_result"])

    def test_cancellation_at_every_model_stage_blocks_results_and_following_work(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage):
                control = self.control()
                model = HookModel(hook=lambda call: control.cancel() if call == stage else None)
                result = self.natural(model=model, control=control)
                self.assertEqual((result.status, result.code), ("cancelled", "turn_cancelled"))
                self.assertEqual(model.calls, stage)
                self.assertIsNone(result.display_answer)
                self.assertIsNone(self.session.confirmed)

    def test_late_analysis_is_not_relabelled_as_timely_facts(self):
        read = CsvDailyBarSource.read_daily_bars
        def late_read(source):
            bars = read(source)
            self.clock.advance(90)
            return bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=late_read) as fetch:
            result = self.natural(control=self.control())
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertIsNone(result.research_result)
        self.assertIsNone(self.session.to_dict()["saved_result"])
        fetch.assert_called_once()

    def test_cancel_during_read_propagates_past_tool_error_handler(self):
        control = self.control()
        read = CsvDailyBarSource.read_daily_bars
        def cancelled_read(source):
            control.cancel()
            return read(source)
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=cancelled_read):
            result = self.natural(control=control)
        self.assertEqual(result.code, "turn_cancelled")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertIsNone(result.display_answer)

    def test_token_threshold_stops_after_observation_not_before_billable_request(self):
        for threshold, calls, analyses in ((15, 1, 0), (30, 2, 0), (45, 3, 1)):
            with self.subTest(threshold=threshold):
                self.session = ResearchSession()
                result = self.natural(model=HookModel(usage=TokenUsage(10, 5, 15)),
                                      control=self.control(observed_tokens=threshold))
                self.assertEqual(result.code, "token_budget_exhausted")
                self.assertEqual((result.model_calls_attempted, result.analyses_used), (calls, analyses))
                self.assertIsNone(self.session.to_dict()["saved_result"])

    def test_deadline_takes_precedence_over_failed_model_and_does_not_leak_error(self):
        def fail(*args, **kwargs):
            self.clock.advance(90)
            raise ModelCallError("timeout", "private-runtime-marker")
        result = self.natural(model=Mock(complete=Mock(side_effect=fail)), control=self.control())
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertNotIn("private-runtime-marker", json.dumps(result.to_dict()))
        self.assertEqual(result.model_calls_attempted, 1)

    def test_default_explicit_path_remains_two_model_calls_and_one_analysis(self):
        result = run_research_turn(EXPLICIT, model=self.explicit_model(), development_source=self.source)
        self.assertEqual(result.code, "research_completed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(result.runtime["limits"]["model_calls"], 2)

    def test_no_new_success_is_published_while_explanation_is_running(self):
        def inspect(call):
            if call == 3:
                self.assertIsNone(self.session.confirmed)
                self.assertIsNone(self.session.to_dict()["saved_result"])
        result = self.natural(model=HookModel(hook=inspect))
        self.assertEqual(result.code, "research_completed")
        self.assertIsNotNone(self.session.confirmed)

    def test_cancel_during_final_local_staging_prevents_commit(self):
        control = self.control()
        remember = ResearchSession.remember_result
        def stage_and_cancel(session, *args, **kwargs):
            value = remember(session, *args, **kwargs)
            control.cancel()
            return value
        with patch.object(ResearchSession, "remember_result", autospec=True, side_effect=stage_and_cancel):
            result = self.natural(control=control)
        self.assertEqual(result.code, "turn_cancelled")
        self.assertIsNone(result.display_answer)
        self.assertIsNone(self.session.confirmed)
        self.assertIsNone(self.session.to_dict()["saved_result"])

    def test_timeout_during_local_staging_keeps_timely_facts_but_not_model_draft_or_session(self):
        remember = ResearchSession.remember_result
        def late_stage(session, *args, **kwargs):
            value = remember(session, *args, **kwargs)
            self.clock.advance(90)
            return value
        with patch.object(ResearchSession, "remember_result", autospec=True, side_effect=late_stage):
            result = self.natural(control=self.control())
        self.assertEqual((result.status, result.code), ("limited", "turn_deadline_exceeded"))
        self.assertIn("10.166667", result.display_answer)
        self.assertIsNone(result.model_draft)
        self.assertIsNone(self.session.to_dict()["saved_result"])

    def test_started_update_consumes_old_base_but_preserves_history_on_stop(self):
        initial = self.natural()
        def cancel(call):
            self.assertIsNone(self.session.confirmed)
            self.assertFalse(self.session.to_dict()["saved_result"]["is_current"])
            control.cancel()
        control = self.control()
        delegate = Mock(complete=Mock(return_value=ToolModelResponse("complete", "not used")))
        result = self.natural("添加ATR2", model=HookModel(hook=cancel, delegate=delegate), control=control)
        self.assertEqual(result.code, "turn_cancelled")
        self.assertIsNone(self.session.confirmed)
        history = self.natural("查看上次结果")
        self.assertIn(initial.confirmed_request.request_id, json.dumps(history.to_dict()))
        self.assertIn("历史查看", history.display_answer)
        self.assertEqual(self.natural(FOLLOWUP).code, "no_current_result")

    def test_late_ambiguous_understanding_cannot_create_pending(self):
        from trading_companion.session_agent_demo import build_demo_model
        model = HookModel(delegate=build_demo_model(), hook=lambda _: self.clock.advance(90))
        result = self.natural("看看平安最近一个月的走势，只看SMA3和RSI2", model=model, control=self.control())
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertIsNone(self.session.pending)

    def test_consumed_selection_cannot_be_reused_after_cancelled_proposal(self):
        from trading_companion.session_agent_demo import build_demo_model
        self.natural("看看平安最近一个月的走势，只看SMA3和RSI2", model=build_demo_model())
        self.assertIsNotNone(self.session.pending)
        control = self.control()
        model = self.explicit_model(hook=lambda _: control.cancel())
        result = self.natural("平安银行", model=model, control=control)
        self.assertEqual(result.code, "turn_cancelled")
        self.assertIsNone(self.session.pending)
        self.assertEqual(self.natural("平安银行").code, "no_pending_clarification")

    def test_local_knowledge_and_history_need_no_model_budget(self):
        self.natural()
        for question in ("RSI是什么", "查看上次结果"):
            result = self.natural(question, control=self.control(model_calls=0, analyses=0))
            self.assertEqual(result.status, "completed")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_followup_uses_same_deadline_and_does_not_remove_prior_success(self):
        model = ConversationDemoModel()
        self.natural(model=model)
        result = self.natural(FOLLOWUP, model=HookModel(delegate=model, hook=lambda _: self.clock.advance(90)),
                              control=self.control())
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertIsNone(result.followup_answer)
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (1, 0))
        self.assertIsNotNone(self.session.confirmed)

    def test_reusing_control_releases_lock_and_next_fresh_turn_still_runs(self):
        control = self.control()
        self.natural("RSI是什么", control=control)
        with self.assertRaises(ModelConfigurationError):
            self.natural("RSI是什么", control=control)
        self.assertEqual(self.natural("RSI是什么").status, "completed")

    def test_reset_prefix_remains_effective_when_subsequent_understanding_is_cancelled(self):
        self.natural()
        control = self.control()
        result = self.natural("新问题：" + QUESTION, control=control,
                              model=HookModel(hook=lambda _: control.cancel()))
        self.assertEqual(result.code, "turn_cancelled")
        self.assertIsNone(self.session.to_dict()["saved_result"])
        self.assertIsNone(self.session.confirmed)

    def test_deadline_during_tool_projection_does_not_accept_late_result(self):
        from trading_companion.research_tool import _project
        def late_project(*args, **kwargs):
            projection = _project(*args, **kwargs)
            self.clock.advance(90)
            return projection
        with patch("trading_companion.research_tool._project", side_effect=late_project):
            result = self.natural(control=self.control())
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertIsNone(result.research_result)
        self.assertIsNone(self.session.to_dict()["saved_result"])

    def test_failure_after_start_consumes_analysis_budget_and_never_retries(self):
        from trading_companion.errors import DataSourceError
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError("private-read-error")) as fetch:
            result = self.natural(control=self.control())
        self.assertEqual(result.code, "research_data_failed")
        self.assertEqual(result.runtime["analyses_used"], 1)
        self.assertEqual(result.model_calls_attempted, 2)
        fetch.assert_called_once()
        self.assertNotIn("private-read-error", json.dumps(result.runtime))

    def test_thread_signal_cancel_does_not_allow_second_turn_or_late_commit(self):
        entered, release = Event(), Event()
        control = self.control()
        def block(_):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test release was not signalled")
        outcomes = []
        failures = []
        def worker():
            try:
                outcomes.append(self.natural(model=HookModel(hook=block), control=control))
            except BaseException as error:
                failures.append(error)
        thread = Thread(target=worker, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.natural("RSI是什么").code, "session_busy")
            self.assertTrue(control.cancel())
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(outcomes[0].code, "turn_cancelled")
        self.assertIsNone(self.session.to_dict()["saved_result"])
        self.assertEqual(self.natural("RSI是什么").status, "completed")


class TransportAndTerminalTests(OfflineTests):
    def test_deepseek_retry_uses_45_seconds_then_remaining_turn_time(self):
        import requests
        control = self.control()
        control.start()
        self.clock.advance(10)
        response = Mock(status_code=200)
        response.json.return_value = {"model": "offline-model", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "offline"}}]}
        waits = []
        def post(*args, **kwargs):
            waits.append(kwargs["timeout"])
            if len(waits) == 1:
                self.clock.advance(45)
                raise requests.exceptions.Timeout()
            return response
        metered = _ConversationModel(DeepSeekToolModel(DeepSeekConfig("offline-model", "dummy-not-real")), 15)
        with bind_turn(control), patch("requests.post", side_effect=post):
            result = ControlledToolModel(metered, control).complete([ToolMessage("user", "offline")], tools=())
        self.assertEqual(result.status, "complete")
        self.assertEqual(waits, [45, 35])
        self.assertEqual(metered.forwarded, 2)
        self.assertEqual(control.to_dict()["model_calls_attempted"], 2)
        response.close.assert_called_once()

    def test_runtime_demo_passes_all_five_cases_in_separate_process(self):
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "trading_companion.turn_control_demo"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=10,
                                 env={**os.environ, "PYTHONPATH": str(self.source.path.parent.parent / "src")})
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["market_data_requested"])
        self.assertEqual(len(report["cases"]), 5)
        self.assertTrue(all(case["passed"] for case in report["cases"]))

    def test_deepseek_timeout_is_clamped_through_nested_conversation_meter(self):
        control = self.control(timeout_seconds=2)
        payload = {"model": "offline-model", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "offline"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        response = Mock(status_code=200)
        response.json.return_value = payload
        def post(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 2)
            self.clock.advance(2)
            return response
        model = _ConversationModel(DeepSeekToolModel(DeepSeekConfig("offline-model", "dummy-not-real")), 15)
        with patch("requests.post", side_effect=post) as send:
            result = self.natural(model=model, control=control)
        self.assertEqual(result.code, "turn_deadline_exceeded")
        self.assertEqual(result.runtime["observed_tokens"], 15)
        self.assertEqual(result.runtime["responses_received"], 1)
        send.assert_called_once()
        response.close.assert_called_once()
        self.assertEqual(model.forwarded, 1)

    def test_conversation_allowance_failure_does_not_refund_turn_attempt(self):
        delegate = Mock()
        result = self.natural(model=_ConversationModel(delegate, 0), control=self.control())
        self.assertEqual(result.model_calls_attempted, 1)
        self.assertEqual(result.failure_code, "conversation_call_limit")
        self.assertFalse(result.runtime["usage_complete_for_attempts"])
        delegate.complete.assert_not_called()

    def test_terminal_ctrl_c_clears_consumed_state_and_preserves_interrupt_exit_code(self):
        lines = []
        model = Mock(complete=Mock(side_effect=KeyboardInterrupt))
        code = run_conversation(model=model, data=ConversationData.from_options("synthetic"), provider="fake",
                                read_line=lambda _: QUESTION, write_line=lines.append, clock=lambda: NOW)
        self.assertEqual(code, 130)
        self.assertIn("收到中断", "\n".join(lines))
        self.assertIn("可能已取数", "\n".join(lines))

    def test_tencent_late_first_year_prevents_next_year_request(self):
        control = self.control(timeout_seconds=2)
        control.start()
        response = Mock()
        response.json.return_value = {"code": 0, "data": {"sz000001": {"day": []}}}
        def fetch(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 2)
            self.clock.advance(2)
            return response
        source = TencentDailyBarSource("000001.SZ", date(2024, 12, 30), date(2025, 1, 3))
        with bind_turn(control), patch("requests.get", side_effect=fetch) as send:
            with self.assertRaises(TurnStopped):
                source.read_daily_bars()
        send.assert_called_once()
        response.close.assert_called_once()

    def test_akshare_exposed_timeout_is_clamped_but_internal_requests_are_not_claimed_controlled(self):
        control = self.control(timeout_seconds=2)
        control.start()
        def fetch(**kwargs):
            self.assertEqual(kwargs["timeout"], 2)
            self.clock.advance(2)
            return Mock()
        api = Mock(side_effect=fetch)
        source = AkshareDailyBarSource("000001.SZ", date(2025, 1, 2), date(2025, 1, 3))
        with bind_turn(control), patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=api)}):
            with self.assertRaises(TurnStopped):
                source.read_daily_bars()
        api.assert_called_once()

    def test_terminal_displays_safe_runtime_metadata_and_explicit_limit_boundaries(self):
        questions = iter((QUESTION, "/exit"))
        lines = []
        code = run_conversation(model=ConversationDemoModel(), data=ConversationData.from_options("synthetic"),
            provider="fake", read_line=lambda _: next(questions), write_line=lines.append, clock=lambda: NOW)
        self.assertEqual(code, 0)
        output = "\n".join(lines)
        self.assertIn("90秒协作截止", output)
        self.assertIn("运行控制：", output)
        self.assertIn("用量缺失或不完整", output)
        self.assertIn("不是硬中断或费用上限", output)
        self.assertNotIn("model_draft", output)


if __name__ == "__main__":
    unittest.main()
