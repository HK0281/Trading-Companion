"""Real API/service/HTTP orchestration with artificial sources; no external I/O or secrets."""

from copy import deepcopy
import json
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import offline_network
from trading_companion.api import create_app
from trading_companion.api_service import ApiProblem, ResearchApiService
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


QUESTION = "分析平安银行的基本面"


class ProductApiTests(unittest.TestCase):
    def setUp(self):
        self.model, self.data, self.source_factory = ProductModelFixture(), CompanyDataFixture(), MemorySourceFactory()
        self.directory_loader = Mock(return_value=directory())
        self.context_loader = Mock(side_effect=AssertionError("No unrelated market-context refresh"))
        for guard in (patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")),
                      patch("trading_companion.product_research._build_source", side_effect=self.source_factory)):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        self.service = ResearchApiService(model_factory=lambda: self.model, directory_loader=self.directory_loader,
            data_mode="tencent", product_data_service=self.data, reference_date=NOW.date(),
            clock=lambda: 100.0, wall_clock=lambda: NOW, market_context_loader=self.context_loader)
        self.addCleanup(self.service.close)
        self.credentials = self.service.create_session()
        self.sid, self.token = self.credentials["session_id"], self.credentials["access_token"]
        self.entry = self.service._entries[self.sid]
        self.headers = {"Authorization": "Bearer " + self.token}
        self.messages_path = "/v1/sessions/" + self.sid + "/messages"

    def send(self, question=QUESTION, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def assert_company_dto(self, turn):
        self.assertEqual(turn["stage"], "company_research")
        company = turn["company_research"]
        self.assertIsInstance(company, dict)
        self.assertEqual(company["version"], "product-research-answer-v1")
        self.assertFalse(company["trading_enabled"])
        self.assertEqual(company["securities"], [{"instrument_id": "000001.SZ", "name": "平安银行"}])
        self.assertEqual(turn["answer"], "\n\n".join(company["paragraphs"]))
        self.assertEqual(company["text"], turn["answer"])
        self.assertEqual(company["daily_cutoff"], "2026-09-25")
        self.assertIsNone(turn["conversation"])
        self.assertIsNone(turn["research_workflow"])
        for key in ("model_responses", "model_draft", "product_bundle", "product_plan", "user_question"):
            self.assertNotIn(key, turn)
        encoded = json.dumps(turn)
        self.assertNotIn("UNTRUSTED_RESEARCH_CONTEXT", encoded)
        self.assertNotIn("interpret_product_research", encoded)
        self.context_loader.assert_not_called()
        return company

    def test_normal_service_uses_company_data_service_and_explicit_public_dto(self):
        turn = self.send()["result"]
        self.assertEqual((turn["status"], turn["code"]), ("completed", "company_research_completed"))
        company = self.assert_company_dto(turn)
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (2, 0))
        self.assertEqual(turn["runtime"]["limits"]["analyses"], 3)
        self.assertEqual(self.credentials["data_mode"], "tencent")
        self.assertTrue(company["evidence"])
        self.assertEqual(self.directory_loader.call_count, 1)
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(self.source_factory.reads, [])
        self.assertIsNone(turn["chart"])
        self.assertIsNone(turn["parameters"])

    def test_http_response_model_preserves_company_evidence_and_source_fields(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            response = client.post(self.messages_path, headers=self.headers,
                                   json={"request_id": uuid4().hex, "question": QUESTION})
            self.assertEqual(response.status_code, 200)
            company = self.assert_company_dto(response.json()["result"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            evidence = next(item for item in company["evidence"] if item["kind"] == "financial")
            self.assertEqual(evidence["source"], "fixture_financial")
            self.assertEqual(evidence["url"], "https://example.invalid/fixture")
            self.assertEqual(evidence["temporal_scope"], "published_before_cutoff")
            self.assertEqual(evidence["retrieved_at"], NOW.isoformat())
            self.assertEqual((evidence["raw_value"], evidence["unit"]), (123000000.0, "cny"))
            self.assertEqual(evidence["report_period"], "2026-06-30")
            coverage = company["coverage"][0]["sections"]
            self.assertEqual({section["source"] for section in coverage}, {"fixture_profile", "fixture_financial"})
            self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 2)

    def test_broad_research_exposes_matching_chart_and_price_source(self):
        turn = self.send("分析平安银行，预测接下来方向")["result"]
        company = self.assert_company_dto(turn)
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["parameters"]["source"], "tencent")
        self.assertEqual(turn["chart"]["source"], "tencent")
        self.assertEqual(turn["chart"]["data_authenticity"], "provider_reported")
        self.assertEqual(turn["chart"]["request_id"], turn["report"]["request_id"])
        self.assertEqual({section["kind"] for section in company["coverage"][0]["sections"]},
                         {"profile", "financial", "news", "announcements", "technical"})
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (2, 1))

    def test_idempotent_service_replay_is_detached_and_does_not_repeat_any_work(self):
        key = uuid4().hex
        initial = self.send(request_id=key)
        expected = deepcopy(initial["result"])
        initial["result"]["company_research"]["coverage"].clear()
        initial["result"]["answer"] = "changed-by-client"
        replay = self.send(request_id=key)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], expected)
        self.assertEqual((len(self.model.calls), len(self.data.calls), len(self.source_factory.reads)), (2, 1, 0))
        with self.assertRaises(ApiProblem) as raised:
            self.send("分析浦发银行的基本面", request_id=key)
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "request_id_conflict"))

    def test_http_idempotency_replays_company_result_and_rejects_conflicting_question(self):
        key = uuid4().hex
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            first = client.post(self.messages_path, headers=self.headers, json={"request_id": key, "question": QUESTION})
            replay = client.post(self.messages_path, headers=self.headers, json={"request_id": key, "question": QUESTION})
            conflict = client.post(self.messages_path, headers=self.headers,
                                   json={"request_id": key, "question": "分析浦发银行的基本面"})
            self.assertEqual((first.status_code, replay.status_code, conflict.status_code), (200, 200, 409))
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(first.json()["result"], replay.json()["result"])
            self.assertEqual(conflict.json()["error"]["code"], "request_id_conflict")
            self.assertEqual((len(self.model.calls), len(self.data.calls)), (2, 1))

    def test_product_followup_technical_domain_keeps_identity_and_reads_new_market_snapshot(self):
        self.send()
        turn = self.send("那技术面呢")["result"]
        self.assertEqual((turn["status"], turn["code"]), ("completed", "company_research_completed"))
        company = self.assert_company_dto(turn)
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (2, 1))
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual([request.instrument_id for request in self.source_factory.reads], ["000001.SZ"])
        self.assertEqual({section["kind"] for section in company["coverage"][0]["sections"]}, {"technical"})
        self.assertTrue(all(item["kind"] == "technical" for item in self.model.contexts[-1]["evidence"]))

    def test_product_why_followup_uses_saved_evidence_and_one_tool_free_call(self):
        initial = self.send()["result"]
        turn = self.send("为什么这么判断")["result"]
        self.assertEqual((turn["status"], turn["code"]), ("completed", "product_followup_answered"))
        company = self.assert_company_dto(turn)
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (1, 0))
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(len(self.source_factory.reads), 0)
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(self.model.calls[-1][1], ())
        self.assertEqual(company["coverage"], initial["company_research"]["coverage"])

    def test_product_direction_followup_inherits_stock_and_collects_current_multidomain_data(self):
        self.send()
        turn = self.send("它还会涨吗")["result"]
        self.assertEqual((turn["status"], turn["code"]), ("completed", "company_research_completed"))
        company = self.assert_company_dto(turn)
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (2, 1))
        self.assertEqual(len(self.data.calls), 2)
        self.assertEqual(self.data.calls[-1][0].instrument_id, "000001.SZ")
        self.assertEqual({section["kind"] for section in company["coverage"][0]["sections"]},
                         {"profile", "financial", "news", "announcements", "technical"})
        self.assertIn("平安银行", self.model.calls[-2][0][1].content)

    def test_http_history_preserves_company_result_without_new_data_or_model_calls(self):
        initial = self.send()["result"]
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            response = client.get("/v1/sessions/" + self.sid + "/result", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            turn = response.json()["result"]
            self.assertEqual(turn["code"], "product_saved_result")
            self.assertEqual(turn["company_research"], json.loads(json.dumps(initial["company_research"])))
            self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (0, 0))
            self.assertEqual((len(self.model.calls), len(self.data.calls)), (2, 1))

    def test_service_cancellation_never_saves_late_answer_and_replay_is_free(self):
        key = uuid4().hex
        self.model.on_synthesis = lambda: self.service.cancel(self.sid, self.token, key)
        turn = self.send(request_id=key)["result"]
        self.assertEqual((turn["status"], turn["code"]), ("cancelled", "turn_cancelled"))
        self.assertIsNone(turn["answer"])
        self.assertIsNone(turn.get("company_research"))
        self.assertIsNone(self.entry.session.product_saved)
        replay = self.send(request_id=key)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], turn)
        self.assertEqual((len(self.model.calls), len(self.data.calls)), (2, 1))

    def test_http_cancel_endpoint_stops_active_product_synthesis(self):
        key, entered, release, responses, errors = uuid4().hex, Event(), Event(), [], []

        def hold_synthesis():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release synthetic model response")

        self.model.on_synthesis = hold_synthesis
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            def send_in_worker():
                try:
                    responses.append(client.post(self.messages_path, headers=self.headers,
                        json={"request_id": key, "question": QUESTION}))
                except Exception as error:
                    errors.append(error)

            worker = Thread(target=send_in_worker, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), "Synthetic product synthesis was not reached")
                cancelled = client.post("/v1/sessions/" + self.sid + "/cancel", headers=self.headers,
                                        json={"request_id": key})
                self.assertEqual(cancelled.status_code, 202)
                self.assertEqual(cancelled.json()["status"], "cancellation_requested")
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0].status_code, 200)
            turn = responses[0].json()["result"]
            self.assertEqual((turn["status"], turn["code"]), ("cancelled", "turn_cancelled"))
            self.assertIsNone(turn["answer"])
            self.assertIsNone(self.entry.session.product_saved)

    def test_partial_news_status_and_remaining_source_details_survive_http_projection(self):
        self.data.unavailable.add("news")
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            response = client.post(self.messages_path, headers=self.headers,
                json={"request_id": uuid4().hex, "question": "分析平安银行的基本面和新闻"})
            self.assertEqual(response.status_code, 200)
            turn = response.json()["result"]
            self.assertEqual(turn["status"], "limited")
            company = self.assert_company_dto(turn)
            news = next(section for section in company["coverage"][0]["sections"] if section["kind"] == "news")
            self.assertEqual((news["status"], news["source"]), ("unavailable", "fixture_news"))
            self.assertIn("financial", {item["kind"] for item in self.model.contexts[0]["evidence"]})


if __name__ == "__main__":
    unittest.main()
