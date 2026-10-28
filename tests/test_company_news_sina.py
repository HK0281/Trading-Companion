"""Artificial Sina HTML fixtures; no external requests, credentials, or environment reads."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.company_news_sina import SinaCompanyNewsAdapter
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import ResearchDataRequest, SHANGHAI
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn


NOW = datetime(2026, 10, 25, 12, tzinfo=SHANGHAI)
CUTOFF = datetime(2026, 10, 24, 23, 59, 59, tzinfo=SHANGHAI)
URL = "https://finance.sina.com.cn/stock/2026-10-23/doc-test123.shtml"
PRIVATE = "private-error-marker-never-export"


def html(rows=None, *, title="中际旭创(300308)个股资讯_新浪财经_新浪网"):
    rows = [("2026-10-23 12:10", "中际旭创人工新闻标题", URL)] if rows is None else rows
    links = "".join(stamp.replace(" ", "&nbsp;") + "&nbsp;&nbsp;<a href='" + url + "'>" + text + "</a> <br>"
                    for stamp, text, url in rows)
    return "<html><title>" + title + "</title><div class='datelist'><ul>" + links + "</ul></div></html>"


def response(text=None, *, status=200, headers=None, chunks=None):
    result = Mock(status_code=status, headers={"Content-Type": "text/html; charset=gbk", **(headers or {})})
    result.iter_content.return_value = [(html() if text is None else text).encode("gb18030")] if chunks is None else chunks
    return result


class SinaCompanyNewsTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.adapter = SinaCompanyNewsAdapter(session=self.session, clock=lambda: NOW)
        self.request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF)
        for guard in (patch("requests.request", side_effect=AssertionError("No external HTTP")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))):
            guard.start()
            self.addCleanup(guard.stop)

    def fetch(self, text=None, **kwargs):
        self.session.request.return_value = response(text, **kwargs)
        return self.adapter.fetch_section(self.request)

    def test_verified_gbk_page_yields_title_evidence_and_preserves_display_time(self):
        section = self.fetch()
        self.assertEqual((section.status, section.source, len(section.items)), ("available", "sina_company_news", 1))
        item = section.items[0]
        self.assertEqual((item.instrument_id, item.metric, item.content_kind), ("300308.SZ", "news_title", "title"))
        self.assertEqual(item.value, "中际旭创人工新闻标题")
        self.assertEqual(item.published_at, datetime(2026, 10, 23, 12, 10, tzinfo=SHANGHAI))
        self.assertEqual(item.retrieved_at, NOW)
        self.assertIn("article body was not retrieved", " ".join(item.warnings))
        self.assertIn("minute precision", " ".join(item.warnings))
        self.assertEqual(self.session.request.call_args.args,
                         ("GET", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php"))
        self.assertEqual(self.session.request.call_args.kwargs["params"], {"symbol": "sz300308", "Page": 1})
        self.assertFalse(self.session.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], (3.0, 6.0))

    def test_default_research_service_uses_verified_sina_provider_without_fallback(self):
        self.session.request.return_value = response()
        section = ResearchDataService(session=self.session, clock=lambda: NOW).fetch_section(self.request, "news")
        self.assertEqual(section.source, "sina_company_news")
        self.session.request.assert_called_once()
        with self.assertRaises(InvalidRequestError):
            ResearchDataService(news_provider="unknown")

    def test_shared_scope_reserves_from_existing_request_balance(self):
        from trading_companion.collection_execution import CollectionUnavailable
        from tests.test_collection_execution import scope
        shared = scope(requests=1, response_bytes=512_000)
        shared.reserve_request()  # Another domain has already consumed the sole request.
        with self.assertRaises(CollectionUnavailable) as caught:
            self.adapter.fetch_section(self.request, deadline=shared.deadline, scope=shared)
        self.assertEqual(caught.exception.code, "research_request_budget_exhausted")
        self.session.request.assert_not_called()

    def test_shared_byte_budget_rejects_sina_body_and_closes_response(self):
        from trading_companion.collection_execution import CollectionUnavailable
        from tests.test_collection_execution import scope
        shared = scope(requests=1, response_bytes=1)
        result = response()
        self.session.request.return_value = result
        with self.assertRaises(CollectionUnavailable) as caught:
            self.adapter.fetch_section(self.request, deadline=shared.deadline, scope=shared)
        self.assertEqual(caught.exception.code, "research_byte_budget_exhausted")
        self.assertEqual(shared.usage(), {"requests": 1, "response_bytes": 0})
        result.close.assert_called_once()

    def test_shanghai_symbol_binding_is_explicit_and_beijing_is_not_guessed(self):
        self.request = replace(self.request, instrument_id="600000.SH", security_name="浦发银行")
        section = self.fetch(html([("2026-10-23 12:10", "浦发银行人工标题", URL)],
                                  title="浦发银行(600000)个股资讯_新浪财经_新浪网"))
        self.assertEqual(section.status, "available")
        self.assertEqual(self.session.request.call_args.kwargs["params"]["symbol"], "sh600000")
        self.session.request.reset_mock()
        section = self.adapter.fetch_section(replace(self.request, instrument_id="920000.BJ"))
        self.assertEqual(section.code, "sina_news_exchange_unverified")
        self.session.request.assert_not_called()

    def test_wrong_page_or_missing_list_cannot_become_empty_news(self):
        self.assertEqual(self.fetch(html(title="别的公司(000001)个股资讯_新浪财经_新浪网")).code,
                         "news_page_instrument_mismatch")
        text = html().replace("class='datelist'", "class='other'")
        self.assertEqual(self.fetch(text).code, "news_list_missing_or_incomplete")
        self.assertEqual(self.fetch(html() + "<div class='datelist'></div>").code, "news_list_not_unique")

    def test_unrelated_future_and_malformed_rows_are_skipped_but_valid_titles_survive(self):
        rows = [("2026-10-23 12:10", "完全无关公司新闻", URL),
                ("2026-10-25 12:10", "中际旭创未来标题", URL),
                ("2026-02-30 12:10", "中际旭创错误日期", URL),
                ("2026-10-23 12:10", "<em>中际旭创</em>人工标题<script>bad()</script>", URL)]
        section = self.fetch(html(rows))
        self.assertEqual((section.status, section.skipped_count, len(section.items)), ("available", 3, 1))
        self.assertEqual(section.items[0].value, "中际旭创人工标题")
        self.assertNotIn("bad()", section.items[0].value)

    def test_article_urls_are_validated_for_display_and_never_fetched(self):
        urls = ("https://attacker.invalid/doc-test.shtml", "http://finance.sina.com.cn/doc-test.shtml",
                "https://finance.sina.com.cn/../doc-test.shtml", "https://finance.sina.com.cn@attacker.invalid/doc-test.shtml",
                "https://cj.sina.cn/articles/view/123/abc?secret=true", "https://cj.sina.cn/articles/view/123/abc")
        section = self.fetch(html([("2026-10-23 12:10", "中际旭创人工标题", url) for url in urls]))
        self.assertEqual((len(section.items), section.skipped_count), (1, 5))
        self.assertEqual(section.items[0].url, urls[-1])
        self.session.request.assert_called_once()

    def test_minute_precision_and_midnight_placeholders_do_not_leak_future_publication(self):
        self.request = replace(self.request, as_of=datetime(2026, 10, 23, 12, 10, 30, tzinfo=SHANGHAI))
        section = self.fetch()
        self.assertEqual(section.status, "empty")
        self.request = replace(self.request, as_of=datetime(2026, 10, 23, 12, 10, 59, tzinfo=SHANGHAI))
        self.assertEqual(self.fetch().status, "available")
        text = html([("2026-10-23 00:00", "中际旭创人工标题", URL)])
        self.assertEqual(self.fetch(text).status, "empty")
        self.request = replace(self.request, as_of=datetime(2026, 10, 23, 23, 59, 59, tzinfo=SHANGHAI))
        item = self.fetch(text).items[0]
        self.assertEqual(item.publication_precision, "date")
        self.assertIsNone(item.published_at)

    def test_empty_page_and_old_records_do_not_claim_news_does_not_exist(self):
        section = self.fetch(html([]))
        self.assertEqual(section.status, "empty")
        self.assertIn("不代表没有", section.message)
        section = self.fetch(html([("2026-01-01 12:10", "中际旭创人工旧标题", URL)]))
        self.assertEqual((section.status, section.skipped_count), ("empty", 1))

    def test_items_are_deduplicated_bounded_and_stable_across_retrievals(self):
        rows = [("2026-10-23 12:10", "中际旭创人工标题", URL)] * 3
        section = self.fetch(html(rows))
        self.assertEqual(len(section.items), 1)
        later = SinaCompanyNewsAdapter(session=self.session, clock=lambda: NOW + timedelta(minutes=1))
        self.assertEqual(section.items[0].id, later.fetch_section(self.request).items[0].id)
        rows = [("2026-10-23 12:10", "中际旭创人工标题" + str(i), URL) for i in range(10)]
        self.assertEqual(len(self.fetch(html(rows)).items), 5)
        self.assertEqual(self.fetch(html(rows * 6)).code, "source_record_limit_exceeded")

    def test_failures_close_responses_without_retry_or_raw_error_export(self):
        for status, code in ((302, "source_redirect_blocked"), (503, "source_http_error")):
            result = response(status=status)
            self.session.request.return_value = result
            section = self.adapter.fetch_section(self.request)
            self.assertEqual(section.code, code)
            result.close.assert_called_once()
        self.session.request.side_effect = requests.Timeout(PRIVATE)
        self.session.request.reset_mock()
        section = self.adapter.fetch_section(self.request)
        self.assertEqual(section.code, "source_connection_failed")
        self.assertNotIn(PRIVATE, json.dumps(section.to_dict()))
        self.session.request.assert_called_once()

    def test_byte_deadline_and_encoding_bounds_fail_cleanly(self):
        cases = (({"headers": {"Content-Length": "9999999"}}, "source_response_too_large"),
                 ({"chunks": [b"x" * 512001]}, "source_response_too_large"),
                 ({"chunks": [b"\x81"]}, "invalid_source_encoding"))
        for kwargs, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.fetch(**kwargs).code, code)
        self.session.request.reset_mock()
        section = self.adapter.fetch_section(self.request, deadline=0)
        self.assertEqual(section.code, "research_data_deadline_exceeded")
        self.session.request.assert_not_called()

    def test_future_cutoff_and_cancellation_fail_before_network(self):
        with self.assertRaises(InvalidRequestError):
            self.adapter.fetch_section(replace(self.request, as_of=NOW + timedelta(seconds=1)))
        control = TurnControl(TurnLimits(model_calls=1))
        control.start()
        control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped):
            self.adapter.fetch_section(self.request)
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
