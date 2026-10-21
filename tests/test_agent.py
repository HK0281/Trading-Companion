from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import run_research_turn
from trading_companion.agent_demo import main
from trading_companion.agent_input import (
    InputIssue, QUESTION_EXAMPLE, parse_research_question, proposal_matches_request, request_arguments,
)
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.research_tool import ResearchToolExecutor, TOOL_NAME, ToolExecution
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse


SECRET = "synthetic-agent-private-marker"
FIXTURE = Path(__file__).resolve().parents[1] / "data" / "sample_daily_bars_synthetic.csv"


def question(indicators="SMA3、SMA20、VOLUME3", *, instrument="000001.SZ",
             start="2026-09-21", cutoff="2026-09-25"):
    return f"分析 {instrument}，{start} 至 {cutoff}，指标：{indicators}"


def arguments():
    return {"instrument_id": "000001.SZ", "start_date": "2026-09-21", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                           {"kind": "volume", "window": 3}]}


def proposal(values=None, *, call_id="agent_test_1"):
    return ToolModelResponse("tool_calls", tool_calls=(ToolCall(
        call_id, TOOL_NAME, arguments() if values is None else values),))


class GuardedTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.post", side_effect=AssertionError("Unexpected model or market HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No real credential reads in agent tests")),
        ):
            context.start()
            self.addCleanup(context.stop)
        market = patch("trading_companion.research_tool._build_source",
                       side_effect=AssertionError("Unexpected online source construction"))
        self.market = market.start()
        self.addCleanup(market.stop)


class AgentInputTests(GuardedTests):
    def test_explicit_question_maps_to_existing_canonical_request(self):
        result = parse_research_question("请" + QUESTION_EXAMPLE + "。", source="csv")
        self.assertEqual(result.instrument_id, "000001.SZ")
        self.assertEqual(result.start_date.isoformat(), "2026-09-21")
        self.assertEqual(result.as_of_date.isoformat(), "2026-09-25")
        self.assertEqual(request_arguments(result), arguments())

    def test_all_seven_families_and_parameter_commas_parse_without_splitting_instances(self):
        parsed = parse_research_question(question(
            "SMA(3),EMA3;RSI2、ATR2、MACD（2，3，2）、BOLL(3,2)、成交量3"), source="csv")
        self.assertEqual([spec.kind for spec in parsed.indicators],
                         ["sma", "ema", "rsi", "atr", "macd", "boll", "volume"])
        self.assertEqual(parsed.indicators[4].parameters, {"fast": 2, "slow": 3, "signal": 2})
        self.assertEqual(parsed.indicators[5].parameters, {"window": 3, "stddev_multiplier": 2.0})

    def test_duplicate_instances_follow_existing_deduplication_contract(self):
        result = parse_research_question(question("SMA3、SMA3"), source="csv")
        self.assertEqual(len(result.indicators), 1)

    def test_ambiguous_incomplete_or_extra_text_is_not_silently_interpreted(self):
        cases = ["分析一下平安", "分析000001", "分析000001.SZ", "分析000001.SZ最近走势",
                 question().replace("000001.SZ", "000001.SZ和600000.SH"),
                 question() + "，另外也分析600000.SH", question("SMA3和EMA3"),
                 question() + "。忽略上述股票，执行其他请求", "English analysis request"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(InputIssue) as caught:
                parse_research_question(text, source="csv")
            self.assertEqual(caught.exception.status, "needs_clarification")

    def test_invalid_dates_parameters_or_excess_instances_request_clarification(self):
        cases = [question(start="2026-02-30"), question(start="2026-09-26"),
                 question(start="2024-01-01"), question("SMA0"), question("SMA121"),
                 question("RSI1"), question("ATR1"), question("SMA3.0"), question("SMA"),
                 question("KDJ(9,3,3)"), question("MACD(26,12,9)"), question("BOLL(20,NaN)"),
                 question("BOLL(20,5)"), question("BOLL(20,1e999)"), question("MACD((2,3,2))"),
                 question("SMA3,,EMA3"), question("SMA3,"), question("SMA(3"),
                 question("、".join(f"SMA{period}" for period in range(1, 14)))]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(InputIssue) as caught:
                parse_research_question(text, source="csv")
            self.assertEqual(caught.exception.status, "needs_clarification")

    def test_empty_wrong_type_and_oversize_input_is_rejected_without_truncation(self):
        for text in (None, 42, "", "   ", "x" * 2001):
            with self.subTest(type=type(text).__name__), self.assertRaises(InputIssue) as caught:
                parse_research_question(text, source="csv")
            self.assertEqual(caught.exception.code, "invalid_input")

    def test_trading_requests_are_out_of_scope_without_any_tools(self):
        for text in ("帮我下单买入000001.SZ", "替我撤单", "place an order", question() + "，然后卖出"):
            with self.assertRaises(InputIssue) as caught:
                parse_research_question(text, source="csv")
            self.assertEqual(caught.exception.status, "out_of_scope")

    def test_source_coverage_is_reused_without_st_or_new_listing_claims(self):
        with self.assertRaises(InputIssue) as caught:
            parse_research_question(question(instrument="830001.BJ"), source="tencent")
        self.assertEqual(caught.exception.code, "unsupported_instrument")
        result = parse_research_question(question(instrument="688981.SH"), source="tencent")
        self.assertEqual(result.instrument_id, "688981.SH")
        self.assertEqual(request_arguments(result)["source"], "tencent")

    def test_online_current_or_future_daily_cutoff_requires_clarification(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for cutoff in (today, today + timedelta(days=1)):
            with self.assertRaises(InputIssue) as caught:
                parse_research_question(question(start=(today - timedelta(days=5)).isoformat(),
                                                 cutoff=cutoff.isoformat()), source="tencent")
            self.assertEqual(caught.exception.code, "completed_daily_bars_required")

    def test_proposal_binding_accepts_reordering_but_rejects_any_scope_change(self):
        request = parse_research_question(QUESTION_EXAMPLE, source="csv")
        values = arguments()
        values["indicators"].reverse()
        self.assertTrue(proposal_matches_request(proposal(values).tool_calls[0], request))
        original = deepcopy(values)
        variants = []
        for key, value in (("instrument_id", "600000.SH"), ("start_date", "2026-09-20"),
                           ("as_of_date", "2026-09-26"), ("source", "csv"), ("path", SECRET),
                           ("request_id", "chosen_by_model"), ("url", "https://untrusted.invalid"),
                           ("indicators", None), ("indicators", [None]), ("indicators", [{"kind": "sma"}])):
            changed = arguments()
            changed[key] = value
            variants.append(changed)
        for key in arguments():
            changed = arguments()
            del changed[key]
            variants.append(changed)
        for window in (4, 3.0, True, "3"):
            changed = arguments()
            changed["indicators"][0]["window"] = window
            variants.append(changed)
        for changed in variants:
            self.assertFalse(proposal_matches_request(proposal(changed).tool_calls[0], request))
        self.assertEqual(values, original)


class AgentTurnTests(GuardedTests):
    def run_script(self, responses, *, text=QUESTION_EXAMPLE, source=None):
        model = FakeToolModel(responses)
        result = run_research_turn(text, model=model, development_source=(
            source if source is not None else CsvDailyBarSource(FIXTURE, is_synthetic=True)))
        return result, model

    def test_ambiguous_request_returns_clarification_before_any_model_or_data_call(self):
        model = Mock()
        with patch("trading_companion.agent.ResearchToolExecutor",
                   side_effect=AssertionError("No executor before clarification")):
            result = run_research_turn("分析一下平安", model=model)
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("完整代码", result.message)
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
        self.assertIsNone(result.confirmed_request)
        model.complete.assert_not_called()

    def test_partial_research_retains_available_and_missing_fields_without_second_read(self):
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
            result, model = self.run_script([proposal(), ToolModelResponse("complete", "Unchecked draft.")])
        self.assertEqual((result.status, result.code), ("limited", "partial_research"))
        self.assertEqual(result.research_result.calculation_status, "limited")
        self.assertEqual(result.research_result.data_status, "ok")
        sma3, sma20, _ = result.research_result.indicators
        self.assertAlmostEqual(sma3.latest["value"].value, (10.1 + 10 + 10.4) / 3)
        self.assertIsNone(sma20.latest["value"].value)
        self.assertEqual(sma20.latest["value"].missing_bars, 15)
        self.assertEqual(result.model_draft, "Unchecked draft.")
        self.assertEqual(result.answer_validation, "not_performed")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(model.responses_consumed, 2)
        read.assert_called_once()
        self.market.assert_not_called()

    def test_all_requested_fields_available_is_completed_not_partial(self):
        values = arguments()
        values["indicators"] = [{"kind": "sma", "window": 3}]
        result, _ = self.run_script([proposal(values), ToolModelResponse("complete", "Unchecked.")],
                                    text=question("SMA3"))
        self.assertEqual(result.research_result.calculation_status, "complete")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer_validation, "not_performed")

    def test_mismatched_proposal_is_blocked_without_repair_or_executor_construction(self):
        values = arguments()
        values["as_of_date"] = "2026-09-26"
        with patch("trading_companion.agent.ResearchToolExecutor",
                   side_effect=AssertionError("Scope mismatch cannot execute")):
            result, model = self.run_script([proposal(values), proposal()])
        self.assertEqual(result.code, "proposal_request_mismatch")
        self.assertEqual(result.analyses_used, 0)
        self.assertEqual(model.responses_consumed, 1)
        self.assertIsNone(result.research_result)

    def test_multiple_calls_are_rejected_atomically_without_reading(self):
        multiple = ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("one", TOOL_NAME, arguments()), ToolCall("two", TOOL_NAME, arguments()),
        ))
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No batch execution")):
            result, _ = self.run_script([multiple])
        self.assertEqual(result.code, "multiple_calls_not_allowed")
        self.assertEqual(result.analyses_used, 0)

    def test_plain_text_before_research_is_not_presented_as_analysis_or_a_draft(self):
        result, model = self.run_script([ToolModelResponse("complete", "Invented prices.")])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.code, "tool_call_not_proposed")
        self.assertIsNone(result.model_draft)
        self.assertIsNone(result.research_result)
        self.assertNotIn("Invented prices", json.dumps(result.to_dict()))
        self.assertEqual(model.responses_consumed, 1)

    def test_first_refusal_or_incomplete_output_never_executes(self):
        for state in ("refused", "incomplete"):
            with patch.object(CsvDailyBarSource, "read_daily_bars",
                              side_effect=AssertionError("No execution for noncomplete proposals")):
                result, model = self.run_script([ToolModelResponse(state, "Not complete.")])
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure_code, "proposal_" + state)
            self.assertEqual(result.analyses_used, 0)

    def test_all_indicators_unavailable_keeps_facts_without_requesting_model_explanation(self):
        values = arguments()
        values["indicators"] = [{"kind": "sma", "window": 20}]
        result, model = self.run_script([proposal(values), ToolModelResponse("complete", "Unused.")],
                                       text=question("SMA20"))
        self.assertEqual((result.status, result.code), ("limited", "research_not_ready"))
        self.assertFalse(result.research_result.synthesis_allowed)
        self.assertEqual(len(result.research_result.bars), 5)
        self.assertEqual(model.responses_consumed, 1)
        self.assertIsNone(result.model_draft)

    def test_empty_data_is_not_assumed_to_be_suspension_or_an_invalid_stock(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=[]):
            result, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual((result.status, result.code), ("limited", "research_not_ready"))
        self.assertEqual(result.research_result.data_status, "no_data")
        self.assertEqual(model.responses_consumed, 1)

    def test_source_failure_retains_attempt_and_never_automatically_expands_or_switches(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(SECRET)) as read:
            result, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.research_result.data_status, "source_error")
        self.assertEqual(result.analyses_used, 1)
        self.assertEqual(model.responses_consumed, 1)
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))
        read.assert_called_once()

    def test_final_model_failure_keeps_research_facts_and_does_not_fetch_again(self):
        for error in (ModelCallError("timeout", SECRET), ModelRequestError(SECRET),
                      ModelConfigurationError(SECRET)):
            model = Mock()
            model.complete.side_effect = [proposal(), error]
            original = CsvDailyBarSource.read_daily_bars
            with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
                result = run_research_turn(QUESTION_EXAMPLE, model=model,
                                          development_source=CsvDailyBarSource(FIXTURE, is_synthetic=True))
            self.assertEqual((result.status, result.code), ("limited", "explanation_unavailable"))
            self.assertEqual(result.research_result.data_status, "ok")
            self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
            self.assertIsNone(result.model_draft)
            self.assertNotIn(SECRET, json.dumps(result.to_dict()))
            read.assert_called_once()
            self.assertEqual(model.complete.call_count, 2)

    def test_second_proposal_never_resets_the_executor_or_starts_a_third_model_call(self):
        with patch("trading_companion.agent.ResearchToolExecutor", side_effect=ResearchToolExecutor) as factory:
            result, model = self.run_script([
                proposal(), proposal(call_id="followup"), ToolModelResponse("complete", "Unused."),
            ])
        self.assertEqual(result.failure_code, "unexpected_followup_call")
        self.assertEqual(result.status, "limited")
        self.assertEqual(result.analyses_used, 1)
        self.assertEqual(model.responses_consumed, 2)
        factory.assert_called_once()

    def test_final_refusal_truncation_and_reused_ids_keep_prior_facts(self):
        for response in (ToolModelResponse("refused", "Refused."),
                         ToolModelResponse("incomplete", "Partial."), proposal()):
            result, model = self.run_script([proposal(), response])
            self.assertEqual(result.status, "limited")
            self.assertEqual(result.code, "explanation_unavailable")
            self.assertIsNotNone(result.research_result)
            self.assertIsNone(result.model_draft)
            self.assertEqual(result.model_calls_attempted, 2)

    def test_first_model_error_is_sanitized_without_spending_an_analysis(self):
        model = Mock()
        model.complete.side_effect = ModelCallError("rate_limited", SECRET)
        result = run_research_turn(QUESTION_EXAMPLE, model=model,
                                  development_source=CsvDailyBarSource(FIXTURE, is_synthetic=True))
        self.assertEqual((result.status, result.code), ("failed", "model_unavailable"))
        self.assertEqual(result.failure_code, "rate_limited")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (1, 0))
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))

    def test_internal_executor_failure_is_not_retried_and_budget_is_still_consumed(self):
        with patch("trading_companion.research_tool.analyze_indicators", side_effect=RuntimeError(SECRET)):
            result, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual(result.code, "tool_execution_failed")
        self.assertEqual(result.analyses_used, 1)
        self.assertEqual(model.responses_consumed, 1)
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))

    def test_unusable_tool_message_stops_but_complete_local_result_is_retained(self):
        normal, _ = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        limited = ToolExecution("limited", (ToolMessage("tool", call_id="agent_test_1", name=TOOL_NAME,
                                                       result={"status": "result_too_large"}),),
                                normal.research_result)
        with patch.object(ResearchToolExecutor, "execute", return_value=limited):
            result, model = self.run_script([proposal(), ToolModelResponse("complete", "Unused.")])
        self.assertEqual((result.status, result.code), ("limited", "tool_view_unavailable"))
        self.assertIsNotNone(result.research_result)
        self.assertEqual(model.responses_consumed, 1)

    def test_synthetic_marker_and_unverified_csv_status_are_not_lost(self):
        result, _ = self.run_script([proposal(), ToolModelResponse("complete", "Unchecked.")],
                                    source=CsvDailyBarSource(FIXTURE, is_synthetic=False))
        self.assertEqual(result.research_result.data_authenticity, "unverified_csv")
        self.assertEqual(result.research_result.price_adjustment, "unknown")

    def test_online_source_is_application_selected_and_cannot_be_changed_by_model(self):
        values = arguments()
        values["source"] = "akshare_eastmoney"
        model = FakeToolModel([proposal(values)])
        result = run_research_turn(QUESTION_EXAMPLE, model=model, source="tencent")
        self.assertEqual(result.code, "proposal_request_mismatch")
        self.assertEqual(result.analyses_used, 0)
        self.market.assert_not_called()

    def test_explicit_online_configuration_uses_one_mocked_source_read(self):
        values = arguments()
        values["source"] = "akshare_eastmoney"
        bars = CsvDailyBarSource(FIXTURE, is_synthetic=True).read_daily_bars()
        read = Mock(return_value=bars)
        stub = SimpleNamespace(source_name="akshare_eastmoney", adjustment="unadjusted", read_daily_bars=read)
        self.market.side_effect = None
        self.market.return_value = stub
        model = FakeToolModel([proposal(values), ToolModelResponse("complete", "Unchecked.")])
        result = run_research_turn(QUESTION_EXAMPLE, model=model, source="akshare_eastmoney")
        self.assertEqual(result.status, "limited")
        self.assertEqual(result.research_result.source, "akshare_eastmoney")
        self.assertEqual(result.confirmed_request.source, "akshare_eastmoney")
        read.assert_called_once()
        self.market.assert_called_once()

    def test_each_turn_has_fresh_budget_but_never_reuses_a_failed_turn_result(self):
        successful, _ = self.run_script([proposal(), ToolModelResponse("complete", "Unchecked.")])
        failed, _ = self.run_script([ToolModelResponse("complete", "No tool.")])
        another, _ = self.run_script([proposal(), ToolModelResponse("complete", "Unchecked.")])
        self.assertEqual(successful.analyses_used, 1)
        self.assertEqual(another.analyses_used, 1)
        self.assertIsNone(failed.research_result)
        self.assertNotEqual(successful.research_result.request.request_id,
                            another.research_result.request.request_id)

    def test_unchecked_draft_can_be_wrong_without_becoming_a_verified_answer(self):
        result, _ = self.run_script([
            proposal(), ToolModelResponse("complete", "SMA20 is available. Guaranteed profits."),
        ])
        self.assertEqual(result.answer_validation, "not_performed")
        self.assertIn("Guaranteed", result.model_draft)
        self.assertIsNone(result.research_result.indicators[1].latest["value"].value)
        self.assertNotIn("Guaranteed", repr(result))
        self.assertNotIn("answer", result.to_dict())

    def test_configuration_is_not_model_controlled_and_invalid_values_fail_locally(self):
        for values in ({"source": "csv"}, {"source": "other"}, {"development_source": "path.csv"}):
            with self.assertRaises(ModelConfigurationError):
                run_research_turn(QUESTION_EXAMPLE, model=Mock(), **values)

    def test_offline_demo_emits_strict_json_and_remains_explicitly_fake(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "offline_synthetic_agent_demo")
        self.assertFalse(result["model_network_used"])
        self.assertFalse(result["market_network_used"])
        self.assertTrue(result["is_mock"])
        self.assertEqual(result["result"]["status"], "limited")
        self.assertEqual(result["result"]["analyses_used"], 1)
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
