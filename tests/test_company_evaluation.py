"""Synthetic evaluation is offline by default; budget checks precede any credential loading."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
import unittest
from unittest.mock import patch

from scripts import evaluate_company_research as evaluation
from scripts.verify import offline_network
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.product_answer import PRODUCT_ANSWER_INSTRUCTION
from trading_companion.tool_model import ToolModelResponse


PRIVATE = "private-evaluation-marker-never-export"


class CompanyEvaluationTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market/news/directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No real provider HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No market sources")),
            patch("trading_companion.product_research._build_source", side_effect=AssertionError("No market sources")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.environment = patch("trading_companion.local_env.load_model_environment", side_effect=AssertionError("No model environment"))
        self.env_loader = self.environment.start()
        self.addCleanup(self.environment.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)

    def test_default_runs_five_real_product_workflows_with_synthetic_data_only(self):
        with (patch.object(evaluation, "prepare_product_plan", wraps=evaluation.prepare_product_plan) as plan,
              patch.object(evaluation, "collect_product_research", wraps=evaluation.collect_product_research) as collect,
              patch.object(evaluation, "synthesize_product_answer", wraps=evaluation.synthesize_product_answer) as answer):
            result = evaluation.evaluate_company_research()
        self.assertEqual((result["status"], result["provider"]), ("passed", "fake"))
        self.assertEqual((plan.call_count, collect.call_count, answer.call_count), (4, 5, 4))
        self.assertEqual(result["summary"]["model_calls"], 9)
        self.assertEqual(result["summary"]["provider_calls_forwarded"], 9)
        self.assertEqual(result["summary"]["analyses"], 3)
        self.assertEqual(len(result["cases"]), 5)
        self.assertEqual(sum(item["synthetic_daily_reads"] for item in result["cases"]), 3)
        self.assertTrue(all(item["accepted"] and not item["fallback"] for item in result["cases"]))
        self.assertTrue(all(item["checks"]["synthetic_evidence"] for item in result["cases"]))
        self.assertFalse(result["live_provider_selected"])
        self.assertFalse(result["real_market_data_requested"])
        self.assertFalse(result["news_network_requested"])
        self.assertFalse(result["directory_network_requested"])
        self.assertEqual(result["fixture_data_authenticity"], "synthetic")
        self.assertEqual(result["language_quality"], "requires_manual_review")
        self.assertIn("not_language_or_investment_quality", result["evaluation_scope"])
        self.assertEqual(result["reference_date"], "2026-09-26")
        self.assertIsNotNone(result["evaluated_at"])
        self.env_loader.assert_not_called()

    def test_nine_call_minimum_and_ten_call_hard_cap_are_checked_before_model_or_env(self):
        for budget in (0, 1, 8, 11, 100, True, 9.0, None):
            with self.subTest(budget=budget), patch.object(evaluation, "_build_model", side_effect=AssertionError("No model construction")) as factory:
                result = evaluation.evaluate_company_research(provider="deepseek", model_call_budget=budget)
            self.assertEqual(result["status"], "invalid_request")
            self.assertEqual(result["summary"]["model_calls"], 0)
            factory.assert_not_called()
        self.env_loader.assert_not_called()

    def test_exact_required_budget_and_a_fixed_case_subset_are_supported(self):
        result = evaluation.evaluate_company_research(model_call_budget=9)
        self.assertEqual(result["status"], "passed")
        selected = evaluation.evaluate_company_research(selected_cases=["risk_scenario"], model_call_budget=2)
        self.assertEqual(selected["status"], "passed")
        self.assertEqual(selected["required_model_call_budget"], 2)
        self.assertEqual(selected["summary"]["model_calls"], 2)
        self.assertEqual(selected["summary"]["analyses"], 1)
        self.assertEqual(selected["cases"][0]["case"], "risk_scenario")
        self.assertIn("参考止损", selected["cases"][0]["answer"])
        self.assertIn("合成历史", selected["cases"][0]["answer"])

    def test_invalid_cases_and_provider_do_not_load_environment_or_construct_models(self):
        for cases in ([], (), "risk_scenario", ["unknown"], ["pe_knowledge", "pe_knowledge"], [{}], [False]):
            with self.subTest(cases=cases), patch.object(evaluation, "_build_model") as factory:
                result = evaluation.evaluate_company_research(selected_cases=cases)
            self.assertEqual(result["status"], "invalid_request")
            factory.assert_not_called()
        self.assertEqual(evaluation.evaluate_company_research(provider="other")["code"], "evaluation_provider_invalid")
        self.env_loader.assert_not_called()

    def test_live_provider_selection_loads_only_explicit_model_configuration_using_mock_adapter(self):
        configuration = {"DEEPSEEK_MODEL": "offline-test-model", "DEEPSEEK_API_KEY": "synthetic-placeholder"}
        with (patch("trading_companion.local_env.load_model_environment", return_value=configuration) as environment,
              patch("trading_companion.deepseek_tool_model.DeepSeekToolModel", return_value=evaluation._FixedModel()) as adapter):
            result = evaluation.evaluate_company_research(provider="deepseek", selected_cases=["pe_knowledge"], model_call_budget=1)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["live_provider_selected"])
        environment.assert_called_once_with()
        adapter.assert_called_once()
        self.assertIs(type(adapter.call_args.args[0]), DeepSeekConfig)
        self.assertEqual(adapter.call_args.args[0].max_output_tokens, 1200)
        self.assertNotIn(configuration["DEEPSEEK_API_KEY"], json.dumps(result))

    def test_configuration_error_is_sanitized_and_does_not_silently_fall_back_to_fake(self):
        with patch("trading_companion.local_env.load_model_environment", side_effect=RuntimeError(PRIVATE)) as environment:
            result = evaluation.evaluate_company_research(provider="deepseek")
        self.assertEqual((result["status"], result["code"]), ("invalid_request", "model_configuration_error"))
        self.assertEqual(result["cases"], [])
        self.assertNotIn(PRIVATE, json.dumps(result))
        environment.assert_called_once_with()

    def test_invalid_final_outputs_are_recorded_as_fallbacks_without_raw_response_leaks(self):
        class InvalidAnswers:
            def complete(self, messages, *, tools):
                if messages[0].content == PRODUCT_ANSWER_INSTRUCTION:
                    return ToolModelResponse("complete", PRIVATE + "<script>bad</script>")
                return evaluation._FixedModel().complete(messages, tools=tools)
        with patch.object(evaluation, "_build_model", return_value=InvalidAnswers()):
            result = evaluation.evaluate_company_research()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["summary"]["passed"], 1)
        self.assertEqual(result["summary"]["failed"], 4)
        self.assertEqual(result["summary"]["model_calls"], 9)
        self.assertTrue(all(item["fallback"] and item["answer_origin"] == "application" for item in result["cases"][1:]))
        self.assertIn("参考止损", result["cases"][-1]["answer"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn("<script>", json.dumps(result))

    def test_provider_failures_are_not_retried_and_do_not_print_private_errors(self):
        class FailingModel:
            def complete(self, messages, *, tools):
                raise ModelCallError(PRIVATE, PRIVATE)
        with patch.object(evaluation, "_build_model", return_value=FailingModel()):
            result = evaluation.evaluate_company_research()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["summary"]["model_calls"], 5)
        self.assertEqual(result["summary"]["analyses"], 0)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_only_reported_usage_is_aggregated_and_missing_usage_is_explicit(self):
        result = evaluation.evaluate_company_research()
        usage = result["summary"]["token_usage"]
        self.assertIsNone(usage["reported_total_tokens"])
        self.assertEqual(usage["requests_without_complete_usage"], 9)
        self.assertFalse(usage["complete_for_forwarded_requests"])
        class UsageModel:
            def complete(self, messages, *, tools):
                response = evaluation._FixedModel().complete(messages, tools=tools)
                return replace(response, usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        with patch.object(evaluation, "_build_model", return_value=UsageModel()):
            result = evaluation.evaluate_company_research()
        usage = result["summary"]["token_usage"]
        self.assertEqual((usage["reported_prompt_tokens"], usage["reported_completion_tokens"], usage["reported_total_tokens"]), (90, 180, 270))
        self.assertEqual(usage["requests_with_complete_usage"], 9)
        self.assertTrue(usage["complete_for_forwarded_requests"])
        self.assertIn("not_cost", usage["scope"])

    def test_cli_defaults_to_fake_and_emits_only_json_without_writing_a_report(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = evaluation.main(["--case", "pe_knowledge", "--model-call-budget", "1"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["provider"], "fake")
        self.assertEqual(result["summary"]["model_calls"], 1)
        self.assertNotIn('"model_response":', output.getvalue())
        self.env_loader.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output):
            status = evaluation.main(["--provider", "deepseek", "--model-call-budget", "8"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "evaluation_budget_too_small")


if __name__ == "__main__":
    unittest.main()
