"""Offline HTTP/service integration for general education and existing research memory."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import offline_network
from trading_companion.api import create_app
from trading_companion.api_service import ApiLimits, ApiProblem, ResearchApiService
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.followup_demo import ConversationDemoModel, QUESTION as RESEARCH_QUESTION
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_INSTRUCTION, GENERAL_KNOWLEDGE_VERSION
from trading_companion.tool_model import ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
QUESTION = "怎么用PE/PB看估值"
TEXT = "PE比较股价与每股收益，PB比较股价与每股净资产。比较时还要看盈利和资产口径。"
PRIVATE = "private-general-knowledge-marker-never-export"


class EducationAndResearchModel:
    def __init__(self):
        self.calls = []
        self.delegate = ConversationDemoModel()
        self.knowledge_response = ToolModelResponse("complete", json.dumps({
            "version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": [TEXT],
        }, ensure_ascii=False), usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        self.error = self.callback = None

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        self.calls.append((tuple(messages), tuple(tools)))
        if messages[0].content == GENERAL_KNOWLEDGE_INSTRUCTION:
            if self.callback is not None:
                self.callback()
            if self.error is not None:
                raise self.error
            return self.knowledge_response
        return self.delegate.complete(messages, tools=tools)


class GeneralKnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market/news/directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No provider HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        self.source, self.directory = load_development_fixtures()
        self.loader = Mock(side_effect=DataSourceError(PRIVATE))
        self.context_loader = Mock(side_effect=AssertionError("Knowledge does not load market context"))
        self.model = EducationAndResearchModel()
        self.install()

    def install(self, **overrides):
        options = dict(model_factory=lambda: self.model, directory_loader=self.loader,
                       development_source=self.source, reference_date=REFERENCE_DATE,
                       market_context_loader=self.context_loader,
                       clock=lambda: 100.0, wall_clock=lambda: NOW)
        options.update(overrides)
        self.service = ResearchApiService(**options)
        self.addCleanup(self.service.close)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        self.entry = self.service._entries[self.sid]

    def send(self, question=QUESTION, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def assert_no_research_views(self, turn):
        for key in ("parameters", "report", "chart", "market_context", "research_workflow"):
            self.assertIsNone(turn[key], key)

    def test_unusable_directory_does_not_block_education_or_cause_market_access(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No price reads")):
            turn = self.send()["result"]
        self.assertEqual((turn["status"], turn["code"], turn["stage"]),
                         ("completed", "general_knowledge_answered", "knowledge"))
        self.assertEqual(turn["answer"], TEXT)
        self.assert_no_research_views(turn)
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (1, 0))
        self.assertEqual((turn["runtime"]["responses_received"], turn["runtime"]["observed_tokens"]), (1, 30))
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0][1], ())
        self.loader.assert_not_called()
        self.context_loader.assert_not_called()

    def test_http_returns_a_plain_answer_through_the_existing_message_protocol(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            response = client.post("/v1/sessions/" + self.sid + "/messages",
                headers={"Authorization": "Bearer " + self.token},
                json={"request_id": uuid4().hex, "question": "如何阅读财报"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["answer"], TEXT)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(PRIVATE, response.text)
        self.loader.assert_not_called()

    def test_idempotent_replay_is_detached_and_does_not_call_the_model_twice(self):
        key = uuid4().hex
        initial = self.send(request_id=key)
        expected = deepcopy(initial["result"])
        initial["result"]["answer"] = PRIVATE
        replay = self.send(request_id=key)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], expected)
        replay["result"]["runtime"]["limits"]["model_calls"] = 99
        self.assertEqual(self.send(request_id=key)["result"], expected)
        self.assertEqual(len(self.model.calls), 1)

    def test_named_security_news_figures_and_forecasts_keep_directory_gates(self):
        for question in ("中际旭创市盈率多少", "中际旭创PE怎么理解", "解释中际旭创财报",
                         "分析平安银行的走势", "预测股票下周走势", "给我股市最新新闻",
                         "PE是什么，顺便解释平安银行的估值"):
            with self.subTest(question=question), self.assertRaises(ApiProblem) as raised:
                self.send(question)
            self.assertEqual(raised.exception.code, "directory_unusable")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.loader.call_count, 7)

    def test_fixed_indicator_definition_keeps_its_existing_zero_model_path(self):
        self.loader.side_effect = None
        self.loader.return_value = self.directory
        turn = self.send("RSI是什么")["result"]
        self.assertEqual(turn["code"], "indicator_knowledge_answered")
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (0, 0))
        self.assertIn("RSI", turn["answer"])
        self.assert_no_research_views(turn)
        self.assertEqual(self.model.calls, [])

    def test_general_education_does_not_overwrite_saved_research_or_attach_its_chart(self):
        self.loader.side_effect = None
        self.loader.return_value = self.directory
        self.context_loader.side_effect = None
        self.context_loader.return_value = None
        original = self.send(RESEARCH_QUESTION)["result"]
        self.assertEqual(original["code"], "research_completed")
        state = deepcopy(self.entry.session.to_dict())
        contexts = deepcopy(self.entry.market_contexts)
        self.loader.side_effect = DataSourceError(PRIVATE)
        self.context_loader.reset_mock()
        turn = self.send()["result"]
        self.assertEqual(turn["answer"], TEXT)
        self.assert_no_research_views(turn)
        self.assertEqual(self.entry.session.to_dict(), state)
        self.assertEqual(self.entry.market_contexts, contexts)
        self.context_loader.assert_not_called()
        self.loader.side_effect = None
        history = self.service.history(self.sid, self.token)["result"]
        self.assertEqual(history["code"], "saved_result_shown")
        self.assertEqual(history["chart"]["request_id"], original["chart"]["request_id"])

    def test_provider_failure_returns_a_short_fallback_without_raw_errors_or_retry(self):
        self.model.error = ModelCallError(PRIVATE, PRIVATE)
        turn = self.send()["result"]
        self.assertEqual((turn["status"], turn["code"]), ("failed", "general_knowledge_model_unavailable"))
        self.assertIsInstance(turn["answer"], str)
        self.assertLess(len(turn["answer"]), 100)
        self.assertNotIn(PRIVATE, json.dumps(turn))
        self.assertEqual(len(self.model.calls), 1)
        self.assert_no_research_views(turn)
        self.loader.assert_not_called()

    def test_invalid_prose_is_not_exposed_through_api(self):
        self.model.knowledge_response = ToolModelResponse("complete", PRIVATE + "<script>bad</script>")
        turn = self.send()["result"]
        self.assertEqual(turn["code"], "general_knowledge_output_invalid")
        self.assertNotIn(PRIVATE, json.dumps(turn))
        self.assertNotIn("<script>", json.dumps(turn))
        self.assertEqual(len(self.model.calls), 1)

    def test_cancellation_discards_the_late_knowledge_answer_and_replays_without_calling_again(self):
        key = uuid4().hex
        self.model.callback = lambda: self.service.cancel(self.sid, self.token, key)
        turn = self.send(request_id=key)["result"]
        self.assertEqual((turn["status"], turn["code"]), ("cancelled", "turn_cancelled"))
        self.assertIsNone(turn["answer"])
        self.assertNotIn(TEXT, json.dumps(turn))
        self.assert_no_research_views(turn)
        self.assertEqual(self.send(request_id=key)["result"], turn)
        self.assertEqual(len(self.model.calls), 1)

    def test_zero_turn_budget_never_forwards_a_knowledge_request(self):
        self.install(control_factory=lambda: TurnControl(TurnLimits(model_calls=0)))
        turn = self.send()["result"]
        self.assertEqual(turn["code"], "model_call_budget_exhausted")
        self.assertEqual(turn["model_calls_attempted"], 0)
        self.assertIsNone(turn["answer"])
        self.assertEqual(self.model.calls, [])

    def test_session_model_allowance_applies_to_knowledge_without_analysis_reservations(self):
        self.install(limits=ApiLimits(model_calls=1))
        self.assertEqual(self.send()["result"]["code"], "general_knowledge_answered")
        second = self.send()["result"]
        self.assertEqual(second["code"], "general_knowledge_model_unavailable")
        self.assertEqual(second["analyses_used"], 0)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 1)

    def test_workflow_disabled_preserves_the_old_route_instead_of_enabling_education(self):
        self.install(workflow_enabled=False)
        with self.assertRaises(ApiProblem) as raised:
            self.send()
        self.assertEqual(raised.exception.code, "directory_unusable")
        self.assertEqual(self.model.calls, [])


if __name__ == "__main__":
    unittest.main()
