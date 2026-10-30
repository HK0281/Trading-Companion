import json
from hashlib import sha256
import unittest

from tests.routing_baseline_support import compare_observations, ROOT
from tests.routing_targets_support import baseline_digest
from tests.routing_decision_support import TARGET, SOURCES, authored_scenarios, observe, validate
from tests.routing_narrative_support import current_decision_contracts, corpus as narrative_corpus
from tests.routing_horizon_support import overlay, validate_current_decisions
from trading_companion.intent_rule_baseline import current_baseline


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
            "src/trading_companion/product_plan.py",  # A2 #176: literal identity completion; historical hash checked below.
            "src/trading_companion/product_agent.py",  # A2 #176: same normalized name scan at the admission gate.
            "tests/test_decision_routing_api.py", "tests/test_product_decision_answer.py", "tests/test_product_agent.py"}]
        for name in frozen:
            registered = current_baseline()['sources_sha256_lf'].get(name.rsplit('/',1)[-1],
                corpus["metadata"]["source_sha256_lf"][name])
            self.assertEqual(registered,
                             sha256((ROOT/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest(), name)
        a2 = json.loads((ROOT/'tests/fixtures/request-routing-a2-deltas.json').read_text(encoding='utf-8'))
        self.assertEqual(a2['parent_v4_sha256_lf'],baseline_digest((ROOT/'tests/fixtures/request-routing-v4.json').read_bytes()))
        for name,record in a2['source_changes'].items():
            self.assertEqual(corpus['metadata']['source_sha256_lf'][name],record['historical_v3_sha256_lf'])
            from trading_companion.intent_router_task import SOURCE_REVISIONS
            self.assertEqual(SOURCE_REVISIONS[name.rsplit('/',1)[-1]][1],record['current_sha256_lf'])
        self.assertEqual(set(a2['source_changes']),{'src/trading_companion/product_plan.py','src/trading_companion/product_agent.py'})
        self.assertEqual(a2['version'],'request-routing-a2-deltas-v1')
        old = json.loads((ROOT/'tests/fixtures/request-routing-v2.json').read_text(encoding='utf-8'))
        from tests.routing_baseline_support import expand_observations
        expected = [{'id':s['id'],'turns':[{'index':i,'fields':[]} for i in range(1,len(expand_observations(s['observed']))+1)]}
                    for s in old['scenarios']+corpus['scenarios']]
        self.assertEqual(a2['scenarios'],expected)
        self.assertEqual(sum(len(s['turns']) for s in expected),117)
        targets = authored_scenarios()
        self.assertEqual([{k:v for k,v in s.items() if k != "observed"} for s in corpus["scenarios"]], targets)
        for scenario in corpus["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                observed = observe(scenario)
                validate_current_decisions(scenario, observed)
                compare_observations({**scenario, "observed": overlay(scenario["id"], scenario["observed"])}, observed)
