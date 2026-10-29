"""Public-context contracts use artificial HTTP fixtures; no credentials or external requests."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.company_research import ResearchDataRequest, ResearchDataService
from trading_companion.company_research_contracts import SHANGHAI
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn


NOW = datetime(2026, 10, 25, 12, tzinfo=SHANGHAI)
CUTOFF = datetime(2026, 10, 24, 23, 59, 59, tzinfo=SHANGHAI)
PRIVATE = "private-data-source-marker-never-export"


def response(payload=None, *, text=None, status=200, headers=None, chunks=None):
    result = Mock(status_code=status, headers=headers or {})
    body = json.dumps(payload, ensure_ascii=False).encode() if text is None else text.encode()
    result.iter_content.return_value = [body] if chunks is None else chunks
    return result


def profile():
    return {"success": True, "result": {"data": [{"SECUCODE": "300308.SZ", "SECURITY_CODE": "300308",
        "ORG_NAME": "人工公司名称", "MAIN_BUSINESS": "人工业务描述", "CSRC_INDUSTRY_NAME": "人工行业",
        "ORG_PROFILE": "人工简介", "LISTING_DATE": "2012-04-10 00:00:00"}]}}


def financial_row(**changes):
    result = {"SECUCODE": "300308.SZ", "SECURITY_CODE": "300308", "REPORT_DATE": "2026-06-30 00:00:00",
        "NOTICE_DATE": "2026-08-22 00:00:00", "UPDATE_DATE": "2026-08-22 00:00:00", "CURRENCY": "CNY",
        "TOTALOPERATEREVE": 123000000, "PARENTNETPROFIT": -4560000, "TOTALOPERATEREVETZ": 25.5,
        "PARENTNETPROFITTZ": -15.5, "ROEJQ": 8.25}
    result.update(changes)
    return result


def financial(rows=None):
    return {"success": True, "result": {"data": [financial_row()] if rows is None else rows}}


def announcement(**changes):
    result = {"art_code": "AN202610231234567890", "title": "人工公告标题",
        "codes": [{"stock_code": "300308", "market_code": "0"}],
        "display_time": "2026-10-23 12:30:51:811", "notice_date": "2026-10-23 00:00:00"}
    result.update(changes)
    return result


def announcements(rows=None):
    return {"success": True, "data": {"list": [announcement()] if rows is None else rows}}


def news_row(**changes):
    result = {"code": "202610230001234567", "title": "<em>中际旭创</em>人工新闻",
        "content": "人工测试摘要，不是真实新闻。", "date": "2026-10-23 10:00:00", "mediaName": "人工媒体"}
    result.update(changes)
    return result


def news(rows=None):
    return {"code": 0, "result": {"cmsArticleWebOld": [news_row()] if rows is None else rows}}


class CompanyResearchTests(unittest.TestCase):
    def test_financial_response_keeps_five_eligible_periods_without_extra_request(self):
        rows = [financial_row(REPORT_DATE=period + " 00:00:00") for period in
                ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
        section = self.fetch("financial", financial(rows))
        self.assertEqual(len(section.items), 25)
        self.assertEqual(len({item.id for item in section.items}), 25)
        self.assertEqual(len({item.report_period for item in section.items}), 5)
        self.assertEqual(self.session.request.call_count, 1)

    def setUp(self):
        self.session = Mock()
        self.service = ResearchDataService(session=self.session, clock=lambda: NOW, news_provider="eastmoney")
        self.request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF, date(2026, 7, 1),
                                           allow_current_background=True)
        for guard in (patch("requests.get", side_effect=AssertionError("No external HTTP")),
                      patch("requests.post", side_effect=AssertionError("No external HTTP")),
                      patch("requests.request", side_effect=AssertionError("No external HTTP")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads"))):
            guard.start()
            self.addCleanup(guard.stop)

    def fetch(self, kind, payload, **kwargs):
        self.session.request.return_value = response(payload, **kwargs)
        return self.service.fetch_section(self.request, kind)

    def test_request_rejects_unknown_symbols_naive_times_future_windows_or_excess_limits(self):
        cases = ({"instrument_id": "https://attacker.invalid"}, {"instrument_id": "300308.SH"},
            {"as_of": CUTOFF.replace(tzinfo=None)}, {"start_date": date(2026, 10, 25)},
            {"start_date": date(2024, 1, 1)}, {"limit": 6}, {"limit": True},
            {"security_name": "bad\nname"}, {"allow_current_background": 1})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(InvalidRequestError):
                replace(self.request, **changes)
        for changes in ({"timeout_seconds": 0}, {"timeout_seconds": 11}, {"deadline_seconds": 31},
                        {"announcement_provider": PRIVATE}):
            with self.assertRaises(InvalidRequestError):
                ResearchDataService(**changes)
        self.session.request.assert_not_called()

    def test_future_cutoff_or_unknown_sections_fail_before_network(self):
        with self.assertRaises(InvalidRequestError):
            self.service.fetch_section(replace(self.request, as_of=NOW + timedelta(seconds=1)), "financial")
        for kinds in (("financial", "financial"), ("unknown",), (), (["financial"],), ["financial"]):
            with self.subTest(kinds=kinds), self.assertRaises(InvalidRequestError):
                self.service.collect(self.request, kinds)
        self.session.request.assert_not_called()

    def test_many_requests_are_validated_together_before_any_network(self):
        second = replace(self.request, instrument_id="000001.SZ", security_name="平安银行")
        third = replace(self.request, instrument_id="600000.SH", security_name="浦发银行")
        for requests_ in ((), [self.request], (None,), (self.request, second, third, self.request),
                          (self.request, second, self.request), (self.request, replace(second, as_of=NOW + timedelta(seconds=1)))):
            with self.subTest(requests=requests_), self.assertRaises(InvalidRequestError):
                self.service.collect_many(requests_)
        for kinds in (("financial", "financial"), (), ["financial"], ("technical",), (True,)):
            with self.subTest(kinds=kinds), self.assertRaises(InvalidRequestError):
                self.service.collect_many((self.request, second), kinds)
        self.session.request.assert_not_called()

    def test_single_collect_delegates_without_changing_return_type(self):
        expected = object()
        with patch.object(self.service, "collect_many", return_value=(expected,)) as many:
            self.assertIs(self.service.collect(self.request, ("financial",)), expected)
        many.assert_called_once_with((self.request,), ("financial",))

    def test_profile_requires_explicit_current_background_and_never_invents_publication(self):
        blocked = self.service.fetch_section(replace(self.request, allow_current_background=False), "profile")
        self.assertEqual((blocked.status, blocked.code), ("unavailable", "current_background_not_allowed"))
        self.session.request.assert_not_called()
        section = self.fetch("profile", profile())
        self.assertEqual((section.status, len(section.items)), ("available", 5))
        for item in section.items:
            self.assertIsNone(item.published_at)
            self.assertIsNone(item.published_date)
            self.assertEqual((item.publication_precision, item.temporal_scope, item.content_kind),
                             ("unknown", "current_context", "background"))
            self.assertEqual(item.retrieved_at, NOW)

    def test_financial_units_report_period_and_date_precision_are_preserved(self):
        section = self.fetch("financial", financial())
        self.assertEqual(section.status, "available")
        values = {item.metric: item for item in section.items}
        self.assertEqual((values["revenue"].value, values["revenue"].unit), (123000000.0, "cny"))
        self.assertEqual((values["parent_net_profit"].value, values["parent_net_profit"].unit), (-4560000.0, "cny"))
        self.assertEqual((values["revenue_yoy"].value, values["revenue_yoy"].unit), (25.5, "percent"))
        for item in section.items:
            self.assertEqual(item.report_period, date(2026, 6, 30))
            self.assertEqual(item.published_date, date(2026, 8, 22))
            self.assertEqual(item.publication_precision, "date")
            self.assertIsNone(item.published_at)
            self.assertNotEqual(item.published_date, item.retrieved_at.date())
            self.assertIn("historical revision versions are not verified", " ".join(item.warnings))

    def test_financial_filters_future_publication_and_future_restatement_before_selecting_latest_report(self):
        rows = [financial_row(REPORT_DATE="2026-09-30 00:00:00", NOTICE_DATE="2026-10-30 00:00:00",
                              UPDATE_DATE="2026-10-30 00:00:00"),
                financial_row(UPDATE_DATE="2026-10-25 00:00:00"), financial_row()]
        section = self.fetch("financial", financial(rows))
        self.assertEqual(section.status, "available")
        self.assertEqual(section.skipped_count, 2)
        self.assertTrue(all(item.report_period == date(2026, 6, 30) for item in section.items))

    def test_date_only_publication_is_not_assumed_available_intraday(self):
        self.request = replace(self.request, as_of=datetime(2026, 8, 22, 12, tzinfo=SHANGHAI))
        section = self.fetch("financial", financial())
        self.assertEqual((section.status, section.items), ("empty", ()))
        self.request = replace(self.request, as_of=datetime(2026, 8, 22, 23, 59, 59, tzinfo=SHANGHAI))
        self.assertEqual(self.fetch("financial", financial()).status, "available")

    def test_unknown_currency_never_becomes_cny_but_valid_percentages_survive(self):
        section = self.fetch("financial", financial([financial_row(CURRENCY="USD")]))
        self.assertEqual(section.status, "available")
        self.assertNotIn("cny", {item.unit for item in section.items})
        self.assertEqual(len(section.items), 3)
        self.assertEqual(section.skipped_count, 2)

    def test_date_only_revision_is_not_assumed_available_before_the_end_of_that_day(self):
        self.request = replace(self.request, as_of=datetime(2026, 10, 24, 12, tzinfo=SHANGHAI))
        section = self.fetch("financial", financial([financial_row(UPDATE_DATE="2026-10-24 00:00:00")]))
        self.assertEqual((section.status, section.skipped_count), ("empty", 1))

    def test_null_boolean_nan_and_unparseable_financial_values_are_missing_not_zero(self):
        for value in (None, True, "NaN", "Infinity", "--", {}, "1e9999"):
            with self.subTest(value=value):
                section = self.fetch("financial", financial([financial_row(TOTALOPERATEREVE=value)]))
                self.assertNotIn("revenue", {item.metric for item in section.items})
                self.assertGreater(section.skipped_count, 0)

    def test_wrong_security_cannot_be_accepted_for_profile_or_financials(self):
        wrong = profile()
        wrong["result"]["data"][0]["SECUCODE"] = "000001.SZ"
        self.assertEqual(self.fetch("profile", wrong).code, "source_instrument_mismatch")
        section = self.fetch("financial", financial([financial_row(SECUCODE="000001.SZ")]))
        self.assertEqual(section.items, ())
        self.assertEqual(section.skipped_count, 1)

    def test_news_callback_articles_and_company_association_must_match(self):
        encoded = json.dumps(news(), ensure_ascii=False)
        section = self.fetch("news", None, text="tcCompanyResearch(" + encoded + ");\n")
        self.assertEqual(section.status, "available")
        item = section.items[0]
        self.assertEqual(item.content_kind, "snippet")
        self.assertNotIn("<em>", item.value)
        self.assertIn("article body was not retrieved", " ".join(item.warnings))
        for text, code in (("wrongCallback(" + encoded + ")", "news_callback_mismatch"),
                           ("tcCompanyResearch(" + encoded + ");evil()", "news_callback_mismatch"),
                           ('{"code":0,"result":{"passportWeb":[]}}', "news_article_result_missing")):
            with self.subTest(code=code):
                result = self.fetch("news", None, text=text)
                self.assertEqual((result.status, result.code), ("unavailable", code))
        other = self.fetch("news", news([news_row(title="完全无关的公司", content="没有证券关联")]))
        self.assertEqual((other.status, other.skipped_count), ("empty", 1))

    def test_news_filters_future_dates_and_does_not_follow_returned_links(self):
        rows = [news_row(date="2026-10-25 10:00:00"),
                news_row(code="https://attacker.invalid/secret"),
                news_row(url="http://127.0.0.1/private", content="中际旭创人工摘要")]
        section = self.fetch("news", news(rows))
        self.assertEqual((section.status, len(section.items), section.skipped_count), ("available", 1, 2))
        self.assertTrue(section.items[0].url.startswith("https://finance.eastmoney.com/a/"))
        self.session.request.assert_called_once()

    def test_announcements_validate_instrument_time_and_title_only_content(self):
        rows = [announcement(codes=[{"stock_code": "000001", "market_code": "0"}]),
                announcement(display_time="2026-10-25 12:00:00:000"), announcement()]
        section = self.fetch("announcements", announcements(rows))
        self.assertEqual((section.status, len(section.items), section.skipped_count), ("available", 1, 2))
        item = section.items[0]
        self.assertEqual(item.content_kind, "title")
        self.assertEqual(item.published_at, datetime(2026, 10, 23, 12, 30, 51, 811000, tzinfo=SHANGHAI))
        self.assertIn("vendor display time", " ".join(item.warnings))
        self.assertNotIn("full_text", item.to_dict())

    def test_optional_cninfo_uses_validated_org_identity_without_fetching_pdf_urls(self):
        self.service = ResearchDataService(session=self.session, clock=lambda: NOW, announcement_provider="cninfo")
        directory = {"stockList": [{"code": "300308", "orgId": "9900022016", "zwjc": "中际旭创"}]}
        row = {"secCode": "300308", "orgId": "9900022016", "announcementId": "1225617691",
               "announcementTitle": "人工披露标题", "announcementTime": int(datetime(2026, 10, 23, 12, tzinfo=SHANGHAI).timestamp() * 1000),
               "adjunctUrl": "https://attacker.invalid/secret"}
        self.session.request.side_effect = [response(directory), response({"announcements": [row]})]
        section = self.service.fetch_section(self.request, "announcements")
        self.assertEqual((section.status, section.source), ("available", "cninfo_announcements"))
        self.assertEqual(self.session.request.call_count, 2)
        self.assertNotIn("attacker", section.items[0].url)
        self.assertTrue(section.items[0].url.startswith("https://www.cninfo.com.cn/new/disclosure/detail?"))
        posted = self.session.request.call_args
        self.assertEqual(posted.args[:2], ("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query"))
        self.assertEqual(posted.kwargs["data"]["stock"], "300308,9900022016")

    def test_announcement_date_without_display_time_does_not_invent_midnight_publication(self):
        self.request = replace(self.request, as_of=datetime(2026, 10, 23, 12, tzinfo=SHANGHAI))
        section = self.fetch("announcements", announcements([announcement(display_time=None)]))
        self.assertEqual((section.status, section.skipped_count), ("empty", 1))
        self.request = replace(self.request, as_of=datetime(2026, 10, 23, 23, 59, 59, tzinfo=SHANGHAI))
        section = self.fetch("announcements", announcements([announcement(display_time=None)]))
        self.assertEqual(section.status, "available")
        self.assertIsNone(section.items[0].published_at)
        self.assertEqual(section.items[0].publication_precision, "date")

    def test_unverified_announcement_exchange_mapping_is_explicitly_unavailable_without_network(self):
        request = replace(self.request, instrument_id="920000.BJ")
        section = self.service.fetch_section(request, "announcements")
        self.assertEqual(section.code, "announcement_exchange_mapping_unverified")
        self.service = ResearchDataService(session=self.session, clock=lambda: NOW, announcement_provider="cninfo")
        for instrument_id in ("600000.SH", "920000.BJ"):
            with self.subTest(instrument_id=instrument_id):
                section = self.service.fetch_section(replace(request, instrument_id=instrument_id), "announcements")
                self.assertEqual(section.code, "cninfo_exchange_mapping_unverified")
        self.session.request.assert_not_called()

    def test_collect_keeps_valid_sections_when_news_connection_fails(self):
        self.session.request.side_effect = [response(profile()), response(financial()),
            requests.Timeout(PRIVATE), response(announcements())]
        batch = self.service.collect(self.request)
        self.assertEqual(batch.status, "partial")
        self.assertEqual([section.status for section in batch.source_results],
                         ["available", "available", "unavailable", "available"])
        self.assertEqual(len(batch.items), 11)
        self.assertNotIn(PRIVATE, json.dumps(batch.to_dict()))
        self.assertEqual(self.session.request.call_count, 4)

    def test_redirect_and_invalid_json_are_sanitized_without_retry_or_fallback(self):
        for kwargs, code in (({"status": 302}, "source_redirect_blocked"),
                             ({"status": 503}, "source_http_error"),
                             ({"text": "not-json " + PRIVATE}, "invalid_source_json"),
                             ({"text": '{"success":true,"success":false}'}, "duplicate_source_json_key")):
            with self.subTest(code=code):
                self.session.request.reset_mock()
                section = self.fetch("financial", {}, **kwargs)
                self.assertEqual((section.status, section.code), ("unavailable", code))
                self.assertNotIn(PRIVATE, json.dumps(section.to_dict()))
                self.session.request.assert_called_once()

    def test_transport_bounds_declared_and_streamed_bytes_closes_responses_and_disallows_redirects(self):
        for kwargs in ({"headers": {"Content-Length": "99999999"}}, {"chunks": [b"x" * 300000, b"y" * 300000]}):
            with self.subTest(kwargs=list(kwargs)):
                result = response({}, **kwargs)
                self.session.request.return_value = result
                section = self.service.fetch_section(self.request, "financial")
                self.assertEqual(section.code, "source_response_too_large")
                result.close.assert_called_once()
                arguments = self.session.request.call_args.kwargs
                self.assertFalse(arguments["allow_redirects"])
                self.assertTrue(arguments["stream"])
                self.assertEqual(arguments["timeout"], (3.0, 6.0))
                self.assertNotIn("Authorization", arguments["headers"])
                self.assertNotIn("Cookie", arguments["headers"])

    def test_total_deadline_is_shared_across_sections_not_reset_for_each_source(self):
        from tests.test_collection_execution import MutableClock
        clock = MutableClock()
        self.service = ResearchDataService(session=self.session, clock=lambda: NOW, monotonic_clock=clock)
        def delayed(*args, **kwargs):
            clock.set(21)
            return response(profile())
        self.session.request.side_effect = delayed
        batch = self.service.collect(self.request)
        self.assertEqual(batch.status, "unavailable")
        self.assertTrue(all(section.code == "research_data_deadline_exceeded" for section in batch.source_results))
        self.session.request.assert_called_once()

    def test_turn_cancellation_propagates_without_becoming_a_source_failure(self):
        control = TurnControl(TurnLimits(model_calls=1))
        control.start()
        control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped):
            self.service.fetch_section(self.request, "financial")
        self.session.request.assert_not_called()

    def test_ids_are_stable_across_retrievals_but_change_with_the_actual_fact(self):
        first = self.fetch("financial", financial())
        later = ResearchDataService(session=self.session, clock=lambda: NOW + timedelta(seconds=1))
        second = later.fetch_section(self.request, "financial")
        self.assertEqual([item.id for item in first.items], [item.id for item in second.items])
        third = self.fetch("financial", financial([financial_row(TOTALOPERATEREVE=42)]))
        self.assertNotEqual(first.items[0].id, third.items[0].id)
        serialized = first.to_dict()
        serialized["items"].clear()
        self.assertEqual(len(first.items), 5)

    def test_empty_or_malformed_sources_do_not_imply_no_company_events_exist(self):
        section = self.fetch("announcements", announcements([]))
        self.assertEqual(section.status, "empty")
        self.assertIn("不代表没有", section.message)
        self.assertEqual(self.fetch("profile", {"success": False, "message": PRIVATE}).code, "source_business_error")
        self.assertEqual(self.fetch("financial", {"success": True, "result": {"data": "bad"}}).code, "invalid_source_rows")


if __name__ == "__main__":
    unittest.main()
