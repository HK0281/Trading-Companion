"""Holding preferences stay local; explicit management cannot change research state."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_companion.agent import AgentTurnResult
from trading_companion.api import create_app, create_demo_service
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.memory_store import SqliteConversationStore
from trading_companion.news_triage_shadow import NewsTitleCapture
from trading_companion.personal_memory import PREFERENCES_VERSION
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_trace import JsonlTraceSink


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
BASE = "/v1/personal/preferences"


class PreferencesApiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SqlitePersonalMemoryStore(self.root / "personal.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.history = SqliteConversationStore(self.root / "history.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.history.close)
        self.directory = SecurityDirectory((Security("000001.SZ", "平安银行"),
            Security("600000.SH", "浦发银行")), "offline_test", NOW)
        self.loader = Mock(side_effect=lambda _: self.directory)
        self.model = Mock()
        self.model.complete.side_effect = AssertionError("Management must not call a model")
        self.capture = NewsTitleCapture(self.root / "titles")
        capture_guard = patch.object(self.capture, "capture", side_effect=AssertionError("No title capture for management"))
        self.capture_spy = capture_guard.start()
        self.addCleanup(capture_guard.stop)
        self.service = ResearchApiService(model_factory=lambda: self.model, directory_loader=self.loader,
            provider="deepseek", data_mode="tencent", wall_clock=lambda: NOW,
            personal_memory_store=self.store, conversation_store=self.history,
            conversation_context=True, trace_sink=JsonlTraceSink(self.root / "trace"),
            headline_capture=self.capture)
        self.addCleanup(self.service.close)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        for guard in (patch("requests.get", side_effect=AssertionError("No HTTP")),
                      patch("requests.post", side_effect=AssertionError("No HTTP"))):
            guard.start()
            self.addCleanup(guard.stop)

    def send(self, question, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def create(self, **changes):
        return self.service.patch_preferences("000001.SZ", changes or {"holding_status": "held"}, None)

    def test_http_partial_updates_strict_fields_conflicts_delete_and_clear(self):
        with TestClient(create_app(service=self.service), base_url="http://127.0.0.1") as client:
            empty = client.get(BASE).json()
            self.assertEqual((empty["version"], empty["enabled"], empty["limit"], empty["items"]),
                             (PREFERENCES_VERSION, True, 50, []))
            saved = client.patch(BASE + "/000001.SZ", json={"expected_revision": None,
                "changes": {"holding_status": "unknown", "horizon": "medium"}}).json()
            item = saved["items"][0]
            self.assertIsNone(item["risk"])
            self.assertEqual(item["holding_status"], "unknown")
            updated = client.patch(BASE + "/000001.SZ", json={"expected_revision": item["revision"],
                "changes": {"risk": "low", "horizon": None}}).json()
            row = updated["items"][0]
            self.assertEqual((row["holding_status"], row["horizon"], row["risk"]), ("unknown", None, "low"))
            for operation in (
                lambda: client.patch(BASE + "/000001.SZ", json={"expected_revision": item["revision"], "changes": {"risk": "high"}}),
                lambda: client.delete(BASE + "/000001.SZ", headers={"If-Match": item["revision"]}),
                lambda: client.delete(BASE, headers={"If-Match": saved["collection_revision"]}),
            ):
                response = operation()
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "preference_revision_conflict")
            self.assertEqual(client.delete(BASE + "/000001.SZ", headers={"If-Match": row["revision"]}).json()["items"], [])
            self.assertEqual(client.delete(BASE + "/000001.SZ", headers={"If-Match": row["revision"]}).status_code, 200)
            current = client.get(BASE).json()
            self.assertEqual(client.delete(BASE, headers={"If-Match": current["collection_revision"]}).json()["items"], [])
            for changes in ({}, {"risk": "unbounded"}, {"holding_status": False}, {"cost": 10},
                            {"position": 20}, {"risk": None}):
                response = client.patch(BASE + "/000001.SZ", json={"expected_revision": None, "changes": changes})
                self.assertEqual(response.status_code, 422, changes)
                self.assertNotIn("position", response.text)
            self.assertEqual(client.delete(BASE).status_code, 422)
            self.assertEqual(client.delete(BASE, headers={"If-Match": "x" * 200}).status_code, 422)
            self.assertEqual(client.patch(BASE + "/000001.SZ", json={"changes": {"risk": "low"}}).status_code, 422)
            self.assertEqual(client.patch(BASE + "/000001.SZ", json={"expected_revision": None,
                "changes": {"risk": "low"}}, headers={"Origin": "https://untrusted.example"}).status_code, 403)

    def test_existing_identity_maintainable_with_stale_directory_and_deletion_scopes(self):
        saved = self.create(horizon="long", risk="high")
        row = saved["items"][0]
        self.service.add_watchlist("000001.SZ")
        self.directory = replace(self.directory, retrieved_at=NOW - timedelta(days=10))
        self.loader.reset_mock()
        self.service.patch_preferences("000001.SZ", {"holding_status": "not_held"}, row["revision"])
        self.loader.assert_not_called()
        self.service.clear_watchlist()
        self.assertEqual(len(self.service.preferences()["items"]), 1)
        self.service.clear_conversations()
        self.service.reset(self.sid, self.token)
        self.assertEqual(len(self.service.preferences()["items"]), 1)
        result = self.send("把平安银行的期限偏好设为短线")["result"]
        self.assertEqual(result["personal_memory"]["items"][0]["horizon"], "short")
        self.loader.assert_not_called()
        with self.assertRaises(ApiProblem) as caught:
            self.service.patch_preferences("600000.SH", {"holding_status": "held"}, None)
        self.assertEqual(caught.exception.code, "directory_unusable")
        current = self.service.preferences()
        self.service.clear_preferences(current["collection_revision"])
        self.assertEqual(self.service.preferences()["items"], [])

    def test_management_local_history_trace_and_state_isolation(self):
        entry = self.service._entries[self.sid]
        pending, subject = object(), object()
        entry.session.product_pending, entry.session.latest_research = pending, subject
        entry.chart_cache["existing"] = {"keep": True}
        for question in ("记住我持有平安银行", "把平安银行的期限偏好设为中线", "把平安银行的风险偏好设为谨慎",
                         "查看持仓与偏好", "查看平安银行的持仓与偏好", "记住我的成本价10元仓位20%",
                         "记住我持有平安银行并分析走势", "删除平安银行的持仓与偏好"):
            result = self.send(question)["result"]
            self.assertEqual((result["stage"], result["model_calls_attempted"], result["analyses_used"]),
                             ("personal_memory", 0, 0))
            self.assertEqual(result["personal_memory"]["version"], PREFERENCES_VERSION)
        row = self.service.preferences()["items"][0]
        self.assertEqual((row["holding_status"], row["horizon"], row["risk"]), ("held", "medium", "low"))
        self.assertEqual(entry.digests, [])
        self.assertEqual(self.history.list("local", data_mode="tencent"), [])
        self.assertIs(entry.session.product_pending, pending)
        self.assertIs(entry.session.latest_research, subject)
        self.assertIn("existing", entry.chart_cache)
        for path in (self.root / "trace").glob("*.jsonl"):
            for private in ("平安银行", "000001.SZ", "holding_status", '"held"', '"medium"', '"low"', "成本价", "20%"):
                self.assertNotIn(private, path.read_text(encoding="utf-8"))
        self.model.complete.assert_not_called()
        self.capture_spy.assert_not_called()
        self.assertEqual(self.service.watchlist()["items"], [])

    def test_ambiguous_confirmation_replay_and_cancel(self):
        result = self.send("记住我未持有平安")["result"]
        self.assertEqual(result["personal_memory"]["status"], "needs_confirmation")
        self.assertEqual(result["candidates"], [])
        reply = result["personal_memory"]["candidates"][0]["reply"]
        self.assertEqual(reply, "把000001.SZ的持有状态设为未持有")
        rid = uuid4().hex
        first = self.send(reply, rid)
        item = self.service.preferences()["items"][0]
        self.service.remove_preferences(item["instrument_id"], item["revision"])
        self.assertEqual(self.send(reply, rid)["result"], first["result"])
        self.assertEqual(self.service.preferences()["items"], [])
        rid = uuid4().hex
        self.loader.side_effect = lambda _: (self.service.cancel(self.sid, self.token, rid), self.directory)[1]
        self.assertEqual(self.send("记住我持有平安银行", rid)["result"]["status"], "cancelled")
        self.assertEqual(self.service.preferences()["items"], [])

    def test_create_with_deleted_old_snapshot_reports_conflict_without_false_store_failure(self):
        self.create()
        original = self.store.list_preferences
        def stale_snapshot(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            row = snapshot["items"][0]
            self.store.remove_preferences(instrument_id=row["instrument_id"], expected_revision=row["revision"])
            return snapshot
        with patch.object(self.store, "list_preferences", side_effect=stale_snapshot), self.assertRaises(ApiProblem) as caught:
            self.service.patch_preferences("000001.SZ", {"risk": "low"}, None)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "preference_revision_conflict"))
        self.assertEqual(self.service.preferences()["items"], [])

    def test_disabled_dev_and_exhausted_research_do_not_expose_or_block_panel(self):
        self.create()
        entry = self.service._entries[self.sid]
        entry.messages_used = self.service.limits.messages
        self.assertEqual(len(self.service.preferences()["items"]), 1)
        with TestClient(create_app(service=self.service, development_service=create_demo_service()),
                        base_url="http://127.0.0.1") as client:
            disabled = client.get("/dev-api" + BASE).json()
            self.assertEqual(disabled, dict(version=PREFERENCES_VERSION, enabled=False, limit=50,
                                           items=[], collection_revision=None))
            self.assertEqual(client.patch("/dev-api" + BASE + "/000001.SZ",
                json={"expected_revision": None, "changes": {"risk": "low"}}).status_code, 404)
        self.model.complete.assert_not_called()

    def test_save_response_refresh_failure_reports_uncertainty_not_false_failure(self):
        original = self.store.list_preferences
        calls = 0
        def read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                from trading_companion.personal_memory import PersonalMemoryError
                raise PersonalMemoryError("personal_memory_store_unavailable")
            return original(*args, **kwargs)
        with patch.object(self.store, "list_preferences", side_effect=read), self.assertRaises(ApiProblem) as caught:
            self.create()
        self.assertIn("变更未确认", caught.exception.message)
        self.assertEqual(len(self.service.preferences()["items"]), 1)

    def test_research_routes_never_read_stored_preferences(self):
        self.create(holding_status="held", horizon="long", risk="high")
        # The research turn retains exactly the explicit question and its normal history.
        for intent in (False, True):
            self.service._research_intent_v2_enabled = intent
            method = "_intent_turn" if intent else "_legacy_turn"
            with patch.object(self.store, "list_preferences", side_effect=AssertionError("Research read personal data")), \
                    patch.object(self.service, method, return_value=(AgentTurnResult(status="completed",
                        code="research_completed", message="research"), self.directory, NOW)) as research:
                question = "请分析000001.SZ的退出观察条件"
                response = self.send(question)
                self.assertEqual(research.call_args.args[2], question)
                self.assertIsNone(response["result"].get("personal_memory"))
                self.assertNotIn("holding_status", json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
