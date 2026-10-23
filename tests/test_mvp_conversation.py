"""Offline regression checks for bounded, resumable MVP conversation updates."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_natural_research_turn
from trading_companion.agent_input import request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.research_edit import indicator_selection, indicator_selection_label
from trading_companion.research_intent import PreparedResearchInput, ResearchConditions
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.research_update import parse_condition_update, update_conditions
from trading_companion.security_directory import load_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse
from trading_companion.turn_control import TurnControl


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
START, CUTOFF = date(2026, 9, 21), date(2026, 9, 25)
SPECS = (IndicatorSpec("sma", window=3), IndicatorSpec("sma", window=4), IndicatorSpec("rsi", window=2))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = load_security_directory(ROOT / "data/security_directory_fixture.json")
        self.source = CsvDailyBarSource(ROOT / "data/sample_daily_bars_synthetic.csv", is_synthetic=True)
        self.session = ResearchSession()
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market/directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No live model")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No keys")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No online source")),
        ):
            context.start()
            self.addCleanup(context.stop)

    def seed(self, specs=SPECS, *, confirmed_at=NOW, source="csv", start=START, cutoff=CUTOFF):
        conditions = ResearchConditions(start, cutoff, specs, source, NOW.date())
        prepared = PreparedResearchInput(status="ready", request=conditions.for_security("000001.SZ"),
            security=self.directory.lookup("000001.SZ").candidates[0], conditions=conditions)
        self.session.remember_confirmed(prepared, now=confirmed_at,
            development_source=self.source if source == "csv" else None)
        return prepared

    def turn(self, question, *, model=None, **changes):
        configuration = dict(session=self.session, model=FakeToolModel([]) if model is None else model,
            directory=self.directory, development_source=self.source, current_time=NOW, reference_date=NOW.date())
        configuration.update(changes)
        return run_natural_research_turn(question, **configuration)

    def model_for(self, prepared):
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall(
            "conversation_proposal", TOOL_NAME, request_arguments(prepared.request)),))
        return ExplanationDemoModel([proposal])

    def prepare(self, question):
        return self.session.prepare_condition_update(question, directory=self.directory, source="csv",
            development_source=self.source, reference_date=NOW.date(), now=NOW)

    def test_ambiguous_window_creates_typed_pending_without_model_or_market(self):
        self.seed()
        original = self.session.confirmed
        result = self.turn("改成5日")
        self.assertEqual((result.status, result.code), ("needs_clarification", "indicator_edit_ambiguous"))
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
        self.assertEqual(result.session_state["state"], "awaiting_indicator")
        self.assertEqual(result.session_state["pending"]["target_window"], 5)
        self.assertEqual(result.session_state["pending"]["candidates"], ["SMA3", "SMA4", "RSI2"])
        self.assertIs(self.session.confirmed, original)
        self.assertIsNone(self.session.pending)

    def test_short_selection_restores_original_window_and_runs_exactly_once(self):
        original = self.seed()
        self.turn("把窗口改成5日")
        expected = replace(original.conditions, indicators=(IndicatorSpec("sma", window=5), *SPECS[1:]))
        prepared = replace(original, request=expected.for_security("000001.SZ"), conditions=expected)
        result = self.turn("SMA3", model=self.model_for(prepared))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.session_event, "indicator_edit_resumed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(result.confirmed_request.indicators, expected.indicators)
        self.assertNotEqual(result.confirmed_request.request_id, original.request.request_id)
        self.assertIsNone(self.session.pending_edit)
        repeated = self.turn("SMA3")
        self.assertEqual(repeated.code, "no_pending_indicator_edit")
        self.assertEqual((repeated.model_calls_attempted, repeated.analyses_used), (0, 0))

    def test_generic_moving_average_limits_candidates_to_matching_kinds(self):
        self.seed()
        result = self.turn("把均线改成5日")
        self.assertEqual(result.session_state["pending"]["candidates"], ["SMA3", "SMA4"])
        refused = self.turn("RSI2")
        self.assertEqual(refused.code, "indicator_edit_reply_not_specific")
        self.assertIsNotNone(self.session.pending_edit)

    def test_single_candidate_runs_without_guessing_a_different_indicator(self):
        original = self.seed((SPECS[0],))
        amended = replace(original.conditions, indicators=(IndicatorSpec("sma", window=5),))
        prepared = replace(original, request=amended.for_security("000001.SZ"), conditions=amended)
        result = self.turn("改成5日", model=self.model_for(prepared))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.confirmed_request.indicators, amended.indicators)
        self.assertIsNone(self.session.pending_edit)

    def test_macd_only_does_not_choose_one_of_its_three_periods(self):
        self.seed((IndicatorSpec("macd", fast=2, slow=3, signal=2),))
        result = self.turn("把窗口改成5日")
        self.assertEqual(result.code, "indicator_edit_target_missing")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_short_selection_requires_the_entire_reply_and_keeps_original_expiry(self):
        self.seed()
        self.turn("改成5日")
        original = self.session.pending_edit
        for question in ("SMA3，另外换成中国平安", "SMA3然后刷新行情", "SMA3\x16", "RSI20", "均线"):
            with self.subTest(question=question):
                result = self.turn(question, current_time=NOW + timedelta(minutes=1))
                self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
                self.assertIs(self.session.pending_edit, original)
                self.assertEqual(self.session.pending_edit.window, 5)

    def test_extra_clause_on_initial_edit_is_not_silently_discarded(self):
        self.seed()
        for text in ("改成5日，另外换成中国平安", "把窗口改成5日然后刷新行情"):
            result = self.turn(text)
            self.assertEqual(result.status, "needs_clarification")
            self.assertIsNone(self.session.pending_edit)
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_no_pending_short_indicator_cannot_create_a_new_research(self):
        for text in ("SMA20", "选择SMA20", "ＳＭＡ２０", "BOLL(20,2)"):
            result = self.turn(text)
            self.assertEqual(result.code, "no_pending_indicator_edit")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_stateless_short_indicator_does_not_start_model_interpretation(self):
        result = self.turn("SMA20", session=None)
        self.assertEqual(result.code, "no_pending_indicator_edit")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_pending_edit_expires_and_time_reversal_cannot_resume_it(self):
        for later in (NOW + timedelta(minutes=15), NOW - timedelta(seconds=1)):
            self.seed()
            self.turn("改成5日")
            result = self.turn("SMA3", current_time=later)
            self.assertEqual(result.code, "indicator_edit_expired")
            self.assertIsNone(self.session.pending_edit)
            self.assertEqual(result.analyses_used, 0)

    def test_new_pending_does_not_extend_original_confirmed_base_lifetime(self):
        self.seed(confirmed_at=NOW - timedelta(minutes=14))
        self.turn("改成5日")
        result = self.turn("SMA3", current_time=NOW + timedelta(minutes=1))
        self.assertEqual(result.code, "research_context_expired")
        self.assertIsNone(self.session.pending_edit)
        self.assertIsNone(self.session.confirmed)

    def test_identity_and_edit_pending_are_mutually_exclusive(self):
        prepared = self.seed()
        self.turn("改成5日")
        ambiguous = replace(prepared, status="needs_clarification", request=None,
            clarification_resumable=True, candidates=(prepared.security,))
        self.assertTrue(self.session.remember(ambiguous, now=NOW, development_source=self.source))
        self.assertIsNone(self.session.pending_edit)
        self.assertIsNotNone(self.session.pending)
        self.session.prepare_indicator_edit("改成5日", directory=self.directory, source="csv",
            development_source=self.source, reference_date=NOW.date(), now=NOW)
        self.assertIsNone(self.session.pending)
        self.assertIsNotNone(self.session.pending_edit)

    def test_changed_base_is_not_used_for_an_old_selection(self):
        self.seed()
        self.turn("改成5日")
        old = self.session.pending_edit
        self.seed((IndicatorSpec("ema", window=3), SPECS[2]))
        self.assertIs(self.session.pending_edit, old)
        result = self.turn("SMA3")
        self.assertEqual(result.code, "indicator_edit_context_changed")
        self.assertEqual(result.analyses_used, 0)
        self.assertIsNone(self.session.pending_edit)

    def test_changed_development_source_invalidates_pending_without_fallback(self):
        self.seed()
        self.turn("改成5日")
        other = CsvDailyBarSource(ROOT / "data/sample_daily_bars_synthetic.csv", is_synthetic=False)
        result = self.turn("SMA3", development_source=other)
        self.assertEqual(result.code, "research_context_configuration_changed")
        self.assertEqual(result.analyses_used, 0)
        self.assertIsNone(self.session.pending_edit)

    def test_cancel_and_reset_clear_both_pending_types(self):
        for command in ("取消", "新对话"):
            self.seed()
            self.turn("改成5日")
            self.turn(command)
            self.assertIsNone(self.session.pending_edit)
            self.assertIsNone(self.session.pending)
            self.assertEqual(self.turn("SMA3").code, "no_pending_indicator_edit")

    def test_cancelled_late_model_response_cannot_republish_pending_or_old_base(self):
        original = self.seed()
        self.turn("改成5日")
        control = TurnControl()
        amended = replace(original.conditions, indicators=(IndicatorSpec("sma", window=5), *SPECS[1:]))
        proposed = replace(original, request=amended.for_security("000001.SZ"), conditions=amended)
        late = self.model_for(proposed)
        model = Mock()
        def cancel_before_return(messages, *, tools):
            self.assertIsNone(self.session.pending_edit)
            self.assertIsNone(self.session.confirmed)
            control.cancel()
            return late.complete(messages, tools=tools)
        model.complete.side_effect = cancel_before_return
        result = self.turn("SMA3", model=model, control=control)
        self.assertEqual(result.code, "turn_cancelled")
        self.assertIsNone(self.session.pending_edit)
        self.assertIsNone(self.session.confirmed)
        self.assertEqual(self.turn("SMA3").code, "no_pending_indicator_edit")

    def test_failed_proposal_consumes_selection_without_retrying_on_short_reply(self):
        self.seed()
        self.turn("改成5日")
        result = self.turn("SMA3")
        self.assertNotEqual(result.status, "completed")
        self.assertIsNone(self.session.pending_edit)
        self.assertIsNone(self.session.confirmed)
        self.assertEqual(self.turn("SMA3").code, "no_pending_indicator_edit")

    def test_full_explicit_edit_can_replace_the_pending_request(self):
        original = self.seed()
        self.turn("改成5日")
        amended = replace(original.conditions, indicators=(IndicatorSpec("sma", window=5), *SPECS[1:]))
        proposed = replace(original, request=amended.for_security("000001.SZ"), conditions=amended)
        result = self.turn("把SMA3改成5日", model=self.model_for(proposed))
        self.assertEqual(result.status, "completed")
        self.assertIsNone(self.session.pending_edit)

    def test_explicit_history_view_does_not_consume_indicator_pending(self):
        self.seed()
        self.turn("改成5日")
        pending = self.session.pending_edit
        result = self.turn("查看上次结果")
        self.assertEqual(result.code, "no_saved_result")
        self.assertIs(self.session.pending_edit, pending)

    def test_fork_and_publish_preserve_edit_pending_until_explicit_consumption(self):
        self.seed()
        self.turn("改成5日")
        working = self.session._fork_for_turn()
        self.assertIs(working.pending_edit, self.session.pending_edit)
        working.clear_pending()
        self.assertIsNotNone(self.session.pending_edit)
        self.session._publish_turn(working)
        self.assertIsNone(self.session.pending_edit)

    def test_window_label_roundtrips_all_supported_explicit_instances(self):
        for spec in (*SPECS, IndicatorSpec("ema", window=3), IndicatorSpec("atr", window=2),
                     IndicatorSpec("volume", window=3), IndicatorSpec("boll", window=3, stddev_multiplier=2.5),
                     IndicatorSpec("macd", fast=2, slow=3, signal=2)):
            self.assertEqual(indicator_selection(indicator_selection_label(spec)), spec)

    def test_relative_month_and_thirty_days_share_deterministic_calendar_dates(self):
        original = self.seed()
        for text in ("改成最近一个月", "把日期改成最近30天", "请把日期改成过去三十自然日，其他不变"):
            prepared = self.prepare(text)
            self.assertEqual(prepared.code, "relative_range_ready")
            self.assertEqual((prepared.request.start_date, prepared.request.as_of_date),
                             (date(2026, 8, 27), CUTOFF))
            self.assertEqual(prepared.request.indicators, original.request.indicators)
            self.assertEqual(prepared.request.instrument_id, original.request.instrument_id)
            self.assertIn("dates:relative_update", prepared.applied_defaults)

    def test_relative_date_turn_runs_a_fresh_research_and_exposes_calendar_rule(self):
        original = self.seed()
        prepared = self.prepare("改成最近一个月")
        result = self.turn("改成最近一个月", model=self.model_for(prepared))
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertNotEqual(result.confirmed_request.request_id, original.request.request_id)
        self.assertIn("月按30天", result.message)
        self.assertIn("2026-08-27", result.message)

    def test_relative_date_limits_future_and_multidimension_requests_are_not_guessed(self):
        self.seed()
        for text in ("把日期改成今天", "把日期改成未来30天", "把日期改成最近30个交易日",
                     "改成最近一个月，另外换成中国平安", "把日期改成最近两年"):
            result = self.turn(text)
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_same_date_refresh_runs_once_and_does_not_claim_new_market_data(self):
        original = self.seed()
        prepared = self.prepare("刷新行情")
        self.assertEqual(prepared.code, "refresh_latest_ready")
        result = self.turn("刷新行情", model=self.model_for(prepared))
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertNotEqual(result.confirmed_request.request_id, original.request.request_id)
        self.assertEqual(result.confirmed_request.start_date, START)
        self.assertEqual(result.confirmed_request.as_of_date, CUTOFF)
        self.assertIn("refresh:same_dates", result.understanding.applied_defaults)
        self.assertIn("区间未变化", result.message)
        self.assertIn("固定参考日期的合成样例", result.message)
        self.assertIn("不是实时行情", result.message)

    def test_refresh_variants_keep_original_calendar_span_and_previous_day_cutoff(self):
        original = self.seed(start=date(2026, 9, 18), cutoff=date(2026, 9, 22))
        for text in ("看最新的", "更新到最新", "刷新行情", "请刷新日线，其他不变"):
            prepared = self.prepare(text)
            self.assertEqual(prepared.code, "refresh_latest_ready")
            self.assertEqual((prepared.request.start_date, prepared.request.as_of_date), (START, CUTOFF))
            self.assertEqual(prepared.request.indicators, original.request.indicators)
            self.assertNotIn("refresh:same_dates", prepared.applied_defaults)

    def test_refresh_is_whole_message_and_does_not_silently_accept_additional_tasks(self):
        self.seed()
        for text in ("刷新行情，换成中国平安", "看最新的然后买入", "更新到最新并且添加SMA20"):
            result = self.turn(text)
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_online_refresh_uses_yesterday_even_if_it_is_not_a_trading_day(self):
        original = self.seed(source="tencent")
        updated = update_conditions(original.conditions, parse_condition_update("刷新行情"),
            instrument_id="000001.SZ", reference_date=date(2026, 9, 28))
        self.assertEqual(updated.as_of_date, date(2026, 9, 27))
        self.assertEqual((updated.as_of_date - updated.start_date).days, 4)
        self.assertEqual(updated.source, "tencent")

    def test_relative_year_keeps_existing_365_day_policy_not_calendar_years(self):
        prepared = self.seed()
        updated = update_conditions(prepared.conditions, parse_condition_update("改成最近一年"),
            instrument_id="000001.SZ", reference_date=NOW.date())
        self.assertEqual((updated.as_of_date - updated.start_date).days + 1, 365)
        self.assertIn("relative_unit:30_days_per_month_365_per_year", updated.applied_defaults)

    def test_no_confirmed_base_cannot_invent_scope_for_refresh_or_relative_update(self):
        for text in ("看最新的", "刷新行情", "改成最近一个月", "改成5日"):
            result = self.turn(text)
            self.assertEqual(result.code, "no_confirmed_research")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))


if __name__ == "__main__":
    unittest.main()
