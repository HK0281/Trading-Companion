from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import date, timedelta
from io import StringIO
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.model import ModelConfigurationError, ModelRequestError
from trading_companion.research_tool import (
    RELATION_RULE_CATALOG, RESEARCH_TOOL_DEFINITION, SEED_METHOD_CATALOG, TOOL_NAME,
    ResearchToolExecutor, ToolExecutionError, _error, _too_large,
)
from trading_companion.research_tool_demo import main, run_demo
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse, validate_tool_messages


START = date(2026, 1, 1)
SECRET = "synthetic-sensitive-path-or-provider-marker"


def make_bars(length, *, volume=100):
    return [DailyBar("000001.SZ", START + timedelta(days=index), 10 + index / 10,
                     10 + index / 10, 10 + index / 10, 10 + index / 10, volume + index, 0.0)
            for index in range(length)]


def arguments(length=30, **overrides):
    values = {"instrument_id": "000001.SZ", "start_date": START.isoformat(),
              "as_of_date": (START + timedelta(days=length - 1)).isoformat()}
    values.update(overrides)
    return values


def proposal(values=None, call_id="research_1", name=TOOL_NAME):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(call_id, name,
                             arguments() if values is None else values),))


def fixture_executor():
    return ResearchToolExecutor(development_source=CsvDailyBarSource(Path("trusted-fixture-not-opened.csv"),
                                                                     is_synthetic=True))


def history():
    return [ToolMessage("user", "Analyze the explicitly configured synthetic fixture.")]


def table(payload, name):
    part = payload["tables"][name]
    return [dict(zip(part["columns"], row)) for row in part["rows"]]


COMMON_TWELVE = ([{"kind": "sma", "window": window} for window in (3, 5, 20, 60)]
                 + [{"kind": "ema", "window": window} for window in (10, 20)]
                 + [{"kind": "macd"}, {"kind": "rsi", "window": 14},
                    {"kind": "rsi", "window": 3}, {"kind": "boll"},
                    {"kind": "atr"}, {"kind": "volume"}])


class ResearchValidationTests(unittest.TestCase):
    def test_invalid_arguments_are_rejected_before_construction_or_read(self):
        invalid = [arguments(path=SECRET), arguments(url="https://untrusted.example"),
                   arguments(request_id="model-selected"), arguments(allow_development_csv=True),
                   arguments(source="csv"), arguments(source=None), arguments(source=True),
                   arguments(instrument_id="000001"), arguments(instrument_id="830001.BJ"),
                   arguments(start_date="20260101"), arguments(start_date=True),
                   arguments(as_of_date="2026-02-30"), arguments(as_of_date="2025-12-31"),
                   arguments(as_of_date="2027-01-02"), arguments(indicators=[]),
                   arguments(indicators=["sma"]), arguments(indicators=[{"kind": "sma", "window": True}]),
                   arguments(indicators=[{"kind": "rsi", "window": 1}]),
                   arguments(indicators=[{"kind": "macd", "fast": 30, "slow": 26}]),
                   arguments(indicators=[{"kind": "sma", "path": SECRET}]),
                   arguments(indicators=[{"kind": "sma", "window": i} for i in range(1, 14)])]
        missing = arguments()
        del missing["start_date"]
        invalid.append(missing)
        for values in invalid:
            with self.subTest(keys=tuple(values)), patch(
                    "trading_companion.research_tool.TencentDailyBarSource") as source:
                executor = ResearchToolExecutor()
                outcome = executor.execute(proposal(values), messages=history())
                self.assertEqual(outcome.status, "rejected")
                self.assertEqual(outcome.messages[0].result["code"], "invalid_arguments")
                self.assertEqual(executor.analyses_used, 0)
                self.assertNotIn(SECRET, json.dumps(outcome.messages[0].to_dict()))
                source.assert_not_called()

    def test_online_current_shanghai_day_is_rejected_before_source_construction(self):
        with patch("trading_companion.research_tool.TencentDailyBarSource") as source:
            executor = ResearchToolExecutor()
            future = date.today() + timedelta(days=2)
            outcome = executor.execute(proposal(arguments(start_date=date.today().isoformat(),
                                                           as_of_date=future.isoformat())), messages=history())
        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(executor.analyses_used, 0)
        source.assert_not_called()

    def test_development_source_is_trusted_only_and_never_silently_overrides_explicit_source(self):
        for configured in ("path.csv", lambda: None, object()):
            with self.assertRaises(ModelConfigurationError):
                ResearchToolExecutor(development_source=configured)
        for source_name in ("csv", "tencent", "akshare_eastmoney"):
            executor = fixture_executor()
            with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
                outcome = executor.execute(proposal(arguments(source=source_name)), messages=history())
            self.assertEqual(outcome.status, "rejected")
            self.assertEqual(executor.analyses_used, 0)
            read.assert_not_called()

    def test_multiple_calls_are_rejected_as_one_batch_without_executing_the_first(self):
        response = ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("one", TOOL_NAME, arguments()), ToolCall("two", TOOL_NAME, arguments(path=SECRET))))
        executor = fixture_executor()
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            outcome = executor.execute(response, messages=history())
        self.assertEqual(len(outcome.messages), 2)
        self.assertEqual({item.call_id for item in outcome.messages}, {"one", "two"})
        self.assertTrue(all(item.result["code"] == "multiple_calls_not_allowed" for item in outcome.messages))
        self.assertEqual(executor.analyses_used, 0)
        read.assert_not_called()
        validate_tool_messages([*history(), response.as_message(), *outcome.messages],
                               tools=(RESEARCH_TOOL_DEFINITION,))

    def test_unknown_tools_and_nonproposals_fail_with_fixed_classification_and_no_io(self):
        executor = fixture_executor()
        for response, expected in ((proposal(name="arbitrary_function"), "unknown_tool"),
                                   (ToolModelResponse("complete", SECRET), "not_a_tool_proposal")):
            with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
                with self.assertRaises(ToolExecutionError) as caught:
                    executor.execute(response, messages=history())
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(SECRET, str(caught.exception))
            read.assert_not_called()
        self.assertEqual(executor.analyses_used, 0)

    def test_history_and_reused_call_ids_are_validated_before_any_attempt(self):
        response = proposal()
        executor = fixture_executor()
        invalid = [ToolMessage("tool", call_id="orphan", name=TOOL_NAME, result={})]
        with self.assertRaises(ModelRequestError):
            executor.execute(response, messages=invalid)
        resolved = [*history(), response.as_message(), _error(response.tool_calls[0], "rejected")]
        with self.assertRaises(ToolExecutionError) as caught:
            executor.execute(response, messages=resolved)
        self.assertEqual(caught.exception.code, "reused_call_id")
        self.assertEqual(executor.analyses_used, 0)


class ResearchBudgetAndFailureTests(unittest.TestCase):
    def test_valid_attempt_consumes_before_source_construction_and_cannot_retry_with_new_id(self):
        executor = ResearchToolExecutor()
        def failure(*unused):
            self.assertEqual(executor.analyses_used, 1)
            raise DataSourceError(SECRET)
        with patch("trading_companion.research_tool.TencentDailyBarSource", side_effect=failure) as source:
            first = executor.execute(proposal(), messages=history())
            second = executor.execute(proposal(call_id="another_id"), messages=history())
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.messages[0].result["code"], "analysis_budget_exhausted")
        self.assertEqual(executor.analyses_used, 1)
        self.assertNotIn(SECRET, json.dumps(first.messages[0].to_dict()))
        source.assert_called_once()

    def test_unexpected_programming_error_is_classified_not_swallowed_or_exposed(self):
        executor = ResearchToolExecutor()
        with patch("trading_companion.research_tool.TencentDailyBarSource", side_effect=RuntimeError(SECRET)):
            with self.assertRaises(ToolExecutionError) as caught:
                executor.execute(proposal(), messages=history())
        self.assertEqual(caught.exception.code, "internal_error")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(executor.analyses_used, 1)

    def test_provider_failure_remains_data_failure_with_requested_indicator_parameters(self):
        fake_source = SimpleNamespace(source_name="tencent", adjustment="unadjusted",
                                      read_daily_bars=Mock(side_effect=DataSourceError(SECRET)))
        with patch("trading_companion.research_tool.TencentDailyBarSource", return_value=fake_source) as factory:
            executor = ResearchToolExecutor()
            outcome = executor.execute(proposal(), messages=history())
        payload = outcome.messages[0].result
        self.assertEqual(payload["data_status"], "source_error")
        self.assertEqual(payload["calculation_status"], "unavailable")
        self.assertFalse(payload["synthesis_allowed"])
        self.assertEqual(len(table(payload, "indicators")), 8)
        self.assertEqual(table(payload, "indicators")[0]["parameters"], {"window": 20})
        self.assertEqual(table(payload, "latest_fields"), [])
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertTrue(any("cannot_restore_precision" in warning for warning in payload["warnings"]))
        factory.assert_called_once_with("000001.SZ", START, START + timedelta(days=29))
        fake_source.read_daily_bars.assert_called_once()

    def test_explicit_online_source_selects_only_its_declared_adapter(self):
        source = SimpleNamespace(source_name="akshare_eastmoney", adjustment="unadjusted",
                                 read_daily_bars=Mock(return_value=make_bars(30)))
        with patch("trading_companion.research_tool.AkshareDailyBarSource", return_value=source) as selected, patch(
                "trading_companion.research_tool.TencentDailyBarSource") as other:
            outcome = ResearchToolExecutor().execute(proposal(arguments(source="akshare_eastmoney")),
                                                       messages=history())
        self.assertEqual(outcome.messages[0].result["source"], "akshare_eastmoney")
        selected.assert_called_once()
        other.assert_not_called()

    def test_atomic_attempt_budget_allows_only_one_concurrent_analysis(self):
        executor = fixture_executor()
        entered, release = Event(), Event()
        def read_fixture(unused):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release fixture")
            return make_bars(30)
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=read_fixture) as read:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(executor.execute, proposal(call_id="first"), messages=history())
                self.assertTrue(entered.wait(2))
                try:
                    second = pool.submit(executor.execute, proposal(call_id="second"), messages=history()).result(2)
                    self.assertEqual(second.messages[0].result["code"], "analysis_budget_exhausted")
                finally:
                    release.set()
                self.assertEqual(first.result(2).status, "executed")
        self.assertEqual(executor.analyses_used, 1)
        read.assert_called_once()


class ResearchProjectionTests(unittest.TestCase):
    def execute_fixture(self, bars, specs=None, messages=None):
        values = arguments(len(bars))
        if specs is not None:
            values["indicators"] = specs
        response = proposal(values)
        executor = fixture_executor()
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars) as read:
            outcome = executor.execute(response, messages=history() if messages is None else messages)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(executor.analyses_used, 1)
        return response, executor, outcome

    def test_default_eight_with_short_and_long_history_fit_complete_fake_round_trips(self):
        for length in (30, 120, 365):
            with self.subTest(length=length):
                response, _, outcome = self.execute_fixture(make_bars(length))
                self.assertEqual(outcome.status, "executed")
                self.assertLessEqual(outcome.projection_metrics["ascii_characters"], 8000)
                self.assertLessEqual(outcome.projection_metrics["nodes"], 1600)
                self.assertEqual(len(table(outcome.messages[0].result, "indicators")), 8)
                final = FakeToolModel([ToolModelResponse("complete", "Offline script completed.")]).complete(
                    [*history(), response.as_message(), *outcome.messages], tools=(RESEARCH_TOOL_DEFINITION,))
                self.assertTrue(final.is_complete)
                self.assertTrue(final.is_mock)

    def test_common_twelve_instances_fit_without_dropping_any_field_or_relation(self):
        for length in (30, 120, 365):
            with self.subTest(length=length):
                response, _, outcome = self.execute_fixture(make_bars(length), COMMON_TWELVE)
                self.assertEqual(outcome.status, "executed")
                payload = outcome.messages[0].result
                self.assertLessEqual(outcome.projection_metrics["ascii_characters"], 10000)
                self.assertLessEqual(outcome.projection_metrics["nodes"], 2000)
                self.assertEqual(len(table(payload, "indicators")), 12)
                self.assertEqual(len(table(payload, "relations")), len(outcome.full_result.relations))
                validate_tool_messages([*history(), response.as_message(), *outcome.messages],
                                       tools=(RESEARCH_TOOL_DEFINITION,))

    def test_extreme_twelve_means_returns_explicit_limit_and_keeps_complete_local_result(self):
        specs = [{"kind": "sma", "window": i} for i in range(1, 13)]
        _, executor, outcome = self.execute_fixture(make_bars(120), specs)
        self.assertEqual(outcome.status, "limited")
        self.assertEqual(outcome.messages[0].result["status"], "result_too_large")
        self.assertFalse(outcome.messages[0].result["synthesis_allowed"])
        self.assertEqual(len(outcome.full_result.indicators), 12)
        self.assertEqual(len(outcome.full_result.relations), 168)
        self.assertTrue(outcome.fact_aliases)
        again = executor.execute(proposal(call_id="cannot_retry"), messages=history())
        self.assertEqual(again.messages[0].result["code"], "analysis_budget_exhausted")

    def test_thirty_default_fields_keep_partial_availability_and_exact_evidence_dependencies(self):
        _, _, outcome = self.execute_fixture(make_bars(30))
        payload = outcome.messages[0].result
        states = {item["id"]: item for item in table(payload, "states")}
        specs = {item["id"]: item for item in table(payload, "indicators")}
        for row in table(payload, "latest_fields"):
            status = states[row["state"]]
            if row["indicator"] == "i1":
                self.assertEqual((status["status"], row["missing"]), ("insufficient_history", 30))
            if specs[row["indicator"]]["kind"] == "macd" and row["field"] in ("dea", "histogram"):
                self.assertEqual((status["status"], row["missing"]), ("insufficient_history", 4))
        aliases = outcome.fact_aliases
        inverse = {fact.fact_id: alias for alias, fact in aliases.items()}
        wire_numeric = {item["fact"]: item for item in table(payload, "numeric_facts")}
        for alias, fact in aliases.items():
            self.assertEqual(fact.request_id, payload["request_id"])
            if not fact.dependencies:
                self.assertEqual(wire_numeric[alias]["value"], fact.value)
                self.assertEqual(payload["dates"][wire_numeric[alias]["date_index"]], fact.trade_date.isoformat())
        for row in table(payload, "relations"):
            self.assertIn(row["field"], RELATION_RULE_CATALOG)
            if row["fact"] is not None:
                fact = aliases[row["fact"]]
                self.assertEqual(row["dependencies"], [inverse[dep] for dep in fact.dependencies])
                self.assertTrue(all(dep in aliases for dep in row["dependencies"]))
                self.assertEqual(row["value"], fact.value)
        for row in table(payload, "components"):
            original = next(item for item in outcome.full_result.indicators[int(row["indicator"][1:])].components
                            if item.name == row["component"])
            self.assertEqual(SEED_METHOD_CATALOG[row["seed"]], original.seed_method)
            self.assertEqual(row["updates"], original.recursive_updates)
        self.assertTrue(payload["retrieved_at"])
        self.assertEqual(payload["data_authenticity"], "synthetic")
        self.assertEqual(payload["price_adjustment"], "unknown")

    def test_zero_baseline_reason_and_valid_zero_are_preserved_in_projection(self):
        bars = [DailyBar("000001.SZ", START + timedelta(days=i), 10, 10, 10, 10, value, 0)
                for i, value in enumerate((0, 0, 0, 30))]
        _, _, outcome = self.execute_fixture(bars, [{"kind": "volume", "window": 3}])
        payload = outcome.messages[0].result
        states = {row["id"]: row for row in table(payload, "states")}
        relative = next(row for row in table(payload, "latest_fields") if row["field"] == "relative_volume")
        self.assertEqual(states[relative["state"]]["reason"], "zero_prior_volume_baseline")
        self.assertEqual(states[relative["state"]]["status"], "undefined")
        self.assertIsNone(relative["fact"])
        self.assertEqual(relative["missing"], 0)
        prior = next(row for row in table(payload, "latest_fields") if row["field"] == "previous_volume_ma")
        self.assertEqual(outcome.fact_aliases[prior["fact"]].value, 0)

    def test_alias_registry_is_detached_and_request_scope_is_explicit(self):
        _, _, outcome = self.execute_fixture(make_bars(30))
        aliases = outcome.fact_aliases
        indicator_fact = next(item for item in aliases.values() if item.parameters)
        indicator_fact.parameters.clear()
        self.assertTrue(any(item.parameters for item in outcome.fact_aliases.values()))
        self.assertIn("request_id-local", outcome.messages[0].result["alias_scope"])
        projected = outcome.messages[0].result
        projected["tables"].clear()
        self.assertTrue(outcome.messages[0].result["tables"])

    def test_empty_and_invalid_source_results_preserve_requests_and_distinct_data_states(self):
        for records, expected in (([], "no_data"), ([None], "invalid_data")):
            with self.subTest(expected=expected), patch.object(
                    CsvDailyBarSource, "read_daily_bars", return_value=records) as read:
                executor = fixture_executor()
                outcome = executor.execute(proposal(), messages=history())
            payload = outcome.messages[0].result
            self.assertEqual(payload["data_status"], expected)
            self.assertEqual(payload["calculation_status"], "unavailable")
            self.assertFalse(payload["synthesis_allowed"])
            self.assertEqual(len(table(payload, "indicators")), 8)
            self.assertEqual(table(payload, "latest_fields"), [])
            self.assertEqual(table(payload, "relations"), [])
            self.assertEqual(executor.analyses_used, 1)
            read.assert_called_once()

    def test_giant_raw_volume_returns_controlled_transport_limit_without_losing_local_result(self):
        bars = make_bars(4, volume=2**20000)
        _, _, outcome = self.execute_fixture(bars, [{"kind": "sma", "window": 3}])
        self.assertEqual(outcome.status, "limited")
        self.assertEqual(outcome.messages[0].result["status"], "result_too_large")
        self.assertEqual(outcome.full_result.bars[-1].volume_shares, 2**20000 + 3)
        self.assertEqual(outcome.projection_metrics["serialization_failed"], 1)
        json.dumps(outcome.messages[0].to_dict(), allow_nan=False)


class ResearchHistoryAndDemoTests(unittest.TestCase):
    def test_maximum_fallback_must_fit_history_before_any_attempt(self):
        response = proposal()
        item = response.tool_calls[0]
        base = [ToolMessage("system", "x"), *history()]
        short_error = _error(item, "invalid_arguments")
        total = (len(json.dumps(RESEARCH_TOOL_DEFINITION.to_dict(), ensure_ascii=True))
                 + sum(len(json.dumps(message.to_dict(), ensure_ascii=True))
                       for message in [*base, response.as_message(), short_error]))
        base[0] = ToolMessage("system", "x" * (20000 - total + 1))
        validate_tool_messages([*base, response.as_message(), short_error], tools=(RESEARCH_TOOL_DEFINITION,))
        with self.assertRaises(ModelRequestError):
            validate_tool_messages([*base, response.as_message(), _too_large(item, "r" * 32, 12, 366)],
                                   tools=(RESEARCH_TOOL_DEFINITION,))
        executor = fixture_executor()
        with patch.object(CsvDailyBarSource, "read_daily_bars") as read:
            with self.assertRaises(ModelRequestError):
                executor.execute(response, messages=base)
        self.assertEqual(executor.analyses_used, 0)
        read.assert_not_called()

    def test_full_projection_that_exceeds_remaining_history_uses_reserved_safe_fallback(self):
        response = proposal()
        messages = [ToolMessage("system", "x" * 10000), *history()]
        executor = fixture_executor()
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=make_bars(30)) as read:
            outcome = executor.execute(response, messages=messages)
        self.assertEqual(outcome.status, "limited")
        self.assertEqual(outcome.messages[0].result["status"], "result_too_large")
        self.assertIsNotNone(outcome.full_result)
        self.assertEqual(executor.analyses_used, 1)
        read.assert_called_once()
        validate_tool_messages([*messages, response.as_message(), *outcome.messages], tools=(RESEARCH_TOOL_DEFINITION,))

    def test_demo_runs_actual_csv_indicators_but_only_fake_model_and_explicit_synthetic_data(self):
        with patch("requests.get", side_effect=AssertionError("Unexpected HTTP")) as get, patch(
                "requests.post", side_effect=AssertionError("Unexpected HTTP")) as post:
            payload = run_demo()
        self.assertFalse(payload["network_used"])
        self.assertTrue(payload["executed_tools"])
        self.assertFalse(payload["real_provider_tool_validated"])
        self.assertEqual(payload["analyses_used"], 1)
        view = payload["tool_result"]["result"]
        self.assertEqual(len(table(view, "indicators")), 3)
        field = next(row for row in table(view, "latest_fields") if row["indicator"] == "i0")
        fact = next(row for row in table(view, "numeric_facts") if row["fact"] == field["fact"])
        self.assertEqual(fact["value"], (10.10 + 10.00 + 10.40) / 3)
        self.assertEqual(view["data_authenticity"], "synthetic")
        self.assertTrue(payload["final_response"]["is_mock"])
        self.assertEqual(payload["final_response"]["status"], "complete")
        get.assert_not_called()
        post.assert_not_called()
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "offline_synthetic_research_demo")


if __name__ == "__main__":
    unittest.main()
