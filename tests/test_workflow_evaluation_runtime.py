"""Explicit real adapters over simulated HTTP only; every preview remains credential-free."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_research_workflows as command
from scripts.verify import offline_network
from trading_companion import workflow_evaluation as evaluation
from trading_companion import workflow_evaluation_runtime as runtime
from trading_companion.evaluation_pricing import DEFAULT_PRICES
from trading_companion.product_answer import PRODUCT_ANSWER_VERSION
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.workflow_evaluation_fixtures import FixedTransport, DeadlineFault, SourceClock, SECURITIES
from trading_companion.security_directory import SecurityDirectory


PRIVATE = "private-plan-detail-sentinel"


class WireResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload, self.closed = payload, False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "directory.json"
        self.directory.write_text(json.dumps({"source": "evaluation_test", "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "is_fixture": False, "securities": [s.to_dict() for s in SECURITIES]}, ensure_ascii=False), encoding="utf-8")
        self.arguments = dict(mode="live-model", provider="deepseek", model="test-model", prices=str(DEFAULT_PRICES), model_call_limit=6)

    def test_compatible_defaults_are_public_and_explicit_incompatibility_fails_before_clients(self):
        with patch("trading_companion.local_env.load_model_environment") as credentials, patch.object(runtime, "build_real_model") as model:
            for mode, cases, steps, removed in (("offline", 40, 59, 0), ("live-model", 39, 57, 1), ("live", 30, 44, 10)):
                args = {} if mode == "offline" else {**self.arguments, "mode": mode, "model_call_limit": 300 if mode == "live-model" else 250}
                if mode == "live":
                    args["directory"] = self.directory
                plan = runtime.build_plan(**args)
                self.assertEqual(plan["status"], "planned", plan)
                self.assertEqual((plan["selected_scenarios"], plan["planned_steps"], plan["not_applicable_to_mode"]), (cases, steps, removed))
                self.assertTrue(all(entry["reason"] for entry in plan["excluded_cases"]))
            bad = runtime.build_plan(**self.arguments, selected_cases=["R01"])
            self.assertEqual(bad["code"], "evaluation_cases_not_applicable_to_mode")
            self.assertEqual(bad["excluded_cases"][0]["case_id"], "R01")
            credentials.assert_not_called()
            model.assert_not_called()

    def test_each_explicit_real_parameter_is_required_and_invalid_prices_are_early(self):
        for key in ("provider", "model", "prices", "model_call_limit"):
            candidate = dict(self.arguments)
            del candidate[key]
            with self.subTest(key=key), patch.object(runtime, "build_real_model") as model:
                self.assertEqual(evaluation.evaluate_workflows(**candidate, selected_cases=["K01"])["status"], "invalid_request")
                model.assert_not_called()
        with patch.object(runtime, "build_real_model") as model:
            bad = evaluation.evaluate_workflows(**{**self.arguments, "prices": "not-an-existing-price-file"}, selected_cases=["K01"])
            self.assertEqual(bad["status"], "invalid_request")
            model.assert_not_called()

    def test_cli_plan_only_produces_one_json_without_credentials_or_sources(self):
        output = StringIO()
        with patch.object(runtime, "build_real_model") as model, patch.object(runtime, "live_sources") as sources, redirect_stdout(output):
            code = command.main(["--mode", "live-model", "--provider", "deepseek", "--model", "test-model",
                                 "--prices", str(DEFAULT_PRICES), "--model-call-limit", "300", "--plan-only"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["planned_steps"], 57)
        model.assert_not_called()
        sources.assert_not_called()

    def assert_invalid_plan(self, arguments, code, detail):
        with offline_network(), patch("trading_companion.local_env.load_model_environment") as credentials, \
                patch.object(runtime, "build_real_model") as model, patch.object(runtime, "live_sources") as sources:
            for evaluate in (runtime.build_plan, evaluation.evaluate_workflows):
                result = evaluate(**arguments)
                self.assertEqual((result["status"], result["code"], result["detail"]), ("invalid_request", code, detail))
                self.assertNotIn(PRIVATE, result["detail"])
                self.assertNotIn(self.temp.name, result["detail"])
            credentials.assert_not_called()
            model.assert_not_called()
            sources.assert_not_called()

    def test_call_limit_error_explains_mode_selection_and_repeat_derived_bounds(self):
        for changes, steps, maximum in ((dict(mode="live", model_call_limit=300), 44, 264),
                                        (dict(selected_cases=["K01"], repeat=2, model_call_limit=13), 2, 12)):
            with self.subTest(changes=changes):
                self.assert_invalid_plan({**self.arguments, **changes}, "evaluation_model_call_limit_out_of_range",
                    f"model-call-limit 必须是 1..{maximum} 之间的整数（6 × {steps} 个计划步骤）。")

    def test_invalid_price_file_detail_does_not_disclose_filename_or_contents(self):
        price_file = Path(self.temp.name) / (PRIVATE + ".json")
        for content in (None, PRIVATE, json.dumps({"version": "evaluation-prices-v1", "entries": [PRIVATE]})):
            if content is not None:
                price_file.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                self.assert_invalid_plan({**self.arguments, "prices": price_file}, "evaluation_price_file_invalid",
                    "prices 文件无法读取或价格配置无效；请检查 evaluation-prices-v1 结构、单价及核对元数据。")

    def test_stale_directory_detail_contains_only_safe_date_and_refresh_instruction(self):
        payload = json.loads(self.directory.read_text(encoding="utf-8"))
        payload["source"] = PRIVATE
        private_path = Path(self.temp.name) / (PRIVATE + ".json")
        with patch.object(runtime, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
            for stamp in ("2026-10-20T00:00:00+08:00", "2026-10-30T00:00:00+08:00", "0001-01-01T00:00:00+14:00"):
                payload["retrieved_at"] = stamp
                private_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(stamp=stamp):
                    self.assert_invalid_plan({**self.arguments, "mode": "live", "directory": private_path},
                        "evaluation_live_directory_stale",
                        f"directory retrieved_at={stamp}；快照须在当前北京时间日期 2026-10-28 前 0..7 天内。"
                        "请运行 python -m trading_companion.security_directory_refresh --live 刷新。")

    def test_other_parameter_errors_name_the_parameter_without_echoing_its_value(self):
        examples = (
            ({"mode": PRIVATE}, "evaluation_mode_invalid", "mode 必须是 offline、live-model 或 live。"),
            ({"repeat": PRIVATE}, "evaluation_repeat_out_of_range", "repeat 必须是 1..3 之间的整数。"),
            ({"collection_workers": 4}, "evaluation_collection_workers_out_of_range", "collection-workers 必须是 1..3 之间的整数。"),
            ({"selected_cases": [PRIVATE]}, "evaluation_case_selection_invalid", "case 必须是语料中有效且不重复的场景 ID，选择不能为空。"),
            ({"assumed_input_tokens": PRIVATE}, "evaluation_assumed_tokens_invalid", "assumed-input-tokens 必须省略或为 0..1000000 之间的整数。"),
            ({"assumed_output_tokens": -1}, "evaluation_assumed_tokens_invalid", "assumed-output-tokens 必须省略或为 0..1000000 之间的整数。"),
            ({"data_source": PRIVATE}, "evaluation_data_source_invalid", "data-source 必须是 tencent 或 akshare_eastmoney。"),
            ({"mode": "offline"}, "evaluation_offline_options_invalid", "offline 不接受 provider、model 或 directory，data-source 必须保留 tencent。"),
            ({"provider": PRIVATE}, "evaluation_provider_invalid", "真实模式必须显式指定 provider，取值为 deepseek 或 openai。"),
            ({"model": PRIVATE + "/"}, "evaluation_model_invalid", "真实模式必须显式指定 model：1..128 个字母、数字、点、下划线或连字符，以字母或数字开头。"),
            ({"prices": None}, "evaluation_price_file_invalid", "真实模式必须用 prices 显式指定价格文件。"),
            ({"model_call_limit": None}, "evaluation_model_call_limit_required", "真实模式必须显式指定 model-call-limit，范围为 1..342（6 × 57 个计划步骤）。"),
            ({"directory": PRIVATE}, "evaluation_live_model_source_options_invalid", "live-model 使用固定资料，不接受 directory，data-source 必须保留 tencent。"),
            ({"trace_dir": "\0" + PRIVATE}, "evaluation_output_directory_invalid", "trace-dir/report-dir 必须是有效目录；目标及已有父路径不能是文件。"),
        )
        for changes, code, detail in examples:
            with self.subTest(code=code, changes=changes):
                self.assert_invalid_plan({**self.arguments, **changes}, code, detail)

    def test_corpus_error_and_last_resort_error_do_not_disclose_underlying_exception(self):
        with patch.object(evaluation, "load_corpus", side_effect=ValueError(PRIVATE)):
            self.assert_invalid_plan(self.arguments, "evaluation_corpus_invalid", "评估语料无法读取或结构无效，请检查版本及场景标注。")
        with patch.object(runtime, "make_plan", side_effect=ValueError(PRIVATE)):
            self.assert_invalid_plan(self.arguments, "evaluation_input_invalid", "无法校验评估输入，请检查参数、语料及本地配置。")

    def test_cli_preview_and_execution_return_the_same_safe_error_json(self):
        for suffix in ([], ["--plan-only"]):
            output = StringIO()
            with offline_network(), patch.object(runtime, "build_real_model") as model, redirect_stdout(output):
                code = command.main(["--mode", "live", "--provider", "deepseek", "--model", "test-model", "--prices",
                                     str(DEFAULT_PRICES), "--model-call-limit", "300", *suffix])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["code"], "evaluation_model_call_limit_out_of_range")
            self.assertEqual(result["detail"], "model-call-limit 必须是 1..264 之间的整数（6 × 44 个计划步骤）。")
            model.assert_not_called()

    def run_wire(self, *, mode="live-model", fail=False, budget=6, selected_provider="deepseek"):
        stderr, responses, calls = StringIO(), [], []

        def credentials(*, provider):
            self.assertIn("evaluation_plan", stderr.getvalue())
            self.assertEqual(provider, selected_provider)
            return {provider.upper() + "_API_KEY": "test-only-credential", provider.upper() + "_MODEL": "environment-model-must-not-win"}

        def wire(url, **kwargs):
            body = kwargs["json"]
            self.assertEqual(url, {"deepseek": "https://api.deepseek.com/chat/completions",
                                   "openai": "https://api.openai.com/v1/chat/completions"}[selected_provider])
            self.assertEqual(body["model"], "test-model")
            self.assertFalse(kwargs["allow_redirects"])
            calls.append(body)
            if fail:
                response = WireResponse({})
                response.status_code = 503
            elif len(calls) == 1:
                self.assertEqual(body["tools"][0]["function"]["name"], PRODUCT_INTENT_TOOL_NAME)
                arguments = {"intent": "research", "instrument_mentions": ["平安银行"], "research_kinds": ["profile", "financial"],
                             "date_expression": None, "outlook_expression": None, "wants_risk": False, "clarification_reason": None}
                response = WireResponse({"model": "test-model", "choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{"id": "wire_intent", "type": "function", "function": {
                        "name": PRODUCT_INTENT_TOOL_NAME, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            else:
                context = next(json.loads(item["content"].split("\n", 1)[1]) for item in body["messages"]
                               if (item.get("content") or "").startswith("UNTRUSTED_RESEARCH_CONTEXT\n"))
                evidence = context["evidence"][0]["id"]
                payload = {"version": PRODUCT_ANSWER_VERSION, "paragraphs": [
                    {"text": "已有资料提供经营观察的依据。", "evidence_ids": [evidence]},
                    {"text": "后续仍需核对风险与相反证据。", "evidence_ids": [evidence]}]}
                response = WireResponse({"model": "test-model", "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            responses.append(response)
            return response
        args = {**self.arguments, "provider": selected_provider, "mode": mode, "selected_cases": ["S02"], "model_call_limit": budget}
        if mode == "live":
            args["directory"] = self.directory
        directory = SecurityDirectory(SECURITIES, "test_transport", datetime.now(timezone.utc), False)
        transport = FixedTransport(directory, "normal", DeadlineFault("normal", SourceClock()))
        with offline_network(), patch("requests.post", wire), patch("requests.Session", transport.factory), \
                patch("trading_companion.local_env.load_model_environment", credentials), redirect_stderr(stderr):
            report = evaluation.evaluate_workflows(**args)
        self.assertTrue(all(response.closed for response in responses))
        self.assertNotIn("test-only-credential", json.dumps(report, ensure_ascii=False))
        return report, calls

    def test_live_model_adapter_and_fixed_collectors_run_with_simulated_model_http(self):
        report, calls = self.run_wire()
        self.assertEqual(report["status"], "passed", report.get("code"))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(report["steps"][0]["offline_expected_matched"])
        self.assertEqual(report["trace_summary"]["models"]["provider_requests_started"], 2)
        self.assertEqual(report["accounting"]["token_usage"]["reported_total_tokens"], 30)
        self.assertEqual(report["data_authenticity"], "fixed_synthetic")

    def test_live_adapter_and_real_collectors_use_simulated_http_without_fixed_relabeling(self):
        report, calls = self.run_wire(mode="live")
        self.assertEqual(report["status"], "passed", report.get("code"))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(report["fixtures"])
        self.assertEqual(report["data_authenticity"], "live_sources")
        self.assertNotIn("fixed_synthetic", json.dumps(report["steps"][0]["observed"]))

    def test_other_existing_provider_is_explicit_and_uses_its_own_simulated_endpoint(self):
        report, calls = self.run_wire(selected_provider="openai")
        self.assertEqual(report["status"], "passed", report.get("code"))
        self.assertEqual(report["provider"], "openai")
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["trace_summary"]["models"]["provider_requests_started"], 2)

    def test_provider_failure_is_charged_once_not_retried_or_replaced(self):
        report, calls = self.run_wire(fail=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["accounting"]["forwarded"], 1)
        self.assertIsNone(report["cost"]["estimated_total"])

    def test_one_whole_run_allowance_blocks_second_model_forward(self):
        report, calls = self.run_wire(budget=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["accounting"]["forwarded"], 1)
        self.assertGreater(report["accounting"]["blocked"], 0)

    def test_fixed_data_boundary_blocks_and_records_caught_source_dns(self):
        import socket
        with runtime.model_network_boundary("deepseek") as attempts:
            with self.assertRaises(OSError):
                socket.getaddrinfo("example.invalid", 443)
        self.assertEqual(len(attempts), 1)

    def test_live_pool_is_shared_and_closed_even_when_body_raises(self):
        plan = runtime.build_plan(**{**self.arguments, "mode": "live", "directory": self.directory}, selected_cases=["K01"])
        with self.assertRaisesRegex(RuntimeError, "test-body"):
            with runtime.live_sources(plan) as sources:
                pool = sources.company._executor
                self.assertIs(pool, sources.quotes._executor)
                raise RuntimeError("test-body")
        self.assertTrue(pool._closed)
        self.directory.write_text("{}", encoding="utf-8")
        with self.assertRaises(runtime.PlanError):
            with runtime.live_sources(plan):
                self.fail("changed directory must not execute")
