"""Offline inbox HTTP, replay, lifecycle and independent research state contracts."""

from copy import deepcopy
from datetime import timedelta, timezone
import json
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import offline_network
from trading_companion.api import create_app
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.company_research_contracts import ResearchEvidence
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from trading_companion.watchlist_news import SOURCES, encode_cursor
from trading_companion.watchlist_news_sources import WatchlistNewsSourceReader
from tests.test_company_news_sina import html
from tests.test_company_research import announcements
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory
from tests.test_watchlist_news_sources import Clock, response


def title(code="000001.SZ"):
    stamp = NOW - timedelta(days=1)
    return ResearchEvidence("test-title", code, "news", SOURCES[0], "news_title", "人工新闻标题", "text",
        "https://finance.sina.com.cn/stock/2026-09-25/doc-test123.shtml", stamp,
        stamp.date(), "datetime", NOW, content_kind="title")


class Reader:
    def __init__(self):
        self.calls = []
        self.before = None
        self.failed_source = None

    def read_watchlist_sources(self, requests, *, cancel, deadline, on_source, start_source=0, before_request=None):
        self.calls.append((requests, start_source))
        count = 0
        for index, request in enumerate(requests):
            for source in SOURCES[start_source if index == 0 else 0:]:
                if before_request:
                    before_request(request.instrument_id, source)
                if cancel():
                    return dict(attempts=count, completed_pairs=count, stopped=True, code="watchlist_news_cancelled")
                if self.before:
                    self.before(request, source)
                failed = source == self.failed_source
                on_source(dict(instrument_id=request.instrument_id, source=source,
                    status="unavailable" if failed else "available" if source == SOURCES[0] else "empty",
                    code="source_connection_failed" if failed else "bounded_evidence_available", skipped_count=0,
                    filtered_count=0,
                    items=() if failed or source != SOURCES[0] else (title(request.instrument_id),),
                    checked_at=NOW, attempted=True))
                count += 1
        return dict(attempts=count, completed_pairs=count, stopped=False, code="ok")


class InboxApiTests(unittest.TestCase):
    def setUp(self):
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        env = patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials"))
        env.start()
        self.addCleanup(env.stop)
        self.store = SqlitePersonalMemoryStore(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.reader = Reader()
        self.model = ProductModelFixture()
        self.service = ResearchApiService(model_factory=lambda: self.model, directory_loader=lambda _: directory(),
            provider="openai", data_mode="tencent", product_data_service=CompanyDataFixture(),
            reference_date=NOW.date(), wall_clock=lambda: NOW, personal_memory_store=self.store,
            inbox_source_reader=self.reader)
        self.addCleanup(self.service.close)

    def test_open_refresh_and_filter_only_read_local_data(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            for path in ("/v1/personal/inbox", "/v1/personal/inbox?unread_only=false&instrument_id=000001.SZ"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"], [])
                self.assertEqual(len(response.json()["source_states"]), 2)
            self.assertEqual(self.reader.calls, [])
            self.assertEqual(self.model.calls, [])

    def test_sidecar_failure_status_is_local_metadata_and_never_a_research_result(self):
        self.assertIsNone(self.service.inbox()["sidecar_status"])
        self.service._inbox.note_sidecar("inbox_sidecar_unavailable")
        self.assertEqual(self.service.inbox()["sidecar_status"], "inbox_sidecar_unavailable")
        self.service._inbox.note_sidecar("arbitrary provider text")
        self.assertEqual(self.service.inbox()["sidecar_status"], "inbox_sidecar_unavailable")
        self.assertEqual(self.reader.calls, [])

    def test_check_read_replay_and_clear_are_independent_of_research(self):
        credentials = self.service.create_session()
        before = self.service.inspect(credentials["session_id"], credentials["access_token"])
        request = uuid4().hex
        first = self.service.check_inbox(request)
        self.assertEqual(first["attempted_requests"], 2)
        self.assertEqual(first["remaining_sources"], 0)
        item = first["inbox"]["items"][0]
        self.assertTrue(item["baseline"])
        self.assertEqual(item["title"], "人工新闻标题")
        self.assertTrue(all(state["last_success_at"] for state in first["inbox"]["source_states"]))
        marked = self.service.mark_inbox_read(item["message_id"])
        self.assertEqual(marked["unread_count"], 0)
        replay = self.service.check_inbox(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["inbox"]["unread_count"], 0)
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(self.service.inspect(credentials["session_id"], credentials["access_token"]), before)
        cleared = self.service.clear_inbox(marked["collection_revision"])
        self.assertEqual(cleared["items"], [])
        self.assertTrue(all(not state["initialized"] for state in cleared["source_states"]))
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.model.calls, [])

    def test_partial_source_failure_never_advances_that_watermark(self):
        self.reader.failed_source = SOURCES[1]
        result = self.service.check_inbox(uuid4().hex)
        self.assertEqual(result["status"], "partial")
        states = {s["source"]: s for s in result["inbox"]["source_states"]}
        self.assertIsNotNone(states[SOURCES[0]]["last_success_at"])
        self.assertIsNone(states[SOURCES[1]]["last_success_at"])
        self.assertEqual(states[SOURCES[1]]["status"], "unavailable")

    def use_sina_rows(self, rows):
        """Run the real bounded reader and validators against artificial HTTP responses."""
        timer = Clock()
        def session():
            client = Mock()
            def request(_method, url, **_kwargs):
                if "sina.com.cn" in url:
                    body = html(rows, title="平安银行(000001)个股资讯_新浪财经_新浪网").encode("utf-8")
                    return response(body, headers={"Content-Type": "text/html; charset=utf-8"})
                return response(json.dumps(announcements([])).encode("utf-8"))
            client.request.side_effect = request
            return client
        self.service._inbox.reader = WatchlistNewsSourceReader(session_factory=session,
            clock=lambda: NOW, monotonic_clock=timer, wait=timer.wait)

    def test_expected_sina_filters_build_baseline_then_allow_new_discoveries(self):
        stamp = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        old = (NOW - timedelta(days=31)).strftime("%Y-%m-%d %H:%M")
        rows = [(stamp, "行业公共标题", "https://finance.sina.com.cn/stock/doc-sector1.shtml"),
                (old, "平安银行过期标题", "https://finance.sina.com.cn/stock/doc-old1.shtml"),
                (stamp, "平安银行公共标题", "https://finance.sina.com.cn/stock/doc-good1.shtml")]
        self.use_sina_rows(rows)
        first = self.service.check_inbox(uuid4().hex)
        sina = first["results"][0]
        state = next(item for item in first["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual(first["status"], "completed")
        self.assertEqual((sina["status"], sina["filtered_count"], sina["saved"], sina["baseline"]),
                         ("available", 2, 1, True))
        self.assertEqual((state["initialized"], state["skipped_count"], state["last_success_at"]),
                         (True, 0, NOW.astimezone(timezone.utc).isoformat()))
        rows.append((stamp, "平安银行后来发现标题", "https://finance.sina.com.cn/stock/doc-good2.shtml"))
        second = self.service.check_inbox(uuid4().hex)
        self.assertEqual((second["status"], second["results"][0]["saved"],
                          second["results"][0]["duplicates"], second["results"][0]["baseline"]),
                         ("completed", 1, 1, False))
        self.assertEqual([item["baseline"] for item in second["inbox"]["items"]].count(False), 1)
        replay = self.service.check_inbox(second["request_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["results"][0]["filtered_count"], 2)

    def test_all_expected_sina_filters_are_a_successful_empty_baseline(self):
        stamp = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        old = (NOW - timedelta(days=31)).strftime("%Y-%m-%d %H:%M")
        self.use_sina_rows([(stamp, "行业公共标题", "https://finance.sina.com.cn/stock/doc-sector1.shtml"),
                           (old, "平安银行过期标题", "https://finance.sina.com.cn/stock/doc-old1.shtml")])
        result = self.service.check_inbox(uuid4().hex)
        state = next(item for item in result["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["results"][0]["status"], result["results"][0]["filtered_count"]), ("empty", 2))
        self.assertEqual((state["initialized"], state["skipped_count"], state["last_success_at"]),
                         (True, 0, NOW.astimezone(timezone.utc).isoformat()))
        self.assertEqual(result["inbox"]["items"], [])

    def test_real_sina_validation_errors_remain_partial_and_do_not_build_baseline(self):
        stamp = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        self.use_sina_rows([(stamp, "行业正常过滤", "https://finance.sina.com.cn/stock/doc-sector1.shtml"),
                           (stamp, "行业坏链接", "https://example.org/invalid"),
                           ("not-a-time", "行业坏时间", "https://finance.sina.com.cn/stock/doc-bad1.shtml"),
                           (stamp, "平安银行合格标题", "https://finance.sina.com.cn/stock/doc-good1.shtml")])
        result = self.service.check_inbox(uuid4().hex)
        state = next(item for item in result["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual((result["status"], result["results"][0]["status"],
                          result["results"][0]["saved"], result["results"][0]["filtered_count"]),
                         ("partial", "partial", 1, 1))
        self.assertEqual((state["initialized"], state["skipped_count"], state["last_success_at"]), (False, 2, None))

    def test_well_formed_mobile_sina_links_are_filtered_and_allow_baseline_and_later_discovery(self):
        stamp = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        unsupported = ["https://wap.cj.sina.cn/pc/7x24/5212024",
                       "https://t.cj.sina.cn/articles/view/1282448044/4c709aac001021ree",
                       "https://t.cj.sina.cn/articles/view/6017646441/166adff6900101qfpw",
                       "https://t.cj.sina.cn/articles/view/1668899380/6379623400101yvq0"]
        rows = [(stamp, f"平安银行人工标题{index}", url) for index, url in enumerate(unsupported)]
        self.use_sina_rows(rows)
        first = self.service.check_inbox(uuid4().hex)
        state = next(item for item in first["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual((first["status"], first["results"][0]["status"],
                          first["results"][0]["filtered_count"], state["skipped_count"], state["initialized"]),
                         ("completed", "empty", 4, 0, True))
        self.assertEqual(first["inbox"]["items"], [])
        canonical = "https://finance.sina.com.cn/stock/doc-good1.shtml"
        rows.append((stamp, "平安银行新发现人工标题", canonical))
        second = self.service.check_inbox(uuid4().hex)
        self.assertEqual((second["status"], second["results"][0]["status"],
                          second["results"][0]["filtered_count"], second["results"][0]["saved"],
                          second["results"][0]["baseline"]), ("completed", "available", 4, 1, False))
        self.assertEqual([item["url"] for item in second["inbox"]["items"]], [canonical])
        self.assertFalse(second["inbox"]["items"][0]["baseline"])

    def test_http_sina_reports_are_filtered_without_saving_or_rewriting_the_scheme(self):
        stamp = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        urls = ["http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/847855976132/index.phtml",
                "http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/847337240697/index.phtml",
                "http://finance.sina.com.cn/stock/doc-good1.shtml"]
        rows = [(stamp, f"平安银行人工研报标题{index}", url) for index, url in enumerate(urls)]
        self.use_sina_rows(rows)
        first = self.service.check_inbox(uuid4().hex)
        state = next(item for item in first["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual((first["status"], first["results"][0]["status"],
                          first["results"][0]["filtered_count"], state["skipped_count"], state["initialized"]),
                         ("completed", "empty", 3, 0, True))
        self.assertEqual(first["inbox"]["items"], [])
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM inbox_messages").fetchone()[0], 0)
        rows.append((stamp, "平安银行非新浪链接", "http://example.org/stock/doc-good1.shtml"))
        second = self.service.check_inbox(uuid4().hex)
        state = next(item for item in second["inbox"]["source_states"] if item["source"] == SOURCES[0])
        self.assertEqual((second["status"], second["results"][0]["status"],
                          second["results"][0]["filtered_count"], state["skipped_count"]),
                         ("partial", "partial", 3, 1))
        self.assertEqual(second["inbox"]["items"], [])

    def test_each_explicit_batch_is_at_most_five_stocks_and_ten_sources(self):
        for code in ("000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ", "000006.SZ"):
            self.store.add(instrument_id=code, name="人工公司")
        first = self.service.check_inbox(uuid4().hex)
        self.assertEqual((first["attempted_requests"], first["checked_stocks"], first["total_stocks"]), (10, 5, 6))
        self.assertEqual(first["remaining_sources"], 2)
        self.assertEqual(len(self.reader.calls), 1)
        second = self.service.check_inbox(uuid4().hex, first["next_cursor"])
        self.assertEqual(second["attempted_requests"], 2)
        self.assertEqual(second["remaining_sources"], 0)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(len(self.reader.calls), 2)

    def test_unstarted_sources_resume_without_repeating_attempted_pair(self):
        delegate = self.reader.read_watchlist_sources
        def partial(requests, *, cancel, deadline, on_source, start_source=0, before_request=None):
            request = requests[0]
            on_source(dict(instrument_id=request.instrument_id, source=SOURCES[0], status="unavailable",
                code="source_connection_failed", skipped_count=0, filtered_count=0, items=(), checked_at=NOW, attempted=True))
            return dict(attempts=1, completed_pairs=1, stopped=True, code="research_data_deadline_exceeded")
        self.reader.read_watchlist_sources = partial
        first = self.service.check_inbox(uuid4().hex)
        self.assertEqual(first["remaining_sources"], 1)
        self.reader.read_watchlist_sources = delegate
        second = self.service.check_inbox(uuid4().hex, first["next_cursor"])
        self.assertEqual(second["attempted_requests"], 1)
        self.assertEqual(self.reader.calls[-1][1], 1)
        self.assertEqual(second["results"][0]["source"], SOURCES[1])

    def test_readd_and_clear_prevent_late_writes_and_old_replays(self):
        identifier = uuid4().hex
        def replace(_request, source):
            if source == SOURCES[0]:
                self.store.remove(instrument_id="000001.SZ")
                self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.reader.before = replace
        with self.assertRaises(ApiProblem) as caught:
            self.service.check_inbox(identifier)
        self.assertEqual(caught.exception.code, "inbox_snapshot_changed")
        self.assertEqual(self.service.inbox()["items"], [])
        with self.assertRaises(ApiProblem) as caught:
            self.service.check_inbox(identifier)
        self.assertEqual(caught.exception.code, "inbox_request_retired")
        self.assertEqual(len(self.reader.calls), 1)

    def test_busy_slot_survives_stopped_wait_until_real_reader_closes(self):
        entered, release, cancelled = Event(), Event(), Event()
        result = []
        def delayed(_request, _source):
            entered.set()
            release.wait(2)
        self.reader.before = delayed
        def work():
            try:
                result.append(self.service.check_inbox(uuid4().hex, cancelled=cancelled))
            except Exception as error:
                result.append(error)
        thread = Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancelled.set()
            with self.assertRaises(ApiProblem) as caught:
                self.service.check_inbox(uuid4().hex)
            self.assertEqual(caught.exception.code, "inbox_busy")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(self.service.inbox()["items"], [])

    def test_http_schema_origin_methods_and_confirmed_clear(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            path = "/v1/personal/inbox/check"
            self.assertEqual(client.post(path, json={"request_id": uuid4().hex, "url": "https://invalid"}).status_code, 422)
            self.assertEqual(client.post(path, json={"request_id": uuid4().hex},
                headers={"Origin": "https://invalid"}).status_code, 403)
            self.assertEqual(client.get("/v1/personal/inbox?extra=1").status_code, 422)
            checked = client.post(path, json={"request_id": uuid4().hex})
            self.assertEqual(checked.status_code, 200, checked.text)
            data = checked.json()["inbox"]
            identifier = data["items"][0]["message_id"]
            self.assertEqual(client.patch("/v1/personal/inbox/" + identifier, json={"read": False}).status_code, 422)
            self.assertEqual(client.patch("/v1/personal/inbox/" + identifier, json={"read": 1}).status_code, 422)
            self.assertEqual(client.patch("/v1/personal/inbox/" + identifier, json={"read": True}).status_code, 200)
            self.assertEqual(client.delete("/v1/personal/inbox", headers={"If-Match": data["collection_revision"]}).status_code, 409)
            fresh = client.get("/v1/personal/inbox").json()
            self.assertEqual(client.delete("/v1/personal/inbox", headers={"If-Match": fresh["collection_revision"]}).status_code, 200)

    def test_cursor_membership_and_separate_list_cursor_validation(self):
        initial = self.store.inbox_snapshot()
        cursor = encode_cursor("check", dict(watchlist_revision=initial["watchlist_revision"], position=1))
        response = self.service.check_inbox(uuid4().hex, cursor)
        self.assertEqual(response["attempted_requests"], 1)
        self.assertEqual(self.reader.calls[0][1], 1)
        with self.assertRaises(ApiProblem):
            self.service.inbox(cursor=cursor)
        self.store.clear_inbox(expected_revision=self.store.inbox_snapshot()["collection_revision"])
        with self.assertRaises(ApiProblem) as caught:
            self.service.check_inbox(uuid4().hex, cursor)
        self.assertEqual(caught.exception.code, "inbox_snapshot_changed")

    def test_disabled_and_development_are_local_and_never_construct_a_reader(self):
        for provider, data_mode in (("fake", "tencent"),):
            service = ResearchApiService(model_factory=ProductModelFixture, directory_loader=lambda _: directory(),
                provider=provider, data_mode=data_mode, personal_memory_store=self.store)
            try:
                self.assertFalse(service.inbox()["enabled"])
                with self.assertRaises(ApiProblem) as caught:
                    service.check_inbox(uuid4().hex)
                self.assertEqual(caught.exception.status, 404)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
