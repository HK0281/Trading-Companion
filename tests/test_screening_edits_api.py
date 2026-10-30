"""Offline condition edits re-read samples and preserve session authority."""

from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from trading_companion.errors import DataSourceError
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.screening import ScreeningCondition as C, render_screening_result
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_product_modules_api import QuoteSource
from tests.test_screening_followup_api import ScreeningJourneyFixture
from tests.test_screening_intent import atoms
from tests.test_screening_agent import Model


class ScreeningEditsApiTests(ScreeningJourneyFixture, unittest.TestCase):
    def test_no_base_never_calls_models_or_any_data_source(self):
        for question in ("再加换手率大于2%", "把PE改为低于15", "按市净率升序选3只"):
            with self.subTest(question=question):
                result = self.send(question)["result"]
                self.assertEqual(result["code"], "screening_list_unavailable")
        self.assertEqual((self.model.calls, self.quotes.calls, self.data.calls, self.daily.reads), ([], [], [], []))

    def test_add_and_sort_use_fresh_full_sample_instead_of_displayed_rows(self):
        self.model.screening = Model(limit=1)
        first = self.send("帮我选1只股票")["result"]
        self.assertEqual(first["screening"]["rows"][0]["instrument_id"], "920000.BJ")
        self.assertEqual(len(self.session.screening_saved.securities), 1)
        # A previously unshown stock becomes highest activity in the next read.
        quotes = tuple(replace(quote, amount_cny=100_000_000, turnover_percent=3)
            if quote.instrument_id == "000001.SZ" else quote for quote in self.quotes.snapshot.quotes)
        self.quotes.snapshot = replace(self.quotes.snapshot, quotes=quotes)
        calls = len(self.model.calls)
        changed = self.send("再加换手率大于2%")["result"]
        self.assertEqual(changed["screening"]["rows"][0]["instrument_id"], "000001.SZ")
        self.assertEqual(self.session.screening_saved.request.conditions, (C("price", "gt", 0), C("turnover_percent", "gt", 2)))
        self.assertEqual((len(self.model.calls), len(self.quotes.calls)), (calls, 2))
        sorted_result = self.send("按市净率升序选3只")["result"]
        self.assertEqual(sorted_result["screening"]["request"]["limit"], 3)
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(len(self.quotes.calls), 3)
        self.assertEqual((self.data.calls, self.daily.reads), ([], []))
        self.assertIn("不是全市场排名", sorted_result["message"])

    def test_replace_rechecks_range_and_displays_all_inherited_conditions(self):
        phrase = "PE在5到20之间"
        self.model.screening = Model(atoms(phrase, "pe_dynamic", ("gte", 5), ("lte", 20)))
        first = self.send("筛选" + phrase)["result"]
        self.assertEqual(first["status"], "completed")
        self.send("再加换手率大于2%")
        calls = len(self.model.calls)
        result = self.send("把PE改为低于10")["result"]
        self.assertEqual(self.session.screening_saved.request.conditions, (C("pe_dynamic", "lt", 10), C("turnover_percent", "gt", 2)))
        self.assertIn("动态市盈率<10倍", result["message"])
        self.assertIn("换手率>2%", result["message"])
        self.assertEqual(len(self.model.calls), calls)

    def test_invalid_edit_preserves_original_time_and_does_not_read_quotes(self):
        self.screen()
        saved = self.session.screening_saved
        calls, reads = len(self.model.calls), len(self.quotes.calls)
        for question in ("再加换手率大于2%且新闻利好", "再加PE在20到10之间",
                         "按市净率升序选30只", "再加价格大于0"):
            with self.subTest(question=question):
                self.wall = saved.saved_at + timedelta(minutes=3)
                result = self.send(question)["result"]
                self.assertEqual(result["code"], "screening_edit_invalid")
                self.assertIs(self.session.screening_saved, saved)
                self.assertEqual((len(self.model.calls), len(self.quotes.calls)), (calls, reads))

    def test_expiry_clock_rollback_and_source_changes_are_rejected_without_reads(self):
        for change, expected in (("expiry", "screening_list_expired"), ("rollback", "screening_list_expired"),
                                 ("source", "screening_list_configuration_changed"),
                                 ("research", "screening_list_configuration_changed"),
                                 ("identity", "screening_list_identity_changed")):
            with self.subTest(change=change):
                self.install()
                self.screen()
                saved = self.session.screening_saved
                reads, calls = len(self.quotes.calls), len(self.model.calls)
                if change == "expiry": self.wall = saved.saved_at + timedelta(minutes=15)
                if change == "rollback": self.wall = saved.saved_at - timedelta(seconds=1)
                if change == "source": self.service._screening_source = QuoteSource(self.quotes.snapshot)
                if change == "research": self.service._research_source = "akshare_eastmoney"
                if change == "identity":
                    self.directory = replace(self.directory, securities=tuple(
                        replace(item, name="更名候选") if item.instrument_id == "920000.BJ" else item
                        for item in self.directory.securities))
                result = self.send("再加换手率大于2%")["result"]
                self.assertEqual(result["code"], expected)
                self.assertEqual((len(self.quotes.calls), len(self.model.calls)), (reads, calls))
                self.assertIsNone(self.session.screening_saved)
                self.directory = self.quotes.snapshot.directory
                self.wall = saved.saved_at

    def test_edit_after_knowledge_and_history_replaces_list_only_on_success(self):
        self.screen()
        saved = self.session.screening_saved
        self.send("怎么用PE/PB看估值")
        self.service.history(self.sid, self.token)
        self.assertIs(self.session.screening_saved, saved)
        self.wall += timedelta(minutes=2)
        result = self.send("再加换手率在2到4%之间")["result"]
        self.assertIsNotNone(result["screening"])
        self.assertEqual(self.session.screening_saved.saved_at, self.wall)
        self.assertNotEqual(self.session.screening_saved.securities, saved.securities)

    def test_edit_consumes_current_company_and_failed_read_does_not_revive_it(self):
        self.screen()
        self.send("第一只的基本面")
        self.assertIsNotNone(self.session.product_saved)
        calls = len(self.model.calls)
        with patch.object(self.quotes, "read_snapshot", side_effect=DataSourceError("offline failure")):
            result = self.send("再加换手率大于2%")["result"]
        self.assertEqual(result["code"], "screening_data_unavailable")
        self.assertEqual(len(self.model.calls), calls)
        self.assertIsNone(result["screening"])
        self.assertIsNone(self.session.screening_saved)
        self.assertIsNone(self.session.product_saved)
        self.assertEqual(self.send("把PE改为低于15")["result"]["code"], "screening_list_unavailable")

    def test_missing_replacement_field_explains_how_to_add_it_without_fetching(self):
        self.screen()
        saved = self.session.screening_saved
        reads, calls = len(self.quotes.calls), len(self.model.calls)
        result = self.send("把PE改为低于15")["result"]
        self.assertEqual(result["code"], "screening_edit_target_missing")
        self.assertIn("当前筛选条件中没有动态市盈率", result["message"])
        self.assertIn("再加动态市盈率低于15", result["message"])
        self.assertEqual((len(self.quotes.calls), len(self.model.calls)), (reads, calls))
        self.assertIs(self.session.screening_saved, saved)
        # The displayed suggestion is an executable addition, not another replacement.
        result = self.send("再加动态市盈率低于15")["result"]
        self.assertIsNotNone(result["screening"])
        self.assertIn(C("pe_dynamic", "lt", 15), self.session.screening_saved.request.conditions)

    def test_named_research_with_edit_like_verbs_uses_the_original_company_route(self):
        from trading_companion.product_agent import run_product_research_turn
        for has_list in (False, True):
            for question in ("按市盈率分析浦发银行的估值", "把浦发银行和平安银行的市盈率比较一下"):
                with self.subTest(has_list=has_list, question=question):
                    self.install()
                    if has_list:
                        self.screen()
                    reads = len(self.quotes.calls)
                    with patch("trading_companion.screening_agent.run_screening_edit_turn",
                               side_effect=AssertionError("This is company research, not a condition edit")), patch(
                            "trading_companion.product_agent.run_product_research_turn", wraps=run_product_research_turn) as research:
                        result = self.send(question)["result"]
                    research.assert_called_once()
                    self.assertEqual(result["stage"], "company_research", result)
                    self.assertEqual(result["code"], "company_research_completed", result)
                    self.assertEqual(len(self.quotes.calls), reads)

    def test_list_research_with_ba_and_scope_prefixes_is_not_a_condition_edit(self):
        from trading_companion.screening_followup import run_screening_followup_turn
        for has_list in (False, True):
            for question, expected in (("把第一只和第二只比较一下", ("920000.BJ", "300001.SZ")),
                                       ("按基本面分析第一只", ("920000.BJ",))):
                with self.subTest(has_list=has_list, question=question):
                    self.install()
                    if has_list:
                        self.screen()
                    reads, data_reads = len(self.quotes.calls), len(self.data.calls)
                    with patch("trading_companion.screening_agent.run_screening_edit_turn",
                               side_effect=AssertionError("This is a list selection, not a condition edit")), patch(
                            "trading_companion.screening_followup.run_screening_followup_turn", wraps=run_screening_followup_turn) as selection:
                        result = self.send(question)["result"]
                    selection.assert_called_once()
                    if has_list:
                        if len(expected) == 2:
                            # Unscoped comparisons request technical research too.
                            # Tencent daily data still excludes the first BSE item.
                            self.assertEqual(result["code"], "invalid_product_parameters", result)
                            self.assertEqual(len(self.data.calls), data_reads)
                        else:
                            self.assertEqual(result["stage"], "company_research", result)
                            self.assertEqual(tuple(request.instrument_id for request, _ in self.data.calls[data_reads:]), expected)
                        self.assertIsNotNone(self.session.screening_saved)
                    else:
                        self.assertEqual(result["code"], "screening_list_unavailable")
                        self.assertEqual(result["model_calls_attempted"], 0)
                        self.assertEqual(len(self.data.calls), data_reads)
                    self.assertEqual(len(self.quotes.calls), reads)

    def test_ba_comparison_completes_when_both_selected_daily_identities_are_supported(self):
        self.model.screening = Model(sort_by="pb", sort_direction="asc")
        self.send("筛选股票，按市净率升序选5只")
        self.assertEqual(tuple(item.instrument_id for item in self.session.screening_saved.securities[:2]),
                         ("000001.SZ", "600010.SH"))
        result = self.send("把第一只和第二只比较一下")["result"]
        self.assertEqual(result["code"], "company_research_completed", result)
        self.assertEqual(tuple(request.instrument_id for request, _ in self.data.calls), ("000001.SZ", "600010.SH"))
        self.assertEqual(len(self.quotes.calls), 1)

    def test_indicator_addition_after_selected_technical_research_uses_shared_plan(self):
        from trading_companion.product_agent import run_product_research_turn
        self.screen()
        first = self.send("分析第二只，只看SMA3")["result"]
        self.assertEqual(first["parameters"]["instrument_id"], "300001.SZ")
        saved = self.session.screening_saved
        reads = len(self.quotes.calls)
        for question in ("再加RSI", "增加RSI3"):
            with self.subTest(question=question):
                self.model.technical.position = 0
                with patch("trading_companion.screening_agent.run_screening_edit_turn",
                           side_effect=AssertionError("RSI is not a screening field")), patch(
                        "trading_companion.product_agent.run_product_research_turn", wraps=run_product_research_turn) as technical:
                    result = self.send(question)["result"]
                technical.assert_called_once()
                self.assertFalse(result["code"].startswith("screening_"), result)
                if question == "增加RSI3":
                    self.assertEqual(result["parameters"]["instrument_id"], "300001.SZ", result)
                    self.assertEqual({spec.kind for spec in self.session.product_saved.plan.indicators}, {"sma", "rsi"})
                self.assertIs(self.session.screening_saved, saved)
                self.assertEqual(len(self.quotes.calls), reads)

    def test_cancel_and_deadline_during_collection_or_render_cannot_publish_new_list(self):
        for stop in ("read_cancel", "render_cancel", "render_deadline"):
            with self.subTest(stop=stop):
                elapsed = [0.0]
                control = None
                self.install(control_factory=lambda: control)
                control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"), clock=lambda: elapsed[0])
                self.screen()
                control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"), clock=lambda: elapsed[0])
                original_read = self.quotes.read_snapshot
                def read(directory):
                    snapshot = original_read(directory)
                    if stop == "read_cancel": control.cancel()
                    return snapshot
                def render(result):
                    text = render_screening_result(result)
                    if stop == "render_cancel": control.cancel()
                    if stop == "render_deadline": elapsed[0] = 91.0
                    return text
                with patch.object(self.quotes, "read_snapshot", side_effect=read), patch(
                        "trading_companion.screening_agent.render_screening_result", side_effect=render):
                    result = self.send("再加换手率大于2%")["result"]
                if stop == "render_deadline":
                    self.assertEqual(result["code"], "turn_deadline_exceeded")
                else:
                    self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["model_calls_attempted"], 0)
                self.assertIsNone(result.get("screening"))
                self.assertIsNone(self.session.screening_saved)

    def test_replay_is_read_free_and_other_sessions_have_no_edit_authority(self):
        self.screen()
        identifier = uuid4().hex
        question = "再加换手率大于2%"
        first = self.send(question, request_id=identifier)
        reads = len(self.quotes.calls)
        replay = self.send(question, request_id=identifier)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], first["result"])
        self.assertEqual(len(self.quotes.calls), reads)
        other = self.service.create_session()
        result = self.service.message(other["session_id"], other["access_token"], uuid4().hex, question)["result"]
        self.assertEqual(result["code"], "screening_list_unavailable")
        self.assertEqual(len(self.quotes.calls), reads)

    def test_pending_identity_has_priority_over_condition_edits(self):
        self.screen()
        # The existing company route accepts only complete name/code replies.
        self.send("分析平安的基本面")
        self.assertIsNotNone(self.session.product_pending)
        with patch("trading_companion.screening_agent.run_screening_edit_turn", side_effect=AssertionError("Pending identity first")):
            result = self.send("把PE改为低于15")["result"]
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotEqual(result["stage"], "screening")

    def test_empty_result_retains_conditions_for_a_later_edit(self):
        self.screen()
        result = self.send("再加价格低于1")["result"]
        self.assertEqual(result["screening"]["rows"], [])
        self.assertIsNotNone(self.session.screening_saved)
        result = self.send("把价格改为在10到20之间")["result"]
        self.assertEqual(len(result["screening"]["rows"]), 5)

    def test_successful_edit_rebinds_ordinal_to_the_new_display_order(self):
        self.screen()
        self.send("按市净率升序选3只")
        self.send("第一只只看新闻")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "000001.SZ")

    def test_development_mode_rejects_an_injected_normal_mode_base_without_live_reads(self):
        self.screen()
        saved = self.session.screening_saved
        development_source, development_directory = load_development_fixtures()
        self.install(data_mode="synthetic", development_source=development_source,
                     directory_loader=lambda now: development_directory)
        self.session.screening_saved = saved
        reads, calls = len(self.quotes.calls), len(self.model.calls)
        result = self.send("再加换手率大于2%")["result"]
        self.assertEqual(result["code"], "screening_list_configuration_changed")
        self.assertEqual((len(self.quotes.calls), len(self.model.calls)), (reads, calls))
        self.assertIsNone(self.session.screening_saved)
