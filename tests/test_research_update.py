from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_natural_research_turn
from trading_companion.agent_input import InputIssue, request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_intent import INTENT_TOOL_NAME, PreparedResearchInput, ResearchConditions
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.research_update import is_condition_update, parse_condition_update, update_conditions
from trading_companion.security_directory import Security, load_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
START, CUTOFF = date(2026, 9, 21), date(2026, 9, 25)
SPECS = (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2))
QUESTION = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
PRIVATE = "private-update-failure-never-display"


def conditions(specs=SPECS, *, source="csv", flags=()):
    return ResearchConditions(START, CUTOFF, specs, source, NOW.date(), flags)


def request(specs=SPECS, *, instrument="000001.SZ", start=START, cutoff=CUTOFF, source="csv"):
    return MultiIndicatorRequest(instrument, start, cutoff, specs, source=source)


def intent():
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("update_intent", INTENT_TOOL_NAME, {
        "intent": "research", "instrument_mentions": ["平安银行"], "date_expression": "2026-09-21 至 2026-09-25",
        "indicator_mentions": ["SMA3", "RSI2"], "clarification_reason": None}),))


def proposal(specs=SPECS, **kwargs):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(
        "update_analysis", TOOL_NAME, request_arguments(request(specs, **kwargs))),))


class UpdateParsingTests(unittest.TestCase):
    def apply(self, question, original=None):
        return update_conditions(conditions() if original is None else original,
            parse_condition_update(question), instrument_id="000001.SZ", reference_date=NOW.date())

    def test_stock_names_codes_fullwidth_and_complete_suffixes(self):
        for text, expected in (("换成中国平安", "中国平安"), ("请把股票改为600519.SH，其余不变。", "600519.SH"),
                               ("改看平安", "平安"), ("换成６８８９８１．ＳＨ", "688981.SH"),
                               ("将研究对象换为*ST测试", "*ST测试")):
            with self.subTest(text=text):
                actual = parse_condition_update(text)
                self.assertEqual((actual.kind, actual.value), ("instrument", expected))
                with self.assertRaises(FrozenInstanceError):
                    actual.value = "other"

    def test_explicit_date_range_start_and_cutoff_preserve_other_fields(self):
        for text, start, cutoff in (("把日期改成2026-09-22至2026-09-24", date(2026, 9, 22), date(2026, 9, 24)),
            ("请将研究区间调整为2026-09-21～2026-09-24，其他不变", START, date(2026, 9, 24)),
            ("把开始日期改成2026-09-22", date(2026, 9, 22), CUTOFF),
            ("截止日期改为2026-09-24", START, date(2026, 9, 24))):
            with self.subTest(text=text):
                updated = self.apply(text)
                self.assertEqual((updated.start_date, updated.as_of_date), (start, cutoff))
                self.assertEqual(updated.indicators, SPECS)
                self.assertEqual(updated.source, "csv")

    def test_addition_accepts_one_explicit_instance_for_each_supported_family(self):
        for text, kind in (("添加SMA5", "sma"), ("加上EMA3", "ema"), ("增加RSI3", "rsi"),
                           ("添加ATR2", "atr"), ("添加BOLL(3,2.5)", "boll"),
                           ("添加MACD(2,3,2)", "macd"), ("添加VOLUME3", "volume"), ("添加5日均线", "sma")):
            with self.subTest(text=text):
                updated = self.apply(text)
                self.assertEqual(updated.indicators[:2], SPECS)
                self.assertEqual(updated.indicators[-1].kind, kind)
                self.assertEqual((updated.start_date, updated.as_of_date), (START, CUTOFF))

    def test_addition_does_not_guess_parameters_or_silently_drop_an_extra_instance(self):
        for text in ("添加RSI", "添加均线", "添加SMA5、ATR2", "添加RSI3和ATR2", "添加KDJ9",
                     "添加ATR1", "添加EMA121", "添加BOLL(3,nan)"):
            with self.subTest(text=text), self.assertRaises(InputIssue):
                self.apply(text)

    def test_duplicate_and_thirteenth_instance_are_rejected(self):
        with self.assertRaises(InputIssue) as caught:
            self.apply("添加RSI2")
        self.assertEqual(caught.exception.code, "condition_update_duplicate")
        original = conditions(tuple(IndicatorSpec("sma", window=n) for n in range(1, 13)))
        with self.assertRaises(InputIssue):
            self.apply("添加EMA3", original)
        self.assertEqual(len(original.indicators), 12)

    def test_removal_selects_one_exact_or_unique_existing_instance(self):
        for text in ("删除RSI2", "移除RSI", "去掉RSI(2)，其他不变"):
            self.assertEqual(self.apply(text).indicators, (SPECS[0],))
        original = conditions((SPECS[0], IndicatorSpec("sma", window=5), SPECS[1]))
        self.assertEqual(self.apply("删除SMA3", original).indicators, original.indicators[1:])

    def test_ambiguous_missing_and_final_indicator_removal_are_rejected(self):
        cases = (("删除均线", conditions((SPECS[0], IndicatorSpec("ema", window=3))), "condition_update_ambiguous"),
                 ("删除SMA20", conditions(), "condition_update_target_missing"),
                 ("删除RSI2", conditions((SPECS[1],)), "condition_update_empty_indicators"))
        for text, original, code in cases:
            with self.subTest(text=text), self.assertRaises(InputIssue) as caught:
                self.apply(text, original)
            self.assertEqual(caught.exception.code, code)

    def test_invalid_reverse_overlong_and_unchanged_date_ranges_do_not_apply(self):
        for text in ("把日期改成2026-02-30至2026-09-25", "把日期改成2026-09-25至2026-09-21",
                     "把日期改成2020-01-01至2026-09-25", "把日期改成2026-09-21至2026-09-25"):
            with self.subTest(text=text), self.assertRaises(InputIssue):
                self.apply(text)

    def test_current_online_cutoff_rule_is_enforced_after_date_merge(self):
        for text in ("把截止日期改成2026-09-26", "把截止日期改成2026-09-27"):
            with self.assertRaises(InputIssue):
                self.apply(text, conditions(source="tencent"))

    def test_applied_default_labels_follow_the_changed_dimension(self):
        original = conditions(flags=("cutoff:previous_shanghai_day", "range:365_calendar_days",
            "relative_unit:30_days_per_month_365_per_year", "indicators:default_eight"))
        added = self.apply("添加ATR2", original)
        self.assertNotIn("indicators:default_eight", added.applied_defaults)
        self.assertIn("range:365_calendar_days", added.applied_defaults)
        dated = self.apply("把截止日期改成2026-09-24", original)
        self.assertEqual(dated.applied_defaults, ("indicators:default_eight", "dates:explicit_update"))

    def test_whole_stock_and_date_commands_reject_negation_extra_clauses_and_control_chars(self):
        for text in ("不要换成中国平安", "换成中国平安，然后买入", "换成中国平安并且添加RSI2",
                     "换成中国平安；source=csv", "换成中国平安为什么高", "换成中国平安\n其他不变", "换成中国平安\x16",
                     "把日期改成2026-09-21至2026-09-24，另外换成茅台", "换成000001.SZ/path",
                     "把日期改成最近一个月，另外换成茅台", "把截止日期改成今天"):
            with self.subTest(text=text), self.assertRaises(InputIssue):
                parse_condition_update(text)

    def test_invalid_mutation_cues_are_not_fresh_queries_and_old_indicator_edits_stay_separate(self):
        for text in ("添加", "添加ATR2", "删除RSI", "换成平安", "把日期改成今天", "换成中国平安为什么高"):
            self.assertTrue(is_condition_update(text), text)
        for text in (QUESTION, "把SMA3改成5日，其他不变", "这里RSI为什么偏高", "解释DIF和DEA是什么"):
            self.assertFalse(is_condition_update(text), text)


class GuardedTests(unittest.TestCase):
    def setUp(self):
        self.directory = load_security_directory(ROOT / "data/security_directory_fixture.json")
        self.source = CsvDailyBarSource(ROOT / "data/sample_daily_bars_synthetic.csv", is_synthetic=True)
        self.session = ResearchSession()
        for context in (patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No live market/directory")),
            patch("requests.post", side_effect=AssertionError("No live model")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No online source"))):
            context.start()
            self.addCleanup(context.stop)

    def turn(self, question, *, model=None, **changes):
        config = {"model": FakeToolModel([]) if model is None else model, "session": self.session,
                  "directory": self.directory, "development_source": self.source, "current_time": NOW}
        config.update(changes)
        return run_natural_research_turn(question, **config)

    def begin(self):
        return self.turn(QUESTION, model=ExplanationDemoModel([intent(), proposal()]))

    def seed_online(self):
        directory = replace(self.directory, is_fixture=False)
        original = conditions(source="tencent")
        prepared = PreparedResearchInput(status="ready", request=original.for_security("000001.SZ"),
            conditions=original, security=directory.lookup("000001.SZ").candidates[0], directory=directory)
        self.session.remember_confirmed(prepared, now=NOW, development_source=None)
        return directory


class ConditionConversationTests(GuardedTests):
    def test_addition_runs_one_new_analysis_with_bound_parameters_and_a_new_id(self):
        first = self.begin()
        specs = (*SPECS, IndicatorSpec("atr", window=2))
        original_read = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original_read) as read:
            added = self.turn("添加ATR2，其他不变", model=ExplanationDemoModel([proposal(specs)]))
        self.assertEqual((added.status, added.model_calls_attempted, added.analyses_used), ("completed", 2, 1))
        self.assertEqual(added.session_event, "condition_update")
        self.assertEqual(added.confirmed_request.indicators, specs)
        self.assertNotEqual(added.confirmed_request.request_id, first.confirmed_request.request_id)
        self.assertIn("ATR(2)", added.display_answer)
        read.assert_called_once()

    def test_removal_research_and_following_date_edit_use_the_latest_successful_base(self):
        self.begin()
        removed = self.turn("删除RSI2", model=ExplanationDemoModel([proposal((SPECS[0],))]))
        dated = self.turn("把截止日期改成2026-09-24", model=ExplanationDemoModel([
            proposal((SPECS[0],), cutoff=date(2026, 9, 24))]))
        self.assertEqual(dated.status, "completed")
        self.assertEqual(dated.confirmed_request.indicators, (SPECS[0],))
        self.assertEqual(dated.research_result.latest_trade_date, date(2026, 9, 24))
        self.assertNotEqual(removed.confirmed_request.request_id, dated.confirmed_request.request_id)
        self.assertEqual(self.session.to_dict()["saved_result"]["request_id"], dated.confirmed_request.request_id)

    def test_date_range_and_start_updates_do_not_reapply_default_dates_or_indicators(self):
        for text, start, cutoff in (("把日期改成2026-09-22至2026-09-25", date(2026, 9, 22), CUTOFF),
                                   ("把开始日期改成2026-09-22", date(2026, 9, 22), CUTOFF)):
            self.session = ResearchSession()
            self.begin()
            result = self.turn(text, model=ExplanationDemoModel([proposal(start=start, cutoff=cutoff)]))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.confirmed_request.indicators, SPECS)
            self.assertEqual(result.confirmed_request.start_date, start)

    def test_stock_switch_is_directory_grounded_not_a_model_supplied_code(self):
        self.begin()
        bars = [replace(bar, instrument_id="601318.SH") for bar in self.source.read_daily_bars()]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            switched = self.turn("换成中国平安，其他不变", model=ExplanationDemoModel([
                proposal(instrument="601318.SH")]))
        self.assertEqual(switched.status, "completed")
        self.assertEqual(switched.confirmed_request.instrument_id, "601318.SH")
        self.assertEqual(switched.confirmed_request.indicators, SPECS)
        self.assertEqual((switched.model_calls_attempted, switched.analyses_used), (2, 1))
        self.assertEqual(switched.understanding.security.name, "中国平安")
        self.assertIn("601318.SH", switched.display_answer)
        self.assertIn("人工样例", switched.display_answer)

    def test_ambiguous_stock_preserves_conditions_without_any_model_or_data_request(self):
        self.begin()
        original = self.session.confirmed
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No read")):
            result = self.turn("换成平安")
        self.assertEqual(result.code, "condition_update_instrument_unresolved")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
        self.assertEqual(len(self.session.pending.candidates), 2)
        self.assertEqual(self.session.pending.conditions, original.conditions)
        self.assertIs(self.session.confirmed, original)
        self.assertIsNone(result.confirmed_request)

    def test_ambiguous_stock_resumes_through_the_original_identity_gate(self):
        self.begin()
        self.turn("换成平安")
        self.assertEqual(self.turn("贵州茅台").code, "candidate_selection_mismatch")
        invalid = self.turn("平安银行，添加不支持的指标")
        self.assertEqual(invalid.status, "needs_clarification")
        self.assertEqual((invalid.model_calls_attempted, invalid.analyses_used), (0, 0))
        self.assertIsNotNone(self.session.pending)
        bars = [replace(bar, instrument_id="601318.SH") for bar in self.source.read_daily_bars()]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = self.turn("中国平安", model=ExplanationDemoModel([proposal(instrument="601318.SH")]))
        self.assertEqual(result.session_event, "clarification_resumed")
        self.assertEqual(result.status, "completed")
        self.assertIsNone(self.session.pending)
        self.assertEqual(result.confirmed_request.start_date, START)

    def test_unknown_and_partial_name_require_explicit_confirmation_not_guessing(self):
        for name in ("不存在的公司", "平安银"):
            self.session = ResearchSession()
            self.begin()
            result = self.turn("换成" + name)
            self.assertEqual(result.code, "condition_update_instrument_unresolved")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
            self.assertIsNotNone(self.session.pending)

    def test_same_stock_noop_by_name_code_or_verified_alias_does_not_query(self):
        self.begin()
        original = self.session.to_dict()
        for text in ("换成平安银行", "换成平安 银行", "换成000001", "换成000001.SZ"):
            result = self.turn(text)
            self.assertEqual(result.code, "condition_update_unchanged")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
            self.assertEqual(self.session.to_dict(), original)

    def test_bare_indicator_switch_does_not_create_a_wrong_stock_selection_task(self):
        self.begin()
        original = self.session.to_dict()
        for text in ("换成RSI14", "改成MACD(2,3,2)", "换成均线", "换成腾讯", "换成今天", "换成最新"):
            result = self.turn(text)
            self.assertEqual(result.code, "condition_update_not_supported")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
            self.assertIsNone(self.session.pending)
            self.assertEqual(self.session.to_dict(), original)

    def test_verified_alias_and_star_st_new_listing_identities_are_not_filtered_out(self):
        for mention, instrument, name in (("茅台", "600519.SH", "贵州茅台"),
            ("中芯国际", "688981.SH", "中芯国际"), ("*ST测试", "000002.SZ", "*ST测试"),
            ("新上市测试", "688989.SH", "新上市测试")):
            self.session = ResearchSession()
            self.begin()
            directory = self.directory
            if directory.lookup(instrument).status != "resolved":
                directory = replace(directory, securities=(*directory.securities, Security(instrument, name)))
            bars = [replace(bar, instrument_id=instrument) for bar in self.source.read_daily_bars()]
            with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
                result = self.turn("换成" + mention, directory=directory,
                                   model=ExplanationDemoModel([proposal(instrument=instrument)]))
            self.assertEqual(result.status, "completed", mention)
            self.assertEqual(result.confirmed_request.instrument_id, instrument)
            self.assertEqual(result.understanding.security.name, name)

    def test_pending_stock_change_expires_without_renewal_from_invalid_replies(self):
        self.begin()
        self.turn("换成平安")
        pending = self.session.pending
        self.turn("添加ATR2", current_time=NOW + timedelta(minutes=14))
        self.assertIs(self.session.pending, pending)
        result = self.turn("中国平安", current_time=NOW + timedelta(minutes=15))
        self.assertEqual(result.code, "clarification_expired")
        self.assertIsNone(self.session.pending)
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_pending_stock_change_rechecks_candidate_identity_before_execution(self):
        self.begin()
        self.turn("换成平安")
        renamed = replace(self.directory, securities=tuple(
            Security(item.instrument_id, "更名测试") if item.instrument_id == "601318.SH" else item
            for item in self.directory.securities))
        result = self.turn("601318.SH", directory=renamed)
        self.assertEqual(result.code, "clarification_directory_changed")
        self.assertIsNone(self.session.pending)
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))

    def test_invalid_composite_commands_leave_both_base_and_saved_result_unchanged(self):
        self.begin()
        original = self.session.to_dict()
        for text in ("添加ATR2，另外换成中国平安", "删除RSI2并把日期改成2026-09-24", "添加RSI2", "删除SMA20",
                     "换成中国平安并且买入", "换成中国平安，使用东方财富", "把截止日期改成今天"):
            result = self.turn(text, current_time=NOW + timedelta(minutes=14))
            self.assertEqual(result.status, "needs_clarification", text)
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0), text)
            self.assertEqual(self.session.to_dict(), original)

    def test_updates_need_a_same_session_base_and_cannot_reactivate_history(self):
        for text in ("换成中国平安", "添加ATR2", "删除RSI2", "把日期改成2026-09-22至2026-09-25"):
            self.assertEqual(self.turn(text).code, "no_confirmed_research")
            self.assertEqual(self.turn(text, session=None).code, "no_confirmed_research")
        self.begin()
        self.session.begin_research()
        self.turn("查看上次结果")
        self.assertEqual(self.turn("添加ATR2").code, "no_confirmed_research")

    def test_model_cannot_change_inherited_stock_dates_source_or_unrequested_indicators(self):
        specs = (*SPECS, IndicatorSpec("atr", window=2))
        for kwargs in ({"instrument": "601318.SH"}, {"start": date(2026, 9, 20)},
                       {"cutoff": date(2026, 9, 24)}, {"source": "tencent"}, {}):
            self.session = ResearchSession()
            self.begin()
            response = proposal(SPECS if not kwargs else specs, **kwargs)
            result = self.turn("添加ATR2", model=FakeToolModel([response]))
            self.assertEqual(result.code, "proposal_request_mismatch")
            self.assertEqual(result.analyses_used, 0)
            self.assertIsNone(self.session.confirmed)

    def test_new_stock_without_fixture_bars_keeps_only_explicit_old_history(self):
        first = self.begin()
        result = self.turn("换成中国平安", model=FakeToolModel([proposal(instrument="601318.SH")]))
        self.assertEqual(result.code, "research_data_failed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (1, 1))
        self.assertIsNone(self.session.confirmed)
        self.assertEqual(self.turn("这里RSI多少").code, "no_current_result")
        history = self.turn("查看上次结果")
        self.assertEqual(history.followup_answer.request_id, first.confirmed_request.request_id)
        self.assertIn("000001.SZ", history.display_answer)
        self.assertIn("历史查看", history.display_answer)

    def test_source_failure_after_valid_merge_does_not_restore_the_old_base(self):
        self.begin()
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(PRIVATE)):
            result = self.turn("删除RSI2", model=FakeToolModel([proposal((SPECS[0],))]))
        self.assertEqual(result.code, "research_data_failed")
        self.assertIsNone(self.session.confirmed)
        self.assertNotIn(PRIVATE, json.dumps(result.to_dict()))
        self.assertEqual(self.turn("查看上次结果").code, "saved_result_shown")

    def test_expired_backward_clock_configuration_or_directory_changes_prevent_updates(self):
        for kwargs, expected in (({"current_time": NOW + timedelta(minutes=15)}, "research_context_expired"),
            ({"current_time": NOW - timedelta(seconds=1)}, "research_context_expired"),
            ({"development_source": None}, "research_context_configuration_changed"),
            ({"directory": replace(self.directory, retrieved_at=NOW - timedelta(days=8))}, "directory_unusable"),
            ({"directory": replace(self.directory, securities=self.directory.securities[1:])}, "research_context_identity_changed")):
            self.session = ResearchSession()
            self.begin()
            result = self.turn("添加ATR2", **kwargs)
            self.assertEqual(result.code, expected)
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
            self.assertIsNone(self.session.confirmed)

    def test_online_today_future_and_unsupported_board_do_not_construct_a_source(self):
        directory = self.seed_online()
        for text in ("把截止日期改成2026-09-26", "把日期改成2026-09-21至2026-09-27"):
            result = self.turn(text, directory=directory, development_source=None)
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
        directory = replace(directory, securities=(*directory.securities, Security("920001.BJ", "北交测试")))
        result = self.turn("换成北交测试", directory=directory, development_source=None)
        self.assertEqual(result.code, "invalid_condition_update")
        self.assertEqual(result.analyses_used, 0)

    def test_cancellation_of_stock_update_preserves_old_base_but_reset_clears_everything(self):
        self.begin()
        original = self.session.confirmed
        self.turn("换成平安")
        self.turn("取消")
        self.assertIsNone(self.session.pending)
        self.assertIs(self.session.confirmed, original)
        self.assertEqual(self.turn("查看上次结果").code, "saved_result_shown")
        self.turn("新对话")
        self.assertIsNone(self.session.confirmed)
        self.assertEqual(self.turn("查看上次结果").code, "no_saved_result")

    def test_original_date_anchor_is_preserved_for_indicator_or_stock_edits(self):
        self.begin()
        original = self.session.confirmed.conditions
        later = NOW + timedelta(minutes=1)
        result = self.turn("添加ATR2", model=ExplanationDemoModel([proposal((*SPECS, IndicatorSpec("atr", window=2)))]),
                           reference_date=NOW.date() + timedelta(days=1), current_time=later)
        self.assertEqual(result.understanding.conditions.reference_date, original.reference_date)
        self.assertEqual(result.confirmed_request.start_date, START)

    def test_pending_update_cannot_be_bypassed_by_another_mutation(self):
        self.begin()
        self.turn("换成平安")
        result = self.turn("添加ATR2")
        self.assertEqual(result.code, "clarification_reply_not_specific")
        self.assertIsNotNone(self.session.pending)

    def test_a_stale_explanation_cannot_be_attached_to_the_new_indicator_set(self):
        first = self.begin()
        result = self.turn("删除RSI2", model=FakeToolModel([
            proposal((SPECS[0],)), ToolModelResponse("complete", first.model_draft)]))
        self.assertEqual(result.explanation_validation.status, "rejected")
        self.assertIsNotNone(self.session.confirmed)
        self.assertEqual(self.session.confirmed.request.indicators, (SPECS[0],))

    def test_edit_base_is_consumed_before_model_and_interruption_preserves_only_history(self):
        self.begin()
        def interrupt(messages, *, tools):
            self.assertIsNone(self.session.confirmed)
            self.assertFalse(self.session.to_dict()["saved_result"]["is_current"])
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.turn("添加ATR2", model=Mock(complete=Mock(side_effect=interrupt)))
        self.assertIsNone(self.session.confirmed)
        self.assertEqual(self.turn("查看上次结果").code, "saved_result_shown")

    def test_same_session_lock_prevents_parallel_condition_mutations(self):
        self.begin()
        nested = []
        scripted = ExplanationDemoModel([proposal((*SPECS, IndicatorSpec("atr", window=2)))])
        def call(messages, *, tools):
            nested.append(self.turn("删除RSI2"))
            return scripted.complete(messages, tools=tools)
        result = self.turn("添加ATR2", model=Mock(complete=Mock(side_effect=call)))
        self.assertEqual(result.status, "completed")
        self.assertTrue(all(item.code == "session_busy" and item.analyses_used == 0 for item in nested))


if __name__ == "__main__":
    unittest.main()
