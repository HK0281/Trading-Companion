"""Offline observer for the pre-integration API; capture is never automatic."""

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.verify import clean_environment, offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.turn_control import TurnControl
from tests import routing_baseline_support as old


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/research-intent-v2-off-baseline.json"


class Journey(old.Journey):
    def model_event(self, original, messages, *, tools):
        self.events.append({"event": "model_request",
            "request": old.fingerprint(self.normal({
                "messages": [m.to_dict() for m in messages],
                "tools": [t.to_dict() for t in tools]}))})
        if self.config.get("index"):
            from trading_companion.decision_demo import DecisionDemoModel
            self.events.append({"event": "model_forward", "tools": [t.name for t in tools]})
            self.model.calls.append((tuple(messages), tuple(tools)))
            return DecisionDemoModel().complete(messages, tools=tools)
        return super().model_event(original, messages, tools=tools)

    def state(self, entry):
        result = super().state(entry)
        saved = entry.session.index_saved
        pending = entry.session.index_pending
        result["index"] = self.normal({
            "pending": None if pending is None else {
                "stage": pending.stage, "instrument_id": pending.instrument_id,
                "created_at": pending.created_at,
                "base": pending.base},
            "saved": None if saved is None else {
                "request": saved.result.request, "saved_at": saved.saved_at},
            "digests": [d.to_dict() for d in entry.digests]})
        return result


def scenarios():
    result = deepcopy(old.read_baseline(ROOT / "tests/fixtures/request-routing-v2.json")["scenarios"])
    # The original observer predates indices; the complete output and model
    # request fingerprints still observe this route without inventing an owner.
    result += [
        dict(id="intent-index", config=dict(development=True, index=True), steps=[
            dict(question="研究上证指数", expected_route=None),
            dict(question="查看上次结果", expected_route=None)]),
        dict(id="intent-index-horizon", config=dict(development=True, index=True), steps=[
            dict(question="上证指数最近一个月止盈止损", expected_route=None),
            dict(question="中线", expected_route=None),
            dict(question="不要价位，只研究上证指数", expected_route=None)]),
        dict(id="intent-memory-recall", config=dict(memory=True), steps=[
            dict(question="分析平安银行", expected_route="company_research"),
            dict(question="回顾我们刚才聊了什么", expected_route=None),
            dict(question="查看上次结果", history=True, expected_route="company_research")]),
    ]
    for scenario in result:
        scenario.pop("observed", None)
    return result


def observe(scenario, **service_options):
    with ExitStack() as stack:
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())
        stack.enter_context(patch("trading_companion.local_env._read_local_values",
                                  side_effect=AssertionError("No credentials")))
        for module in ("multi_analysis", "analysis", "research_tool"):
            stack.enter_context(patch(f"trading_companion.{module}.datetime", old.FixedDateTime))
        original_init = TurnControl.__init__

        def init(control, *args, **kwargs):
            kwargs.setdefault("clock", lambda: 10.0)
            original_init(control, *args, **kwargs)
        stack.enter_context(patch.object(TurnControl, "__init__", init))

        def service(**kwargs):
            kwargs["conversation_context"] = scenario.get("config", {}).get("memory", False)
            return ResearchApiService(**kwargs, **service_options)
        stack.enter_context(patch.object(old, "ResearchApiService", service))
        journey = Journey(stack, scenario.get("config", {}))
        observed = [journey.step(action, i) for i, action in enumerate(scenario["steps"], 1)]
        for action, actual in zip(scenario["steps"], observed, strict=True):
            if "expected_route" in action and actual["route"] != action["expected_route"]:
                raise AssertionError((scenario["id"], action["question"], actual["route"]))
        return observed


def capture(revision):
    """Run only before production edits, against a manually verified revision."""
    import subprocess
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if actual != revision or not revision.startswith("a113238"):
        raise ValueError("Capture requires the reviewed a113238 revision")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "src", "scripts", "tests"],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    if BASELINE.exists():
        raise ValueError("Never overwrite a baseline")
    rows = scenarios()
    for row in rows:
        row["observed"] = old.project_observations(observe(row))
    payload = dict(metadata=dict(code_revision=actual, kind="intent-integration-off-baseline",
        observer_sha256=old.observer_digest(Path(__file__).read_bytes()),
        parent_observer_sha256=old.observer_digest(Path(old.__file__).read_bytes()),
        capture_command=".\\.venv\\Scripts\\python.exe -X utf8 -B -c "
            "\"from tests.intent_integration_support import capture; capture('<full-a113238-revision>')\"",
        normalization="Existing routing observer: stable UUID mapping; fixed clocks; full output and request digests",
        paid_calls=0), scenarios=rows)
    BASELINE.write_bytes(old.serialize_corpus(payload))
    return dict(scenarios=len(rows), turns=sum(len(r["steps"]) for r in rows))
