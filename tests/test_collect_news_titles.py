"""Collection is preview-only by default; live-path tests inject all public responses."""

from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from scripts.collect_news_titles import COMPANIES, _LimitedTransport, build_plan, collect_titles, main
from tests.test_company_news_sina import html, response as html_response, URL
from tests.test_company_research import announcement, announcements, response as json_response
from tests.test_product_research import NOW


class Clock:
    def __init__(self):
        self.seconds, self.sleeps = 0.0, []

    def __call__(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds


class PublicSession:
    def __init__(self, clock, *, fail_first=False, empty=False):
        self.clock, self.starts, self.calls, self.closed = clock, [], [], False
        self.fail_first, self.empty = fail_first, empty

    def request(self, method, url, **kwargs):
        self.starts.append(self.clock())
        self.calls.append((method, url, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise requests.Timeout("private-transport-error")
        if "sina.com.cn" in url:
            symbol = kwargs["params"]["symbol"]
            code = symbol[2:] + "." + symbol[:2].upper()
            name = next(row[1] for row in COMPANIES if row[0] == code)
            rows = [] if self.empty else [("2026-09-25 12:10", name + "人工新闻" + str(i), URL) for i in range(8)]
            return html_response(html(rows, title=f"{name}({symbol[2:]})个股资讯_新浪财经_新浪网"))
        code = kwargs["params"]["stock_list"]
        market = "1" if code.startswith("6") else "0"
        rows = [] if self.empty else [announcement(art_code="AN20260925123456789" + str(i),
            title="人工公告" + str(i), codes=[{"stock_code": code, "market_code": market}],
            display_time="2026-09-25 12:30:00") for i in range(5)]
        return json_response(announcements(rows))

    def close(self):
        self.closed = True


class CollectNewsTitlesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "collection"
        for guard in (patch("socket.socket.connect", side_effect=AssertionError("No network")),
                      patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))):
            guard.start()
            self.addCleanup(guard.stop)

    def collect(self, *, selected=("300308.SZ",), limit=2, **session_options):
        self.clock = Clock()
        self.session = PublicSession(self.clock, **session_options)
        return collect_titles(build_plan(selected=selected, max_requests=limit), output_root=self.root,
            clock=lambda: NOW, timer=self.clock, sleeper=self.clock.sleep, session_factory=lambda: self.session)

    def test_default_preview_lists_fifty_companies_without_transport_storage_or_credentials(self):
        with patch("requests.Session", side_effect=AssertionError("Preview must not construct HTTP")), \
                patch("scripts.collect_news_titles.NewsTitleCapture", side_effect=AssertionError("No storage")), \
                redirect_stdout(StringIO()) as output:
            self.assertEqual(main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((len(plan["companies"]), plan["planned_network_requests"], plan["max_title_records"]), (50, 100, 500))
        self.assertEqual({c["board"] for c in plan["companies"]}, {"sh_main", "sz_main", "chinext", "star"})
        self.assertEqual(len({c["instrument_id"] for c in plan["companies"]}), 50)
        self.assertEqual(plan["model_calls"], 0)
        self.assertFalse(self.root.exists())

    def test_invalid_or_unbudgeted_live_fails_before_constructing_http(self):
        for flags in (["--live"], ["--live", "--max-requests", "101"],
                      ["--company", "https://invalid"], ["--max-requests", "0"]):
            with self.subTest(flags=flags), patch("requests.Session") as client, redirect_stdout(StringIO()):
                self.assertEqual(main(flags), 2)
                client.assert_not_called()

    def test_real_adapters_limit_each_source_to_five_and_use_identical_capture_schema(self):
        report = self.collect()
        self.assertEqual(report["status"], "completed")
        self.assertEqual((report["network_requests_attempted"], report["capture"]["titles_written"]), (2, 10))
        records = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*/*.json")]
        self.assertEqual(len(records), 2)
        self.assertEqual({r["version"] for r in records}, {"news-title-capture-v1"})
        self.assertEqual({r["origin"] for r in records}, {"collection"})
        self.assertTrue(all(r["title_count"] == 5 and r["model_calls"] == 0 for r in records))
        self.assertEqual(self.session.starts, [0, 1])
        self.assertTrue(self.session.closed)
        self.assertNotIn("user_question", json.dumps(records))
        for method, url, kwargs in self.session.calls:
            self.assertEqual(method, "GET")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertNotIn("Authorization", kwargs["headers"])

    def test_failed_attempts_count_and_limit_stops_before_next_source_or_company(self):
        report = self.collect(selected=("000001.SZ", "300308.SZ"), limit=1, fail_first=True)
        self.assertEqual((report["status"], report["code"]), ("partial", "collection_request_budget_exhausted"))
        self.assertEqual((report["network_requests_attempted"], len(self.session.calls)), (1, 1))
        self.assertEqual(report["source_sections_not_run"], 3)
        self.assertEqual(report["source_sections_unavailable"], 1)
        self.assertNotIn("private-transport-error", json.dumps(report))

    def test_failure_is_not_retried_and_next_source_still_obeys_interval(self):
        report = self.collect(fail_first=True)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["network_requests_attempted"], 2)
        self.assertEqual(self.session.starts, [0, 1])
        self.assertEqual(report["capture"]["titles_written"], 5)

    def test_empty_lists_are_recorded_with_explicit_raw_sample_shortfall(self):
        report = self.collect(empty=True)
        self.assertEqual(report["capture"]["records_written"], 2)
        self.assertEqual(report["raw_title_shortfall_for_400"], 400)
        self.assertEqual(report["annotation_status"], "not_annotated_or_group_deduplicated")

    def test_all_fifty_companies_stay_within_one_hundred_serial_paced_requests(self):
        report = self.collect(selected=None, limit=100)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["network_requests_attempted"], 100)
        self.assertEqual(report["capture"]["titles_written"], 500)
        self.assertEqual(report["unique_company_titles"], 500)
        self.assertEqual(report["raw_title_shortfall_for_400"], 0)
        self.assertTrue(all(b - a >= 1 for a, b in zip(self.session.starts, self.session.starts[1:])))

    def test_write_failure_stops_more_collection_and_request_plan_cannot_be_expanded(self):
        self.root.write_text("block capture directory", encoding="utf-8")
        report = self.collect()
        self.assertEqual((report["code"], report["network_requests_attempted"]), ("capture_unavailable", 1))
        plan = build_plan(selected=("300308.SZ",), max_requests=2)
        plan["titles_per_source"] = 100
        with patch("requests.Session") as client, self.assertRaises(ValueError):
            collect_titles(plan)
        client.assert_not_called()

    def test_transport_rejects_other_endpoints_and_preserves_bound_after_direct_calls(self):
        timer = Clock()
        client = Mock()
        transport = _LimitedTransport(client, 1, timer=timer, sleeper=timer.sleep)
        with self.assertRaises(ValueError):
            transport.request("POST", "https://api.typesafe.ai/v1/systemone", allow_redirects=False)
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        transport.request("GET", url, allow_redirects=False)
        with self.assertRaises(requests.RequestException):
            transport.request("GET", url, allow_redirects=False)
        client.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
