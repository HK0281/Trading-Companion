"""Real message routing/collection with artificial inputs; no model-quality claims. Also covers workflow metrics and reporting."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
import unittest

from scripts import evaluate_research_workflows as command, compare_research_workflows as compare_command
from scripts.verify import offline_network
from trading_companion import workflow_evaluation as evaluation
from trading_companion.api_service import ResearchApiService
from trading_companion.company_research_contracts import ResearchDataRequest
from trading_companion.request_routing import ROUTE_SPECS, IntentKind
from trading_companion.tool_model import ToolModelResponse
from trading_companion.trace_cli import TraceReader, TraceSummary
from trading_companion.turn_trace import JsonlTraceSink, TraceLimits
from trading_companion.workflow_evaluation import evaluate_workflows
from trading_companion.workflow_evaluation_fixtures import FixedSources, ScriptedWorkflowModel, NOW
from trading_companion.workflow_evaluation_metrics import summarize_steps, evaluation_metrics
from trading_companion.workflow_evaluation_reporting import (
    manual_review,
    verified_facts,
    format_report,
    write_report,
    load_report,
    compare_reports,
    format_comparison,
)


# ---- test_workflow_evaluation: Real message routing/collection with artificial inputs; no model-quality claims.

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
                self.assertEqual(report["accounting"]["forwarded"], 67)
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
        self.assertEqual(expected["models"]["attempted"], 67)
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

    def test_b_delta_preserves_parent_and_changes_only_two_versioned_steps(self):
        raw = evaluation.CORPUS.read_bytes().replace(b'\r\n', b'\n')
        parent = json.loads(raw)
        current = evaluation._apply_b_delta(deepcopy(parent), raw)
        _, digest = evaluation.load_corpus()
        descriptor = evaluation.b_delta_descriptor()
        self.assertEqual(descriptor['parent_sha256'], sha256(raw).hexdigest())
        self.assertNotEqual(digest, descriptor['parent_sha256'])
        changed = [(old['id'], index) for old, new in zip(parent['cases'], current['cases'])
                   for index, (a, b) in enumerate(zip(old['steps'], new['steps']), 1) if a != b]
        self.assertEqual(changed, [('C05', 2), ('C05', 3)])
        delta = json.loads(evaluation.B_DELTA.read_text(encoding='utf-8'))
        for key in ('parent_sha256_lf', 'old'):
            broken = deepcopy(delta)
            if key == 'old':
                broken['changes'][0]['old']['question'] = 'tampered'
            else:
                broken[key] = '0' * 64
            path = Path(self.temporary.name) / 'delta.json'
            path.write_text(json.dumps(broken), encoding='utf-8')
            with patch.object(evaluation, 'B_DELTA', path), self.assertRaises(ValueError):
                evaluation.load_corpus()

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

    def test_technical_delta_preserves_questions_and_requirements_and_reports_version(self):
        delta = json.loads(evaluation.TECHNICAL_DELTA.read_text(encoding='utf-8'))
        self.assertEqual([(c['case_id'], c['step']) for c in delta['changes']],
                         [('C02', 1), ('C02', 2), ('C03', 1), ('C03', 2), ('C03', 3), ('D02', 1)])
        for change in delta['changes']:
            for key in ('question', 'history'):
                self.assertEqual(change['old'][key], change['new'][key])
            for key in ('required', 'quality'):
                self.assertEqual(change['old']['expectation'][key], change['new']['expectation'][key])
        from trading_companion.workflow_evaluation_runtime import make_plan
        plan, _, _ = make_plan(selected_cases=['C02'])
        self.assertEqual(plan['corpus']['rule_baseline_version'], 'decision-horizon-v1')
        self.assertEqual(plan['corpus']['technical_delta'], evaluation.technical_delta_descriptor())
        broken = deepcopy(delta)
        broken['changes'][0]['new']['question'] = 'tampered'
        path = Path(self.temporary.name) / 'technical-delta.json'
        path.write_text(json.dumps(broken), encoding='utf-8')
        with patch.object(evaluation, 'TECHNICAL_DELTA', path), self.assertRaises(ValueError):
            evaluation.load_corpus()

    def test_horizon_delta_keeps_original_question_and_exposes_parent_chain(self):
        delta = json.loads(evaluation.HORIZON_DELTA.read_text(encoding='utf-8'))
        self.assertEqual([(c['case_id'],c['step']) for c in delta['changes']], [('S05',1)])
        change = delta['changes'][0]
        self.assertEqual(change['old']['question'], change['new']['question'])
        self.assertEqual(change['new']['fixture_script']['phases'], ['company_intent'])
        self.assertEqual(change['new']['expectation']['offline_expected']['code'], 'decision_horizon_required')
        from trading_companion.workflow_evaluation_runtime import make_plan
        plan, _, _ = make_plan(selected_cases=['S05'])
        self.assertEqual(plan['corpus']['horizon_delta'], evaluation.horizon_delta_descriptor())
        self.assertEqual(plan['scripted_model_calls'],1)
        self.assertEqual(set(plan['corpus']['horizon_delta']['parents_sha256_lf']),
            {'research-api-v1.json','research-api-b-delta-v1.json','research-api-technical-delta-v1.json'})

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
            if phase == "company_answer":
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


# ---- test_workflow_evaluation_metrics: Hand-checked denominators, legal alternatives, repair history and incomplete trace details.

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


# ---- test_workflow_evaluation_reporting: Exported reports preserve unknowns, human blanks, failures and unpaired comparison rows.

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


if __name__ == '__main__':
    unittest.main()
