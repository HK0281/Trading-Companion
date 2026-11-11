from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.news_triage_product import OPTIONS, questions, input_state
from trading_companion.news_triage_product_batch import SPLIT_ARM
from trading_companion.news_triage_product_data import write_json
from trading_companion.news_triage_product_ensemble import ENSEMBLE_ARM, ENSEMBLE_POLICY
from trading_companion.news_triage_product_evaluation import evaluate, make_plan, run_experiment
from trading_companion.news_triage_product_models import (
    ProductDeepSeek, validate_batch_items, validate_question_batch_items,
)
from trading_companion.typesafe_jev import ClassifierError
from tests.test_news_triage_product import response, sample
from tests.test_news_triage_product_batch import packet
from tests.test_news_triage_product_data import dataset_for
from tests.test_typesafe_jev import http_response


def labels(sid, *, relevance="primary", tone="neutral", importance="high"):
    return {"id": sid, "relevance": relevance, "tone": tone, "importance": importance}


class EnsembleProtocolTests(unittest.TestCase):
    def test_three_question_batch_uses_frozen_definitions_and_no_gold(self):
        requested = [{"id": "a", **input_state(sample())}]
        post = Mock(return_value=http_response(packet([labels("a")])))
        result = ProductDeepSeek(DeepSeekConfig("deepseek-flash", "test"), post=post).classify_questions_batch(requested)
        body = post.call_args.kwargs["json"]
        self.assertIn(json.dumps(questions(), ensure_ascii=False), body["messages"][0]["content"])
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"items": requested})
        self.assertEqual(set(result["items"][0]["answers"]), set(OPTIONS))
        self.assertTrue(all(a["confidence"] is None and a["probabilities"] is None
                            for a in result["items"][0]["answers"].values()))

    def test_positional_and_shape_failures_never_repair_other_items(self):
        req, good = [{"id": sid} for sid in "abc"], [labels(sid) for sid in "abc"]
        for name, returned, states in (
            ("missing", good[:2], ["ok", "ok", "unresolved"]),
            ("reordered", [good[1], good[0], good[2]], ["unresolved", "unresolved", "ok"]),
            ("duplicate", [good[0], good[0], good[2]], ["unresolved", "unresolved", "ok"]),
            ("extra", [*good, labels("foreign")], ["ok"]*3),
            ("missing_middle", [good[0], good[2]], ["ok", "unresolved", "unresolved"]),
            ("shape", [good[0], {"id": "b", "importance": "high"}, good[2]], ["ok", "unresolved", "ok"]),
            ("id_type", [good[0], {**good[1], "id": []}, good[2]], ["ok", "unresolved", "ok"]),
        ):
            with self.subTest(name=name):
                result = validate_question_batch_items(req, returned)
                self.assertEqual([r["status"] for r in result["items"]], states)
                self.assertEqual([r["id"] for r in result["items"]], list("abc"))
                self.assertEqual(result["extra_returned_items"], max(0, len(returned)-3))

    def test_invalid_question_or_abstention_keeps_other_questions(self):
        for bad, reason in (("buy", "invalid_label"), ([], "invalid_label"), ("unresolved", "model_abstention")):
            with self.subTest(bad=bad):
                r = validate_question_batch_items([{"id": "a"}], [labels("a", tone=bad)])["items"][0]
                self.assertEqual(r["answers"]["tone"]["choice"], "unresolved")
                self.assertEqual(r["question_status"]["tone"]["reason"], reason)
                self.assertEqual(r["answers"]["relevance"]["choice"], "primary")
                self.assertEqual(r["answers"]["importance"]["choice"], "high")
        r = validate_question_batch_items([{"id": "a"}], [labels("a", relevance="unrelated", tone="positive")])["items"][0]
        self.assertEqual(r["answers"]["tone"]["choice"], "unresolved")
        self.assertEqual(r["question_status"]["tone"]["reason"], "unrelated_directional_tone")

    def test_invalid_envelope_preserves_usage_and_rejects_untrusted_batches(self):
        payload = packet([])
        payload["choices"][0]["message"]["content"] = '{"items": {}, "tone":"neutral"}'
        model = ProductDeepSeek(DeepSeekConfig("deepseek-flash", "test"), post=Mock(return_value=http_response(payload)))
        with self.assertRaises(ClassifierError) as error:
            model.classify_questions_batch([{"id": "a", **input_state(sample())}])
        self.assertEqual(error.exception.metadata["usage"]["total_tokens"], 15)
        for items in ([], [{"id": str(i), "instrument_id": "600000.SH"} for i in range(11)],
                      [{"id": "a", "instrument_id": code} for code in ("600000.SH", "600036.SH")]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                model.classify_questions_batch(items)
        self.assertEqual(model.post.call_count, 1)


class EnsembleEvaluationTests(unittest.TestCase):
    def run_case(self, *, jev_answers=None, returned=None, jev_error=None, ds_error=None,
                 limit=4, usage=None, clock=lambda: 0, arms=None):
        arms = [ENSEMBLE_ARM] if arms is None else arms
        dataset = dataset_for()
        dataset["rows"] = [{**deepcopy(dataset["rows"][0]), "id": sid, "capture_batch": "B", "capture_file": "B/1.json"}
                           for sid in "abc"]
        annotations = {"rows": [{"id": sid, "gold": {"relevance": "primary", "tone": "neutral", "importance": "high"}}
                                for sid in "abc"]}
        with TemporaryDirectory() as temp:
            path = Path(temp)/"dataset.json"
            write_json(path, dataset_for())
            plan, _, _, prices = make_plan(dataset_path=path, arms=arms)
        plan.update(call_limit=limit)
        first = deepcopy(response() if jev_answers is None else jev_answers)
        if usage is not None:
            first["usage"] = usage
        requested = [{"id": sid} for sid in "abc"]
        returned = [labels(sid) for sid in "abc"] if returned is None else returned
        ds = {"model": "deepseek-flash", "usage": response()["usage"],
              **validate_question_batch_items(requested, returned)}
        split_ds = {"model": "deepseek-flash", "usage": response()["usage"],
                    **validate_batch_items(requested, [{"id": sid, "importance": "high"} for sid in "abc"])}
        models = {"jev": Mock(classify=Mock(side_effect=jev_error, return_value=first)),
                  "deepseek": Mock(classify_questions_batch=Mock(side_effect=ds_error, return_value=ds),
                                   classify_importance_batch=Mock(return_value=split_ds))}
        return run_experiment(plan, dataset, annotations, prices, models, clock=clock), models

    def test_both_models_answer_three_questions_with_single_batch_cost_and_diagnostics(self):
        report, models = self.run_case()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["accounting"]["attempted"], 4)
        self.assertEqual(models["jev"].classify.call_count, 3)
        models["deepseek"].classify_questions_batch.assert_called_once()
        for call in models["jev"].classify.call_args_list:
            self.assertEqual(call.kwargs["selected_questions"], tuple(OPTIONS))
            self.assertEqual(set(call.args[0]), {"title", "instrument_id", "company_name", "identity"})
        for item in models["deepseek"].classify_questions_batch.call_args.args[0]:
            self.assertEqual(set(item), {"id", "title", "instrument_id", "company_name", "identity"})
        scored = report["metrics"][ENSEMBLE_ARM]
        self.assertEqual(scored["provider_calls"], {"typesafe": 3, "deepseek": 1})
        self.assertEqual(scored["costs"]["deepseek"]["requests"], 1)
        for provider in ("jev", "deepseek"):
            component = scored["components"][provider]
            self.assertNotIn("costs", component)
            self.assertEqual(component["questions"]["importance"]["accuracy_all_titles"], 1)
        for row in report["rows"]:
            result = row["results"][ENSEMBLE_ARM]
            self.assertEqual(result["component_results"]["deepseek"]["call_ids"], [4])
            self.assertEqual(result["question_status"]["importance"]["call_ids"][-1], 4)
            self.assertIsNone(result["answers"]["importance"]["confidence"])
            self.assertEqual(result["answers"]["relevance"]["confidence"], .8)
            self.assertFalse(result["proposed_filter"])

    def test_predeclared_tone_agreement_high_union_and_unknown_fallback(self):
        first = response(tone="positive", importance="ordinary")
        report, _ = self.run_case(jev_answers=first, returned=[
            labels("a", tone="positive", importance="high"),
            labels("b", tone="negative", importance="unknown"),
            labels("c", tone="neutral", importance="ordinary")])
        results = [r["results"][ENSEMBLE_ARM] for r in report["rows"]]
        self.assertEqual([r["answers"]["tone"]["choice"] for r in results], ["positive", "unresolved", "unresolved"])
        self.assertEqual([r["answers"]["importance"]["choice"] for r in results], ["high", "unknown", "ordinary"])
        self.assertEqual(report["status"], "completed")  # Policy abstention is not a transport failure.
        scored = report["metrics"][ENSEMBLE_ARM]
        self.assertEqual(scored["questions"]["tone"]["coverage"], 1/3)
        self.assertEqual(scored["combination"]["tone"]["valid_disagreements"], 2)
        self.assertEqual(scored["components"]["jev"]["questions"]["tone"]["coverage"], 1)
        self.assertEqual(scored["components"]["deepseek"]["questions"]["tone"]["coverage"], 1)

    def test_jev_high_survives_ds_ordinary_and_jev_relevance_wins_diagnostic_disagreement(self):
        report, _ = self.run_case(returned=[labels(sid, relevance="mentioned", importance="ordinary") for sid in "abc"])
        scored = report["metrics"][ENSEMBLE_ARM]
        self.assertEqual(scored["questions"]["importance"]["per_class"]["high"]["recall"], 1)
        self.assertEqual(scored["questions"]["relevance"]["accuracy_all_titles"], 1)
        self.assertEqual(scored["components"]["deepseek"]["questions"]["relevance"]["accuracy_all_titles"], 0)
        self.assertEqual(scored["combination"]["relevance"]["valid_disagreements"], 3)

    def test_failure_abstention_and_invalid_question_do_not_become_agreement_or_unknown(self):
        error = ClassifierError("invalid_response", metadata={"model": "jev-latest", "usage": response()["usage"]})
        report, _ = self.run_case(jev_error=error, returned=[labels("a"), labels("b", importance="ordinary"),
                                                         labels("c", importance="unresolved")])
        results = [r["results"][ENSEMBLE_ARM] for r in report["rows"]]
        self.assertEqual([r["answers"]["importance"]["choice"] for r in results], ["high", "unresolved", "unresolved"])
        self.assertEqual(report["status"], "partial")
        scored = report["metrics"][ENSEMBLE_ARM]["questions"]
        self.assertEqual(scored["tone"]["coverage"], 0)
        self.assertEqual(scored["importance"]["coverage"], 1/3)
        self.assertEqual(scored["importance"]["denominator"], 3)
        report, _ = self.run_case(jev_answers=response(importance="ordinary"), returned=[
            labels("a", importance="unresolved"), labels("b", importance="buy"), labels("c", tone="buy")])
        results = [r["results"][ENSEMBLE_ARM] for r in report["rows"]]
        self.assertEqual([r["answers"]["importance"]["choice"] for r in results], ["unresolved", "unresolved", "high"])
        self.assertEqual([r["answers"]["tone"]["choice"] for r in results], ["neutral", "neutral", "unresolved"])
        self.assertEqual(report["metrics"][ENSEMBLE_ARM]["components"]["deepseek"]["questions"]["relevance"]["coverage"], 1)

    def test_ds_failure_preserves_jev_relevance_and_only_valid_high(self):
        error = ClassifierError("invalid_response", metadata={"model": "deepseek-flash", "usage": response()["usage"]})
        for importance in ("high", "ordinary", "unknown"):
            with self.subTest(importance=importance):
                report, _ = self.run_case(jev_answers=response(importance=importance), ds_error=error)
                result = report["rows"][0]["results"][ENSEMBLE_ARM]
                self.assertEqual(result["answers"]["relevance"]["choice"], "primary")
                self.assertEqual(result["answers"]["importance"]["choice"], "high" if importance == "high" else "unresolved")
                self.assertEqual(result["answers"]["tone"]["choice"], "unresolved")
                self.assertEqual(report["accounting"]["attempted"], 4)

    def test_both_failures_and_extra_items_are_explicit(self):
        err = ClassifierError("invalid_response", metadata={"model": "served", "usage": response()["usage"]})
        report, _ = self.run_case(jev_error=err, ds_error=err)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["metrics"][ENSEMBLE_ARM]["questions"]["importance"]["coverage"], 0)
        report, _ = self.run_case(returned=[labels(sid) for sid in "abcx"])
        scored = report["metrics"][ENSEMBLE_ARM]
        self.assertEqual(report["status"], "partial")
        self.assertEqual(scored["question_batches"]["extra_returned_items"], 1)
        self.assertEqual(scored["question_batches"]["unknown_returned_items"], 1)
        self.assertEqual(scored["questions"]["importance"]["coverage"], 1)

    def test_global_usage_token_and_time_limits_stop_without_retrying(self):
        for kwargs, attempts, reason in (({"limit": 2}, 2, None),
              ({"usage": {}}, 1, "usage_unknown_or_invalid"),
              ({"usage": {"prompt_tokens": 24000, "completion_tokens": 1, "total_tokens": 24001}}, 1, None)):
            with self.subTest(kwargs=kwargs):
                report, models = self.run_case(**kwargs)
                self.assertEqual(report["accounting"]["attempted"], attempts)
                self.assertEqual(report["accounting"]["stop_reason"], reason)
                models["deepseek"].classify_questions_batch.assert_not_called()
                self.assertEqual(report["metrics"][ENSEMBLE_ARM]["questions"]["tone"]["coverage"], 0)
        now = [0]
        def clock():
            now[0] += 35
            return now[0]
        report, models = self.run_case(clock=clock)
        self.assertEqual(report["accounting"]["attempted"], 1)
        models["deepseek"].classify_questions_batch.assert_not_called()
        self.assertEqual(report["metrics"][ENSEMBLE_ARM]["question_batches"]["batches"][0]["reason"], "batch_budget")

    def test_mixed_split_and_ensemble_have_separate_budgets_and_no_free_reuse(self):
        report, models = self.run_case(arms=[SPLIT_ARM, ENSEMBLE_ARM], limit=8)
        self.assertEqual(report["accounting"]["attempted"], 8)
        self.assertEqual(models["jev"].classify.call_count, 6)
        models["deepseek"].classify_importance_batch.assert_called_once()
        models["deepseek"].classify_questions_batch.assert_called_once()
        for arm in (SPLIT_ARM, ENSEMBLE_ARM):
            self.assertEqual(report["metrics"][arm]["calls"], 4)
            self.assertEqual(report["metrics"][arm]["costs"]["deepseek"]["requests"], 1)
        for call in report["ledger"]:
            expected = ["importance"] if call["provider"] == "deepseek" and call["arm"] == SPLIT_ARM else (
                ["relevance", "tone"] if call["arm"] == SPLIT_ARM else list(OPTIONS))
            self.assertEqual(call["requested_questions"], expected)

    def test_plan_is_opt_in_frozen_and_default_never_loads_credentials(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/"dataset.json"
            write_json(path, dataset_for())
            with patch("trading_companion.news_triage_product_evaluation.build_models") as models:
                plan = evaluate(dataset_path=path, arms=[ENSEMBLE_ARM])
            models.assert_not_called()
            self.assertEqual(plan["worst_case_calls"], 2)
            self.assertEqual(plan["planned_question_batches"], 1)
            self.assertEqual(plan["ensemble_policy"], ENSEMBLE_POLICY)
            mixed, *_ = make_plan(dataset_path=path, arms=[SPLIT_ARM, ENSEMBLE_ARM])
            self.assertEqual(mixed["worst_case_calls"], 4)
            default, *_ = make_plan(dataset_path=path)
            self.assertNotIn(ENSEMBLE_ARM, default["arms"])
            self.assertEqual(default["worst_case_calls"], 6)
            plan["ensemble_policy"]["tone"] = "changed_after_results"
            with self.assertRaisesRegex(ValueError, "frozen"):
                run_experiment(plan, {}, {}, {}, {})
