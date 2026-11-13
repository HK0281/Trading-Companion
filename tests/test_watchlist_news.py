"""Public-title validation and separate opaque list/check cursors, entirely offline."""

from datetime import datetime, timezone
import unittest

from trading_companion.company_research_contracts import ResearchEvidence
from trading_companion.watchlist_news import (
    SOURCES, WatchlistNewsError, checked_title, title_from_evidence, encode_cursor, decode_cursor,
)


NOW = datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc)


def title(number=1, *, source=SOURCES[0], day="2026-11-12", title_text="平安银行公共标题", **changes):
    url = (f"https://finance.sina.com.cn/stock/doc-test{number}.shtml" if source == SOURCES[0] else
           f"https://data.eastmoney.com/notices/detail/000001/AN{number:012d}.html")
    return {"title": title_text, "url": url, "published_date": day, "published_at": None,
            "publication_precision": "date", "retrieved_at": NOW.isoformat(), **changes}


class WatchlistNewsTests(unittest.TestCase):
    def checked(self, value, source=SOURCES[0], **kwargs):
        return checked_title(value, instrument_id="000001.SZ", source=source, **kwargs)

    def test_identity_preserves_document_and_date_but_not_corrected_title(self):
        first = self.checked(title())
        self.assertEqual(first["message_id"], self.checked(title(title_text="平安银行标题更正"))["message_id"])
        self.assertNotEqual(first["message_id"], self.checked(title(2))["message_id"])
        self.assertNotEqual(first["message_id"], self.checked(title(day="2026-11-11"))["message_id"])
        self.assertNotEqual(first["message_id"], self.checked(title(source=SOURCES[1]), SOURCES[1])["message_id"])

    def test_only_fixed_canonical_source_links_are_accepted(self):
        for url in ("https://evil.example/doc-a.shtml", "https://finance.sina.com.cn/stock/doc-test1.shtml?x=1",
                    "http://finance.sina.com.cn/doc-a.shtml", "https://finance.sina.com.cn@evil.example/doc-a.shtml"):
            with self.subTest(url=url), self.assertRaises(WatchlistNewsError):
                self.checked(title(url=url))
        with self.assertRaises(WatchlistNewsError):
            self.checked(title(source=SOURCES[1], url="https://data.eastmoney.com/notices/detail/600000/AN000000000001.html"), SOURCES[1])
        cj = self.checked(title(url="https://cj.sina.cn/articles/view/12345/Ab12"))
        self.assertEqual(cj["document_key"], "sina-cj:12345:Ab12")

    def test_unknown_precision_unsafe_title_and_extra_fields_are_rejected(self):
        for changes in ({"title": "<script>\nprivate</script>"}, {"title": " 标题"}, {"title": "x" * 601},
                        {"question": "not allowed"}, {"publication_precision": "unknown"},
                        {"published_at": "2026-11-12T00:00:00+08:00"}, {"published_date": "2026-02-30"}):
            with self.subTest(changes=changes), self.assertRaises(WatchlistNewsError):
                self.checked(title(**changes))

    def test_date_precision_is_never_promoted_to_intraday_and_minute_boundary_is_conservative(self):
        date_only = title(day="2026-11-11")
        self.assertIsNone(self.checked(date_only, checked_at=NOW)["published_at"])
        with self.assertRaises(WatchlistNewsError):
            self.checked(title(), checked_at=NOW)
        exact = title(publication_precision="datetime", published_at="2026-11-12T20:29:00+08:00")
        self.checked(exact, checked_at=NOW)
        with self.assertRaises(WatchlistNewsError):
            self.checked(title(publication_precision="datetime", published_at="2026-11-12T20:30:00+08:00"), checked_at=NOW)
        with self.assertRaises(WatchlistNewsError):
            self.checked(title(day="2026-09-01"), checked_at=NOW)
        millisecond = title(source=SOURCES[1], publication_precision="datetime", published_at="2026-11-12T20:29:00.123+08:00")
        self.assertEqual(self.checked(millisecond, SOURCES[1], checked_at=NOW)["published_at"], "2026-11-12T20:29:00.123000+08:00")

    def test_evidence_conversion_rejects_snippets_and_wrong_sources(self):
        evidence = ResearchEvidence("company:test", "000001.SZ", "news", SOURCES[0], "news_title", "平安银行标题",
            "text", title()["url"], None, NOW.date(), "date", NOW, content_kind="title")
        self.assertEqual(title_from_evidence(evidence)["title"], evidence.value)
        from dataclasses import replace
        for changed in (replace(evidence, content_kind="snippet"), replace(evidence, source="eastmoney_news")):
            with self.assertRaises(WatchlistNewsError):
                title_from_evidence(changed)

    def test_check_and_list_cursors_are_separate_bounded_strict_contracts(self):
        payload = {"watchlist_revision": "a" * 64, "position": 7}
        cursor = encode_cursor("check", payload)
        self.assertEqual(decode_cursor(cursor, "check"), payload)
        for value, kind in ((cursor, "list"), (cursor + "=", "check"), ("x" * 2049, "check"), ("%bad", "check")):
            with self.subTest(value=value[:20]), self.assertRaisesRegex(WatchlistNewsError, "inbox_invalid_cursor"):
                decode_cursor(value, kind)
        for changed in ({**payload, "position": True}, {**payload, "position": 101}, {**payload, "extra": 0}):
            with self.assertRaises(WatchlistNewsError):
                encode_cursor("check", changed)


if __name__ == "__main__":
    unittest.main()
