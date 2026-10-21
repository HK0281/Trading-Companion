from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.model import ModelCallError, ModelRequestError
from trading_companion.research_tool import TOOL_NAME, ResearchToolExecutor, ToolExecution
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse
from trading_companion.tool_smoke import _fixture_arguments, main, run_smoke_test


SECRET = "synthetic-smoke-credential-not-a-real-key"
MODEL = "synthetic-smoke-model"


def proposal(arguments=None, *, call_id="smoke_call_1"):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(
        call_id, TOOL_NAME, _fixture_arguments() if arguments is None else arguments),))


def response_payload(*, tool=False):
    message = {"role": "assistant", "content": None if tool else "人工样例；SMA20历史不足。"}
    if tool:
        message["tool_calls"] = [{
            "id": "provider_smoke_1", "type": "function", "function": {
                "name": TOOL_NAME, "arguments": json.dumps(_fixture_arguments()),
            },
        }]
    return {"model": MODEL, "choices": [{
        "finish_reason": "tool_calls" if tool else "stop", "message": message,
    }], "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


class ToolSmokeTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        http = patch("requests.post", side_effect=AssertionError("Unexpected HTTP call"))
        self.post = http.start()
        self.addCleanup(http.stop)
        market = patch("trading_companion.research_tool._build_source",
                       side_effect=AssertionError("Market sources are forbidden in smoke tests"))
        self.market = market.start()
        self.addCleanup(market.stop)

    def run_script(self, responses):
        model = FakeToolModel(responses)
        with patch("trading_companion.tool_smoke._fake_model", return_value=model):
            result = run_smoke_test()
        return result, model

    def cli_report(self, argv=None):
        output = StringIO()
        with redirect_stdout(output):
            code = main(argv)
        return code, json.loads(output.getvalue())

    def test_default_cli_is_offline_and_never_reads_model_credentials(self):
        with patch("trading_companion.tool_smoke.DeepSeekConfig.from_env",
                   side_effect=AssertionError("Offline mode must not inspect credentials")):
            code, report = self.cli_report([])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["provider"], "fake")
        self.assertFalse(report["live_provider_selected"])
        self.assertTrue(all(item["is_mock"] for item in report["model_responses"]))
        self.assertTrue(report["tool_round_trip_completed"])
        self.assertEqual(report["answer_validation"], "not_performed")
        self.assertEqual(report["data_authenticity"], "synthetic")
        self.assertFalse(report["market_data_requested"])
        self.post.assert_not_called()
        self.market.assert_not_called()

    def test_default_round_trip_really_reads_one_fixture_and_keeps_missing_fields(self):
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True,
                          side_effect=original) as read:
            report = run_smoke_test()
        read.assert_called_once()
        self.assertTrue(read.call_args.args[0].is_synthetic)
        self.assertEqual((report["model_calls_attempted"], report["analyses_used"]), (2, 1))
        tool = report["tool_result"]
        self.assertEqual(tool["call_id"], "smoke_research_1")
        payload = tool["result"]
        self.assertEqual((payload["source"], payload["data_status"], payload["calculation_status"]),
                         ("csv", "ok", "limited"))
        self.assertEqual(payload["data_authenticity"], "synthetic")
        self.assertEqual(payload["bars_used"], 5)
        tables = payload["tables"]
        fields = [dict(zip(tables["latest_fields"]["columns"], row))
                  for row in tables["latest_fields"]["rows"]]
        self.assertTrue(any(field["missing"] == 15 and field["fact"] is None for field in fields))
        numeric = [dict(zip(tables["numeric_facts"]["columns"], row))
                   for row in tables["numeric_facts"]["rows"]]
        self.assertTrue(any(abs(fact["value"] - (10.10 + 10.00 + 10.40) / 3) < 1e-12
                            for fact in numeric if type(fact["value"]) in (int, float)))
        self.market.assert_not_called()

    def test_fixture_arguments_are_detached_between_runs(self):
        changed = _fixture_arguments()
        changed["indicators"][0]["window"] = 99
        self.assertEqual(_fixture_arguments()["indicators"][0]["window"], 3)
        self.assertEqual(run_smoke_test()["status"], "ok")

    def test_reordered_indicators_are_accepted_without_rewriting_the_proposal(self):
        arguments = _fixture_arguments()
        arguments["indicators"].reverse()
        original = deepcopy(arguments)
        report, model = self.run_script([proposal(arguments), ToolModelResponse("complete", "Synthetic.")])
        self.assertTrue(report["tool_round_trip_completed"])
        self.assertEqual(arguments, original)
        self.assertEqual(model.responses_consumed, 2)

    def test_missing_extra_or_changed_fixture_arguments_stop_before_any_execution(self):
        variants = []
        for key in _fixture_arguments():
            changed = _fixture_arguments()
            del changed[key]
            variants.append(changed)
        for key, value in (("source", "tencent"), ("source", "csv"),
                           ("path", SECRET), ("url", "https://untrusted.invalid"),
                           ("request_id", "model_selected"), ("instrument_id", "600000.SH"),
                           ("start_date", "2026-09-20"), ("as_of_date", "2026-09-24"),
                           ("indicators", None), ("indicators", [])):
            changed = _fixture_arguments()
            changed[key] = value
            variants.append(changed)
        for value in (3.0, True, 4, "3"):
            changed = _fixture_arguments()
            changed["indicators"][0]["window"] = value
            variants.append(changed)
        duplicate = _fixture_arguments()
        duplicate["indicators"][1] = duplicate["indicators"][0]
        variants.append(duplicate)
        for arguments in variants:
            with self.subTest(keys=tuple(arguments)), patch(
                    "trading_companion.tool_smoke.ResearchToolExecutor",
                    side_effect=AssertionError("Do not construct an executor for a mismatched task")):
                report, model = self.run_script([proposal(arguments)])
                self.assertEqual(report["code"], "smoke_arguments_mismatch")
                self.assertEqual(report["status"], "not_verified")
                self.assertEqual((report["model_calls_attempted"], report["analyses_used"]), (1, 0))
                self.assertIsNone(report["tool_result"])
                self.assertEqual(model.responses_consumed, 1)

    def test_multiple_proposals_stop_as_a_batch_without_executing_one(self):
        response = ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("one", TOOL_NAME, _fixture_arguments()),
            ToolCall("two", TOOL_NAME, _fixture_arguments()),
        ))
        with patch.object(CsvDailyBarSource, "read_daily_bars",
                          side_effect=AssertionError("No IO for multiple proposals")):
            report, model = self.run_script([response])
        self.assertEqual(report["code"], "multiple_calls_not_allowed")
        self.assertEqual(report["analyses_used"], 0)
        self.assertEqual(model.responses_consumed, 1)

    def test_non_tool_first_response_does_not_count_as_verified_or_trigger_retry(self):
        for status, code in (("complete", "tool_call_not_proposed"),
                             ("refused", "proposal_refused"), ("incomplete", "proposal_incomplete")):
            with self.subTest(status=status), patch.object(
                    CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No proposal, no IO")):
                report, model = self.run_script([ToolModelResponse(status, "Not a tool call."), proposal()])
            self.assertEqual(report["code"], code)
            self.assertEqual(report["stage"], "proposal")
            self.assertEqual(report["model_calls_attempted"], 1)
            self.assertEqual(report["analyses_used"], 0)
            self.assertFalse(report["tool_round_trip_completed"])
            self.assertEqual(model.responses_consumed, 1)
            self.assertIsNone(report["answer"])

    def test_second_proposal_never_resets_executor_or_makes_a_third_call(self):
        with patch("trading_companion.tool_smoke.ResearchToolExecutor",
                   side_effect=ResearchToolExecutor) as factory:
            report, model = self.run_script([
                proposal(), proposal(call_id="another_call"), ToolModelResponse("complete", "Unused."),
            ])
        factory.assert_called_once()
        self.assertEqual(report["code"], "unexpected_followup_call")
        self.assertEqual(report["stage"], "final")
        self.assertEqual((report["model_calls_attempted"], report["analyses_used"]), (2, 1))
        self.assertEqual(model.responses_consumed, 2)
        self.assertFalse(report["tool_round_trip_completed"])
        self.assertIsNone(report["answer"])

    def test_second_refusal_or_incomplete_response_stops_without_success_or_retry(self):
        for status, code in (("refused", "final_refused"), ("incomplete", "final_incomplete")):
            report, model = self.run_script([
                proposal(), ToolModelResponse(status, "Not complete."), ToolModelResponse("complete", "Unused."),
            ])
            self.assertEqual(report["code"], code)
            self.assertEqual(report["model_calls_attempted"], 2)
            self.assertEqual(model.responses_consumed, 2)
            self.assertIsNone(report["answer"])

    def test_reusing_the_first_call_id_is_a_protocol_error_not_a_new_execution(self):
        report, model = self.run_script([proposal(), proposal()])
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["code"], "invalid_response")
        self.assertEqual(report["analyses_used"], 1)
        self.assertFalse(report["tool_round_trip_completed"])

    def test_unusable_source_does_not_trigger_a_second_model_call(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(SECRET)) as read:
            report, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        read.assert_called_once()
        self.assertEqual(report["code"], "synthetic_result_not_usable")
        self.assertEqual(report["stage"], "execution")
        self.assertEqual(report["analyses_used"], 1)
        self.assertEqual(model.responses_consumed, 1)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_empty_fixture_is_not_a_successful_round_trip(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=[]):
            report, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(report["code"], "synthetic_result_not_usable")
        self.assertEqual(model.responses_consumed, 1)
        self.assertEqual(report["analyses_used"], 1)

    def test_rejected_execution_is_not_sent_to_the_model_as_a_success(self):
        rejected = ToolExecution("rejected", (ToolMessage(
            "tool", call_id="smoke_call_1", name=TOOL_NAME, result={"status": "rejected"}),))
        with patch.object(ResearchToolExecutor, "execute", return_value=rejected):
            report, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(report["code"], "tool_execution_not_usable")
        self.assertEqual(model.responses_consumed, 1)
        self.assertFalse(report["tool_round_trip_completed"])

    def test_internal_execution_failure_keeps_the_consumed_budget_and_hides_details(self):
        with patch("trading_companion.research_tool.analyze_indicators", side_effect=RuntimeError(SECRET)):
            report, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(report["code"], "tool_execution_error")
        self.assertEqual(report["analyses_used"], 1)
        self.assertEqual(model.responses_consumed, 1)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_first_or_second_model_failure_is_counted_without_automatic_retry(self):
        for before, attempts, analyses in (([], 1, 0), ([proposal()], 2, 1)):
            model = Mock()
            model.complete.side_effect = [*before, ModelCallError("timeout", SECRET)]
            with patch("trading_companion.tool_smoke._fake_model", return_value=model):
                report = run_smoke_test()
            self.assertEqual(report["code"], "timeout")
            self.assertEqual(report["status"], "error")
            self.assertEqual((report["model_calls_attempted"], report["analyses_used"]), (attempts, analyses))
            self.assertEqual(model.complete.call_count, attempts)
            self.assertNotIn(SECRET, json.dumps(report))

    def test_model_request_errors_are_reported_without_raw_details(self):
        model = Mock()
        model.complete.side_effect = ModelRequestError(SECRET)
        with patch("trading_companion.tool_smoke._fake_model", return_value=model):
            report = run_smoke_test()
        self.assertEqual(report["code"], "model_request_invalid")
        self.assertNotIn(SECRET, json.dumps(report))

    def test_semantically_wrong_final_text_is_explicitly_not_validated(self):
        report, _ = self.run_script([
            proposal(), ToolModelResponse("complete", "SMA20 is available and predicts guaranteed profits."),
        ])
        self.assertTrue(report["tool_round_trip_completed"])
        self.assertEqual(report["answer_validation"], "not_performed")
        self.assertIn("guaranteed", report["answer"])

    def test_explicit_live_selection_without_configuration_exits_two_without_http(self):
        code, report = self.cli_report(["--provider", "deepseek"])
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "invalid_request")
        self.assertEqual(report["code"], "model_configuration_error")
        self.assertEqual(report["stage"], "configuration")
        self.assertEqual(report["model_calls_attempted"], 0)
        self.post.assert_not_called()

    def test_cli_nonverification_exits_one_and_reports_machine_readable_reason(self):
        model = FakeToolModel([ToolModelResponse("complete", "No call.")])
        with patch("trading_companion.tool_smoke._fake_model", return_value=model):
            code, report = self.cli_report([])
        self.assertEqual(code, 1)
        self.assertEqual(report["code"], "tool_call_not_proposed")

    def test_cli_invalid_provider_is_rejected_without_network(self):
        with redirect_stdout(StringIO()), patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit) as caught:
                main(["--provider", "other"])
        self.assertEqual(caught.exception.code, 2)
        self.post.assert_not_called()

    def test_explicit_provider_cli_round_trip_uses_mocked_http_and_actual_fixture(self):
        responses = []
        for payload in (response_payload(tool=True), response_payload()):
            response = Mock()
            response.status_code = 200
            response.json.return_value = payload
            responses.append(response)
        self.post.side_effect = responses
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": MODEL, "DEEPSEEK_API_KEY": SECRET}, clear=True):
            code, report = self.cli_report(["--provider", "deepseek"])
        self.assertEqual(code, 0)
        self.assertTrue(report["live_provider_selected"])
        self.assertTrue(report["tool_round_trip_completed"])
        self.assertEqual(report["answer_validation"], "not_performed")
        self.assertTrue(all(not item["is_mock"] for item in report["model_responses"]))
        self.assertEqual(report["model_responses"][1]["model"], MODEL)
        self.assertEqual(report["model_responses"][0]["usage"]["total_tokens"], 30)
        self.assertEqual(self.post.call_count, 2)
        first = self.post.call_args_list[0].kwargs["json"]
        second = self.post.call_args_list[1].kwargs["json"]
        self.assertEqual(second["messages"][:2], first["messages"])
        assistant, tool = second["messages"][-2:]
        self.assertEqual(assistant["tool_calls"][0]["id"], "provider_smoke_1")
        self.assertEqual(tool["tool_call_id"], "provider_smoke_1")
        self.assertEqual(json.loads(tool["content"]), report["tool_result"]["result"])
        self.assertEqual(report["tool_result"]["result"]["data_authenticity"], "synthetic")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(SECRET, json.dumps(first))
        self.assertNotIn(SECRET, json.dumps(second))
        self.market.assert_not_called()
        for response in responses:
            response.close.assert_called_once_with()

    def test_live_path_network_errors_never_retry_or_report_a_verified_round_trip(self):
        self.post.side_effect = requests.exceptions.Timeout(SECRET)
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": MODEL, "DEEPSEEK_API_KEY": SECRET}, clear=True):
            code, report = self.cli_report(["--provider", "deepseek"])
        self.assertEqual(code, 1)
        self.assertEqual(report["code"], "timeout")
        self.assertEqual(report["model_calls_attempted"], 1)
        self.assertFalse(report["tool_round_trip_completed"])
        self.post.assert_called_once()
        self.assertNotIn(SECRET, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
