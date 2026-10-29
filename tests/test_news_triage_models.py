from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from trading_companion.deepseek_model import DeepSeekConfig, DEEPSEEK_CHAT_URL
from trading_companion.news_triage import input_state, load_corpus, questions
from trading_companion.news_triage_models import DeepSeekTriage, JevTriage, fixture_post
from trading_companion.typesafe_jev import ClassifierError, JevConfig
from tests.test_typesafe_jev import http_response


class TriageModelTests(unittest.TestCase):
    def test_both_adapters_consume_exact_same_state_and_criteria(self):
        items, _ = load_corpus()
        state = input_state(items[0])
        for provider, cls, config in (("jev", JevTriage, JevConfig("test")),
                                     ("deepseek", DeepSeekTriage, DeepSeekConfig("test-model", "test"))):
            post = Mock(side_effect=fixture_post(provider))
            result = cls(config, post=post).classify(state)
            body = post.call_args.kwargs["json"]
            self.assertEqual(set(result["answers"]), set(questions()))
            if provider == "deepseek":
                self.assertEqual(json.loads(body["messages"][-1]["content"]), state)
                self.assertEqual(body["temperature"], 0)
                self.assertEqual(body["response_format"], {"type":"json_object"})
                self.assertIn(json.dumps(questions(), ensure_ascii=False), body["messages"][0]["content"])
                self.assertTrue(all(r["confidence"] is None for r in result["answers"].values()))
                self.assertEqual(post.call_args.args[0], DEEPSEEK_CHAT_URL)
            else:
                self.assertEqual(body["state"], state)
                self.assertEqual(body["questions"], questions())

    def test_deepseek_rejects_malformed_labels_and_does_not_invent_confidence(self):
        good = {"relevance":"primary","event_type":"capital","tone":"neutral"}
        for content in ("```json\n{}\n```", json.dumps({**good,"confidence":.9}), json.dumps({**good,"tone":"buy"}),
                        '{"relevance":"primary","relevance":"mentioned","event_type":"capital","tone":"neutral"}'):
            payload = {"model":"served-model", "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":content}}],
                       "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}
            post = Mock(return_value=http_response(payload))
            with self.subTest(content=content), self.assertRaises(ClassifierError) as context:
                DeepSeekTriage(DeepSeekConfig("requested-model","test"),post=post).classify({})
            self.assertEqual(context.exception.metadata["usage"]["total_tokens"],15)
            self.assertEqual(context.exception.metadata["model"],"served-model")
            post.assert_called_once()

    def test_fixture_does_not_depend_on_gold(self):
        items, _ = load_corpus()
        for provider, model in (("deepseek", DeepSeekTriage(DeepSeekConfig("test-model","test"),post=fixture_post("deepseek"))),
                                ("jev", JevTriage(JevConfig("test"),post=fixture_post("jev")))):
            with self.subTest(provider=provider):
                item = deepcopy(items[0])
                before = model.classify(input_state(item))
                item["gold"] = {"relevance":"unrelated","event_type":"other","tone":"negative"}
                item["reason"] = "changed annotation must not enter fixture input"
                after = model.classify(input_state(item))
                self.assertEqual(before,after)
