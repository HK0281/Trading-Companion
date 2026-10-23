"""Evaluation correctness is offline; fake passes are never live-quality evidence."""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

from scripts.verify import offline_network
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.mvp_evaluation import FixtureModel, _percentile, _usage, main, run_mvp_evaluation
from trading_companion.mvp_evaluation_cases import SCENARIOS
from trading_companion.tool_model import ToolModelResponse


PRIVATE = "private-provider-marker-not-for-report"


class MvpEvaluationTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No real market request")),
                      patch("requests.post", side_effect=AssertionError("No real model request")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                            side_effect=AssertionError("Do not read credentials in offline evaluation"))):
            guard.start()
            self.addCleanup(guard.stop)
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def test_catalogue_has_thirty_distinct_reviewable_scenarios(self):
        self.assertEqual(len(SCENARIOS), 30)
        self.assertEqual(len({item.name for item in SCENARIOS}), 30)
        self.assertTrue(all(item.steps for item in SCENARIOS))
        categories = {item.category for item in SCENARIOS}
        self.assertTrue({"numeric_research", "identity", "clarification", "defaults", "conversation", "scope", "memory", "knowledge"} <= categories)

    def test_default_is_one_offline_case_not_thirty_paid_calls(self):
        result = run_mvp_evaluation()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["selected_cases"], ["single_sma"])
        self.assertEqual(result["summary"]["provider_calls_forwarded"], 2)
        self.assertFalse(result["live_provider_selected"])
        self.assertFalse(result["market_data_requested"])
        self.assertFalse(result["directory_network_requested"])
        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["summary"]["quality_acceptance"], "pending_live_repeats_and_human_review")

    def test_report_records_date_provider_identity_and_prompt_fingerprints_not_messages(self):
        result = run_mvp_evaluation()
        self.assertIn("+00:00", result["started_at"])
        step = result["cases"][0]["steps"][0]
        self.assertEqual(len(step["system_tool_sha256"]), 2)
        self.assertTrue(all(len(value) == 64 and all(character in "0123456789abcdef" for character in value)
                            for value in step["system_tool_sha256"]))
        self.assertEqual([item["status"] for item in step["responses"]], ["tool_calls", "complete"])
        self.assertTrue(all(item["provider"] == "fake" and item["is_mock"] for item in step["responses"]))
        for forbidden in ("model_draft", '"messages":', '"api_key":'):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_full_catalogue_three_repeats_reuses_real_agent_and_has_no_skips(self):
        result = run_mvp_evaluation(case="all", repeat=3, model_call_limit=512)
        self.assertEqual(result["status"], "passed", [(item["case"], item["steps"]) for item in result["cases"] if item["status"] != "passed"])
        self.assertEqual((result["summary"]["executed"], result["summary"]["passed"], result["summary"]["not_run"]), (90, 90, 0))
        planned = sum(item.model_call_limit for item in SCENARIOS) * 3
        self.assertEqual(result["planned_model_calls"], planned)
        self.assertEqual(result["summary"]["model_calls_attempted"], planned)
        self.assertEqual(result["summary"]["provider_calls_forwarded"], planned)
        self.assertTrue(all(all(all(step["checks"].values()) for step in item["steps"]) for item in result["cases"]))

    def test_repeats_use_new_sessions_and_fresh_fixture_models(self):
        result = run_mvp_evaluation(case="ambiguous_edit", repeat=3, model_call_limit=20)
        self.assertEqual(result["status"], "passed")
        self.assertEqual([item["repeat"] for item in result["cases"]], [1, 2, 3])
        self.assertTrue(all(item["steps"][1]["observed"]["analyses"] == 0 for item in result["cases"]))
        self.assertTrue(all(item["steps"][2]["observed"]["analyses"] == 1 for item in result["cases"]))

    def test_inadequate_whole_run_allowance_rejects_before_fixture_or_credentials(self):
        with patch("trading_companion.mvp_evaluation.load_development_fixtures", side_effect=AssertionError("No fixture read")):
            for provider in ("fake", "deepseek"):
                with self.subTest(provider=provider), self.assertRaises(ModelConfigurationError):
                    run_mvp_evaluation(provider=provider, case="all", repeat=3, model_call_limit=12)

    def test_invalid_options_never_expand_into_model_work(self):
        cases = ({"repeat": 0}, {"repeat": 4}, {"repeat": True}, {"provider": "openai"},
                 {"case": "custom path"}, {"model_call_limit": -1}, {"model_call_limit": 513},
                 {"model_call_limit": True}, {"provider": []}, {"case": None})
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ModelConfigurationError):
                run_mvp_evaluation(**options)

    def test_local_definition_accepts_zero_model_allowance(self):
        result = run_mvp_evaluation(case="knowledge", model_call_limit=0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["summary"]["provider_calls_forwarded"], 0)
        self.assertIsNone(result["summary"]["token_usage"]["reported_total_tokens"])

    def test_fixture_failure_is_reported_without_paths_or_model_work(self):
        from trading_companion.errors import DataValidationError
        with patch("trading_companion.mvp_evaluation.load_development_fixtures", side_effect=DataValidationError(PRIVATE)):
            result = run_mvp_evaluation(provider="deepseek")
        self.assertEqual(result["code"], "fixture_unavailable")
        self.assertEqual(result["summary"]["executed"], 0)
        self.assertEqual(result["summary"]["not_run"], 1)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_bad_live_configuration_is_sanitized_and_all_work_remains_not_run(self):
        with patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=ModelConfigurationError(PRIVATE)):
            result = run_mvp_evaluation(provider="deepseek")
        self.assertEqual(result["code"], "model_configuration_error")
        self.assertEqual(result["summary"]["executed"], 0)
        self.assertEqual(result["summary"]["provider_calls_forwarded"], 0)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_explicit_live_adapter_path_is_tested_with_mocked_provider_only(self):
        fixture = FixtureModel()
        fixture.step = SCENARIOS[0].steps[0]
        class MockLive:
            def complete(self, messages, *, tools):
                response = fixture.complete(messages, tools=tools)
                return replace(response, provider="deepseek", is_mock=False,
                               requested_model="mocked-live-transport", model="mocked-live-transport")
        with patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", return_value=object()) as config, patch(
            "trading_companion.deepseek_tool_model.DeepSeekToolModel", return_value=MockLive()) as adapter:
            result = run_mvp_evaluation(provider="deepseek")
        self.assertEqual(result["status"], "passed")
        config.assert_called_once()
        adapter.assert_called_once()
        self.assertTrue(result["live_provider_selected"])
        self.assertEqual(result["summary"]["quality_acceptance"], "pending_live_repeats_and_human_review")

    def test_first_failure_stops_remaining_cases_and_repeats_without_retry(self):
        with patch.object(FixtureModel, "complete", side_effect=ModelCallError(PRIVATE, PRIVATE)) as calls:
            result = run_mvp_evaluation(case="all", repeat=3, model_call_limit=512)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["summary"]["executed"], 1)
        self.assertEqual(result["summary"]["not_run"], 89)
        self.assertEqual(result["summary"]["provider_calls_forwarded"], 1)
        calls.assert_called_once()
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_unexpected_runtime_exception_is_not_echoed(self):
        with patch("trading_companion.mvp_evaluation.run_research_turn", side_effect=RuntimeError(PRIVATE)):
            result = run_mvp_evaluation()
        step = result["cases"][0]["steps"][0]
        self.assertEqual(step["code"], "evaluation_internal_error")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_wrong_expected_scope_does_not_pass_just_because_agent_completed(self):
        from trading_companion.agent import run_research_turn as real
        def wrong(*args, **kwargs):
            outcome = real(*args, **kwargs)
            return replace(outcome, confirmed_request=replace(outcome.confirmed_request, instrument_id="600519.SH"))
        with patch("trading_companion.mvp_evaluation.run_research_turn", side_effect=wrong):
            result = run_mvp_evaluation()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cases"][0]["steps"][0]["checks"]["confirmed_scope"])

    def test_partial_calculation_cannot_hide_a_rejected_model_explanation(self):
        original = FixtureModel.complete
        def unstructured(model, messages, *, tools):
            response = original(model, messages, tools=tools)
            return replace(response, text=PRIVATE) if response.status == "complete" else response
        with patch.object(FixtureModel, "complete", unstructured):
            result = run_mvp_evaluation(case="partial_history")
        step = result["cases"][0]["steps"][0]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(step["checks"]["agent_code"])
        self.assertFalse(step["checks"]["explanation_status"])
        self.assertFalse(step["checks"]["no_failure_code"])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_completed_agent_cannot_hide_blocked_or_missing_synthesis(self):
        from trading_companion.research_synthesis import ResearchSynthesis
        for synthesis in (None, ResearchSynthesis("blocked", "synthesis_evidence_mismatch", "Synthesis blocked")):
            with self.subTest(synthesis=synthesis), patch(
                "trading_companion.agent.build_research_synthesis", return_value=synthesis):
                result = run_mvp_evaluation(case="complete_combination")
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["cases"][0]["steps"][0]["checks"]["synthesis_status"])

    def test_historical_overview_must_actually_display_rechecked_synthesis(self):
        from trading_companion.research_synthesis import ResearchSynthesis
        with patch("trading_companion.research_followup.build_research_synthesis",
                   return_value=ResearchSynthesis("blocked", "synthesis_evidence_mismatch", "Synthesis blocked")):
            result = run_mvp_evaluation(case="explicit_history")
        self.assertEqual(result["status"], "failed")
        step = result["cases"][0]["steps"][1]
        self.assertTrue(step["checks"]["synthesis_status"])
        self.assertFalse(step["checks"]["synthesis_display"])

    def test_usage_preserves_missing_information_and_counts_forwarded_errors(self):
        responses = [ToolModelResponse("complete", "OK", usage=TokenUsage(10, 2, 12)),
                     ToolModelResponse("complete", "OK", usage=TokenUsage(prompt_tokens=5))]
        usage = _usage(responses, 3)
        self.assertEqual(usage["reported_prompt_tokens"], 15)
        self.assertEqual(usage["reported_total_tokens"], 12)
        self.assertEqual(usage["requests_with_complete_usage"], 1)
        self.assertEqual(usage["requests_without_complete_usage"], 2)
        self.assertFalse(usage["complete_for_forwarded_requests"])

    def test_percentiles_are_explicit_small_sample_interpolations(self):
        self.assertIsNone(_percentile([], .95))
        self.assertEqual(_percentile([20], .5), 20)
        self.assertEqual(_percentile([0, 100], .5), 50)
        self.assertEqual(_percentile([0, 100], .95), 95)

    def test_cli_default_outputs_safe_json_without_creating_report_files(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "passed")
        self.assertNotIn("model_draft", output.getvalue())
        self.assertNotIn("access_token", output.getvalue())

    def test_cli_insufficient_allowance_and_configuration_have_distinct_exit_codes(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--case", "all", "--repeat", "3"]), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_evaluation_options")
        with patch.object(FixtureModel, "complete", side_effect=ModelCallError("timeout", PRIVATE)), redirect_stdout(StringIO()):
            self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
