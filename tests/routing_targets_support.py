"""Independent target contracts plus explicit, no-overwrite worktree observation.

The v1 observer and data are immutable. This layer adds semantic decisions and the
new local executor without rewriting their historical hashes or capture policy.
"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from tests import routing_baseline_support as legacy


TARGET = legacy.ROOT / "tests/fixtures/request-routing-v2.json"
BASE_REVISION = "03bfe1c62c6d5440e9071e905b5d53e85d224578"
BASELINE_HASH_FORMAT = "sha256_utf8_normalized_lf"
# Authored from the reviewed policy and old inputs, not from the new router.
# K=general knowledge, I=fixed indicator definition, S=new screen, E=screen edit,
# L=list follow-up, C=company research, T=technical research, P=company pending,
# A=technical pending, D=indicator pending, H=history, X=command, O=disabled,
# -=directory admission failure or idempotent replay (no decision).
INTENTS = {
    "knowledge-ready": "K K", "knowledge-directory-missing": "K", "knowledge-stale": "K C",
    "workflow-off": "O I", "workflow-off-fixed": "I", "fixed-knowledge": "I I",
    "fixed-knowledge-directory-gate": "-", "mixed-named-directory-gate": "- -",
    "screen-new-and-replace": "S C S", "screen-explicit-interval": "S", "screen-no-source": "S",
    "edits-no-list": "E E E", "edits-refresh-sample": "S E E L", "edits-empty-list": "S E E",
    "edit-company-lookalikes": "S C C", "ordinal-no-list": "L", "ordinal-selections": "S L L",
    "named-time-ordinals": "C C S C S C", "company-pending-owns-replies": "S C P P I P",
    "technical-pending": "S T A I A", "indicator-pending": "T T I D",
    "company-technical-capabilities": "C T C C", "company-disabled": "T S L",
    "knowledge-does-not-renew-list": "S K H L", "current-pronoun-versus-list": "S L C L",
    "company-followups-and-history": "C C C H H", "technical-edit-and-commands": "T T X H X",
    "replay-and-isolation": "S - L E", "source-change": "S E",
    "lazy-clock-explicit-ordinal": "S L", "lazy-clock-named-member": "S C",
    "selected-technical-followup": "S L T L", "failed-research-consumes-current": "C T T H",
    "cancelled-selection": "S L L", "development-mode": "I K S",
}
MEANINGS = {
    "K": ("general_knowledge", "general_knowledge", "knowledge.general"),
    "I": ("indicator_knowledge", "technical_research", "knowledge.indicator"),
    "S": ("screening", "screening", "screening.new"),
    "E": ("screening_edit", "screening_edit", "screening.edit"),
    "L": ("screening_followup", "screening_followup", "screening.followup"),
    "C": ("company_research", "company_research", "research.company"),
    "T": ("technical_research", "technical_research", "research.technical"),
    "P": ("company_clarification", "company_research", "clarification.company"),
    "A": ("technical_clarification", "technical_research", "clarification.technical"),
    "D": ("indicator_clarification", "technical_research", "clarification.indicator"),
    "H": ("history", None, "session.history"), "X": ("command", None, "session.command"),
    "O": ("knowledge_disabled", "local_reply", "knowledge.disabled"),
}
# Every changed observation, including later cache/state effects, needs a reason.
CHANGES = {
    ("workflow-off", 1): "B4", ("workflow-off", 2): "B4",
    ("company-pending-owns-replies", 5): "B1", ("company-pending-owns-replies", 6): "B1",
    ("technical-pending", 5): "B2",
}
OVERRIDES = {
    ("workflow-off", 1): ("local_reply", "general_knowledge_disabled", (0, 0, 0, 0)),
    ("workflow-off", 2): ("technical_research", "indicator_knowledge_answered", (0, 0, 0, 0)),
    ("company-pending-owns-replies", 5): ("technical_research", "indicator_knowledge_answered", (0, 0, 0, 0)),
    ("technical-pending", 5): ("technical_research", "research_completed", (2, 0, 0, 1)),
}
CALL_KEYS = ("model_forwards", "quote_reads", "company_reads", "daily_reads")
SOURCE_FILES = (
    "src/trading_companion/request_routing.py", "src/trading_companion/api_service.py",
    "src/trading_companion/agent.py", "tests/routing_baseline_support.py",
    "tests/routing_targets_support.py", "tests/test_routing_baseline.py",
    "tests/test_request_routing.py", "tests/test_routing_conflicts_api.py",
)


def observe_target_scenario(scenario):
    """Wrap, do not edit, the original observer. No candidate scans or extra IO."""
    from trading_companion import api_service
    decisions, all_decisions = [], []
    original_init, original_step = legacy.Journey.__init__, legacy.Journey.step

    def initialize(journey, stack, config):
        original_init(journey, stack, config)
        stack.enter_context(patch.object(api_service, "run_routing_reply_turn",
            journey.executor(api_service.run_routing_reply_turn, "local_reply")))

    def decide(original):
        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                decisions.append({key: value.value if hasattr(value, "value") else value
                                  for key, value in asdict(result).items()})
            return result
        return wrapped

    def step(journey, action, index):
        decisions.clear()
        observed = original_step(journey, action, index)
        if len(decisions) > 1:
            raise AssertionError("Only one winning decision per turn")
        all_decisions.append(decisions[0] if decisions else None)
        return observed

    with ExitStack() as stack:
        stack.enter_context(patch.object(legacy.Journey, "__init__", initialize))
        stack.enter_context(patch.object(legacy.Journey, "step", step))
        for name in ("route_research_request", "resolve_general_knowledge"):
            stack.enter_context(patch.object(api_service, name, decide(getattr(api_service, name))))
        observations = legacy.observe_scenario(scenario)
    return observations, all_decisions


def contracts_for(scenario):
    """Old facts are defaults; only reviewed B1/B2/B4 outcomes change in this corpus."""
    contracts = []
    old = legacy.expand_observations(scenario["observed"])
    tokens = INTENTS[scenario["id"]].split()
    if len(tokens) != len(scenario["steps"]):
        raise AssertionError(f"Handwritten intents have wrong length: {scenario['id']}")
    for index, (token, observed) in enumerate(zip(tokens, old, strict=True), 1):
        result = observed["outcome"].get("response", {}).get("result", {})
        route = observed["route"]
        code = result.get("code", observed["outcome"].get("problem", {}).get("code"))
        calls = observed["calls"]
        if (scenario["id"], index) in OVERRIDES:
            route, code, counts = OVERRIDES[scenario["id"], index]
            calls = dict(zip(CALL_KEYS, counts, strict=True))
        decision = None
        if token != "-":
            intent, kind, reason = MEANINGS[token]
            decision = {"intent": intent, "kind": kind or route, "rule_id": reason}
        contracts.append({"route": route, "code": code, "calls": calls, "decision": decision})
    return contracts


def validate_contracts(scenario, observations, decisions):
    if len(observations) != len(scenario["contracts"]) or len(decisions) != len(observations):
        raise AssertionError("Target observation count changed")
    for index, (contract, observed, decision) in enumerate(zip(scenario["contracts"], observations, decisions, strict=True), 1):
        result = observed["outcome"].get("response", {}).get("result", {})
        code = result.get("code", observed["outcome"].get("problem", {}).get("code"))
        actual = {"route": observed["route"], "code": code, "calls": observed["calls"], "decision": decision}
        if actual != contract:
            raise AssertionError(f"{scenario['id']} step {index}: independent target contract differs: {actual} != {contract}")
        if decision and decision["intent"] in ("indicator_knowledge", "general_knowledge", "knowledge_disabled"):
            for field in ("pending", "pending_edit", "product_pending", "screening_saved"):
                if observed["before"][field] != observed["after"][field]:
                    raise AssertionError(f"{scenario['id']} step {index}: knowledge changed {field}")
        if (scenario["id"], index) == ("technical-pending", 5):
            before = observed["before"]["pending"]
            after = observed["after"]["technical"]["confirmed"]
            conditions = deepcopy(before["conditions"])
            conditions["indicators"] = [{"kind": item["kind"], **dict(item["_parameters"])}
                                        for item in conditions["indicators"]]
            if after["conditions"] != conditions:
                raise AssertionError("B2 lost original technical conditions")
            parameters = result["parameters"]
            if parameters["instrument_id"] != "000001.SZ" or parameters["indicators"] != [{"kind": "sma", "window": 3}]:
                raise AssertionError("B2 expanded the original security/indicator scope")


def field_deltas(old, new, path=""):
    """Exhaustive dictionary paths. Lists are whole ordered values, never filtered."""
    if isinstance(old, dict) and isinstance(new, dict):
        result = []
        for key in sorted(old.keys() | new.keys()):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in old or key not in new:
                result.append({"path": pointer, "before": {"present": key in old, "value": old.get(key)},
                               "after": {"present": key in new, "value": new.get(key)}})
            else:
                result.extend(field_deltas(old[key], new[key], pointer))
        return result
    return [] if old == new else [{"path": path, "before": old, "after": new}]


def source_manifest():
    return {name: legacy.observer_digest((legacy.ROOT / name).read_bytes()) for name in SOURCE_FILES}


def baseline_digest(content: bytes) -> str:
    """Hash UTF-8 fixture bytes with checkout CRLF normalized to LF, like source hashes."""
    return legacy.observer_digest(content)


def propose(output, full_output):
    """Explicit candidate only; never claims the dirty source is a clean commit."""
    output, full_output = Path(output), Path(full_output)
    if output.exists() or full_output.exists() or output.resolve() == full_output.resolve():
        raise ValueError("Candidate output files must be new and distinct")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=legacy.ROOT, text=True).strip()
    if revision != BASE_REVISION:
        raise ValueError("This reviewed proposal is tied to the 1a base revision")
    corpus = legacy.read_baseline()
    candidate = {"metadata": {"schema_version": 2, "corpus_version": "request-routing-v2",
        "source_kind": "uncommitted_worktree_candidate", "base_revision": revision,
        "source_sha256_lf": source_manifest(), "v1_sha256": baseline_digest(legacy.BASELINE.read_bytes()),
        "v1_hash_format": BASELINE_HASH_FORMAT,
        "projection_version": legacy.PROJECTION_VERSION,
        "contracts": "Handwritten policy intents and B1/B2/B4 overrides; unchanged expectations from frozen v1.",
        "capture_command": ".\\.venv\\Scripts\\python.exe -X utf8 -B -m tests.routing_targets_support --propose --output <new-file> --full-output <new-full-file>"},
        "scenarios": []}
    full = []
    for old in corpus["scenarios"]:
        scenario = {key: deepcopy(old[key]) for key in ("id", "config", "steps") if key in old}
        for action in scenario["steps"]:
            action.pop("expected_route", None)
            action.pop("expected_code", None)
        scenario["contracts"] = contracts_for(old)
        observed, decisions = observe_target_scenario(scenario)
        validate_contracts(scenario, observed, decisions)
        projected = [legacy.project_observation(value) for value in observed]
        previous = legacy.expand_observations(old["observed"])
        differences = []
        for index, (before, after) in enumerate(zip(previous, projected, strict=True), 1):
            delta = field_deltas(before, after)
            reason = CHANGES.get((old["id"], index))
            if bool(delta) != bool(reason):
                raise AssertionError(f"Unreviewed or missing difference: {old['id']} step {index}; paths: {[item['path'] for item in delta]}")
            differences.append({"behavior": reason, "fields": delta})
        scenario["observed"] = legacy.project_observations(observed)
        scenario["deltas"] = differences
        candidate["scenarios"].append(scenario)
        full.append({"id": scenario["id"], "observed": observed, "decisions": decisions})
    full_output.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.write_bytes(legacy.serialize_corpus(candidate))
    print(f"Proposed {len(full)} scenarios / {sum(len(s['observed']) for s in full)} turns; not a clean-commit capture")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--propose", required=True, action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--full-output", required=True, type=Path)
    arguments = parser.parse_args()
    propose(arguments.output, arguments.full_output)
