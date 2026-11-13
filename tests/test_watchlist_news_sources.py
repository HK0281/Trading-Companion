"""Artificial fixed-endpoint responses and clocks; never access network or local data."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.collection_execution import CollectionExecutor
from trading_companion.company_news_sina import SinaCompanyNewsAdapter
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import ResearchDataRequest, SHANGHAI
from trading_companion.errors import InvalidRequestError
from trading_companion.watchlist_news_sources import SOURCES, WatchlistNewsSourceReader, _SourceFailure, _Transport, _ReadScope
from tests.test_company_news_sina import html
from tests.test_company_research import announcement, announcements


NOW = datetime(2026, 10, 25, 12, tzinfo=SHANGHAI)
CUTOFF = datetime(2026, 10, 24, 23, 59, 59, tzinfo=SHANGHAI)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def wait(self, duration):
        self.value += duration


def response(body, *, headers=None, status=200):
    result = Mock(status_code=status, headers=headers or {})
    result.iter_content.return_value = [body]
    return result


class WatchlistNewsSourceTests(unittest.TestCase):
    def setUp(self):
        self.timer, self.cancel = Clock(), Event()
        self.pool, self.sessions, self.calls, self.responses = CollectionExecutor(), [], [], []
        self.addCleanup(self.pool.close, wait=True)
        self.request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF)
        self.result = []
        self.handler = self.default_response
        self.reader = WatchlistNewsSourceReader(session_factory=self.session, executor=self.pool,
            clock=lambda: NOW, monotonic_clock=self.timer, wait=self.timer.wait)
        for target in ("requests.request", "requests.sessions.Session.request",
                       "trading_companion.local_env._read_local_values"):
            guard = patch(target, side_effect=AssertionError("No network or local settings"))
            guard.start()
            self.addCleanup(guard.stop)

    def default_response(self, method, url, **kwargs):
        if "sina.com.cn" in url:
            result = response(html().encode("gb18030"), headers={"Content-Type": "text/html; charset=gbk"})
        else:
            result = response(json.dumps(announcements()).encode())
        self.responses.append(result)
        return result

    def session(self):
        client = Mock()
        def send(method, url, **kwargs):
            self.calls.append((self.timer(), method, url, kwargs))
            return self.handler(method, url, **kwargs)
        client.request.side_effect = send
        self.sessions.append(client)
        return client

    def read(self, requests_=None, **kwargs):
        return self.reader.read_watchlist_sources(requests_ or (self.request,), cancel=self.cancel,
            deadline=self.timer() + 20, on_source=self.result.append, **kwargs)

    def test_fixed_sources_use_inline_validators_and_keep_rate_ledger_between_batches(self):
        with patch.object(self.pool._pool, "submit", side_effect=AssertionError("No future")):
            first = self.read()
            second = self.read(start_source=1)
        self.assertEqual((first["attempts"], first["completed_pairs"], second["attempts"]), (2, 2, 1))
        self.assertEqual([item["source"] for item in self.result], [*SOURCES, SOURCES[1]])
        self.assertTrue(all(item["status"] == "available" and item["attempted"] for item in self.result))
        self.assertTrue(all(len(item["items"]) == 1 for item in self.result))
        self.assertTrue(all(b[0] - a[0] >= 1 for a, b in zip(self.calls, self.calls[1:])))
        self.assertTrue(all(call[1] == "GET" and call[3]["allow_redirects"] is False for call in self.calls))
        self.assertTrue(all(call[3]["timeout"][1] <= 6 for call in self.calls))
        for resource in [*self.sessions, *self.responses]:
            resource.close.assert_called_once()

    def test_five_companies_are_ten_actual_attempts_and_fifty_titles_at_most(self):
        requests_ = tuple(replace(self.request, instrument_id=f"30030{i}.SZ") for i in range(5))
        def empty(method, url, **kwargs):
            if "sina.com.cn" in url:
                symbol = kwargs["params"]["symbol"][2:]
                return response(html([], title=f"中际旭创({symbol})个股资讯_新浪财经_新浪网").encode("gb18030"))
            return response(json.dumps(announcements([])).encode())
        self.handler = empty
        result = self.read(requests_)
        self.assertEqual((result["attempts"], result["completed_pairs"], len(self.calls)), (10, 10, 10))
        self.assertTrue(all(item["status"] == "empty" for item in self.result))
        self.assertEqual(len({(item[3]["params"].get("stock_list"), item[2]) for item in self.calls if "sina" not in item[2]}), 5)
        with self.assertRaises(InvalidRequestError):
            self.read(requests_ + (replace(self.request, instrument_id="300399.SZ"),))

    def test_each_verified_source_is_bounded_to_five_preserving_date_precision_and_skips(self):
        def titles(method, url, **kwargs):
            if "sina.com.cn" in url:
                rows = [("2026-10-23 00:00", f"中际旭创人工标题{i}",
                    f"https://finance.sina.com.cn/stock/2026-10-23/doc-test{i}.shtml") for i in range(7)]
                rows.insert(0, ("2026-10-23 12:00", "其他股票", "https://example.org/invalid"))
                return response(html(rows).encode("gb18030"))
            rows = [announcement(art_code=f"AN20261023123456789{i}", display_time=None,
                title=f"人工公告{i}") for i in range(7)]
            return response(json.dumps(announcements(rows)).encode())
        self.handler = titles
        result = self.read()
        self.assertEqual(result["attempts"], 2)
        self.assertEqual([len(item["items"]) for item in self.result], [5, 5])
        self.assertEqual(self.result[0]["skipped_count"], 1)
        for source in self.result:
            for item in source["items"]:
                self.assertEqual(item.publication_precision, "date")
                self.assertIsNone(item.published_at)

    def test_benign_sina_filters_are_empty_success_and_normal_research_still_counts_skips(self):
        url = "https://finance.sina.com.cn/stock/2026-10-23/doc-filter123.shtml"
        rows = [("2026-10-23 12:00", "其他股票人工标题", url),
                ("2026-08-01 12:00", "中际旭创窗口以外标题", url),
                ("2026-10-25 12:00", "中际旭创截止以后标题", url)]
        body = html(rows).encode("gb18030")
        self.handler = lambda *args, **kwargs: (response(body) if "sina.com.cn" in args[1]
            else response(json.dumps(announcements([])).encode()))
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"]), (2, 2))
        self.assertEqual((self.result[0]["status"], self.result[0]["skipped_count"],
                          self.result[0]["filtered_count"], self.result[0]["items"]), ("empty", 0, 3, ()))
        self.assertEqual(self.result[1]["filtered_count"], 0)
        client = Mock()
        client.request.return_value = response(body)
        normal = SinaCompanyNewsAdapter(session=client, clock=lambda: NOW).fetch_section(self.request)
        self.assertEqual((normal.status, normal.skipped_count, normal.items), ("empty", 3, ()))
        self.assertEqual(normal.code, "no_eligible_evidence_in_bounded_response")
        self.assertNotIn("filtered_count", normal.to_dict())

    def test_sina_each_row_retains_ordinary_skips_but_narrow_path_validates_before_filtering(self):
        safe_url = "https://finance.sina.com.cn/stock/2026-10-23/doc-shape123.shtml"
        cases = (
            (("2026-10-23 12:00", "其他股票标题", safe_url), 1, 0),
            (("2026-08-01 12:00", "中际旭创旧标题", safe_url), 1, 0),
            (("2026-10-23 12:00", "其他股票标题", "https://example.org/invalid"), 0, 1),
            (("2026-10-23 12:00", "中际旭创HTTP合成标题", "http://finance.sina.com.cn/doc-bad.shtml"), 1, 0),
            (("not-a-date", "中际旭创坏时间格式", safe_url), 0, 1),
            (("2026-02-30 12:00", "其他股票坏时间", safe_url), 0, 1),
            (("2026-10-23 25:00", "其他股票坏时刻", safe_url), 0, 1),
            (("2026-10-23 12:00", "", safe_url), 0, 1),
            (("2026-10-23 12:00", "中际旭创" + "x" * 600, safe_url), 0, 1),
        )
        for row, filtered, skipped in cases:
            with self.subTest(row=row):
                body = html([row]).encode("gb18030")
                client = Mock()
                client.request.return_value = response(body)
                normal = SinaCompanyNewsAdapter(session=client, clock=lambda: NOW).fetch_section(self.request)
                self.assertEqual((normal.status, normal.skipped_count, normal.items), ("empty", 1, ()))
                self.assertNotIn("filtered_count", normal.to_dict())
                self.result.clear()
                self.handler = lambda *args, **kwargs: (response(body) if "sina.com.cn" in args[1]
                    else response(json.dumps(announcements([])).encode()))
                self.read()
                self.assertEqual((self.result[0]["filtered_count"], self.result[0]["skipped_count"]), (filtered, skipped))
                self.assertEqual(self.result[0]["status"], "empty")

    def test_mixed_sina_rows_separate_benign_filters_from_genuine_failures(self):
        safe_url = "https://finance.sina.com.cn/stock/2026-10-23/doc-mixed123.shtml"
        rows = [("2026-10-23 12:00", "其他股票标题", safe_url),
                ("2026-08-01 12:00", "中际旭创旧标题", safe_url),
                ("2026-10-23 12:00", "其他股票坏链接", "https://example.org/invalid"),
                ("2026-02-30 12:00", "其他股票坏日期", safe_url),
                ("2026-10-23 12:00", "中际旭创有效标题", safe_url)]
        body = html(rows).encode("gb18030")
        self.handler = lambda *args, **kwargs: (response(body) if "sina.com.cn" in args[1]
            else response(json.dumps(announcements([])).encode()))
        self.read()
        self.assertEqual((self.result[0]["filtered_count"], self.result[0]["skipped_count"],
                          len(self.result[0]["items"])), (2, 2, 1))
        client = Mock()
        client.request.return_value = response(body)
        normal = SinaCompanyNewsAdapter(session=client, clock=lambda: NOW).fetch_section(self.request)
        self.assertEqual((normal.skipped_count, len(normal.items)), (4, 1))
        self.assertEqual(self.result[0]["items"], normal.items)

    def assert_sina_row_classification_preserves_normal_research(self, rows, *, filtered, skipped):
        control = ("2026-10-23 12:00", "中际旭创人工合法对照标题",
            "https://finance.sina.com.cn/stock/2026-10-23/doc-control633.shtml")
        body = html([*rows, control]).encode("gb18030")
        client = Mock()
        client.request.side_effect = [response(html([control]).encode("gb18030")), response(body)]
        adapter = SinaCompanyNewsAdapter(session=client, clock=lambda: NOW)
        original = adapter.fetch_section(self.request)
        normal = adapter.fetch_section(self.request)
        self.assertEqual(len(original.items), 1)
        self.assertEqual(normal.to_dict(), replace(original, skipped_count=len(rows)).to_dict())
        self.assertEqual(set(normal.to_dict()), {"kind", "source", "status", "items", "code", "message",
            "retrieved_at", "skipped_count", "bounded_snapshot"})
        self.result.clear()
        self.handler = lambda *args, **kwargs: (response(body) if "sina.com.cn" in args[1]
            else response(json.dumps(announcements([])).encode()))
        calls_before = len(self.calls)
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"]), (2, 2))
        self.assertEqual([call[2] for call in self.calls[calls_before:]], [
            "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php",
            "https://np-anotice-stock.eastmoney.com/api/security/ann"])
        narrow = self.result[0]
        self.assertEqual((narrow["status"], narrow["filtered_count"], narrow["skipped_count"]),
            ("available", filtered, skipped))
        self.assertEqual(narrow["items"], original.items)
        self.assertTrue(all(item.url not in {row[2] for row in rows} for item in narrow["items"]))
        self.assertEqual(self.result[1]["filtered_count"], 0)

    def test_supported_sina_host_but_unsaved_link_forms_are_benign_filters(self):
        urls = (
            "https://wap.cj.sina.cn/pc/7x24/5212024",
            "https://t.cj.sina.cn/articles/view/1282448044/4c709aac001021ree",
            "https://t.cj.sina.cn/articles/view/6017646441/166adff6900101qfpw",
            "https://t.cj.sina.cn/articles/view/1668899380/6379623400101yvq0",
            "https://t.cj.sina.cn/articles/view/1282448044/path%20part",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_sina_row_classification_preserves_normal_research(
                    [("2026-10-23 12:00", "中际旭创合成不支持保存标题", url)], filtered=1, skipped=0)
        self.assert_sina_row_classification_preserves_normal_research(
            [("2026-10-23 12:00", f"中际旭创合成标题{i}", url) for i, url in enumerate(urls)],
            filtered=5, skipped=0)

    def test_http_sina_reports_and_https_whitelist_shapes_are_filtered_without_rewrite_or_fetch(self):
        urls = (
            "http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/847855976132/index.phtml",
            "http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/847337240697/index.phtml",
            "http://finance.sina.com.cn/stock/2026-10-23/doc-http635.shtml",
            "http://cj.sina.cn/articles/view/1234567890/abc635",
            "http://t.cj.sina.cn/articles/view/1282448044/4c709aac001021ree",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_sina_row_classification_preserves_normal_research(
                    [("2026-10-23 12:00", "中际旭创合成HTTP标题", url)], filtered=1, skipped=0)
        self.assert_sina_row_classification_preserves_normal_research(
            [("2026-10-23 12:00", f"中际旭创合成HTTP标题{i}", url) for i, url in enumerate(urls)],
            filtered=5, skipped=0)

    def test_unsupported_sina_link_cannot_mask_bad_title_or_timestamp(self):
        url = "https://wap.cj.sina.cn/pc/7x24/5212024"
        rows = (
            ("2026-10-23 12:00", "", url),
            ("2026-10-23 12:00", "中际旭创" + "x" * 600, url),
            ("not-a-date", "中际旭创合成坏时间标题", url),
            ("2026-02-30 12:00", "中际旭创合成坏日期标题", url),
            ("2026-10-23 25:00", "其他股票合成坏时刻标题", url),
        )
        for row in rows:
            with self.subTest(row=row):
                self.assert_sina_row_classification_preserves_normal_research([row], filtered=0, skipped=1)

    def test_malformed_or_disguised_sina_links_are_failures_in_both_paths(self):
        path = "/articles/view/1282448044/4c709aac001021ree"
        urls = (
            "http://example.org" + path,
            "ftp://t.cj.sina.cn" + path,
            "https://example.org" + path,
            "https://t.cj.sina.cn.attacker.invalid" + path,
            "https://sina.com.cn.attacker.invalid" + path,
            "https://t.cj.sina.cn@attacker.invalid" + path,
            "https://attacker.invalid@t.cj.sina.cn" + path,
            "https://user:password@t.cj.sina.cn" + path,
            "https://@t.cj.sina.cn" + path,
            "http://t.cj.sina.cn@attacker.invalid" + path,
            "http://user:password@t.cj.sina.cn" + path,
            "https://t.cj.sina.cn:not-a-port" + path,
            "https://t.cj.sina.cn:65536" + path,
            "https://t.cj.sina.cn:-1" + path,
            "https://t.cj.sina.cn:" + path,
            "https://t.cj.sina.cn\\" + path,
            "https://t.cj.sina.cn" + path + " ",
            "https://t..sina.cn" + path,
            "https://.sina.cn" + path,
            "https://t.cj.sina.cn",
            "https://t.cj.sina.cn" + path + "\n",
            "https://t.cj.sina.cn" + path + "\t",
            "https://t.cj.sina.cn" + path + "\x00",
            "https://t.cj.sina.cn" + path + "%ZZ",
            "https://t.cj.sina.cn" + path + "%",
            "https://t.cj.sina.cn/" + "x" * 3000,
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_sina_row_classification_preserves_normal_research(
                    [("2026-10-23 12:00", "中际旭创合成坏链接标题", url)], filtered=0, skipped=1)

    def test_unsupported_link_and_title_decimal_length_boundaries(self):
        prefix, name = "https://t.cj.sina.cn/", "中际旭创"
        url600 = prefix + "x" * (600 - len(prefix))
        title600 = name + "x" * (600 - len(name))
        self.assertEqual((len(url600), len(title600)), (600, 600))
        cases = ((url600, title600, 1, 0), (url600 + "x", title600, 0, 1),
                 (url600, title600 + "x", 0, 1))
        for url, title, filtered, skipped in cases:
            with self.subTest(url_length=len(url), title_length=len(title)):
                self.assert_sina_row_classification_preserves_normal_research(
                    [("2026-10-23 12:00", title, url)], filtered=filtered, skipped=skipped)

    def test_waiting_for_shared_permits_observes_cancel_without_creating_session(self):
        for _ in range(3):
            self.assertTrue(self.pool._permits.acquire(blocking=False))
        result, error = [], []
        def run():
            try:
                result.append(self.read())
            except BaseException as caught:
                error.append(caught)
        thread = Thread(target=run, daemon=True)
        try:
            thread.start()
            self.cancel.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(error, [])
            self.assertEqual((result[0]["attempts"], result[0]["completed_pairs"]), (0, 0))
            self.assertEqual(self.sessions, [])
        finally:
            for _ in range(3):
                self.pool._permits.release()

    def test_errors_never_retry_redirect_fallback_or_repeat_same_pair(self):
        self.handler = lambda *a, **kw: response(b"", status=302)
        result = self.read()
        self.assertEqual((result["attempts"], len(self.calls)), (2, 2))
        self.assertEqual([item["code"] for item in self.result], ["source_redirect_blocked"] * 2)
        self.result.clear()
        self.handler = Mock(side_effect=requests.ConnectionError("private-marker"))
        result = self.read()
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(all(item["code"] == "source_connection_failed" for item in self.result))
        self.assertNotIn("private-marker", repr(self.result))

    def test_transport_and_close_os_errors_are_source_failures_and_never_escape(self):
        self.handler = Mock(side_effect=OSError("private-marker"))
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"]), (2, 2))
        self.assertTrue(all(item["code"] == "source_connection_failed" for item in self.result))
        self.result.clear()
        self.handler = self.default_response
        original = self.reader._factory
        def broken_close():
            client = original()
            client.close.side_effect = OSError("private-marker")
            return client
        self.reader._factory = broken_close
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"]), (2, 2))
        self.assertTrue(all(item["code"] == "source_connection_failed" for item in self.result))
        self.assertNotIn("private-marker", repr(self.result))

    def test_unsupported_exchange_consumes_pairs_without_attempts(self):
        result = self.read((replace(self.request, instrument_id="920000.BJ"),))
        self.assertEqual((result["attempts"], result["completed_pairs"]), (0, 2))
        self.assertTrue(all(item["status"] == "unavailable" and not item["attempted"] for item in self.result))
        self.assertEqual(self.calls, [])
        self.assertTrue(all(client.close.call_count == 1 for client in self.sessions))

    def test_cancellation_before_send_does_not_consume_pair_and_after_send_does(self):
        self.cancel.set()
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"], result["stopped"]), (0, 0, True))
        self.cancel.clear()
        def cancel_inside(method, url, **kwargs):
            self.cancel.set()
            return self.default_response(method, url, **kwargs)
        self.handler = cancel_inside
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"], result["stopped"]), (1, 1, True))
        self.assertEqual(self.result[0]["code"], "watchlist_news_cancelled")
        self.assertTrue(self.result[0]["attempted"])
        self.responses[0].close.assert_called_once()
        self.sessions[0].close.assert_called_once()

    def test_identity_callback_cancels_after_pacing_before_any_actual_attempt(self):
        seen = []
        def before_request(instrument_id, source):
            seen.append((instrument_id, source))
            self.cancel.set()
        result = self.read(before_request=before_request)
        self.assertEqual(seen, [(self.request.instrument_id, SOURCES[0])])
        self.assertEqual((result["attempts"], result["completed_pairs"], result["stopped"]), (0, 0, True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.result, [])
        self.sessions[0].close.assert_called_once()

    def test_identity_callback_runs_after_cross_batch_pacing_and_refreshes_timeout(self):
        self.read(start_source=1)
        seen = []
        def before_request(instrument_id, source):
            seen.append((self.timer(), instrument_id, source))
            self.timer.value += 0.2
        result = self.read(before_request=before_request)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual([row[2] for row in seen], list(SOURCES))
        self.assertGreaterEqual(seen[0][0] - self.calls[0][0], 1)
        self.assertTrue(all(call[0] >= stamp + .2 for call, (stamp, _, _) in zip(self.calls[1:], seen)))
        with self.assertRaises(InvalidRequestError):
            self.read(before_request=True)

    def test_cancellation_during_cross_batch_pacing_does_not_send_or_consume(self):
        self.read(start_source=1)
        self.result.clear()
        self.reader._wait = lambda delay: self.cancel.set()
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"]), (0, 0))
        self.assertEqual(len(self.calls), 1)

    def test_batch_and_single_request_deadlines_close_resources_and_count_started_pair(self):
        def late(method, url, **kwargs):
            self.timer.value += 21
            return self.default_response(method, url, **kwargs)
        self.handler = late
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"], result["stopped"]), (1, 1, True))
        self.assertEqual(self.result[0]["code"], "research_data_deadline_exceeded")
        self.responses[0].close.assert_called_once()
        self.result.clear()
        def request_late(method, url, **kwargs):
            self.timer.value += 6.01
            return self.default_response(method, url, **kwargs)
        self.handler = request_late
        result = self.read()
        self.assertEqual((result["attempts"], result["completed_pairs"], result["stopped"]), (2, 2, False))
        self.assertEqual([item["code"] for item in self.result], ["source_request_deadline_exceeded"] * 2)

    def test_declared_and_streamed_decimal_byte_limits_close_rejected_responses(self):
        for headers, body in (({"Content-Length": "512001"}, b""), ({"Content-Length": "n/a"}, b""),
                              ({}, b"x" * 512001)):
            self.result.clear()
            self.responses.clear()
            def oversized(*args, **kwargs):
                value = response(body, headers=headers)
                self.responses.append(value)
                return value
            self.handler = oversized
            result = self.read()
            self.assertEqual(result["attempts"], 2)
            self.assertEqual([item["code"] for item in self.result], ["source_response_too_large"] * 2)
            for item in self.responses:
                item.close.assert_called_once()

    def test_full_batch_actual_byte_ledger_uses_decimal_5120000_limit(self):
        requests_ = tuple(replace(self.request, instrument_id=f"30030{i}.SZ") for i in range(5))
        self.handler = lambda *args, **kwargs: response(b" " * 512000)
        result = self.read(requests_)
        self.assertEqual((result["attempts"], result["response_bytes"]), (10, 5120000))
        self.assertEqual(result["completed_pairs"], 10)

    def test_transport_rechecks_exact_get_endpoint_security_params_and_one_attempt(self):
        scope = _ReadScope(deadline=self.timer() + 20, cancel=self.cancel.is_set, clock=self.timer)
        client = self.session()
        transport = _Transport(self.reader, client, self.request, SOURCES[0], scope)
        kwargs = {"params": {"symbol": "sz300308", "Page": 1}, "allow_redirects": False, "stream": True}
        for method, url, changes in (("POST", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php", {}),
                ("GET", "https://example.org/", {}),
                ("GET", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php", {"params": {"symbol": "sz000001", "Page": 1}})):
            with self.assertRaises(_SourceFailure):
                transport.request(method, url, **{**kwargs, **changes})
        self.assertEqual(self.calls, [])
        result = transport.request("GET", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php", **kwargs)
        result.close()
        with self.assertRaises(_SourceFailure):
            transport.request("GET", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php", **kwargs)
        self.assertEqual((scope.requests, len(self.calls)), (1, 1))

    def test_narrow_entry_ignores_ordinary_research_provider_settings(self):
        service = ResearchDataService(news_provider="eastmoney", announcement_provider="cninfo", clock=lambda: NOW,
            monotonic_clock=self.timer)
        scope = _ReadScope(deadline=self.timer() + 20, cancel=self.cancel.is_set, clock=self.timer)
        client = self.session()
        transport = _Transport(self.reader, client, self.request, SOURCES[0], scope)
        result = service.read_watchlist_section(self.request, SOURCES[0], deadline=scope.deadline, scope=scope, session=transport)
        self.assertEqual(result.source, SOURCES[0])
        self.assertIn("sina.com.cn", self.calls[0][2])
        for changes in ({"limit": 4}, {"start_date": CUTOFF.date() - timedelta(days=31)}):
            with self.assertRaises(InvalidRequestError):
                self.read((replace(self.request, **changes),))
