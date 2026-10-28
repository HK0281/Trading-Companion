"""Exported reports preserve unknowns, human blanks, failures and unpaired comparison rows."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import compare_research_workflows as compare_command
from scripts import evaluate_research_workflows as command
from trading_companion.workflow_evaluation import evaluate_workflows
from trading_companion.workflow_evaluation_metrics import evaluation_metrics
from trading_companion.workflow_evaluation_reporting import (
    manual_review, verified_facts, format_report, write_report, load_report, compare_reports, format_comparison,
)


class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluate_workflows(selected_cases=["K01", "K02", "C01", "R01"], repeat=3)
        assert cls.report["status"] == "passed", cls.report["code"]

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_sampling_is_bounded_stable_independent_of_step_order_and_leaves_human_blanks(self):
        a = manual_review(self.report)
        reversed_report = {**self.report, "steps": list(reversed(self.report["steps"]))}
        self.assertEqual(a, manual_review(reversed_report))
        self.assertEqual(a["sampled"], 10)
        self.assertTrue(all(all(value is None for value in item["human"].values()) for item in a["items"]))
        self.assertTrue(all("question" in item and "run_id" in item and "evidence_index" in item for item in a["items"]))
        empty = manual_review({"steps": []})
        self.assertEqual((empty["sampled"], empty["empty_reason"]), (0, "no_generated_answers"))

    def test_artifacts_match_stdout_schema_and_never_overwrite_previous_runs(self):
        report = deepcopy(self.report)
        first = write_report(report, self.temp.name)
        old = Path(first["json"]).read_bytes()
        second = write_report(report, self.temp.name)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first["json"]).read_bytes(), old)
        loaded = load_report(second["json"])
        self.assertEqual(loaded["summary"], report["summary"])
        self.assertEqual(json.loads(Path(second["manual_review"]).read_text(encoding="utf-8")), report["manual_review"])
        markdown = Path(second["markdown"]).read_text(encoding="utf-8")
        self.assertLess(markdown.index("| Verified facts |"), markdown.index("## 分母与覆盖"))
        for text in ("Offline contract evaluation", "人工抽查", "未知", "未运行"):
            self.assertIn(text, markdown)
        for private in ("private-evaluation-draft", "access_token", '"messages":', '"tool_results":'):
            self.assertNotIn(private, json.dumps(loaded, ensure_ascii=False))

    def test_readme_facts_row_is_mode_specific_and_markdown_escapes_untrusted_text(self):
        report = deepcopy(self.report)
        report["mode"] = "live-model"
        row = verified_facts(report)
        self.assertIn("Live-model evaluation (corpus research-api-v1", row)
        self.assertIn("scenarios /", row)
        report["manual_review"]["items"][0]["answer"] = "<script>alert(1)</script> [link](https://example.invalid) | extra"
        markdown = format_report(report)
        self.assertNotIn("<script>", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        self.assertIn("\\[link\\]", markdown)
        self.assertIn("\\| extra", markdown)

    def test_comparison_includes_failures_and_latency_uses_all_recorded_steps(self):
        left, right = deepcopy(self.report), deepcopy(self.report)
        right["plan"]["collection_workers"] = right["collection_workers"] = 1
        for step in left["steps"]:
            step["trace"]["total_elapsed_ms"] = 100
        for step in right["steps"]:
            step["trace"]["total_elapsed_ms"] = 50
        right["steps"][0]["status"] = "failed"
        for report in (left, right):
            report["metrics"] = evaluation_metrics(report)
        result = compare_reports(left, right)
        self.assertEqual(result["status"], "comparable")
        self.assertEqual(len(result["rows"]), len(left["steps"]))
        self.assertTrue(any(row["case_id"] == "K01" and row["repeat"] == 1 and row["right"]["status"] == "failed"
                            for row in result["rows"]))
        self.assertEqual(result["latency"]["p95"]["change_percent"], -50)

    def test_missing_side_and_missing_trace_do_not_disappear_or_produce_speed_claim(self):
        left, right = deepcopy(self.report), deepcopy(self.report)
        removed = right["steps"].pop()
        result = compare_reports(left, right)
        self.assertEqual(result["status"], "incompatible")
        self.assertIn("step_set", result["incompatible_fields"])
        self.assertTrue(any(row["right"] is None for row in result["rows"]))
        self.assertIn("未知", format_comparison(result))
        right["steps"].append(removed)
        right["steps"][0]["trace"] = None
        result = compare_reports(left, right)
        self.assertEqual(result["status"], "comparable")
        self.assertFalse(result["controlled_latency_comparison"])
        self.assertIsNone(result["latency"]["p95"]["change_percent"])

    def test_incompatible_configuration_and_live_source_time_are_not_causal_improvements(self):
        right = deepcopy(self.report)
        right["plan"]["model"] = "different-model"
        result = compare_reports(self.report, right)
        self.assertIn("model", result["incompatible_fields"])
        self.assertIsNone(result["latency"]["p50"]["change_percent"])
        left, right = deepcopy(self.report), deepcopy(self.report)
        for item in (left, right):
            item["mode"] = item["plan"]["mode"] = "live"
        result = compare_reports(left, right)
        self.assertEqual(result["status"], "comparable")
        self.assertFalse(result["controlled_latency_comparison"])

    def test_invalid_or_duplicate_steps_are_rejected(self):
        right = deepcopy(self.report)
        right["steps"].append(right["steps"][0])
        with self.assertRaises(ValueError):
            compare_reports(self.report, right)
        right = deepcopy(self.report)
        right["steps"][0]["trace"]["total_elapsed_ms"] = -1
        with self.assertRaises(ValueError):
            compare_reports(self.report, right)
        path = Path(self.temp.name) / "bad.json"
        path.write_text('{"version":"research-workflow-evaluation-v2","version":"other"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_report(path)

    def test_cli_exports_and_comparison_reads_without_any_model_execution(self):
        output = StringIO()
        with patch.object(command, "evaluate_workflows", return_value=deepcopy(self.report)), redirect_stdout(output):
            self.assertEqual(command.main(["--report-dir", self.temp.name]), 0)
        first = json.loads(output.getvalue())["artifacts"]["json"]
        before = Path(first).read_bytes()
        output = StringIO()
        with patch("trading_companion.workflow_evaluation.evaluate_workflows") as execute, redirect_stdout(output):
            self.assertEqual(compare_command.main(["--left", first, "--right", first, "--report-dir", self.temp.name]), 0)
            execute.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "comparable")
        self.assertTrue(Path(result["artifacts"]["markdown"]).is_file())
        self.assertEqual(Path(first).read_bytes(), before)
