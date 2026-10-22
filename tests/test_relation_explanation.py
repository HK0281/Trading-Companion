from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_research_turn
from trading_companion.agent_input import parse_research_question, request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.explanation_demo import main
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError
from trading_companion.natural_agent_demo import run_demo as natural_demo
from trading_companion.relation_explanation import (
    EXPLANATION_VERSION, MAX_CLAIMS, validate_indicator_explanation,
)
from trading_companion.research_tool import ResearchToolExecutor, TOOL_NAME
from trading_companion.session_agent_demo import run_demo as session_demo
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "sample_daily_bars_synthetic.csv"
QUESTION = "分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、EMA3、MACD(2,3,2)、RSI2、BOLL(3,2)、ATR2、VOLUME3"


def proposal(request, identifier="relation_test"):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(identifier, TOOL_NAME, request_arguments(request)),))


def rebound(execution, result):
    """Keep aliases aligned to isolate checks deeper than the alias boundary."""
    return replace(execution, full_result=result,
                   _fact_aliases=tuple((f"f{index}", fact) for index, fact in enumerate(result.facts)))


def changed_fact(execution, identifier, **changes):
    result = execution.full_result
    return rebound(execution, replace(result, facts=tuple(
        replace(fact, **changes) if fact.fact_id == identifier else fact for fact in result.facts)))


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No network")),
            patch("requests.post", side_effect=AssertionError("No network")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No credentials")),
            patch("trading_companion.research_tool._build_source", side_effect=AssertionError("No online data")),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.source = CsvDailyBarSource(FIXTURE, is_synthetic=True)
        self.request = parse_research_question(QUESTION, source="csv")
        self.execution = self.execute(self.request)

    def execute(self, request):
        execution = ResearchToolExecutor(development_source=self.source, request_id=request.request_id).execute(
            proposal(request), messages=[ToolMessage("user", "Fixed offline research")])
        self.assertEqual(execution.status, "executed")
        return execution

    def payload(self, *, execution=None, relations=None):
        execution = self.execution if execution is None else execution
        result = execution.full_result
        relations = [result.relations[0]] if relations is None else relations
        aliases = {fact.fact_id: alias for alias, fact in execution.fact_aliases.items()}
        return {"version": EXPLANATION_VERSION, "request_id": result.request.request_id,
                "instrument_id": result.request.instrument_id, "trade_date": result.latest_trade_date.isoformat(),
                "claims": [{"evidence_id": aliases[item.fact_id], "relation": item.field, "value": item.value}
                           for item in relations]}

    def check(self, payload=None, *, execution=None, text=None, request=None):
        return validate_indicator_explanation(
            json.dumps(self.payload() if payload is None else payload) if text is None else text,
            execution=self.execution if execution is None else execution,
            expected_request=self.request if request is None else request)

    def rejected(self, validation):
        self.assertEqual(validation.status, "rejected")
        self.assertEqual(validation.claims, ())
        self.assertIsNone(validation.text)

    def with_bars(self, closes, volumes=None):
        template = self.execution.full_result.bars[0]
        bars = [replace(template, trade_date=date(2026, 9, 21) + timedelta(days=index),
                        open_price=value, high_price=value, low_price=value, close_price=value,
                        volume_shares=100 if volumes is None else volumes[index])
                for index, value in enumerate(closes)]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            return self.execute(self.request)


class RelationValidationTests(GuardedTests):
    def test_all_seven_families_and_mean_pairs_are_supported_in_bounded_batches(self):
        relations = self.execution.full_result.relations
        self.assertEqual(len(relations), 17)
        for start in range(0, len(relations), MAX_CLAIMS):
            group = relations[start:start + MAX_CLAIMS]
            result = self.check(self.payload(relations=group))
            self.assertEqual(result.status, "accepted", result.code)
            self.assertEqual(len(result.claims), len(group))

    def test_checked_output_contains_full_ids_dependencies_and_application_text(self):
        result = self.check()
        self.assertEqual(result.status, "accepted")
        claim = result.claims[0]
        self.assertEqual(claim.fact_id, self.execution.full_result.relations[0].fact_id)
        self.assertEqual(claim.dependencies, self.execution.full_result.relations[0].dependencies)
        self.assertEqual(claim.text, "收盘价高于SMA(3)。")
        self.assertEqual(result.to_dict()["origin"], "application")
        self.assertIn("not_predictions", result.to_dict()["validation_scope"])

    def test_reversed_relation_is_rejected_even_when_citation_exists(self):
        payload = self.payload()
        payload["claims"][0]["value"] = "below"
        result = self.check(payload)
        self.rejected(result)
        self.assertEqual(result.code, "explanation_relation_mismatch")

    def test_current_position_does_not_prove_a_cross(self):
        payload = self.payload()
        payload["claims"][0].update(relation="price_cross", value="cross_up")
        self.rejected(self.check(payload))

    def test_numeric_citation_cannot_masquerade_as_relation(self):
        payload = self.payload()
        payload["claims"][0]["evidence_id"] = "f0"
        self.assertEqual(self.execution.fact_aliases["f0"].unit, "CNY/share")
        self.rejected(self.check(payload))

    def test_unknown_alias_is_rejected(self):
        payload = self.payload()
        payload["claims"][0]["evidence_id"] = "f99999"
        self.rejected(self.check(payload))

    def test_root_stock_request_and_date_must_match_this_turn(self):
        for key, value in (("request_id", "old-request"), ("instrument_id", "600000.SH"),
                           ("trade_date", "2026-09-24")):
            payload = self.payload()
            payload[key] = value
            self.rejected(self.check(payload))

    def test_actual_latest_date_not_requested_cutoff_is_required(self):
        request = replace(self.request, as_of_date=date(2026, 9, 26))
        execution = self.execute(request)
        payload = self.payload(execution=execution)
        self.assertEqual(self.check(payload, execution=execution, request=request).status, "accepted")
        payload["trade_date"] = "2026-09-26"
        self.rejected(self.check(payload, execution=execution, request=request))

    def test_one_wrong_claim_rejects_all_without_partial_acceptance(self):
        payload = self.payload(relations=self.execution.full_result.relations[:3])
        payload["claims"][-1]["value"] = "cross_down"
        self.rejected(self.check(payload))

    def test_old_request_alias_map_is_not_reusable(self):
        old = self.execute(replace(self.request, request_id="previous-request"))
        self.rejected(self.check(execution=replace(self.execution, _fact_aliases=old._fact_aliases)))

    def test_alias_map_must_not_omit_add_or_swap_evidence(self):
        aliases = self.execution._fact_aliases
        for altered in (aliases[:-1], (*aliases, ("f99999", aliases[0][1])),
                        ((aliases[0][0], aliases[1][1]), (aliases[1][0], aliases[0][1]), *aliases[2:])):
            self.rejected(self.check(execution=replace(self.execution, _fact_aliases=altered)))

    def test_fact_request_stock_date_parameters_and_unit_mismatches_are_rejected(self):
        relation = self.execution.full_result.relations[0]
        for changes in ({"request_id": "old"}, {"instrument_id": "600000.SH"},
                        {"trade_date": date(2026, 9, 24)}, {"parameters": {"window": 20}},
                        {"unit": "CNY/share"}, {"field": "price_cross"}):
            self.rejected(self.check(execution=changed_fact(self.execution, relation.fact_id, **changes)))

    def test_duplicate_relation_objects_are_rejected(self):
        result = self.execution.full_result
        self.rejected(self.check(execution=rebound(self.execution, replace(
            result, relations=(*result.relations, result.relations[0])))))

    def test_unavailable_relation_is_never_accepted(self):
        result = self.execution.full_result
        bad = replace(result.relations[0], status="insufficient_history", value=None)
        self.rejected(self.check(execution=rebound(self.execution, replace(
            result, relations=(bad, *result.relations[1:])))))

    def test_missing_repeated_and_reordered_dependencies_are_rejected(self):
        result = self.execution.full_result
        relation = result.relations[0]
        for dependencies in ((), ("missing",), relation.dependencies[::-1],
                             (relation.dependencies[0],) * 2):
            changed = replace(relation, dependencies=dependencies)
            execution = rebound(self.execution, replace(result, relations=(changed, *result.relations[1:])))
            execution = changed_fact(execution, relation.fact_id, dependencies=dependencies)
            self.rejected(self.check(execution=execution))

    def test_available_relation_requires_valid_metadata_and_latest_date(self):
        result = self.execution.full_result
        relation = result.relations[0]
        for changes in ({"reason": "unknown"}, {"relation_id": "wrong"}, {"required_bars": 1},
                        {"actual_bars": 100}, {"missing_bars": 1}, {"trade_date": date(2026, 9, 24)}):
            self.rejected(self.check(execution=rebound(self.execution, replace(
                result, relations=(replace(relation, **changes), *result.relations[1:])))))

    def test_changed_fact_and_relation_values_still_require_numeric_support(self):
        result = self.execution.full_result
        relation = result.relations[0]
        execution = rebound(self.execution, replace(
            result, relations=(replace(relation, value="below"), *result.relations[1:])))
        execution = changed_fact(execution, relation.fact_id, value="below")
        payload = self.payload(execution=execution)
        self.rejected(self.check(payload, execution=execution))

    def test_previous_operand_must_be_the_immediately_previous_record(self):
        result = self.execution.full_result
        relation = next(item for item in result.relations if item.field == "mean_change")
        payload = self.payload(relations=[relation])
        previous_id = relation.dependencies[-1]
        execution = changed_fact(self.execution, previous_id, trade_date=date(2026, 9, 23))
        self.rejected(self.check(payload, execution=execution))

    def test_previous_operand_units_fields_and_values_are_checked(self):
        relation = next(item for item in self.execution.full_result.relations if item.field == "mean_change")
        payload = self.payload(relations=[relation])
        for changes in ({"unit": "shares"}, {"field": "dif"}, {"value": 999.0}, {"value": True}):
            self.rejected(self.check(payload, execution=changed_fact(
                self.execution, relation.dependencies[-1], **changes)))

    def test_sma20_insufficiency_cannot_be_filled_by_sma3_evidence(self):
        request = parse_research_question(QUESTION.rsplit("指标：", 1)[0] + "指标：SMA3、SMA20", source="csv")
        execution = self.execute(request)
        payload = self.payload(execution=execution)
        self.assertEqual(self.check(payload, execution=execution, request=request).status, "accepted")
        missing = next(item for item in execution.full_result.relations if "window=20" in item.indicator_id)
        self.assertIsNone(missing.fact_id)
        fake = replace(execution.full_result.relations[0], indicator_id=missing.indicator_id)
        result = replace(execution.full_result, relations=(fake, *execution.full_result.relations[1:]))
        self.rejected(self.check(payload, execution=rebound(execution, result), request=request))

    def test_flat_prices_cover_equality_coincident_bands_and_zero_changes(self):
        execution = self.with_bars([10.0] * 5)
        relations = execution.full_result.relations
        for start in range(0, len(relations), MAX_CLAIMS):
            result = self.check(self.payload(execution=execution, relations=relations[start:start + MAX_CLAIMS]),
                                execution=execution)
            self.assertEqual(result.status, "accepted", result.code)
        self.assertIn("at_coincident_bands", [item.value for item in relations])
        self.assertIn("unchanged", [item.value for item in relations])

    def test_downward_values_do_not_get_rendered_as_upward_momentum(self):
        execution = self.with_bars([10.8, 10.5, 10.4, 10.2, 10.0])
        relations = execution.full_result.relations
        for start in range(0, len(relations), MAX_CLAIMS):
            result = self.check(self.payload(execution=execution, relations=relations[start:start + MAX_CLAIMS]),
                                execution=execution)
            self.assertEqual(result.status, "accepted", result.code)
        histogram = next(item for item in relations if item.field == "histogram_change")
        result = self.check(self.payload(execution=execution, relations=[histogram]), execution=execution)
        self.assertIn("不推断上涨或下跌动量", result.text)

    def test_zero_prior_volume_allows_share_comparison_not_a_defined_ratio(self):
        execution = self.with_bars([10.0] * 5, volumes=[0, 0, 0, 0, 100])
        relation = next(item for item in execution.full_result.relations if item.field == "current_vs_prior_mean")
        result = self.check(self.payload(execution=execution, relations=[relation]), execution=execution)
        self.assertEqual(result.status, "accepted", result.code)
        ratio = next(item for item in execution.full_result.relations if item.field == "relative_volume_position")
        self.assertEqual(ratio.status, "undefined")

    def test_rsi_exact_30_and_70_are_neutral_not_threshold_crossings(self):
        # Stub the numerical boundary itself; floating-point RSI computation may be just below 30.
        for boundary in (30.0, 70.0):
            with patch("trading_companion.multi_analysis.calculations.rsi_close",
                       return_value=[None, None, boundary, boundary, boundary]):
                execution = self.execute(self.request)
            relation = next(item for item in execution.full_result.relations if item.field == "reference_zone")
            self.assertEqual(relation.value, "neutral")
            result = self.check(self.payload(execution=execution, relations=[relation]), execution=execution)
            self.assertEqual(result.status, "accepted", result.code)
            self.assertIn("含边界", result.text)

    def test_rsi_values_near_boundaries_are_not_rounded_before_comparison(self):
        for boundary, expected in ((29.999999999999996, "below_30"), (70.00000000000001, "above_70")):
            with patch("trading_companion.multi_analysis.calculations.rsi_close",
                       return_value=[None, None, boundary, boundary, boundary]):
                execution = self.execute(self.request)
            relation = next(item for item in execution.full_result.relations if item.field == "reference_zone")
            result = self.check(self.payload(execution=execution, relations=[relation]), execution=execution)
            self.assertEqual(result.status, "accepted", result.code)
            self.assertEqual(result.claims[0].value, expected)

    def test_full_precision_relation_does_not_depend_on_display_rounding(self):
        execution = self.with_bars([10.0, 10.0, 10.0, 10.0, 10.000000000001])
        result = self.check(self.payload(execution=execution), execution=execution)
        self.assertEqual(result.status, "accepted", result.code)
        self.assertEqual(result.claims[0].value, "above")

    def test_multiple_mean_pairs_use_exact_indicator_instances(self):
        request = parse_research_question(QUESTION.rsplit("指标：", 1)[0] + "指标：SMA1、SMA3、EMA3", source="csv")
        execution = self.execute(request)
        pairs = [item for item in execution.full_result.relations if item.field == "mean_position"]
        result = self.check(self.payload(execution=execution, relations=pairs), execution=execution, request=request)
        self.assertEqual(result.status, "accepted", result.code)
        self.assertEqual(len(result.claims), 3)
        self.assertIn("SMA(1)", result.claims[0].text)

    def test_blocked_or_missing_expected_request_cannot_authorize_claims(self):
        result = validate_indicator_explanation(json.dumps(self.payload()), execution=self.execution,
                                                expected_request=None)
        self.rejected(result)

    def test_mean_pair_orientation_follows_actual_execution_not_requested_order(self):
        reversed_request = replace(self.request, indicators=self.request.indicators[::-1])
        execution = self.execute(reversed_request)
        pair = next(item for item in execution.full_result.relations if item.field == "mean_position")
        result = self.check(self.payload(execution=execution, relations=[pair]), execution=execution)
        self.assertEqual(result.status, "accepted", result.code)
        self.assertTrue(result.claims[0].text.startswith("EMA(3)"))

    def test_same_short_alias_is_interpreted_only_against_current_mapping(self):
        result = self.check()
        self.assertEqual(result.claims[0].fact_id, self.execution.fact_aliases[result.claims[0].evidence_id].fact_id)
        # No checker can determine which historical alias the model privately had in mind.
        self.assertEqual(result.request_id, self.request.request_id)

    def test_validation_is_pure_and_export_is_detached(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No second read")):
            first, second = self.check(), self.check()
        self.assertEqual(first, second)
        exported = first.to_dict()
        exported["claims"][0]["text"] = "invented"
        self.assertNotIn("invented", first.text)
        json.dumps(first.to_dict(), allow_nan=False)

    def test_unusable_report_or_unexecuted_tool_cannot_authorize_explanation(self):
        for execution in (replace(self.execution, status="limited"), replace(self.execution, full_result=None),
                          replace(self.execution, full_result=replace(self.execution.full_result, source="tencent"))):
            self.rejected(self.check(execution=execution))


class ExplanationShapeTests(GuardedTests):
    def test_arbitrary_prose_and_code_fences_are_rejected(self):
        for text in ("明天保证上涨", "```json\n" + json.dumps(self.payload()) + "\n```",
                     json.dumps(self.payload()) + " plus extra prose", "", " " * 4001):
            self.rejected(self.check(text=text))

    def test_root_shapes_and_version_are_strict(self):
        payloads = [[], None, "text", {**self.payload(), "version": "future"},
                    {**self.payload(), "text": "buy now"}]
        for key in self.payload():
            payload = self.payload()
            del payload[key]
            payloads.append(payload)
        for payload in payloads:
            self.rejected(self.check(text=json.dumps(payload)))

    def test_duplicate_json_keys_at_root_and_claim_level_are_rejected(self):
        text = json.dumps(self.payload())
        for changed in (text.replace('{', '{"version":"bad",', 1),
                        text.replace('"relation":', '"value":"below","relation":')):
            self.rejected(self.check(text=changed))

    def test_nonfinite_and_numeric_fields_are_rejected(self):
        text = json.dumps(self.payload())
        for number in ("NaN", "Infinity", "-Infinity", "1e999", "42", "true", "null"):
            changed = text.replace('"above"', number)
            self.rejected(self.check(text=changed))

    def test_claim_count_has_both_bounds(self):
        for claims in ([], self.payload()["claims"] * 9):
            payload = {**self.payload(), "claims": claims}
            self.rejected(self.check(payload))

    def test_duplicate_claims_are_not_silently_deduplicated(self):
        payload = self.payload()
        payload["claims"] *= 2
        self.rejected(self.check(payload))

    def test_claim_fields_and_types_are_strict(self):
        for claim in (None, [], "text", {**self.payload()["claims"][0], "text": "up"},
                      {"evidence_id": "f1", "value": "above"},
                      {"evidence_id": 1, "relation": "price_position", "value": "above"},
                      {"evidence_id": " f1", "relation": "price_position", "value": "above"},
                      {"evidence_id": "f1", "relation": "price_position", "value": ["above"]}):
            self.rejected(self.check({**self.payload(), "claims": [claim]}))

    def test_deeply_nested_and_oversized_json_are_rejected_without_crashing(self):
        for text in ("[" * 1200 + "]" * 1200, json.dumps(self.payload()) + " " * 4000):
            self.rejected(self.check(text=text))


class AgentExplanationTests(GuardedTests):
    def agent(self, model, *, question=QUESTION):
        return run_research_turn(question, model=model, development_source=self.source)

    def test_valid_contract_completes_with_two_calls_and_one_read(self):
        model = ExplanationDemoModel([proposal(self.request)])
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
            result = self.agent(model)
        self.assertEqual((result.status, result.code), ("completed", "research_completed"))
        self.assertEqual(result.explanation_validation.status, "accepted")
        self.assertEqual((result.model_calls_attempted, result.analyses_used, model.responses_consumed), (2, 1, 2))
        self.assertIn("已核对的指标关系", result.display_answer)
        self.assertNotIn('"claims"', result.display_answer)
        self.assertEqual(result.answer_validation, "not_performed")
        read.assert_called_once()

    def test_free_prose_no_longer_counts_as_success_even_with_complete_facts(self):
        bad = "Wrong stock 600000.SH; guaranteed 999 profit"
        model = FakeToolModel([proposal(self.request), ToolModelResponse("complete", bad)])
        result = self.agent(model)
        self.assertEqual((result.status, result.code), ("limited", "explanation_rejected"))
        self.assertEqual(result.safe_answer.status, "complete")
        self.assertEqual(result.model_draft, bad)
        self.assertNotIn(bad, result.display_answer)
        self.assertIn("仅展示事实摘要", result.display_answer)

    def test_rejected_structured_claims_do_not_leak_or_trigger_repair_calls(self):
        delegate = ExplanationDemoModel([proposal(self.request)])
        def wrong(messages, *, tools):
            response = delegate.complete(messages, tools=tools)
            if response.status == "complete":
                payload = json.loads(response.text)
                payload["claims"][0]["value"] = "below"
                return replace(response, text=json.dumps(payload))
            return response
        model = Mock()
        model.complete.side_effect = wrong
        result = self.agent(model)
        self.assertEqual(result.explanation_validation.code, "explanation_relation_mismatch")
        self.assertNotIn("收盘价低于", result.display_answer)
        self.assertEqual(result.explanation_validation.claims, ())
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(model.complete.call_count, 2)

    def test_valid_available_claims_do_not_hide_missing_indicators(self):
        question = QUESTION.rsplit("指标：", 1)[0] + "指标：SMA3、SMA20"
        request = parse_research_question(question, source="csv")
        result = self.agent(ExplanationDemoModel([proposal(request)]), question=question)
        self.assertEqual((result.status, result.code), ("limited", "partial_research"))
        self.assertEqual(result.safe_answer.status, "limited")
        self.assertEqual(result.explanation_validation.status, "accepted")
        self.assertIn("SMA(20)：历史记录不足", result.display_answer)

    def test_refusal_truncation_and_extra_tool_call_never_enter_the_checker(self):
        for response in (ToolModelResponse("refused", "no"), ToolModelResponse("incomplete", "half"),
                         proposal(self.request, "another_call")):
            result = self.agent(FakeToolModel([proposal(self.request), response]))
            self.assertEqual(result.code, "explanation_unavailable")
            self.assertEqual(result.explanation_validation.status, "not_performed")
            self.assertIn("收盘价", result.display_answer)
            self.assertNotIn("已核对的指标关系", result.display_answer)
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))

    def test_model_timeout_keeps_facts_and_does_not_retry(self):
        model = Mock()
        model.complete.side_effect = [proposal(self.request), ModelCallError("timeout", "private-marker")]
        result = self.agent(model)
        self.assertEqual(result.code, "explanation_unavailable")
        self.assertEqual(result.explanation_validation.status, "not_performed")
        self.assertNotIn("private-marker", result.display_answer)
        self.assertEqual(model.complete.call_count, 2)

    def test_display_revalidates_after_request_draft_or_evidence_replacement(self):
        result = self.agent(ExplanationDemoModel([proposal(self.request)]))
        self.assertEqual(result.explanation_validation.status, "accepted")
        for changed in (replace(result, confirmed_request=replace(result.confirmed_request, request_id="other")),
                        replace(result, model_draft="invalid text"),
                        replace(result, research_result=replace(result.research_result, facts=result.research_result.facts[:-1]))):
            self.assertEqual(changed.explanation_validation.status, "rejected")
            self.assertNotIn("已核对的指标关系", changed.display_answer)

    def test_debug_export_does_not_claim_full_semantic_validation(self):
        result = self.agent(ExplanationDemoModel([proposal(self.request)]))
        payload = result.to_dict()
        self.assertEqual(payload["answer_validation"], "not_performed")
        self.assertEqual(payload["answer_validation_scope"], "free_text_semantics_not_performed")
        self.assertEqual(payload["explanation_validation"]["status"], "accepted")
        self.assertIn("debug_only", payload["model_draft_policy"])
        json.dumps(payload, allow_nan=False)

    def test_natural_entry_shares_the_checker_without_an_extra_model_call(self):
        result = natural_demo()["result"]
        self.assertEqual(result["explanation_validation"]["status"], "accepted")
        self.assertEqual((result["model_calls_attempted"], result["analyses_used"]), (3, 1))

    def test_identity_selection_uses_current_evidence_not_pending_aliases(self):
        result = session_demo()
        self.assertEqual(result["first_turn"]["explanation_validation"]["status"], "not_performed")
        self.assertEqual(result["selection_turn"]["explanation_validation"]["status"], "accepted")
        self.assertEqual(result["repeated_selection"]["explanation_validation"]["status"], "not_performed")
        self.assertEqual(result["total_model_responses"], 3)

    def test_explanation_instructions_are_in_the_first_system_message(self):
        delegate = ExplanationDemoModel([proposal(self.request)])
        calls = []
        model = Mock()
        def capture(messages, *, tools):
            calls.append(messages)
            return delegate.complete(messages, tools=tools)
        model.complete.side_effect = capture
        result = self.agent(model)
        self.assertEqual(result.explanation_validation.status, "accepted")
        self.assertIn(EXPLANATION_VERSION, calls[0][0].content)
        self.assertEqual(sum(message.role == "system" for message in calls[1]), 1)
        self.assertEqual(calls[1][-1].role, "tool")

    def test_demo_model_has_one_final_response_and_no_hidden_analysis(self):
        model = ExplanationDemoModel([])
        messages = [ToolMessage("user", "offline"), proposal(self.request).as_message(), *self.execution.messages]
        from trading_companion.research_tool import RESEARCH_TOOL_DEFINITION
        response = model.complete(messages, tools=(RESEARCH_TOOL_DEFINITION,))
        self.assertEqual(response.status, "complete")
        self.assertTrue(response.is_mock)
        self.assertEqual(model.responses_consumed, 1)
        with self.assertRaises(ModelCallError):
            model.complete(messages, tools=(RESEARCH_TOOL_DEFINITION,))

    def test_display_demo_prints_checked_text_without_raw_json(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 0)
        text = output.getvalue()
        self.assertIn("人工样例 / 非真实行情", text)
        self.assertIn("收盘价高于SMA(3)", text)
        self.assertNotIn('"evidence_id"', text)

    def test_demo_is_runnable_in_a_clean_process_without_credentials(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "trading_companion.explanation_demo"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True,
            text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已核对的指标关系", result.stdout)


if __name__ == "__main__":
    unittest.main()
