import json
from hashlib import sha256
import unittest

from tests.routing_baseline_support import compare_observations, ROOT
from tests.routing_targets_support import baseline_digest
from tests.routing_decision_support import TARGET, SOURCES, authored_scenarios, observe, validate


class DecisionRoutingBaselineTests(unittest.TestCase):
    def test_new_targets_and_full_observations_are_frozen_independently(self):
        corpus = json.loads(TARGET.read_text(encoding="utf-8"))
        self.assertEqual(corpus["metadata"]["v2_sha256"], baseline_digest((ROOT/"tests/fixtures/request-routing-v2.json").read_bytes()))
        self.assertEqual(corpus["metadata"]["old_differences_expected"], 0)
        self.assertEqual(corpus["metadata"]["source_sha256_lf"], {name:sha256((ROOT/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                                                               for name in SOURCES})
        targets = authored_scenarios()
        self.assertEqual([{k:v for k,v in s.items() if k != "observed"} for s in corpus["scenarios"]], targets)
        for scenario in corpus["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                observed = observe(scenario)
                validate(scenario, observed)
                compare_observations(scenario, observed)
