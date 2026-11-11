from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.news_triage_product import input_state, questions
from trading_companion.news_triage_product_batch import SPLIT_ARM, make_batches
from trading_companion.news_triage_product_data import write_json
from trading_companion.news_triage_product_evaluation import make_plan, run_experiment
from trading_companion.news_triage_product_models import ProductDeepSeek, ProductJev, validate_batch_items
from trading_companion.typesafe_jev import ClassifierError, JevConfig
from tests.test_news_triage_product import response, sample
from tests.test_news_triage_product_data import dataset_for
from tests.test_typesafe_jev import http_response


def packet(items):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"items": items})}}], "usage": response()["usage"]}


class BatchProtocolTests(unittest.TestCase):
    def test_only_responsible_questions_reach_each_provider(self):
        answers = {q: {"type": "choice", "choice": "primary" if q == "relevance" else "neutral", "confidence": .8,
                       "probabilities": {v: .8 if v == ("primary" if q == "relevance" else "neutral") else .1
                                         for v in questions()[q]["criteria"]}} for q in ("relevance", "tone")}
        post = Mock(return_value=http_response({"model": "jev-latest", "answers": answers,
                                                "usage": {"input_tokens": 10, "output_tokens": 5}}))
        ProductJev(JevConfig("test"), post=post).classify(input_state(sample()), selected_questions=("relevance", "tone"))
        self.assertEqual(set(post.call_args.kwargs["json"]["questions"]), {"relevance", "tone"})
        requested = [{"id": "a", **input_state(sample())}]
        ds_post = Mock(return_value=http_response(packet([{"id": "a", "importance": "high"}])))
        result = ProductDeepSeek(DeepSeekConfig("deepseek-flash", "test"), post=ds_post).classify_importance_batch(requested)
        body = ds_post.call_args.kwargs["json"]
        self.assertIn(json.dumps({"importance": questions()["importance"]}, ensure_ascii=False), body["messages"][0]["content"])
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"items": requested})
        self.assertEqual(result["items"][0]["answer"]["choice"], "high")
        self.assertIsNone(result["items"][0]["answer"]["confidence"])

    def test_positional_failures_keep_good_siblings_without_id_reordering(self):
        req = [{"id": x} for x in "abc"]
        good = [{"id": x, "importance": "ordinary"} for x in "abc"]
        for name, returned, statuses in (
            ("missing", good[:2], ["ok", "ok", "unresolved"]),
            ("reordered", [good[1], good[0], good[2]], ["unresolved", "unresolved", "ok"]),
            ("duplicate", [good[0], good[0], good[2]], ["unresolved", "unresolved", "ok"]),
            ("invalid_label", [good[0], {"id": "b", "importance": "buy"}, good[2]], ["ok", "unresolved", "ok"]),
            ("extra", [*good, {"id": "foreign", "importance": "high"}], ["ok", "ok", "ok"]),
            ("missing_middle", [good[0], good[2]], ["ok", "unresolved", "unresolved"]),
            ("invalid_shape", [good[0], [], good[2]], ["ok", "unresolved", "ok"]),
        ):
            with self.subTest(name=name):
                result = validate_batch_items(req, returned)
                self.assertEqual([r["status"] for r in result["items"]], statuses)
                self.assertEqual([r["id"] for r in result["items"]], list("abc"))
                self.assertEqual(result["extra_returned_items"], max(0, len(returned)-3))

    def test_entire_invalid_response_keeps_usage_metadata_and_rejects_other_questions(self):
        payload = packet([])
        payload["choices"][0]["message"]["content"] = '{"items": {}, "tone":"neutral"}'
        with self.assertRaises(ClassifierError) as error:
            ProductDeepSeek(DeepSeekConfig("deepseek-flash", "test"), post=Mock(return_value=http_response(payload))).classify_importance_batch(
                [{"id": "a", **input_state(sample())}])
        self.assertEqual(error.exception.metadata["usage"]["total_tokens"], 15)

    def test_batches_separate_company_and_capture_and_cap_ten_without_gold(self):
        rows = []
        for i in range(23):
            row = {**sample(), "id": str(i), "capture_batch": "B", "capture_file": "B/one.json", "gold": "never_group_on_this"}
            if i == 21:
                row["capture_file"] = "B/two.json"
            if i == 22:
                row["instrument_id"] = "600036.SH"
            rows.append(row)
        batches = make_batches(rows)
        self.assertEqual(sorted(len(b["rows"]) for b in batches), [1, 1, 1, 10, 10])
        self.assertEqual(batches, make_batches(list(reversed(rows))))
        for b in batches:
            self.assertEqual(len({(r["instrument_id"], r["capture_file"]) for r in b["rows"]}), 1)
        self.assertEqual(len(make_batches([{**sample(), "id": str(i)} for i in range(2)])), 2)


class BatchEvaluationTests(unittest.TestCase):
    def run_batch(self, *, limit=4, jev_error=None, ds_error=None, ds_items=None, usage=None, clock=lambda: 0):
        dataset = dataset_for()
        # Runner fixtures: three titles in one checked record, all in debug.
        dataset["rows"] = [{**deepcopy(dataset["rows"][0]), "id": sid, "capture_batch": "B", "capture_file": "B/1.json"}
                           for sid in "abc"]
        annotations = {"rows": [{"id": r["id"], "gold": {"relevance": "primary", "tone": "neutral", "importance": "high"}}
                                for r in dataset["rows"]]}
        with TemporaryDirectory() as temp:
            path = Path(temp)/"ds.json"
            write_json(path, dataset_for())
            _, _, _, prices = make_plan(dataset_path=path, arms=[SPLIT_ARM])
        plan = {"split": "debug", "arms": [SPLIT_ARM], "call_limit": limit, "models": {"jev": "jev-latest", "deepseek": "deepseek-flash"}}
        first = response()
        first["answers"].pop("importance")
        if usage is not None:
            first["usage"] = usage
        items = [{"id": sid, "importance": "high"} for sid in "abc"] if ds_items is None else ds_items
        requested = [{"id": r["id"]} for r in dataset["rows"]]
        ds = {"model": "deepseek-flash", "usage": response()["usage"], **validate_batch_items(requested, items)}
        jev = Mock(classify=Mock(side_effect=jev_error, return_value=first))
        models = {"jev": jev, "deepseek": Mock(classify_importance_batch=Mock(side_effect=ds_error, return_value=ds))}
        return run_experiment(plan, dataset, annotations, prices, models, clock=clock), models

    def test_three_titles_cost_one_deepseek_request_with_shared_ids(self):
        report, models = self.run_batch()
        self.assertEqual(report["accounting"]["attempted"], 4)
        models["deepseek"].classify_importance_batch.assert_called_once()
        scored = report["metrics"][SPLIT_ARM]
        self.assertEqual(scored["provider_calls"], {"typesafe": 3, "deepseek": 1})
        self.assertEqual(scored["costs"]["deepseek"]["requests"], 1)
        self.assertEqual(scored["importance_batches"]["batches"][0]["size"], 3)
        self.assertEqual(scored["importance_batches"]["failed_items"], 0)
        self.assertEqual([r["results"][SPLIT_ARM]["question_status"]["importance"]["call_ids"] for r in report["rows"]], [[4]]*3)
        for call in models["jev"].classify.call_args_list:
            self.assertEqual(call.kwargs["selected_questions"], ("relevance", "tone"))
        for item in models["deepseek"].classify_importance_batch.call_args.args[0]:
            self.assertEqual(set(item), {"id", "title", "instrument_id", "company_name", "identity"})

    def test_partial_importance_keeps_two_valid_jev_questions_and_all_denominators(self):
        report, _ = self.run_batch(ds_items=[{"id": "a", "importance": "ordinary"}])
        scored = report["metrics"][SPLIT_ARM]
        self.assertEqual(scored["importance_batches"]["failed_items"], 2)
        self.assertEqual(scored["questions"]["tone"]["resolved_predictions"], 3)
        self.assertEqual(scored["questions"]["importance"]["resolved_predictions"], 1)
        self.assertEqual(scored["questions"]["importance"]["denominator"], 3)
        self.assertEqual(report["status"], "partial")

    def test_known_usage_jev_failure_keeps_importance_and_failures_count(self):
        report, _ = self.run_batch(jev_error=ClassifierError("invalid_response", metadata={"model": "jev-latest", "usage": response()["usage"]}))
        self.assertEqual(report["accounting"]["attempted"], 4)
        scored = report["metrics"][SPLIT_ARM]["questions"]
        self.assertEqual(scored["relevance"]["resolved_predictions"], 0)
        self.assertEqual(scored["importance"]["resolved_predictions"], 3)

    def test_entire_deepseek_failure_and_extra_items_are_explicit_without_erasing_jev(self):
        for kwargs in ({"ds_error": ClassifierError("invalid_response", metadata={"model": "deepseek-flash", "usage": response()["usage"]})},
                       {"ds_items": [{"id": sid, "importance": "high"} for sid in "abcx"]}):
            with self.subTest(kwargs=kwargs):
                report, _ = self.run_batch(**kwargs)
                scored = report["metrics"][SPLIT_ARM]
                self.assertEqual(scored["questions"]["tone"]["resolved_predictions"], 3)
                self.assertEqual(scored["provider_calls"]["deepseek"], 1)
                self.assertEqual(report["status"], "partial")
                if "ds_error" in kwargs:
                    self.assertEqual(scored["importance_batches"]["failed_items"], 3)
                else:
                    self.assertEqual(scored["importance_batches"]["extra_returned_items"], 1)
                    self.assertEqual(scored["questions"]["importance"]["resolved_predictions"], 3)

    def test_unknown_usage_and_global_budget_stop_paid_batches(self):
        for kwargs, attempts, reason in (({"limit": 2}, 2, None),
                                        ({"usage": {}}, 1, "usage_unknown_or_invalid"),
                                        ({"usage": {"prompt_tokens": 24000, "completion_tokens": 1, "total_tokens": 24001}}, 1, None)):
            with self.subTest(kwargs=kwargs):
                report, models = self.run_batch(**kwargs)
                self.assertEqual(report["accounting"]["attempted"], attempts)
                self.assertEqual(report["accounting"]["stop_reason"], reason)
                models["deepseek"].classify_importance_batch.assert_not_called()
                self.assertEqual(report["metrics"][SPLIT_ARM]["importance_batches"]["failed_items"], 3)

    def test_split_plan_is_opt_in_and_maximum_is_titles_plus_batches(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/"dataset.json"
            write_json(path, dataset_for())
            plan, *_ = make_plan(dataset_path=path, arms=[SPLIT_ARM])
            self.assertEqual(plan["providers"], ["jev", "deepseek"])
            self.assertEqual(plan["worst_case_calls"], 2)
            self.assertEqual(plan["importance_batch_sizes"], [1])
            default, *_ = make_plan(dataset_path=path)
            self.assertNotIn(SPLIT_ARM, default["arms"])
            self.assertEqual(default["worst_case_calls"], 6)

    def test_shared_batch_time_budget_stops_remaining_calls(self):
        now = [0]
        def clock():
            now[0] += 35
            return now[0]
        report, models = self.run_batch(clock=clock)
        self.assertEqual(report["accounting"]["attempted"], 1)
        models["deepseek"].classify_importance_batch.assert_not_called()
        self.assertEqual(report["metrics"][SPLIT_ARM]["importance_batches"]["batches"][0]["reason"], "batch_budget")
