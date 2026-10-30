"""Hand-checked denominators, legal alternatives, repair history and incomplete trace details."""

from copy import deepcopy
import unittest

from trading_companion.workflow_evaluation import evaluate_workflows
from trading_companion.workflow_evaluation_metrics import summarize_steps, evaluation_metrics


class MetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluate_workflows(selected_cases=["K02", "S02", "M03", "P01", "P02", "D02", "D03", "R01"])
        assert cls.report["status"] == "passed", cls.report["code"]

    def steps(self, *ids):
        return deepcopy([step for step in self.report["steps"] if step["case_id"] in ids])

    def test_failure_and_unrun_stay_in_route_and_scenario_denominators(self):
        steps = self.steps("K02", "S02", "R01")
        # Four steps, three scenarios; the two R01 steps share one scenario.
        steps[1]["status"] = "failed"
        steps[1]["observed"]["route"] = "unrouted"
        steps[2].update(status="not_run", observed=None, trace=None, outside_acceptable=None)
        output = summarize_steps(steps)
        self.assertEqual(output["planned_steps"], 4)
        self.assertEqual(output["execution_coverage"]["value"], 3/4)
        self.assertEqual(output["step_success"]["value"], 2/4)
        self.assertEqual(output["scenario_success"]["value"], 1/3)
        self.assertEqual(output["route_accuracy"]["value"], 2/4)
        self.assertEqual(output["route_accuracy"]["unknown"], 1)
        self.assertEqual(output["route_accuracy_executed"]["value"], 2/3)
        self.assertEqual(output["result_categories"]["not_run"], 1)
        self.assertEqual(output["joint_evidence_checks"]["applicable"], 4)
        self.assertEqual(output["joint_evidence_checks"]["not_executed"], 1)

    def test_quality_counts_steps_and_does_not_become_safety(self):
        steps = self.steps("S02")
        steps[0].update(status="failed", quality_requirement_failures=["answer_origin", "answer_status"])
        output = summarize_steps(steps)
        self.assertEqual(output["quality_requirement_failures"], 1)
        self.assertEqual(output["hard_requirement_violations"], 0)
        self.assertEqual(output["outside_acceptable"], 0)

    def test_legal_alternative_does_not_change_static_confusion_row(self):
        steps = self.steps("M03")
        steps[0]["observed"].update(category="rejected", status="out_of_scope", code="product_request_out_of_scope")
        output = summarize_steps(steps)
        self.assertEqual(output["result_categories"]["expected_by_actual"]["clarification"]["rejected"], 1)
        self.assertEqual(output["result_categories"]["accepted_alternative_categories"], 1)
        self.assertEqual(output["step_success"]["value"], 1)

    def test_rejected_first_draft_and_final_fallback_are_visible_separately(self):
        output = summarize_steps(self.steps("R01"))
        checks = output["joint_evidence_checks"]
        self.assertEqual((checks["applicable"], checks["passed"], checks["failed"]), (2, 1, 1))
        self.assertEqual((checks["attempts"], checks["rejected_attempts"]), (4, 3))
        self.assertEqual(output["repairs"]["model_rewrite"]["affected_steps"], 2)
        self.assertEqual(output["fallback"]["applicable"]["value"], 1/2)
        self.assertEqual(output["quality_requirement_failures"], 0)

    def test_skipped_rule_is_not_counted_as_an_entered_correction(self):
        steps = self.steps("D02")
        rule = {'kind': 'repair', 'operation': 'intent_rule', 'entered': False, 'status': 'skipped', 'code': 'rule_skipped'}
        steps[0]['trace']['event_records'].append(rule)
        self.assertEqual(summarize_steps(steps)["repairs"]["rule_correction"]["affected_steps"], 0)
        rule.update(entered=True, code="rule_applied", status="ok")
        output = summarize_steps(steps)["repairs"]["rule_correction"]
        self.assertEqual(output["entered_attempts"], 1)
        self.assertEqual(output["applicable"]["value"], 1)

    def test_deadline_partial_all_missing_and_ordinary_error_are_distinct(self):
        output = summarize_steps(self.steps("P01", "P02", "D03"))["source_deadline"]
        self.assertEqual((output["planned_company_steps"], output["entered_collection"]), (3, 3))
        self.assertEqual((output["partial_deadline"], output["all_missing"], output["ordinary_source_failure"]), (1, 1, 1))
        self.assertEqual(output["partial_per_planned_company"]["value"], 1/3)
        self.assertEqual(output["natural_partial_deadline"], 0)
        self.assertIsNone(output["natural_partial_per_planned_company"]["value"])
        for missing in ("code", "evidence", "display"):
            steps = self.steps("P01")
            if missing == "code":
                for company in steps[0]["observed"]["coverage"]:
                    for section in company["sections"]:
                        section["code"] = "other"
            elif missing == "evidence":
                steps[0]["observed"]["company_research"]["evidence"] = []
            else:
                steps[0]["observed"]["answer"] = ""
            with self.subTest(missing=missing):
                self.assertEqual(summarize_steps(steps)["source_deadline"]["partial_deadline"], 0)

    def test_truncated_details_are_unknown_even_with_aggregate_stopped_count(self):
        steps = self.steps("P01")
        steps[0]["trace"]["truncated"] = True
        output = summarize_steps(steps)
        self.assertEqual(output["source_deadline"]["unknown"], 1)
        self.assertEqual(output["source_deadline"]["partial_deadline"], 0)
        self.assertEqual(output["joint_evidence_checks"]["unknown"], 1)
        self.assertEqual(output["trace"]["event_summaries"]["sources"]["stopped"], 1)

    def test_nearest_rank_latency_includes_failed_steps_and_keeps_empty_unknown(self):
        steps = self.steps("K02", "S02", "P01")
        for step, elapsed in zip(steps, (10, 20, 100)):
            step["trace"]["total_elapsed_ms"] = elapsed
        steps[-1]["status"] = "failed"
        output = summarize_steps(steps)
        self.assertEqual(output["trace"]["overall"]["total_elapsed_ms"], {"n": 3, "p50_ms": 20, "p95_ms": 100})
        empty = summarize_steps([])
        self.assertIsNone(empty["route_accuracy"]["value"])
        self.assertIsNone(empty["trace"]["overall"]["total_elapsed_ms"]["p95_ms"])

    def test_tag_groups_overlap_but_each_uses_the_same_planned_denominator(self):
        report = deepcopy(self.report)
        report["steps"] = self.steps("P01")
        metrics = evaluation_metrics(report)
        self.assertEqual(metrics["overall"]["planned_steps"], 1)
        self.assertEqual(metrics["by_tag"]["fault_injected"]["planned_steps"], 1)
        self.assertEqual(metrics["by_tag"]["comparison"]["planned_steps"], 1)
        self.assertGreater(sum(g["planned_steps"] for g in metrics["by_tag"].values()), 1)
