"""Grounded scope constraints override model hints without guessing arbitrary negation."""

from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from trading_companion.product_plan import (
    RESEARCH_KINDS, is_explicit_technical_request, prepare_product_plan, wants_product_research,
)
from trading_companion.product_scope import document_request_scope, has_company_domain
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

    def test_document_types_share_positive_domains_without_generic_content_authority(self):
        for phrase in ("临时股东会通知", "董事会决议", "股东会议案", "投资者关系活动记录表", "调研记录",
                       "会议记录", "交流纪要", "业绩说明会", "董事会工作报告", "监事会年度工作报告"):
            question = "平安银行最近的" + phrase + "具体说了什么？"
            with self.subTest(phrase=phrase):
                positive, excluded, kinds = document_request_scope(question)
                self.assertEqual(positive, question)
                self.assertEqual(excluded, frozenset())
                self.assertEqual(kinds, frozenset(("announcements",)))
                self.assertTrue(has_company_domain(question))
        for question in ("具体内容", "报告说了什么", "记录讲了什么", "记录平安银行的SMA20参数",
                         "分析平安银行的交易记录", "平安银行财务报告的具体内容", "平安银行技术报告讲了什么",
                         "通知我平安银行的价格", "不要通知我", "通知设置"):
            with self.subTest(question=question):
                self.assertEqual(document_request_scope(question)[2], frozenset())

    def test_document_negations_share_front_and_local_postfix_masks(self):
        for phrase in ("不看通知", "不要读投资者关系活动记录", "无需阅读董事会工作报告", "别展开纪要",
                       "不需要读取说明会", "公告内容不用看", "纪要不看", "投资者关系活动记录的内容不要读"):
            with self.subTest(phrase=phrase):
                _, excluded, kinds = document_request_scope("分析平安银行，" + phrase)
                self.assertEqual(excluded, frozenset(("announcements",)))
                self.assertEqual(kinds, frozenset())
                plan, _ = self.prepare("分析平安银行，" + phrase)
                self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial", "news", "technical")))

    def test_document_alias_lists_stop_before_a_positive_clause(self):
        _, excluded, kinds = document_request_scope("不要新闻和通知以及会议记录，公告内容值得关注")
        self.assertEqual(excluded, frozenset(("news", "announcements")))
        self.assertEqual(kinds, frozenset())
        plan, _ = self.prepare("平安银行只看技术，不用看公告内容")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("technical",)))
        plan, _ = self.prepare("只看平安银行的财务报告具体内容，不看会议纪要")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial")))

    def test_negated_document_only_never_becomes_a_document_exclusion(self):
        question = "分析平安银行，不要只看会议纪要"
        _, excluded, kinds = document_request_scope(question)
        self.assertEqual((excluded, kinds), (frozenset(), frozenset()))
        plan, _ = self.prepare(question, kinds=("announcements",))
        self.assertEqual((plan.status, plan.kinds), ("ready", RESEARCH_KINDS))
        plan, _ = self.prepare("不要只看会议纪要，只看平安银行的财务")
        self.assertEqual((plan.status, plan.kinds), ("ready", ("profile", "financial")))

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
