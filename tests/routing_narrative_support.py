"""Reviewed v4 field overlay; v1/v2/v3 captures and routing contracts stay immutable."""
from copy import deepcopy
import json
from pathlib import Path

from tests.routing_baseline_support import ROOT, expand_observations
from tests.routing_targets_support import baseline_digest

TARGET = ROOT / "tests/fixtures/request-routing-v4.json"

# Authored semantic scope: A changes company answer serialization, never routes or reads.
# Before-state propagation of a changed saved answer is an expected change too.
CHANGED_TURNS = {
    "edits-refresh-sample": (4,), "edit-company-lookalikes": (2, 3), "ordinal-selections": (3,),
    "named-time-ordinals": (2, 3, 6), "company-pending-owns-replies": (6,),
    "company-technical-capabilities": (1, 2, 3, 4), "current-pronoun-versus-list": (2, 3, 4),
    "company-followups-and-history": (1, 2, 3, 4, 5), "lazy-clock-named-member": (2,),
    "selected-technical-followup": (4,), "failed-research-consumes-current": (1, 2),
    "decision-direct-and-history": (1, 2), "decision-paraphrases": (1, 2, 3, 4),
    "decision-identity-boundary": (2, 3), "decision-domain-limits": (1, 2, 3),
}
ALLOWED_PATHS = frozenset(
    ["/outcome/response/result/full_json/" + key for key in ("sha256", "utf8_bytes")]
    + [f"/{when}/product_saved/answer/{suffix}" for when in ("before", "after")
       for suffix in ("code", "full_json/sha256", "full_json/utf8_bytes")])


def validate_delta(fields):
    for field in fields:
        if field["path"] not in ALLOWED_PATHS:
            raise AssertionError("A batch changed routing/state outside the authored answer scope: " + field["path"])
        if field["path"].endswith("/code") and (field["before"], field["after"]) != ("product_research_answered", "answer_ready"):
            raise AssertionError("Unexpected answer acceptance/status change")


def corpus():
    value = json.loads(TARGET.read_text(encoding="utf-8"))
    for name, digest in value["metadata"]["parents_sha256_lf"].items():
        if baseline_digest((ROOT/"tests/fixtures"/name).read_bytes()) != digest:
            raise AssertionError("Historical routing fixture changed: " + name)
    return value


def overlay(identifier, expected):
    """Validate every before value, then apply explicit fields to historical expectations."""
    scenario = next(s for s in corpus()["scenarios"] if s["id"] == identifier)
    output = expand_observations(expected)
    if len(output) != len(scenario["turns"]):
        raise AssertionError("Scenario length changed")
    changed = []
    for index, (observation, turn) in enumerate(zip(output, scenario["turns"], strict=True), 1):
        fields = turn["fields"]
        validate_delta(fields)
        if fields:
            changed.append(index)
        for field in fields:
            keys = [key.replace("~1", "/").replace("~0", "~") for key in field["path"].split("/")[1:]]
            parent = observation
            for key in keys[:-1]:
                parent = parent[key]
            key = keys[-1]
            if parent[key] != field["before"]:
                raise AssertionError(f"{identifier}/{index}: delta parent value changed: {field['path']}")
            parent[key] = deepcopy(field["after"])
    if tuple(changed) != CHANGED_TURNS.get(identifier, ()):
        raise AssertionError(identifier + ": differs from authored changed-turn scope")
    return output


def current_decision_contracts(scenario):
    result = deepcopy(scenario)
    for contract in result["contracts"]:
        if contract["version"] is not None:
            contract["version"] = "product-research-answer-v3"
    return result
