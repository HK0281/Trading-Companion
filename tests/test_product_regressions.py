"""Targeted product-scope and answer regressions using only artificial sources and HTTP responses."""

from datetime import date, datetime
import json
import re
import unittest
from unittest.mock import Mock, patch

from scripts.verify import offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.openai_tool_model import OpenAIConfig, OpenAIToolModel
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_answer import synthesize_product_answer
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.research_session import ResearchSession
from trading_companion.tool_model import ToolModelResponse
from trading_companion.turn_control import bind_turn, company_turn_control
from tests.test_product_agent import ProductModelFixture
from tests.test_product_answer import RecordingModel
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, SECURITIES, directory, plan


class ScopeAwareModel(ProductModelFixture):
    """Extract literal historical dates if orchestration places them in the effective question."""

    def complete(self, messages, tools):
        if tools and self.date_expression is None:
            literal = re.search(r"截至[0-9]{4}-[0-9]{2}-[0-9]{2}|"
                                r"[0-9]{4}-[0-9]{2}-[0-9]{2}至[0-9]{4}-[0-9]{2}-[0-9]{2}", messages[1].content)
            if literal:
                self.date_expression = literal.group()
        return super().complete(messages, tools)


def prose_response(paragraphs):
    return ToolModelResponse("complete", json.dumps({"version": "product-research-answer-v1",
        "paragraphs": list(paragraphs)}, ensure_ascii=False))


class ProductRegressions(unittest.TestCase):
    def setUp(self):
        self.session, self.data, self.source_factory = ResearchSession(), CompanyDataFixture(), MemorySourceFactory()
        for guard in (patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.product_research._build_source", side_effect=AssertionError("Use fixture source"))):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)

    def run_turn(self, question, *, model=None):
        return run_product_research_turn(question, session=self.session, model=model or ScopeAwareModel(),
            directory=directory(), now=NOW, source="tencent", data_service=self.data,
            source_factory=self.source_factory)

    def historical_research(self, *, horizon=False):
        question = "分析平安银行截至2026-09-20的基本面" + ("，展望未来30天" if horizon else "")
        result = self.run_turn(question, model=ScopeAwareModel(date_expression="截至2026-09-20"))
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(result.product_plan.end_date, date(2026, 9, 20))
        self.assertTrue(result.product_plan.explicit_historical)
        return result

    def test_historical_then_technical_followup_preserves_date_window_and_historical_policy(self):
        previous = self.historical_research()
        turn = self.run_turn("那技术面呢")
        self.assertIn(turn.status, ("completed", "limited"))
        self.assertEqual((turn.product_plan.start_date, turn.product_plan.end_date),
                         (previous.product_plan.start_date, previous.product_plan.end_date))
        self.assertTrue(turn.product_plan.explicit_historical)
        self.assertEqual(self.source_factory.reads[-1].as_of_date, date(2026, 9, 20))
        self.assertEqual((turn.model_calls_attempted, turn.analyses_used), (2, 1))

    def test_historical_then_news_followup_cannot_fetch_news_from_after_the_saved_cutoff(self):
        previous = self.historical_research()
        turn = self.run_turn("那新闻呢")
        self.assertIn(turn.status, ("completed", "limited"))
        request, kinds = self.data.calls[-1]
        self.assertEqual(kinds, ("news",))
        self.assertEqual(request.as_of.date(), previous.product_plan.end_date)
        self.assertFalse(request.allow_current_background)
        self.assertTrue(turn.product_plan.explicit_historical)
        self.assertTrue(all(item.published_at is None or datetime.fromisoformat(item.published_at).date() <= date(2026, 9, 20)
                            for item in turn.product_bundle.evidence))

    def test_explicit_latest_refresh_leaves_historical_scope(self):
        self.historical_research()
        turn = self.run_turn("那按最新数据重新分析基本面")
        self.assertEqual(turn.status, "completed")
        self.assertEqual(turn.product_plan.end_date, date(2026, 9, 25))
        self.assertFalse(turn.product_plan.explicit_historical)
        request, _ = self.data.calls[-1]
        self.assertEqual(request.as_of, NOW)
        self.assertTrue(request.allow_current_background)

    def test_explicit_new_historical_window_overrides_saved_scope(self):
        self.historical_research()
        turn = self.run_turn("那看2026-09-21至2026-09-24的新闻",
            model=ScopeAwareModel(date_expression="2026-09-21至2026-09-24"))
        self.assertEqual(turn.status, "completed")
        self.assertEqual((turn.product_plan.start_date, turn.product_plan.end_date), (date(2026, 9, 21), date(2026, 9, 24)))
        self.assertTrue(turn.product_plan.explicit_historical)

    def test_implicit_followup_preserves_explicit_outlook_objective(self):
        previous = self.historical_research(horizon=True)
        turn = self.run_turn("那技术面呢")
        self.assertEqual(previous.product_plan.objective.horizon, "未来30天")
        self.assertEqual(turn.product_plan.objective, previous.product_plan.objective)

    def test_comparison_answer_cannot_silently_omit_other_requested_companies(self):
        runtime = company_turn_control()
        runtime.start()
        with bind_turn(runtime):
            bundle = collect_product_research(plan(securities=SECURITIES[:3]),
                data_service=self.data, now=NOW, source_factory=self.source_factory)
        context = model_research_context(bundle)
        identifier = next(item["id"] for item in context["evidence"] if item["instrument_id"] == "000001.SZ")
        paragraphs = ({"text": "平安银行已有经营资料，可以结合盈利质量继续观察。", "evidence_ids": [identifier]},
                      {"text": "平安银行的后续表现仍需结合经营和价格变化。", "evidence_ids": [identifier]})
        run = synthesize_product_answer("比较平安银行、浦发银行和招商银行的基本面", bundle,
                                        model=RecordingModel(prose_response(paragraphs)))
        self.assertNotEqual(run.answer.status, "accepted")

    def test_risk_reply_cannot_swap_computed_stop_and_target_prices(self):
        runtime = company_turn_control()
        runtime.start()
        with bind_turn(runtime):
            bundle = collect_product_research(plan(kinds=("technical",), wants_risk=True),
                data_service=self.data, now=NOW, source_factory=self.source_factory)
        context = model_research_context(bundle)
        scenario = next(item for item in context["evidence"] if item["kind"] == "risk")
        paragraphs = ({"text": "平安银行历史情景的参考止损为16.00元，参考目标为7.00元。",
                       "evidence_ids": [scenario["id"]]},
                      {"text": "这些历史参考值需要结合后续价格观察。", "evidence_ids": [scenario["id"]]})
        run = synthesize_product_answer("分析平安银行的止盈止损", bundle,
                                        model=RecordingModel(prose_response(paragraphs)))
        self.assertNotEqual(run.answer.status, "accepted")

    def test_direction_question_model_failure_still_uses_available_technical_relationships(self):
        def unavailable_model():
            raise RuntimeError("Artificial synthesis failure")

        turn = self.run_turn("分析平安银行，预测接下来的方向",
                             model=ScopeAwareModel(on_synthesis=unavailable_model))
        self.assertEqual((turn.status, turn.product_answer.status), ("limited", "fallback"))
        self.assertTrue(any(item.kind == "technical" for item in turn.product_bundle.evidence))
        self.assertTrue(any(item.content_kind == "trade_plan" for item in turn.product_answer.evidence))
        self.assertIsNone(turn.product_answer.decisions[0]["rating"])

    def api_turn(self, question, *, model=None):
        selected_model = ScopeAwareModel() if model is None else model
        service = ResearchApiService(model_factory=lambda: selected_model, directory_loader=lambda now: directory(),
            data_mode="tencent", product_data_service=self.data, reference_date=NOW.date(),
            clock=lambda: 100.0, wall_clock=lambda: NOW)
        self.addCleanup(service.close)
        credentials = service.create_session()
        with patch("trading_companion.product_research._build_source", side_effect=self.source_factory):
            return service.message(credentials["session_id"], credentials["access_token"], "a" * 32, question)["result"]

    def test_common_named_stock_questions_reach_multidomain_product_route(self):
        for question in ("平安银行值得买吗", "帮我看看平安银行", "平安银行现在处于什么位置",
                         "平安银行有什么利好利空", "平安银行为什么涨了"):
            with self.subTest(question=question):
                model = ScopeAwareModel()
                turn = self.api_turn(question, model=model)
                self.assertIn(turn["status"], ("completed", "limited"))
                self.assertEqual(turn["stage"], "company_research")
                self.assertEqual(turn["company_research"]["securities"], [{"instrument_id": "000001.SZ", "name": "平安银行"}])
                self.assertEqual(len(model.calls), 2)

    def test_explicitly_excluded_domains_do_not_trigger_tools_or_analysis_budget(self):
        for question in ("只看平安银行的新闻，不需要技术指标", "只看平安银行的新闻，不看基本面",
                         "只看平安银行的新闻，不需要止盈止损"):
            with self.subTest(question=question):
                turn = self.api_turn(question)
                self.assertIn(turn["status"], ("completed", "limited"))
                coverage = turn["company_research"]["coverage"][0]["sections"]
                self.assertEqual({item["kind"] for item in coverage}, {"news"})
                self.assertEqual(turn["analyses_used"], 0)
                self.assertEqual(self.data.calls[-1][1], ("news",))

    def test_direct_technical_domain_followup_reuses_product_identity(self):
        model = ScopeAwareModel()
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda now: directory(),
            data_mode="tencent", product_data_service=self.data, reference_date=NOW.date(),
            clock=lambda: 100.0, wall_clock=lambda: NOW)
        self.addCleanup(service.close)
        credentials = service.create_session()
        with patch("trading_companion.product_research._build_source", side_effect=self.source_factory):
            first = service.message(credentials["session_id"], credentials["access_token"], "a" * 32,
                                    "分析平安银行的基本面")["result"]
            self.assertEqual(first["status"], "completed")
            second = service.message(credentials["session_id"], credentials["access_token"], "b" * 32,
                                     "只看技术面")["result"]
        self.assertIn(second["status"], ("completed", "limited"))
        self.assertEqual(second["stage"], "company_research")
        self.assertEqual(second["company_research"]["securities"], [{"instrument_id": "000001.SZ", "name": "平安银行"}])
        self.assertEqual(second["analyses_used"], 1)
        self.assertEqual(len(self.data.calls), 1)

    def test_execution_wording_is_rejected_before_any_provider_or_data_request(self):
        model = ScopeAwareModel()
        turn = self.api_turn("帮我买入平安银行100股", model=model)
        self.assertEqual(turn["code"], "product_trade_execution_forbidden")
        self.assertEqual(turn["model_calls_attempted"], 0)
        self.assertEqual(model.calls, [])
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.source_factory.reads, [])

    @staticmethod
    def intent_payload(mentions=("平安银行",)):
        arguments = {"intent": "research", "instrument_mentions": list(mentions), "research_kinds": [],
                     "date_expression": None, "outlook_expression": None, "wants_risk": False, "clarification_reason": None}
        return {"model": "synthetic-openai-served-model", "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "intent", "type": "function", "function": {
                "name": "interpret_product_research", "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    def test_openai_provider_error_during_synthesis_keeps_collected_financial_fallback(self):
        first, second = Mock(status_code=200), Mock(status_code=500)
        first.json.return_value = self.intent_payload()
        second.json.return_value = {"error": "private-provider-error-never-export"}
        model = OpenAIToolModel(OpenAIConfig("synthetic-chat-model", "synthetic-credential-not-real"))
        with patch("requests.post", side_effect=[first, second]) as post:
            turn = self.run_turn("分析平安银行的基本面", model=model)
        self.assertEqual((turn.status, turn.product_answer.status), ("limited", "fallback"))
        self.assertEqual(turn.failure_code, "answer_facts_only")
        self.assertIn("answer_model_unavailable", str(turn.product_answer.quality["reasons"]))
        self.assertIn("1.23亿元", turn.display_answer)
        self.assertNotIn("private-provider-error", turn.display_answer)
        self.assertEqual((post.call_count, turn.model_calls_attempted, turn.analyses_used), (2, 2, 0))
        self.assertEqual(len(self.data.calls), 1)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_openai_wrong_stock_intent_is_blocked_before_company_reads(self):
        response = Mock(status_code=200)
        response.json.return_value = self.intent_payload(("600000.SH",))
        model = OpenAIToolModel(OpenAIConfig("synthetic-chat-model", "synthetic-credential-not-real"))
        with patch("requests.post", return_value=response) as post:
            turn = self.run_turn("分析平安银行的基本面", model=model)
        self.assertEqual(turn.status, "needs_clarification")
        self.assertEqual(turn.code, "ungrounded_product_intent")
        self.assertEqual((post.call_count, turn.model_calls_attempted), (1, 1))
        self.assertEqual(self.data.calls, [])
        self.assertIsNone(self.session.product_saved)


if __name__ == "__main__":
    unittest.main()
