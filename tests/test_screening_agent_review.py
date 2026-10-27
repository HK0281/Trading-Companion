"""Adversarial screening regressions: wording must constrain the executed request."""

from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from trading_companion.api_service import ResearchApiService, public_turn
from trading_companion.research_session import ResearchSession
from trading_companion.screening import demo_screening_snapshot, render_screening_result
from trading_companion.screening_agent import run_screening_turn
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_product_modules_api import ModulesModel
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory
from tests.test_screening_agent import Model


PE = {"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "PE低于20"}


class ReviewSource:
    def __init__(self, *, callback=None):
        self.snapshot = demo_screening_snapshot(clock=lambda: NOW)
        self.calls, self.callback = 0, callback

    def read_snapshot(self, directory):
        self.calls += 1
        if self.callback:
            self.callback()
        return self.snapshot


class ScreeningAgentReviewTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No live requests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def run_turn(self, question, model, *, source=None, control=None, clock=None, selected_directory=None):
        source = ReviewSource() if source is None else source
        result = run_screening_turn(question, session=ResearchSession(), model=model,
            directory=selected_directory or source.snapshot.directory, source=source,
            now=NOW, control=control, clock=clock or (lambda: NOW))
        return result, source

    def assert_rejected_before_quotes(self, question, model, **options):
        result, source = self.run_turn(question, model, **options)
        self.assertEqual(source.calls, 0, result.code)
        self.assertIsNone(result.screening_result)
        self.assertEqual(result.status, "needs_clarification")

    def test_empty_model_conditions_cannot_drop_an_explicit_exceeds_threshold(self):
        self.assert_rejected_before_quotes("筛选换手率超过2%的股票", Model())

    def test_one_phrase_cannot_silently_drop_one_of_two_fields_sharing_a_threshold(self):
        wrong = {"field": "pb", "operator": "lt", "value": 20, "phrase": "PE和PB都低于20"}
        self.assert_rejected_before_quotes("筛选PE和PB都低于20的股票", Model((wrong,)))

    def test_negated_operator_cannot_be_interpreted_as_its_positive_substring(self):
        wrong = {"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "PE不小于20"}
        self.assert_rejected_before_quotes("筛选PE不小于20的股票", Model((wrong,)))

    def test_reversed_comparison_cannot_reverse_the_requested_inequality(self):
        wrong = {"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "20小于PE"}
        self.assert_rejected_before_quotes("筛选20小于PE的股票", Model((wrong,)))

    def test_compound_unit_cannot_drop_a_trillion_multiplier(self):
        wrong = {"field": "market_cap_cny", "operator": "lt", "value": 10000, "phrase": "总市值低于1万亿"}
        self.assert_rejected_before_quotes("筛选总市值低于1万亿的股票", Model((wrong,)))

    def test_foreign_currency_threshold_cannot_be_silently_treated_as_cny(self):
        wrong = {"field": "market_cap_cny", "operator": "lt", "value": 10000000000,
                 "phrase": "总市值低于100亿美元"}
        self.assert_rejected_before_quotes("筛选总市值低于100亿美元的股票", Model((wrong,)))

    def test_fall_magnitude_cannot_become_positive_price_change(self):
        wrong = {"field": "change_percent", "operator": "gt", "value": 5, "phrase": "跌幅大于5%"}
        self.assert_rejected_before_quotes("筛选跌幅大于5%的股票", Model((wrong,)))

    def test_float_market_cap_sort_cannot_be_replaced_by_total_market_cap(self):
        self.assert_rejected_before_quotes("筛选股票，按流通市值降序选5只", Model(sort_by="market_cap_cny"))

    def test_explicit_result_count_cannot_be_replaced_by_default_five(self):
        for question in ("选出10只股票", "筛选成交额最高的前10名股票"):
            with self.subTest(question=question):
                self.assert_rejected_before_quotes(question, Model())

    def test_any_condition_cannot_be_executed_as_all_conditions(self):
        pb = {"field": "pb", "operator": "lt", "value": 1, "phrase": "PB小于1"}
        for question in ("筛选PE低于20、PB小于1中任一条件的股票",
                         "筛选PE低于20 OR PB小于1的股票",
                         "筛选PE低于20，PB小于1，两者满足其中一个即可"):
            with self.subTest(question=question):
                self.assert_rejected_before_quotes(question, Model((PE, pb)))

    def test_second_sort_key_cannot_be_silently_discarded(self):
        self.assert_rejected_before_quotes("筛选PE低于20，按成交额升序，再按换手率降序",
            Model((PE,), sort_direction="asc"))

    def test_unknown_stock_codes_cannot_silently_expand_the_requested_universe(self):
        self.assert_rejected_before_quotes("筛选000999.SZ和000888.SZ，PE低于20", Model((PE,)))

    def test_ambiguous_stock_alias_cannot_silently_expand_the_requested_universe(self):
        self.assert_rejected_before_quotes("筛选平安，PE低于20", Model((PE,)), selected_directory=directory())

    def test_old_snapshot_is_checked_against_the_later_clock_not_turn_start(self):
        result, source = self.run_turn("筛选PE低于20", Model((PE,)), clock=lambda: NOW + timedelta(days=2))
        self.assertEqual(source.calls, 1)
        self.assertEqual(result.code, "screening_data_unavailable")
        self.assertIsNone(result.screening_result)

    def test_cancelled_quote_collection_cannot_produce_a_screening_result(self):
        control = TurnControl(TurnLimits(model_calls=1))
        result, source = self.run_turn("筛选PE低于20", Model((PE,)),
            source=ReviewSource(callback=control.cancel), control=control)
        self.assertEqual(source.calls, 1)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.screening_result)

    def test_cancellation_during_rendering_also_removes_public_screening_rows(self):
        control = TurnControl(TurnLimits(model_calls=1))

        def cancel_after_render(result):
            text = render_screening_result(result)
            control.cancel()
            return text

        with patch("trading_companion.screening_agent.render_screening_result", side_effect=cancel_after_render):
            result, source = self.run_turn("筛选PE低于20", Model((PE,)), control=control)
        self.assertEqual((source.calls, result.status), (1, "cancelled"))
        self.assertIsNone(result.screening_result)
        self.assertIsNone(public_turn(result)["screening"])

    def test_deadline_during_rendering_discards_late_screening_rows(self):
        elapsed = [0.0]
        control = TurnControl(TurnLimits(model_calls=1), clock=lambda: elapsed[0])

        def finish_after_deadline(result):
            text = render_screening_result(result)
            elapsed[0] = 91.0
            return text

        with patch("trading_companion.screening_agent.render_screening_result", side_effect=finish_after_deadline):
            result, source = self.run_turn("筛选PE低于20", Model((PE,)), control=control)
        self.assertEqual((source.calls, result.code), (1, "turn_deadline_exceeded"))
        self.assertIsNone(result.screening_result)
        self.assertIsNone(public_turn(result)["screening"])

    def test_new_screening_cannot_leave_previous_company_as_implicit_followup_subject(self):
        model, data, daily = ModulesModel(), CompanyDataFixture(), MemorySourceFactory()
        source = ReviewSource()
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda now: directory(),
            data_mode="tencent", product_data_service=data, screening_source=source,
            reference_date=NOW.date(), clock=lambda: 100.0, wall_clock=lambda: NOW)
        self.addCleanup(service.close)
        credentials = service.create_session()
        sid, token = credentials["session_id"], credentials["access_token"]
        with patch("trading_companion.product_research._build_source", daily):
            first = service.message(sid, token, uuid4().hex, "分析平安银行的基本面")
            self.assertEqual(first["result"]["stage"], "company_research")
            selected = service.message(sid, token, uuid4().hex, "筛选PE低于20且换手率大于2%，按成交额降序选5只")
            self.assertEqual(selected["result"]["stage"], "screening")
            self.assertEqual(source.calls, 1)
            followup = service.message(sid, token, uuid4().hex, "它的基本面呢")
        self.assertIsNone(followup["result"].get("company_research"),
            "After a new shortlist, the previous company is no longer the implicit subject")
        self.assertEqual(followup["result"]["status"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
