from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.evaluation_pricing import DEFAULT_PRICES
from trading_companion.news_triage_product_annotations import merge, write_blind_sheets
from trading_companion.news_triage_product_data import write_json
from trading_companion.news_triage_product_evaluation import evaluate, make_plan, run_experiment
from trading_companion.news_triage_product_metrics import scores, wilson_lower
from trading_companion.typesafe_jev import ClassifierError
from tests.test_news_triage_product import response, sample
from tests.test_news_triage_product_data import dataset_for, fill


class ProductEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = dataset_for()
        self.dataset_path = self.root/"dataset.json"
        write_json(self.dataset_path, self.dataset)
        write_blind_sheets(self.dataset, self.root)
        self.paths = [self.root/(name+".csv") for name in ("codex", "claude")]
        self.annotation_path = self.root/"annotations.json"

    def annotated(self):
        for path in self.paths: fill(path)
        report = merge(self.dataset, *self.paths)
        write_json(self.annotation_path, report)
        return report

    def experiment(self, *, arms=None, call_limit=6, jev=None, deepseek=None):
        annotations = self.annotated()
        plan, dataset, _, prices = make_plan(dataset_path=self.dataset_path, annotations_path=self.annotation_path,
                                             arms=arms, call_limit=call_limit)
        models = {"jev": Mock(classify=Mock(return_value=response() if jev is None else jev)),
                  "deepseek": Mock(classify=Mock(return_value=response(confidence=None, model="deepseek-flash")
                                                   if deepseek is None else deepseek))}
        return run_experiment(plan, dataset, annotations, prices, models), models

    def test_default_plan_reads_no_credentials_constructs_no_clients_and_writes_nothing(self):
        output = self.root/"must_not_exist"
        with patch("trading_companion.news_triage_product_evaluation.load_evaluation_key_environment") as keys, \
                patch("trading_companion.news_triage_product_evaluation.build_models") as clients:
            result = evaluate(dataset_path=self.dataset_path, report_dir=output)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["live_annotation_ready"])
        self.assertEqual(result["worst_case_calls"], 6)
        self.assertEqual(result["thresholds"], [.65, .90])
        keys.assert_not_called()
        clients.assert_not_called()
        self.assertFalse(output.exists())

    def test_live_rejects_unreviewed_blanks_missing_budget_before_keys(self):
        write_json(self.annotation_path, merge(self.dataset, *self.paths))
        for extra in ({}, {"call_limit": 2, "prices_path": DEFAULT_PRICES, "deepseek_model": "deepseek-flash"}):
            with self.subTest(extra=extra), patch("trading_companion.news_triage_product_evaluation.build_models") as models:
                with self.assertRaises(ValueError):
                    evaluate(dataset_path=self.dataset_path, annotations_path=self.annotation_path, live=True, **extra)
                models.assert_not_called()

    def test_separate_arms_really_call_models_and_thresholds_differ(self):
        report, models = self.experiment()
        self.assertEqual(report["accounting"]["attempted"], 5)
        self.assertEqual(models["jev"].classify.call_count, 3)
        self.assertEqual(models["deepseek"].classify.call_count, 2)
        self.assertEqual(report["metrics"]["cascade_0.65"]["calls"], 1)
        self.assertEqual(report["metrics"]["cascade_0.90"]["calls"], 2)
        self.assertEqual(report["metrics"]["jev"]["costs"]["jev"]["requests"], 1)
        for model in models.values():
            for call in model.classify.call_args_list:
                self.assertEqual(set(call.args[0]), {"title", "company_name", "instrument_id", "identity"})
        self.assertFalse(report["product_enablement"])

    def test_no_budget_leaves_review_unresolved_and_zero_attempt_arms_not_run(self):
        report, _ = self.experiment(arms=["cascade_0.90", "deepseek"], call_limit=1)
        results = report["rows"][0]["results"]
        self.assertEqual(results["cascade_0.90"]["status"], "unresolved")
        self.assertEqual(results["deepseek"]["status"], "not_run")
        self.assertEqual(report["status"], "partial")
        scored = report["metrics"]["cascade_0.90"]["questions"]["importance"]
        self.assertEqual(scored["denominator"], 1)
        self.assertEqual(scored["per_class"]["high"]["recall"], 0)
        self.assertEqual(scored["coverage"], 0)

    def test_unknown_usage_stops_further_paid_work_and_alias_cost_is_unknown(self):
        bad = response(usage={"prompt_tokens": 10, "completion_tokens": None, "total_tokens": None}, model="jev-1.14.0")
        report, models = self.experiment(jev=bad)
        self.assertEqual(report["accounting"]["attempted"], 1)
        models["deepseek"].classify.assert_not_called()
        cost = report["metrics"]["jev"]["costs"]["jev"]
        self.assertIsNone(cost["estimated_total"])
        self.assertEqual(cost["unknown_requests"], 1)
        self.assertIn("model_identity_unknown_or_mismatch", cost["unknown_reasons"])
        self.assertEqual(report["accounting"]["stop_reason"], "usage_unknown_or_invalid")

    def test_observed_per_title_token_limit_skips_remaining_optional_requests(self):
        report, models = self.experiment(jev=response(usage={"prompt_tokens": 24000, "completion_tokens": 5, "total_tokens": 24005}))
        self.assertEqual(report["accounting"]["attempted"], 1)
        models["deepseek"].classify.assert_not_called()
        self.assertEqual(report["rows"][0]["results"]["deepseek"]["reason"], "per_title_budget")
        self.assertEqual(report["status"], "partial")

    def test_high_confidence_identity_collision_still_requires_deepseek(self):
        row = self.dataset["rows"][0]
        row["identity"].update(collision_suspected=True, credible_target_evidence=False, mismatch_basis=["extended_entity_name"])
        # Runner uses the prepared immutable row; this local injection tests routing only.
        annotation = {"rows": [{"id": row["id"], "gold": {"relevance": "unrelated", "tone": "neutral", "importance": "ordinary"}}]}
        plan, _, _, prices = make_plan(dataset_path=self.dataset_path, arms=["cascade_0.65"], call_limit=2)
        models = {"jev": Mock(classify=Mock(return_value=response(confidence=.99))),
                  "deepseek": Mock(classify=Mock(return_value=response(relevance="unrelated", importance="ordinary", confidence=None)))}
        report = run_experiment(plan, self.dataset, annotation, prices, models)
        self.assertFalse(report["rows"][0]["results"]["cascade_0.65"]["proposed_filter"])
        self.assertEqual(report["rows"][0]["results"]["cascade_0.65"]["answers"]["relevance"]["choice"], "unrelated")
        models["deepseek"].classify.assert_called_once()

    def test_failures_consume_budget_preserve_metadata_and_do_not_expose_error_body(self):
        annotations = self.annotated()
        plan, dataset, _, prices = make_plan(dataset_path=self.dataset_path, annotations_path=self.annotation_path,
                                             arms=["cascade_0.65"], call_limit=2)
        error = ClassifierError("invalid_response", metadata={"model": "served", "usage": response()["usage"],
                                                              "secret": "NEVER_PERSIST"})
        models = {"jev": Mock(classify=Mock(side_effect=error)),
                  "deepseek": Mock(classify=Mock(return_value=response()))}
        report = run_experiment(plan, dataset, annotations, prices, models)
        self.assertEqual(len(report["ledger"]), 2)
        self.assertEqual(report["ledger"][0]["status"], "failed")
        self.assertEqual(report["ledger"][0]["usage"]["total_tokens"], 15)
        self.assertNotIn("NEVER_PERSIST", json.dumps(report))

    def test_gold_unresolved_and_model_abstention_are_explicit_in_denominator(self):
        report, _ = self.experiment(deepseek=response(importance="unresolved", confidence=None))
        self.assertEqual(report["metrics"]["deepseek"]["statuses"], {"unresolved": 1})
        importance = report["metrics"]["deepseek"]["questions"]["importance"]
        self.assertEqual(importance["denominator"], 1)
        self.assertEqual(importance["coverage"], 0)
        self.assertEqual(importance["accuracy_all_titles"], 0)

    def test_support_gates_and_wilson_are_not_weakened(self):
        row = {"gold": {"relevance": "primary", "tone": "positive", "importance": "high"},
               "results": {"test": {"status": "ok", **response(tone="positive"), "proposed_filter": False}}}
        result = scores([row]*29, "test")
        self.assertFalse(result["gates"]["importance_sorting"]["sufficient_support"])
        self.assertFalse(result["gates"]["unrelated_filtering"]["passed"])
        self.assertFalse(result["gates"]["tone_labels"]["passed"])
        self.assertTrue(scores([row]*30, "test")["gates"]["importance_sorting"]["passed"])
        self.assertGreater(wilson_lower(50, 50), .90)
        self.assertLess(wilson_lower(49, 50), .90)

    def test_cli_default_prints_plan_only_and_bad_live_returns_two(self):
        from scripts.evaluate_news_triage_product import main
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(["--dataset", str(self.dataset_path)]), 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "planned")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--dataset", str(self.dataset_path), "--live"]), 2)
