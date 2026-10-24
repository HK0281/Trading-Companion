from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.agent import run_research_turn
from trading_companion.agent_evaluation import (
    CASES, ROOT, SUITE_VERSION, _MeteredModel, _fake_model, _usage_summary, main, run_evaluation,
)
from trading_companion.agent_input import request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse


SECRET = "synthetic-evaluation-private-marker-not-a-real-key"
MODEL = "synthetic-evaluation-model"


def proposal(case=CASES[0], *, changes=None):
    arguments = request_arguments(case.request())
    arguments.update(changes or {})
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall("test_proposal", TOOL_NAME, arguments),))


def final_payload(view):
    states = {row[0]: row[1] for row in view["tables"]["states"]["rows"]}
    relation = next(row for row in view["tables"]["relations"]["rows"] if states[row[3]] == "available")
    return {"version": "indicator-explanation-v1", "request_id": view["request_id"],
            "instrument_id": view["instrument_id"], "trade_date": view["dates"][0],
            "claims": [{"evidence_id": relation[0], "relation": relation[2], "value": relation[4]}]}


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch("trading_companion.local_env._read_local_values", return_value={}),
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No directory or market HTTP")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No online source")),
        ):
            context.start()
            self.addCleanup(context.stop)
        context = patch("requests.post", side_effect=AssertionError("No real provider HTTP"))
        self.post = context.start()
        self.addCleanup(context.stop)

    def cli(self, argv=()):
        output = StringIO()
        with redirect_stdout(output):
            code = main(list(argv))
        return code, json.loads(output.getvalue())

    def scripted(self, responses, *, case="complete"):
        model = FakeToolModel(responses)
        with patch("trading_companion.agent_evaluation._build_model", return_value=model):
            report = run_evaluation(case=case)
        return report, model

    def live(self, *, case="complete"):
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": MODEL, "DEEPSEEK_API_KEY": SECRET}, clear=True):
            return self.cli(["--provider", "deepseek", "--case", case])

    def install_http(self, *, case="complete", final_transform=None, final_finish="stop", usage=True):
        selected = next(item for item in CASES if item.name == case)
        responses = []
        def reply(url, **kwargs):
            body = kwargs["json"]
            last = body["messages"][-1]
            if last["role"] == "tool":
                payload = final_payload(json.loads(last["content"]))
                text = json.dumps(payload if final_transform is None else final_transform(payload))
                message = {"role": "assistant", "content": text}
                finish = final_finish
            else:
                name = body["tools"][0]["function"]["name"]
                if name == INTENT_TOOL_NAME:
                    arguments = {"intent": "research",
                                 "instrument_mentions": ["平安" if case == "clarification" else "平安银行"],
                                 "date_expression": "2026-09-21 至 2026-09-25",
                                 "indicator_mentions": ["SMA3"] if case == "clarification" else ["SMA3", "RSI2"],
                                 "clarification_reason": None}
                else:
                    arguments = request_arguments(selected.request())
                message = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": "http_evaluation_call", "type": "function",
                               "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                finish = "tool_calls"
            payload = {"model": MODEL, "choices": [{"finish_reason": finish, "message": message}]}
            if usage:
                payload["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
            response = Mock(status_code=200)
            response.json.return_value = payload
            responses.append(response)
            return response
        self.post.side_effect = reply
        return responses


class OfflineEvaluationTests(GuardedTests):
    def test_default_is_one_offline_case_without_any_credential_reads(self):
        with patch("trading_companion.agent_evaluation.DeepSeekConfig.from_env",
                   side_effect=AssertionError("No credential reads")):
            code, report = self.cli()
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertEqual(report["version"], SUITE_VERSION)
        self.assertEqual(report["selected_cases"], ["complete"])
        self.assertFalse(report["live_provider_selected"])
        self.assertFalse(report["market_data_requested"])
        self.assertFalse(report["directory_network_requested"])
        self.assertEqual(report["summary"]["model_calls_attempted"], 2)
        self.assertEqual(report["summary"]["analyses_used"], 1)
        self.post.assert_not_called()

    def test_all_cases_pass_with_explicit_expected_states_and_bounded_counts(self):
        report = run_evaluation(case="all")
        self.assertEqual(report["status"], "passed")
        self.assertEqual((report["summary"]["selected"], report["summary"]["executed"],
                          report["summary"]["passed"], report["summary"]["not_run"]), (4, 4, 4, 0))
        self.assertEqual((report["model_call_limit"], report["analysis_limit"]), (8, 3))
        self.assertEqual(report["summary"]["provider_calls_forwarded"], 8)
        self.assertEqual(report["summary"]["analyses_used"], 3)
        self.assertTrue(all(not item["failed_checks"] for item in report["cases"]))

    def test_partial_research_passes_without_pretending_data_is_complete(self):
        result = run_evaluation(case="partial")["cases"][0]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed"]["agent_status"], "limited")
        self.assertEqual(result["observed"]["explanation_status"], "accepted")
        self.assertTrue(result["checks"]["missing_sma20_preserved"])

    def test_clarification_never_reads_prices(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No price reads")):
            report = run_evaluation(case="clarification")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["model_calls_attempted"], 1)
        self.assertEqual(report["summary"]["analyses_used"], 0)
        self.assertTrue(report["cases"][0]["checks"]["identity_candidates"])
        self.assertIsNone(report["cases"][0]["observed"]["data_authenticity"])

    def test_natural_case_reuses_existing_three_call_runner(self):
        report = run_evaluation(case="natural")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["model_calls_attempted"], 3)
        self.assertTrue(report["cases"][0]["checks"]["confirmed_scope"])

    def test_all_analysis_cases_read_only_the_synthetic_fixture_once_each(self):
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
            report = run_evaluation(case="all")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(read.call_count, 3)
        self.assertTrue(all(call.args[0].is_synthetic for call in read.call_args_list))

    def test_evaluation_does_not_write_report_files(self):
        with patch.object(Path, "write_text", side_effect=AssertionError("No file writes")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("No file writes")):
            self.assertEqual(run_evaluation()["status"], "passed")

    def test_invalid_provider_and_case_fail_before_fixtures_or_credentials(self):
        with patch("trading_companion.agent_evaluation._fixtures", side_effect=AssertionError("No fixture reads")):
            for kwargs in ({"provider": "other"}, {"provider": None}, {"case": "unknown"}, {"case": []}):
                with self.assertRaises(ModelConfigurationError):
                    run_evaluation(**kwargs)
        self.post.assert_not_called()

    def test_invalid_cli_flags_do_not_enable_network_or_arbitrary_paths(self):
        for argv in (["--provider", "unknown"], ["--case", "unknown"], ["--source", "tencent"],
                     ["--output", "report.json"], ["--repeat", "10"]):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertEqual(caught.exception.code, 2)
        self.post.assert_not_called()

    def test_missing_fixture_stops_before_live_configuration(self):
        with patch.object(Path, "read_bytes", side_effect=OSError(SECRET)), \
             patch("trading_companion.agent_evaluation.DeepSeekConfig.from_env",
                   side_effect=AssertionError("No credentials before fixture verification")):
            code, report = self.cli(["--provider", "deepseek"])
        self.assertEqual((code, report["code"]), (1, "evaluation_fixture_unusable"))
        self.assertEqual(report["summary"]["executed"], 0)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_modified_fixture_cannot_be_silently_labeled_as_known_synthetic_data(self):
        with patch.object(Path, "read_bytes", return_value=SECRET.encode()), \
             patch("trading_companion.agent_evaluation.DeepSeekConfig.from_env",
                   side_effect=AssertionError("No credentials for unpinned fixtures")):
            report = run_evaluation(provider="deepseek", case="all")
        self.assertEqual(report["code"], "evaluation_fixture_unusable")
        self.assertEqual(report["summary"]["not_run"], 4)
        self.post.assert_not_called()

    def test_fixture_pins_tolerate_git_line_endings_only(self):
        original = Path.read_bytes
        def windows_newlines(path):
            return original(path).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with patch.object(Path, "read_bytes", autospec=True, side_effect=windows_newlines):
            self.assertEqual(run_evaluation()["status"], "passed")

    def test_repeat_invocations_have_independent_budgets(self):
        first, second = run_evaluation(), run_evaluation()
        self.assertEqual(first["summary"]["provider_calls_forwarded"], 2)
        self.assertEqual(second["summary"]["provider_calls_forwarded"], 2)

    def test_report_is_strict_json_without_raw_model_or_tool_payloads(self):
        report = run_evaluation(case="all")
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
        for field in ('"model_draft":', '"tool_calls":', '"research_result":', '"messages":', str(ROOT)):
            self.assertNotIn(field, encoded)
        self.assertIn("not_language_or_investment_quality", report["evaluation_scope"])

    def test_elapsed_time_is_measured_not_a_hard_deadline(self):
        with patch("trading_companion.agent_evaluation.perf_counter", side_effect=[10.0, 10.125]):
            report = run_evaluation()
        self.assertEqual(report["cases"][0]["elapsed_ms"], 125)

    def test_clean_process_default_uses_no_credentials(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "trading_companion.agent_evaluation"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True,
            text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "passed")


class EvaluationFailureTests(GuardedTests):
    def test_direct_model_text_is_a_failed_case_not_a_successful_completion(self):
        report, model = self.scripted([ToolModelResponse("complete", SECRET)])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cases"][0]["observed"]["agent_code"], "tool_call_not_proposed")
        self.assertEqual(model.responses_consumed, 1)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_plain_final_text_fails_without_being_printed(self):
        report, model = self.scripted([proposal(), ToolModelResponse("complete", SECRET)])
        self.assertEqual(report["status"], "failed")
        result = report["cases"][0]
        self.assertEqual(result["observed"]["report_status"], "complete")
        self.assertEqual(result["observed"]["explanation_status"], "rejected")
        self.assertEqual(model.responses_consumed, 2)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_same_partial_status_is_not_enough_when_explanation_is_wrong(self):
        report, _ = self.scripted([proposal(CASES[1]), ToolModelResponse("complete", SECRET)], case="partial")
        result = report["cases"][0]
        self.assertTrue(result["checks"]["agent_status"])
        self.assertTrue(result["checks"]["agent_code"])
        self.assertFalse(result["checks"]["explanation_status"])
        self.assertEqual(result["status"], "failed")

    def test_wrong_stock_proposal_stops_before_any_analysis(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No changed-stock query")):
            report, model = self.scripted([proposal(changes={"instrument_id": "600000.SH"})])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["analyses_used"], 0)
        self.assertEqual(model.responses_consumed, 1)

    def test_first_failed_case_skips_remaining_cases_without_hidden_retries(self):
        model = FakeToolModel([ToolModelResponse("complete", SECRET)])
        with patch("trading_companion.agent_evaluation._build_model", return_value=model) as factory:
            report = run_evaluation(case="all")
        self.assertEqual(report["status"], "failed")
        self.assertEqual((report["summary"]["executed"], report["summary"]["failed"],
                          report["summary"]["not_run"]), (1, 1, 3))
        self.assertTrue(all(item["status"] == "skipped" for item in report["cases"][1:]))
        self.assertEqual(model.responses_consumed, 1)
        factory.assert_called_once()

    def test_model_timeout_is_counted_without_guessing_usage_or_leaking_details(self):
        model = Mock()
        model.complete.side_effect = [proposal(), ModelCallError("timeout", SECRET)]
        with patch("trading_companion.agent_evaluation._build_model", return_value=model):
            report = run_evaluation()
        result = report["cases"][0]
        self.assertEqual(result["observed"]["failure_code"], "timeout")
        self.assertEqual(result["provider_calls_forwarded"], 2)
        self.assertEqual(len(result["responses"]), 1)
        self.assertEqual(result["observed"]["analyses_used"], 1)
        self.assertIsNone(result["token_usage"]["reported_total_tokens"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_unrecognized_failure_code_is_not_forwarded_as_untrusted_text(self):
        model = Mock()
        model.complete.side_effect = ModelCallError(SECRET, SECRET)
        with patch("trading_companion.agent_evaluation._build_model", return_value=model):
            report = run_evaluation()
        self.assertEqual(report["cases"][0]["observed"]["failure_code"], "unclassified_failure")
        self.assertNotIn(SECRET, json.dumps(report))

    def test_additional_tool_call_is_not_a_second_analysis(self):
        extra = proposal(changes={})
        extra = replace(extra, tool_calls=(ToolCall("extra_id", TOOL_NAME, extra.tool_calls[0].arguments),))
        report, model = self.scripted([proposal(), extra, ToolModelResponse("complete", "unused")])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["analyses_used"], 1)
        self.assertEqual(model.responses_consumed, 2)

    def test_clarification_cannot_be_passed_by_guessing_a_code_from_model_memory(self):
        bad = ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent_test", INTENT_TOOL_NAME, {
            "intent": "research", "instrument_mentions": ["000001.SZ"],
            "date_expression": "2026-09-21 至 2026-09-25", "indicator_mentions": ["SMA3"],
            "clarification_reason": None,
        }),))
        report, _ = self.scripted([bad], case="clarification")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["analyses_used"], 0)
        self.assertFalse(report["cases"][0]["checks"]["identity_candidates"])

    def test_reported_agent_counters_cannot_replace_independent_model_counting(self):
        original = run_research_turn
        def wrong_counter(*args, **kwargs):
            return replace(original(*args, **kwargs), model_calls_attempted=1)
        with patch("trading_companion.agent_evaluation.run_research_turn", side_effect=wrong_counter):
            report = run_evaluation()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["cases"][0]["checks"]["agent_model_count_matches_meter"])

    def test_completed_flag_cannot_hide_missing_history_in_partial_case(self):
        original = run_research_turn
        def wrong_status(*args, **kwargs):
            return replace(original(*args, **kwargs), status="completed", code="research_completed")
        with patch("trading_companion.agent_evaluation.run_research_turn", side_effect=wrong_status):
            report = run_evaluation(case="partial")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["cases"][0]["checks"]["missing_sma20_preserved"])
        self.assertFalse(report["cases"][0]["checks"]["agent_status"])

    def test_fake_provider_cannot_masquerade_as_live_provider_in_report(self):
        delegate = _fake_model(CASES[0])
        model = Mock()
        model.complete.side_effect = lambda messages, *, tools: replace(
            delegate.complete(messages, tools=tools), provider="deepseek", is_mock=False)
        with patch("trading_companion.agent_evaluation._build_model", return_value=model):
            report = run_evaluation()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["cases"][0]["checks"]["provider_mode"])


class LivePathMockTests(GuardedTests):
    def test_explicit_live_selection_without_config_fails_locally(self):
        code, report = self.live_without_environment()
        self.assertEqual((code, report["status"]), (2, "invalid_request"))
        self.assertEqual(report["code"], "model_configuration_error")
        self.assertEqual(report["summary"]["provider_calls_forwarded"], 0)
        self.post.assert_not_called()

    def live_without_environment(self):
        return self.cli(["--provider", "deepseek"])

    def test_live_path_reuses_adapter_and_runner_with_mocked_http(self):
        responses = self.install_http()
        code, report = self.live()
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertTrue(report["live_provider_selected"])
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(report["summary"]["token_usage"]["reported_total_tokens"], 60)
        self.assertTrue(report["summary"]["token_usage"]["complete_for_forwarded_requests"])
        self.assertTrue(all(not item["is_mock"] for item in report["cases"][0]["responses"]))
        self.assertNotIn(SECRET, json.dumps(report))
        for response in responses:
            response.close.assert_called_once_with()
        second = self.post.call_args_list[1].kwargs["json"]
        self.assertEqual(json.loads(second["messages"][-1]["content"])["data_authenticity"], "synthetic")
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(sum(item["role"] == "system" for item in second["messages"]), 1)

    def test_live_partial_result_can_pass_the_expected_limited_case(self):
        self.install_http(case="partial")
        code, report = self.live(case="partial")
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertEqual(report["cases"][0]["observed"]["agent_status"], "limited")

    def test_live_natural_case_has_three_calls_and_no_real_directory_request(self):
        self.install_http(case="natural")
        code, report = self.live(case="natural")
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(report["summary"]["analyses_used"], 1)

    def test_live_ambiguity_case_stops_after_one_interpretation(self):
        self.install_http(case="clarification")
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No analysis")):
            code, report = self.live(case="clarification")
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(report["summary"]["analyses_used"], 0)

    def test_live_reversed_claim_is_rejected_even_with_two_successful_http_responses(self):
        def reverse(payload):
            payload["claims"][0]["value"] = "below"
            return payload
        self.install_http(final_transform=reverse)
        code, report = self.live()
        self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertEqual(report["cases"][0]["observed"]["explanation_code"], "explanation_relation_mismatch")
        self.assertEqual(self.post.call_count, 2)

    def test_live_refusal_and_truncation_fail_without_retry(self):
        for finish in ("content_filter", "length"):
            self.post.reset_mock()
            self.install_http(final_finish=finish)
            code, report = self.live()
            self.assertEqual((code, report["status"]), (1, "failed"))
            self.assertEqual(self.post.call_count, 2)
            self.assertEqual(report["cases"][0]["observed"]["explanation_status"], "not_performed")

    def test_live_timeout_stops_all_cases_without_printing_provider_exception(self):
        self.post.side_effect = requests.exceptions.Timeout(SECRET)
        code, report = self.live(case="all")
        self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertEqual(report["summary"]["not_run"], 3)
        self.post.assert_called_once()
        self.assertNotIn(SECRET, json.dumps(report))

    def test_live_missing_usage_remains_null_not_zero(self):
        self.install_http(usage=False)
        code, report = self.live()
        self.assertEqual(code, 0)
        usage = report["summary"]["token_usage"]
        self.assertIsNone(usage["reported_total_tokens"])
        self.assertFalse(usage["complete_for_forwarded_requests"])
        self.assertEqual(usage["requests_without_complete_usage"], 2)


class MeterAndUsageTests(unittest.TestCase):
    def test_meter_consumes_allowance_before_delegate_error_and_never_retries(self):
        delegate = Mock()
        delegate.complete.side_effect = ModelCallError("timeout", SECRET)
        meter = _MeteredModel(delegate, 1)
        with self.assertRaises(ModelCallError):
            meter.complete([ToolMessage("user", "offline")], tools=())
        with self.assertRaises(ModelCallError) as caught:
            meter.complete([ToolMessage("user", "offline")], tools=())
        self.assertEqual(caught.exception.code, "evaluation_call_limit")
        self.assertEqual((meter.attempts, meter.forwarded, meter.blocked), (2, 1, 1))
        delegate.complete.assert_called_once()

    def test_usage_aggregates_only_known_values_and_exposes_missing_requests(self):
        first = ToolModelResponse("complete", "fixed", usage=TokenUsage(10, 5, 15))
        second = ToolModelResponse("complete", "fixed", usage=TokenUsage(20, None, None))
        report = _usage_summary([first, second], 3)
        self.assertEqual(report["reported_prompt_tokens"], 30)
        self.assertEqual(report["reported_completion_tokens"], 5)
        self.assertEqual(report["reported_total_tokens"], 15)
        self.assertEqual(report["requests_with_complete_usage"], 1)
        self.assertEqual(report["requests_without_complete_usage"], 2)
        self.assertFalse(report["complete_for_forwarded_requests"])

    def test_zero_reported_usage_is_distinct_from_missing_usage(self):
        response = ToolModelResponse("complete", "fixed", usage=TokenUsage(0, 0, 0))
        known, unknown = _usage_summary([response], 1), _usage_summary([], 0)
        self.assertEqual(known["reported_total_tokens"], 0)
        self.assertTrue(known["complete_for_forwarded_requests"])
        self.assertIsNone(unknown["reported_total_tokens"])
        self.assertFalse(unknown["complete_for_forwarded_requests"])


if __name__ == "__main__":
    unittest.main()
