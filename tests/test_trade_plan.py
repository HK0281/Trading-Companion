"""Hand-calculated plan expectations and snapshot tampering, with artificial bars."""

from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

from trading_companion.risk_research import RiskScenario, RiskValues, build_risk_scenario
from trading_companion.decision_demo import load_decision_demo_source
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.research_objective import ResearchObjective

from trading_companion.product_research import collect_product_research
from trading_companion.trade_plan import build_trade_plans, plan_display
from tests.test_product_research import plan, CompanyDataFixture, MemorySourceFactory


def market_result():
    bundle = collect_product_research(plan(kinds=("technical",), wants_risk=True),
        data_service=CompanyDataFixture(), now=None, source_factory=MemorySourceFactory())
    return bundle.companies[0].technical


class TradePlanTests(unittest.TestCase):
    def test_hand_calculated_levels_and_distinct_purposes(self):
        result = market_result()  # close=10, true range=2, ATR=2 => stop=7, R=3
        entry, exit_plan, wait = build_trade_plans(result, expected_request=result.request)
        self.assertEqual(dict(entry.values), dict(reference_entry="10.00", reference_stop="7.00", target_1="14.50",
            target_2="17.50", reward_risk_1="1.500000", reward_risk_2="2.500000", atr="2.0000", atr_multiple="1.5"))
        self.assertEqual(dict(exit_plan.values), {"reference_close": "10.00", "exit_below": "7.00"})
        self.assertEqual(dict(wait.values), {"observe_below": "7.00", "observe_above": "14.50"})
        self.assertEqual(entry.condition["operator"], "<=")
        self.assertFalse(entry.to_dict()["execution_allowed"])

    def test_long_calculated_atr_serializes_within_the_frontend_decimal_contract(self):
        selected = replace(plan(kinds=("technical",)), source="csv",
            indicators=(IndicatorSpec("atr", window=14),), objective=ResearchObjective("decision", "未来20个交易日"))
        company = collect_product_research(selected, data_service=CompanyDataFixture(), now=None,
            development_source=load_decision_demo_source()).companies[0]
        raw_atr = build_risk_scenario(company.technical, expected_request=company.technical.request).values.atr
        self.assertEqual(raw_atr, "0.810216762360921")
        candidate = company.trade_plans[0]
        values = candidate.to_dict()["values"]
        self.assertEqual((values["atr"], values["reference_entry"], values["reference_stop"], values["target_1"], values["target_2"]),
                         ("0.8102", "10.40", "9.19", "12.21", "13.42"))
        self.assertEqual((values["reward_risk_1"], values["reward_risk_2"]), ("1.495867", "2.495867"))
        for value in values.values():
            self.assertRegex(value, r"^[0-9]{1,18}(?:\.[0-9]{1,12})?$")
        self.assertIn("ATR0.8102元", plan_display(candidate))
        self.assertNotIn(raw_atr, plan_display(candidate))

    def test_atr_rounding_changes_display_only_at_a_price_grid_boundary(self):
        result = market_result()
        values = RiskValues("10.4", ".80665", "1.5", "2", "9.20", "12.81", "1.2", "2.41", "11.5385", "2.008333")
        with patch("trading_companion.trade_plan.build_risk_scenario", return_value=RiskScenario("available", "ok", values=values)):
            actual = build_trade_plans(result, expected_request=result.request)[0].to_dict()["values"]
        # Recalculating S from displayed .8067 would give 9.19 and wrong targets.
        self.assertEqual((actual["atr"], actual["reference_entry"], actual["reference_stop"], actual["target_1"], actual["target_2"]),
                         ("0.8067", "10.40", "9.20", "12.20", "13.40"))

    def test_rejects_wrong_request_identity_and_date(self):
        result = market_result()
        for request in (replace(result.request, instrument_id="600000.SH"),
                        replace(result.request, as_of_date=result.request.as_of_date-timedelta(days=1))):
            with self.subTest(request=request):
                self.assertTrue(all(p.status == "unavailable" for p in build_trade_plans(result, expected_request=request)))

    def test_no_atr_does_not_create_a_fixed_percentage_plan(self):
        result = market_result()
        without = replace(result.request, indicators=tuple(i for i in result.request.indicators if i.kind != "atr"))
        self.assertTrue(all(p.values == () for p in build_trade_plans(result, expected_request=without)))

    def test_unavailable_or_explicitly_excluded_has_no_values(self):
        for code in (None, "plan_excluded"):
            plans = build_trade_plans("000001.SZ", expected_request=None, unavailable_code=code)
            self.assertEqual(len(plans), 3)
            self.assertTrue(all(p.status == "unavailable" and p.to_dict()["values"] is None for p in plans))

    def test_plan_identity_is_request_specific(self):
        result = market_result()
        first = build_trade_plans(result, expected_request=result.request)[0]
        self.assertIn(result.request.request_id, first.plan_id)
        self.assertNotEqual(first.plan_id, build_trade_plans(result, expected_request=replace(result.request, request_id="other"))[0].plan_id)

    def test_targets_use_rounded_actual_risk_and_report_effective_ratios(self):
        result = market_result()
        values = RiskValues("10", ".02", "1.5", "2", "9.97", "10.06", ".03", ".06", ".3", "2")
        scenario = RiskScenario("available", "historical_long_scenario_ready", values=values)
        with patch("trading_companion.trade_plan.build_risk_scenario", return_value=scenario):
            p = dict(build_trade_plans(result, expected_request=result.request)[0].values)
        self.assertEqual((p["target_1"], p["target_2"]), ("10.04", "10.07"))
        self.assertEqual((p["reward_risk_1"], p["reward_risk_2"]), ("1.333333", "2.333333"))

    def test_rounding_cannot_create_equal_targets(self):
        result = market_result()
        values = RiskValues("10", ".001", "1.5", "2", "9.999", "10.002", ".001", ".002", ".01", "2")
        with patch("trading_companion.trade_plan.build_risk_scenario", return_value=RiskScenario("available", "ok", values=values)):
            self.assertTrue(all(p.status == "unavailable" for p in build_trade_plans(result, expected_request=result.request)))
