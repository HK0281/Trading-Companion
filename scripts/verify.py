"""Shared local/CI checks. Model and market checks use synthetic inputs only."""

import argparse
from contextlib import contextmanager
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import local
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/trading_companion/web_assets"
SAFE_ENVIRONMENT = (
    "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL",
    "VIRTUAL_ENV", "CI", "GITHUB_ACTIONS",
)
MVP_CASE_NAMES = (
    "single_sma", "single_ema", "single_macd", "single_rsi", "single_boll", "single_atr", "single_volume",
    "complete_combination", "partial_history", "default_indicators", "natural_name", "bare_code",
    "ambiguous_name", "unknown_name", "multiple_stocks", "today_rejected", "future_rejected", "default_dates",
    "knowledge", "news_not_supported", "order_not_supported", "target_not_supported", "followup_without_result",
    "identity_continuation", "indicator_edit", "ambiguous_edit", "latest_refresh", "relative_range_update",
    "saved_followup", "explicit_history",
)
MVP_REQUIRED_CHECKS = {
    "agent_status", "agent_code", "model_calls", "agent_model_count", "analysis_count", "report_status",
    "explanation_status", "no_failure_code", "synthesis_status", "response_count", "provider_mode", "no_raw_draft_display",
}
COMPANY_CASE_EXPECTATIONS = {
    # Model calls, analyses, synthetic company collections, synthetic daily reads.
    "pe_knowledge": (1, 0, 0, 0),
    "company_fundamentals": (2, 0, 1, 0),
    "two_stock_comparison": (2, 2, 2, 2),
    "announcements_news": (2, 0, 1, 0),
    "risk_scenario": (2, 1, 0, 1),
}
COMPANY_REQUIRED_CHECKS = {
    "answer_accepted", "synthetic_evidence", "model_budget", "model_calls", "analysis_budget", "plan_ready",
}


class VerificationError(RuntimeError):
    """A failed acceptance gate, not an application failure."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


@contextmanager
def clean_environment():
    """Do not forward provider credentials, proxies or custom Python hooks."""
    environment = {name: os.environ[name] for name in SAFE_ENVIRONMENT if name in os.environ}
    environment.update(PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT))),
                       PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
    with patch.dict(os.environ, environment, clear=True):
        yield


@contextmanager
def offline_network():
    """Reject in-process DNS/connect attempts, even when application code catches them."""
    attempts = []
    internal = local()
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_pair = socket.socketpair

    def denied(*args, **kwargs):
        attempts.append(True)
        raise VerificationError("Outbound network is disabled during offline checks")

    def connect(*args, **kwargs):
        if getattr(internal, "socketpair", False):
            return original_connect(*args, **kwargs)
        return denied()

    def connect_ex(*args, **kwargs):
        if getattr(internal, "socketpair", False):
            return original_connect_ex(*args, **kwargs)
        return denied()

    def socketpair(*args, **kwargs):
        # Windows Python 3.11 implements socketpair with a private loopback connect.
        internal.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.socketpair = False

    try:
        with patch("socket.socket.connect", connect), patch("socket.socket.connect_ex", connect_ex), \
                patch("socket.getaddrinfo", denied), patch("socket.socketpair", socketpair):
            yield
    finally:
        require(not attempts, "Offline checks attempted DNS or an outbound connection")


def run_strict_suite(suite, *, stream=None):
    planned = suite.countTestCases()
    require(planned > 0, "No Python tests discovered")
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    require(result.wasSuccessful() and not result.skipped and not result.expectedFailures
            and result.testsRun == planned,
            "Python suite failed, skipped tests, expected failures, or did not run all discovered tests")
    return result.testsRun


def check_evaluation(report):
    """Pin acceptance expectations independently of the evaluator's exit status."""
    summary = report["summary"]
    require(report["status"] == "passed" and report["provider"] == "fake"
            and report["live_provider_selected"] is False
            and report["fixture_data_authenticity"] == "synthetic"
            and report["market_data_requested"] is False
            and report["directory_network_requested"] is False,
            "Agent evaluation did not use the passing offline synthetic mode")
    require(summary["selected"] == summary["executed"] == summary["passed"] == 4
            and summary["failed"] == summary["not_run"] == 0
            and len(report["cases"]) == 4
            and {item["case"] for item in report["cases"]} == {"complete", "partial", "natural", "clarification"}
            and all(item["status"] == "passed" for item in report["cases"]),
            "All four Agent contract scenarios must run and pass")


def check_mvp_evaluation(report):
    """Require every independent repeat and its checks, not just a green summary."""
    summary, cases = report["summary"], report["cases"]
    require(report["version"] == "mvp-synthetic-evaluation-v1" and report["status"] == "passed"
            and report["provider"] == "fake" and report["live_provider_selected"] is False
            and report["fixture_data_authenticity"] == "synthetic" and report["market_data_requested"] is False
            and report["directory_network_requested"] is False and report["human_review_required"] is True
            and summary["quality_acceptance"] == "pending_live_repeats_and_human_review",
            "MVP evaluation must remain offline and must not claim live quality acceptance")
    require(report["repeat"] == 3 and set(report["selected_cases"]) == set(MVP_CASE_NAMES)
            and len(report["selected_cases"]) == 30 and len(cases) == 90
            and summary["selected"] == summary["executed"] == summary["passed"] == 90
            and summary["failed"] == summary["not_run"] == 0
            and {(item["case"], item["repeat"]) for item in cases}
                == {(name, repetition) for name in MVP_CASE_NAMES for repetition in (1, 2, 3)},
            "All 30 named MVP scenarios must pass three independent repeats without omissions")
    require(report["planned_model_calls"] == summary["model_calls_attempted"]
            == summary["provider_calls_forwarded"] == 201 and summary["analyses_used"] == 72
            and all(item["status"] == "passed" and item["steps"]
                and all(step["status"] == "passed" and step["failed_checks"] == []
                    and MVP_REQUIRED_CHECKS <= step["checks"].keys()
                    and all(value is True for value in step["checks"].values()) for step in item["steps"])
                for item in cases),
            "MVP model/analysis counts or per-step evidence and explanation checks did not pass")


def check_company_evaluation(report):
    """Verify each company workflow and budget independently of its green summary."""
    require(type(report) is dict, "Company evaluation report must be an object")
    require(report.get("version") == "company-research-evaluation-v1"
            and report.get("status") == "passed" and report.get("code") == "scenario_expectations_met"
            and report.get("provider") == "fake" and report.get("live_provider_selected") is False
            and report.get("fixture_data_authenticity") == "synthetic"
            and all(report.get(field) is False for field in (
                "real_market_data_requested", "news_network_requested", "directory_network_requested"))
            and report.get("evaluation_scope") == (
                "fixed_synthetic_product_workflows_and_output_contracts_not_language_or_investment_quality")
            and report.get("language_quality") == "requires_manual_review"
            and report.get("answer_display_policy") == "checked_application_display_only_not_raw_model_responses"
            and report.get("reference_date") == "2026-09-26",
            "Company evaluation must use offline synthetic inputs without claiming language or investment quality")
    selected, cases, summary = report.get("selected_cases"), report.get("cases"), report.get("summary")
    require(type(selected) is list and len(selected) == 5 and all(type(name) is str for name in selected)
            and set(selected) == set(COMPANY_CASE_EXPECTATIONS)
            and type(cases) is list and len(cases) == 5 and all(type(item) is dict for item in cases)
            and all(type(item.get("case")) is str for item in cases)
            and {item["case"] for item in cases} == set(COMPANY_CASE_EXPECTATIONS)
            and type(summary) is dict,
            "All five named company workflows must run once without omissions or duplicates")

    def count_is(mapping, field, expected):
        return type(mapping.get(field)) is int and mapping[field] == expected

    require(all(count_is(summary, field, expected) for field, expected in (
                ("selected", 5), ("executed", 5), ("passed", 5), ("failed", 0),
                ("model_calls", 9), ("provider_calls_forwarded", 9), ("analyses", 3)))
            and count_is(report, "required_model_call_budget", 9)
            and type(report.get("model_call_budget")) is int and 9 <= report["model_call_budget"] <= 10,
            "Company workflows require exactly nine model calls and three analyses within the fixed budget")
    for item in cases:
        calls, analyses, collections, reads = COMPANY_CASE_EXPECTATIONS[item["case"]]
        checks = item.get("checks")
        require(item.get("status") == "passed" and item.get("accepted") is True
                and item.get("fallback") is False and item.get("answer_origin") == "model"
                and type(item.get("answer")) is str and bool(item["answer"].strip())
                and item.get("code") == ("general_knowledge_answered" if item["case"] == "pe_knowledge"
                                          else "product_research_answered")
                and item.get("answer_validation_scope") == (
                    "format_and_scope_guards_not_prose_semantics" if item["case"] == "pe_knowledge" else
                    "citation_membership_and_lexical_numbers_not_arbitrary_prose_semantics")
                and type(checks) is dict and COMPANY_REQUIRED_CHECKS <= checks.keys()
                and all(value is True for value in checks.values()),
                "Every company workflow must accept its checked model answer with synthetic evidence and no fallback")
        require(all(count_is(item, field, expected) for field, expected in (
                    ("model_calls", calls), ("provider_calls_forwarded", calls), ("analyses", analyses),
                    ("synthetic_company_collections", collections), ("synthetic_daily_reads", reads))),
                "A company workflow has unexpected model, analysis or synthetic data access counts")
    require(all(summary[field] == sum(item[field] for item in cases)
                for field in ("model_calls", "provider_calls_forwarded", "analyses")),
            "Company summary counts must match the independently verified workflow counts")


def python_checks():
    with clean_environment(), offline_network():
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
        count = run_strict_suite(suite)
        from trading_companion.agent_evaluation import run_evaluation
        from trading_companion.api_demo import main as api_demo
        from trading_companion.mvp_evaluation import run_mvp_evaluation
        from scripts.evaluate_company_research import evaluate_company_research

        check_evaluation(run_evaluation(provider="fake", case="all"))
        check_mvp_evaluation(run_mvp_evaluation(case="all", repeat=3, model_call_limit=201))
        check_company_evaluation(evaluate_company_research())
        require(api_demo() == 0, "In-process API walkthrough failed")
    print(f"PASS: {count} Python tests, 4 Agent scenarios, 90 MVP repeats, "
          "5 company workflows and API walkthrough; no skips", flush=True)


def command(arguments, *, timeout=300):
    """Use argument arrays and fail on nonzero exit; never retry automatically."""
    print("RUN: " + " ".join(str(item) for item in arguments), flush=True)
    subprocess.run([str(item) for item in arguments], cwd=ROOT, check=True, timeout=timeout)


def check_frontend_report(report):
    assertions = [item for group in report["testResults"] for item in group["assertionResults"]]
    count = report["numTotalTests"]
    require(report["success"] is True and count > 0 and len(assertions) == count
            and report["numPassedTests"] == count and report["numFailedTests"] == 0
            and report.get("numPendingTests", 0) == report.get("numTodoTests", 0) == 0
            and all(item["status"] == "passed" for item in assertions),
            "Frontend tests failed, skipped, todo, missing, or empty")
    return count


def _web_checks():
    pnpm = shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm")
    require(pnpm is not None, "Install the packageManager version of pnpm from frontend/package.json first")
    with tempfile.TemporaryDirectory(prefix="trading-companion-vitest-") as temporary:
        report = Path(temporary) / "vitest.json"
        command([pnpm, "--dir", "frontend", "test", "--reporter=json", "--outputFile=" + str(report)])
        count = check_frontend_report(json.loads(report.read_text(encoding="utf-8")))
    command([pnpm, "--dir", "frontend", "build"])
    print(f"PASS: {count} frontend tests without skips, TypeScript check and production build", flush=True)


def web_checks():
    with clean_environment():
        _web_checks()


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script":
            require(attrs.get("type") == "module" and "src" in attrs,
                    "Compiled HTML must reference module scripts, not inline code")
            self.references.append((attrs["src"], ".js"))
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            require("href" in attrs, "Stylesheet link has no href")
            self.references.append((attrs["href"], ".css"))


def built_assets(root):
    require(root.is_dir() and (root / "index.html").is_file(), "Web build is missing; run the web gate first")
    parser = AssetReferences()
    index = (root / "index.html").read_bytes()
    parser.feed(index.decode("utf-8"))
    require({extension for _, extension in parser.references} == {".js", ".css"},
            "Compiled HTML must reference both JavaScript and CSS")
    files = {"index.html": index}
    for url, extension in parser.references:
        require(re.fullmatch(r"/assets/[A-Za-z0-9_-]{1,100}" + re.escape(extension), url) is not None,
                "Compiled asset URL is not an allowed same-origin path")
        relative = url.lstrip("/")
        path = root / relative
        require(path.resolve().is_relative_to(root.resolve()) and path.is_file(), "Compiled asset is missing or escaped")
        require(relative not in files, "Compiled HTML references duplicate assets")
        files[relative] = path.read_bytes()
        require(bool(files[relative]), "Compiled asset is empty")
    inventory = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    require(inventory == set(files), "Build contains unreferenced or unexpected files")
    return files


def check_served_assets(files):
    from fastapi.testclient import TestClient
    from trading_companion.api import create_app

    with TestClient(create_app(), base_url="http://localhost") as client:
        require(client.get("/health").status_code == 200, "Same-origin API health check failed")
        for relative, content in files.items():
            url = "/" if relative == "index.html" else "/" + relative
            response = client.get(url)
            media = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[Path(relative).suffix]
            require(response.status_code == 200 and response.content == content
                    and media in response.headers.get("content-type", ""),
                    "FastAPI did not serve the exact compiled asset with its expected MIME type")
            require("script-src 'self'" in response.headers.get("content-security-policy", "")
                    and response.headers.get("x-content-type-options") == "nosniff",
                    "Compiled asset security headers are missing")
        for path in ("/src/App.tsx", "/.env", "/assets/app.js.map"):
            require(client.get(path).status_code == 404, "Web route exposed a non-public file")


def check_wheel(wheel, files):
    prefix = "trading_companion/web_assets/"
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Wheel has duplicate entries")
        for name in names:
            parts = name.split("/")
            require(not any(part.startswith(".env") or part == "node_modules" for part in parts)
                    and not name.endswith((".tsx", ".ts", ".map")), "Wheel contains non-public frontend inputs")
        assets = {name[len(prefix):] for name in names if name.startswith(prefix) and not name.endswith("/")}
        require(assets == set(files), "Wheel is missing compiled assets or contains stale assets")
        for relative, content in files.items():
            require(archive.read(prefix + relative) == content, "Wheel asset bytes differ from the checked build")


def stage_package(destination):
    """Copy only package inputs; never reuse or delete the developer's build directory."""
    project = destination / "project"
    (project / "src").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(ROOT / name, project / name)
    shutil.copytree(ROOT / "src/trading_companion", project / "src/trading_companion",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    return project


def bundle_checks():
    with clean_environment(), offline_network():
        files = built_assets(ASSETS)
        check_served_assets(files)
    # Build isolation may download build tools; no application code is run against a live provider.
    with tempfile.TemporaryDirectory(prefix="trading-companion-wheel-") as temporary:
        project = stage_package(Path(temporary))
        command([sys.executable, "-m", "pip", "wheel", project, "--no-deps", "--wheel-dir", temporary])
        wheels = list(Path(temporary).glob("*.whl"))
        require(len(wheels) == 1, "Expected exactly one project wheel")
        check_wheel(wheels[0], files)
    print(f"PASS: {len(files)} exact compiled assets served by ASGI and included in wheel", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline contract/build acceptance; never selects a live provider")
    parser.add_argument("suite", choices=("python", "web", "bundle", "all"), nargs="?", default="all")
    args = parser.parse_args(argv)
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    try:
        for name, check in (("python", python_checks), ("web", web_checks), ("bundle", bundle_checks)):
            if args.suite in (name, "all"):
                check()
        command([sys.executable, "-m", "pip", "check"])
    except (VerificationError, subprocess.SubprocessError, OSError, ImportError, KeyError, TypeError, ValueError) as error:
        # Do not print arbitrary provider responses, environments or credentials.
        detail = str(error) if isinstance(error, VerificationError) else type(error).__name__
        print(f"FAIL: {detail}. See docs/ci-design.md.", file=sys.stderr)
        return 1
    print("PASS: selected acceptance gates completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
