"""Product collectors run against artificial in-memory sources, never network data."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import unittest
from unittest.mock import patch

from trading_companion.company_research_contracts import (
    SHANGHAI, ResearchDataBatch, ResearchDataSection, ResearchEvidence,
)
from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import TurnStopped, bind_turn, company_turn_control


NOW = datetime(2026, 9, 26, 12, tzinfo=SHANGHAI)
SECURITIES = (Security("000001.SZ", "平安银行", ("平安",)),
              Security("600000.SH", "浦发银行"),
              Security("600036.SH", "招商银行"),
              Security("601318.SH", "中国平安", ("平安",)))


def directory():
    return SecurityDirectory(SECURITIES, "artificial-directory", NOW)


def plan(*, securities=SECURITIES[:1], kinds=("profile", "financial"), wants_risk=False, historical=False):
    return ProductPlan(status="ready", securities=tuple(securities), kinds=tuple(kinds),
        start_date=date(2025, 9, 26), end_date=date(2026, 9, 25), as_of=date(2026, 9, 25),
        source="tencent", explicit_historical=historical, wants_risk=wants_risk, user_question="人工公司研究")


class CompanyDataFixture:
    """Contract-faithful domain records with optional source failures and tampering hooks."""

    def __init__(self, *, unavailable=(), transform=None, on_collect=None):
        self.calls, self.unavailable, self.transform, self.on_collect = [], set(unavailable), transform, on_collect

    def collect(self, request, kinds):
        self.calls.append((request, kinds))
        sections = []
        for kind in kinds:
            blocked = kind in self.unavailable or kind == "profile" and not request.allow_current_background
            records = () if blocked else self._records(request, kind)
            sections.append(ResearchDataSection(kind, "fixture_" + kind, "unavailable" if blocked else "available",
                records, "fixture_unavailable" if blocked else "fixture_available", "Artificial source fixture", NOW))
        batch = ResearchDataBatch(request, tuple(sections))
        if self.transform:
            batch = self.transform(batch)
        if self.on_collect:
            self.on_collect()
        return batch

    @staticmethod
    def _records(request, kind):
        source = "fixture_" + kind
        published = request.as_of.astimezone(SHANGHAI) - timedelta(days=1)
        if kind == "profile":
            return (ResearchEvidence("profile:" + request.instrument_id, request.instrument_id, kind, source,
                "main_business", "人工主营业务说明", "text", "https://example.invalid/fixture", None, None,
                "unknown", NOW, content_kind="background", temporal_scope="current_context"),)
        fields = (("revenue", 123000000.0, "cny"), ("parent_net_profit", 4500000.0, "cny")) if kind == "financial" else (
            ("news_title" if kind == "news" else "announcement_title", request.security_name + "人工标题", "text"),)
        return tuple(ResearchEvidence(kind + ":" + request.instrument_id + ":" + metric,
            request.instrument_id, kind, source, metric, value, unit, "https://example.invalid/fixture",
            published, published.date(), "datetime", NOW, report_period=date(2026, 6, 30) if kind == "financial" else None,
            content_kind="fact" if kind == "financial" else "title") for metric, value, unit in fields)


class MemoryDailySource:
    adjustment = "unadjusted"

    def __init__(self, request, owner):
        self.request, self.owner = request, owner
        self.source_name = request.source
        self.instrument_id, self.start_date, self.end_date = request.instrument_id, request.start_date, request.as_of_date

    def read_daily_bars(self):
        self.owner.reads.append(self.request)
        if self.owner.fail:
            raise DataSourceError("Artificial source failure")
        symbol = self.owner.wrong_instrument or self.request.instrument_id
        return [DailyBar(symbol, self.request.as_of_date - timedelta(days=99 - index),
                         10.0, 11.0, 9.0, 10.0, 100000 + index * 100, 1000000.0) for index in range(100)]


class MemorySourceFactory:
    def __init__(self, *, fail=False, wrong_instrument=None):
        self.requests, self.reads, self.fail, self.wrong_instrument = [], [], fail, wrong_instrument

    def __call__(self, request):
        self.requests.append(request)
        return MemoryDailySource(request, self)


class ProductResearchTests(unittest.TestCase):
    def setUp(self):
        self.data, self.factory = CompanyDataFixture(), MemorySourceFactory()
        for guard in (patch("socket.socket.connect", side_effect=AssertionError("No external connections")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.product_research._build_source", side_effect=AssertionError("Use fixture factory"))):
            guard.start()
            self.addCleanup(guard.stop)

    def collect(self, selected=None, *, data=None, control=None):
        selected = plan() if selected is None else selected
        runtime = company_turn_control() if control is None else control
        runtime.start()
        with bind_turn(runtime):
            bundle = collect_product_research(selected, data_service=self.data if data is None else data,
                now=NOW, source_factory=self.factory)
        return bundle, runtime

    def test_fundamentals_use_current_documents_and_do_not_read_market_bars(self):
        bundle, control = self.collect()
        self.assertEqual([c.instrument_id for c in bundle.companies], ["000001.SZ"])
        request, kinds = self.data.calls[0]
        self.assertEqual((request.as_of, request.allow_current_background), (NOW, True))
        self.assertEqual(kinds, ("profile", "financial"))
        self.assertEqual({item.kind for item in bundle.evidence}, {"profile", "financial"})
        self.assertIn("1.23亿元", [item.display for item in bundle.evidence])
        self.assertEqual(control.to_dict()["analyses_used"], 0)
        self.assertEqual(self.factory.reads, [])
        self.assertFalse(bundle.to_dict()["trading_enabled"])

    def test_three_stock_comparison_uses_three_separate_reads_under_one_budget(self):
        bundle, control = self.collect(plan(securities=SECURITIES[:3], kinds=("profile", "financial", "technical")))
        expected = [item.instrument_id for item in SECURITIES[:3]]
        self.assertEqual([request.instrument_id for request in self.factory.reads], expected)
        self.assertEqual([request.instrument_id for request, _ in self.data.calls], expected)
        self.assertEqual(control.to_dict()["analyses_used"], 3)
        self.assertEqual(control.limits.analyses, 3)
        self.assertTrue(all({item.instrument_id for item in company.evidence} == {company.instrument_id}
                            for company in bundle.companies))
        self.assertEqual({item["instrument_id"] for item in model_research_context(bundle)["evidence"]}, set(expected))

    def test_news_failure_does_not_discard_financials_or_technical_evidence(self):
        bundle, _ = self.collect(plan(kinds=("financial", "news", "technical")),
                                 data=CompanyDataFixture(unavailable=("news",)))
        self.assertTrue(bundle.partial)
        self.assertEqual({item.kind for item in bundle.evidence}, {"financial", "technical"})
        coverage = next(item for item in bundle.companies[0].coverage if item["kind"] == "news")
        self.assertEqual((coverage["status"], coverage["code"], coverage["source"]),
                         ("unavailable", "fixture_unavailable", "fixture_news"))

    def test_explicit_history_uses_shanghai_day_end_and_disallows_current_profile(self):
        bundle, _ = self.collect(plan(historical=True))
        request, _ = self.data.calls[0]
        self.assertEqual(request.as_of, datetime(2026, 9, 25, 23, 59, 59, 999999, tzinfo=SHANGHAI))
        self.assertFalse(request.allow_current_background)
        self.assertEqual({item.kind for item in bundle.evidence}, {"financial"})
        self.assertTrue(bundle.partial)

    def test_other_stock_or_other_request_cannot_enter_the_bundle(self):
        def other_stock(batch):
            section = batch.source_results[0]
            wrong = replace(section.items[0], instrument_id="600000.SH")
            return replace(batch, source_results=(replace(section, items=(wrong,)),))
        def other_request(batch):
            return replace(batch, request=replace(batch.request, instrument_id="600000.SH"))
        for transform in (other_stock, other_request):
            with self.subTest(transform=transform), self.assertRaises(ValueError):
                self.collect(data=CompanyDataFixture(transform=transform))
        self.assertEqual(self.factory.reads, [])

    def test_second_boundary_rejects_future_current_background_and_unrequested_domains(self):
        def future_publication(batch):
            section = batch.source_results[0]
            future = batch.request.as_of + timedelta(seconds=1)
            item = replace(section.items[0], published_at=future, published_date=future.date())
            return replace(batch, source_results=(replace(section, items=(item,)),))

        def current_background(batch):
            section = batch.source_results[0]
            request = replace(batch.request, allow_current_background=True)
            records = CompanyDataFixture._records(request, "profile")
            return replace(batch, source_results=(replace(section, items=records, status="available"),))

        def unrequested_domain(batch):
            return replace(batch, source_results=(replace(batch.source_results[0], kind="unrequested_domain"),))

        cases = ((plan(kinds=("financial",)), future_publication),
                 (plan(kinds=("profile",), historical=True), current_background),
                 (plan(kinds=("financial",)), unrequested_domain))
        for selected, transform in cases:
            with self.subTest(transform=transform), self.assertRaises(ValueError):
                self.collect(selected, data=CompanyDataFixture(transform=transform))
        self.assertEqual(self.factory.reads, [])

    def test_risk_levels_are_calculated_without_model_involvement(self):
        bundle, control = self.collect(plan(kinds=("technical",), wants_risk=True))
        risk = bundle.companies[0].risk
        self.assertEqual((risk.status, risk.source_date), ("available", "2026-09-25"))
        self.assertEqual((risk.values.reference_entry, risk.values.atr), ("10", "2"))
        self.assertEqual((risk.values.reference_stop, risk.values.reference_target), ("7.00", "16.00"))
        scenario = next(item for item in bundle.evidence if item.kind == "risk")
        self.assertEqual((scenario.content_kind, scenario.dependencies), ("scenario", risk.evidence_ids))
        self.assertEqual(control.to_dict()["model_calls_attempted"], 0)
        self.assertEqual(control.to_dict()["analyses_used"], 1)

    def test_wrong_stock_market_bars_cannot_produce_technical_or_risk_evidence(self):
        self.factory.wrong_instrument = "600000.SH"
        bundle, _ = self.collect(plan(kinds=("technical",), wants_risk=True))
        self.assertEqual(bundle.evidence, ())
        self.assertTrue(bundle.partial)
        self.assertIsNone(bundle.companies[0].risk.values)

    def test_cancelled_collection_does_not_advance_to_market_reads(self):
        control = company_turn_control()
        data = CompanyDataFixture(on_collect=control.cancel)
        with self.assertRaises(TurnStopped):
            self.collect(plan(kinds=("financial", "technical")), data=data, control=control)
        self.assertEqual(self.factory.reads, [])

    def test_invalid_or_four_stock_plans_fail_before_any_external_dependency(self):
        for selected in (replace(plan(), status="needs_clarification"), plan(securities=()), plan(securities=SECURITIES)):
            with self.subTest(plan=selected), self.assertRaises(ValueError):
                self.collect(selected)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.factory.requests, [])

    def test_model_context_is_compact_round_robin_and_preserves_full_bundle(self):
        bundle, _ = self.collect(plan(securities=SECURITIES[:3], kinds=("profile", "financial", "technical")))
        evidence_count = len(bundle.evidence)
        context = model_research_context(bundle, limit=5000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 5000)
        self.assertEqual({item["instrument_id"] for item in context["evidence"]},
                         {item.instrument_id for item in SECURITIES[:3]})
        self.assertGreater(context["omitted_from_model"], 0)
        self.assertEqual(len(bundle.evidence), evidence_count)
        self.assertFalse(any("dependencies" in item for item in context["evidence"]))


if __name__ == "__main__":
    unittest.main()
