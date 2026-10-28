"""Offline characterization observer; capture is explicit and never used by tests."""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
from unittest.mock import patch

from scripts.verify import clean_environment, offline_network
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.screening import demo_screening_snapshot
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.tool_model import ToolCall
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_product_modules_api import QuoteSource
from tests.test_product_research import CompanyDataFixture, MemoryDailySource, MemorySourceFactory, NOW
from tests.test_screening_followup_api import JourneyModel


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/request-routing-v1.json"
OBSERVER_VERSION = "routing-observer-v1"
PROJECTION_VERSION = "routing-projection-v1"
UUID = re.compile(r"\b[0-9a-f]{32}\b")
EXECUTORS = (
    ("trading_companion.api_service.run_general_knowledge_turn", "general_knowledge"),
    ("trading_companion.api_service.run_natural_research_turn", "technical_research"),
    ("trading_companion.screening_agent.run_screening_edit_turn", "screening_edit"),
    ("trading_companion.screening_agent.run_screening_turn", "screening"),
    ("trading_companion.screening_followup.run_screening_followup_turn", "screening_followup"),
    ("trading_companion.product_agent.run_product_research_turn", "company_research"),
    ("trading_companion.screening_followup.execute_product", "selected_company_execution"),
    ("trading_companion.screening_followup._run_prepared", "selected_technical_execution"),
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


class Normalizer:
    """Only random UUIDs change; semantic numbers, dates, order and text survive."""

    def __init__(self):
        self.ids = {}

    def text(self, value):
        def substitute(match):
            identifier = match.group()
            if identifier not in self.ids:
                self.ids[identifier] = f"<uuid-{len(self.ids) + 1}>"
            return self.ids[identifier]
        return UUID.sub(substitute, value)

    def __call__(self, value):
        if value is None or type(value) in (bool, int, float):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {self.text(str(key)): self(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(item) for item in value]
        if is_dataclass(value):
            return {field.name: self(getattr(value, field.name)) for field in fields(value)}
        raise TypeError(f"Unregistered baseline value: {type(value).__name__}")


class Journey:
    """Real service/Agent with existing synthetic delegates and a read-only observer."""

    def __init__(self, stack, config):
        self.stack, self.config = stack, config
        self.events, self.executions, self.errors = [], [], []
        self.now, self.clock_values = NOW, []
        self.normal = Normalizer()
        sample = demo_screening_snapshot(clock=lambda: NOW)
        identities = tuple(replace(item, aliases=("平安",)) if item.instrument_id == "000001.SZ" else item
                           for item in sample.directory.securities)
        self.directory = SecurityDirectory((*identities, Security("600000.SH", "浦发银行"),
            Security("601318.SH", "中国平安", ("平安",))), "offline_identity_directory", NOW)
        quote_directory = self.directory
        self.development_source = None
        if config.get("development"):
            self.development_source, self.directory = load_development_fixtures()
        self.quotes = QuoteSource(replace(sample, directory=quote_directory, is_fixture=False,
                                          source="offline_quote_source"))
        self.quote_tag = "original_quotes"
        self.model, self.data, self.daily = JourneyModel(self.directory), CompanyDataFixture(), MemorySourceFactory()
        self.technical_indicators = ("SMA3",)
        self.directory_mode = config.get("directory", "ready")
        self.control = None
        self.stack.enter_context(patch("trading_companion.product_research._build_source", self.daily))
        self.stack.enter_context(patch("trading_companion.research_tool._build_source", self.daily))
        self.observe(self.model, "complete", self.model_event)
        self.observe(self.data, "collect", self.company_event)
        self.observe(self.quotes, "read_snapshot", self.quote_event)
        original_read = MemoryDailySource.read_daily_bars

        def read_daily(source):
            self.events.append({"event": "daily_read", "request": self.normal(source.request)})
            return original_read(source)
        self.stack.enter_context(patch.object(MemoryDailySource, "read_daily_bars", read_daily))
        for target, kind in EXECUTORS:
            module_name, attribute = target.rsplit(".", 1)
            from importlib import import_module
            original = getattr(import_module(module_name), attribute)
            self.stack.enter_context(patch(target, self.executor(original, kind)))
        self.service = ResearchApiService(model_factory=lambda: self.model, directory_loader=self.load_directory,
            provider="fake", data_mode="synthetic" if self.development_source is not None else "tencent",
            development_source=self.development_source, product_data_service=None if config.get("company") is False else self.data,
            screening_source=None if config.get("screening") is False else self.quotes,
            workflow_enabled=config.get("workflow", True), clock=lambda: 100.0,
            wall_clock=self.wall_clock, control_factory=self.new_control)
        self.stack.callback(self.service.close)
        self.credentials = {"main": self.service.create_session()}

    def observe(self, owner, name, wrapper):
        original = getattr(owner, name)
        self.stack.enter_context(patch.object(owner, name, lambda *args, **kwargs: wrapper(original, *args, **kwargs)))

    def executor(self, original, kind):
        def execute(*args, **kwargs):
            self.executions.append(kind)
            self.events.append({"event": "executor", "kind": kind})
            try:
                return original(*args, **kwargs)
            except Exception as error:
                self.errors.append(f"{kind}: {type(error).__name__}: {error}")
                raise
        return execute

    def new_control(self):
        company = self.config.get("company") is not False
        self.control = TurnControl(TurnLimits(model_calls=6, analyses=3 if company else 2,
            profile="company_research" if company else "research_workflow"), clock=lambda: 10.0)
        return self.control

    def wall_clock(self):
        if self.clock_values:
            self.now = self.clock_values.pop(0)
        self.events.append({"event": "wall_clock", "value": self.now.isoformat()})
        return self.now

    def load_directory(self, now):
        self.events.append({"event": "directory_load", "at": now.isoformat(), "mode": self.directory_mode})
        if self.directory_mode == "missing":
            raise DataSourceError("Artificial unavailable directory")
        if self.directory_mode == "stale":
            return replace(self.directory, retrieved_at=NOW - timedelta(days=40))
        return self.directory

    def model_event(self, original, messages, *, tools):
        self.events.append({"event": "model_forward", "tools": [tool.name for tool in tools]})
        response = original(messages, tools=tools)
        if tools and tools[0].name == INTENT_TOOL_NAME:
            call = response.tool_calls[0]
            response = replace(response, tool_calls=(ToolCall(call.call_id, call.name,
                {**call.arguments, "indicator_mentions": list(self.technical_indicators)}),))
        return response

    def company_event(self, original, request, kinds):
        self.events.append({"event": "company_read", "request": self.normal(request), "kinds": list(kinds)})
        return original(request, kinds)

    def quote_event(self, original, directory):
        self.events.append({"event": "quote_read", "source": self.quote_tag})
        return original(directory)

    def state(self, entry):
        session = entry.session
        saved = session.screening_saved
        company = session.product_saved
        # to_dict() alone omits product/shortlist state. Sources are represented by
        # stable identity tags; the real source objects stay untouched in memory.
        def pending(value):
            if value is None:
                return None
            return {field.name: self.normal(getattr(value, field.name)) for field in fields(value)
                    if field.name != "development_source"}
        return self.normal({
            "technical": session.to_dict(), "pending": pending(session.pending),
            "pending_edit": pending(session.pending_edit), "product_pending": pending(session.product_pending),
            "product_saved": None if company is None else {"saved_at": company.saved_at,
                "plan": company.plan.to_dict(), "answer": company.answer.to_dict()},
            "screening_saved": None if saved is None else {"saved_at": saved.saved_at, "expires_at": saved.expires_at,
                "request": saved.request.to_dict(), "securities": saved.securities, "research_source": saved.research_source,
                "is_fixture": saved.is_fixture, "same_quote_source": saved.quote_source is self.service._screening_source},
            "messages_used": entry.messages_used, "model_calls_forwarded": entry.model.forwarded,
            "busy": entry.control is not None, "active_id": entry.active_id,
            "records": {key: {"digest": record.digest, "retired": record.retired,
                "problem": record.problem, "has_response": record.response is not None,
                "result_code": record.response["result"]["code"] if record.response else None}
                for key, record in entry.records.items()},
            "market_contexts": entry.market_contexts, "service_active": self.service._active,
        })

    def step(self, action, index):
        if "at_seconds" in action:
            self.now = NOW + timedelta(seconds=action["at_seconds"])
        self.clock_values = [NOW + timedelta(seconds=value) for value in action.get("clock_seconds", [])]
        self.directory_mode = action.get("directory", self.directory_mode)
        self.daily.fail = action.get("daily_fail", False)
        self.technical_indicators = tuple(action.get("technical_indicators", ["SMA3"]))
        self.model.technical.position = 0
        if "screening_conditions" in action:
            self.model.screening.payload["conditions"] = action["screening_conditions"]
        if action.get("replace_quote_source"):
            self.service._screening_source = QuoteSource(self.quotes.snapshot)
            self.quote_tag = "replacement_quotes"
        self.data.on_collect = (lambda: self.control.cancel()) if action.get("cancel_on_collect") else None
        who = action.get("session", "main")
        if who not in self.credentials:
            self.credentials[who] = self.service.create_session()
        credentials = self.credentials[who]
        sid, token = credentials["session_id"], credentials["access_token"]
        entry = self.service._entries[sid]
        before = self.state(entry)
        self.events, self.executions = [], []
        counts = (len(self.model.calls), len(self.quotes.calls), len(self.data.calls), len(self.daily.reads))
        try:
            if action.get("history"):
                outcome = {"response": self.service.history(sid, token)}
            else:
                outcome = {"response": self.service.message(sid, token, action.get("request_id", f"turn-{index}"), action["question"])}
        except ApiProblem as error:
            outcome = {"problem": {"status": error.status, "code": error.code, "message": error.message}}
        observed = {"route": self.executions[0] if self.executions else None,
            "executor_chain": list(self.executions), "before": before,
            "outcome": self.normal(outcome), "after": self.state(entry),
            "events": self.normal(self.events),
            "calls": {"model_forwards": len(self.model.calls) - counts[0],
                "quote_reads": len(self.quotes.calls) - counts[1], "company_reads": len(self.data.calls) - counts[2],
                "daily_reads": len(self.daily.reads) - counts[3]}}
        code = outcome.get("problem", {}).get("code") or outcome["response"]["result"]["code"]
        if code == "research_internal_error":
            raise AssertionError(f"Step {index}: observer/fixture error: {self.errors}")
        return observed


def observe_scenario(scenario):
    with ExitStack() as stack:
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())
        stack.enter_context(patch("trading_companion.local_env._read_local_values",
                                  side_effect=AssertionError("No credentials in a routing baseline")))
        for module in ("multi_analysis", "analysis", "research_tool"):
            stack.enter_context(patch(f"trading_companion.{module}.datetime", FixedDateTime))
        original_init = TurnControl.__init__

        def fixed_control_init(control, *args, **kwargs):
            # history() creates its zero-budget control inside _admit(), bypassing
            # the injected factory. Fix that clock too, without replacing its type.
            kwargs.setdefault("clock", lambda: 10.0)
            original_init(control, *args, **kwargs)
        stack.enter_context(patch.object(TurnControl, "__init__", fixed_control_init))
        journey = Journey(stack, scenario.get("config", {}))
        return [journey.step(action, index) for index, action in enumerate(scenario["steps"], 1)]


def read_baseline(path=BASELINE):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_json(value) -> bytes:
    """Fingerprint logical JSON, including list order and explicit missing/null."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fingerprint(value) -> dict:
    content = canonical_json(value)
    return {"sha256": sha256(content).hexdigest(), "utf8_bytes": len(content)}


def _pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def _summarize(value, keys):
    return {**_pick(value, keys), "full_json": fingerprint(value)}


def project_observation(observed):
    """Keep diagnostic fields readable and seal every large value with a digest."""
    projected = deepcopy(observed)
    response = projected["outcome"].get("response")
    if response is not None:
        result = response["result"]
        summary = _summarize(result, ("status", "code", "stage", "input_mode", "parameters",
                                     "model_calls_attempted", "analyses_used"))
        if result.get("runtime") is not None:
            summary["runtime"] = _pick(result["runtime"],
                ("stop_code", "responses_received", "observed_tokens", "limits"))
        if result.get("company_research") is not None:
            summary["company_research"] = _pick(result["company_research"],
                ("securities", "daily_cutoff", "explicit_historical"))
        if result.get("screening") is not None:
            screen = result["screening"]
            summary["screening"] = {"request": screen["request"],
                "row_ids": [row["instrument_id"] for row in screen["rows"]]}
        response["result"] = summary
    for name in ("before", "after"):
        company = projected[name].get("product_saved")
        if company is not None:
            company["plan"] = _summarize(company["plan"], ("status", "code", "securities", "kinds", "as_of",
                "start_date", "end_date", "source", "explicit_historical", "objective", "indicators"))
            company["answer"] = _summarize(company["answer"], ("status", "code", "origin"))
    return projected


def project_observations(observations):
    result, previous_after = [], None
    for observed in observations:
        projected = project_observation(observed)
        if previous_after is not None and projected["before"] == previous_after:
            projected["before"] = {"$ref": "previous.after"}
        previous_after = projected["after"]
        result.append(projected)
    return result


def expand_observations(observations):
    """Resolve stored references only against expected state, never actual state."""
    result = deepcopy(observations)
    for index, observed in enumerate(result):
        if observed["before"] == {"$ref": "previous.after"}:
            if index == 0:
                raise ValueError("A first observation cannot reference previous.after")
            observed["before"] = deepcopy(result[index - 1]["after"])
    return result


def project_corpus(corpus, full_capture_bytes):
    """Derive a compact baseline from frozen data without running any executor."""
    if corpus["metadata"].get("projection_version") is not None:
        raise ValueError("Project the original full capture, not an existing projection")
    if json.loads(full_capture_bytes) != corpus:
        raise ValueError("The provenance bytes must describe this exact full capture")
    projected = deepcopy(corpus)
    metadata = projected["metadata"]
    metadata.update(schema_version=2, projection_version=PROJECTION_VERSION,
        projection_observer_sha256=observer_digest(Path(__file__).read_bytes()),
        projection_derived_from={"kind": "deterministic_projection_of_full_capture",
            "sha256": sha256(full_capture_bytes).hexdigest(), "utf8_bytes": len(full_capture_bytes),
            "capture_observer_sha256": corpus["metadata"]["observer_sha256"],
            "capture_command_at_capture": corpus["metadata"]["capture_command"]},
        projection_policy="Large result/plan/answer: SHA-256 and byte length of canonical UTF-8 JSON "
            "(sorted keys, compact separators, no NaN). Key fields/state/events stay readable. "
            "Identical before state references previous.after. Fingerprints detect differences; "
            "a failed comparison writes the new full observation for diagnosis.",
        full_capture_command=".\\.venv\\Scripts\\python.exe -X utf8 -B -m tests.routing_baseline_support --capture "
            "--input tests/fixtures/request-routing-v1.json --output tests/fixtures/request-routing-v2.json "
            "--full-output .agentbus/tmp/request-routing-v2-full.json "
            "--source-commit <full-checked-out-commit> --corpus-version request-routing-v2")
    for scenario in projected["scenarios"]:
        scenario["observed"] = project_observations(scenario["observed"])
    return projected


def serialize_corpus(corpus) -> bytes:
    # One compact scenario per line keeps future diffs bounded and reviewable.
    return (b'{"metadata":' + canonical_json(corpus["metadata"]) + b',"scenarios":[\n'
            + b",\n".join(canonical_json(scenario) for scenario in corpus["scenarios"]) + b"\n]}\n")


def _write_diagnostic(scenario_id, step, full_observation, expected, actual, directory):
    parent = Path(directory) if directory is not None else ROOT / ".agentbus/tmp"
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="routing-baseline-", dir=parent))
    path = folder / f"step-{step}.json"
    payload = {"scenario": scenario_id, "step": step, "expected_projection": expected,
               "actual_projection": actual, "actual_full_observation": full_observation}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def compare_observations(scenario, full_observations, *, diagnostic_dir=None):
    expected = expand_observations(scenario["observed"])
    if len(full_observations) != len(expected):
        path = _write_diagnostic(scenario["id"], 0, full_observations, expected, None, diagnostic_dir)
        raise AssertionError(f"{scenario['id']}: observation count differs; full observations: {path}")
    for step, (full, frozen) in enumerate(zip(full_observations, expected, strict=True), 1):
        try:
            actual = project_observation(full)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            path = _write_diagnostic(scenario["id"], step, full, frozen, None, diagnostic_dir)
            raise AssertionError(f"{scenario['id']} step {step}: cannot project changed result shape; "
                                 f"full observation: {path}") from error
        if actual != frozen:
            path = _write_diagnostic(scenario["id"], step, full, frozen, actual, diagnostic_dir)
            fields = sorted(key for key in frozen.keys() | actual.keys()
                            if key not in frozen or key not in actual or frozen[key] != actual[key])
            raise AssertionError(f"{scenario['id']} step {step}: differing fields {fields}; "
                f"route {frozen.get('route')} -> {actual.get('route')}; full observation: {path}")


def observer_digest(content: bytes) -> str:
    """Git may check text out with CRLF on Windows; hash the same logical source."""
    return sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def validate_expectations(scenario, observations):
    for index, (action, observed) in enumerate(zip(scenario["steps"], observations, strict=True), 1):
        if observed["route"] != action["expected_route"]:
            raise AssertionError(f"{scenario['id']} step {index}: expected route {action['expected_route']}, observed {observed['route']}")
        outcome = observed["outcome"]
        code = outcome.get("problem", {}).get("code") or outcome["response"]["result"]["code"]
        if "expected_code" in action and code != action["expected_code"]:
            raise AssertionError(f"{scenario['id']} step {index}: expected {action['expected_code']}, observed {code}")


def capture(args):
    """Explicit, no-overwrite capture on the named clean code revision only."""
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if source != args.source_commit:
        raise ValueError("Capture requires the exact --source-commit checked out")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "src", "scripts", "tests"],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    full_output = args.full_output
    if Path(args.output).exists() or full_output is not None and full_output.exists():
        raise ValueError("Capture never overwrites a baseline; use a new version/output")
    if full_output is not None and full_output.resolve() == Path(args.output).resolve():
        raise ValueError("Full and projected outputs must be separate files")
    corpus = read_baseline(args.input)
    for key in tuple(corpus["metadata"]):
        if key.startswith("projection_") or key == "full_capture_command":
            del corpus["metadata"][key]
    corpus["metadata"].update(code_revision=source, corpus_version=args.corpus_version,
        schema_version=1, observer_version=OBSERVER_VERSION,
        observer_hash_format="sha256_utf8_normalized_lf",
        observer_sha256=observer_digest(Path(__file__).read_bytes()))
    for scenario in corpus["scenarios"]:
        try:
            scenario["observed"] = observe_scenario(scenario)
            validate_expectations(scenario, scenario["observed"])
        except Exception as error:
            raise AssertionError(f"Capture failed: {scenario['id']}") from error
    full_bytes = (json.dumps(corpus, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if full_output is not None:
        full_output.write_bytes(full_bytes)
    Path(args.output).write_bytes(serialize_corpus(project_corpus(corpus, full_bytes)))
    print(f"Captured {len(corpus['scenarios'])} scenarios / {sum(len(s['steps']) for s in corpus['scenarios'])} turns from {source}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", required=True)
    parser.add_argument("--input", type=Path, default=BASELINE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-output", type=Path, help="Optional full capture for diagnosis; never committed")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--corpus-version", required=True)
    capture(parser.parse_args())


if __name__ == "__main__":
    main()
