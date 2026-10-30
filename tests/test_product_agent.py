"""Same-turn company orchestration with artificial models, sources and company data."""

from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.research_session import ResearchSession
from trading_companion.tool_model import ToolCall, ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


class ProductModelFixture:
    """Return an intent proposal, then prose citing only the supplied current-turn records."""

    def __init__(self, mentions=("平安银行",), *, date_expression=None, on_synthesis=None, synthesis_response=None):
        self.mentions, self.date_expression = mentions, date_expression
        self.calls, self.contexts = [], []
        self.on_synthesis, self.synthesis_response = on_synthesis, synthesis_response

    def complete(self, messages, tools):
        self.calls.append((tuple(messages), tuple(tools)))
        if tools:
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent", PRODUCT_INTENT_TOOL_NAME,
                {"intent": "research", "instrument_mentions": list(self.mentions), "research_kinds": [],
                 "date_expression": self.date_expression, "outlook_expression": None,
                 "wants_risk": False, "clarification_reason": None}),))
        if self.on_synthesis:
            self.on_synthesis()
        if self.synthesis_response is not None:
            return self.synthesis_response
        context = json.loads(next(message.content.split("\n", 1)[1] for message in messages
            if message.role == "assistant" and message.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n")))
        self.contexts.append(context)
        if context.get("objective", {}).get("kind") == "decision":
            from trading_companion.decision_demo import scripted_decision
            return ToolModelResponse("complete", text=json.dumps(scripted_decision(context), ensure_ascii=False))
        paragraphs = []
        for company in context["securities"]:
            record = next((item for item in context["evidence"] if item["instrument_id"] == company["instrument_id"]), None)
            if record:
                paragraphs.append({"text": company["name"] + "已取得对应研究资料，经营表现应结合主营业务和财务变化分析。",
                                   "evidence_ids": [record["id"]]})
        if len(paragraphs) == 1:
            paragraphs.append({"text": "判断未来方向还需对照价格表现和相反证据，现有资料只能支持有条件的观察。",
                               "evidence_ids": paragraphs[0]["evidence_ids"]})
        return ToolModelResponse("complete", text=json.dumps(
            {"version": "product-research-answer-v1", "paragraphs": paragraphs}, ensure_ascii=False))


class ProductAgentTests(unittest.TestCase):
    def setUp(self):
        self.session, self.data, self.factory = ResearchSession(), CompanyDataFixture(), MemorySourceFactory()
        self.model = ProductModelFixture()
        for guard in (patch("socket.socket.connect", side_effect=AssertionError("No external connections")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.product_research._build_source", side_effect=AssertionError("Use fixture factory"))):
            guard.start()
            self.addCleanup(guard.stop)

    def run_turn(self, question="分析平安银行的基本面", *, model=None, data=None, now=NOW, **kwargs):
        return run_product_research_turn(question, session=self.session, model=self.model if model is None else model,
            directory=directory(), now=now, source=kwargs.pop("source", "tencent"),
            data_service=self.data if data is None else data, source_factory=self.factory, **kwargs)

    def test_single_company_fundamentals_have_two_calls_and_tool_free_synthesis(self):
        result = self.run_turn()
        self.assertEqual((result.status, result.code, result.stage),
                         ("completed", "company_research_completed", "company_research"))
        self.assertEqual(result.product_answer.status, "accepted")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 0))
        self.assertEqual([tool.name for tool in self.model.calls[0][1]], [PRODUCT_INTENT_TOOL_NAME])
        self.assertEqual(self.model.calls[1][1], ())
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(self.factory.reads, [])
        self.assertEqual(self.session.product_saved.answer, result.product_answer)
        self.assertEqual(result.display_answer, result.product_answer.text)

    def test_broad_direction_question_uses_all_domains_in_same_turn(self):
        result = self.run_turn("分析平安银行，预测接下来的方向")
        self.assertEqual(result.product_plan.kinds, ("profile", "financial", "news", "announcements", "technical"))
        self.assertEqual((result.status, result.model_calls_attempted, result.analyses_used), ("completed", 2, 1))
        self.assertEqual(len(self.factory.reads), 1)
        self.assertEqual({item.kind for item in result.product_bundle.evidence}, set(result.product_plan.kinds) | {"risk"})
        self.assertEqual(self.model.calls[1][1], ())

    def test_three_stock_comparison_keeps_identities_and_uses_three_analysis_budget(self):
        model = ProductModelFixture(("平安银行", "浦发银行", "招商银行"))
        result = self.run_turn("比较平安银行、浦发银行和招商银行的基本面和走势", model=model)
        self.assertEqual((result.status, result.model_calls_attempted, result.analyses_used), ("completed", 2, 3))
        expected = {"000001.SZ", "600000.SH", "600036.SH"}
        self.assertEqual({item.instrument_id for item in result.product_bundle.companies}, expected)
        self.assertEqual({item["instrument_id"] for item in model.contexts[0]["evidence"]}, expected)
        self.assertEqual({item.instrument_id for item in self.factory.reads}, expected)
        self.assertEqual(result.runtime["limits"]["analyses"], 3)

    def test_news_failure_retains_financial_evidence_for_natural_answer(self):
        result = self.run_turn("分析平安银行的基本面和新闻", data=CompanyDataFixture(unavailable=("news",)))
        self.assertEqual((result.status, result.code), ("limited", "company_research_partial"))
        self.assertEqual(result.product_answer.status, "accepted")
        self.assertIn("financial", {item["kind"] for item in self.model.contexts[0]["evidence"]})
        coverage = self.model.contexts[0]["securities"][0]["coverage"]
        self.assertTrue(any(item["kind"] == "news" and item["status"] == "unavailable" for item in coverage))
        self.assertIsNotNone(self.session.product_saved)

    def test_historical_fundamentals_never_use_current_profile(self):
        question = "分析平安银行2026-09-01至2026-09-20的基本面"
        model = ProductModelFixture(date_expression="2026-09-01至2026-09-20")
        result = self.run_turn(question, model=model)
        self.assertTrue(result.product_plan.explicit_historical)
        request, _ = self.data.calls[0]
        self.assertEqual(str(request.as_of.date()), "2026-09-20")
        self.assertFalse(request.allow_current_background)
        self.assertNotIn("profile", {item["kind"] for item in model.contexts[0]["evidence"]})

    def test_requested_risk_scenario_is_computed_before_tool_free_model_answer(self):
        result = self.run_turn("分析平安银行的止盈止损风险情景")
        self.assertEqual((result.status, result.model_calls_attempted, result.analyses_used), ("completed", 2, 1))
        risk = result.product_bundle.companies[0].risk
        self.assertEqual((risk.values.reference_stop, risk.values.reference_target), ("7.00", "16.00"))
        scenarios = [item for item in self.model.contexts[0]["evidence"] if item["content_kind"] == "scenario"]
        self.assertEqual(len(scenarios), 1)
        self.assertIn("参考止损7.00元", scenarios[0]["display"])
        self.assertIn("参考目标16.00元", scenarios[0]["display"])
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.model.calls[1][1], ())

    def test_ambiguous_name_then_explicit_selection_preserves_original_question(self):
        model = ProductModelFixture(("平安",))
        question = "分析平安的基本面和新闻"
        pending = self.run_turn(question, model=model)
        self.assertEqual(pending.status, "needs_clarification")
        self.assertIsNotNone(self.session.product_pending)
        self.assertEqual((len(model.calls), len(self.data.calls)), (1, 0))
        result = self.run_turn("平安银行", model=model, now=NOW + timedelta(seconds=10))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.product_plan.user_question, question)
        self.assertEqual(model.calls[-1][0][1].content, question)
        self.assertEqual(result.model_calls_attempted, 1)
        self.assertEqual(self.data.calls[0][1], ("profile", "financial", "news"))
        self.assertIsNone(self.session.product_pending)

    def test_model_invented_wrong_stock_is_blocked_before_source_access(self):
        result = self.run_turn(model=ProductModelFixture(("600000.SH",)))
        self.assertEqual((result.status, result.code), ("needs_clarification", "ungrounded_product_intent"))
        self.assertEqual(result.model_calls_attempted, 1)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.factory.reads, [])
        self.assertIsNone(self.session.product_saved)

    def test_saved_history_replays_without_new_model_or_data_calls(self):
        original = self.run_turn()
        model_count, data_count, read_count = len(self.model.calls), len(self.data.calls), len(self.factory.reads)
        history = self.run_turn("查看上次结果", now=NOW + timedelta(minutes=1))
        self.assertEqual(history.code, "product_saved_result")
        self.assertEqual(history.product_answer, original.product_answer)
        self.assertEqual((history.model_calls_attempted, history.analyses_used), (0, 0))
        self.assertEqual((len(self.model.calls), len(self.data.calls), len(self.factory.reads)),
                         (model_count, data_count, read_count))

    def test_expired_history_is_cleared_without_refetching(self):
        self.run_turn()
        calls = len(self.data.calls), len(self.model.calls)
        history = self.run_turn("查看上次结果", now=NOW + timedelta(minutes=16))
        self.assertEqual(history.code, "product_result_unavailable")
        self.assertIsNone(self.session.product_saved)
        self.assertEqual((len(self.data.calls), len(self.model.calls)), calls)

    def test_failed_new_company_work_consumes_old_saved_result(self):
        self.run_turn()
        self.assertIsNotNone(self.session.product_saved)
        model = ProductModelFixture(("浦发银行",))
        result = self.run_turn("分析浦发银行的基本面", model=model,
            data=CompanyDataFixture(unavailable=("profile", "financial")))
        self.assertEqual(result.status, "failed")
        self.assertIsNone(self.session.product_saved)
        self.assertEqual(len(model.calls), 1)
        history = self.run_turn("查看上次结果")
        self.assertEqual(history.code, "product_result_unavailable")

    def test_short_followup_reuses_identity_but_collects_fresh_requested_domain(self):
        self.run_turn()
        model = ProductModelFixture()
        result = self.run_turn("那新闻呢", model=model, now=NOW + timedelta(minutes=1))
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.data.calls[-1][1], ("news",))
        self.assertEqual(len(self.data.calls), 2)
        self.assertEqual({item["kind"] for item in model.contexts[0]["evidence"]}, {"news"})
        self.assertEqual({item.instrument_id for item in result.product_bundle.companies}, {"000001.SZ"})

    def test_cancellation_after_synthesis_discards_late_output_and_saved_state(self):
        self.run_turn()
        control = TurnControl(TurnLimits(model_calls=2))
        model = ProductModelFixture(on_synthesis=control.cancel)
        result = self.run_turn(model=model, control=control)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.failure_code, "turn_cancelled")
        self.assertIsNone(result.product_answer)
        self.assertIsNone(self.session.product_saved)
        self.assertEqual(len(model.calls), 2)

    def test_tighter_analysis_budget_stops_before_third_market_read_and_synthesis(self):
        model = ProductModelFixture(("平安银行", "浦发银行", "招商银行"))
        control = TurnControl(TurnLimits(model_calls=2, analyses=2, profile="company_research"))
        result = self.run_turn("比较平安银行、浦发银行和招商银行的走势", model=model, control=control)
        self.assertEqual(result.failure_code, "analysis_budget_exhausted")
        self.assertEqual((result.analyses_used, len(self.factory.reads)), (2, 2))
        self.assertEqual(len(model.calls), 1)
        self.assertIsNone(self.session.product_saved)

    def test_synthesis_tool_request_is_rejected_without_tool_execution_or_retry(self):
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("forbidden", "execute_trade", {}),))
        model = ProductModelFixture(synthesis_response=proposal)
        result = self.run_turn(model=model)
        self.assertEqual((result.status, result.product_answer.status), ("limited", "fallback"))
        self.assertEqual(result.product_answer.code, "answer_facts_only")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1][1], ())
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(self.factory.reads, [])

    def test_incomplete_optional_citation_uses_supplied_fallback_without_extra_collection(self):
        class CitationRewriteModel(ProductModelFixture):
            rewrites = 0

            def complete(self, messages, tools):
                original = super().complete(messages, tools)
                if tools:
                    return original
                self.rewrites += 1
                context = self.contexts[-1]
                revenue = next(item for item in context["evidence"] if item["label"] == "营业收入")
                other = next(item for item in context["evidence"] if item["kind"] == "profile")
                identifier = other["id"] if self.rewrites == 1 else revenue["id"]
                return ToolModelResponse("complete", json.dumps({"version": "product-research-answer-v1", "paragraphs": [
                    {"text": "平安银行营业收入为" + revenue["display"] + "，还需结合盈利质量观察。", "evidence_ids": [identifier]},
                    {"text": "现有经营资料不足以单独确定未来价格方向。", "evidence_ids": [revenue["id"]]},
                ]}, ensure_ascii=False))

        model = CitationRewriteModel()
        result = self.run_turn(model=model)
        self.assertEqual((result.status, result.product_answer.status), ("completed", "accepted"))
        self.assertEqual((result.model_calls_attempted, len(result.model_responses), len(model.calls)), (2, 2, 2))
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(self.factory.reads, [])
        self.assertEqual(model.calls[-1][1], ())


if __name__ == "__main__":
    unittest.main()
