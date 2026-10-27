"""Independent range boundaries and exact, complete condition mutations."""

from dataclasses import replace
import unittest

from trading_companion.screening import ScreeningCondition as C, ScreeningRequest
from trading_companion.screening_intent import checked_screening_request, edited_screening_request, is_screening_edit
from trading_companion.research_session import ResearchSession
from tests import test_screening_agent as fixture

Model = fixture.Model


def atoms(phrase, field, *boundaries):
    return [{"field": field, "operator": operator, "value": value, "phrase": phrase}
            for operator, value in boundaries]


class ScreeningIntentTests(unittest.TestCase):
    def checked(self, question, items, **choices):
        return checked_screening_request(question, Model(items, **choices).payload)[0]

    def test_edit_routing_requires_a_field_in_the_anchored_mutation_structure(self):
        session = ResearchSession()
        for question in ("再加换手率大于2%", "把PE改为低于15", "按市净率升序选3只",
                         "新增PE在10到20之间", "再加10 < PE <= 20", "按市净率排序选3只",
                         "在原筛选条件上再加换手率大于2%", "再加PE低于20且新闻利好"):
            with self.subTest(question=question):
                self.assertTrue(is_screening_edit(question, session=session))
        for question in ("按市盈率分析浦发银行的估值", "把浦发银行和平安银行的市盈率比较一下",
                         "把第一只和第二只比较一下", "按基本面分析第一只", "再加RSI", "增加RSI3",
                         "再加浦发银行的PE研究", "把PE比较一下", "再加PE低于", "按市净率"):
            with self.subTest(question=question):
                self.assertFalse(is_screening_edit(question, session=session))

    def test_common_intervals_are_inclusive_and_two_atoms_are_visible(self):
        for phrase in ("PE在10到20之间", "PE介于10和20之间", "动态市盈率在10至20之间"):
            with self.subTest(phrase=phrase):
                model = Model(atoms(phrase, "pe_dynamic", ("gte", 10), ("lte", 20)))
                # Use the real controlled entry, not just a parser assertion.
                result, source, _ = fixture.ScreeningAgentTests().run_turn("筛选" + phrase + "的股票", model)
                self.assertEqual(result.status, "completed", result.code)
                self.assertEqual(source.calls, 1)
                self.assertEqual(result.screening_result.request.conditions, (C("pe_dynamic", "gte", 10), C("pe_dynamic", "lte", 20)))
                self.assertIn("动态市盈率≥10倍", result.display_answer)
                self.assertIn("动态市盈率≤20倍", result.display_answer)

    def test_symbolic_and_shared_field_boundaries_preserve_each_operator(self):
        cases = (("10 < PE <= 20", (("gt", 10), ("lte", 20))),
                 ("20 >= PE > 10", (("lte", 20), ("gt", 10))),
                 ("PE不低于10且不高于20", (("gte", 10), ("lte", 20))),
                 ("PE在-10到-2之间", (("gte", -10), ("lte", -2))))
        for phrase, bounds in cases:
            with self.subTest(phrase=phrase):
                request = self.checked("筛选" + phrase, atoms(phrase, "pe_dynamic", *bounds))
                self.assertEqual(request.conditions, tuple(C("pe_dynamic", op, value) for op, value in bounds))

    def test_numeric_execution_obeys_open_and_closed_boundaries(self):
        from trading_companion.screening import ScreeningService, demo_screening_snapshot
        from tests.test_screening_agent import NOW
        snapshot = demo_screening_snapshot(clock=lambda: NOW)
        quotes = tuple(replace(quote, pe_dynamic=value) for quote, value in zip(snapshot.quotes, (10, 15, 20, 9, 21)))
        snapshot = replace(snapshot, quotes=quotes)
        for phrase, bounds, expected in (
                ("PE在10到20之间", (("gte", 10), ("lte", 20)), {"000001.SZ", "600010.SH", "688981.SH"}),
                ("10 < PE <= 20", (("gt", 10), ("lte", 20)), {"600010.SH", "688981.SH"})):
            with self.subTest(phrase=phrase):
                request = self.checked("筛选" + phrase, atoms(phrase, "pe_dynamic", *bounds))
                result = ScreeningService(clock=lambda: NOW).screen(snapshot, request)
                self.assertEqual({row["instrument_id"] for row in result.rows}, expected)

    def test_units_are_bound_to_each_numeric_endpoint(self):
        for phrase, field, bounds in (
                ("成交额在1亿到2亿元之间", "amount_cny", (("gte", 100_000_000), ("lte", 200_000_000))),
                ("换手率在2到5%之间", "turnover_percent", (("gte", 2), ("lte", 5))),
                ("价格在10元到20元之间", "price", (("gte", 10), ("lte", 20)))):
            with self.subTest(phrase=phrase):
                request = self.checked("筛选" + phrase, atoms(phrase, field, *bounds))
                self.assertEqual(request.conditions, tuple(C(field, op, value) for op, value in bounds))
        # Decimal scale conversion must not invent a binary floating point delta.
        request = self.checked("请筛选出成交额大于0.07亿元的股票，按市净率升序排序选3只",
            atoms("成交额大于0.07亿元", "amount_cny", ("gt", 7_000_000)), sort_by="pb", sort_direction="asc", limit=3)
        self.assertEqual(request.conditions, (C("amount_cny", "gt", 7_000_000),))

    def test_six_digit_interval_values_are_thresholds_and_tickers_stay_unsupported(self):
        phrase = "成交额在100000到200000之间"
        result, source, _ = fixture.ScreeningAgentTests().run_turn("筛选" + phrase,
            Model(atoms(phrase, "amount_cny", ("gte", 100000), ("lte", 200000))))
        self.assertEqual(source.calls, 1)
        self.assertIsNotNone(result.screening_result)
        result, source, _ = fixture.ScreeningAgentTests().run_turn("筛选000001.SZ且PE低于20",
            Model(atoms("PE低于20", "pe_dynamic", ("lt", 20))))
        self.assertEqual((result.code, source.calls), ("screening_universe_not_supported", 0))

    def test_separator_whitespace_is_not_required_in_the_copied_condition_phrase(self):
        for phrase, bounds in (("PE低于20", (("lt", 20),)),
                               ("10 < PE <= 20", (("gt", 10), ("lte", 20)))):
            with self.subTest(phrase=phrase):
                items = atoms(phrase, "pe_dynamic", *bounds) + atoms("换手率大于2%", "turnover_percent", ("gt", 2))
                result, source, _ = fixture.ScreeningAgentTests().run_turn(
                    "筛选" + phrase + " 且 换手率大于2% ，按成交额降序选5只", Model(items))
                self.assertEqual((result.status, source.calls), ("completed", 1))

    def test_changed_missing_duplicate_or_fabricated_boundaries_fail_before_quotes(self):
        phrase = "PE在10到20之间"
        correct = atoms(phrase, "pe_dynamic", ("gte", 10), ("lte", 20))
        for items in (correct[:1], correct + correct[:1],
                      [correct[0], {**correct[1], "value": 21}],
                      [{**correct[0], "operator": "gt"}, correct[1]],
                      [correct[0], {**correct[1], "phrase": "PE不高于20"}],
                      [correct[0], {**correct[1], "field": "pb"}]):
            with self.subTest(items=items):
                result, source, _ = fixture.ScreeningAgentTests().run_turn("筛选" + phrase, Model(items))
                self.assertEqual((result.code, source.calls), ("screening_intent_invalid", 0))

    def test_unknown_clauses_ambiguous_ranges_or_wrong_units_are_not_dropped(self):
        original = ScreeningRequest((C("price", "gt", 0),))
        for phrase in ("PE在20到10之间", "PE在10到20以上", "PE在10到20之间且新闻利好",
                       "PE在10到20之间或PB小于1", "PE在10到20元之间", "换手率在1万到2万之间",
                       "成交额在1到2亿之间", "PE在10到20之间且", "10 < PE > 20",
                       "PE低于20且盈利稳定", "PE低于20且静态市盈率大于1", "股价低于10美元"):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                edited_screening_request("再加" + phrase, original)

    def test_replace_removes_both_old_bounds_and_preserves_other_settings(self):
        original = ScreeningRequest((C("price", "gt", 0), C("pe_dynamic", "gte", 10),
            C("pe_dynamic", "lte", 20), C("turnover_percent", "gt", 2)),
            sort_by="pb", sort_direction="asc", limit=3, max_quote_age_days=2)
        request = edited_screening_request("把PE改为低于15", original)
        self.assertEqual(request, replace(original, conditions=(C("price", "gt", 0), C("pe_dynamic", "lt", 15), C("turnover_percent", "gt", 2))))
        ranged = edited_screening_request("把PE改为在-5到12之间", request)
        self.assertEqual([item for item in ranged.conditions if item.field == "pe_dynamic"], [C("pe_dynamic", "gte", -5), C("pe_dynamic", "lte", 12)])

    def test_add_and_sort_keep_unmentioned_conditions_and_count(self):
        original = ScreeningRequest((C("pe_dynamic", "lt", 20),), limit=7, max_quote_age_days=2)
        added = edited_screening_request("再加换手率大于2%", original)
        self.assertEqual(added, replace(original, conditions=original.conditions + (C("turnover_percent", "gt", 2),)))
        sorted_request = edited_screening_request("按市净率升序选3只", added)
        self.assertEqual(sorted_request, replace(added, sort_by="pb", sort_direction="asc", limit=3))
        self.assertEqual(edited_screening_request("按股价降序", sorted_request).limit, 3)

    def test_conflicts_noops_unknown_suffixes_and_twelve_atom_limit_are_rejected(self):
        original = ScreeningRequest((C("pe_dynamic", "gt", 10), C("pe_dynamic", "lte", 20)))
        for question in ("再加PE大于10", "再加PE低于10", "再加PE等于25", "把PB改为低于1",
                         "把PE改为大于10且不高于20", "按成交额降序选10只",
                         "按市净率升序选21只", "按市净率升序选三只", "按市净率升序再按股价降序",
                         "把PE改为低于15并换手率大于2%", "再加PB低于1并自动买入", "再加PE低于20选3只"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                edited_screening_request(question, original)
        eleven = ScreeningRequest(tuple(C("price", "gt", index) for index in range(11)))
        twelve = edited_screening_request("再加PE低于20", eleven)
        self.assertEqual(len(twelve.conditions), 12)
        with self.assertRaises(ValueError):
            edited_screening_request("再加PB低于1", twelve)

    def test_equal_endpoint_is_allowed_only_when_both_are_closed(self):
        original = ScreeningRequest((C("price", "gt", 0),))
        closed = edited_screening_request("再加PE在10到10之间", original)
        self.assertEqual(closed.conditions[-2:], (C("pe_dynamic", "gte", 10), C("pe_dynamic", "lte", 10)))
        for question in ("再加10 < PE <= 10", "再加PE大于10且低于5", "再加PE等于10且低于10"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                edited_screening_request(question, original)
