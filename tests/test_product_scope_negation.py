"""Grounded scope constraints override model hints without guessing arbitrary negation."""

from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from trading_companion.product_plan import (
    RESEARCH_KINDS, is_explicit_technical_request, prepare_product_plan, wants_product_research,
)
from tests.test_product_plan import NOW, REFERENCE, _arguments, _directory, _response


class ProductScopeNegationTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("No network in scope planning"))
        guard.start()
        self.addCleanup(guard.stop)

    def prepare(self, question, *, kinds=RESEARCH_KINDS, mentions=("平安银行",), context=None):
        model = Mock()
        model.complete.return_value = _response(_arguments(list(mentions), list(kinds)))
        plan = prepare_product_plan(question, model=model, directory=_directory(), reference_date=REFERENCE,
            now=NOW + timedelta(minutes=1) if context else NOW, source="csv", development=True, context=context)
        return plan, model

    def test_negated_only_preserves_broad_analysis_instead_of_forcing_news(self):
        question = "分析平安银行，不要只看新闻"
        plan, model = self.prepare(question, kinds=("news",))
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.kinds, RESEARCH_KINDS)
        self.assertFalse(plan.wants_risk)
        self.assertEqual(plan.model_calls_attempted, 1)
        model.complete.assert_called_once()
        self.assertEqual(model.complete.call_args.args[0][1].content, question)

    def test_negative_conjunction_excludes_both_domains_even_if_model_readds_them(self):
        plan, _ = self.prepare("分析平安银行，不要新闻和公告")
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.kinds, ("profile", "financial", "technical"))

    def test_positive_only_and_negative_list_keep_only_requested_fundamentals(self):
        plan, _ = self.prepare("只看平安银行的基本面，不要技术面和新闻")
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.kinds, ("profile", "financial"))

    def test_known_list_separators_are_supported_without_consuming_another_clause(self):
        for separator in ("和", "与", "及", "以及", "、", "或", " / "):
            with self.subTest(separator=separator):
                plan, _ = self.prepare("分析平安银行，不要新闻" + separator + "公告")
                self.assertEqual(plan.kinds, ("profile", "financial", "technical"))

    def test_known_negative_prefixes_apply_to_the_whole_domain_list(self):
        for prefix in ("不需要看", "无需研究", "不用考虑", "不必查询", "不要", "不看"):
            with self.subTest(prefix=prefix):
                plan, _ = self.prepare("分析平安银行，" + prefix + "新闻和公告")
                self.assertEqual(plan.kinds, ("profile", "financial", "technical"))

    def test_negated_only_is_broad_for_technical_and_named_owner_phrases_too(self):
        for phrase in ("不要只看技术面", "不只看技术面", "不要仅看技术面", "不要只看平安银行的新闻"):
            with self.subTest(phrase=phrase):
                question = "分析平安银行，" + phrase
                self.assertTrue(wants_product_research(question))
                self.assertFalse(is_explicit_technical_request(question))
                plan, _ = self.prepare(question, kinds=("news",))
                self.assertEqual((plan.status, plan.kinds), ("ready", RESEARCH_KINDS))

    def test_real_only_clause_remains_effective_after_a_negated_only_clause(self):
        plan, _ = self.prepare("不要只看新闻，只看平安银行的基本面")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial")))

    def test_negated_only_does_not_undo_a_separate_explicit_exclusion(self):
        plan, _ = self.prepare("分析平安银行，不要只看新闻，不要公告和技术面")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial", "news")))

    def test_list_stops_before_a_positive_clause(self):
        plan, _ = self.prepare("只看平安银行的基本面，不要新闻和公告，但结合技术面")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial", "technical")))
        plan, _ = self.prepare("分析平安银行，不要新闻，公告值得关注", kinds=())
        self.assertEqual((plan.status, plan.kinds), ("ready", ("announcements",)))

    def test_excluded_risk_in_a_list_cannot_enable_price_scenario(self):
        plan, _ = self.prepare("只看平安银行的新闻，不要公告和止盈止损")
        self.assertEqual((plan.status, plan.kinds, plan.wants_risk), ("ready", ("news",), False))

    def test_excluding_every_registered_domain_requires_clarification(self):
        plan, _ = self.prepare("分析平安银行，不要基本面和新闻和公告和技术面")
        self.assertEqual((plan.status, plan.code), ("needs_clarification", "product_no_research_domain"))
        self.assertEqual(plan.kinds, ())

    def test_identity_clarification_preserves_the_filtered_scope_without_another_call(self):
        pending, _ = self.prepare("只看平安的基本面，不要技术面和新闻", mentions=("平安",))
        self.assertEqual(pending.status, "needs_clarification")
        self.assertEqual(pending.context.kinds, ("profile", "financial"))
        result, model = self.prepare("平安银行", context=pending.context)
        self.assertEqual((result.status, result.kinds), ("ready", ("profile", "financial")))
        model.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
