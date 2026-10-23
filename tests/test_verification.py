"""Acceptance gates must reject incomplete, skipped and misleading green results."""

from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts import verify


class StrictPythonTests(unittest.TestCase):
    def run_cases(self, *cases):
        return verify.run_strict_suite(unittest.TestSuite(cases), stream=StringIO())

    def test_passing_suite_reports_executed_count(self):
        self.assertEqual(self.run_cases(unittest.FunctionTestCase(lambda: None)), 1)

    def test_empty_discovery_is_not_a_pass(self):
        with self.assertRaises(verify.VerificationError):
            self.run_cases()

    def test_failures_and_errors_are_not_a_pass(self):
        for error in (AssertionError, ValueError):
            def broken():
                raise error("artificial failure")
            with self.subTest(error=error), self.assertRaises(verify.VerificationError):
                self.run_cases(unittest.FunctionTestCase(broken))

    def test_optional_dependency_skip_is_not_a_pass(self):
        def skipped():
            raise unittest.SkipTest("artificial missing dependency")
        with self.assertRaises(verify.VerificationError):
            self.run_cases(unittest.FunctionTestCase(skipped))

    def test_expected_failure_is_not_a_pass(self):
        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def test_failure(self):
                self.fail("artificial expected failure")
        with self.assertRaises(verify.VerificationError):
            self.run_cases(Expected("test_failure"))

    def test_unexpected_success_is_not_a_pass(self):
        class Unexpected(unittest.TestCase):
            @unittest.expectedFailure
            def test_success(self):
                pass
        with self.assertRaises(verify.VerificationError):
            self.run_cases(Unexpected("test_success"))


class OfflineBoundaryTests(unittest.TestCase):
    def test_socketpair_remains_available_for_asgi_event_loop(self):
        with verify.offline_network():
            left, right = socket.socketpair()
            with left, right:
                left.sendall(b"internal-only")
                self.assertEqual(right.recv(32), b"internal-only")

    def test_caught_dns_failure_still_fails_gate(self):
        with self.assertRaises(verify.VerificationError):
            with verify.offline_network():
                with self.assertRaises(verify.VerificationError):
                    socket.getaddrinfo("must-not-resolve.invalid", 443)

    def test_caught_connect_and_connect_ex_still_fail_gate(self):
        for method in ("connect", "connect_ex"):
            with self.subTest(method=method), self.assertRaises(verify.VerificationError):
                with verify.offline_network(), socket.socket() as connection:
                    with self.assertRaises(verify.VerificationError):
                        getattr(connection, method)(("192.0.2.1", 443))

    def test_environment_forwards_only_allowlisted_settings(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "artificial-only", "CUSTOM_SECRET": "artificial-only",
                                     "HTTPS_PROXY": "artificial-only", "PYTHONPATH": "untrusted-hook"}):
            with verify.clean_environment():
                for name in ("DEEPSEEK_API_KEY", "CUSTOM_SECRET", "HTTPS_PROXY"):
                    self.assertNotIn(name, os.environ)
                self.assertEqual(os.environ["PYTHONPATH"], os.pathsep.join((str(verify.ROOT / "src"), str(verify.ROOT))))
                self.assertEqual(os.environ["PYTHONUTF8"], "1")
            self.assertEqual(os.environ["CUSTOM_SECRET"], "artificial-only")


def frontend_report():
    return {"success": True, "numTotalTests": 2, "numPassedTests": 2, "numFailedTests": 0,
            "numPendingTests": 0, "numTodoTests": 0,
            "testResults": [{"assertionResults": [{"status": "passed"}, {"status": "passed"}]}]}


class FrontendGateTests(unittest.TestCase):
    def test_all_assertions_must_pass(self):
        self.assertEqual(verify.check_frontend_report(frontend_report()), 2)

    def test_success_flag_does_not_hide_pending_todo_or_failed_counts(self):
        for field in ("numPendingTests", "numTodoTests", "numFailedTests"):
            report = frontend_report()
            report[field] = 1
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_frontend_report(report)

    def test_empty_frontend_suite_is_not_a_pass(self):
        report = frontend_report()
        report.update(numTotalTests=0, numPassedTests=0, testResults=[])
        with self.assertRaises(verify.VerificationError):
            verify.check_frontend_report(report)

    def test_skipped_assertion_and_missing_results_are_not_a_pass(self):
        for status in ("skipped", "todo", "pending", "failed"):
            report = frontend_report()
            report["testResults"][0]["assertionResults"][0]["status"] = status
            with self.subTest(status=status), self.assertRaises(verify.VerificationError):
                verify.check_frontend_report(report)
        report = frontend_report()
        report["testResults"] = []
        with self.assertRaises(verify.VerificationError):
            verify.check_frontend_report(report)


class EvaluationGateTests(unittest.TestCase):
    def report(self):
        return {"status": "passed", "provider": "fake", "live_provider_selected": False,
                "fixture_data_authenticity": "synthetic", "market_data_requested": False,
                "directory_network_requested": False,
                "summary": {"selected": 4, "executed": 4, "passed": 4, "failed": 0, "not_run": 0},
                "cases": [{"case": name, "status": "passed"} for name in ("complete", "partial", "natural", "clarification")]}

    def test_four_named_offline_cases_are_accepted(self):
        verify.check_evaluation(self.report())

    def test_live_or_non_synthetic_modes_are_rejected(self):
        for field, value in (("provider", "deepseek"), ("live_provider_selected", True),
                             ("fixture_data_authenticity", "unverified"), ("market_data_requested", True),
                             ("directory_network_requested", True)):
            report = self.report()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_evaluation(report)

    def test_skipped_failed_or_replaced_case_cannot_hide_behind_summary(self):
        for field, value in (("status", "skipped"), ("status", "failed"), ("case", "different")):
            report = self.report()
            report["cases"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(verify.VerificationError):
                verify.check_evaluation(report)


class MvpEvaluationGateTests(unittest.TestCase):
    def report(self):
        return {"version": "mvp-synthetic-evaluation-v1", "status": "passed", "provider": "fake",
                "live_provider_selected": False, "fixture_data_authenticity": "synthetic",
                "market_data_requested": False, "directory_network_requested": False, "human_review_required": True,
                "repeat": 3, "selected_cases": list(verify.MVP_CASE_NAMES), "planned_model_calls": 201,
                "summary": {"selected": 90, "executed": 90, "passed": 90, "failed": 0, "not_run": 0,
                    "model_calls_attempted": 201, "provider_calls_forwarded": 201, "analyses_used": 72,
                    "quality_acceptance": "pending_live_repeats_and_human_review"},
                "cases": [{"case": name, "repeat": repeat, "status": "passed",
                    "steps": [{"status": "passed", "failed_checks": [],
                               "checks": dict.fromkeys(verify.MVP_REQUIRED_CHECKS, True)}]}
                    for name in verify.MVP_CASE_NAMES for repeat in (1, 2, 3)]}

    def test_all_named_offline_repeats_with_step_checks_are_accepted(self):
        verify.check_mvp_evaluation(self.report())

    def test_live_data_or_a_premature_quality_claim_is_rejected(self):
        for field, value in (("provider", "deepseek"), ("live_provider_selected", True),
                             ("market_data_requested", True), ("directory_network_requested", True),
                             ("fixture_data_authenticity", "unverified"), ("human_review_required", False)):
            report = self.report()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_mvp_evaluation(report)
        report = self.report()
        report["summary"]["quality_acceptance"] = "accepted"
        with self.assertRaises(verify.VerificationError):
            verify.check_mvp_evaluation(report)

    def test_duplicate_missing_or_replaced_repeat_cannot_hide_behind_ninety_passes(self):
        for changes in ({"case": "invented_case"}, {"repeat": 2}, {"status": "not_run"}):
            report = self.report()
            report["cases"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(verify.VerificationError):
                verify.check_mvp_evaluation(report)

    def test_incorrect_call_or_analysis_count_is_rejected(self):
        for field in ("model_calls_attempted", "provider_calls_forwarded", "analyses_used"):
            report = self.report()
            report["summary"][field] -= 1
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.check_mvp_evaluation(report)

    def test_green_summary_cannot_hide_a_failed_missing_or_empty_check(self):
        for mode in ("failed", "missing", "empty", "non_boolean", "failed_keys", "no_steps"):
            report = self.report()
            step = report["cases"][0]["steps"][0]
            if mode == "failed":
                step["checks"]["explanation_status"] = False
            elif mode == "missing":
                del step["checks"]["synthesis_status"]
            elif mode == "empty":
                step["checks"] = {}
            elif mode == "non_boolean":
                step["checks"]["no_failure_code"] = 1
            elif mode == "failed_keys":
                step["failed_checks"] = ["explanation_status"]
            else:
                report["cases"][0]["steps"] = []
            with self.subTest(mode=mode), self.assertRaises(verify.VerificationError):
                verify.check_mvp_evaluation(report)


HTML = b'<script type="module" src="/assets/app-check.js"></script><link rel="stylesheet" href="/assets/index-check.css">'


class BundleGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "assets").mkdir()
        (self.root / "index.html").write_bytes(HTML)
        (self.root / "assets/app-check.js").write_bytes(b'"use strict";')
        (self.root / "assets/index-check.css").write_bytes(b"body { color: black; }")

    def test_compiled_references_resolve_and_match_http_bytes(self):
        files = verify.built_assets(self.root)
        self.assertEqual(len(files), 3)
        with patch("trading_companion.web_ui.ASSET_ROOT", self.root), verify.offline_network():
            verify.check_served_assets(files)

    def test_missing_build_is_a_failure_not_an_optional_skip(self):
        with self.assertRaises(verify.VerificationError):
            verify.built_assets(self.root / "absent")

    def test_external_source_traversal_and_query_urls_are_rejected(self):
        for url in ("https://external.invalid/app.js", "/src/App.tsx", "/assets/../app.js", "/assets/app-check.js?v=1"):
            (self.root / "index.html").write_bytes(HTML.replace(b"/assets/app-check.js", url.encode()))
            with self.subTest(url=url), self.assertRaises(verify.VerificationError):
                verify.built_assets(self.root)

    def test_inline_scripts_and_incomplete_build_are_rejected(self):
        for content in (b"<script>alert(1)</script>", b'<script type="module" src="/assets/app-check.js"></script>'):
            (self.root / "index.html").write_bytes(content)
            with self.subTest(content=content), self.assertRaises(verify.VerificationError):
                verify.built_assets(self.root)

    def test_unreferenced_map_or_stale_build_file_is_rejected(self):
        (self.root / "assets/stale.js.map").write_bytes(b"{}")
        with self.assertRaises(verify.VerificationError):
            verify.built_assets(self.root)

    def test_empty_asset_is_rejected(self):
        (self.root / "assets/app-check.js").write_bytes(b"")
        with self.assertRaises(verify.VerificationError):
            verify.built_assets(self.root)

    def make_wheel(self, files, extras=None):
        wheel = self.root / "test.whl"
        with ZipFile(wheel, "w") as archive:
            for relative, content in files.items():
                archive.writestr("trading_companion/web_assets/" + relative, content)
            for name, content in (extras or {}).items():
                archive.writestr(name, content)
        return wheel

    def test_wheel_contains_the_exact_checked_build(self):
        files = verify.built_assets(self.root)
        verify.check_wheel(self.make_wheel(files), files)

    def test_missing_changed_and_stale_wheel_assets_are_rejected(self):
        files = verify.built_assets(self.root)
        for mode in ("missing", "changed", "stale"):
            changed = deepcopy(files)
            if mode == "missing":
                del changed["index.html"]
            elif mode == "changed":
                changed["index.html"] = b"wrong build"
            else:
                changed["assets/old.js"] = b"stale"
            wheel = self.make_wheel(changed)
            with self.subTest(mode=mode), self.assertRaises(verify.VerificationError):
                verify.check_wheel(wheel, files)

    def test_wheel_rejects_environment_sources_maps_and_dependencies(self):
        files = verify.built_assets(self.root)
        for name in (".env", "nested/.env.local", "frontend/src/App.tsx", "frontend/node_modules/library.js", "assets/app.js.map"):
            wheel = self.make_wheel(files, {name: b"artificial-only"})
            with self.subTest(name=name), self.assertRaises(verify.VerificationError):
                verify.check_wheel(wheel, files)

    def test_package_staging_ignores_old_builds_and_repository_private_files(self):
        repository = self.root / "repository"
        package = repository / "src/trading_companion"
        package.mkdir(parents=True)
        (package / "__init__.py").write_bytes(b"")
        (package / "__pycache__").mkdir()
        (package / "__pycache__/old.pyc").write_bytes(b"cache")
        (repository / "pyproject.toml").write_bytes(b"package configuration")
        (repository / "README.md").write_bytes(b"readme")
        (repository / ".env").write_bytes(b"artificial-private-file")
        (repository / "build").mkdir()
        (repository / "build/stale.js").write_bytes(b"stale")
        with patch.object(verify, "ROOT", repository):
            staged = verify.stage_package(self.root / "staging")
        names = {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()}
        self.assertEqual(names, {"pyproject.toml", "README.md", "src/trading_companion/__init__.py"})
        self.assertTrue((repository / "build/stale.js").is_file())


class EntrypointTests(unittest.TestCase):
    def test_selected_suite_runs_without_other_suites_or_hidden_install(self):
        with patch.object(verify, "python_checks") as python, patch.object(verify, "web_checks") as web, \
                patch.object(verify, "bundle_checks") as bundle, patch.object(verify, "command") as command:
            with redirect_stdout(StringIO()):
                self.assertEqual(verify.main(["web"]), 0)
        web.assert_called_once_with()
        python.assert_not_called()
        bundle.assert_not_called()
        self.assertEqual(command.call_args.args[0][-2:], ["pip", "check"])

    def test_failed_gate_returns_nonzero_and_does_not_run_later_gates(self):
        with patch.object(verify, "python_checks", side_effect=verify.VerificationError("artificial-only")), \
                patch.object(verify, "web_checks") as web, patch.object(verify, "command") as command:
            with redirect_stderr(StringIO()):
                self.assertEqual(verify.main(["all"]), 1)
        web.assert_not_called()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
