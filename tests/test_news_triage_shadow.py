"""Title-only snapshots preserve research inputs and fail independently of the answer."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.news_triage_shadow import NewsTitleCapture, title_snapshot
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_research import collect_product_research
from trading_companion.research_session import ResearchSession
from trading_companion.turn_control import bind_turn, company_turn_control, TurnStopped
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory, plan, SECURITIES


def public_data(*, unavailable=(), title=None):
    def transform(batch):
        sources = {"news": "sina_company_news", "announcements": "eastmoney_announcements"}
        return replace(batch, source_results=tuple(replace(section,
            source=sources.get(section.kind, section.source),
            items=tuple(replace(item, source=sources.get(section.kind, section.source),
                value=title if title is not None and item.content_kind == "title" else item.value,
                url=("https://data.eastmoney.com/notices/detail/" + item.instrument_id[:6] + "/AN202609251234567890.html"
                     if section.kind == "announcements" else "https://finance.sina.com.cn/stock/doc-test123.shtml"))
                for item in section.items))
            for section in batch.source_results))
    return CompanyDataFixture(transform=transform, unavailable=unavailable)


class NewsTitleCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "capture"
        for guard in (patch("socket.socket.connect", side_effect=AssertionError("No network")),
                      patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))):
            guard.start()
            self.addCleanup(guard.stop)

    def bundle(self, **kwargs):
        return collect_product_research(plan(kinds=("news", "announcements"), **kwargs),
                                        data_service=public_data(), now=NOW)

    def test_lazy_unique_runs_and_whitelisted_public_fields_preserve_full_title(self):
        title = "人工标题" * 140
        selected = replace(plan(kinds=("news", "announcements")), user_question="private-question-marker")
        bundle = collect_product_research(selected, data_service=public_data(title=title), now=NOW)
        capture = NewsTitleCapture(self.root)
        self.assertFalse(self.root.exists())
        self.assertTrue(capture.capture(bundle, captured_at=NOW))
        record = json.loads(next(capture.directory.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["title_count"], 2)
        self.assertEqual(record["model_calls"], 0)
        self.assertEqual(record["companies"][0]["titles"][0]["title"], title)
        self.assertNotIn("private-question-marker", json.dumps(record))
        self.assertNotIn("user_question", json.dumps(record))
        self.assertNotIn("message", json.dumps(record))
        second = NewsTitleCapture(self.root)
        self.assertTrue(second.capture(bundle, captured_at=NOW))
        self.assertNotEqual(capture.directory, second.directory)
        self.assertEqual(len(list(capture.directory.glob("*.json"))), 1)

    def test_three_company_snapshot_keeps_identity_and_no_empty_source_is_hidden(self):
        bundle = collect_product_research(plan(securities=SECURITIES[:3], kinds=("news", "announcements")),
            data_service=public_data(unavailable=("news",)), now=NOW)
        record = title_snapshot(bundle, captured_at=NOW)
        self.assertEqual(record["title_count"], 3)
        self.assertEqual(len(record["companies"]), 3)
        for company in record["companies"]:
            self.assertEqual(company["source_results"][0]["status"], "unavailable")
            self.assertEqual({t["instrument_id"] for t in company["titles"]}, {company["instrument_id"]})

    def test_capacity_and_disk_failure_disable_writes_without_raw_exception_or_repeated_warning(self):
        warning = Mock()
        capture = NewsTitleCapture(self.root, max_bytes=1, warning=warning)
        self.assertFalse(capture.capture(self.bundle(), captured_at=NOW))
        self.assertEqual(capture.status()["stop_code"], "capture_capacity_reached")
        self.assertFalse(self.root.exists())
        self.assertFalse(capture.capture(self.bundle(), captured_at=NOW))
        warning.assert_called_once_with()
        capture = NewsTitleCapture(self.root, warning=Mock())
        with patch.object(Path, "mkdir", side_effect=OSError("private-error-marker")):
            self.assertFalse(capture.capture(self.bundle(), captured_at=NOW))
        self.assertEqual(capture.status()["stop_code"], "capture_unavailable")
        self.assertNotIn("private-error-marker", json.dumps(capture.status()))

    def test_invalid_identity_future_timestamp_and_overlong_titles_are_not_captured(self):
        bundle = self.bundle()
        company = bundle.companies[0]
        for changes in ({"instrument_id": "600000.SH"}, {"raw_value": "x" * 601},
                        {"published_at": (NOW + timedelta(seconds=1)).isoformat()},
                        {"publication_precision": "unknown"}):
            with self.subTest(changes=changes):
                tampered = replace(bundle, companies=(replace(company,
                    evidence=(replace(company.evidence[0], **changes),)),))
                capture = NewsTitleCapture(self.root)
                self.assertFalse(capture.capture(tampered, captured_at=NOW))
                self.assertFalse(self.root.exists())

    def test_cancellation_propagates_instead_of_becoming_logging_failure(self):
        capture = NewsTitleCapture(self.root)
        bundle = self.bundle()
        control = company_turn_control()
        control.start()
        control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped):
            capture.capture(bundle, captured_at=NOW)
        self.assertFalse(self.root.exists())

    def test_on_off_and_failed_capture_leave_answer_context_evidence_and_call_count_equal(self):
        results, contexts, calls = [], [], []
        for state in ("off", "on", "broken"):
            capture = NewsTitleCapture(self.root / state) if state != "off" else None
            if state == "broken":
                capture.root.parent.mkdir(parents=True, exist_ok=True)
                capture.root.write_text("file blocks capture directory", encoding="utf-8")
            model = ProductModelFixture()
            result = run_product_research_turn("分析平安银行的基本面和新闻", session=ResearchSession(), model=model,
                directory=directory(), now=NOW, source="tencent", data_service=public_data(),
                source_factory=MemorySourceFactory(), headline_capture=capture)
            results.append((result.status, result.product_answer, result.product_bundle))
            contexts.append(model.contexts)
            calls.append(result.model_calls_attempted)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertEqual(contexts, [contexts[0]] * 3)
        self.assertEqual(calls, [2, 2, 2])

    def test_historical_and_development_research_do_not_write(self):
        capture = NewsTitleCapture(self.root)
        run_product_research_turn("分析平安银行的新闻", prepared=plan(kinds=("news",), historical=True),
            session=ResearchSession(), model=ProductModelFixture(), directory=directory(), now=NOW,
            source="tencent", data_service=public_data(), headline_capture=capture)
        self.assertFalse(self.root.exists())
        run_product_research_turn("分析平安银行的新闻", prepared=plan(kinds=("news",)),
            session=ResearchSession(), model=ProductModelFixture(), directory=directory(), now=NOW,
            source="tencent", development_source=object(), data_service=public_data(), headline_capture=capture)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
