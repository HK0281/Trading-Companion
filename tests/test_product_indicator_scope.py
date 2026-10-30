"""Company and technical research share the user's explicit indicator conditions."""

from datetime import timedelta
import unittest
from unittest.mock import patch

from trading_companion.product_agent import run_product_research_turn
from trading_companion.research_session import ResearchSession
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


class ProductIndicatorScopeTests(unittest.TestCase):
    def setUp(self):
        self.session, self.data, self.factory = ResearchSession(), CompanyDataFixture(), MemorySourceFactory()
        for name in ("requests.get", "requests.post"):
            guard = patch(name, side_effect=AssertionError("No external network"))
            guard.start()
            self.addCleanup(guard.stop)

    def run_turn(self, question, model=None):
        return run_product_research_turn(question, model=model or ProductModelFixture(), session=self.session,
            directory=directory(), now=NOW, source="tencent", data_service=self.data, source_factory=self.factory)

    def test_financial_question_respects_explicit_rsi_period(self):
        result = self.run_turn("分析平安银行的基本面，并结合RSI7判断走势")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ["rsi(window=7)"])
        self.assertEqual(result.product_bundle.companies[0].technical.request.indicators, result.product_plan.indicators)
        self.assertIn("financial", {item["kind"] for item in result.product_bundle.companies[0].coverage})
        self.assertNotIn("RSI(14)", " ".join(item.label for item in result.product_bundle.evidence))

    def test_daily_indicator_period_is_not_misread_as_a_historical_window(self):
        result = self.run_turn("分析平安银行的基本面与20日均线")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ["sma(window=20)"])
        self.assertEqual(result.product_plan.end_date, NOW.date() - timedelta(days=1))

    def test_historical_window_and_indicator_period_remain_distinct(self):
        result = self.run_turn("分析平安银行2026-09-01至2026-09-20的基本面与7日RSI",
                              ProductModelFixture(date_expression="2026-09-01至2026-09-20"))
        self.assertIn(result.status, ("completed", "limited"))
        self.assertTrue(result.product_plan.explicit_historical)
        self.assertEqual(str(result.product_plan.end_date), "2026-09-20")
        self.assertEqual(result.product_plan.indicators[0].parameters, {"window": 7})

    def test_stock_clarification_preserves_requested_indicators(self):
        pending = self.run_turn("分析平安的基本面和RSI7", ProductModelFixture(("平安",)))
        self.assertEqual(pending.status, "needs_clarification")
        self.assertEqual(self.factory.reads, [])
        result = self.run_turn("平安银行", ProductModelFixture(("平安",)))
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(result.product_plan.indicators[0].parameters, {"window": 7})

    def test_domain_followup_keeps_previous_explicit_indicator_choice(self):
        original = self.run_turn("分析平安银行基本面和RSI7")
        result = self.run_turn("那技术面呢")
        self.assertEqual(result.product_plan.indicators, original.product_plan.indicators)
        self.assertEqual(result.product_bundle.companies[0].technical.request.indicators, original.product_plan.indicators)

    def test_invalid_and_unsupported_indicators_never_silently_revert_to_defaults(self):
        for phrase in ("KDJ", "RSI999", "MACD(26,12,9)"):
            with self.subTest(phrase=phrase):
                result = self.run_turn("分析平安银行的基本面和" + phrase)
                self.assertEqual(result.status, "needs_clarification")
                self.assertEqual(self.data.calls, [])
                self.assertEqual(self.factory.reads, [])

    def test_explicitly_excluded_indicator_does_not_add_a_technical_query(self):
        result = self.run_turn("只看平安银行的基本面，不要RSI7")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(result.product_plan.kinds, ("profile", "financial"))
        self.assertEqual(self.factory.reads, [])
        self.assertFalse(any(spec.kind == "rsi" for spec in result.product_plan.indicators))

    def test_risk_scenario_includes_required_atr_without_replacing_requested_rsi(self):
        result = self.run_turn("分析平安银行基本面和RSI7，并计算止盈止损，中线")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ["rsi(window=7)", "atr(window=14)"])
        self.assertIn("indicator_dependency:atr14_for_risk", result.product_plan.applied_defaults)
        self.assertEqual(result.product_bundle.companies[0].risk.status, "available")

    def test_risk_followup_adds_dependency_to_previous_custom_indicator_scope(self):
        self.run_turn("分析平安银行基本面和RSI7")
        result = self.run_turn("那止盈止损呢，中线")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ["rsi(window=7)", "atr(window=14)"])
        self.assertEqual(result.product_bundle.companies[0].risk.status, "available")


if __name__ == "__main__":
    unittest.main()
