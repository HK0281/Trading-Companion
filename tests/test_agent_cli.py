from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.agent import AgentTurnResult, run_natural_research_turn
from trading_companion.agent_cli import ConversationData, _ConversationModel, _display_turn, main, run_conversation
from trading_companion.agent_input import request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.development_fixtures import REFERENCE_DATE, ROOT, load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.session_agent_demo import build_demo_model
from trading_companion.followup_demo import ConversationDemoModel, main as followup_demo_main
from trading_companion.research_followup import FOLLOWUP_TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


NOW = datetime(2026, 9, 26, 10, tzinfo=timezone(timedelta(hours=8)))
QUESTION = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
AMBIGUOUS = "看看平安最近一个月的走势，只看SMA3和RSI2"
PRIVATE = "private-cli-marker-never-print"


def intent(mention="平安银行", *, expression="2026-09-21 至 2026-09-25", indicators=None):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("cli_intent", INTENT_TOOL_NAME, {
        "intent": "research", "instrument_mentions": [mention], "date_expression": expression,
        "indicator_mentions": ["SMA3", "RSI2"] if indicators is None else indicators,
        "clarification_reason": None,
    }),))


def proposal(*, instrument="000001.SZ", source="csv", indicators=None, start="2026-09-21"):
    request = MultiIndicatorRequest(instrument, date.fromisoformat(start), date(2026, 9, 25),
        (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2)) if indicators is None else indicators,
        source=source)
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("cli_analysis", TOOL_NAME, request_arguments(request)),))


def final_payload(view):
    states = {row[0]: row[1] for row in view["tables"]["states"]["rows"]}
    row = next(row for row in view["tables"]["relations"]["rows"] if states[row[3]] == "available")
    return {"version": "indicator-explanation-v1", "request_id": view["request_id"],
            "instrument_id": view["instrument_id"], "trade_date": view["dates"][0],
            "claims": [{"evidence_id": row[0], "relation": row[2], "value": row[4]}]}


def edit_conversation_model():
    initial = ExplanationDemoModel([intent(), proposal()])
    amendment = ExplanationDemoModel([proposal(indicators=(IndicatorSpec("sma", window=5), IndicatorSpec("rsi", window=2)))])
    model = Mock()
    def complete(messages, *, tools):
        script = initial if initial.responses_consumed < 3 else amendment
        return script.complete(messages, tools=tools)
    model.complete.side_effect = complete
    return model


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market or directory HTTP")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No live source")),
        ):
            context.start()
            self.addCleanup(context.stop)
        context = patch("requests.post", side_effect=AssertionError("No real provider HTTP"))
        self.post = context.start()
        self.addCleanup(context.stop)
        self.data = ConversationData.from_options("synthetic")
        self.source, self.directory = load_development_fixtures()

    def cli(self, argv=(), replies=()):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors), patch("builtins.input", side_effect=list(replies)):
            code = main(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def chat(self, replies=(), *, model=None, data=None, clock=None):
        supplied = iter(replies)
        def read(prompt):
            try:
                value = next(supplied)
            except StopIteration:
                raise EOFError from None
            if isinstance(value, BaseException):
                raise value
            return value
        if model is None:
            model = ExplanationDemoModel([intent(), proposal()])
        output = []
        code = run_conversation(model=model, data=self.data if data is None else data,
            read_line=read, write_line=output.append, clock=(lambda: NOW) if clock is None else clock)
        return code, "\n".join(output), model


class EntryPointTests(GuardedTests):
    def test_no_arguments_only_show_help_without_credentials_files_or_input(self):
        with patch("trading_companion.agent_cli.ConversationData.from_options", side_effect=AssertionError("No reads")), \
             patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")):
            code, output, errors = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("--demo", output)
        self.assertIn("--provider", output)
        self.assertEqual(errors, "")
        self.post.assert_not_called()

    def test_help_exits_without_reading_configuration(self):
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_invalid_flag_combinations_are_rejected_before_any_runtime_setup(self):
        cases = (["--provider", "deepseek"], ["--data", "synthetic"], ["--directory", "anything.json"],
                 ["--demo", "--provider", "deepseek"], ["--demo", "--data", "synthetic"],
                 ["--demo", "--directory", "anything.json"], ["--provider", "fake"],
                 ["--provider", "deepseek", "--data", "synthetic", "--directory", "anything.json"],
                 ["--provider", "deepseek", "--data", "akshare_eastmoney"], ["--output", "report.json"],
                 ["--max-turns", "100"])
        with patch("trading_companion.agent_cli.ConversationData.from_options", side_effect=AssertionError("No setup")):
            for argv in cases:
                with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_demo_uses_real_session_flow_but_no_credentials_or_interactive_input(self):
        with patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")), \
             patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("Do not export debug payloads")):
            code, output, errors = self.cli(["--demo"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("离线固定脚本演示", output)
        self.assertIn("needs_clarification/instrument_needs_clarification", output)
        self.assertIn("completed/research_completed", output)
        self.assertIn("转发3次模型调用，已确认1次分析", output)
        self.assertNotIn('"model_draft"', output)
        self.post.assert_not_called()

    def test_demo_writes_no_reports_or_snapshots(self):
        with patch.object(Path, "write_text", side_effect=AssertionError("No writes")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("No writes")):
            self.assertEqual(self.cli(["--demo"])[0], 0)

    def test_modified_fixture_stops_before_live_configuration(self):
        with patch.object(Path, "read_bytes", return_value=PRIVATE.encode()), \
             patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")):
            code, output, errors = self.cli(["--provider", "deepseek", "--data", "synthetic"])
        self.assertEqual(code, 1)
        self.assertIn("directory_unusable", errors)
        self.assertNotIn(PRIVATE, output + errors)

    def test_missing_fixture_does_not_leak_path_or_underlying_exception(self):
        with patch.object(Path, "read_bytes", side_effect=OSError(PRIVATE)):
            code, output, errors = self.cli(["--demo"])
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, output + errors)

    def test_missing_model_configuration_exits_before_reading_input(self):
        code, output, errors = self.cli(["--provider", "deepseek", "--data", "synthetic"])
        self.assertEqual(code, 2)
        self.assertIn("model_configuration_error", errors)
        self.post.assert_not_called()

    def test_bad_key_is_not_echoed_in_configuration_error(self):
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "test-model", "DEEPSEEK_API_KEY": PRIVATE + "\x16"}):
            code, output, errors = self.cli(["--provider", "deepseek", "--data", "synthetic"])
        self.assertEqual(code, 2)
        self.assertNotIn(PRIVATE, output + errors)

    def test_missing_live_directory_stops_before_credentials_and_network(self):
        with patch("trading_companion.agent_cli.load_security_directory", side_effect=DataSourceError(PRIVATE)), \
             patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")):
            code, output, errors = self.cli(["--provider", "deepseek", "--data", "tencent"])
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, output + errors)
        self.post.assert_not_called()

    def test_fixture_directory_cannot_authorize_live_mode(self):
        with patch("trading_companion.agent_cli.load_security_directory", return_value=self.directory), \
             patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")):
            code, _, errors = self.cli(["--provider", "deepseek", "--data", "tencent"])
        self.assertEqual(code, 1)
        self.assertIn("directory_unusable", errors)

    def test_stale_or_future_live_directory_cannot_authorize_calls(self):
        for offset in (-8, 2):
            directory = replace(self.directory, is_fixture=False,
                                retrieved_at=datetime.now(timezone.utc) + timedelta(days=offset))
            with self.subTest(offset=offset), \
                 patch("trading_companion.agent_cli.load_security_directory", return_value=directory), \
                 patch("trading_companion.agent_cli.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")):
                self.assertEqual(self.cli(["--provider", "deepseek", "--data", "tencent"])[0], 1)

    def test_source_configuration_rejects_mixed_or_unpinned_modes(self):
        cases = (("other",), ("synthetic", Path("other.json"), self.source), ("synthetic",),
                 ("tencent", None), ("tencent", Path("live.json"), self.source),
                 ("synthetic", None, CsvDailyBarSource(Path("private.csv"), is_synthetic=True)),
                 ("synthetic", None, replace(self.source, is_synthetic=False)))
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ModelConfigurationError):
                ConversationData(*args)

    def test_default_and_explicit_live_paths_are_application_configuration(self):
        self.assertEqual(ConversationData.from_options("tencent").directory_path,
                         ROOT / "data/security_directory_live.json")
        self.assertEqual(ConversationData.from_options("tencent", "chosen.json").directory_path, Path("chosen.json"))

    def test_clean_process_demo_uses_only_fixed_offline_inputs(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "trading_companion.agent_cli", "--demo"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True,
            text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("转发3次模型调用，已确认1次分析", result.stdout)


class ConversationTests(GuardedTests):
    def test_full_offline_conversation_connects_research_followup_knowledge_edit_and_history(self):
        output = StringIO()
        with redirect_stdout(output), patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("No debug export")):
            self.assertEqual(followup_demo_main(), 0)
        text = output.getvalue()
        for expected in ("saved_question_answered", "indicator_knowledge_answered", "saved_result_shown", "SMA(5)", "10.21"):
            self.assertIn(expected, text)
        self.assertIn("已计入4条问题，转发6次模型调用，已确认2次分析", text)
        self.post.assert_not_called()

    def test_last_command_is_local_and_does_not_consume_input_or_model_allowance(self):
        code, output, model = self.chat([QUESTION, "/last", "/last", "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("已计入1条问题，转发3次模型调用，已确认1次分析", output)
        self.assertEqual(output.count("[completed/saved_result_shown]"), 2)
        self.assertIn("历史查看", output)

    def test_last_after_reset_has_no_saved_result_and_does_not_call_model(self):
        code, output, model = self.chat([QUESTION, "/reset", "/last", "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("no_saved_result", output)
        self.assertIn("结果快照也已清除", output)

    def test_knowledge_does_not_consume_model_allowance_but_counts_as_an_input(self):
        code, output, model = self.chat(["RSI是什么", "解释一下MACD里的DIF和DEA", "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 0))
        self.assertIn("已计入2条问题，转发0次模型调用，已确认0次分析", output)
        self.assertIn("Wilder", output)

    def test_history_after_failed_edit_is_explicit_without_silent_current_result_fallback(self):
        initial = ExplanationDemoModel([intent(), proposal()])
        model = Mock()
        def complete(messages, *, tools):
            if initial.responses_consumed < 3:
                return initial.complete(messages, tools=tools)
            raise ModelCallError("timeout", PRIVATE)
        model.complete.side_effect = complete
        code, output, _ = self.chat([QUESTION, "把SMA3改成5日", "这里RSI为什么高", "/last", "/exit"], model=model)
        self.assertEqual((code, model.complete.call_count), (0, 4))
        self.assertIn("no_current_result", output)
        self.assertIn("[completed/saved_result_shown]", output)
        self.assertNotIn(PRIVATE, output)

    def test_exit_and_interrupt_remove_the_result_snapshot(self):
        for ending in ("/exit", EOFError(), KeyboardInterrupt()):
            session = ResearchSession()
            with patch("trading_companion.agent_cli.ResearchSession", return_value=session):
                self.chat([QUESTION, ending])
            self.assertIsNone(session.to_dict()["saved_result"])

    def test_last_uses_actual_clock_for_expiry(self):
        times = iter((NOW, NOW + timedelta(minutes=16)))
        code, output, model = self.chat([QUESTION, "/last", "/exit"], clock=lambda: next(times))
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("saved_result_expired", output)

    def test_indicator_edit_reuses_session_and_displays_new_facts_without_debug_export(self):
        model = edit_conversation_model()
        with patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("No debug exports")):
            code, output, _ = self.chat([QUESTION, "把SMA3改成5日，其他不变", "/exit"], model=model)
        self.assertEqual((code, model.complete.call_count), (0, 5))
        self.assertIn("SMA(5)", output)
        self.assertIn("10.21", output)
        self.assertIn("仅替换本轮明确指定的一项", output)
        self.assertIn("转发5次模型调用，已确认2次分析", output)

    def test_reset_discards_confirmed_conditions_without_refunding_call_allowance(self):
        code, output, model = self.chat([QUESTION, "/reset", "把SMA3改成5日", "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("no_confirmed_research", output)
        self.assertIn("额度不重置", output)
        self.assertIn("转发3次模型调用，已确认1次分析", output)

    def test_invalid_edit_uses_no_model_allowance_and_a_complete_restatement_can_follow(self):
        model = edit_conversation_model()
        code, output, _ = self.chat([QUESTION, "把SMA3改成5日，另外换成中国平安",
                                     "把SMA3改成5日，其他不变", "/exit"], model=model)
        self.assertEqual((code, model.complete.call_count), (0, 5))
        self.assertIn("needs_clarification/invalid_indicator_edit", output)
        self.assertIn("转发5次模型调用，已确认2次分析", output)

    def test_eof_and_interrupt_clear_confirmed_conditions_as_well_as_pending_identity(self):
        for ending in (EOFError(), KeyboardInterrupt()):
            session = ResearchSession()
            with patch("trading_companion.agent_cli.ResearchSession", return_value=session):
                code, output, _ = self.chat([QUESTION, ending])
            self.assertIsNone(session.confirmed)
            self.assertIsNone(session.pending)
            self.assertIn("研究条件已清除", output)
            self.assertEqual(code, 130 if isinstance(ending, KeyboardInterrupt) else 0)

    def test_edit_expiry_uses_live_session_clock_not_the_fixture_reference_date(self):
        times = iter((NOW, NOW + timedelta(minutes=16)))
        code, output, model = self.chat([QUESTION, "把SMA3改成5日", "/exit"], clock=lambda: next(times))
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("research_context_expired", output)
        self.assertIn("转发3次模型调用，已确认1次分析", output)

    def test_failed_amendment_does_not_display_old_facts_under_the_new_turn(self):
        initial = ExplanationDemoModel([intent(), proposal()])
        model = Mock()
        def fail_amendment(messages, *, tools):
            if initial.responses_consumed < 3:
                return initial.complete(messages, tools=tools)
            raise ModelCallError("timeout", PRIVATE)
        model.complete.side_effect = fail_amendment
        code, output, _ = self.chat([QUESTION, "把SMA3改成5日", "把RSI2改成3日", "/exit"], model=model)
        self.assertEqual((code, model.complete.call_count), (0, 4))
        failure_display = output.split("[failed/model_unavailable]")[1]
        self.assertNotIn("收盘价：", failure_display)
        self.assertNotIn(PRIVATE, output)
        self.assertIn("no_confirmed_research", failure_display)

    def test_explicit_exit_and_eof_have_zero_model_or_analysis_calls(self):
        for replies in (["/exit"], []):
            code, output, model = self.chat(replies)
            self.assertEqual(code, 0)
            self.assertEqual(model.responses_consumed, 0)
            self.assertIn("已计入0条问题，转发0次模型调用，已确认0次分析", output)

    def test_help_blank_reset_and_unknown_slash_commands_are_local(self):
        code, output, model = self.chat(["", "  ", "/help", "/reset", "/not-a-command", "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 0))
        self.assertIn("未知本地命令", output)
        self.assertIn("已计入0条问题", output)

    def test_free_form_question_reuses_agent_and_checked_display(self):
        with patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("No debug exports")):
            code, output, model = self.chat([QUESTION, "/exit"])
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("completed/research_completed", output)
        self.assertIn("收盘价：10.4", output)
        self.assertIn("已核对的指标关系", output)
        self.assertIn("人工样例 / 非真实行情", output)

    def test_same_session_resumes_identity_and_preserves_original_conditions(self):
        with patch("trading_companion.agent_cli.run_natural_research_turn", wraps=run_natural_research_turn) as turn:
            code, output, model = self.chat([AMBIGUOUS, "我指的是平安银行", "/exit"], model=build_demo_model())
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIs(turn.call_args_list[0].kwargs["session"], turn.call_args_list[1].kwargs["session"])
        self.assertIn("候选：平安银行（000001.SZ）；中国平安（601318.SH）", output)
        self.assertIn("请求区间：2026-08-27 至 2026-09-25", output)

    def test_reset_discards_pending_identity_without_extra_model_calls(self):
        code, output, model = self.chat([AMBIGUOUS, "/reset", "平安银行", "/exit"], model=build_demo_model())
        self.assertEqual((code, model.responses_consumed), (0, 1))
        self.assertIn("no_pending_clarification", output)
        self.assertIn("已确认0次分析", output)

    def test_cancel_is_handled_by_existing_agent_without_another_model_call(self):
        code, output, model = self.chat([AMBIGUOUS, "取消", "/exit"], model=build_demo_model())
        self.assertEqual((code, model.responses_consumed), (0, 1))
        self.assertIn("cancelled/research_cancelled", output)

    def test_reply_cannot_silently_change_indicators(self):
        code, output, model = self.chat([AMBIGUOUS, "平安银行，改看RSI3", "/exit"], model=build_demo_model())
        self.assertEqual((code, model.responses_consumed), (0, 1))
        self.assertIn("clarification_reply_not_specific", output)
        self.assertIn("已确认0次分析", output)

    def test_full_new_question_replaces_pending_request_explicitly(self):
        model = ExplanationDemoModel([intent("平安", expression="最近一个月"), intent(), proposal()])
        code, output, _ = self.chat([AMBIGUOUS, "新问题：" + QUESTION, "/exit"], model=model)
        self.assertEqual((code, model.responses_consumed), (0, 4))
        self.assertIn("请求区间：2026-09-21 至 2026-09-25", output)

    def test_repeated_short_selection_cannot_repeat_analysis(self):
        code, output, model = self.chat([AMBIGUOUS, "平安银行", "平安银行", "/exit"], model=build_demo_model())
        self.assertEqual((code, model.responses_consumed), (0, 3))
        self.assertIn("no_pending_clarification", output)
        self.assertIn("已确认1次分析", output)

    def test_each_launch_has_an_independent_pending_session(self):
        self.chat([AMBIGUOUS, "/exit"], model=build_demo_model())
        _, output, model = self.chat(["平安银行", "/exit"])
        self.assertEqual(model.responses_consumed, 0)
        self.assertIn("no_pending_clarification", output)

    def test_eof_and_interrupt_clear_pending_state(self):
        for ending in (EOFError(), KeyboardInterrupt()):
            session = ResearchSession()
            with patch("trading_companion.agent_cli.ResearchSession", return_value=session):
                code, _, _ = self.chat([AMBIGUOUS, ending], model=build_demo_model())
            self.assertIsNone(session.pending)
            self.assertEqual(code, 130 if isinstance(ending, KeyboardInterrupt) else 0)

    def test_pending_expiration_uses_real_clock_not_synthetic_reference_date(self):
        times = iter((NOW, NOW + timedelta(minutes=16)))
        code, output, model = self.chat([AMBIGUOUS, "平安银行", "/exit"], model=build_demo_model(), clock=lambda: next(times))
        self.assertEqual((code, model.responses_consumed), (0, 1))
        self.assertIn("clarification_expired", output)

    def test_partial_result_retains_missing_fields_and_checked_available_relations(self):
        specs = (IndicatorSpec("sma", window=3), IndicatorSpec("sma", window=20), IndicatorSpec("volume", window=3))
        model = ExplanationDemoModel([intent(indicators=["SMA3", "SMA20", "VOLUME3"]), proposal(indicators=specs)])
        question = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3、SMA20、VOLUME3"
        code, output, _ = self.chat([question, "/exit"], model=model)
        self.assertEqual(code, 0)
        self.assertIn("limited/partial_research", output)
        self.assertIn("还缺15条记录", output)
        self.assertIn("已核对的指标关系", output)

    def test_unchecked_final_text_is_not_displayed(self):
        model = FakeToolModel([intent(), proposal(), ToolModelResponse("complete", PRIVATE)])
        _, output, _ = self.chat([QUESTION, "/exit"], model=model)
        self.assertNotIn(PRIVATE, output)
        self.assertIn("limited/explanation_rejected", output)
        self.assertIn("收盘价：10.4", output)
        self.assertIn("仅展示事实摘要", output)

    def test_wrong_stock_proposal_is_blocked_before_csv_read(self):
        model = FakeToolModel([intent(), proposal(instrument="600519.SH")])
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No changed-stock query")):
            _, output, _ = self.chat([QUESTION, "/exit"], model=model)
        self.assertIn("proposal_request_mismatch", output)
        self.assertEqual(model.responses_consumed, 2)

    def test_failed_intent_does_not_display_model_text(self):
        model = FakeToolModel([ToolModelResponse("complete", PRIVATE)])
        _, output, _ = self.chat([QUESTION, "/exit"], model=model)
        self.assertNotIn(PRIVATE, output)
        self.assertIn("invalid_intent_response", output)

    def test_timeout_during_explanation_preserves_fact_report_without_retry(self):
        model = Mock()
        model.complete.side_effect = [intent(), proposal(), ModelCallError("timeout", PRIVATE)]
        code, output, _ = self.chat([QUESTION, "/exit"], model=model)
        self.assertEqual(code, 0)
        self.assertEqual(model.complete.call_count, 3)
        self.assertIn("limited/explanation_unavailable", output)
        self.assertIn("收盘价：10.4", output)
        self.assertNotIn(PRIVATE, output)
        self.assertNotIn(PRIVATE, json.dumps([call.kwargs["json"] for call in self.post.call_args_list]))

    def test_overlong_questions_count_toward_turn_limit_without_model_calls(self):
        code, output, model = self.chat(["x" * 2001] * 6)
        self.assertEqual((code, model.responses_consumed), (0, 0))
        self.assertEqual(output.count("问题超过2000字符"), 5)
        self.assertIn("已达到本次启动的问题次数上限", output)

    def test_five_full_questions_have_at_most_fifteen_calls_and_five_analyses(self):
        delegates = iter(ExplanationDemoModel([intent(), proposal()]) for _ in range(5))
        current = [None]
        def complete(messages, *, tools):
            if tools[0].name == INTENT_TOOL_NAME:
                current[0] = next(delegates)
            return current[0].complete(messages, tools=tools)
        model = Mock()
        model.complete.side_effect = complete
        code, output, _ = self.chat([QUESTION] * 6, model=model)
        self.assertEqual((code, model.complete.call_count), (0, 15))
        self.assertIn("转发15次模型调用，已确认5次分析", output)

    def test_reset_does_not_replenish_problem_or_model_allowances(self):
        model = FakeToolModel([ToolModelResponse("complete", PRIVATE)] * 5)
        code, output, _ = self.chat([value for _ in range(6) for value in (QUESTION, "/reset")], model=model)
        self.assertEqual((code, model.responses_consumed), (0, 5))
        self.assertIn("已计入5条问题，转发5次模型调用", output)

    def test_visible_defaults_are_retained_even_when_the_sample_history_is_short(self):
        from trading_companion.multi_contracts import DEFAULT_INDICATORS
        model = ExplanationDemoModel([
            intent(expression="最近", indicators=[]), proposal(indicators=DEFAULT_INDICATORS, start="2025-09-26"),
        ])
        _, output, _ = self.chat(["看看平安银行最近走势", "/exit"], model=model)
        self.assertIn("默认区间为365个自然日", output)
        self.assertIn("默认8项组合（7类指标）", output)
        self.assertIn("2025-09-26 至 2026-09-25", output)
        self.assertIn("历史记录不足", output)

    def test_independent_cap_stops_cli_if_a_runner_attempts_too_many_calls(self):
        model = Mock()
        model.complete.return_value = ToolModelResponse("complete", "fixed offline response")
        def excessive_turn(question, *, model, **kwargs):
            for _ in range(16):
                try:
                    model.complete([], tools=())
                except ModelCallError:
                    break
            return AgentTurnResult()
        with patch("trading_companion.agent_cli.run_natural_research_turn", side_effect=excessive_turn):
            code, output, _ = self.chat([QUESTION], model=model)
        self.assertEqual((code, model.complete.call_count), (1, 15))
        self.assertIn("模型调用上限已触发", output)

    def test_current_directory_is_reloaded_before_every_nonlocal_input(self):
        data = ConversationData.from_options("tencent", "chosen.json")
        directory = replace(self.directory, is_fixture=False, retrieved_at=NOW)
        model = FakeToolModel([intent("平安", expression="最近一个月")])
        with patch("trading_companion.agent_cli.load_security_directory", return_value=directory) as load:
            code, output, _ = self.chat([AMBIGUOUS, "取消", "/exit"], model=model, data=data)
        self.assertEqual((code, load.call_count, model.responses_consumed), (0, 2, 1))
        self.assertIn("取消", output)

    def test_directory_failure_between_turns_stops_without_refresh_or_new_model_call(self):
        data = ConversationData.from_options("tencent", "chosen.json")
        directory = replace(self.directory, is_fixture=False, retrieved_at=NOW)
        model = FakeToolModel([intent("平安", expression="最近一个月")])
        with patch("trading_companion.agent_cli.load_security_directory", side_effect=[directory, DataSourceError(PRIVATE)]):
            code, output, _ = self.chat([AMBIGUOUS, "平安银行"], model=model, data=data)
        self.assertEqual((code, model.responses_consumed), (1, 1))
        self.assertIn("directory_unusable", output)
        self.assertNotIn(PRIVATE, output)

    def test_directory_expiry_between_turns_stops_before_resumed_analysis(self):
        data = ConversationData.from_options("tencent", "chosen.json")
        directory = replace(self.directory, is_fixture=False, retrieved_at=NOW)
        times = iter((NOW, NOW + timedelta(days=8)))
        with patch("trading_companion.agent_cli.load_security_directory", return_value=directory):
            code, output, model = self.chat([AMBIGUOUS, "平安银行"], data=data,
                model=FakeToolModel([intent("平安", expression="最近一个月")]), clock=lambda: next(times))
        self.assertEqual((code, model.responses_consumed), (1, 1))
        self.assertIn("已确认0次分析", output)

    def test_changed_directory_identity_cannot_resume_old_pending_request(self):
        data = ConversationData.from_options("tencent", "chosen.json")
        directory = replace(self.directory, is_fixture=False, retrieved_at=NOW)
        changed = replace(directory, securities=(replace(directory.securities[0], name="变更后名称"), *directory.securities[1:]))
        model = FakeToolModel([intent("平安", expression="最近一个月")])
        with patch("trading_companion.agent_cli.load_security_directory", side_effect=[directory, changed]):
            code, output, _ = self.chat([AMBIGUOUS, "000001.SZ", "/exit"], model=model, data=data)
        self.assertEqual((code, model.responses_consumed), (0, 1))
        self.assertIn("clarification_directory_changed", output)

    def test_interrupt_during_model_call_does_not_claim_known_zero_analysis(self):
        model = Mock()
        model.complete.side_effect = [intent(), proposal(), KeyboardInterrupt()]
        code, output, _ = self.chat([QUESTION], model=model)
        self.assertEqual((code, model.complete.call_count), (130, 3))
        self.assertIn("可能已取数", output)
        self.assertIn("仅含已返回的轮次", output)

    def test_display_uses_safe_property_not_debug_export_or_raw_draft(self):
        result = AgentTurnResult(model_draft=PRIVATE)
        output = []
        with patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("No debug export")):
            _display_turn(result, output.append)
        self.assertNotIn(PRIVATE, "\n".join(output))


class ProviderPathTests(GuardedTests):
    def install_http(self, *, final_finish="stop", source="csv"):
        def reply(url, **kwargs):
            body = kwargs["json"]
            last = body["messages"][-1]
            if last["role"] == "tool":
                message = {"role": "assistant", "content": json.dumps(final_payload(json.loads(last["content"])))}
                finish = final_finish
            else:
                response = intent() if body["tools"][0]["function"]["name"] == INTENT_TOOL_NAME else proposal(source=source)
                call = response.tool_calls[0]
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id,
                    "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}]}
                finish = "tool_calls"
            result = Mock(status_code=200)
            result.json.return_value = {"model": "test-cli-model", "choices": [{"finish_reason": finish, "message": message}]}
            return result
        self.post.side_effect = reply

    def live_cli(self, replies):
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "test-cli-model", "DEEPSEEK_API_KEY": PRIVATE}):
            return self.cli(["--provider", "deepseek", "--data", "synthetic"], replies)

    def test_explicit_deepseek_entry_uses_adapter_and_real_csv_with_mock_http(self):
        self.install_http()
        code, output, errors = self.live_cli([QUESTION, "/exit"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.post.call_count, 3)
        self.assertIn("completed/research_completed", output)
        self.assertIn("收盘价：10.4", output)
        self.assertNotIn(PRIVATE, output)

    def test_live_provider_exit_without_question_does_not_send_requests(self):
        code, _, errors = self.live_cli(["/exit"])
        self.assertEqual((code, errors), (0, ""))
        self.post.assert_not_called()

    def test_explicit_tencent_mode_uses_online_contract_but_all_io_is_mocked(self):
        self.install_http(source="tencent")
        directory = replace(self.directory, is_fixture=False, retrieved_at=datetime.now(timezone.utc))
        bars = self.source.read_daily_bars()
        source = SimpleNamespace(source_name="tencent", adjustment="unadjusted", read_daily_bars=Mock(return_value=bars))
        with patch("trading_companion.agent_cli.load_security_directory", return_value=directory) as load, \
             patch("trading_companion.research_tool._build_source", return_value=source) as build, \
             patch.dict(os.environ, {"DEEPSEEK_MODEL": "test-cli-model", "DEEPSEEK_API_KEY": PRIVATE}):
            code, output, errors = self.cli(["--provider", "deepseek", "--data", "tencent"], [QUESTION, "/exit"])
        self.assertEqual((code, errors, self.post.call_count, load.call_count), (0, "", 3, 2))
        self.assertEqual(build.call_args.args[0].source, "tencent")
        source.read_daily_bars.assert_called_once()
        self.assertIn("completed/research_completed", output)
        self.assertIn("供应商提供的历史日线", output)
        self.assertNotIn("人工样例 / 非真实行情", output)

    def test_http_timeout_is_sanitized_and_not_retried(self):
        self.post.side_effect = requests.exceptions.Timeout(PRIVATE)
        code, output, _ = self.live_cli([QUESTION, "/exit"])
        self.assertEqual(code, 0)
        self.post.assert_called_once()
        self.assertNotIn(PRIVATE, output)
        self.assertIn("intent_model_unavailable", output)

    def test_truncated_final_response_preserves_facts_without_extra_call(self):
        self.install_http(final_finish="length")
        code, output, _ = self.live_cli([QUESTION, "/exit"])
        self.assertEqual((code, self.post.call_count), (0, 3))
        self.assertIn("limited/explanation_unavailable", output)
        self.assertIn("收盘价：10.4", output)


class MeterTests(unittest.TestCase):
    def test_call_allowance_is_consumed_before_error_and_blocks_additional_delegation(self):
        delegate = Mock()
        delegate.complete.side_effect = ModelCallError("timeout", PRIVATE)
        meter = _ConversationModel(delegate, 1)
        with self.assertRaises(ModelCallError):
            meter.complete([], tools=())
        with self.assertRaises(ModelCallError) as caught:
            meter.complete([], tools=())
        self.assertEqual(caught.exception.code, "conversation_call_limit")
        self.assertEqual((meter.forwarded, meter.blocked), (1, 1))
        delegate.complete.assert_called_once()


class ConditionUpdateCliTests(GuardedTests):
    def sequence(self, requests):
        scripts = [ExplanationDemoModel([intent(), proposal()])]
        scripts.extend(ExplanationDemoModel([item]) for item in requests)
        cursor = 0
        def complete(messages, *, tools):
            nonlocal cursor
            script = scripts[cursor]
            response = script.complete(messages, tools=tools)
            if response.status == "complete":
                cursor += 1
            return response
        return Mock(complete=Mock(side_effect=complete))

    def test_add_and_remove_use_the_same_terminal_session_and_existing_caps(self):
        expanded = (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2), IndicatorSpec("atr", window=2))
        reduced = (expanded[0], expanded[2])
        model = self.sequence([proposal(indicators=expanded), proposal(indicators=reduced)])
        code, output, _ = self.chat([QUESTION, "添加ATR2", "删除RSI2", "/last", "/exit"], model=model)
        self.assertEqual(code, 0)
        self.assertEqual(output.count("[completed/research_completed]"), 3)
        self.assertIn("已计入3条问题，转发7次模型调用，已确认3次分析", output)
        self.assertIn("已确认股票：平安银行（000001.SZ）", output)
        self.assertIn("仅添加或删除本轮明确的一项指标", output)
        self.assertIn("历史查看", output)

    def test_cutoff_update_displays_new_dates_without_changing_indicator_parameters(self):
        arguments = request_arguments(MultiIndicatorRequest("000001.SZ", date(2026, 9, 21), date(2026, 9, 24),
            (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2)), source="csv"))
        response = ToolModelResponse("tool_calls", tool_calls=(ToolCall("updated_dates", TOOL_NAME, arguments),))
        code, output, _ = self.chat([QUESTION, "把截止日期改成2026-09-24", "/exit"], model=self.sequence([response]))
        self.assertEqual(code, 0)
        self.assertIn("已校验条件：2026-09-21 至 2026-09-24；指标：SMA(3)、RSI(2)", output)
        self.assertIn("未指定的日期边界沿用原值", output)
        self.assertIn("已计入2条问题，转发5次模型调用，已确认2次分析", output)

    def test_stock_ambiguity_is_visible_and_cancel_does_not_spend_a_model_call(self):
        code, output, _ = self.chat([QUESTION, "换成平安", "取消", "/last", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("condition_update_instrument_unresolved", output)
        self.assertIn("中国平安（601318.SH）", output)
        self.assertIn("已计入3条问题，转发3次模型调用，已确认1次分析", output)

    def test_invalid_extra_clause_does_not_consume_an_analysis_or_silently_apply_a_prefix(self):
        code, output, _ = self.chat([QUESTION, "添加ATR2，另外下单买入", "/last", "/exit"])
        self.assertEqual(code, 0)
        self.assertIn("invalid_condition_update", output)
        self.assertIn("已计入2条问题，转发3次模型调用，已确认1次分析", output)

    def test_reset_and_interrupt_clear_pending_update_conditions_and_results(self):
        captured = []
        from trading_companion.agent_cli import ResearchSession as RealSession
        def create_session():
            session = RealSession()
            captured.append(session)
            return session
        for ending in ("/reset", KeyboardInterrupt()):
            with patch("trading_companion.agent_cli.ResearchSession", side_effect=create_session):
                code, output, _ = self.chat([QUESTION, "换成平安", ending, "/exit"])
            self.assertIn(code, (0, 130))
            self.assertIsNone(captured[-1].pending)
            self.assertIsNone(captured[-1].confirmed)
            self.assertIsNone(captured[-1].to_dict()["saved_result"])

    def test_condition_update_demo_is_offline_and_explicit_about_intended_data_failure(self):
        from trading_companion.condition_update_demo import main as update_demo_main
        output = StringIO()
        with patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")), \
             redirect_stdout(output):
            self.assertEqual(update_demo_main(), 0)
        text = output.getvalue()
        self.assertIn("Fake模型调用10次，分析尝试5次", text)
        self.assertIn("[failed/research_data_failed]", text)
        self.assertIn("历史查看", text)
        self.assertIn("预期的跨股票样例拒绝已发生", text)

    def test_condition_update_demo_runs_in_an_independent_process_without_credentials(self):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("DEEPSEEK_")}
        environment["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m",
                                   "trading_companion.condition_update_demo"], cwd=ROOT,
            env=environment, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertIn("Fake模型调用10次", completed.stdout)


if __name__ == "__main__":
    unittest.main()
