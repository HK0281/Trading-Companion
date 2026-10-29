import json
from hashlib import sha256
import unittest

from tests.routing_baseline_support import compare_observations, ROOT
from tests.routing_targets_support import baseline_digest
from tests.routing_decision_support import TARGET, SOURCES, authored_scenarios, observe, validate
from tests.routing_narrative_support import overlay, current_decision_contracts, corpus as narrative_corpus


class DecisionRoutingBaselineTests(unittest.TestCase):
    def test_new_targets_and_full_observations_are_frozen_independently(self):
        corpus = json.loads(TARGET.read_text(encoding="utf-8"))
        self.assertEqual(corpus["metadata"]["v2_sha256"], baseline_digest((ROOT/"tests/fixtures/request-routing-v2.json").read_bytes()))
        self.assertEqual(corpus["metadata"]["old_differences_expected"], 0)
        # v3 is a historical capture. v4 seals that capture and records A's explicit output deltas.
        self.assertIn(TARGET.name, narrative_corpus()["metadata"]["parents_sha256_lf"])
        frozen = [name for name in SOURCES if name not in {
            "src/trading_companion/product_research.py", "src/trading_companion/product_answer.py",
            "src/trading_companion/decision_demo.py",  # A review #168: complete demo; legacy script unchanged.
            "tests/test_decision_routing_api.py", "tests/test_product_decision_answer.py", "tests/test_product_agent.py"}]
        for name in frozen:
            self.assertEqual(corpus["metadata"]["source_sha256_lf"][name],
                             sha256((ROOT/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest(), name)
        targets = authored_scenarios()
        self.assertEqual([{k:v for k,v in s.items() if k != "observed"} for s in corpus["scenarios"]], targets)
        for scenario in corpus["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                observed = observe(scenario)
                validate(current_decision_contracts(scenario), observed)
                compare_observations({**scenario, "observed": overlay(scenario["id"], scenario["observed"])}, observed)
