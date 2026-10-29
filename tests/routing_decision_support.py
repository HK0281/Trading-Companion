"""New decision targets and full observations, without rewriting the v1/v2 observers."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

from tests import routing_baseline_support as legacy
from tests.routing_targets_support import observe_target_scenario, baseline_digest
from tests.test_decision_routing_api import RoutingDecisionModel

TARGET = legacy.ROOT / "tests/fixtures/request-routing-v3.json"
SOURCES = (
    "src/trading_companion/research_decision.py", "src/trading_companion/trade_plan.py",
    "src/trading_companion/product_decision_answer.py", "src/trading_companion/product_plan.py",
    "src/trading_companion/product_research.py", "src/trading_companion/product_agent.py",
    "src/trading_companion/product_answer.py", "src/trading_companion/request_routing.py",
    "src/trading_companion/research_objective.py", "src/trading_companion/decision_demo.py",
    "tests/routing_decision_support.py", "tests/test_decision_routing_api.py",
    "tests/test_product_decision_answer.py", "tests/test_product_agent.py",
)

# Handwritten from #149/#150 before capture. Calls mean model forwards / daily reads.
SCENARIOS = (
    ("decision-direct-and-history", (
        ("平安银行现在能买吗", "company_research_completed", "product-research-answer-v2", 2, 1),
        ("查看上次结果", "product_saved_result", "product-research-answer-v2", 0, 0))),
    ("decision-paraphrases", (
        ("分析平安银行的基本面", "company_research_completed", "product-research-answer-v1", 2, 0),
        ("现在这个价位值得购买吗", "company_research_completed", "product-research-answer-v2", 2, 1),
        ("那现在能买吗", "company_research_completed", "product-research-answer-v2", 2, 1),
        ("它后面怎么走", "company_research_completed", "product-research-answer-v2", 2, 1))),
    ("decision-identity-boundary", (
        ("那现在能买吗", "decision_instrument_required", None, 0, 0),
        ("平安银行现在能买吗", "company_research_completed", "product-research-answer-v2", 2, 1),
        ("新问题：现在能买吗", "decision_instrument_required", None, 0, 0))),
    ("decision-domain-limits", (
        ("只看平安银行新闻，现在能买吗", "company_research_completed", "product-research-answer-v2", 2, 0),
        ("平安银行现在能买吗，不要ATR", "company_research_completed", "product-research-answer-v2", 2, 1),
        ("平安银行现在能买吗，不要交易计划", "company_research_completed", "product-research-answer-v2", 2, 1))),
)


def authored_scenarios():
    return [dict(id=name, steps=[{"question": s[0]} for s in steps], contracts=[
        dict(code=s[1], version=s[2], model_forwards=s[3], daily_reads=s[4]) for s in steps]) for name, steps in SCENARIOS]


def observe(scenario):
    original = legacy.Journey.__init__
    def initialize(journey, stack, config):
        original(journey, stack, config)
        journey.model.company = RoutingDecisionModel()
    with patch.object(legacy.Journey, "__init__", initialize):
        return observe_target_scenario(scenario)[0]


def validate(scenario, observations):
    for contract, observed in zip(scenario["contracts"], observations, strict=True):
        result = observed["outcome"]["response"]["result"]
        actual = dict(code=result["code"], version=(result.get("company_research") or {}).get("version"),
                      model_forwards=observed["calls"]["model_forwards"], daily_reads=observed["calls"]["daily_reads"])
        if actual != contract:
            raise AssertionError(f"{scenario['id']}: {actual} != authored target {contract}")


def capture(path):
    if path.exists():
        raise ValueError("Refusing to overwrite a candidate")
    scenarios = authored_scenarios()
    for scenario in scenarios:
        observed = observe(scenario)
        validate(scenario, observed)
        scenario["observed"] = legacy.project_observations(observed)
    payload = dict(metadata=dict(corpus_version="request-routing-v3", source_kind="uncommitted_worktree_candidate",
        base_revision="b714276", v2_sha256=baseline_digest((legacy.ROOT/"tests/fixtures/request-routing-v2.json").read_bytes()),
        old_scenarios=35, old_turns=105, old_differences_expected=0,
        source_sha256_lf={name:sha256((legacy.ROOT/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                          for name in SOURCES}), scenarios=scenarios)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    capture(parser.parse_args().output)
