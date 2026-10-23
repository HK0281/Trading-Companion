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
                       PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
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


def python_checks():
    with clean_environment(), offline_network():
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
        count = run_strict_suite(suite)
        from trading_companion.agent_evaluation import run_evaluation
        from trading_companion.api_demo import main as api_demo

        check_evaluation(run_evaluation(provider="fake", case="all"))
        require(api_demo() == 0, "In-process API walkthrough failed")
    print(f"PASS: {count} Python tests, 4 Agent scenarios and API walkthrough; no skips", flush=True)


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
