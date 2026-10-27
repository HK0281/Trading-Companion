"""Displayed-list identity, lifetime and complete selection contracts, without models or IO."""

from dataclasses import replace
from datetime import timedelta
import unittest

from trading_companion.errors import DataValidationError
from trading_companion.research_session import ResearchSession
from trading_companion.screening import ScreeningRequest, ScreeningService, demo_screening_snapshot
from trading_companion.screening_context import SavedScreening, is_screening_followup, select_screening
from trading_companion.security_directory import Security, SecurityDirectory
from tests.test_product_research import NOW


class ScreeningContextTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = demo_screening_snapshot(clock=lambda: NOW)
        self.directory = self.snapshot.directory
        self.source = object()
        self.result = ScreeningService(clock=lambda: NOW).screen(self.snapshot, ScreeningRequest.from_dict({
            "conditions": [{"field": "price", "operator": "gt", "value": 0}], "limit": 5}))
        self.saved = SavedScreening.checked(self.result, directory=self.directory, quote_source=self.source, now=NOW)

    def select(self, question, **changes):
        options = dict(saved=self.saved, directory=self.directory, quote_source=self.source,
                       source="tencent", development=True, now=NOW)
        options.update(changes)
        return select_screening(question, **options)

    def test_display_order_and_original_qualifiers_are_bound_to_immutable_identities(self):
        self.result.rows[0]["instrument_id"] = "000001.SZ"
        self.result.rows[0]["name"] = "公开字典被改动"
        selection = self.select("第一只只看新闻，不要技术指标，未来一个月关注什么？")
        self.assertTrue(selection.ready)
        self.assertEqual([item.instrument_id for item in selection.securities], ["920000.BJ"])
        self.assertEqual(selection.effective_question,
                         "分析920000.BJ只看新闻,不要技术指标,未来一个月关注什么")

    def test_selectors_names_codes_and_nonadjacent_ordinals_preserve_chosen_order(self):
        for text, expected in (
            ("分析第一只的基本面", ("920000.BJ",)),
            ("比较前两只的营收增长", ("920000.BJ", "300001.SZ")),
            ("研究第1和第3只的新闻", ("920000.BJ", "688981.SH")),
            ("名单中第二只", ("300001.SZ",)),
            ("分析安徽凤凰和特锐德的风险", ("920000.BJ", "300001.SZ")),
            ("研究300001和920000.BJ的公告", ("300001.SZ", "920000.BJ")),
        ):
            with self.subTest(text=text):
                selection = self.select(text)
                self.assertTrue(selection.ready, selection.message)
                self.assertEqual(tuple(item.instrument_id for item in selection.securities), expected)

    def test_ambiguous_overflow_repeated_and_unconsumed_selectors_are_never_truncated(self):
        for text in ("它的基本面呢", "这两只的新闻", "分析第六只", "比较前四只", "分析全部",
                     "分析第1和第1只", "第一只或第二只", "第一只和不存在公司", "第一只不要第二只",
                     "第二只并且分析垃圾股票", "第一只,垃圾股票的基本面", "第一只并且分析其他公司"):
            with self.subTest(text=text):
                self.assertFalse(self.select(text).ready)

    def test_singleton_and_exact_plural_are_unique_without_guessing_the_first_of_many(self):
        one = replace(self.saved, securities=self.saved.securities[:1])
        self.assertEqual(self.select("它的新闻呢", saved=one).securities, one.securities)
        two = replace(self.saved, securities=self.saved.securities[:2])
        self.assertEqual(self.select("比较这两只的基本面", saved=two).securities, two.securities)

    def test_age_boundary_backward_clock_and_configuration_changes_invalidate(self):
        self.assertTrue(self.select("第一只", now=NOW + timedelta(minutes=15, microseconds=-1)).ready)
        for changes, code in (
            ({"now": NOW + timedelta(minutes=15)}, "screening_list_expired"),
            ({"now": NOW - timedelta(microseconds=1)}, "screening_list_expired"),
            ({"quote_source": object()}, "screening_list_configuration_changed"),
            ({"source": "akshare_eastmoney"}, "screening_list_configuration_changed"),
            ({"development": False}, "screening_list_configuration_changed"),
        ):
            with self.subTest(changes=changes):
                selection = self.select("第一只", **changes)
                self.assertFalse(selection.ready)
                self.assertEqual(selection.code, code)
                self.assertTrue(selection.invalidate)

    def test_changed_identity_and_stale_directory_block_even_when_only_another_row_is_selected(self):
        altered = replace(self.directory, securities=tuple(replace(item, name="新名称") if item.instrument_id == "000001.SZ"
                                                          else item for item in self.directory.securities))
        self.assertEqual(self.select("第一只", directory=altered).code, "screening_list_identity_changed")
        stale = replace(self.directory, retrieved_at=NOW - timedelta(days=8))
        self.assertEqual(self.select("第一只", directory=stale).code, "screening_list_directory_unusable")

    def test_only_displayed_rows_are_available_and_empty_lists_cannot_select(self):
        two = replace(self.saved, securities=self.saved.securities[:2])
        self.assertFalse(self.select("第三只", saved=two).ready)
        self.assertEqual(self.select("第一只", saved=replace(self.saved, securities=())).code, "screening_list_empty")
        self.assertEqual(self.select("第一只", saved=None).code, "screening_list_unavailable")

    def test_context_cannot_be_created_from_changed_order_or_mixed_authenticity(self):
        with self.assertRaises(DataValidationError):
            SavedScreening.checked(replace(self.result, rows=tuple(reversed(self.result.rows))),
                                  directory=self.directory, quote_source=self.source, now=NOW)
        real_directory = SecurityDirectory(self.directory.securities, "offline_real_directory", NOW)
        with self.assertRaises(DataValidationError):
            SavedScreening.checked(self.result, directory=real_directory, quote_source=self.source, now=NOW)

    def test_fork_only_publishes_new_lists_explicitly_and_consumption_is_immediate(self):
        session = ResearchSession()
        session.screening_saved = self.saved
        working = session._fork_for_turn()
        replacement = replace(self.saved, saved_at=NOW + timedelta(seconds=1))
        working.screening_saved = replacement
        self.assertIs(session.screening_saved, self.saved)
        session._publish_turn(working)
        self.assertIs(session.screening_saved, replacement)
        working.begin_research(preserve_screening=True)
        self.assertIs(session.screening_saved, replacement)
        working.begin_research()
        self.assertIsNone(session.screening_saved)
        session.screening_saved = self.saved
        session.clear()
        self.assertIsNone(session.screening_saved)

    def test_explicit_ordinals_route_even_without_a_list_but_pronouns_respect_current_subject(self):
        session = ResearchSession()
        self.assertTrue(is_screening_followup("分析第一只", session=session, directory=self.directory))
        session.screening_saved = self.saved
        self.assertTrue(is_screening_followup("它的基本面呢", session=session, directory=self.directory))
        session.product_saved = object()
        self.assertFalse(is_screening_followup("它的基本面呢", session=session, directory=self.directory))
        self.assertTrue(is_screening_followup("名单中第二只", session=session, directory=self.directory))

    def test_ordinals_inside_research_scope_do_not_become_shortlist_subjects(self):
        session = ResearchSession()
        for saved in (None, self.saved):
            session.screening_saved = saved
            for question in ("分析浦发银行上市后第一个交易日以来的走势", "分析浦发银行第三个季度的基本面",
                             "分析第一个交易日的规则", "分析第三个季度的财务特点"):
                with self.subTest(saved=saved is not None, question=question):
                    self.assertFalse(is_screening_followup(question, session=session, directory=self.directory))
            for question in ("分析第一只的基本面", "比较前两只的营收增长", "第一个",
                             "分析筛选结果中的第一只", "名单中第二只的新闻"):
                with self.subTest(saved=saved is not None, question=question):
                    self.assertTrue(is_screening_followup(question, session=session, directory=self.directory))

    def test_pronouns_need_a_stock_or_research_boundary_to_refer_to_the_list(self):
        session = ResearchSession()
        session.screening_saved = self.saved
        for question in ("全部A股的风险", "这些行业的新闻", "全部市场的基本面", "这些概念如何影响公司"):
            with self.subTest(question=question):
                self.assertFalse(is_screening_followup(question, session=session, directory=self.directory))
        for question in ("全部", "这些股票的基本面", "这些只看新闻", "这些的公告", "比较这两只的基本面"):
            with self.subTest(question=question):
                self.assertTrue(is_screening_followup(question, session=session, directory=self.directory))

    def test_ordinal_research_scope_after_a_selected_identity_is_preserved(self):
        selection = self.select("分析第一只第三个季度的基本面")
        self.assertTrue(selection.ready, selection.message)
        self.assertEqual(selection.effective_question, "分析920000.BJ第三个季度的基本面")
