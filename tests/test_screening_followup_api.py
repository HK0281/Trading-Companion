"""Offline shortlist-to-research journeys, including routing, state and stop races."""

from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from trading_companion.api_service import ResearchApiService
from trading_companion.errors import DataSourceError
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_INSTRUCTION
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.screening import demo_screening_snapshot, render_screening_result
from trading_companion.screening_agent import SCREEN_TOOL
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.tool_model import ToolCall, ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_open_knowledge_api import EducationAndResearchModel
from tests.test_product_agent import ProductModelFixture
from tests.test_product_modules_api import QuoteSource
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW
from tests.test_research_workflow import WorkflowScriptModel
from tests.test_screening_agent import Model as ScreeningModel


class JourneyModel:
    """Copy literal identities, then use the existing contract-faithful answer fixtures."""

    def __init__(self, directory):
        self.directory = directory
        self.screening, self.company = ScreeningModel(), ProductModelFixture()
        self.knowledge = EducationAndResearchModel()
        self.technical = WorkflowScriptModel(date_expression=None, indicator_mentions=("SMA3",))
        self.calls, self.override_mentions, self.on_intent = [], None, None

    def complete(self, messages, *, tools):
        self.calls.append((tuple(messages), tuple(tools)))
        if messages[0].content == GENERAL_KNOWLEDGE_INSTRUCTION:
            return self.knowledge.complete(messages, tools=tools)
        if tools and tools[0].name == SCREEN_TOOL.name:
            return self.screening.complete(messages, tools=tools)
        if tools and tools[0].name in (PRODUCT_INTENT_TOOL_NAME, INTENT_TOOL_NAME):
            question = messages[1].content
            mentions = tuple(question[start:end] for start, end in self.directory.mentioned_spans(question))
            mentions = mentions if self.override_mentions is None else self.override_mentions
            if self.on_intent:
                self.on_intent()
            if tools[0].name == PRODUCT_INTENT_TOOL_NAME:
                self.company.mentions = mentions
                return self.company.complete(messages, tools=tools)
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent", INTENT_TOOL_NAME,
                {"intent": "research", "instrument_mentions": list(mentions), "date_expression": None,
                 "indicator_mentions": ["SMA3"], "clarification_reason": None}),))
        if tools:
            return self.technical.complete(messages, tools=tools)
        return self.company.complete(messages, tools=tools)


class ScreeningJourneyFixture:
    def setUp(self):
        sample = demo_screening_snapshot(clock=lambda: NOW)
        identities = tuple(replace(item, aliases=("平安",)) if item.instrument_id == "000001.SZ" else item
                           for item in sample.directory.securities)
        self.directory = SecurityDirectory((*identities, Security("600000.SH", "浦发银行"),
            Security("601318.SH", "中国平安", ("平安",))), "offline_identity_directory", NOW)
        self.quotes = QuoteSource(replace(sample, directory=self.directory, is_fixture=False,
                                          source="offline_quote_source"))
        self.model, self.data = JourneyModel(self.directory), CompanyDataFixture()
        self.daily = MemorySourceFactory()
        self.wall = NOW
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No live network")),
            patch("requests.get", side_effect=AssertionError("No live provider HTTP")),
            patch("requests.post", side_effect=AssertionError("No live model HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials")),
            patch("trading_companion.product_research._build_source", self.daily),
            patch("trading_companion.research_tool._build_source", self.daily),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.install()

    def install(self, **changes):
        options = dict(model_factory=lambda: self.model, directory_loader=lambda now: self.directory,
            provider="fake", data_mode="tencent", product_data_service=self.data, screening_source=self.quotes,
            clock=lambda: 100.0, wall_clock=lambda: self.wall)
        options.update(changes)
        self.service = ResearchApiService(**options)
        self.addCleanup(self.service.close)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]

    def send(self, question, *, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    @property
    def session(self):
        return self.service._entries[self.sid].session

    def screen(self):
        result = self.send("帮我选5只股票")["result"]
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual([row["instrument_id"] for row in result["screening"]["rows"]],
                         ["920000.BJ", "300001.SZ", "688981.SH", "600010.SH", "000001.SZ"])
        return result


class ScreeningFollowupApiTests(ScreeningJourneyFixture, unittest.TestCase):
    def test_no_list_ordinal_never_calls_model_or_research_sources(self):
        turn = self.send("分析第一只的基本面")["result"]
        self.assertEqual((turn["status"], turn["code"]), ("needs_clarification", "screening_list_unavailable"))
        self.assertIn("当前没有可用的筛选名单", turn["message"])
        self.assertEqual((self.model.calls, self.quotes.calls, self.data.calls, self.daily.reads), ([], [], [], []))

    def test_named_questions_with_scope_ordinals_use_the_original_company_route(self):
        from trading_companion.product_agent import run_product_research_turn
        for has_list in (False, True):
            for question in ("分析浦发银行上市后第一个交易日以来的走势", "分析浦发银行第三个季度的基本面"):
                with self.subTest(has_list=has_list, question=question):
                    self.install()
                    if has_list:
                        self.screen()
                    with patch("trading_companion.product_agent.run_product_research_turn",
                               wraps=run_product_research_turn) as company_route:
                        result = self.send(question)["result"]
                    # Date interpretation may still ask for a precise window, as it
                    # did before shortlists existed; the list must not intercept it.
                    company_route.assert_called_once()
                    self.assertEqual(company_route.call_args.args[0], question)
                    self.assertFalse(result["code"].startswith("screening_"), result)
                    self.assertGreater(result["model_calls_attempted"], 0)

    def test_named_list_member_can_keep_an_ordinal_in_its_research_scope(self):
        self.screen()
        result = self.send("分析平安银行第三个季度的基本面")["result"]
        self.assertEqual(result["stage"], "company_research", result)
        self.assertEqual(self.data.calls[-1][0].instrument_id, "000001.SZ")

    def test_nonreferential_plural_does_not_request_shortlist_selection(self):
        self.screen()
        with patch("trading_companion.screening_followup.run_screening_followup_turn",
                   side_effect=AssertionError("This question is not about the displayed list")):
            result = self.send("全部A股的新闻")["result"]
        self.assertFalse(result["code"].startswith("screening_"), result)

    def test_single_selection_preserves_only_news_and_original_question(self):
        first = self.screen()
        first["screening"]["rows"][0]["instrument_id"] = "000001.SZ"
        question = "第一只只看新闻，不要技术指标"
        turn = self.send(question)["result"]
        self.assertEqual(turn["stage"], "company_research")
        self.assertEqual([request.instrument_id for request, _ in self.data.calls], ["920000.BJ"])
        self.assertEqual(self.data.calls[0][1], ("news",))
        self.assertEqual(self.daily.reads, [])
        self.assertEqual(turn["model_calls_attempted"], 2)
        self.assertEqual(self.session.product_saved.plan.user_question, question)
        self.assertEqual(self.session.screening_saved.saved_at, NOW)

    def test_two_and_three_selection_respects_budget_and_original_list_can_be_selected_again(self):
        self.screen()
        turn = self.send("比较第2和第3只的基本面和走势")["result"]
        self.assertEqual(turn["stage"], "company_research")
        self.assertEqual([request.instrument_id for request, _ in self.data.calls], ["300001.SZ", "688981.SH"])
        self.assertEqual(turn["analyses_used"], 2)
        self.assertEqual(turn["model_calls_attempted"], 2)
        self.data.calls.clear()
        three = self.send("比较名单中第2和第3和第5只的基本面和走势")["result"]
        self.assertEqual([request.instrument_id for request, _ in self.data.calls],
                         ["300001.SZ", "688981.SH", "000001.SZ"])
        self.assertEqual(three["analyses_used"], 3)
        self.assertEqual(len(self.quotes.calls), 1)

    def test_ambiguous_pronouns_and_outside_bounds_need_no_model_or_source_reads(self):
        self.screen()
        calls = len(self.model.calls)
        for question in ("它的基本面呢", "比较前四只", "分析第六只", "第一只和垃圾股票", "第一只并且分析垃圾股票"):
            with self.subTest(question=question):
                turn = self.send(question)["result"]
                self.assertEqual(turn["status"], "needs_clarification")
                self.assertEqual(turn["model_calls_attempted"], 0)
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.daily.reads, [])

    def test_knowledge_and_history_do_not_consume_or_extend_the_list(self):
        self.screen()
        saved = self.session.screening_saved
        self.assertEqual(self.send("怎么用PE/PB看估值")["result"]["stage"], "knowledge")
        self.service.history(self.sid, self.token)
        self.assertIs(self.session.screening_saved, saved)
        self.wall += timedelta(minutes=14)
        self.send("名单中第二只只看新闻")
        self.assertEqual(self.data.calls[0][0].instrument_id, "300001.SZ")
        self.assertIs(self.session.screening_saved, saved)
        self.wall = NOW + timedelta(minutes=15)
        count = len(self.model.calls)
        turn = self.send("分析名单中第一只")["result"]
        self.assertEqual(turn["code"], "screening_list_expired")
        self.assertEqual(len(self.model.calls), count)
        self.assertIsNone(self.session.screening_saved)

    def test_current_research_pronoun_keeps_list_for_a_later_explicit_ordinal(self):
        self.screen()
        self.send("第一只的基本面")
        self.send("它的新闻呢")
        self.assertEqual([request.instrument_id for request, _ in self.data.calls], ["920000.BJ", "920000.BJ"])
        self.assertEqual(self.data.calls[-1][1], ("news",))
        self.send("名单中第二只的基本面")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "300001.SZ")
        self.assertEqual(len(self.quotes.calls), 1)

    def test_pending_identity_ordinal_selects_its_candidates_before_the_shortlist(self):
        self.screen()
        pending = self.send("分析平安的基本面")["result"]
        self.assertEqual(pending["code"], "product_instrument_needs_clarification")
        saved = self.session.screening_saved
        count = len(self.model.calls)
        reply = self.send("第一个")["result"]
        self.assertEqual(reply["code"], "company_research_completed")
        self.assertEqual(len(self.model.calls), count+1)
        self.assertIsNone(self.session.screening_saved)
        self.assertIsNone(self.session.product_pending)
        self.assertEqual(self.data.calls[-1][0].instrument_id, "000001.SZ")

    def test_named_outside_research_consumes_the_old_list_and_new_screening_changes_ordinals(self):
        self.screen()
        self.send("分析浦发银行的基本面")
        self.assertIsNone(self.session.screening_saved)
        turn = self.send("名单中第二只")["result"]
        self.assertEqual(turn["code"], "screening_list_unavailable")
        self.quotes.snapshot = replace(self.quotes.snapshot, quotes=tuple(
            replace(quote, amount_cny=100000000.0) if quote.instrument_id == "000001.SZ" else quote
            for quote in self.quotes.snapshot.quotes))
        self.send("帮我选5只股票")
        self.send("第一只只看新闻")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "000001.SZ")

    def test_named_member_and_six_digit_code_continue_without_recollecting_quotes(self):
        self.screen()
        self.send("研究特锐德的其它风险")
        self.assertEqual(self.session.product_saved.plan.securities[0].instrument_id, "300001.SZ")
        self.send("研究920000的新闻")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "920000.BJ")
        self.assertEqual(len(self.quotes.calls), 1)

    def test_explicit_screening_list_reference_is_not_a_new_screening_request(self):
        self.screen()
        self.send("分析筛选名单中的第一只的基本面")
        self.send("分析筛选结果中第二只的新闻")
        self.assertEqual([request.instrument_id for request, _ in self.data.calls], ["920000.BJ", "300001.SZ"])
        self.assertEqual(len(self.quotes.calls), 1)

    def test_expired_list_does_not_block_independent_named_research(self):
        self.screen()
        self.wall += timedelta(minutes=16)
        turn = self.send("分析安徽凤凰的基本面")["result"]
        self.assertEqual(turn["stage"], "company_research")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "920000.BJ")
        self.assertIsNone(self.session.screening_saved)

    def test_omitted_or_added_model_objects_cannot_start_collection(self):
        self.screen()
        for mentions in (("920000.BJ",), ("920000.BJ", "300001.SZ", "000001.SZ")):
            self.model.override_mentions = mentions
            turn = self.send("比较前两只的基本面")["result"]
            self.assertEqual(turn["status"], "needs_clarification")
            self.assertEqual(self.data.calls, [])
            self.assertEqual(self.daily.reads, [])

    def test_plan_identity_is_pinned_even_if_an_upstream_plan_validator_regresses(self):
        from tests.test_product_research import plan
        self.screen()
        wrong = plan(securities=(self.directory.lookup("000001.SZ").candidates[0],))
        with patch("trading_companion.product_agent.prepare_product_plan", return_value=wrong):
            turn = self.send("第一只的基本面")["result"]
        self.assertEqual(turn["code"], "screening_selection_plan_mismatch")
        self.assertEqual(self.data.calls, [])

    def test_cancelled_company_selection_publishes_no_current_subject_and_keeps_original_list(self):
        control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"))
        self.install(control_factory=lambda: control)
        self.screen()
        # Each request needs a fresh controller; the second one is cancelled during synthesis.
        control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"))
        self.model.company.on_synthesis = control.cancel
        turn = self.send("第一只的基本面")["result"]
        self.assertEqual(turn["status"], "cancelled")
        self.assertIsNone(turn.get("company_research"))
        self.assertIsNone(self.session.product_saved)
        self.assertIsNotNone(self.session.screening_saved)

    def test_failed_new_screening_consumes_the_previous_list_and_company_subject(self):
        self.screen()
        self.send("第一只的基本面")
        with patch.object(self.quotes, "read_snapshot", side_effect=DataSourceError("offline failure")):
            turn = self.send("帮我选5只股票")["result"]
        self.assertEqual(turn["code"], "screening_data_unavailable")
        self.assertIsNone(self.session.screening_saved)
        self.assertIsNone(self.session.product_saved)

    def test_deadline_during_render_never_publishes_the_new_list(self):
        elapsed = [0.0]
        self.install(control_factory=lambda: TurnControl(
            TurnLimits(model_calls=6, analyses=3, profile="company_research"), clock=lambda: elapsed[0]))
        def late_render(result):
            text = render_screening_result(result)
            elapsed[0] = 91.0
            return text
        with patch("trading_companion.screening_agent.render_screening_result", side_effect=late_render):
            turn = self.send("帮我选5只股票")["result"]
        self.assertEqual(turn["code"], "turn_deadline_exceeded")
        self.assertIsNone(turn["screening"])
        self.assertIsNone(self.session.screening_saved)

    def test_replay_and_other_sessions_never_rebind_an_existing_shortlist(self):
        self.screen()
        identifier = uuid4().hex
        first = self.send("第一只的基本面", request_id=identifier)
        reads, calls = len(self.data.calls), len(self.model.calls)
        replay = self.send("第一只的基本面", request_id=identifier)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["result"], replay["result"])
        self.assertEqual((len(self.data.calls), len(self.model.calls)), (reads, calls))
        other = self.service.create_session()
        turn = self.service.message(other["session_id"], other["access_token"], uuid4().hex, "分析第一只")["result"]
        self.assertEqual(turn["code"], "screening_list_unavailable")

    def test_reset_and_changed_identity_or_source_configuration_reject_old_lists(self):
        self.screen()
        self.directory = replace(self.directory, securities=tuple(
            replace(item, name="更名候选") if item.instrument_id == "920000.BJ" else item
            for item in self.directory.securities))
        self.assertEqual(self.send("第一只的新闻")["result"]["code"], "screening_list_identity_changed")
        self.assertEqual(self.data.calls, [])
        self.assertIsNone(self.session.screening_saved)
        # Source identity is pinned even if a replacement object advertises the same provider name.
        self.directory = self.quotes.snapshot.directory
        self.screen()
        self.service._screening_source = QuoteSource(self.quotes.snapshot)
        self.assertEqual(self.send("第一只的新闻")["result"]["code"], "screening_list_configuration_changed")
        self.service._screening_source = self.quotes
        self.screen()
        self.service.reset(self.sid, self.token)
        self.assertIsNone(self.session.screening_saved)

    def test_explicit_technical_selection_uses_original_executor_and_retains_list_on_refresh(self):
        self.screen()
        turn = self.send("分析第二只，只看SMA3")["result"]
        self.assertIsNotNone(turn["parameters"], turn)
        self.assertEqual(turn["parameters"]["instrument_id"], "300001.SZ", turn)
        self.assertEqual(turn["analyses_used"], 1)
        self.assertEqual(turn["model_calls_attempted"], 3)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.daily.reads[0].instrument_id, "300001.SZ")
        self.model.technical.position = 0
        refreshed = self.send("刷新行情")["result"]
        self.assertEqual(refreshed["parameters"]["instrument_id"], "300001.SZ")
        self.assertIsNotNone(self.session.screening_saved)
        self.send("名单中第二只只看新闻")
        self.assertEqual(self.data.calls[-1][0].instrument_id, "300001.SZ")

    def test_company_configuration_missing_is_explicit_and_does_not_call_a_model(self):
        self.install(product_data_service=None)
        self.screen()
        count = len(self.model.calls)
        turn = self.send("第一只的基本面")["result"]
        self.assertEqual(turn["code"], "screening_research_unavailable")
        self.assertEqual(len(self.model.calls), count)
