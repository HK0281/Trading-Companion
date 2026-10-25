"""Deterministic fixed-candidate comparisons; no investment-return or language scoring."""

from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import patch

from trading_companion.comparison_research import METHOD, METRICS, VERSION, build_candidate_comparison
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle
from trading_companion.security_directory import Security
from trading_companion.turn_control import TurnControl, TurnLimits, bind_turn


SECURITIES = (Security("000001.SZ", "平安银行"), Security("600000.SH", "浦发银行"), Security("600036.SH", "招商银行"))


def bundle(values=((5.0, 3.0, 8.0), (6.0, 2.0, 9.0), (4.0, 4.0, 7.0))):
    companies = []
    for security, numbers in zip(SECURITIES, values):
        records = tuple(ProductEvidence(f"synthetic:{security.instrument_id}:{metric}", security.instrument_id,
            "financial", label, str(value) + "%", "synthetic", date="2026-08-20", report_period="2026-06-30",
            raw_value=value, unit="percent", data_authenticity="synthetic")
            for (metric, label), value in zip(METRICS, numbers))
        companies.append(ProductCompany(security.instrument_id, security.name, records,
            ({"kind": "financial", "status": "available"},)))
    plan = ProductPlan(status="ready", securities=SECURITIES[:len(companies)], kinds=("financial",),
        start_date=date(2025, 9, 26), end_date=date(2026, 9, 25), as_of=date(2026, 9, 25), source="tencent")
    return ProductResearchBundle(plan, tuple(companies))


def change_record(selected, company_index=0, metric_index=0, **changes):
    companies = list(selected.companies)
    company = companies[company_index]
    records = list(company.evidence)
    records[metric_index] = replace(records[metric_index], **changes)
    companies[company_index] = replace(company, evidence=tuple(records))
    return replace(selected, companies=tuple(companies))


class CandidateComparisonTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch("socket.socket.connect", side_effect=AssertionError("No network")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
                      patch("trading_companion.product_research._build_source", side_effect=AssertionError("No market source")),
                      patch("trading_companion.deepseek_tool_model.DeepSeekToolModel.complete", side_effect=AssertionError("No model"))):
            guard.start()
            self.addCleanup(guard.stop)

    def test_three_metrics_rank_independently_without_an_overall_winner(self):
        result = build_candidate_comparison(bundle())
        self.assertEqual((result.status, result.code), ("comparable", "candidate_metrics_compared"))
        self.assertEqual([metric.rows[0].instrument_id for metric in result.metrics], ["600000.SH", "600036.SH", "600000.SH"])
        for metric in result.metrics:
            self.assertEqual(metric.status, "comparable")
            self.assertEqual([row.rank for row in metric.rows], [1, 2, 3])
            self.assertEqual(metric.unit, "percent")
            self.assertEqual(metric.report_period, "2026-06-30")
            self.assertTrue(all(row.evidence_id for row in metric.rows))
        payload = result.to_dict()
        self.assertEqual((payload["version"], payload["method"]), (VERSION, METHOD))
        self.assertIsNone(payload["overall_winner"])
        self.assertFalse(payload["trading_enabled"])
        self.assertEqual(payload["coverage"]["comparable_metrics"], 3)

    def test_ties_use_same_rank_and_stable_identity_order_with_competition_gaps(self):
        selected = bundle(((5, 3, 8), (5.0, 3, 8), (4, 3, 8)))
        forward = build_candidate_comparison(selected)
        reverse = build_candidate_comparison(replace(selected, companies=tuple(reversed(selected.companies))))
        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual([row.rank for row in forward.metrics[0].rows], [1, 1, 3])
        self.assertEqual([row.rank for row in forward.metrics[1].rows], [1, 1, 1])
        self.assertEqual([row.instrument_id for row in forward.metrics[0].rows[:2]], ["000001.SZ", "600000.SH"])

    def test_raw_values_control_order_even_when_display_values_are_equal(self):
        selected = bundle(((5.0000001, 3, 8), (5.0000002, 3, 8)))
        selected = change_record(selected, display="5%")
        selected = change_record(selected, company_index=1, display="5%")
        result = build_candidate_comparison(selected)
        self.assertEqual([row.instrument_id for row in result.metrics[0].rows], ["600000.SH", "000001.SZ"])
        self.assertEqual([row.rank for row in result.metrics[0].rows], [1, 2])
        self.assertEqual([row.display for row in result.metrics[0].rows], ["5%", "5%"])
        self.assertEqual([row.value for row in result.metrics[0].rows], [5.0000002, 5.0000001])

    def test_large_finite_integers_are_sorted_without_decimal_context_rounding(self):
        value = 10 ** 40
        selected = bundle(((value, 3, 8), (value + 1, 3, 8)))
        result = build_candidate_comparison(selected)
        self.assertEqual([row.value for row in result.metrics[0].rows], [value + 1, value])
        self.assertEqual([row.rank for row in result.metrics[0].rows], [1, 2])

    def test_negative_and_zero_observations_are_valid_but_missing_is_not_zero(self):
        selected = bundle(((-5, 0, -1), (0, -2, -3)))
        result = build_candidate_comparison(selected)
        self.assertEqual(result.status, "comparable")
        self.assertEqual([row.value for row in result.metrics[0].rows], [0, -5])
        company = replace(selected.companies[0], evidence=selected.companies[0].evidence[1:])
        result = build_candidate_comparison(replace(selected, companies=(company, selected.companies[1])))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.metrics[0].status, "not_comparable")
        self.assertIsNone(result.metrics[0].rows[0].value)
        self.assertTrue(all(row.rank is None for row in result.metrics[0].rows))
        self.assertEqual(result.metrics[0].to_dict()["coverage"]["unavailable_candidates"], ["000001.SZ"])

    def test_nonfinite_boolean_numeric_string_and_missing_raw_values_are_unavailable(self):
        for value in (None, True, False, "5.0", float("nan"), float("inf"), float("-inf"), {}, 10 ** 1000):
            with self.subTest(value=repr(value)[:30]):
                result = build_candidate_comparison(change_record(bundle(), raw_value=value))
                metric = result.metrics[0]
                self.assertEqual(metric.status, "not_comparable")
                self.assertEqual(metric.rows[0].code, "metric_raw_value_invalid")
                self.assertIsNone(metric.rows[0].value)
                self.assertTrue(all(row.rank is None for row in metric.rows))
                json.dumps(result.to_dict(), allow_nan=False)

    def test_period_mismatch_only_blocks_that_metric_without_picking_a_latest_row(self):
        selected = change_record(bundle(), report_period="2026-03-31")
        result = build_candidate_comparison(selected)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.metrics[0].code, "candidate_report_periods_differ")
        self.assertTrue(all(row.value is not None and row.rank is None for row in result.metrics[0].rows))
        self.assertEqual(result.metrics[0].to_dict()["coverage"]["eligible_candidates"], 3)
        self.assertEqual(result.metrics[1].status, "comparable")

    def test_unknown_invalid_or_future_report_periods_do_not_rank(self):
        for period in (None, "", "2026H1", "2026-6-30", "2026-02-31", "2026-12-31", date(2026, 6, 30)):
            with self.subTest(period=period):
                result = build_candidate_comparison(change_record(bundle(), report_period=period))
                self.assertEqual(result.metrics[0].rows[0].code, "metric_report_period_invalid")
                self.assertTrue(all(row.rank is None for row in result.metrics[0].rows))

    def test_units_must_use_the_same_canonical_percentage_convention(self):
        for unit in (None, "", "%", "ratio", "cny", "percentage_points"):
            with self.subTest(unit=unit):
                result = build_candidate_comparison(change_record(bundle(), unit=unit))
                self.assertEqual(result.metrics[0].status, "not_comparable")
                self.assertEqual(result.metrics[0].rows[0].code, "metric_unit_not_percent")

    def test_duplicate_evidence_is_not_resolved_by_picking_the_first(self):
        selected = bundle()
        company = selected.companies[0]
        company = replace(company, evidence=(*company.evidence, replace(company.evidence[0], id="other", raw_value=10.0)))
        result = build_candidate_comparison(replace(selected, companies=(company, *selected.companies[1:])))
        self.assertEqual(result.metrics[0].rows[0].code, "metric_evidence_ambiguous")
        self.assertIsNone(result.metrics[0].rows[0].evidence_id)
        self.assertTrue(all(row.rank is None for row in result.metrics[0].rows))

    def test_wrong_domain_label_kind_or_instrument_never_supplies_a_metric(self):
        for changes, code in (({"kind": "technical"}, "metric_evidence_missing"),
                              ({"label": "revenue_yoy"}, "metric_evidence_missing"),
                              ({"content_kind": "snippet"}, "metric_evidence_not_fact"),
                              ({"instrument_id": "600000.SH"}, "metric_evidence_identity_mismatch"),
                              ({"display": ""}, "metric_display_invalid")):
            with self.subTest(changes=changes):
                result = build_candidate_comparison(change_record(bundle(), **changes))
                self.assertEqual(result.metrics[0].rows[0].code, code)
                self.assertEqual(result.metrics[0].status, "not_comparable")

    def test_same_evidence_identifier_cannot_be_reused_across_candidates(self):
        selected = bundle()
        result = build_candidate_comparison(change_record(selected, company_index=1,
            id=selected.companies[0].evidence[0].id))
        self.assertEqual(result.metrics[0].code, "candidate_evidence_ids_not_unique")
        self.assertTrue(all(row.rank is None for row in result.metrics[0].rows))

    def test_one_candidate_remains_explicitly_not_comparable(self):
        result = build_candidate_comparison(bundle(((5, 3, 8),)))
        self.assertEqual(result.status, "not_comparable")
        for metric in result.metrics:
            self.assertEqual(metric.code, "at_least_two_candidates_required")
            self.assertEqual(len(metric.rows), 1)
            self.assertIsNotNone(metric.rows[0].value)
            self.assertIsNone(metric.rows[0].rank)

    def test_candidate_bounds_and_plan_identity_scope_are_enforced(self):
        selected = bundle()
        cases = (replace(selected, companies=()), replace(selected, companies=selected.companies * 2),
                 replace(selected, plan=replace(selected.plan, securities=SECURITIES[:2])),
                 replace(selected, plan=replace(selected.plan, status="failed")),
                 replace(selected, plan=None),
                 replace(selected, companies=(selected.companies[0], selected.companies[0])))
        for item in cases:
            with self.subTest(count=len(item.companies)):
                result = build_candidate_comparison(item)
                self.assertEqual(result.status, "not_comparable")
                self.assertTrue(all(row.rank is None for metric in result.metrics for row in metric.rows))
                self.assertEqual(result.candidate_count, len(item.companies))
        self.assertEqual(build_candidate_comparison(cases[1]).code, "comparison_candidate_limit")

    def test_invalid_bundle_shape_returns_not_comparable_instead_of_fabricated_rows(self):
        for item in (None, {}, [], False, replace(bundle(), companies=[]), replace(bundle(), companies=(object(),))):
            with self.subTest(type=type(item)):
                result = build_candidate_comparison(item)
                self.assertEqual(result.status, "not_comparable")
                self.assertTrue(all(not metric.rows for metric in result.metrics))

    def test_serialization_is_detached_and_comparison_uses_no_model_or_analysis_budget(self):
        selected = bundle()
        control = TurnControl(TurnLimits(model_calls=0, analyses=0))
        control.start()
        with bind_turn(control):
            result = build_candidate_comparison(selected)
        payload = result.to_dict()
        payload["metrics"][0]["rows"][0]["value"] = 999999
        payload["metrics"][0]["coverage"]["unavailable_candidates"].append("changed")
        self.assertNotEqual(result.to_dict(), payload)
        self.assertEqual(control.to_dict()["model_calls_attempted"], 0)
        self.assertEqual(control.to_dict()["analyses_used"], 0)
        self.assertEqual(selected.companies[0].evidence[0].raw_value, 5.0)


if __name__ == "__main__":
    unittest.main()
