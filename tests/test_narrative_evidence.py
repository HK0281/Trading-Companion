"""Window/quarter facts are derived from frozen eligible data, without extra collection."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import unittest

from trading_companion.financial_periods import quarterly_evidence
from trading_companion.research_price_context import price_context_evidence
from trading_companion.product_research import ProductEvidence
from tests.test_product_decision_answer import decision_bundle


class NarrativeEvidenceTests(unittest.TestCase):
    def test_windows_need_full_samples_and_ties_use_latest_date(self):
        bundle = decision_bundle()
        result = bundle.companies[0].technical
        evidence = price_context_evidence(result)
        by_label = {e.label: e for e in evidence}
        self.assertEqual(by_label["近20日最高价"].date, result.bars[-1].trade_date.isoformat())
        self.assertEqual(by_label["近20日最低价"].date, result.bars[-1].trade_date.isoformat())
        self.assertEqual(Decimal(by_label["MA20"].raw_value), Decimal("10"))
        self.assertEqual(Decimal(by_label["近60日涨跌幅"].raw_value), 0)
        self.assertEqual(len(by_label["MA60"].dependencies), 60)
        # Validate the dependency identity against the already frozen bar dates.
        bars = {b.trade_date.isoformat(): b for b in result.bars}
        for e in evidence:
            self.assertEqual(dict(e.calculation_basis)["price_adjustment"], result.price_adjustment)
            for dependency in e.dependencies:
                request, scope, day, field = dependency.split("|")
                self.assertEqual((request, scope), (result.request.request_id, "raw_daily_bar"))
                self.assertIsNotNone(getattr(bars[day], field))

    def test_full_window_boundaries_are_not_shortened(self):
        from tests.test_product_research import MemorySourceFactory
        class ShortSource(MemorySourceFactory):
            def __call__(self, request):
                source = super().__call__(request)
                read = source.read_daily_bars
                source.read_daily_bars = lambda: read()[-self.count:]
                return source
        factory = ShortSource()
        for count in (5, 6, 19, 20, 60, 61):
            factory.count = count
            result = decision_bundle(source=factory).companies[0].technical
            labels = {e.label for e in price_context_evidence(result)}
            self.assertEqual("近5日涨跌幅" in labels, count >= 6)
            self.assertEqual("近60日涨跌幅" in labels, count >= 61)
            self.assertEqual("MA20" in labels, count >= 20)
            self.assertEqual("MA60" in labels, count >= 60)

    def test_financial_quarter_difference_dependencies_and_yoy(self):
        def record(period, amount, **extra):
            return ProductEvidence(period, "000001.SZ", "financial", "营业收入", "累计", "fixture",
                date="2026-08-20", report_period=period, raw_value=amount, unit="cny", **extra)
        records = [record("2026-06-30", 900), record("2026-03-31", 400),
                   record("2025-06-30", 700), record("2025-03-31", 300)]
        quarter, yoy = quarterly_evidence(records)
        self.assertEqual(Decimal(quarter.raw_value), 500)
        self.assertEqual(quarter.dependencies, ("2026-06-30", "2026-03-31"))
        self.assertEqual(Decimal(yoy.raw_value), 25)
        self.assertEqual(len(yoy.dependencies), 4)
        self.assertEqual(quarter.temporal_scope, "financial_quarter_derived")
        self.assertEqual(len(quarterly_evidence(records[:2])), 1)
        self.assertEqual(quarterly_evidence(records[:1]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], source="other")]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], instrument_id="600000.SH")]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], unit="USD")]), ())
        self.assertEqual(quarterly_evidence([records[0], records[1], records[1]]), ())
        self.assertEqual(Decimal(quarterly_evidence([records[1]])[0].raw_value), 400)
