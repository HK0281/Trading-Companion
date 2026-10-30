"""Golden master captured before changing production routing; never update here."""

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.routing_baseline_support import (
    BASELINE, ROOT, Normalizer, OBSERVER_VERSION, PROJECTION_VERSION, capture, compare_observations,
    expand_observations, fingerprint, observe_scenario, observer_digest, project_observation,
    project_observations, read_baseline, validate_expectations,
)
from tests.routing_targets_support import (
    BASELINE_HASH_FORMAT, BASE_REVISION, CHANGES, TARGET, baseline_digest, contracts_for,
    field_deltas, observe_target_scenario, validate_contracts,
)
from tests.routing_horizon_support import overlay
from tests.routing_technical_support import validate_current_contracts


class RoutingBaselineTests(unittest.TestCase):
    def test_v7_preserves_all_original_messages_and_exact_parent_field_changes(self):
        from tests.routing_horizon_support import corpus, overlay
        from tests.routing_relaxed_support import overlay as parent_overlay
        originals = read_baseline(TARGET)['scenarios'] + read_baseline(ROOT/'tests/fixtures/request-routing-v3.json')['scenarios']
        delta = corpus()
        self.assertEqual([s['id'] for s in delta['scenarios']], [s['id'] for s in originals])
        self.assertEqual(sum(len(s['turns']) for s in delta['scenarios']),117)
        for scenario, changes in zip(originals,delta['scenarios'],strict=True):
            before = parent_overlay(scenario['id'],scenario['observed'])
            after = overlay(scenario['id'],scenario['observed'])
            self.assertEqual(len(after),len(scenario['steps']))
            for index,(old,new,turn) in enumerate(zip(before,after,changes['turns'],strict=True),1):
                self.assertEqual(turn['index'],index)
                self.assertEqual(turn['fields'],field_deltas(old,new))

    def test_v6_seals_all_parents_and_every_field_delta_across_117_original_turns(self):
        from tests.routing_technical_support import overlay
        from tests.routing_technical_support import corpus as technical_corpus
        from tests.routing_b_support import overlay as parent_overlay
        delta = technical_corpus()
        originals = read_baseline(TARGET)['scenarios'] + read_baseline(ROOT/'tests/fixtures/request-routing-v3.json')['scenarios']
        self.assertEqual([s['id'] for s in delta['scenarios']], [s['id'] for s in originals])
        self.assertEqual(sum(len(s['turns']) for s in delta['scenarios']), 117)
        for name, digest in delta['metadata']['observer_sha256_lf'].items():
            self.assertEqual(baseline_digest((ROOT/'tests'/name).read_bytes()), digest)
        for scenario, changes in zip(originals, delta['scenarios'], strict=True):
            before = parent_overlay(scenario['id'], scenario['observed'])
            after = overlay(scenario['id'], scenario['observed'])
            self.assertEqual(len(changes['turns']), len(scenario['steps']))
            for index, (a, b, turn) in enumerate(zip(before, after, changes['turns'], strict=True), 1):
                self.assertEqual(turn['index'], index)
                self.assertEqual(turn['fields'], field_deltas(a, b))

    def test_validation_layer_seals_all_117_turns_without_changing_routes_or_budgets(self):
        from tests.routing_relaxed_support import corpus, overlay
        from tests.routing_technical_support import overlay as parent_overlay
        originals = read_baseline(TARGET)['scenarios'] + read_baseline(ROOT/'tests/fixtures/request-routing-v3.json')['scenarios']
        changes = corpus()['scenarios']
        self.assertEqual([s['id'] for s in originals], [s['id'] for s in changes])
        self.assertEqual(sum(len(s['turns']) for s in changes), 117)
        for scenario, delta in zip(originals, changes, strict=True):
            before = parent_overlay(scenario['id'], scenario['observed'])
            after = overlay(scenario['id'], scenario['observed'])
            self.assertEqual(len(after), len(scenario['steps']))
            for index, (a, b, turn) in enumerate(zip(before, after, delta['turns'], strict=True), 1):
                self.assertEqual(turn['index'], index)
                self.assertEqual(turn['fields'], field_deltas(a, b))
                for key in ('route', 'executor_chain', 'calls'):
                    self.assertEqual(a[key], b[key])

    def test_frozen_legacy_fact_integrity_and_provenance(self):
        corpus = read_baseline()
        self.assertEqual(corpus["metadata"]["schema_version"], 2)
        self.assertEqual(corpus["metadata"]["observer_version"], OBSERVER_VERSION)
        self.assertEqual(corpus["metadata"]["observer_hash_format"], "sha256_utf8_normalized_lf")
        self.assertEqual(corpus["metadata"]["projection_version"], PROJECTION_VERSION)
        self.assertEqual(corpus["metadata"]["projection_observer_sha256"],
                         observer_digest(Path(__file__).with_name("routing_baseline_support.py").read_bytes()))
        origin = corpus["metadata"]["projection_derived_from"]
        self.assertEqual(origin["kind"], "deterministic_projection_of_full_capture")
        self.assertEqual(origin["sha256"], "baf85086f1ad3727891744d04d38b4a61501a6b3c924bc2d5887e20207b04243")
        self.assertEqual(origin["capture_observer_sha256"], corpus["metadata"]["observer_sha256"])
        self.assertLess(BASELINE.stat().st_size, 300_000)
        self.assertEqual(baseline_digest(BASELINE.read_bytes()),
                         "d751c6a46db4e09a8eebb14f0c07b93f5d9cb6a12024b97dfaff3ced229eee6b")
        self.assertEqual(corpus["metadata"]["code_revision"], "09cd36682d4cd3f9dbcb66251ac5ec3dfe0aa6e3")
        identifiers = [scenario["id"] for scenario in corpus["scenarios"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertGreaterEqual(len(identifiers), 30)
        for scenario in corpus["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                validate_expectations(scenario, scenario["observed"])

    def test_real_entry_matches_independent_v2_targets_and_every_v1_delta(self):
        original, target = read_baseline(), read_baseline(TARGET)
        self.assertEqual(target["metadata"]["base_revision"], BASE_REVISION)
        self.assertEqual(target["metadata"]["source_kind"], "uncommitted_worktree_candidate")
        self.assertEqual(target["metadata"]["v1_hash_format"], BASELINE_HASH_FORMAT)
        self.assertEqual(target["metadata"]["v1_hash_format"], "sha256_utf8_normalized_lf")
        self.assertEqual(target["metadata"]["v1_sha256"], baseline_digest(BASELINE.read_bytes()))
        self.assertEqual(target["metadata"]["source_sha256_lf"]["tests/routing_baseline_support.py"],
                         observer_digest(Path(__file__).with_name("routing_baseline_support.py").read_bytes()))
        self.assertEqual(target["metadata"]["source_sha256_lf"]["tests/routing_targets_support.py"],
                         observer_digest(Path(__file__).with_name("routing_targets_support.py").read_bytes()))
        self.assertEqual(len(original["scenarios"]), len(target["scenarios"]))
        differences = set()
        for old, new in zip(original["scenarios"], target["scenarios"], strict=True):
            with self.subTest(scenario=old["id"]):
                self.assertEqual(old["id"], new["id"])
                self.assertEqual(old.get("config", {}), new.get("config", {}))
                old_inputs = [{key: value for key, value in step.items()
                    if key not in ("expected_route", "expected_code")} for step in old["steps"]]
                self.assertEqual(old_inputs, new["steps"])
                self.assertEqual(new["contracts"], contracts_for(old))
                observed, decisions = observe_target_scenario(new)
                validate_current_contracts(new, observed, decisions)
                self.assertEqual(len(new["deltas"]), len(new["steps"]))
                frozen_old, frozen_new = expand_observations(old["observed"]), expand_observations(new["observed"])
                expected_current = []
                for step, (before, after, delta) in enumerate(zip(frozen_old, frozen_new, new["deltas"], strict=True), 1):
                    fields = field_deltas(before, after)
                    self.assertEqual(delta["fields"], fields)
                    reason = CHANGES.get((new["id"], step))
                    self.assertEqual(delta["behavior"], reason)
                    self.assertEqual(bool(fields), bool(reason))
                    if fields:
                        differences.add((new["id"], step))
                    expected_current.append(after if fields else before)
                compare_observations({"id": new["id"], "observed": overlay(new["id"], expected_current)}, observed)
        self.assertEqual(differences, set(CHANGES))

    def test_v2_contracts_detect_mutated_indicator_knowledge_technical_owner_and_workflow_gate(self):
        from trading_companion import request_routing as routing
        corpus = read_baseline(TARGET)
        for scenario_id, missing_intent in (
                ("technical-pending", routing.IntentKind.COMPANY_CLARIFICATION),
                ("company-pending-owns-replies", routing.IntentKind.INDICATOR_KNOWLEDGE),
                ("workflow-off", routing.IntentKind.KNOWLEDGE_DISABLED)):
            with self.subTest(scenario=scenario_id):
                scenario = next(item for item in corpus["scenarios"] if item["id"] == scenario_id)
                mutant = tuple(spec for spec in routing.ROUTE_SPECS if spec.intent != missing_intent)
                with patch.object(routing, "ROUTE_SPECS", mutant):
                    observed, decisions = observe_target_scenario(scenario)
                with self.assertRaisesRegex(AssertionError, "independent target contract differs"):
                    validate_current_contracts(scenario, observed, decisions)

    def test_uuid_normalization_preserves_references_and_semantic_differences(self):
        identifier = "ab" * 16
        raw = {"request_id": identifier, "evidence": f"{identifier}|close", "instrument_id": "000001.SZ",
            "date": "2026-09-25", "expires_at": "2026-09-26T12:15:00+08:00", "count": 2,
            "order": ["000001.SZ", "600000.SH"], "answer": "SMA3", "elapsed_ms": 7}
        original = deepcopy(raw)
        normalized = Normalizer()(raw)
        self.assertEqual(raw, original)
        self.assertEqual(normalized["evidence"], normalized["request_id"] + "|close")
        self.assertEqual(Normalizer()({**raw, "request_id": "cd" * 16})["request_id"], normalized["request_id"])
        for key, replacement in (("instrument_id", "600000.SH"), ("date", "2026-09-24"),
            ("expires_at", "2026-09-26T12:15:01+08:00"), ("count", 3),
            ("order", list(reversed(raw["order"]))), ("answer", "SMA5"), ("elapsed_ms", 8)):
            with self.subTest(key=key):
                self.assertNotEqual(Normalizer()({**raw, key: replacement}), normalized)

    def test_observer_digest_ignores_checkout_line_endings_but_preserves_code_changes(self):
        self.assertEqual(observer_digest(b"first\nsecond\n"), observer_digest(b"first\r\nsecond\r\n"))
        self.assertNotEqual(observer_digest(b"first\nsecond\n"), observer_digest(b"first\nchanged\n"))

    def test_baseline_digest_ignores_crlf_checkout_but_detects_content_changes(self):
        content = BASELINE.read_bytes().replace(b"\r\n", b"\n")
        checked_out = content.replace(b"\n", b"\r\n")
        self.assertNotEqual(content, checked_out)
        self.assertEqual(baseline_digest(content), baseline_digest(checked_out))
        self.assertEqual(baseline_digest(checked_out), read_baseline(TARGET)["metadata"]["v1_sha256"])
        self.assertNotEqual(baseline_digest(content), baseline_digest(content.replace(b'"scenarios"', b'"changed"', 1)))

    def sample_observation(self):
        state = {"technical": {"state": "idle"}, "messages_used": 0, "product_saved": {
            "saved_at": "2026-09-26T12:00:00+08:00",
            "plan": {"securities": [{"instrument_id": "000001.SZ", "name": "fixture"}],
                     "end_date": "2026-09-25", "message": "old plan prose"},
            "answer": {"status": "accepted", "code": "accepted", "text": "old answer"}}}
        return {"route": "company_research", "executor_chain": ["company_research"],
            "before": deepcopy(state), "after": deepcopy(state), "events": [], "calls": {"model_forwards": 2},
            "outcome": {"response": {"request_id": "turn-1", "replayed": False, "result": {
                "status": "completed", "code": "complete", "stage": "company_research", "answer": "old",
                "model_calls_attempted": 2, "analyses_used": 1, "chart": {"values": [1.0, 2.0]},
                "parameters": {"instrument_id": "000001.SZ", "as_of_date": "2026-09-25"},
                "screening": {"request": {"limit": 2}, "rows": [{"instrument_id": "000001.SZ"}, {"instrument_id": "600000.SH"}]}}}}}

    def test_fingerprints_are_canonical_and_cover_text_values_nulls_and_array_order(self):
        self.assertEqual(fingerprint({"b": 2, "a": 1}), fingerprint({"a": 1, "b": 2}))
        self.assertEqual(fingerprint("old")["utf8_bytes"], fingerprint("new")["utf8_bytes"])
        self.assertNotEqual(fingerprint("old")["sha256"], fingerprint("new")["sha256"])
        self.assertNotEqual(fingerprint({}), fingerprint({"a": None}))
        self.assertNotEqual(fingerprint([1, 2]), fingerprint([2, 1]))
        with self.assertRaises(ValueError):
            fingerprint(float("nan"))

    def test_projection_keeps_key_fields_and_detects_changes_in_hashed_content(self):
        original = self.sample_observation()
        frozen_copy = deepcopy(original)
        projected = project_observation(original)
        self.assertEqual(original, frozen_copy)
        result = projected["outcome"]["response"]["result"]
        self.assertEqual(result["parameters"], original["outcome"]["response"]["result"]["parameters"])
        self.assertEqual(result["screening"]["row_ids"], ["000001.SZ", "600000.SH"])
        self.assertEqual(projected["calls"], original["calls"])
        for location in ("answer", "chart", "plan", "saved_answer", "before", "row_order"):
            changed = deepcopy(original)
            if location == "answer":
                changed["outcome"]["response"]["result"]["answer"] = "new"
            elif location == "chart":
                changed["outcome"]["response"]["result"]["chart"]["values"][0] = 3.0
            elif location == "plan":
                changed["after"]["product_saved"]["plan"]["message"] = "new plan prose"
            elif location == "saved_answer":
                changed["after"]["product_saved"]["answer"]["text"] = "new answer"
            elif location == "before":
                changed["before"]["messages_used"] = 1
            else:
                changed["outcome"]["response"]["result"]["screening"]["rows"].reverse()
            with self.subTest(location=location):
                self.assertNotEqual(project_observation(changed), projected)

    def test_duplicate_state_reference_resolves_against_frozen_expectation(self):
        full = [self.sample_observation(), self.sample_observation()]
        stored = project_observations(full)
        self.assertEqual(stored[1]["before"], {"$ref": "previous.after"})
        self.assertEqual(expand_observations(stored), [project_observation(value) for value in full])
        changed = deepcopy(full)
        changed[1]["before"]["messages_used"] = 1
        parent = ROOT / ".agentbus/tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            with self.assertRaisesRegex(AssertionError, "step 2.*before"):
                compare_observations({"id": "state-reference", "observed": stored}, changed, diagnostic_dir=folder)
        with self.assertRaises(ValueError):
            expand_observations([{**stored[0], "before": {"$ref": "previous.after"}}])

    def test_mismatch_writes_new_full_observation_and_success_writes_nothing(self):
        full = self.sample_observation()
        scenario = {"id": "fingerprint-diagnostic", "observed": project_observations([full])}
        parent = ROOT / ".agentbus/tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            compare_observations(scenario, [full], diagnostic_dir=folder)
            self.assertEqual(list(Path(folder).iterdir()), [])
            changed = deepcopy(full)
            changed["outcome"]["response"]["result"]["answer"] = "new"
            with self.assertRaises(AssertionError) as caught:
                compare_observations(scenario, [changed], diagnostic_dir=folder)
            paths = list(Path(folder).glob("routing-baseline-*/step-1.json"))
            self.assertEqual(len(paths), 1)
            self.assertIn(str(paths[0]), str(caught.exception))
            diagnostic = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["actual_full_observation"], changed)
            self.assertEqual(diagnostic["expected_projection"], scenario["observed"][0])

    def test_explicit_capture_writes_both_formats_and_refuses_to_overwrite_them(self):
        revision = "09cd36682d4cd3f9dbcb66251ac5ec3dfe0aa6e3"
        parent = ROOT / ".agentbus/tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            folder = Path(folder)
            source, compact, full_path = (folder / name for name in ("input.json", "compact.json", "full.json"))
            source.write_text(json.dumps({"metadata": {"capture_command": "explicit fixture capture"},
                "scenarios": [{"id": "capture-output", "steps": [{"expected_route": "company_research"}]}]}), encoding="utf-8")
            arguments = SimpleNamespace(source_commit=revision, corpus_version="fixture-v2",
                input=source, output=compact, full_output=full_path)
            with patch("tests.routing_baseline_support.subprocess.check_output", return_value=revision), \
                 patch("tests.routing_baseline_support.subprocess.run"), \
                 patch("tests.routing_baseline_support.observe_scenario", return_value=[self.sample_observation()]), \
                 redirect_stdout(StringIO()):
                capture(arguments)
                projected, full = read_baseline(compact), read_baseline(full_path)
                self.assertEqual(full["metadata"]["schema_version"], 1)
                self.assertEqual(projected["metadata"]["schema_version"], 2)
                self.assertEqual(full["scenarios"][0]["observed"], [self.sample_observation()])
                compare_observations(projected["scenarios"][0], full["scenarios"][0]["observed"], diagnostic_dir=folder)
                saved = {path: path.read_bytes() for path in (compact, full_path)}
                with self.assertRaises(ValueError):
                    capture(arguments)
                self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_invalid_result_shape_still_writes_the_full_new_observation(self):
        full = self.sample_observation()
        scenario = {"id": "invalid-result-shape", "observed": project_observations([full])}
        full["outcome"]["response"]["result"] = None
        parent = ROOT / ".agentbus/tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            with self.assertRaisesRegex(AssertionError, "cannot project changed result shape"):
                compare_observations(scenario, [full], diagnostic_dir=folder)
            paths = list(Path(folder).glob("routing-baseline-*/step-1.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8"))["actual_full_observation"], full)


if __name__ == "__main__":
    unittest.main()
