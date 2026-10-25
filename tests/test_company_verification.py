"""Company acceptance must inspect each offline workflow, not trust green totals."""

from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
import unittest
from unittest.mock import patch

from scripts import evaluate_company_research as evaluation
from scripts import verify


class CompanyEvaluationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (verify.clean_environment(), verify.offline_network(),
              patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
              patch("requests.get", side_effect=AssertionError("No real data HTTP")),
              patch("requests.post", side_effect=AssertionError("No live model HTTP"))):
            cls.passing_report = evaluation.evaluate_company_research()

    def report(self):
        return deepcopy(self.passing_report)

    def test_default_fake_workflows_pass_independent_acceptance(self):
        report = self.report()
        verify.check_company_evaluation(report)
        self.assertEqual({item["case"] for item in report["cases"]}, {
            "pe_knowledge", "company_fundamentals", "two_stock_comparison", "announcements_news", "risk_scenario",
        })
        self.assertEqual((report["summary"]["model_calls"], report["summary"]["analyses"]), (9, 3))

    def test_live_mode_network_or_non_synthetic_marker_is_rejected(self):
        for field, value in (
            ("provider", "deepseek"), ("live_provider_selected", True), ("live_provider_selected", 0),
            ("real_market_data_requested", True), ("news_network_requested", True),
            ("directory_network_requested", True), ("fixture_data_authenticity", "real"),
        ):
            report = self.report()
            report[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_missing_network_markers_are_not_treated_as_false(self):
        for field in ("real_market_data_requested", "news_network_requested", "directory_network_requested"):
            report = self.report()
            del report[field]
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_changed_scope_quality_claim_or_raw_display_policy_is_rejected(self):
        for field, value in (
            ("version", "unknown"), ("status", "failed"), ("code", "other"),
            ("evaluation_scope", "live_language_and_investment_quality"),
            ("language_quality", "accepted"), ("answer_display_policy", "raw_model_responses"),
            ("reference_date", "2026-09-27"),
        ):
            report = self.report()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_duplicate_missing_or_replaced_case_cannot_hide_behind_five_passes(self):
        for mode in ("missing", "duplicate", "replaced", "selected_missing", "selected_duplicate", "selected_replaced"):
            report = self.report()
            if mode == "missing":
                report["cases"].pop()
            elif mode == "duplicate":
                report["cases"][0] = deepcopy(report["cases"][1])
            elif mode == "replaced":
                report["cases"][0]["case"] = "invented_case"
            elif mode == "selected_missing":
                report["selected_cases"].pop()
            elif mode == "selected_duplicate":
                report["selected_cases"][0] = report["selected_cases"][1]
            else:
                report["selected_cases"][0] = "invented_case"
            with self.subTest(mode=mode), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_each_case_requires_accepted_model_output_without_application_fallback(self):
        for index in range(5):
            for field, value in (
                ("status", "failed"), ("accepted", False), ("accepted", 1),
                ("fallback", True), ("fallback", 0), ("answer_origin", "application"),
                ("answer", None), ("answer", "  "), ("code", "product_answer_fallback"),
                ("answer_validation_scope", "unvalidated_raw_output"),
            ):
                report = self.report()
                report["cases"][index][field] = value
                with self.subTest(case=index, field=field, value=value), self.assertRaises(verify.VerificationError):
                    verify.check_company_evaluation(report)

    def test_each_required_check_must_be_present_and_strictly_true_in_every_case(self):
        for index in range(5):
            for check in verify.COMPANY_REQUIRED_CHECKS:
                for mode in ("false", "missing", "integer"):
                    report = self.report()
                    checks = report["cases"][index]["checks"]
                    if mode == "missing":
                        del checks[check]
                    else:
                        checks[check] = False if mode == "false" else 1
                    with self.subTest(case=index, check=check, mode=mode), self.assertRaises(verify.VerificationError):
                        verify.check_company_evaluation(report)
        report = self.report()
        report["cases"][1]["checks"]["additional_safety_check"] = False
        with self.assertRaises(verify.VerificationError):
            verify.check_company_evaluation(report)

    def test_case_counts_are_pinned_even_when_the_summary_remains_green(self):
        for index in range(5):
            for field in ("model_calls", "provider_calls_forwarded", "analyses",
                          "synthetic_company_collections", "synthetic_daily_reads"):
                report = self.report()
                report["cases"][index][field] += 1
                with self.subTest(case=index, field=field), self.assertRaises(verify.VerificationError):
                    verify.check_company_evaluation(report)

    def test_counts_cannot_be_redistributed_between_cases_to_preserve_nine_and_three(self):
        for field in ("model_calls", "provider_calls_forwarded", "analyses"):
            report = self.report()
            report["cases"][1][field] += 1
            report["cases"][2][field] -= 1
            self.assertEqual(sum(item[field] for item in report["cases"]), report["summary"][field])
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_summary_counts_and_required_budget_are_independently_pinned(self):
        for field in ("selected", "executed", "passed", "failed", "model_calls", "provider_calls_forwarded", "analyses"):
            report = self.report()
            report["summary"][field] += 1
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)
        for field, value in (("required_model_call_budget", 8), ("model_call_budget", 8),
                             ("model_call_budget", 11), ("model_call_budget", 10.0)):
            report = self.report()
            report[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_numeric_booleans_and_floats_are_not_valid_observed_counts(self):
        for scope, field, value in (("case", "model_calls", True), ("case", "analyses", False),
                                    ("case", "provider_calls_forwarded", 1.0),
                                    ("summary", "failed", False), ("summary", "analyses", 3.0)):
            report = self.report()
            target = report["cases"][0] if scope == "case" else report["summary"]
            target[field] = value
            with self.subTest(scope=scope, field=field), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    def test_malformed_or_missing_details_fail_as_a_gate_error(self):
        reports = [None, [], {}, self.report(), self.report(), self.report(), self.report(), self.report()]
        reports[3]["summary"] = None
        reports[4]["cases"][0] = None
        reports[5]["cases"][0]["checks"] = None
        reports[6]["selected_cases"][0] = {}
        del reports[7]["cases"][0]["analyses"]
        for report in reports:
            with self.subTest(report_type=type(report)), self.assertRaises(verify.VerificationError):
                verify.check_company_evaluation(report)

    @contextmanager
    def isolated_python_checks(self, report):
        with ExitStack() as stack:
            stack.enter_context(patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()))
            stack.enter_context(patch.object(verify, "run_strict_suite", return_value=3))
            stack.enter_context(patch("trading_companion.agent_evaluation.run_evaluation", return_value={}))
            stack.enter_context(patch("trading_companion.mvp_evaluation.run_mvp_evaluation", return_value={}))
            stack.enter_context(patch.object(verify, "check_evaluation"))
            stack.enter_context(patch.object(verify, "check_mvp_evaluation"))
            company = stack.enter_context(patch.object(evaluation, "evaluate_company_research", return_value=report))
            walkthrough = stack.enter_context(patch("trading_companion.api_demo.main", return_value=0))
            stack.enter_context(redirect_stdout(StringIO()))
            yield company, walkthrough

    def test_python_checks_calls_default_fake_evaluator_and_runs_company_gate(self):
        with self.isolated_python_checks(self.report()) as (company, walkthrough):
            verify.python_checks()
        company.assert_called_once_with()
        walkthrough.assert_called_once_with()

    def test_company_gate_cannot_be_skipped_when_older_gates_pass(self):
        report = self.report()
        report["cases"][1]["fallback"] = True
        with self.isolated_python_checks(report) as (company, walkthrough):
            with self.assertRaises(verify.VerificationError):
                verify.python_checks()
        company.assert_called_once_with()
        walkthrough.assert_not_called()


if __name__ == "__main__":
    unittest.main()
