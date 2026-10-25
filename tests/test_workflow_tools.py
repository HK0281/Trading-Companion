"""Offline authorization, snapshot isolation and budget checks for workflow tools."""

from datetime import date, timedelta
from pathlib import Path
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent_input import request_arguments
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.model import ModelConfigurationError, ModelRequestError
from trading_companion.multi_contracts import MultiIndicatorRequest
from trading_companion.research_tool import (
    RESEARCH_TOOL_DEFINITION, RELATION_RULE_CATALOG, SEED_METHOD_CATALOG, TOOL_NAME, ToolExecutionError,
)
from trading_companion.tool_model import ToolCall, ToolMessage, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.workflow_tools import (
    ADDITIONAL_TOOL_NAME, KNOWLEDGE_TOOL_NAME, MAX_WORKFLOW_TOOL_CALLS,
    SNAPSHOT_TOOL_NAME, WORKFLOW_PRIMARY_DEFINITION, WorkflowToolExecutor, _WORKFLOW_RELATION_RULES,
)


START = date(2026, 1, 1)
SECRET = "private-provider-error-or-model-argument"


def bars(length=100):
    return [DailyBar("000001.SZ", START + timedelta(days=index), 10 + index / 10,
                     10 + index / 10, 10 + index / 10, 10 + index / 10,
                     1000 + index, 0.0) for index in range(length)]


def request(*, source="csv", indicators=None):
    options = {} if indicators is None else {"indicators": tuple(indicators)}
    return MultiIndicatorRequest("000001.SZ", START, START + timedelta(days=99),
                                 source=source, request_id="trusted_primary", **options)


def fixture_executor(confirmed=None):
    return WorkflowToolExecutor(request() if confirmed is None else confirmed,
        development_source=CsvDailyBarSource(Path("fixture-not-opened.csv"), is_synthetic=True))


def call(name=TOOL_NAME, arguments=None, *, call_id="primary_1", confirmed=None):
    return ToolCall(call_id, name,
                    request_arguments(request() if confirmed is None else confirmed)
                    if arguments is None else arguments)


def history():
    return [ToolMessage("user", "Analyze the explicitly configured synthetic snapshot.")]


def append(messages, proposal, output):
    return [*messages, ToolMessage("assistant", tool_calls=(proposal,)), output]


class WorkflowToolTests(unittest.TestCase):
    def test_compact_workflow_declaration_preserves_schema_rules_and_seed_catalog(self):
        expected = RESEARCH_TOOL_DEFINITION.parameters
        for item in expected["properties"].values():
            item.pop("description", None)
        self.assertEqual(WORKFLOW_PRIMARY_DEFINITION.parameters, expected)
        self.assertEqual(_WORKFLOW_RELATION_RULES.keys(), RELATION_RULE_CATALOG.keys())
        for method in SEED_METHOD_CATALOG.values():
            self.assertIn(method, WORKFLOW_PRIMARY_DEFINITION.description)
        self.assertLess(len(WORKFLOW_PRIMARY_DEFINITION.description), len(RESEARCH_TOOL_DEFINITION.description))
        self.assertIn("description", RESEARCH_TOOL_DEFINITION.parameters["properties"]["instrument_id"])

    def setUp(self):
        for guard in (patch("requests.get", side_effect=AssertionError("No real market HTTP")),
                      patch("requests.post", side_effect=AssertionError("No real model HTTP"))):
            guard.start()
            self.addCleanup(guard.stop)

    def primary(self, executor=None, messages=None):
        executor = fixture_executor() if executor is None else executor
        messages = history() if messages is None else messages
        proposal = call(confirmed=executor._request)
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars()) as read:
            output = executor.execute(proposal, messages=messages)
        self.assertEqual(read.call_count, 1)
        return executor, append(messages, proposal, output), output

    def test_constructor_requires_valid_trusted_request_and_matching_source(self):
        for confirmed in ({}, "request", object()):
            with self.subTest(confirmed=type(confirmed)), self.assertRaises(ModelConfigurationError):
                WorkflowToolExecutor(confirmed)
        with self.assertRaises(ModelConfigurationError):
            WorkflowToolExecutor(request())
        with self.assertRaises(ModelConfigurationError):
            fixture_executor(request(source="tencent"))

    def test_declares_only_four_read_only_tools(self):
        executor = fixture_executor()
        self.assertEqual({definition.name for definition in executor.definitions}, {
            TOOL_NAME, KNOWLEDGE_TOOL_NAME, SNAPSHOT_TOOL_NAME, ADDITIONAL_TOOL_NAME})
        self.assertEqual(executor.tool_calls_used, 0)
        self.assertEqual(executor.steps, ())
        self.assertEqual(executor.observations, ())
        self.assertIsNone(executor.primary_execution)

    def test_wrong_stock_dates_source_or_indicators_are_rejected_without_io(self):
        original = request_arguments(request())
        variants = [dict(original, instrument_id="600000.SH"),
                    dict(original, start_date="2026-01-02"),
                    dict(original, as_of_date="2026-04-09"),
                    dict(original, source="tencent"),
                    dict(original, indicators=[{"kind": "sma", "window": 2}]),
                    dict(original, url=SECRET)]
        for arguments in variants:
            with self.subTest(arguments=arguments), patch.object(
                    CsvDailyBarSource, "read_daily_bars") as read:
                executor = fixture_executor()
                proposal = call(arguments=arguments)
                output = executor.execute(proposal, messages=history())
            self.assertEqual(output.result["code"], "proposal_request_mismatch")
            self.assertEqual(executor.tool_calls_used, 1)
            self.assertIsNone(executor.primary_execution)
            self.assertNotIn(SECRET, json.dumps(output.to_dict()))
            validate_tool_messages(append(history(), proposal, output), tools=executor.definitions)
            read.assert_not_called()

    def test_primary_preserves_complete_evidence_and_adds_snapshot_key(self):
        executor, messages, output = self.primary()
        self.assertEqual(output.result["status"], "analysis_result")
        self.assertEqual(output.result["snapshot_key"], "primary")
        self.assertEqual(executor.observations[0].key, "primary")
        self.assertEqual(executor.primary_execution.full_result.request.request_id, "trusted_primary")
        self.assertTrue(executor.primary_execution.fact_aliases)
        validate_tool_messages(messages, tools=executor.definitions)

    def test_primary_duplicate_does_not_read_or_retry_with_another_call_id(self):
        executor, messages, _ = self.primary()
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            output = executor.execute(call(call_id="primary_again"), messages=messages)
        self.assertEqual(output.result["code"], "duplicate_request")
        self.assertEqual(len(executor.observations), 1)
        self.assertEqual(executor.tool_calls_used, 2)
        read.assert_not_called()

    def test_primary_failure_consumes_the_attempt_and_hides_provider_errors(self):
        source = SimpleNamespace(source_name="tencent", adjustment="unadjusted",
                                 read_daily_bars=Mock(side_effect=DataSourceError(SECRET)))
        executor = WorkflowToolExecutor(request(source="tencent"))
        proposal = call(confirmed=executor._request)
        with patch("trading_companion.research_tool.TencentDailyBarSource", return_value=source) as factory:
            output = executor.execute(proposal, messages=history())
            again = executor.execute(call(call_id="retry", confirmed=executor._request),
                                     messages=append(history(), proposal, output))
        self.assertEqual(output.result["data_status"], "source_error")
        self.assertEqual(again.result["code"], "duplicate_request")
        self.assertNotIn(SECRET, json.dumps(output.to_dict()))
        factory.assert_called_once()
        source.read_daily_bars.assert_called_once()

    def test_knowledge_uses_application_definitions_and_never_reads_market(self):
        executor = fixture_executor()
        proposal = call(KNOWLEDGE_TOOL_NAME, {"topics": ["dif", "dea"]})
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            output = executor.execute(proposal, messages=history())
        self.assertEqual(output.result["origin"], "application_curated")
        self.assertFalse(output.result["market_data_used"])
        self.assertEqual(output.result["topics"], ["dif", "dea"])
        self.assertIsNone(executor.primary_execution)
        read.assert_not_called()
        validate_tool_messages(append(history(), proposal, output), tools=executor.definitions)

    def test_knowledge_rejects_bad_keys_types_duplicates_and_more_than_three(self):
        for values in ({}, {"topics": []}, {"topics": ["DIF"]}, {"topics": [1]},
                       {"topics": ["dif", "dif"]}, {"topics": ["dif", "dea", "rsi", "atr"]},
                       {"topics": ["dif"], "url": SECRET}):
            with self.subTest(values=values):
                output = fixture_executor().execute(call(KNOWLEDGE_TOOL_NAME, values), messages=history())
            self.assertEqual(output.result["code"], "invalid_arguments")
            self.assertNotIn(SECRET, json.dumps(output.to_dict()))

    def test_knowledge_duplicate_is_order_independent(self):
        executor = fixture_executor()
        first = call(KNOWLEDGE_TOOL_NAME, {"topics": ["dif", "dea"]})
        output = executor.execute(first, messages=history())
        second = call(KNOWLEDGE_TOOL_NAME, {"topics": ["dea", "dif"]}, call_id="knowledge_again")
        duplicate = executor.execute(second, messages=append(history(), first, output))
        self.assertEqual(duplicate.result["code"], "duplicate_request")

    def test_snapshot_summary_retains_scope_provenance_and_availability(self):
        executor, messages, _ = self.primary()
        initial = executor.primary_execution
        for section in ("overview", "availability"):
            proposal = call(SNAPSHOT_TOOL_NAME, {"section": section}, call_id="inspect_" + section)
            with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
                output = executor.execute(proposal, messages=messages)
            self.assertEqual(output.result["snapshot_key"], "primary")
            self.assertFalse(output.result["market_data_requested"])
            self.assertEqual(output.result["data_authenticity"], "synthetic")
            self.assertEqual(output.result["price_adjustment"], "unknown")
            self.assertEqual(output.result["retrieved_at"], initial.full_result.retrieved_at.isoformat())
            self.assertNotIn("tables", output.result)
            if section == "availability":
                self.assertTrue(output.result["fields"])
            messages = append(messages, proposal, output)
            read.assert_not_called()
        self.assertEqual(executor.primary_execution, initial)

    def test_snapshot_requires_primary_and_rejects_unknown_sections_or_args(self):
        executor = fixture_executor()
        output = executor.execute(call(SNAPSHOT_TOOL_NAME, {"section": "overview"}), messages=history())
        self.assertEqual(output.result["code"], "primary_snapshot_unavailable")
        for arguments in ({"section": "relations"}, {"section": []}, {"section": "overview", "url": SECRET}):
            with self.subTest(arguments=arguments):
                output = fixture_executor().execute(call(SNAPSHOT_TOOL_NAME, arguments), messages=history())
            self.assertEqual(output.result["code"], "invalid_arguments")

    def test_supplement_uses_same_bars_without_network_and_preserves_primary(self):
        executor, messages, _ = self.primary()
        original = executor.primary_execution
        proposal = call(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 90},
                                                            {"kind": "ema", "window": 30}]}, call_id="supplement_1")
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read, patch(
                "trading_companion.research_tool.TencentDailyBarSource") as online:
            output = executor.execute(proposal, messages=messages)
        read.assert_not_called()
        online.assert_not_called()
        self.assertEqual(output.result["snapshot_key"], "supplement")
        self.assertEqual(output.result["based_on_request_id"], original.full_result.request.request_id)
        self.assertFalse(output.result["market_data_requested"])
        self.assertEqual([item.key for item in executor.observations], ["primary", "supplement"])
        supplementary = executor.observations[1].execution
        self.assertEqual(executor.primary_execution, original)
        self.assertNotEqual(supplementary.full_result.request.request_id, original.full_result.request.request_id)
        self.assertEqual(supplementary.full_result.bars, original.full_result.bars)
        for attribute in ("source", "data_authenticity", "price_adjustment", "retrieved_at"):
            self.assertEqual(getattr(supplementary.full_result, attribute), getattr(original.full_result, attribute))
        self.assertTrue(set(supplementary.fact_aliases) & set(original.fact_aliases))
        self.assertTrue(all(fact.request_id == supplementary.full_result.request.request_id
                            for fact in supplementary.fact_aliases.values()))
        validate_tool_messages(append(messages, proposal, output), tools=executor.definitions)

    def test_supplement_rejects_missing_primary(self):
        proposal = call(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 90}]})
        output = fixture_executor().execute(proposal, messages=history())
        self.assertEqual(output.result["code"], "primary_snapshot_unavailable")

    def test_supplement_rejects_unknown_keys_bad_specs_duplicates_and_shortening(self):
        variants = [({}, "invalid_arguments"), ({"indicators": []}, "invalid_arguments"),
                    ({"indicators": ["rsi"]}, "invalid_arguments"),
                    ({"indicators": [{"kind": "atr", "window": True}]}, "invalid_arguments"),
                    ({"indicators": [{"kind": "rsi", "window": 14}]}, "duplicate_indicator"),
                    ({"indicators": [{"kind": "rsi", "window": 2}]}, "indicator_period_reduction_not_allowed"),
                    ({"indicators": [{"kind": "sma", "window": 90}], "instrument_id": "600000.SH"}, "invalid_arguments"),
                    ({"indicators": [{"kind": "sma", "window": 90}] * 2}, "invalid_arguments"),
                    ({"indicators": [{"kind": "sma", "window": i} for i in (80, 90, 100, 110)]}, "invalid_arguments")]
        for values, expected in variants:
            with self.subTest(values=values):
                executor, messages, _ = self.primary()
                with patch("trading_companion.workflow_tools.analyze_indicators") as analyze:
                    output = executor.execute(call(ADDITIONAL_TOOL_NAME, values, call_id="supplement"),
                                              messages=messages)
                self.assertEqual(output.result["code"], expected)
                analyze.assert_not_called()
                self.assertEqual(len(executor.observations), 1)

    def test_supplement_once_even_when_new_request_is_different(self):
        executor, messages, _ = self.primary()
        first = call(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 90}]}, call_id="supplement")
        output = executor.execute(first, messages=messages)
        second = call(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 100}]}, call_id="supplement_2")
        with patch("trading_companion.workflow_tools.analyze_indicators") as analyze:
            duplicate = executor.execute(second, messages=append(messages, first, output))
        self.assertEqual(duplicate.result["code"], "additional_analysis_budget_exhausted")
        analyze.assert_not_called()
        self.assertEqual(len(executor.observations), 2)

    def test_online_supplement_preserves_online_source_without_constructing_an_adapter(self):
        executor = WorkflowToolExecutor(request(source="tencent"))
        source = SimpleNamespace(source_name="tencent", adjustment="unadjusted",
                                 read_daily_bars=Mock(return_value=bars()))
        first = call(confirmed=executor._request)
        with patch("trading_companion.research_tool.TencentDailyBarSource", return_value=source) as adapter:
            primary = executor.execute(first, messages=history())
            output = executor.execute(call(ADDITIONAL_TOOL_NAME,
                {"indicators": [{"kind": "sma", "window": 90}]}, call_id="online_supplement"),
                messages=append(history(), first, primary))
        adapter.assert_called_once()
        source.read_daily_bars.assert_called_once()
        self.assertEqual(output.result["source"], "tencent")
        self.assertEqual(output.result["mode"], "online")
        self.assertEqual(output.result["data_authenticity"], "provider_reported")
        self.assertEqual(output.result["price_adjustment"], "unadjusted")

    def test_supplement_projection_too_large_keeps_local_facts_and_original_primary(self):
        executor, messages, _ = self.primary()
        original = executor.primary_execution
        from trading_companion.research_tool import _project

        def oversized(result, **kwargs):
            payload, facts = _project(result, **kwargs)
            payload["oversized"] = "x" * 11000
            return payload, facts

        with patch("trading_companion.workflow_tools._project", side_effect=oversized):
            output = executor.execute(call(ADDITIONAL_TOOL_NAME,
                {"indicators": [{"kind": "sma", "window": 90}]}, call_id="oversized_supplement"),
                messages=messages)
        self.assertEqual(output.result["status"], "result_too_large")
        self.assertEqual(output.result["snapshot_key"], "supplement")
        self.assertTrue(output.result["full_result_retained"])
        self.assertEqual(executor.primary_execution, original)
        self.assertTrue(executor.observations[1].execution.fact_aliases)
        self.assertEqual(executor.observations[1].execution.status, "limited")

    def test_programming_error_is_closed_without_exception_details_and_cannot_retry(self):
        executor = WorkflowToolExecutor(request(source="tencent"))
        proposal = call(confirmed=executor._request)
        with patch("trading_companion.research_tool.TencentDailyBarSource", side_effect=RuntimeError(SECRET)):
            output = executor.execute(proposal, messages=history())
        self.assertEqual(output.result["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(output.to_dict()))
        validate_tool_messages(append(history(), proposal, output), tools=executor.definitions)
        again = executor.execute(call(call_id="retry_programming_error", confirmed=executor._request),
                                 messages=append(history(), proposal, output))
        self.assertEqual(again.result["code"], "duplicate_request")

    def test_shared_analysis_budget_is_reserved_before_supplement_calculation(self):
        control = TurnControl(TurnLimits(analyses=1))
        control.start()
        with bind_turn(control):
            executor, messages, _ = self.primary()
            with patch("trading_companion.workflow_tools.analyze_indicators") as analyze:
                with self.assertRaises(TurnStopped) as caught:
                    executor.execute(call(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 90}]},
                                          call_id="supplement"), messages=messages)
            self.assertEqual(caught.exception.code, "analysis_budget_exhausted")
            analyze.assert_not_called()
        self.assertEqual(control.to_dict()["analyses_used"], 1)

    def test_limits_all_tool_attempts_including_invalid_arguments(self):
        executor, messages = fixture_executor(), history()
        for index in range(MAX_WORKFLOW_TOOL_CALLS + 1):
            proposal = call(KNOWLEDGE_TOOL_NAME, {"topics": [SECRET]}, call_id=f"bad_{index}")
            output = executor.execute(proposal, messages=messages)
            messages = append(messages, proposal, output)
            self.assertEqual(output.result["code"], "invalid_arguments" if index < MAX_WORKFLOW_TOOL_CALLS
                             else "tool_call_budget_exhausted")
        self.assertEqual(executor.tool_calls_used, MAX_WORKFLOW_TOOL_CALLS)

    def test_call_ids_cannot_be_reused_even_if_the_caller_omits_history(self):
        executor = fixture_executor()
        first = call(KNOWLEDGE_TOOL_NAME, {"topics": ["dif"]})
        output = executor.execute(first, messages=history())
        for messages in (history(), append(history(), first, output)):
            with self.subTest(length=len(messages)), self.assertRaises(ToolExecutionError) as caught:
                executor.execute(first, messages=messages)
            self.assertEqual(caught.exception.code, "reused_call_id")
        self.assertEqual(executor.tool_calls_used, 1)

    def test_unknown_tool_and_invalid_history_fail_without_io(self):
        executor = fixture_executor()
        with self.assertRaises(ToolExecutionError) as caught:
            executor.execute(call("arbitrary_function", {}), messages=history())
        self.assertEqual(caught.exception.code, "unknown_tool")
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            with self.assertRaises(ModelRequestError):
                executor.execute(call(), messages=[ToolMessage("tool", call_id="orphan", name=TOOL_NAME, result={})])
        self.assertEqual(executor.tool_calls_used, 0)
        read.assert_not_called()

    def test_steps_export_only_safe_whitelisted_metadata(self):
        executor = fixture_executor()
        executor.execute(call(KNOWLEDGE_TOOL_NAME, {"topics": [SECRET]}), messages=history())
        self.assertEqual(set(executor.steps[0]), {"tool", "status", "code"})
        self.assertEqual(executor.steps[0]["status"], "rejected")
        self.assertNotIn(SECRET, json.dumps(executor.steps))
        detached = executor.steps
        detached[0]["code"] = SECRET
        self.assertNotIn(SECRET, json.dumps(executor.steps))

    def test_observations_and_primary_are_detached_from_external_mutation(self):
        executor, _, _ = self.primary()
        fact = next(fact for fact in executor.primary_execution._fact_aliases if fact[1].parameters)
        fact[1].parameters.clear()
        self.assertTrue(any(item.parameters for item in executor.primary_execution.fact_aliases.values()))
        detached = executor.observations[0].execution.full_result.indicators[0]
        detached.parameters.clear()
        self.assertTrue(executor.observations[0].execution.full_result.indicators[0].parameters)

    def test_output_exceeding_remaining_history_retains_complete_primary(self):
        executor = fixture_executor()
        messages = [ToolMessage("system", "x" * 11000), *history()]
        executor, actual, output = self.primary(executor, messages)
        self.assertEqual(output.result["status"], "result_too_large")
        self.assertTrue(output.result["full_result_retained"])
        self.assertEqual(executor.primary_execution.status, "limited")
        self.assertEqual(len(executor.primary_execution.full_result.bars), 100)
        self.assertTrue(executor.primary_execution.fact_aliases)
        validate_tool_messages(actual, tools=executor.definitions)

    def test_insufficient_fallback_capacity_prevents_io_and_budget_consumption(self):
        executor = fixture_executor()
        messages = [ToolMessage("system", "x" * 17000), *history()]
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            with self.assertRaises(ModelRequestError):
                executor.execute(call(), messages=messages)
        self.assertEqual(executor.tool_calls_used, 0)
        self.assertEqual(executor.observations, ())
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
