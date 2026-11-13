"""Local watchlist boundaries, research isolation and launcher configuration; no live data."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_companion.agent import AgentTurnResult
from trading_companion.api import create_app, create_demo_service
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel
from trading_companion.memory_store import SqliteConversationStore
from trading_companion.personal_memory import VERSION
from trading_companion.personal_memory_commands import parse_personal_memory_command
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_trace import JsonlTraceSink


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


class WatchlistApiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = SecurityDirectory((Security("000001.SZ", "平安银行"),
            Security("600000.SH", "浦发银行"), Security("000002.SZ", "万科A", ("万科",))), "offline_test", NOW)
        self.store = SqlitePersonalMemoryStore(self.root / "personal.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.conversations = SqliteConversationStore(self.root / "history.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.conversations.close)
        self.loader = Mock(side_effect=lambda _: self.directory)
        self.model = Mock()
        self.model.complete.side_effect = AssertionError("No model call for management")
        self.sink = JsonlTraceSink(self.root / "trace")
        self.service = self.build()
        self.addCleanup(self.service.close)
        self.credentials = self.service.create_session()
        self.sid, self.token = self.credentials["session_id"], self.credentials["access_token"]
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No market HTTP")),
                      patch("requests.post", side_effect=AssertionError("No model HTTP"))):
            guard.start()
            self.addCleanup(guard.stop)

    def build(self, **options):
        args = dict(model_factory=lambda: self.model, directory_loader=self.loader, provider="deepseek",
            data_mode="tencent", wall_clock=lambda: NOW, personal_memory_store=self.store,
            conversation_store=self.conversations, conversation_context=True, trace_sink=self.sink)
        args.update(options)
        return ResearchApiService(**args)

    def send(self, question, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def test_buttons_search_add_remove_clear_and_same_origin_boundary(self):
        with TestClient(create_app(service=self.service, development_service=create_demo_service()),
                        base_url="http://127.0.0.1") as client:
            base = "/v1/personal/watchlist"
            initial = client.get(base).json()
            self.assertEqual(initial, dict(version=VERSION, enabled=True, limit=50, items=[]))
            search = client.get(base + "/search", params={"q": "银行"}).json()
            self.assertEqual(len(search["candidates"]), 2)
            self.assertFalse(search["truncated"])
            added = client.post(base, json={"instrument_id": "000001.SZ"}).json()
            self.assertEqual(added["items"][0]["name"], "平安银行")
            self.assertEqual(client.post(base, json={"instrument_id": "000001.SZ"}).json(), added)
            for body in ({"instrument_id": "000001"}, {"instrument_id": "000001.SZ", "name": "untrusted"}):
                self.assertEqual(client.post(base, json=body).status_code, 422)
            self.assertEqual(client.post(base, json={"instrument_id": "999999.SZ"}).status_code, 422)
            self.assertEqual(client.post(base, json={"instrument_id": "600000.SH"},
                headers={"Origin": "https://untrusted.example"}).status_code, 403)
            response = client.get("/dev-api" + base)
            self.assertFalse(response.json()["enabled"])
            self.assertEqual(response.json()["items"], [])
            self.assertEqual(client.post("/dev-api" + base, json={"instrument_id": "000001.SZ"}).status_code, 404)
            self.assertEqual(client.delete(base + "/000001.SZ").json()["items"], [])
            client.post(base, json={"instrument_id": "000001.SZ"})
            self.assertEqual(client.delete(base).json()["items"], [])
            self.assertEqual(client.delete(base).json()["items"], [])
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.model.complete.assert_not_called()

    def test_management_preserves_research_context_and_does_not_enter_history_or_trace(self):
        entry = self.service._entries[self.sid]
        pending, saved, subject = object(), object(), object()
        entry.session.product_pending, entry.session.product_saved, entry.session.latest_research = pending, saved, subject
        entry.chart_cache["old"] = {"private": "retained"}
        entry.market_contexts["old"] = {"status": "retained"}
        for question in ("把平安银行加入自选", "查看自选", "从自选移除平安银行", "记住我的成本价10元"):
            answer = self.send(question)["result"]
            self.assertEqual(answer["stage"], "personal_memory")
            self.assertEqual(answer["model_calls_attempted"], 0)
            self.assertEqual(answer["analyses_used"], 0)
        self.assertIs(entry.session.product_pending, pending)
        self.assertIs(entry.session.product_saved, saved)
        self.assertIs(entry.session.latest_research, subject)
        self.assertIn("old", entry.chart_cache)
        self.assertIn("old", entry.market_contexts)
        self.assertEqual(entry.digests, [])
        self.assertEqual(self.conversations.list("local", data_mode="tencent"), [])
        for path in (self.root / "trace").glob("*.jsonl"):
            for secret in ("平安银行", "000001.SZ", "成本价", "10元"):
                self.assertNotIn(secret, path.read_text(encoding="utf-8"))
        self.model.complete.assert_not_called()

    def test_replay_is_original_result_not_a_second_write_or_current_list(self):
        rid = uuid4().hex
        first = self.send("把平安银行加入自选", rid)
        self.service.remove_watchlist("000001.SZ")
        replay = self.send("把平安银行加入自选", rid)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], first["result"])
        self.assertEqual(self.service.watchlist()["items"], [])
        self.assertEqual(self.service._entries[self.sid].messages_used, 1)

    def test_cancel_during_directory_read_prevents_the_management_write(self):
        rid = uuid4().hex
        def cancelled_directory(_):
            self.service.cancel(self.sid, self.token, rid)
            return self.directory
        self.loader.side_effect = cancelled_directory
        result = self.send("把平安银行加入自选", rid)["result"]
        self.assertEqual(result["code"], "turn_cancelled")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.service.watchlist()["items"], [])
        self.model.complete.assert_not_called()

    def test_partial_confirmation_carries_full_action_and_remove_needs_no_fresh_directory(self):
        response = self.send("把平安加入自选")["result"]
        payload = response["personal_memory"]
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertEqual(payload["candidates"][0]["reply"], "把000001.SZ加入自选")
        self.assertEqual(response["candidates"], [])
        self.send(payload["candidates"][0]["reply"])
        self.directory = replace(self.directory, retrieved_at=NOW - timedelta(days=10))
        self.assertEqual(self.send("把浦发银行加入自选")["result"]["code"], "directory_unusable")
        self.loader.reset_mock()
        self.assertEqual(self.send("从自选移除平安银行")["result"]["personal_memory"]["status"], "completed")
        self.loader.assert_not_called()
        self.assertEqual(self.service.watchlist()["items"], [])

    def test_disabled_and_development_do_not_touch_an_injected_personal_store(self):
        source, _ = load_development_fixtures()
        for options in ({"personal_memory_store": None}, {"data_mode": "synthetic", "development_source": source},
                        {"trace_mode": "development"}, {"trace_mode": "demo"}):
            with self.subTest(options=options):
                service = self.build(**options)
                with patch.object(self.store, "list", side_effect=AssertionError("No private reads")), \
                        patch.object(self.store, "add", side_effect=AssertionError("No private writes")):
                    self.assertFalse(service.watchlist()["enabled"])
                    credentials = service.create_session()
                    result = service.message(credentials["session_id"], credentials["access_token"],
                        uuid4().hex, "把平安银行加入自选")["result"]
                    self.assertEqual(result["code"], "personal_memory_disabled")
                    self.assertIn("--personal-memory-db", result["message"])
                    self.assertNotIn(str(self.root), result["message"])
                service.close()
        self.loader.assert_not_called()

    def test_store_failure_is_visible_and_does_not_block_research(self):
        self.store.close()
        result = self.send("查看自选")["result"]
        self.assertEqual(result["personal_memory"]["status"], "unavailable")
        with patch.object(self.service, "_legacy_turn", return_value=(
                AgentTurnResult(status="completed", code="research_completed", message="research"), self.directory, NOW)) as research:
            self.assertEqual(self.send("分析平安银行")["result"]["code"], "research_completed")
            research.assert_called_once()

    def test_research_questions_and_existing_intent_corpora_are_not_intercepted(self):
        # Existing offline questions retain their research/knowledge routes unchanged.
        from trading_companion.intent_router_task import load_corpus
        rows, _ = load_corpus()
        questions = [r["question"] for r in rows]
        for name in ("research-intent-v2.5-dev.jsonl", "research-intent-v2.6-dev.jsonl"):
            for line in (Path(__file__).parents[1] / "data/evaluation" / name).read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if "question" in value:
                    questions.append(value["question"])
        questions.extend(("分析自选股里的平安银行", "平安银行值得加入自选吗", "平安银行适合加入自选吗？"))
        for question in questions:
            with self.subTest(question=question):
                self.assertIsNone(parse_personal_memory_command(question))
        for use_intent in (False, True):
            self.service._research_intent_v2_enabled = use_intent
            method = "_intent_turn" if use_intent else "_legacy_turn"
            with patch.object(self.service, method, return_value=(AgentTurnResult(status="completed",
                    code="research_completed", message="research"), self.directory, NOW)) as research:
                for question in questions[-3:]:
                    self.assertEqual(self.send(question)["result"]["code"], "research_completed")
                self.assertEqual(research.call_count, 3)

    def test_reset_delete_history_and_new_session_keep_watchlist_and_exhausted_chat_has_panel(self):
        self.send("把平安银行加入自选")
        self.service.reset(self.sid, self.token)
        self.service.clear_conversations()
        self.service.create_session()
        entry = self.service._entries[self.sid]
        entry.messages_used = self.service.limits.messages
        with self.assertRaises(ApiProblem) as caught:
            self.send("查看自选")
        self.assertEqual(caught.exception.code, "session_message_limit")
        self.service.add_watchlist("600000.SH")
        self.assertEqual(len(self.service.watchlist()["items"]), 2)


class PersonalMemoryStartupTests(unittest.TestCase):
    def test_explicit_normal_start_persists_across_restart_and_unusable_file_is_sanitized(self):
        from trading_companion import api_server
        with TemporaryDirectory() as root:
            path = Path(root) / "private-watchlist.sqlite3"
            for iteration in range(2):
                def inspect_running(app, **kwargs):
                    with TestClient(app, base_url="http://127.0.0.1") as client:
                        base = "/v1/personal/watchlist"
                        self.assertTrue(client.get("/health").json()["personal_memory_enabled"])
                        self.assertEqual(len(client.get(base).json()["items"]), iteration)
                        # Persistence is independent of a browser session and ordinary history.
                        if not iteration:
                            app_store = SqlitePersonalMemoryStore(path)
                            try:
                                app_store.add(instrument_id="000001.SZ", name="本机测试名称")
                            finally:
                                app_store.close()
                with patch("uvicorn.run", side_effect=inspect_running), \
                        patch.object(api_server, "_prepare_startup_directory"), \
                        patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: Mock()}), \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                    self.assertEqual(api_server.main(["--personal-memory-db", str(path), "--no-research-intent-v2"]), 0)
                    self.assertEqual(errors.getvalue(), "")
            bad = Path(root) / "private-bad.sqlite3"
            bad.write_bytes(b"not-a-database-private-marker")
            with patch.object(api_server, "_prepare_startup_directory"), \
                    patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: Mock()}), \
                    patch("uvicorn.run") as server, redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                self.assertEqual(api_server.main(["--personal-memory-db", str(bad), "--no-research-intent-v2"]), 2)
                self.assertIn("personal_memory_store_unavailable", errors.getvalue())
                self.assertNotIn(str(root), errors.getvalue())
                self.assertNotIn("not-a-database-private-marker", errors.getvalue())
                server.assert_not_called()

    def test_no_memory_precedes_mode_and_same_file_checks_and_creates_nothing(self):
        from trading_companion import api_server
        with TemporaryDirectory() as root:
            path = Path(root) / "must-not-exist.sqlite3"
            with patch("uvicorn.run"), redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                    patch("trading_companion.personal_memory_store.SqlitePersonalMemoryStore") as store:
                self.assertEqual(api_server.main(["--demo", "--no-memory", "--memory-db", str(path),
                    "--personal-memory-db", str(path)]), 0)
                store.assert_not_called()
                self.assertFalse(path.exists())

    def test_no_personal_memory_keeps_conversation_history_in_either_option_order(self):
        from trading_companion import api_server
        for disable_first in (False, True):
            with self.subTest(disable_first=disable_first), TemporaryDirectory() as root:
                path = Path(root) / "history.sqlite3"
                flags = ["--personal-memory-db", str(path)]
                flags.insert(0 if disable_first else len(flags), "--no-personal-memory")

                def inspect_running(app, **kwargs):
                    with TestClient(app, base_url="http://127.0.0.1") as client:
                        health = client.get("/health").json()
                        self.assertFalse(health["personal_memory_enabled"])
                        self.assertTrue(health["memory_enabled"])

                with patch("uvicorn.run", side_effect=inspect_running) as server, \
                        patch.object(api_server, "_prepare_startup_directory"), \
                        patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: Mock()}), \
                        patch("trading_companion.personal_memory_store.SqlitePersonalMemoryStore") as store, \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                    self.assertEqual(api_server.main([*flags, "--memory-db", str(path), "--no-research-intent-v2"]), 0)
                    self.assertEqual(errors.getvalue(), "")
                server.assert_called_once()
                store.assert_not_called()
                self.assertTrue(path.is_file())

    def test_no_personal_memory_precedes_offline_mode_and_empty_path_checks(self):
        from trading_companion import api_server
        for mode in (["--demo"], ["--data", "synthetic"]):
            for path in ("", "must-not-exist.sqlite3"):
                with self.subTest(mode=mode, path=path), patch("uvicorn.run"), \
                        patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: Mock()}), \
                        patch("trading_companion.personal_memory_store.SqlitePersonalMemoryStore") as store, \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(api_server.main([*mode, "--no-personal-memory", "--personal-memory-db", path,
                                                     "--no-research-intent-v2"]), 0)
                store.assert_not_called()

    def test_direct_server_defaults_keep_personal_memory_and_document_reads_off(self):
        from trading_companion import api_server
        with patch("uvicorn.run"), patch.object(api_server, "_prepare_startup_directory"), \
                patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: Mock()}), \
                patch("trading_companion.api_service.ResearchApiService", wraps=ResearchApiService) as factory, \
                patch("trading_companion.personal_memory_store.SqlitePersonalMemoryStore") as store, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(api_server.main(["--no-research-intent-v2"]), 0)
        self.assertIsNone(factory.call_args.kwargs["personal_memory_store"])
        self.assertEqual(factory.call_args.kwargs["research_documents"], "off")
        store.assert_not_called()

    def test_personal_database_is_rejected_in_synthetic_modes_and_same_file(self):
        from trading_companion import api_server
        for flags in (("--demo",), ("--data", "synthetic"), ("--memory-db", "same.sqlite3"),
                      ("--snapshot-db", "same.sqlite3")):
            with self.subTest(flags=flags), redirect_stderr(StringIO()) as errors, \
                    patch("trading_companion.model_registry.configured_models") as models, \
                    self.assertRaises(SystemExit) as caught:
                api_server.main([*flags, "--personal-memory-db", "same.sqlite3"])
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("same.sqlite3", errors.getvalue())
            models.assert_not_called()
