from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.news_triage import questions as v1_questions
from trading_companion.news_triage_product import (
    OPTIONS, POLICY_RULES, descriptor, digest, filter_allowed, identity_precheck, input_state, normalized, questions, route_reasons,
)
from trading_companion.news_triage_product_models import ProductDeepSeek, ProductJev
from trading_companion.security_directory import Security
from trading_companion.typesafe_jev import ClassifierError, JevConfig
from tests.test_typesafe_jev import http_response


DIRECTORY = SimpleNamespace(securities=(Security("600000.SH", "浦发银行"), Security("600036.SH", "招商银行"),
                                      Security("600999.SH", "招商证券"), Security("000001.SZ", "平安银行")))


def sample(title="浦发银行公布季度业绩", *, day="2026-11-01", source="sina_company_news", issuer="600000.SH"):
    row = {"id": "P" + digest([title, day, source, issuer])[:20], "instrument_id": issuer,
           "company_name": next(s.name for s in DIRECTORY.securities if s.instrument_id == issuer),
           "kind": "news" if source == "sina_company_news" else "announcements", "source": source,
           "title": title, "normalized_title": normalized(title), "published_date": day,
           "url": "https://example.invalid/" + digest([title, source]),
           "capture_identity_checked": True}
    row["identity"] = identity_precheck(row, DIRECTORY)
    return row


def response(*, relevance="primary", tone="neutral", importance="high", confidence=.8, model="jev-latest", usage=None):
    labels = {"relevance": relevance, "tone": tone, "importance": importance}
    return {"model": model, "usage": usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "answers": {q: {"choice": value, "confidence": confidence, "probabilities": None} for q, value in labels.items()}}


class ProductTaskTests(unittest.TestCase):
    def test_v2_is_independent_and_versioned(self):
        self.assertEqual(set(questions()), {"relevance", "tone", "importance"})
        self.assertIn("event_type", v1_questions())
        self.assertEqual(descriptor()["task_version"], "news-triage-product-v2")
        self.assertEqual(descriptor()["rule_version"], "news-triage-product-rules-v4")
        self.assertEqual(descriptor(), json.loads((Path(__file__).resolve().parents[1] /
                         "data/evaluation/news-triage-product-v2.rules.json").read_text("utf-8")))

    def test_user_rules_reach_all_three_questions_with_small_move_precedence(self):
        self.assertEqual(len(POLICY_RULES), 6)
        for question in questions().values():
            self.assertTrue(all(rule in question["instructions"] for rule in POLICY_RULES))
        self.assertIn("不到1%", questions()["tone"]["criteria"]["neutral"])
        self.assertIn("规则3优先", POLICY_RULES[5])
        self.assertIn("行业对比、排名", questions()["importance"]["criteria"]["ordinary"])

    def test_verified_unnamed_announcement_is_preserved(self):
        row = sample("关于召开股东大会的通知", source="eastmoney_announcements")
        self.assertTrue(row["identity"]["issuer_verified"])
        self.assertFalse(row["identity"]["name_evidence"])
        result = {"status": "ok", **response(relevance="unrelated")}
        self.assertFalse(filter_allowed(row["identity"], result, deepseek_reviewed=True))
        self.assertIn("verified_issuer_conflict", route_reasons(row["identity"], result, .65))

    def test_name_substring_in_longer_institution_is_only_suspicion(self):
        row = sample("浦发银行行业协会召开会议")
        check = row["identity"]
        self.assertTrue(check["collision_suspected"])
        self.assertFalse(check["credible_target_evidence"])
        self.assertEqual(check["mismatch_basis"], ["extended_entity_name"])
        partner = sample("浦发银行与浦发银行行业协会开展合作")["identity"]
        self.assertTrue(partner["credible_target_evidence"])
        self.assertFalse(partner["mismatch_basis"])

    def test_number_context_market_and_digit_boundaries(self):
        for title in ("产品编号600000发行", "信托计划（600000）到期"):
            check = sample(title)["identity"]
            self.assertTrue(check["number_collision"])
            self.assertFalse(check["credible_target_evidence"])
        self.assertFalse(sample("项目16000001到期")["identity"]["numeric_match"])
        self.assertFalse(sample("600000发布信息")["identity"]["security_code_evidence"])
        self.assertTrue(sample("股票代码：600000公布报告")["identity"]["security_code_evidence"])
        self.assertTrue(sample("600000.SH公布报告")["identity"]["security_code_evidence"])
        self.assertTrue(sample("600000.SZ公布报告")["identity"]["collision_suspected"])

    def test_trusted_aliases_only_and_multi_subject_forces_review(self):
        with self.assertRaises(ValueError):
            identity_precheck({**sample(), "company_name": "模型猜测名称"}, DIRECTORY)
        row = sample("浦发银行、招商银行集体上涨，银行ETF大涨")
        self.assertTrue(row["identity"]["multi_subject"])
        result = {"status": "ok", **response(confidence=.99)}
        reasons = route_reasons(row["identity"], result, .65)
        self.assertIn("multi_subject", reasons)
        self.assertIn("industry_title", reasons)
        row["gold"] = "never_send_this"
        self.assertEqual(set(input_state(row)), {"title", "company_name", "instrument_id", "identity"})

    def test_abandoned_filter_retains_even_reviewed_mismatches(self):
        check = sample("产品编号600000发行")["identity"]
        result = {"status": "ok", **response(relevance="unrelated")}
        self.assertFalse(filter_allowed(check, result, deepseek_reviewed=False))
        self.assertFalse(filter_allowed(check, result, deepseek_reviewed=True))
        self.assertFalse(filter_allowed(sample()["identity"], result, deepseek_reviewed=True))


class ProductAdaptersTests(unittest.TestCase):
    def test_deepseek_abstains_without_inventing_probabilities(self):
        labels = {"relevance": "unresolved", "tone": "neutral", "importance": "unknown"}
        payload = {"model": "served-model", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(labels)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        post = Mock(return_value=http_response(payload))
        result = ProductDeepSeek(DeepSeekConfig("requested", "test"), post=post).classify(input_state(sample()))
        self.assertEqual(result["answers"]["relevance"]["choice"], "unresolved")
        self.assertTrue(all(a["confidence"] is None and a["probabilities"] is None for a in result["answers"].values()))
        body = post.call_args.kwargs["json"]
        self.assertIn(json.dumps(questions(), ensure_ascii=False), body["messages"][0]["content"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(json.loads(body["messages"][-1]["content"]), input_state(sample()))
        for bad in ({**labels, "confidence": .9}, {**labels, "importance": "buy"},
                    {**labels, "relevance": "unrelated", "tone": "negative"}):
            payload["choices"][0]["message"]["content"] = json.dumps(bad)
            with self.subTest(bad=bad), self.assertRaises(ClassifierError) as error:
                ProductDeepSeek(DeepSeekConfig("requested", "test"), post=Mock(return_value=http_response(payload))).classify({})
            self.assertEqual(error.exception.metadata["usage"]["total_tokens"], 15)

    def test_jev_uses_three_choices_without_fabricating_abstention_probability(self):
        answers = {q: {"type": "choice", "choice": next(iter(options)), "confidence": .8,
                       "probabilities": {v: .8 if i == 0 else .1 for i, v in enumerate(options)}} for q, options in OPTIONS.items()}
        post = Mock(return_value=http_response({"model": "jev-1.14.0", "answers": answers,
                                               "usage": {"input_tokens": 10, "output_tokens": 5}}))
        result = ProductJev(JevConfig("test"), post=post).classify(input_state(sample()))
        self.assertEqual(result["model"], "jev-1.14.0")
        self.assertEqual(post.call_args.kwargs["json"]["questions"], questions())
        self.assertNotIn("unresolved", post.call_args.kwargs["json"]["questions"]["relevance"]["criteria"])
