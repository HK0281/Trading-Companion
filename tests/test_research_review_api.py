"""Offline review service/HTTP lifecycle using artificial research and temporary history only."""

import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import offline_network
from trading_companion.api import _review_price_request, create_app
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.memory_store import SqliteConversationStore
from trading_companion.research_review_prices import ReviewPriceError
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.network = offline_network()
        self.network.__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)
        self.guard = patch("trading_companion.local_env._read_local_values",
                           side_effect=AssertionError("No credential or .env access"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        source = patch("trading_companion.product_research._build_source", side_effect=MemorySourceFactory())
        source.start()
        self.addCleanup(source.stop)
        self.wall = NOW
        self.store = SqliteConversationStore(":memory:", clock=lambda: self.wall)
        self.addCleanup(self.store.close)
        self.model = ProductModelFixture()
        self.directory = Mock(return_value=directory())
        self.reader = Mock(side_effect=lambda _instrument, **kw: [
            {"date": kw["start_date"].isoformat(), "close": 10.5}])
        self.service = self.build()
        self.addCleanup(self.service.close)
        self.credentials = self.service.create_session()
        self.request_id = uuid4().hex
        self.response = self.service.message(self.credentials["session_id"], self.credentials["access_token"],
            self.request_id, "平安银行中线能买吗")
        self.conversation_id = self.response["conversation_id"]
        self.wall += timedelta(days=30)

    def build(self, **kwargs):
        options = dict(model_factory=lambda: self.model, directory_loader=self.directory,
            provider="openai", data_mode="tencent", product_data_service=CompanyDataFixture(),
            reference_date=NOW.date(), wall_clock=lambda: self.wall, conversation_store=self.store,
            review_price_loader=self.reader)
        options.update(kwargs)
        return ResearchApiService(**options)

    def record(self, conversation_id=None, sequence=1, request_id=None, instrument_id="000001.SZ"):
        return (conversation_id or self.conversation_id, sequence, request_id or self.request_id, instrument_id)

    def legacy(self):
        result = deepcopy(self.response["result"])
        result.pop("review_context", None)
        self.wall = NOW + timedelta(minutes=20)
        conversation = self.store.create("local", provider="openai", data_mode="tencent", title="人工旧记录")
        self.store.append("local", conversation, request_id=self.request_id, asked_at=NOW,
            question="人工旧记录", result=result, digest="{}")
        self.wall = NOW + timedelta(days=30)
        return conversation, result

    def test_new_completion_is_saved_once_and_not_exposed_to_model_or_digest(self):
        context = self.response["result"]["review_context"]
        self.assertEqual(context["version"], "research-review-context-v1")
        self.assertEqual(context["event"], "company_research")
        self.assertEqual(context["completed_at"], NOW.astimezone(timezone.utc).isoformat())
        stored = self.store.load("local", self.conversation_id)
        self.assertEqual(stored["turns"][0]["result"]["review_context"], context)
        self.assertNotIn("review_context", str(stored["turns"][0]["digest"]))
        replay = self.service.message(self.credentials["session_id"], self.credentials["access_token"],
            self.request_id, "平安银行中线能买吗")
        self.assertEqual(replay["result"]["review_context"], context)
        self.wall = NOW + timedelta(minutes=1)
        history = self.service.history(self.credentials["session_id"], self.credentials["access_token"])
        self.assertEqual(history["result"]["code"], "product_saved_result")
        self.assertNotIn("review_context", history["result"])
        self.assertEqual(len(self.store.load("local", self.conversation_id)["turns"]), 1)
        for messages, _ in self.model.calls:
            self.assertNotIn("review_context", str(messages))

    def test_list_and_detail_do_not_call_models_directory_or_prices(self):
        calls = len(self.model.calls), self.directory.call_count
        listing = self.service.research_reviews("000001.SZ")
        self.assertEqual(listing["version"], "research-review-v1")
        self.assertEqual(len(listing["items"]), 1)
        item = listing["items"][0]
        self.assertEqual(item["status"], "available")
        self.assertTrue(item["prices"]["enabled"], item["prices"])
        self.assertEqual(item["prices"]["basis"], "completed_at")
        self.assertEqual(self.service.research_review(*self.record())["result"], self.response["result"])
        self.assertEqual((len(self.model.calls), self.directory.call_count), calls)
        self.reader.assert_not_called()

    def test_legacy_bound_moves_when_source_conversation_is_continued(self):
        conversation, original = self.legacy()
        first = self.service.research_review(*self.record(conversation))["item"]
        self.assertEqual(first["prices"]["basis"], "conversation_updated_at")
        self.assertIn("继续该对话会使起点后移", first["prices"]["notice"])
        self.store.append("local", conversation, request_id=uuid4().hex, asked_at=self.wall,
            question="人工后续问题", result={"code": "general_knowledge_answered"}, digest="{}")
        moved = self.service.research_review(*self.record(conversation))["item"]
        self.assertGreater(moved["prices"]["start_date"], first["prices"]["start_date"])
        self.assertEqual(self.service.research_review(*self.record())["item"]["prices"]["basis"], "completed_at")
        self.assertEqual(self.store.load("local", conversation)["turns"][0]["result"], original)

    def test_slow_legacy_directory_crossing_midnight_uses_actual_save_date(self):
        self.wall = NOW.astimezone(timezone.utc).replace(hour=15, minute=45, second=0, microsecond=0)
        asked = self.wall

        def delayed_directory(_):
            self.wall += timedelta(minutes=20)
            return directory()

        service = self.build(directory_loader=delayed_directory)
        try:
            credentials = service.create_session()
            result = service.message(credentials["session_id"], credentials["access_token"],
                uuid4().hex, "平安银行中线能买吗")["result"]
            self.assertIsNone(result["runtime"]["stop_code"])
            result.pop("review_context")
            conversation = self.store.create("local", provider="openai", data_mode="tencent", title="跨午夜旧记录")
            self.store.append("local", conversation, request_id=self.request_id, asked_at=asked,
                question="跨午夜旧记录", result=result, digest="{}")
            self.wall += timedelta(days=10)
            item = self.service.research_review(*self.record(conversation))["item"]
            self.assertEqual(item["prices"]["basis"], "conversation_updated_at")
            shanghai = timezone(timedelta(hours=8))
            unsafe = (asked + timedelta(minutes=10)).astimezone(shanghai).date() + timedelta(days=1)
            self.assertGreater(item["prices"]["start_date"], unsafe.isoformat())
        finally:
            service.close()

    def test_exact_binding_and_removed_source_cannot_be_replayed(self):
        for args in (self.record(sequence=2), self.record(request_id=uuid4().hex),
                     self.record(instrument_id="600000.SH")):
            with self.subTest(args=args), self.assertRaises(ApiProblem) as error:
                self.service.research_review(*args)
            self.assertEqual(error.exception.status, 404)
        self.service.delete_conversation(self.conversation_id)
        self.assertEqual(self.service.research_reviews("000001.SZ")["items"], [])
        with self.assertRaises(ApiProblem) as error:
            self.service.research_review_prices(*self.record())
        self.assertEqual(error.exception.status, 404)
        self.reader.assert_not_called()

    def test_explicit_prices_are_bounded_and_do_not_use_research_allowances(self):
        before = self.service.inspect(self.credentials["session_id"], self.credentials["access_token"])
        prices = self.service.research_review_prices(*self.record())
        self.assertEqual(prices["status"], "available", prices)
        self.assertEqual(prices["data_authenticity"], "provider_reported")
        self.assertEqual(len(prices["points"]), 1)
        self.reader.assert_called_once()
        after = self.service.inspect(self.credentials["session_id"], self.credentials["access_token"])
        self.assertEqual(after, before)

    def test_unknown_adjustment_is_rejected_before_network(self):
        conversation, result = self.legacy()
        # All original basis statements are removed; no guess from chart or answer prose is allowed.
        company = result["company_research"]
        for plan in company.get("trade_plans", []):
            plan["price_adjustment"] = None
        for evidence in company.get("evidence", []):
            if "calculation_basis" in evidence:
                evidence["calculation_basis"].pop("price_adjustment", None)
        other = self.store.create("local", provider="openai", data_mode="tencent", title="未知口径")
        self.store.append("local", other, request_id=self.request_id, asked_at=NOW,
            question="未知口径", result=result, digest="{}")
        prices = self.service.research_review_prices(*self.record(other))
        self.assertEqual(prices["status"], "unavailable")
        self.reader.assert_not_called()

    def test_transport_empty_bad_data_and_timeout_preserve_the_original(self):
        original = deepcopy(self.response["result"])
        cases = (([], "review_prices_empty"),
                 ([{"date": "2099-01-01", "close": 1}], "review_prices_invalid_data"),
                 ([{"date": "2026-09-27", "close": float("nan")}], "review_prices_invalid_data"),
                 (ReviewPriceError("review_prices_timeout"), "review_prices_timeout"))
        for output, expected in cases:
            with self.subTest(expected=expected):
                self.reader.side_effect = output if isinstance(output, Exception) else None
                self.reader.return_value = output
                value = self.service.research_review_prices(*self.record())
                self.assertEqual(value["code"], expected)
                self.assertEqual(value["points"], [])
                self.assertEqual(self.service.research_review(*self.record())["result"], original)

    def test_busy_slot_lives_until_worker_cleanup_and_deletion_rejects_result(self):
        entered, release = Event(), Event()
        observed, outcome = [], []

        def blocked(_instrument, **kw):
            entered.set()
            release.wait(3)
            observed.append(kw["cancelled"]())
            return [{"date": kw["start_date"].isoformat(), "close": 10}]

        self.reader.side_effect = blocked

        def fetch():
            try:
                outcome.append(self.service.research_review_prices(*self.record()))
            except ApiProblem as error:
                outcome.append(error.code)

        thread = Thread(target=fetch)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ApiProblem) as error:
                self.service.research_review_prices(*self.record())
            self.assertEqual(error.exception.code, "review_prices_busy")
            self.service.delete_conversation(self.conversation_id)
            self.assertIsNotNone(self.service._review_active)
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [True])
        self.assertEqual(outcome, ["review_not_found"])
        self.assertIsNone(self.service._review_active)

    def test_legacy_source_boundary_changed_during_fetch_rejects_stale_points(self):
        conversation, _ = self.legacy()

        def changed(_instrument, **kw):
            self.store.append("local", conversation, request_id=uuid4().hex, asked_at=self.wall,
                question="后来问题", result={"code": "general_knowledge_answered"}, digest="{}")
            return [{"date": kw["start_date"].isoformat(), "close": 10}]

        self.reader.side_effect = changed
        with self.assertRaises(ApiProblem) as error:
            self.service.research_review_prices(*self.record(conversation))
        self.assertEqual(error.exception.code, "review_source_changed")
        self.assertIsNone(self.service._review_active)

    def test_disabled_development_and_no_memory_paths_never_read_history(self):
        from trading_companion.api import create_demo_service
        for service in (self.build(conversation_store=None), create_demo_service()):
            with self.subTest(service=service):
                try:
                    value = service.research_reviews("000001.SZ")
                    self.assertFalse(value["enabled"])
                    with self.assertRaises(ApiProblem) as error:
                        service.research_review(*self.record())
                    self.assertEqual(error.exception.code, "review_disabled")
                finally:
                    service.close()

    def test_http_readonly_queries_price_body_and_origin_boundaries(self):
        base = f"/v1/research-reviews/{self.conversation_id}/1"
        query = f"?request_id={self.request_id}&instrument_id=000001.SZ"
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            listing = client.get("/v1/research-reviews?instrument_id=000001.SZ")
            self.assertEqual(listing.status_code, 200, listing.text)
            detail = client.get(base + query)
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertIn("review_context", detail.json()["result"])
            self.reader.assert_not_called()
            body = {"request_id": self.request_id, "instrument_id": "000001.SZ"}
            loaded = client.post(base + "/prices", json=body)
            self.assertEqual(loaded.status_code, 200, loaded.text)
            self.assertEqual(loaded.json()["status"], "available", loaded.text)
            self.assertEqual(loaded.headers["cache-control"], "no-store")
            for response in (
                    client.get("/v1/research-reviews?instrument_id=000001.SZ&from_date=bad"),
                    client.get("/v1/research-reviews?instrument_id=000001.SZ&source=other"),
                    client.post(base + "/prices", json={**body, "url": "https://example.invalid"})):
                self.assertEqual(response.status_code, 422)
            self.assertEqual(client.post(base + "/prices", json=body,
                headers={"Origin": "https://example.invalid"}).status_code, 403)


class ReviewDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_flag_reaches_worker_without_releasing_it_early(self):
        entered, release = Event(), Event()
        seen = []

        class Request:
            async def is_disconnected(self):
                return True

        def operation(cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(2))
            seen.append(True)
            release.wait(2)
            return "finished"

        task = asyncio.create_task(_review_price_request(Request(), operation))
        try:
            await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0.06)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, "finished")
        self.assertEqual(seen, [True])

    async def test_handler_cancellation_signals_the_shielded_worker(self):
        entered, ended = Event(), Event()

        class Request:
            async def is_disconnected(self):
                return False

        def operation(cancelled):
            entered.set()
            cancelled.wait(2)
            ended.set()
            return None

        task = asyncio.create_task(_review_price_request(Request(), operation))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await asyncio.to_thread(ended.wait, 2))
