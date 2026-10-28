"""Real message routing/collection with artificial inputs; no model-quality claims."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from scripts import evaluate_research_workflows as command
from scripts.verify import offline_network
from trading_companion import workflow_evaluation as evaluation
from trading_companion.api_service import ResearchApiService
from trading_companion.company_research_contracts import ResearchDataRequest
from trading_companion.request_routing import ROUTE_SPECS, IntentKind
from trading_companion.trace_cli import TraceReader, TraceSummary
from trading_companion.tool_model import ToolModelResponse
from trading_companion.turn_trace import JsonlTraceSink, TraceLimits
from trading_companion.workflow_evaluation_fixtures import FixedSources, ScriptedWorkflowModel, NOW


class WorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.outputs = {width: evaluation.evaluate_workflows(collection_workers=width, trace_dir=cls.temporary.name)
                       for width in (1, 3)}

    def test_all_40_cases_and_59_real_messages_run_at_both_widths(self):
        for width, report in self.outputs.items():
            with self.subTest(width=width):
                self.assertEqual(report["status"], "passed", [(s["case_id"], s.get("framework_errors"))
                                                              for s in report["steps"] if s["status"] != "passed"])
                self.assertEqual(len(report["cases"]), 40)
                self.assertEqual(report["summary"], {"planned": 59, "executed": 59, "passed": 59, "failed": 0,
                    "not_run": 0, "hard_requirement_violations": 0, "outside_acceptable": 0,
                    "quality_requirement_failures": 0, "trace_missing": 0})
                self.assertEqual(report["model_call_limit"], 354)
                self.assertEqual(report["accounting"]["forwarded"], 69)
                self.assertEqual(len({s["trace"]["run_id"] for s in report["steps"]}), 59)
                self.assertTrue(all(c["transport"]["maximum_in_flight"] <= width and c["transport"]["responses_closed"]
                                    and c["transport"]["requests"] == c["transport"]["closed_clients"] for c in report["cases"]))

    def test_serial_and_parallel_preserve_inputs_identity_codes_and_coverage(self):
        def project(report):
            return [(step["case_id"], step["step"], step["observed"]["route"], step["observed"]["intent"],
                     step["observed"]["code"], step["observed"]["securities"], step["observed"]["coverage"],
                     step["observed"]["answer"], step["status"]) for step in report["steps"]]
        self.assertEqual(project(self.outputs[1]), project(self.outputs[3]))
        self.assertEqual(self.outputs[1]["corpus"], self.outputs[3]["corpus"])
        self.assertEqual(self.outputs[1]["fixtures"], self.outputs[3]["fixtures"])

    def test_aggregate_is_exactly_existing_trace_cli_statistics_including_failures(self):
        report = self.outputs[3]
        summary, reader = TraceSummary(), TraceReader()
        for case in report["cases"]:
            local = TraceReader()
            rows = list(local.read(local.directory(case["trace_directory"])))
            for row in rows:
                summary.add(row)
            reader.valid_turns += local.valid_turns
            reader.bytes_read += local.bytes_read
            reader.lines_read += local.lines_read
        expected = summary.report(reader)
        self.assertEqual(report["trace_summary"], expected)
        self.assertGreater(expected["categories"]["error"], 0)
        self.assertEqual(expected["overall"]["total_elapsed_ms"]["n"], 59)
        self.assertEqual(expected["models"]["provider_requests_started"], 0)
        self.assertEqual(expected["models"]["attempted"], 69)
        self.assertIsNone(report["accounting"]["token_usage"]["reported_total_tokens"])

    def test_deadline_partial_full_missing_and_complete_are_distinct(self):
        for width, report in self.outputs.items():
            steps = {s["case_id"]: s for s in report["steps"] if s["case_id"].startswith("P")}
            partial = steps["P01"]
            self.assertEqual(partial["trace"]["summaries"]["sources"]["stopped"], 1)
            self.assertEqual(partial["trace"]["summaries"]["sources"]["failed"], 0)
            sections = [section for company in partial["observed"]["coverage"] for section in company["sections"]]
            self.assertEqual(sum(s["status"] == "available" for s in sections), 5)
            self.assertIn("未取得", partial["observed"]["answer"])
            self.assertEqual(steps["P02"]["observed"]["status"], "failed")
            self.assertTrue(all(s["status"] == "unavailable" for c in steps["P02"]["observed"]["coverage"] for s in c["sections"]))
            self.assertEqual(steps["P03"]["observed"]["status"], "completed")
            self.assertNotIn("research_data_deadline_exceeded", steps["P03"]["observed"]["codes"])

    def test_repair_and_fallback_are_visible_without_exporting_bad_model_draft(self):
        steps = [s for s in self.outputs[3]["steps"] if s["case_id"] == "R01"]
        self.assertEqual(steps[0]["trace"]["summaries"]["repairs"]["entered"], 1)
        self.assertEqual(steps[0]["observed"]["answer_status"], "accepted")
        self.assertEqual((steps[1]["observed"]["answer_status"], steps[1]["observed"]["answer_origin"]), ("fallback", "application"))
        self.assertEqual(steps[1]["quality_requirement_failures"], [])
        self.assertEqual(steps[1]["quality_checks"], {"answer_origin": True, "answer_status": True})
        text = json.dumps(self.outputs[3], ensure_ascii=False)
        for marker in ("private-evaluation-draft-never-export", "access_token", '"messages":', '"tool_results":'):
            self.assertNotIn(marker, text)


class WorkflowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_invalid_parameters_fail_before_model_or_sources_or_credentials(self):
        invalid = ({"mode": "live"}, {"mode": "live-model"}, {"repeat": True}, {"repeat": 4},
                   {"collection_workers": 0}, {"model_call_limit": 0}, {"model_call_limit": 355},
                   {"selected_cases": []}, {"selected_cases": ["S02", "S02"]}, {"selected_cases": "S02"},
                   {"trace_dir": "bad\0path"})
        with patch.object(evaluation, "ScriptedWorkflowModel") as model, patch.object(evaluation, "FixedSources") as sources:
            for values in invalid:
                with self.subTest(values=values):
                    self.assertEqual(evaluation.evaluate_workflows(**values)["status"], "invalid_request")
            model.assert_not_called()
            sources.assert_not_called()

    def test_repeat_uses_distinct_sessions_and_ids_and_one_whole_run_allowance(self):
        original, calls = ResearchApiService.message, []

        def message(service, sid, token, request, question, **kwargs):
            calls.append((sid, request))
            return original(service, sid, token, request, question, **kwargs)
        with patch.object(ResearchApiService, "message", message):
            report = evaluation.evaluate_workflows(selected_cases=["C01"], repeat=3)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["planned_steps"], 6)
        self.assertEqual(len({sid for sid, request in calls}), 3)
        self.assertEqual(len({request for sid, request in calls}), 6)
        self.assertIsNone(report["trace_directory"])
        self.assertTrue(all(c["trace_directory"] is None for c in report["cases"]))
        limited = evaluation.evaluate_workflows(selected_cases=["K02", "S02"], repeat=2, model_call_limit=1)
        self.assertEqual(limited["status"], "failed")
        self.assertEqual(limited["accounting"]["forwarded"], 1)
        self.assertEqual((limited["summary"]["planned"], limited["summary"]["executed"], limited["summary"]["not_run"]), (4, 1, 3))
        self.assertTrue(all(s["code"] == "evaluation_budget_exhausted" for s in limited["steps"] if s["status"] == "not_run"))
        self.assertTrue(all(s["quality_checks"] is None for s in limited["steps"] if s["status"] == "not_run"))
        self.assertEqual(limited["summary"]["quality_requirement_failures"], 0)

    def test_a_failed_attempt_is_counted_once_and_later_independent_cases_continue(self):
        original = ScriptedWorkflowModel._respond

        def fail(model, phase, messages, tools):
            if phase == "knowledge":
                raise RuntimeError("private-failure-never-export")
            return original(model, phase, messages, tools)
        with patch.object(ScriptedWorkflowModel, "_respond", fail):
            report = evaluation.evaluate_workflows(selected_cases=["K02", "S02"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual([s["status"] for s in report["steps"]], ["failed", "passed"])
        self.assertEqual(report["accounting"]["forwarded"], 3)
        self.assertNotIn("private-failure-never-export", json.dumps(report))

    def test_wrong_route_is_not_adapted_into_a_passing_fixture(self):
        decision = next(spec.decision() for spec in ROUTE_SPECS if spec.intent == IntentKind.TECHNICAL_RESEARCH)
        with patch("trading_companion.api_service.route_research_request", return_value=decision):
            report = evaluation.evaluate_workflows(selected_cases=["S02"])
        step = report["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertTrue(step["outside_acceptable"])
        self.assertIn("fixture_script_mismatch", step["framework_errors"])

    def test_expectations_do_not_control_the_fixed_model(self):
        corpus, digest = evaluation.load_corpus()
        modified = deepcopy(corpus)
        step = next(c for c in modified["cases"] if c["id"] == "S02")["steps"][0]
        step["expectation"]["offline_expected"]["code"] = "company_research_partial"
        step["expectation"]["acceptable"][0]["code"] = ["company_research_partial"]
        with patch.object(evaluation, "load_corpus", return_value=(modified, digest)):
            report = evaluation.evaluate_workflows(selected_cases=["S02"])
        self.assertEqual(report["steps"][0]["observed"]["code"], "company_research_completed")
        self.assertEqual(report["status"], "failed")

    def test_partial_data_cannot_hide_a_rejected_model_answer(self):
        original = ScriptedWorkflowModel._respond

        def invalid(model, phase, messages, tools):
            if phase == "technical_answer":
                return ToolModelResponse("complete", "private-rejected-answer")
            return original(model, phase, messages, tools)
        with patch.object(ScriptedWorkflowModel, "_respond", invalid):
            report = evaluation.evaluate_workflows(selected_cases=["D02"])
        step = report["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["hard_requirement_violations"], [])
        self.assertEqual(step["quality_requirement_failures"], ["answer_origin"])
        self.assertFalse(step["outside_acceptable"])
        self.assertTrue(step["offline_expected_matched"])
        self.assertEqual(report["summary"]["hard_requirement_violations"], 0)
        self.assertEqual(report["summary"]["outside_acceptable"], 0)
        self.assertEqual(report["summary"]["quality_requirement_failures"], 1)
        self.assertNotIn("private-rejected-answer", json.dumps(report))

    def test_correct_identity_with_safe_fallback_is_only_a_quality_failure(self):
        corpus, _ = evaluation.load_corpus()
        step = next(c for c in corpus["cases"] if c["id"] == "D01")["steps"][0]
        observed = {**step["expectation"]["offline_expected"], "codes": ["company_research_partial"],
                    "securities": ["000001.SZ"], "answer_origin": "application", "answer_status": "fallback",
                    "coverage": [{"instrument_id": "000001.SZ", "sections": [{"kind": "financial", "status": "empty",
                                  "code": "no_eligible_evidence_in_bounded_response"}]}]}
        grade = evaluation.grade_step(step, observed, calls=2, collections=2)
        self.assertEqual(grade["hard_requirement_violations"], [])
        self.assertEqual(grade["quality_requirement_failures"], ["answer_origin", "answer_status"])
        self.assertTrue(grade["acceptable_outcome"])
        self.assertTrue(grade["offline_expected_matched"])
        # The same fallback must not mask a wrong security or missing evidence metadata.
        unsafe = {**observed, "securities": ["600000.SH"], "coverage": []}
        self.assertEqual(evaluation.grade_step(step, unsafe, calls=2, collections=2)["hard_requirement_violations"],
                         ["securities", "coverage_0"])

    def test_display_requirement_is_quality_and_unanswered_origin_is_not_checked(self):
        corpus, _ = evaluation.load_corpus()
        step = next(c for c in corpus["cases"] if c["id"] == "P01")["steps"][0]
        observed = {**step["expectation"]["offline_expected"], "codes": step["expectation"]["required"]["require_codes"],
                    "securities": step["expectation"]["required"]["securities_if_answered"],
                    "answer": "", "answer_origin": "model", "answer_status": "accepted", "coverage": []}
        grade = evaluation.grade_step(step, observed, calls=2, collections=6)
        self.assertEqual(grade["quality_requirement_failures"], ["display"])
        self.assertNotIn("display", grade["hard_requirement_violations"])
        rejected = {**observed, "category": "error"}
        self.assertEqual(evaluation.grade_step(step, rejected, calls=2, collections=6)["quality_checks"], {"display": False})

    def test_every_acceptable_annotation_has_a_unique_scenario_reason(self):
        corpus, _ = evaluation.load_corpus()
        reasons = [item["reason"] for case in corpus["cases"] for step in case["steps"]
                   for item in step["expectation"]["acceptable"]]
        self.assertEqual(len(reasons), 60)
        self.assertEqual(len(set(reasons)), len(reasons))
        for reason in reasons:
            self.assertNotIn("按照问题的明确对象和领域处理", reason)
        for case in corpus["cases"]:
            for step in case["steps"]:
                self.assertFalse(evaluation.QUALITY_KEYS.intersection(step["expectation"]["required"]))

    def test_missing_or_duplicate_trace_cannot_be_a_zero_call_success(self):
        with patch.object(JsonlTraceSink, "write", return_value=False):
            report = evaluation.evaluate_workflows(selected_cases=["S02"])
        self.assertEqual(report["summary"]["trace_missing"], 1)
        self.assertIn("trace_incomplete", report["steps"][0]["framework_errors"])
        self.assertEqual(report["accounting"]["forwarded"], 2)
        with patch("trading_companion.turn_trace.uuid4", return_value=type("Id", (), {"hex": "1" * 32})()):
            repeated = evaluation.evaluate_workflows(selected_cases=["C01"])
        self.assertEqual(repeated["status"], "failed")
        self.assertEqual(repeated["summary"]["trace_missing"], 1)

    def test_truncation_is_exposed_without_inventing_event_details(self):
        sink = JsonlTraceSink
        with patch.object(evaluation, "JsonlTraceSink", side_effect=lambda path: sink(path, limits=TraceLimits(max_events=0))):
            report = evaluation.evaluate_workflows(selected_cases=["S02"])
        trace = report["steps"][0]["trace"]
        self.assertTrue(trace["truncated"])
        self.assertEqual(trace["event_records"], [])
        self.assertGreater(trace["summaries"]["checks"]["seen"], 0)

    def test_network_attempt_is_detected_even_if_api_catches_the_exception(self):
        import socket
        original = ScriptedWorkflowModel._respond

        def connect(model, phase, messages, tools):
            try:
                socket.getaddrinfo("example.invalid", 443)
            except Exception:
                pass
            return original(model, phase, messages, tools)
        with patch.object(ScriptedWorkflowModel, "_respond", connect):
            report = evaluation.evaluate_workflows(selected_cases=["K02"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["code"], "evaluation_offline_boundary_or_io_error")

    def test_credentials_are_blocked_even_when_caught(self):
        original = ScriptedWorkflowModel._respond

        def read(model, phase, messages, tools):
            from trading_companion.local_env import load_model_environment
            try:
                load_model_environment()
            except Exception:
                pass
            return original(model, phase, messages, tools)
        with patch.object(ScriptedWorkflowModel, "_respond", read):
            report = evaluation.evaluate_workflows(selected_cases=["K02"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["credential_reads_attempted"], 1)

    def test_hard_requirements_and_acceptable_outcomes_are_graded_separately(self):
        corpus, _ = evaluation.load_corpus()
        step = next(c for c in corpus["cases"] if c["id"] == "M03")["steps"][0]
        observed = {**step["expectation"]["offline_expected"], "codes": ["product_security_limit"], "coverage": []}
        grade = evaluation.grade_step(step, observed, calls=1, collections=1)
        self.assertEqual(grade["hard_requirement_violations"], ["no_collection"])
        self.assertTrue(grade["acceptable_outcome"])
        alternative = {**observed, "status": "out_of_scope", "category": "rejected", "code": "product_request_out_of_scope"}
        self.assertTrue(evaluation.grade_step(step, alternative, calls=1, collections=0)["acceptable_outcome"])
        self.assertFalse(evaluation.grade_step(step, alternative, calls=1, collections=0)["offline_expected_matched"])

    def test_corpus_rejects_ambiguous_annotations_and_executable_configuration(self):
        corpus, _ = evaluation.load_corpus()
        for kind in ("duplicate_id", "empty_steps", "unknown_profile", "unknown_phase", "conflicting_codes",
                     "unaccepted_offline", "arbitrary_url", "missing_quality", "unknown_quality", "invalid_quality",
                     "invalid_origin", "invalid_answer_status", "invalid_answer_contains", "unknown_mode",
                     "missing_exclusion_reason", "missing_offline", *evaluation.QUALITY_KEYS):
            candidate = deepcopy(corpus)
            case = candidate["cases"][0]
            step = case["steps"][0]
            if kind == "duplicate_id":
                candidate["cases"][1]["id"] = case["id"]
            elif kind == "empty_steps":
                case["steps"] = []
            elif kind == "unknown_profile":
                case["data_profile"] = "https://example.invalid"
            elif kind == "unknown_phase":
                step["fixture_script"]["phases"] = ["eval"]
            elif kind == "conflicting_codes":
                step["expectation"]["required"]["require_codes"] = ["research_internal_error"]
            elif kind == "unaccepted_offline":
                step["expectation"]["offline_expected"]["code"] = "other"
            elif kind == "arbitrary_url":
                step["fixture_script"]["url"] = "https://example.invalid"
            elif kind == "missing_quality":
                del step["expectation"]["quality"]
            elif kind == "unknown_quality":
                step["expectation"]["quality"]["arbitrary"] = True
            elif kind == "invalid_quality":
                step["expectation"]["quality"] = []
            elif kind == "invalid_origin":
                step["expectation"]["quality"]["answer_origin_if_answered"] = "guessed"
            elif kind == "invalid_answer_status":
                step["expectation"]["quality"]["answer_status_if_answered"] = "guessed"
            elif kind == "invalid_answer_contains":
                step["expectation"]["quality"]["answer_contains"] = "text"
            elif kind == "unknown_mode":
                case["modes"].append("pretend-live")
            elif kind == "missing_exclusion_reason":
                case["modes"].remove("live")
            elif kind == "missing_offline":
                case["modes"].remove("offline")
                case["mode_exclusions"]["offline"] = "Not allowed to remove offline coverage"
            else:
                step["expectation"]["required"][kind] = ["text"] if kind == "answer_contains" else "model"
            path = Path(self.temporary.name) / "corpus.json"
            path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                evaluation.load_corpus(path)

    def test_fixed_company_and_quote_collectors_share_the_real_executor(self):
        with offline_network(), FixedSources("normal", 3) as sources:
            self.assertIs(sources.company._executor, sources.pool)
            self.assertIs(sources.quotes._executor, sources.pool)
            self.assertEqual((sources.company.max_workers, sources.quotes.max_workers), (3, 3))
            barrier = Barrier(3)
            original = sources.fault.before_response
            entries = []

            def gate(instrument, kind):
                entries.append(instrument)
                if len(entries) <= 3:
                    barrier.wait(5)
                original(instrument, kind)
            sources.fault.before_response = gate
            requests = tuple(ResearchDataRequest(item.instrument_id, item.name, NOW, date(2026, 7, 1), allow_current_background=True)
                             for item in sources.directory.securities[:3])
            batches = sources.company.collect_many(requests, kinds=("financial", "news"))
            self.assertEqual(len(batches), 3)
            self.assertEqual(sources.http.maximum, 3)

    def test_cli_defaults_offline_and_requires_explicit_real_configuration(self):
        output = StringIO()
        with redirect_stdout(output):
            code = command.main(["--case", "K01"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "offline")
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(command.main(["--mode", "live-model"]), 2)
