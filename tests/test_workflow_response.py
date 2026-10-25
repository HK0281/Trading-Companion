"""Synthetic snapshot fixtures only; no model, credentials or network requests."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_companion.agent_input import parse_research_question, request_arguments
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.relation_explanation import EXPLANATION_VERSION
from trading_companion.research_response import RESPONSE_VERSION
from trading_companion.research_tool import ResearchToolExecutor, TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolMessage, ToolModelResponse
from trading_companion.workflow_response import (
    WORKFLOW_RESPONSE_INSTRUCTION, WORKFLOW_RESPONSE_VERSION, validate_workflow_response,
)


class WorkflowResponseTests(unittest.TestCase):
    def setUp(self):
        self.source, _ = load_development_fixtures()
        self.request = parse_research_question(
            "分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3", source="csv")
        self.primary = self.execute(self.request)
        request = replace(self.request, request_id="supporting-snapshot", indicators=(IndicatorSpec("rsi", window=2),))
        execution = self.execute(request)
        self.supplement = replace(execution, full_result=replace(execution.full_result,
            retrieved_at=self.primary.full_result.retrieved_at))
        self.observations = (SimpleNamespace(key="primary", execution=self.primary),
                             SimpleNamespace(key="supplement", execution=self.supplement))
        for context in (patch("requests.get", side_effect=AssertionError("No HTTP")),
                        patch("requests.post", side_effect=AssertionError("No HTTP"))):
            context.start()
            self.addCleanup(context.stop)

    def execute(self, request):
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall(
            "fixture-call", TOOL_NAME, request_arguments(request)),))
        execution = ResearchToolExecutor(development_source=self.source, request_id=request.request_id).execute(
            proposal, messages=[ToolMessage("user", "Synthetic calculation fixture")])
        self.assertEqual(execution.status, "executed")
        return execution

    def claim(self, snapshot, execution):
        relation = next(item for item in execution.full_result.relations if item.status == "available")
        aliases = {fact.fact_id: alias for alias, fact in execution.fact_aliases.items()}
        return {"snapshot": snapshot, "evidence_id": aliases[relation.fact_id],
                "relation": relation.field, "value": relation.value}

    def payload(self, *, supplement=True):
        claims = [self.claim("primary", self.primary)]
        if supplement:
            claims.append(self.claim("supplement", self.supplement))
        citations = [f"{claim['snapshot']}:{claim['evidence_id']}" for claim in claims]
        return {"version": WORKFLOW_RESPONSE_VERSION, "request_id": self.request.request_id,
                "instrument_id": self.request.instrument_id,
                "trade_date": self.primary.full_result.latest_trade_date.isoformat(), "claims": claims,
                "paragraphs": [{"role": role, "text": text, "evidence_ids": citations.copy()}
                    for role, text in zip(("answer", "outlook", "watch_for"),
                        ("价格处于均线上方，但需要结合动量谨慎判断。",
                         "如果价格继续维持在均线上方，偏强走势可能延续。",
                         "接下来观察趋势和动量能否配合，失守均线则需要调整判断。"))]}

    def check(self, payload=None, *, text=None, observations=None, expected_request=None):
        return validate_workflow_response(json.dumps(self.payload() if payload is None else payload,
            ensure_ascii=False) if text is None else text,
            observations=self.observations if observations is None else observations,
            expected_request=self.request if expected_request is None else expected_request)

    def rejected(self, value):
        self.assertEqual(value.status, "rejected", value)
        self.assertEqual(value.paragraphs, ())
        self.assertEqual(value.relations.claims, ())

    def rebound(self, execution, result):
        return replace(execution, full_result=result,
                       _fact_aliases=tuple((f"f{index}", fact) for index, fact in enumerate(result.facts)))

    def test_combined_claims_are_checked_and_citations_resolve_full_fact_ids(self):
        checked = self.check()
        self.assertEqual(checked.status, "accepted", checked.code)
        self.assertEqual(checked.relations.request_id, self.request.request_id)
        self.assertEqual(len(checked.relations.claims), 2)
        claims = {claim.evidence_id: claim.fact_id for claim in checked.relations.claims}
        self.assertTrue(any(key.startswith("primary:") for key in claims))
        self.assertTrue(any(key.startswith("supplement:") for key in claims))
        for role, text, facts in checked.paragraphs:
            self.assertEqual(set(facts), set(claims.values()))
            self.assertTrue(all("|000001.SZ|" in identifier for identifier in facts))

    def test_primary_only_new_format_does_not_require_supplement(self):
        self.assertEqual(self.check(self.payload(supplement=False),
            observations=self.observations[:1]).status, "accepted")

    def unavailable_supplement(self):
        execution = self.execute(replace(self.request, request_id="unavailable-supplement",
                                         indicators=(IndicatorSpec("sma", window=60),)))
        result = replace(execution.full_result, retrieved_at=self.primary.full_result.retrieved_at)
        self.assertFalse(result.synthesis_allowed)
        return replace(execution, full_result=result)

    def test_unavailable_supplement_does_not_block_valid_primary_only_answer(self):
        observations = (self.observations[0], SimpleNamespace(key="supplement",
                         execution=self.unavailable_supplement()))
        self.assertEqual(self.check(self.payload(supplement=False), observations=observations).status,
                         "accepted")

    def test_unavailable_supplement_cannot_supply_an_invented_relation(self):
        observations = (self.observations[0], SimpleNamespace(key="supplement",
                         execution=self.unavailable_supplement()))
        payload = self.payload(supplement=False)
        payload["claims"].append({"snapshot": "supplement", "evidence_id": "f0",
                                   "relation": "price_position", "value": "above"})
        self.rejected(self.check(payload, observations=observations))

    def test_unavailable_primary_is_not_rescued_by_a_supplement(self):
        primary = self.unavailable_supplement()
        observations = (SimpleNamespace(key="primary", execution=primary),)
        self.rejected(self.check(self.payload(supplement=False), observations=observations,
                                 expected_request=primary.full_result.request))

    def test_same_local_alias_in_two_snapshots_keeps_distinct_full_identifiers(self):
        # Equal f aliases are allowed, but are never a shared global evidence identifier.
        alternate = self.execute(replace(self.request, request_id="second-same-calculation"))
        alternate = replace(alternate, full_result=replace(alternate.full_result,
                            retrieved_at=self.primary.full_result.retrieved_at))
        observations = (self.observations[0], SimpleNamespace(key="supplement", execution=alternate))
        payload = self.payload(supplement=False)
        payload["claims"].append(self.claim("supplement", alternate))
        first, second = payload["claims"]
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        for paragraph in payload["paragraphs"]:
            paragraph["evidence_ids"].append("supplement:" + second["evidence_id"])
        checked = self.check(payload, observations=observations)
        self.assertEqual(checked.status, "accepted")
        self.assertNotEqual(*checked.paragraphs[0][2])

    def test_top_level_request_stock_and_date_bind_primary_not_supplement(self):
        for field, value in (("request_id", "supporting-snapshot"), ("instrument_id", "600000.SH"),
                             ("trade_date", "2026-09-24")):
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = value
                self.rejected(self.check(payload))

    def test_reversed_value_and_wrong_snapshot_alias_reject_entire_response(self):
        for change in (
            lambda payload: payload["claims"][0].update(value="below"),
            lambda payload: payload["claims"][1].update(snapshot="primary"),
            lambda payload: payload["claims"][0].update(evidence_id="f0"),
            lambda payload: payload["claims"][0].update(evidence_id="primary:f7"),
            lambda payload: payload["claims"][0].update(snapshot="missing"),
        ):
            payload = self.payload()
            change(payload)
            self.rejected(self.check(payload))

    def test_changed_fact_and_aligned_aliases_are_recomputed_not_trusted(self):
        relation = next(item for item in self.supplement.full_result.relations if item.status == "available")
        numeric = relation.dependencies[0]
        changed = replace(self.supplement.full_result, facts=tuple(
            replace(fact, value=999.0) if fact.fact_id == numeric else fact
            for fact in self.supplement.full_result.facts))
        observations = (self.observations[0], SimpleNamespace(key="supplement",
                         execution=self.rebound(self.supplement, changed)))
        self.rejected(self.check(observations=observations))

    def test_stale_alias_dictionary_is_rejected(self):
        bad = replace(self.supplement, _fact_aliases=self.primary._fact_aliases)
        self.rejected(self.check(observations=(self.observations[0],
            SimpleNamespace(key="supplement", execution=bad))))

    def test_distinct_stock_date_range_source_authenticity_adjustment_or_collection_is_rejected(self):
        result = self.supplement.full_result
        variants = (
            replace(result, request=replace(result.request, instrument_id="600000.SH")),
            replace(result, request=replace(result.request, as_of_date=result.request.as_of_date + timedelta(days=1))),
            replace(result, request=replace(result.request, start_date=result.request.start_date - timedelta(days=1))),
            replace(result, request=replace(result.request, request_id=self.request.request_id)),
            replace(result, request=replace(result.request, source="tencent"), source="tencent"),
            replace(result, data_authenticity="provider_reported"),
            replace(result, price_adjustment="unadjusted"),
            replace(result, retrieved_at=result.retrieved_at + timedelta(seconds=1)),
        )
        for changed in variants:
            with self.subTest(result=changed):
                self.rejected(self.check(observations=(self.observations[0],
                    SimpleNamespace(key="supplement", execution=self.rebound(self.supplement, changed)))))

    def test_different_frozen_bars_are_rejected_even_when_latest_date_agrees(self):
        result = self.supplement.full_result
        changed = replace(result, bars=(replace(result.bars[0], volume_shares=999), *result.bars[1:]))
        self.rejected(self.check(observations=(self.observations[0],
            SimpleNamespace(key="supplement", execution=replace(self.supplement, full_result=changed)))))

    def test_invalid_missing_or_duplicate_observations_are_rejected(self):
        for observations in ((), (self.observations[1],), (self.observations[0], self.observations[0]),
                             ({},), self.observations * 2, "primary"):
            with self.subTest(observations=observations):
                self.rejected(self.check(observations=observations))

    def test_primary_request_replacement_or_execution_not_ready_is_rejected(self):
        self.rejected(self.check(expected_request=replace(self.request, request_id="other")))
        self.rejected(self.check(observations=(SimpleNamespace(key="primary",
            execution=replace(self.primary, status="limited")),)))

    def test_unprefixed_unknown_duplicate_or_unselected_citations_are_rejected(self):
        alias = self.payload()["claims"][0]["evidence_id"]
        for citations in ([], [alias], ["primary:f9999"], ["unknown:" + alias],
                          ["primary:" + alias] * 2, [{}], ["primary:" + alias] * 9):
            payload = self.payload()
            payload["paragraphs"][0]["evidence_ids"] = citations
            self.rejected(self.check(payload))

    def test_invalid_shapes_claim_counts_and_duplicate_claims_reject_all(self):
        changes = (
            lambda payload: payload.update(extra="private"),
            lambda payload: payload.update(claims=[]),
            lambda payload: payload.update(claims=payload["claims"] * 5),
            lambda payload: payload["claims"].append(deepcopy(payload["claims"][0])),
            lambda payload: payload["claims"][0].update(extra="private"),
            lambda payload: payload["claims"][0].update(value=1),
            lambda payload: payload["paragraphs"][0].update(extra="private"),
            lambda payload: payload.update(paragraphs=[]),
            lambda payload: payload["paragraphs"][0].update(role="outlook"),
            lambda payload: payload["paragraphs"][0].update(text=" "),
            lambda payload: payload["paragraphs"][0].update(text="中" * 601),
            lambda payload: payload["paragraphs"][0].update(text="走势\u0001偏强"),
            lambda payload: payload["paragraphs"][0].update(text="走势\u007f偏强"),
        )
        for change in changes:
            payload = self.payload()
            change(payload)
            self.rejected(self.check(payload))

    def test_unapproved_numeric_prose_is_rejected(self):
        for text in ("上涨概率90%", "目标价15元", "SMA(90)暂时走强", "截至2026-09-25", "量能为２倍"):
            payload = self.payload()
            payload["paragraphs"][0]["text"] = text
            self.rejected(self.check(payload))

    def test_exact_indicator_labels_from_primary_and_supplement_are_allowed(self):
        payload = self.payload()
        payload["paragraphs"][0]["text"] = "价格高于SMA(3)，但RSI(2)并不保证后续上涨。"
        self.assertEqual(self.check(payload).status, "accepted")

    def test_duplicate_keys_nonfinite_invalid_json_fences_and_size_are_rejected(self):
        encoded = json.dumps(self.payload())
        for text in ('{"version":"research-workflow-response-v1",' + encoded[1:],
                     encoded.replace('"claims":', '"claims":NaN,"ignored":'),
                     "```json\n" + encoded + "\n```", "x" * 8001,
                     "[]", "null", "{", "Infinity", ""):
            self.rejected(self.check(text=text))

    def legacy(self, *, prose=False):
        claim = self.claim("primary", self.primary)
        claim.pop("snapshot")
        payload = {"version": RESPONSE_VERSION if prose else EXPLANATION_VERSION,
                   "request_id": self.request.request_id, "instrument_id": self.request.instrument_id,
                   "trade_date": self.primary.full_result.latest_trade_date.isoformat(), "claims": [claim]}
        if prose:
            payload["paragraphs"] = [{"role": role, "text": "价格偏强但仍需观察。",
                                      "evidence_ids": [claim["evidence_id"]]}
                                     for role in ("answer", "outlook", "watch_for")]
        return payload

    def test_legacy_relation_format_is_primary_only(self):
        checked = self.check(self.legacy())
        self.assertEqual(checked.relations.status, "accepted")
        self.assertEqual(checked.paragraphs, ())
        self.assertTrue(all(claim.fact_id.startswith(self.request.request_id + "|")
                            for claim in checked.relations.claims))

    def test_legacy_prose_format_remains_primary_only(self):
        checked = self.check(self.legacy(prose=True))
        self.assertEqual(checked.status, "accepted")
        self.assertTrue(all(identifier.startswith(self.request.request_id + "|")
                            for _, _, facts in checked.paragraphs for identifier in facts))

    def test_legacy_format_cannot_switch_its_request_to_supplement(self):
        payload = self.legacy(prose=True)
        payload["request_id"] = self.supplement.full_result.request.request_id
        self.rejected(self.check(payload))

    def test_repeated_validation_never_returns_shared_mutable_state_or_reads_data(self):
        payload = self.payload()
        checked = self.check(payload)
        with patch.object(type(self.source), "read_daily_bars", side_effect=AssertionError("No read")):
            self.assertEqual(self.check(payload), checked)
        self.assertIsInstance(checked.paragraphs, tuple)
        self.assertIsInstance(checked.relations.claims, tuple)

    def test_instruction_allows_research_before_final_and_explains_evidence_limits(self):
        self.assertIn(WORKFLOW_RESPONSE_VERSION, WORKFLOW_RESPONSE_INSTRUCTION)
        self.assertIn("supplement:f7", WORKFLOW_RESPONSE_INSTRUCTION)
        self.assertIn("no invented news", WORKFLOW_RESPONSE_INSTRUCTION)
        self.assertNotIn("make another tool call", WORKFLOW_RESPONSE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
