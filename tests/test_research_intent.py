from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_natural_research_turn
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.natural_agent_demo import main
from trading_companion.research_intent import INTENT_TOOL_NAME, prepare_research_question
from trading_companion.research_tool import ResearchToolExecutor, TOOL_NAME
from trading_companion.security_directory import Security, SecurityDirectory, load_security_directory
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "sample_daily_bars_synthetic.csv"
DIRECTORY = ROOT / "data" / "security_directory_fixture.json"
REFERENCE = date(2026, 9, 26)
QUESTION = "看看平安银行最近的走势，关注3日均线和量能"
SECRET = "private-error-marker-never-export"


def intent_arguments(mentions=None, *, expression="最近", indicators=None, task="research", reason=None):
    return {"intent": task, "instrument_mentions": ["平安银行"] if mentions is None else mentions,
            "date_expression": expression, "indicator_mentions": ["3日均线", "量能"] if indicators is None else indicators,
            "clarification_reason": reason}


def interpretation(values=None):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(
        "intent_1", INTENT_TOOL_NAME, intent_arguments() if values is None else values),))


def analysis_proposal(*, call_id="analysis_1", **changes):
    values = {"instrument_id": "000001.SZ", "start_date": "2025-09-26", "as_of_date": "2026-09-25",
              "indicators": [{"kind": "sma", "window": 3}, {"kind": "volume", "window": 20}]}
    values.update(changes)
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(call_id, TOOL_NAME, values),))


class GuardedTests(unittest.TestCase):
    def setUp(self):
        self.directory = load_security_directory(DIRECTORY)
        self.source = CsvDailyBarSource(FIXTURE, is_synthetic=True)
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.post", side_effect=AssertionError("Unexpected model HTTP")),
            patch("requests.get", side_effect=AssertionError("Unexpected directory or price HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No real credential reads")),
        ):
            context.start()
            self.addCleanup(context.stop)
        market = patch("trading_companion.research_tool._build_source",
                       side_effect=AssertionError("Unexpected online source construction"))
        self.market = market.start()
        self.addCleanup(market.stop)

    def prepare(self, text=QUESTION, values=None, *, directory=None, **configuration):
        model = FakeToolModel([interpretation(values)])
        result = prepare_research_question(text, model=model, directory=directory or self.directory,
                                           source="csv", reference_date=REFERENCE, development=True,
                                           **configuration)
        self.assertEqual(model.responses_consumed, 1)
        self.market.assert_not_called()
        return result

    def run_turn(self, responses, *, text=QUESTION, **configuration):
        model = FakeToolModel(responses)
        result = run_natural_research_turn(text, model=model, directory=self.directory,
                                          development_source=self.source, reference_date=REFERENCE,
                                          **configuration)
        return result, model


class DirectoryTests(GuardedTests):
    def test_snapshot_preserves_source_timestamp_and_fixture_scope(self):
        self.assertTrue(self.directory.is_fixture)
        self.assertEqual(len(self.directory.securities), 4)
        self.assertEqual(self.directory.metadata()["retrieved_at"], "2026-09-26T00:00:00+08:00")
        self.assertIn("not_historical", self.directory.metadata()["identity_scope"])

    def test_exact_name_bare_code_and_qualified_code_resolve_the_same_security(self):
        for mention in ("平安银行", "000001", "000001.sz", "０００００１"):
            result = self.directory.lookup(mention)
            self.assertEqual(result.status, "resolved")
            self.assertEqual(result.candidates[0].instrument_id, "000001.SZ")

    def test_ambiguous_partial_and_unknown_names_are_not_auto_selected(self):
        for mention, expected, count in (("平安", "ambiguous", 2), ("贵州", "needs_confirmation", 1),
                                         ("不存在的公司", "not_found", 0)):
            result = self.directory.lookup(mention)
            self.assertEqual((result.status, len(result.candidates)), (expected, count))

    def test_aliases_require_snapshot_evidence_and_cannot_hide_other_candidates(self):
        self.assertEqual(self.directory.lookup("茅台").status, "resolved")
        changed = replace(self.directory, securities=(
            replace(self.directory.securities[0], aliases=("平安",)), *self.directory.securities[1:]))
        self.assertEqual(changed.lookup("平安").status, "ambiguous")

    def test_st_and_star_names_are_not_stripped_or_excluded(self):
        directory = replace(self.directory, securities=(*self.directory.securities, Security("600001.SH", "*ST测试")))
        self.assertEqual(directory.lookup("*ST测试").status, "resolved")
        self.assertEqual(directory.lookup("中芯国际").candidates[0].instrument_id, "688981.SH")
        self.assertNotIn("st_status", directory.lookup("*ST测试").candidates[0].to_dict())

    def test_longest_literal_mentions_prevent_nested_alias_false_positives(self):
        question = "看看中国平安和000001.SZ"
        spans = self.directory.mentioned_spans(question)
        self.assertEqual([question[start:end] for start, end in spans], ["中国平安", "000001.SZ"])

    def test_directory_requires_recent_aware_time_and_cannot_authorize_live_with_fixture(self):
        for reference, development in ((REFERENCE, False), (REFERENCE + timedelta(days=8), True),
                                        (REFERENCE - timedelta(days=1), True)):
            with self.assertRaises(DataValidationError):
                self.directory.assert_usable(reference, development=development)
        self.directory.assert_usable(REFERENCE + timedelta(days=7), development=True)
        with self.assertRaises(DataValidationError):
            replace(self.directory, retrieved_at=datetime(2026, 9, 26))

    def test_invalid_or_duplicate_security_records_fail_instead_of_overwriting(self):
        for records in ((), (self.directory.securities[0], self.directory.securities[0]), ("invalid",)):
            with self.assertRaises(DataValidationError):
                replace(self.directory, securities=records)
        for values in (("000001.SH", "错误"), ("000001.SZ", ""), ("000001.SZ", "bad\nname")):
            with self.assertRaises(DataValidationError):
                Security(*values)

    def test_snapshot_loader_rejects_unknown_fields_duplicate_json_and_invalid_rows(self):
        original = json.loads(DIRECTORY.read_text(encoding="utf-8"))
        cases = ['{"source":"one","source":"two"}', "[]"]
        for change in ({"path": SECRET}, {"retrieved_at": "not-a-time"},
                       {"securities": [{"instrument_id": "000001.SZ", "name": "平安银行", "url": SECRET}]},
                       {"is_fixture": "false"}):
            values = deepcopy(original)
            values.update(change)
            cases.append(json.dumps(values))
        for content in cases:
            with patch.object(Path, "read_text", return_value=content), self.assertRaises(DataValidationError):
                load_security_directory(DIRECTORY)

    def test_snapshot_read_errors_and_oversized_files_do_not_leak_paths(self):
        with patch.object(Path, "stat", side_effect=OSError(SECRET)), self.assertRaises(DataSourceError) as caught:
            load_security_directory(DIRECTORY)
        self.assertNotIn(SECRET, str(caught.exception))
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=5_000_001)), \
                self.assertRaises(DataValidationError):
            load_security_directory(DIRECTORY)


class IntentTests(GuardedTests):
    def test_model_receives_raw_question_before_directory_resolution_and_returns_one_candidate(self):
        fake = FakeToolModel([interpretation()])
        model = Mock()
        model.complete.side_effect = fake.complete
        result = prepare_research_question(QUESTION, model=model, directory=self.directory,
                                           source="csv", reference_date=REFERENCE, development=True)
        messages = model.complete.call_args.args[0]
        self.assertEqual(messages[-1].content, QUESTION)
        self.assertIn("2026-09-26", messages[0].content)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.security.instrument_id, "000001.SZ")
        self.assertEqual(result.model_calls_attempted, 1)

    def test_stock_name_needs_no_code_and_omitted_dates_and_indicators_use_visible_defaults(self):
        result = self.prepare("帮我分析贵州茅台", intent_arguments(["贵州茅台"], expression=None, indicators=[]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.request.instrument_id, "600519.SH")
        self.assertEqual((result.request.start_date, result.request.as_of_date), (date(2025, 9, 26), date(2026, 9, 25)))
        self.assertEqual(len(result.request.indicators), 8)
        self.assertIn("indicators:default_eight", result.applied_defaults)
        self.assertIn("range:365_calendar_days", result.to_dict()["applied_defaults"])

    def test_ambiguous_name_returns_directory_candidates_without_trusting_model_text_confidence(self):
        values = intent_arguments(["平安"], indicators=[])
        response = replace(interpretation(values), text="99% sure it means the bank.")
        model = FakeToolModel([response])
        result = prepare_research_question("看看平安最近走势", model=model, directory=self.directory,
                                           source="csv", reference_date=REFERENCE, development=True)
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(len(result.candidates), 2)
        self.assertIn("中国平安", result.message)
        self.assertIsNone(result.request)

    def test_model_cannot_replace_raw_ambiguous_name_with_a_remembered_name_or_code(self):
        for mention in ("平安银行", "000001.SZ"):
            result = self.prepare("看看平安最近走势", intent_arguments([mention], indicators=[]))
            self.assertEqual(result.code, "ungrounded_intent")
            self.assertIsNone(result.request)

    def test_model_cannot_hide_second_stock_or_conflicting_name_and_code(self):
        text = "看看平安银行和中国平安最近走势"
        dropped = self.prepare(text, intent_arguments(["平安银行"], indicators=[]))
        self.assertEqual(dropped.code, "instrument_not_grounded")
        both = self.prepare(text, intent_arguments(["平安银行", "中国平安"], indicators=[]))
        self.assertEqual(both.code, "instrument_needs_clarification")
        conflict = self.prepare("看看中国平安000001.SZ最近走势",
                                intent_arguments(["中国平安", "000001.SZ"], indicators=[]))
        self.assertEqual(conflict.code, "instrument_needs_clarification")

    def test_name_and_correct_code_can_identify_one_security_without_unnecessary_confirmation(self):
        result = self.prepare("看看平安银行000001.SZ最近走势",
                              intent_arguments(["平安银行", "000001.SZ"], indicators=[]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.candidates), 1)

    def test_unknown_name_unique_partial_and_missing_instrument_do_not_trigger_guesses(self):
        for text, mentions, expected in (("看看不存在公司最近走势", ["不存在公司"], "instrument_needs_clarification"),
                                         ("看看贵州最近走势", ["贵州"], "instrument_needs_clarification"),
                                         ("看看最近走势", [], "missing_instrument")):
            result = self.prepare(text, intent_arguments(mentions, indicators=[]))
            self.assertEqual(result.code, expected)

    def test_explicit_historical_dates_and_indicator_periods_are_preserved(self):
        expression = "2025-09-01至2025-09-05"
        result = self.prepare("看看平安银行" + expression + "走势，只看SMA3",
                              intent_arguments(expression=expression, indicators=["SMA3"]))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.request.start_date, date(2025, 9, 1))
        self.assertEqual(result.request.as_of_date, date(2025, 9, 5))
        self.assertEqual([spec.parameters for spec in result.request.indicators], [{"window": 3}])
        self.assertEqual(result.applied_defaults, ())

    def test_relative_dates_are_computed_by_program_and_month_policy_is_visible(self):
        for expression, days in (("最近一个月", 30), ("近3个月", 90), ("最近一周", 7),
                                  ("最近20日", 20), ("最近二十天", 20)):
            result = self.prepare("看看平安银行" + expression + "走势",
                                  intent_arguments(expression=expression, indicators=[]))
            self.assertEqual(result.status, "ready", result.message)
            self.assertEqual((result.request.as_of_date - result.request.start_date).days + 1, days)
            if "月" in expression:
                self.assertIn("relative_unit:30_days_per_month_365_per_year", result.applied_defaults)

    def test_omitted_or_shortened_dates_are_recovered_but_invented_dates_are_rejected(self):
        for expression in (None, "最近"):
            result = self.prepare("看看平安银行最近20日走势",
                                  intent_arguments(expression=expression, indicators=[]))
            self.assertEqual(result.status, "ready")
            self.assertEqual((result.request.start_date, result.request.as_of_date),
                             (date(2026, 9, 6), date(2026, 9, 25)))
            self.assertIn("date_expression:local_recognition", result.applied_defaults)
        invented = self.prepare("看看平安银行最近20日走势",
                                intent_arguments(expression="2026-09-25", indicators=[]))
        self.assertEqual(invented.code, "ungrounded_intent")
        half_year = self.prepare("看看平安银行近半年走势", intent_arguments(expression=None, indicators=[]))
        self.assertEqual(half_year.code, "date_not_grounded")

    def test_today_future_trading_days_and_invalid_ranges_need_clarification(self):
        for expression in ("今天", "最近20个交易日", "近半年", "上周", "2026-09-26", "2026-09-27",
                           "2026-02-30", "最近400天", "2025-01-01至2026-09-25"):
            model = FakeToolModel([interpretation(intent_arguments(expression=expression, indicators=[]))])
            directory = replace(self.directory, is_fixture=False)
            result = prepare_research_question("看看平安银行" + expression + "走势", model=model,
                                               directory=directory, reference_date=REFERENCE)
            self.assertEqual(result.status, "needs_clarification", expression)
            self.assertIsNone(result.request)

    def test_common_indicator_names_use_known_parameters_without_a_form(self):
        text = "看看平安银行最近走势，关注均线、EMA、MACD、RSI、布林带、ATR、量能"
        phrases = ["均线", "EMA", "MACD", "RSI", "布林带", "ATR", "量能"]
        result = self.prepare(text, intent_arguments(indicators=phrases))
        self.assertEqual(result.status, "ready", result.message)
        self.assertEqual(len(result.request.indicators), 8)
        self.assertEqual(result.request.indicators[0].parameters, {"window": 20})
        self.assertEqual(result.request.indicators[1].parameters, {"window": 60})
        chinese = self.prepare("看看平安银行最近走势，关注五日均线",
                               intent_arguments(indicators=["五日均线"]))
        self.assertEqual(chinese.status, "ready")
        self.assertEqual(chinese.request.indicators[0].parameters, {"window": 5})

    def test_model_cannot_drop_indicators_or_replace_explicit_period_with_generic_defaults(self):
        for indicators in ([], ["均线"]):
            result = self.prepare("看看平安银行最近走势，关注20日均线",
                                  intent_arguments(indicators=indicators))
            self.assertEqual(result.code, "indicator_not_grounded")
        wrong = self.prepare("看看平安银行最近走势，关注SMA20", intent_arguments(indicators=["SMA60"]))
        self.assertEqual(wrong.code, "ungrounded_intent")
        for original, shortened in (("五日均线", "均线"), ("SMA-3", "SMA"),
                                    ("SMA3.5", "SMA3"), ("-5日均线", "5日均线")):
            result = self.prepare("看看平安银行最近走势，关注" + original,
                                  intent_arguments(indicators=[shortened]))
            self.assertEqual(result.code, "indicator_not_grounded")

    def test_unsupported_and_invalid_indicators_are_not_silently_removed(self):
        for phrase in ("KDJ", "RSI1", "MACD(26,12,9)", "SMA0", "MACD(12,26", "SMA-3", "SMA3.5",
                       "-5日均线", "3.5日均线", "一百二日均线"):
            result = self.prepare("看看平安银行最近走势，关注" + phrase,
                                  intent_arguments(indicators=[phrase]))
            self.assertEqual(result.status, "needs_clarification", phrase)
            self.assertIsNone(result.request)

    def test_stale_or_future_snapshot_fails_after_interpretation_without_refreshing(self):
        for shift in (-8, 1):
            changed = replace(self.directory, retrieved_at=self.directory.retrieved_at + timedelta(days=shift))
            result = self.prepare(directory=changed)
            self.assertEqual(result.code, "directory_unusable")
            self.assertIsNone(result.request)

    def test_fixture_cannot_authorize_online_research_and_supported_source_is_checked(self):
        model = FakeToolModel([interpretation()])
        result = prepare_research_question(QUESTION, model=model, directory=self.directory, reference_date=REFERENCE)
        self.assertEqual(result.code, "directory_unusable")
        directory = replace(self.directory, is_fixture=False, securities=(Security("830001.BJ", "测试北交股"),))
        model = FakeToolModel([interpretation(intent_arguments(["测试北交股"], indicators=[]))])
        result = prepare_research_question("看看测试北交股最近走势", model=model, directory=directory, reference_date=REFERENCE)
        self.assertEqual(result.status, "needs_clarification")

    def test_source_requested_in_text_cannot_be_silently_ignored(self):
        result = self.prepare(QUESTION + "，使用东方财富数据")
        self.assertEqual(result.code, "source_selection_mismatch")

    def test_knowledge_unsupported_and_model_clarification_do_not_require_market_data(self):
        for task, text, expected in (("knowledge", "RSI超卖是什么意思", "knowledge_entry_not_connected"),
                                     ("unsupported", "帮我下单买入", "unsupported_intent"),
                                     ("clarify", "看看那个", "intent_needs_clarification")):
            result = self.prepare(text, intent_arguments([], expression=None, indicators=[], task=task))
            self.assertEqual(result.code, expected)
            self.assertIsNone(result.request)

    def test_bad_candidate_schema_and_unexpected_tools_are_rejected(self):
        variants = []
        for key, value in (("confidence", .99), ("path", SECRET), ("intent", "trade"),
                           ("instrument_mentions", "平安银行"), ("date_expression", 123),
                           ("indicator_mentions", [None])):
            values = intent_arguments()
            values[key] = value
            variants.append(values)
        missing = intent_arguments()
        del missing["intent"]
        variants.append(missing)
        for values in variants:
            self.assertEqual(self.prepare(values=values).code, "invalid_intent_response")
        for response in (ToolModelResponse("complete", "I guessed a stock."),
                         ToolModelResponse("refused", "No."), ToolModelResponse("incomplete", "Partial."),
                         analysis_proposal()):
            model = FakeToolModel([response])
            result = prepare_research_question(QUESTION, model=model, directory=self.directory,
                                               source="csv", reference_date=REFERENCE, development=True)
            self.assertEqual(result.status, "failed")

    def test_invalid_input_is_rejected_before_model_and_errors_are_sanitized(self):
        model = Mock()
        for question in (None, "", "x" * 2001):
            result = prepare_research_question(question, model=model, directory=self.directory)
            self.assertEqual(result.code, "invalid_input")
        model.complete.assert_not_called()
        for error in (ModelCallError("timeout", SECRET), ModelRequestError(SECRET), ModelConfigurationError(SECRET)):
            model.complete.side_effect = error
            result = prepare_research_question(QUESTION, model=model, directory=self.directory,
                                               source="csv", reference_date=REFERENCE, development=True)
            self.assertEqual(result.code, "intent_model_unavailable")
            self.assertEqual(result.model_calls_attempted, 1)
            self.assertNotIn(SECRET, json.dumps(result.to_dict()))


class NaturalTurnTests(GuardedTests):
    def test_named_question_reuses_actual_synthetic_calculations_and_keeps_three_call_budget(self):
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
            result, model = self.run_turn([interpretation(), analysis_proposal(), ToolModelResponse("complete", "Unchecked.")])
        self.assertEqual((result.status, result.code), ("limited", "partial_research"))
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (3, 1))
        self.assertEqual(model.responses_consumed, 3)
        self.assertEqual(len(result.model_responses), 3)
        self.assertAlmostEqual(result.research_result.indicators[0].latest["value"].value, 10.166666666666666)
        self.assertEqual(result.understanding.security.name, "平安银行")
        self.assertEqual(result.answer_validation, "not_performed")
        self.assertEqual(result.to_dict()["input_mode"], "natural")
        read.assert_called_once()

    def test_ambiguity_never_constructs_executor_or_reads_prices(self):
        with patch("trading_companion.agent.ResearchToolExecutor", side_effect=AssertionError("No ambiguous research")):
            result, model = self.run_turn([interpretation(intent_arguments(["平安"], indicators=[])), analysis_proposal()],
                                          text="看看平安最近走势")
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (1, 0))
        self.assertEqual(model.responses_consumed, 1)

    def test_resolved_identity_cannot_be_changed_by_later_analysis_proposal(self):
        result, model = self.run_turn([interpretation(), analysis_proposal(instrument_id="601318.SH")])
        self.assertEqual(result.code, "proposal_request_mismatch")
        self.assertEqual(result.confirmed_request.instrument_id, "000001.SZ")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 0))
        self.assertEqual(model.responses_consumed, 2)

    def test_third_model_failure_preserves_research_and_never_repeats_analysis(self):
        model = Mock()
        model.complete.side_effect = [interpretation(), analysis_proposal(), ModelCallError("timeout", SECRET)]
        result = run_natural_research_turn(QUESTION, model=model, directory=self.directory,
                                          development_source=self.source, reference_date=REFERENCE)
        self.assertEqual(result.code, "explanation_unavailable")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (3, 1))
        self.assertIsNotNone(result.research_result)
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))

    def test_repeated_tool_proposal_cannot_start_fourth_call_or_reset_executor(self):
        with patch("trading_companion.agent.ResearchToolExecutor", side_effect=ResearchToolExecutor) as factory:
            result, model = self.run_turn([interpretation(), analysis_proposal(), analysis_proposal(call_id="analysis_2"),
                                          ToolModelResponse("complete", "Unused.")])
        self.assertEqual(result.failure_code, "unexpected_followup_call")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (3, 1))
        self.assertEqual(model.responses_consumed, 3)
        factory.assert_called_once()

    def test_source_failure_stops_before_explanation_and_does_not_reset_understanding_count(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(SECRET)):
            result, model = self.run_turn([interpretation(), analysis_proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(result.status, "failed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(model.responses_consumed, 2)

    def test_export_does_not_include_raw_question_or_model_interpretation_text(self):
        response = replace(interpretation(), text=SECRET)
        result, _ = self.run_turn([response, analysis_proposal(), ToolModelResponse("complete", "Unchecked.")])
        exported = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET, exported)
        self.assertNotIn(QUESTION, exported)

    def test_demo_is_strict_json_with_separate_research_and_clarification_cases(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["directory_is_fixture"])
        self.assertFalse(payload["model_network_used"])
        self.assertFalse(payload["market_network_used"])
        self.assertEqual(payload["result"]["model_calls_attempted"], 3)
        self.assertEqual(payload["clarification_example"]["result"]["analyses_used"], 0)
        json.dumps(payload, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
