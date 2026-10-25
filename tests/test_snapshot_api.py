"""Opt-in API snapshot persistence uses only temporary databases and artificial research."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import offline_network
from trading_companion.api import create_app
from trading_companion.api_service import ResearchApiService
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_INSTRUCTION, GENERAL_KNOWLEDGE_VERSION
from trading_companion.snapshot_store import ResearchSnapshotStore
from trading_companion.tool_model import ToolModelResponse
from tests.test_product_agent import ProductModelFixture
from tests.test_product_api import QUESTION
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


PRIVATE_QUESTION = "private-user-question-not-for-the-ledger"
PRIVATE_ANSWER = "private-model-answer-not-for-the-ledger"
PRIVATE_ERROR = "private-database-error-not-for-the-client"


class SnapshotModelFixture(ProductModelFixture):
    def complete(self, messages, tools):
        if messages[0].content == GENERAL_KNOWLEDGE_INSTRUCTION:
            self.calls.append((tuple(messages), tuple(tools)))
            return ToolModelResponse("complete", json.dumps({"version": GENERAL_KNOWLEDGE_VERSION,
                "paragraphs": ["财报可对照利润、资产负债与现金流，理解经营表现。"]}, ensure_ascii=False))
        return super().complete(messages, tools)


class SnapshotApiTests(unittest.TestCase):
    def setUp(self):
        self.model, self.data, self.source_factory = SnapshotModelFixture(), CompanyDataFixture(), MemorySourceFactory()
        for guard in (patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")),
                      patch("trading_companion.product_research._build_source", side_effect=self.source_factory)):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "research.sqlite3"
        self.store = ResearchSnapshotStore(self.path, clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.control_observations = []
        original_save = self.store.save

        def save_after_finished(bundle):
            control = self.entry.control
            self.assertIsNotNone(control)
            metadata = control.to_dict()
            self.assertEqual(metadata["state"], "finished")
            self.assertIsNone(metadata["stop_code"])
            self.control_observations.append(metadata)
            return original_save(bundle)

        self.save_spy = Mock(side_effect=save_after_finished)
        guard = patch.object(self.store, "save", self.save_spy)
        guard.start()
        self.addCleanup(guard.stop)
        self.service = self.build(snapshot_store=self.store)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        self.entry = self.service._entries[self.sid]
        self.headers = {"Authorization": "Bearer " + self.token}

    def build(self, **kwargs):
        service = ResearchApiService(model_factory=lambda: self.model, directory_loader=lambda now: directory(),
            data_mode="tencent", product_data_service=self.data, reference_date=NOW.date(),
            clock=lambda: 100.0, wall_clock=lambda: NOW, **kwargs)
        self.addCleanup(service.close)
        return service

    def send(self, question=QUESTION, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def test_new_research_persists_after_turn_finishes_and_exposes_loadable_identifier(self):
        turn = self.send()["result"]
        self.assertEqual(turn["status"], "completed")
        company = turn["company_research"]
        self.assertEqual(company["snapshot_status"], "saved")
        identifier = company["snapshot_id"]
        self.assertRegex(identifier, r"^[0-9a-f]{64}$")
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(len(self.control_observations), 1)
        payload = self.store.load(identifier)
        self.assertEqual(payload["plan"]["securities"], [{"instrument_id": "000001.SZ", "name": "平安银行"}])
        self.assertEqual({item["kind"] for item in payload["companies"][0]["evidence"]}, {"profile", "financial"})
        self.assertEqual(len(self.store.list()), 1)

    def test_http_preserves_snapshot_reference_without_exposing_database_location(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            response = client.post("/v1/sessions/" + self.sid + "/messages", headers=self.headers,
                                   json={"request_id": uuid4().hex, "question": QUESTION})
            self.assertEqual(response.status_code, 200)
            company = response.json()["result"]["company_research"]
            self.assertEqual(company["snapshot_status"], "saved")
            self.assertEqual(self.store.load(company["snapshot_id"])["snapshot_id"], company["snapshot_id"])
            self.assertNotIn(str(self.path), response.text)
            self.assertNotIn("sqlite3", response.text)

    def test_snapshot_failure_keeps_accepted_natural_answer_and_does_not_leak_errors(self):
        self.save_spy.side_effect = OSError(PRIVATE_ERROR)
        turn = self.send()["result"]
        self.assertEqual((turn["status"], turn["code"]), ("completed", "company_research_completed"))
        company = turn["company_research"]
        self.assertEqual(company["status"], "accepted")
        self.assertEqual(company["snapshot_status"], "unavailable")
        self.assertIsNone(company.get("snapshot_id"))
        self.assertTrue(turn["answer"])
        self.assertNotIn(PRIVATE_ERROR, json.dumps(turn))
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(self.store.list(), ())

    def test_idempotent_replay_and_explicit_history_never_save_again(self):
        key = uuid4().hex
        initial = self.send(request_id=key)
        replay = self.send(request_id=key)
        history = self.service.history(self.sid, self.token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], initial["result"])
        self.assertEqual(history["result"]["code"], "product_saved_result")
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual((len(self.model.calls), len(self.data.calls)), (2, 1))

    def test_saved_explanation_and_knowledge_do_not_duplicate_research_snapshots(self):
        self.send()
        explanation = self.send("为什么这么判断")["result"]
        fixed_knowledge = self.send("RSI是什么")["result"]
        general_knowledge = self.send("如何阅读财报")["result"]
        self.assertEqual(explanation["code"], "product_followup_answered")
        self.assertEqual(fixed_knowledge["code"], "indicator_knowledge_answered")
        self.assertEqual(general_knowledge["code"], "general_knowledge_answered")
        self.assertEqual(self.save_spy.call_count, 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(len(self.data.calls), 1)

    def test_cancelled_synthesis_cannot_persist_late_result(self):
        key = uuid4().hex
        self.model.on_synthesis = lambda: self.service.cancel(self.sid, self.token, key)
        turn = self.send(request_id=key)["result"]
        self.assertEqual((turn["status"], turn["code"]), ("cancelled", "turn_cancelled"))
        self.assertIsNone(turn["answer"])
        self.save_spy.assert_not_called()
        self.assertEqual(self.store.list(), ())
        self.assertIsNone(self.entry.session.product_saved)

    def test_missing_evidence_and_pending_clarification_do_not_write_snapshots(self):
        self.data.unavailable.update(("profile", "financial"))
        turn = self.send()["result"]
        self.assertEqual(turn["status"], "failed")
        self.save_spy.assert_not_called()
        self.data.unavailable.clear()
        self.model.mentions = ("平安",)
        pending = self.send("分析平安的基本面")["result"]
        self.assertEqual(pending["status"], "needs_clarification")
        self.save_spy.assert_not_called()
        self.assertEqual(self.store.list(), ())

    def test_partial_domains_can_be_saved_with_their_coverage_intact(self):
        self.data.unavailable.add("news")
        turn = self.send("分析平安银行的基本面和新闻")["result"]
        self.assertEqual(turn["status"], "limited")
        self.assertEqual(turn["company_research"]["snapshot_status"], "saved")
        payload = self.store.load(turn["company_research"]["snapshot_id"])
        coverage = payload["companies"][0]["coverage"]
        self.assertTrue(any(item["kind"] == "news" and item["status"] == "unavailable" for item in coverage))
        self.assertIn("financial", {item["kind"] for item in payload["companies"][0]["evidence"]})

    def test_questions_model_output_and_answers_are_absent_from_snapshot_payload_and_database(self):
        self.model.synthesis_response = ToolModelResponse("complete", json.dumps({
            "version": "product-research-answer-v1", "paragraphs": [
                {"text": PRIVATE_ANSWER + "对应人工研究结果。", "evidence_ids": ["E1"]},
                {"text": "判断仍需结合经营表现和相反证据。", "evidence_ids": ["E1"]},
            ]}, ensure_ascii=False))
        turn = self.send(QUESTION + "，" + PRIVATE_QUESTION)["result"]
        self.assertIn(PRIVATE_ANSWER, turn["answer"])
        payload = self.store.load(turn["company_research"]["snapshot_id"])
        encoded, raw = json.dumps(payload), self.path.read_bytes()
        for marker in (PRIVATE_QUESTION, PRIVATE_ANSWER):
            self.assertNotIn(marker, encoded)
            self.assertNotIn(marker.encode(), raw)
        for key in ("user_question", "model_response", "context"):
            self.assertNotIn(key, payload["plan"])
        self.assertNotIn("answer", payload)
        self.assertNotIn("paragraphs", payload)

    def test_disabled_persistence_never_opens_a_database_on_creation_or_message(self):
        with patch("sqlite3.connect", side_effect=AssertionError("Disabled persistence must not open a database")):
            service = self.build()
            credentials = service.create_session()
            result = service.message(credentials["session_id"], credentials["access_token"], uuid4().hex, QUESTION)
        self.assertEqual(result["result"]["status"], "completed")
        self.save_spy.assert_not_called()
        self.assertEqual(self.store.list(), ())


if __name__ == "__main__":
    unittest.main()
