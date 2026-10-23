from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from trading_companion.agent import AgentTurnResult, run_research_turn
from trading_companion.agent_input import parse_research_question, request_arguments
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.report_demo import main
from trading_companion.research_report import build_research_report
from trading_companion.research_tool import ResearchToolExecutor, TOOL_NAME
from trading_companion.session_agent_demo import run_demo as run_session_demo
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolMessage, ToolModelResponse


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "sample_daily_bars_synthetic.csv"
QUESTION = "分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、SMA20、VOLUME3"
PRIVATE_MARKER = "private-provider-detail-not-for-display"


class ResearchReportTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market or directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No model HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No real credential reads")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.request = parse_research_question(QUESTION, source="csv")
        self.result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), self.request,
                                         allow_development_csv=True)

    def report(self, result=None, request=None):
        return build_research_report(self.result if result is None else result,
                                     expected_request=self.request if request is None else request)

    def changed_latest(self, name="value", index=0, **changes):
        indicators = list(self.result.indicators)
        indicator = indicators[index]
        fields = {**indicator.latest, name: replace(indicator.latest[name], **changes)}
        indicators[index] = replace(indicator, series=(*indicator.series[:-1],
                                                      replace(indicator.series[-1], fields=fields)))
        return replace(self.result, indicators=tuple(indicators))

    def assert_blocked(self, result):
        report = self.report(result)
        self.assertEqual((report.status, report.code), ("blocked", "report_evidence_mismatch"))
        self.assertEqual(report.fields, ())
        self.assertNotIn("收盘价：", report.text)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(report.to_dict()))

    def agent_result(self, final, *, text=QUESTION):
        request = parse_research_question(text, source="csv")
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall(
            "report_test", TOOL_NAME, request_arguments(request)),))
        model = Mock()
        model.complete.side_effect = [proposal, final]
        result = run_research_turn(text, model=model,
                                   development_source=CsvDailyBarSource(FIXTURE, is_synthetic=True))
        return result, model

    def test_latest_fields_are_request_bound_and_cite_full_server_facts(self):
        report = self.report()
        self.assertEqual((report.status, report.code), ("limited", "deterministic_facts"))
        self.assertEqual(report.request_id, self.request.request_id)
        self.assertEqual(len(report.fields), 8)
        index = {fact.fact_id: fact for fact in self.result.facts}
        for item in report.fields:
            self.assertEqual(item.trade_date, "2026-09-25")
            if item.status == "available":
                fact = index[item.fact_id]
                self.assertEqual((fact.request_id, fact.instrument_id),
                                 (self.request.request_id, "000001.SZ"))
                self.assertEqual((fact.value, fact.unit), (item.value, item.unit))
            else:
                self.assertIsNone(item.value)
                self.assertIsNone(item.fact_id)

    def test_context_shows_requested_and_actual_dates_source_and_requested_indicators(self):
        report = self.report()
        for value in ("000001.SZ", "开发CSV", "2026-09-21 至 2026-09-25", "实际最新交易日：2026-09-25",
                      "共5条日线", "SMA(3)", "SMA(20)", "量能(3)"):
            self.assertIn(value, report.text)
        self.assertNotIn("RSI", report.text)

    def test_synthetic_and_unverified_csv_are_prominently_distinguished(self):
        self.assertTrue(self.report().text.startswith("人工样例 / 非真实行情"))
        unverified = replace(self.result, data_authenticity="unverified_csv")
        self.assertTrue(self.report(unverified).text.startswith("开发CSV / 真实性未经验证"))

    def test_missing_history_stays_null_and_is_not_displayed_as_zero(self):
        report = self.report()
        missing = next(item for item in report.fields if item.indicator_id == "sma(window=20)")
        self.assertEqual((missing.status, missing.value, missing.missing_bars),
                         ("insufficient_history", None, 15))
        self.assertIn("SMA(20)：历史记录不足，还缺15条记录", report.text)
        self.assertNotIn("SMA(20)：0", report.text)

    def test_complete_seven_family_report_preserves_units_and_parameter_names(self):
        request = parse_research_question(QUESTION.replace("SMA3、SMA20、VOLUME3",
            "SMA3、EMA3、MACD(2,3,2)、RSI2、BOLL(3,2)、ATR2、VOLUME2"), source="csv")
        result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), request,
                                    allow_development_csv=True)
        report = self.report(result, request)
        self.assertEqual(report.status, "complete")
        for label in ("EMA(3)", "MACD(2,3,2) DIF", "MACD(2,3,2) DEA", "MACD(2,3,2) 柱值",
                      "RSI(2)", "BOLL(3,2) 上轨", "ATR(2)", "相对此前均量倍率"):
            self.assertIn(label, report.text)
        self.assertEqual({item.unit for item in report.fields},
                         {"CNY/share", "shares", "index_points", "multiple"})

    def test_text_rounding_does_not_change_structured_precision(self):
        report = self.report()
        field = next(item for item in report.fields if item.indicator_id == "sma(window=3)")
        original = self.result.indicators[0].latest["value"].value
        self.assertEqual(field.value, original)
        self.assertEqual(report.to_dict()["fields"][2]["value"], original)
        self.assertIn("显示舍入", report.text)

    def test_all_computed_fields_unavailable_still_displays_price_without_trend(self):
        request = replace(self.request, indicators=(IndicatorSpec("sma", window=20),))
        result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), request,
                                    allow_development_csv=True)
        report = self.report(result, request)
        self.assertEqual(report.status, "limited")
        self.assertIn("收盘价：10.4 元/股", report.text)
        self.assertIn("不生成综合走势判断", report.text)

    def test_zero_prior_volume_baseline_is_undefined_not_a_zero_ratio(self):
        bars = [replace(bar, volume_shares=0, amount_cny=0) for bar in self.result.bars]
        bars[-1] = replace(bars[-1], volume_shares=50, amount_cny=500)
        request = replace(self.request, indicators=(IndicatorSpec("volume", window=3),))
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), request,
                                        allow_development_csv=True)
        report = self.report(result, request)
        ratio = next(item for item in report.fields if item.field == "relative_volume")
        self.assertEqual((ratio.status, ratio.value), ("undefined", None))
        self.assertIn("未定义（此前均量为零）", report.text)

    def test_numeric_failure_keeps_other_facts_without_mislabeling_as_short_history(self):
        with patch("trading_companion.multi_analysis.calculations.sma_close", side_effect=ValueError(PRIVATE_MARKER)):
            result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), self.request,
                                        allow_development_csv=True)
        report = self.report(result)
        self.assertEqual(report.status, "limited")
        self.assertIn("SMA(3)：数值计算失败", report.text)
        self.assertIn("收盘价：10.4", report.text)
        self.assertNotIn(PRIVATE_MARKER, report.text)

    def test_wrong_request_id_stock_dates_source_or_indicators_cannot_reuse_a_report(self):
        requests = [replace(self.request, request_id="another-turn"),
                    replace(self.request, instrument_id="600000.SH"),
                    replace(self.request, as_of_date=date(2026, 9, 26)),
                    replace(self.request, source="tencent"),
                    replace(self.request, indicators=(IndicatorSpec("rsi", window=2),))]
        for request in requests:
            with self.subTest(request=request):
                report = self.report(request=request)
                self.assertEqual(report.status, "blocked")
                self.assertEqual(report.fields, ())

    def test_equivalent_proposal_order_is_allowed_but_report_uses_the_confirmed_order(self):
        values = request_arguments(self.request)
        values["indicators"].reverse()
        model = FakeToolModel([
            ToolModelResponse("tool_calls", tool_calls=(ToolCall("reordered", TOOL_NAME, values),)),
            ToolModelResponse("complete", "Unchecked draft"),
        ])
        result = run_research_turn(QUESTION, model=model,
                                   development_source=CsvDailyBarSource(FIXTURE, is_synthetic=True))
        self.assertEqual(result.safe_answer.status, "limited")
        self.assertEqual([item.indicator_id for item in result.safe_answer.fields[2:4]],
                         ["sma(window=3)", "sma(window=20)"])
        self.assertEqual(result.confirmed_request.request_id, result.research_result.request.request_id)

    def test_result_source_and_authenticity_must_match_the_configured_mode(self):
        for changes in ({"source": "tencent"}, {"data_authenticity": "provider_reported"},
                        {"price_adjustment": "unadjusted"}):
            with self.subTest(changes=changes):
                self.assert_blocked(replace(self.result, **changes))

    def test_missing_or_duplicate_evidence_ids_block_the_entire_numeric_report(self):
        referenced = self.result.indicators[0].latest["value"].fact_id
        without = tuple(fact for fact in self.result.facts if fact.fact_id != referenced)
        self.assert_blocked(replace(self.result, facts=without))
        self.assert_blocked(replace(self.result, facts=(*self.result.facts, self.result.facts[0])))
        for identifier in (None, ""):
            changed = self.changed_latest(fact_id=identifier)
            facts = tuple(replace(fact, fact_id=identifier) if fact.fact_id == referenced else fact
                          for fact in changed.facts)
            self.assert_blocked(replace(changed, facts=facts))

    def test_wrong_fact_request_stock_date_indicator_parameters_value_or_unit_is_rejected(self):
        referenced = self.result.indicators[0].latest["value"].fact_id
        original = next(fact for fact in self.result.facts if fact.fact_id == referenced)
        for changes in ({"request_id": "old-turn"}, {"instrument_id": "600000.SH"},
                        {"trade_date": date(2026, 9, 24)}, {"indicator_id": "sma(window=20)"},
                        {"parameters": {"window": 20}}, {"value": 999}, {"unit": "shares"},
                        {"dependencies": (referenced,)}):
            with self.subTest(changes=changes):
                facts = tuple(replace(original, **changes) if fact.fact_id == referenced else fact
                              for fact in self.result.facts)
                self.assert_blocked(replace(self.result, facts=facts))

    def test_wrong_field_date_unit_counts_or_evidence_value_is_rejected(self):
        for changes in ({"trade_date": date(2026, 9, 24)}, {"unit": "shares"},
                        {"actual_bars": 4}, {"missing_bars": 1}, {"required_bars": False},
                        {"value": 100}, {"fact_id": "invented"}):
            with self.subTest(changes=changes):
                self.assert_blocked(self.changed_latest(**changes))

    def test_nonfinite_or_boolean_values_are_not_rendered_as_prices(self):
        for value in (float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(value=value):
                self.assert_blocked(self.changed_latest(value=value))

    def test_unavailable_field_cannot_carry_an_old_value_or_citation(self):
        for changes in ({"value": 12}, {"fact_id": "old-field"},
                        {"status": "unknown"}, {"required_bars": 3, "missing_bars": 0}):
            with self.subTest(changes=changes):
                self.assert_blocked(self.changed_latest(index=1, **changes))

    def test_unknown_missing_or_duplicated_requested_indicator_is_rejected(self):
        self.assert_blocked(replace(self.result, indicators=self.result.indicators[:-1]))
        self.assert_blocked(replace(self.result, indicators=(self.result.indicators[0],) * 3))
        self.assert_blocked(replace(self.result, indicators=(
            replace(self.result.indicators[0], algorithm_version="other-algorithm"), *self.result.indicators[1:])))

    def test_wrong_stock_out_of_range_or_unordered_bars_are_rejected(self):
        for bars in ((replace(self.result.bars[0], instrument_id="600000.SH"), *self.result.bars[1:]),
                     (*self.result.bars[:-1], replace(self.result.bars[-1], trade_date=date(2026, 9, 26))),
                     tuple(reversed(self.result.bars)), ()):
            with self.subTest(bars=bars):
                self.assert_blocked(replace(self.result, bars=bars))

    def test_naive_retrieval_timestamp_is_not_presented_as_an_absolute_time(self):
        self.assert_blocked(replace(self.result, retrieved_at=datetime(2026, 9, 26)))
        self.assertIn("采集时间：未知", self.report(replace(self.result, retrieved_at=None)).text)

    def test_retrieval_and_latest_trade_dates_are_not_labeled_as_real_time(self):
        stamp = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        report = self.report(replace(self.result, retrieved_at=stamp))
        self.assertIn("2026-10-20T12:00:00+00:00", report.text)
        self.assertIn("实际最新交易日：2026-09-25", report.text)
        self.assertNotIn("实时行情", report.text)

    def test_cutoff_gap_and_recursive_initialization_limits_remain_visible(self):
        request = replace(self.request, as_of_date=date(2026, 9, 26),
                          indicators=(IndicatorSpec("ema", window=3),))
        result = analyze_indicators(CsvDailyBarSource(FIXTURE, is_synthetic=True), request,
                                    allow_development_csv=True)
        report = self.report(result, request)
        self.assertIn("实际最新交易日早于请求截止日", report.text)
        self.assertIn("部分递推指标可能受计算起点影响", report.text)

    def test_online_metadata_does_not_claim_independent_authenticity_or_live_quotes(self):
        request = replace(self.request, source="tencent")
        result = replace(self.result, request=request, source="tencent", data_authenticity="provider_reported",
                         price_adjustment="unadjusted")
        report = self.report(result, request)
        self.assertEqual(report.status, "limited")
        self.assertIn("供应商提供的历史日线，不是实时行情", report.text)
        self.assertIn("使用不复权价格", report.text)
        self.assertIn("腾讯源端量额可能已舍入", report.text)

    def test_no_data_source_error_and_invalid_data_have_no_numeric_fields(self):
        for state in ("no_data", "source_error", "invalid_data"):
            result = replace(self.result, data_status=state, message=PRIVATE_MARKER)
            report = self.report(result)
            self.assertEqual((report.status, report.code), ("unavailable", state))
            self.assertEqual(report.fields, ())
            self.assertNotIn("收盘价：", report.text)
            self.assertNotIn(PRIVATE_MARKER, report.text)
        self.assertIn("不能据此判断停牌", self.report(replace(self.result, data_status="no_data")).text)

    def test_unknown_data_status_is_blocked_instead_of_guessed(self):
        self.assert_blocked(replace(self.result, data_status="unknown-status"))

    def test_exception_and_unknown_warning_text_cannot_leak_into_the_report(self):
        report = self.report(replace(self.result, message=PRIVATE_MARKER, warnings=(PRIVATE_MARKER,)))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(report.to_dict()))
        self.assertIn("未验证历史发布时间", report.text)

    def test_report_export_is_detached_and_has_no_files_paths_or_model_prose(self):
        report = self.report()
        payload = report.to_dict()
        payload["fields"][0]["value"] = -999
        payload["limitations"].clear()
        self.assertEqual(report.fields[0].value, 10.4)
        self.assertTrue(report.limitations)
        serialized = json.dumps(report.to_dict(), allow_nan=False)
        self.assertNotIn(str(FIXTURE), serialized)
        self.assertNotIn("model_draft", serialized)
        self.assertEqual(report.to_dict()["validation_scope"], "deterministic_report_only_not_model_prose")

    def test_report_generation_is_pure_and_does_not_fetch_or_call_a_model(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No re-read")):
            first, second = self.report().to_dict(), self.report().to_dict()
        self.assertEqual(first, second)

    def test_bad_model_draft_is_debug_only_and_never_becomes_the_safe_answer(self):
        bad = "600000.SH的SMA20已经可用，收盘价999元，明天保证涨停。"
        # A legitimate timestamp may contain the same digits as an unsupported price.
        retrieved_at = datetime(2026, 9, 26, 2, 0, 0, 899928, tzinfo=timezone.utc)
        with patch("trading_companion.multi_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = retrieved_at
            result, model = self.agent_result(ToolModelResponse("complete", bad))
        payload = result.to_dict()
        self.assertEqual(payload["model_draft"], bad)
        self.assertEqual(payload["answer_validation"], "not_performed")
        self.assertEqual(payload["model_draft_policy"], "debug_only_unchecked_not_for_default_display")
        self.assertEqual(payload["safe_answer"]["origin"], "application")
        self.assertNotIn("600000.SH", payload["safe_answer"]["text"])
        self.assertIn(retrieved_at.isoformat(), payload["safe_answer"]["text"])
        self.assertNotIn("收盘价999元", payload["safe_answer"]["text"])
        prices = [field["value"] for field in payload["safe_answer"]["fields"]
                  if field["indicator_id"] == "daily_bar" and field["field"] == "close_price"]
        self.assertEqual(prices, [10.4])
        self.assertTrue(all(field["value"] != 999 for field in payload["safe_answer"]["fields"]))
        self.assertNotIn("保证涨停", payload["safe_answer"]["text"])
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))
        self.assertEqual(model.complete.call_count, 2)

    def test_agent_preserves_one_trusted_request_id_through_execution_and_evidence(self):
        result, _ = self.agent_result(ToolModelResponse("complete", "Unchecked draft"))
        self.assertEqual(result.confirmed_request, result.research_result.request)
        self.assertEqual({fact.request_id for fact in result.research_result.facts},
                         {result.confirmed_request.request_id})
        self.assertEqual(result.safe_answer.request_id, result.confirmed_request.request_id)

    def test_executor_trusted_id_is_optional_and_never_taken_from_model_arguments(self):
        source = CsvDailyBarSource(FIXTURE, is_synthetic=True)
        messages = [ToolMessage("user", "Fixed synthetic test")]
        arguments = request_arguments(self.request)
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("id_test", TOOL_NAME, arguments),))
        for identifier in (None, "trusted-report-1"):
            executor = ResearchToolExecutor(development_source=source, request_id=identifier)
            execution = executor.execute(proposal, messages=messages)
            self.assertEqual(execution.status, "executed")
            if identifier is not None:
                self.assertEqual(execution.full_result.request.request_id, identifier)
            else:
                self.assertTrue(execution.full_result.request.request_id)
        bad = ToolModelResponse("tool_calls", tool_calls=(ToolCall(
            "bad_id_test", TOOL_NAME, {**arguments, "request_id": "model-chosen"}),))
        executor = ResearchToolExecutor(development_source=source, request_id="trusted-report-2")
        self.assertEqual(executor.execute(bad, messages=messages).status, "rejected")
        self.assertEqual(executor.analyses_used, 0)

    def test_invalid_trusted_request_id_fails_before_any_read(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No reads")):
            for identifier in ("", "x" * 101, "../path", "a b", "中文", True, 123):
                with self.subTest(identifier=identifier), self.assertRaises(ModelConfigurationError):
                    ResearchToolExecutor(request_id=identifier)

    def test_model_failure_retains_safe_facts_without_adding_another_request(self):
        result, model = self.agent_result(ModelCallError("timeout", PRIVATE_MARKER))
        self.assertEqual((result.status, result.code), ("limited", "explanation_unavailable"))
        self.assertIsNone(result.model_draft)
        self.assertIn("收盘价：10.4", result.safe_answer.text)
        self.assertNotIn(PRIVATE_MARKER, result.safe_answer.text)
        self.assertEqual(model.complete.call_count, 2)

    def test_source_failure_has_a_safe_error_report_without_retry(self):
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(PRIVATE_MARKER)) as read:
            result, model = self.agent_result(ToolModelResponse("complete", "Unused"))
        self.assertEqual((result.status, result.safe_answer.status), ("failed", "unavailable"))
        self.assertNotIn(PRIVATE_MARKER, result.safe_answer.text)
        self.assertEqual(model.complete.call_count, 1)
        read.assert_called_once()

    def test_clarification_and_preexecution_failure_do_not_invent_a_fact_report(self):
        result = run_research_turn("分析平安", model=FakeToolModel([]))
        self.assertIsNone(result.safe_answer)
        self.assertIsNone(result.to_dict()["safe_answer"])

    def test_missing_confirmed_request_does_not_authorize_a_report(self):
        result = AgentTurnResult(research_result=self.result)
        self.assertEqual(result.safe_answer.status, "blocked")
        self.assertEqual(result.safe_answer.fields, ())

    def test_report_status_is_distinct_from_agent_and_model_answer_validation(self):
        result, _ = self.agent_result(ModelCallError("timeout", PRIVATE_MARKER),
                                      text=QUESTION.replace("SMA3、SMA20、VOLUME3", "SMA3"))
        self.assertEqual(result.status, "limited")
        self.assertEqual(result.safe_answer.status, "complete")
        self.assertEqual(result.answer_validation, "not_performed")

    def test_safe_demo_prints_only_application_facts(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main()
        self.assertEqual(code, 0)
        self.assertIn("人工样例 / 非真实行情", output.getvalue())
        self.assertIn("SMA(20)：历史记录不足", output.getvalue())
        self.assertNotIn("离线脚本草稿", output.getvalue())

    def test_natural_session_produces_a_report_only_after_a_valid_selection(self):
        payload = run_session_demo()
        self.assertIsNone(payload["first_turn"]["safe_answer"])
        self.assertEqual(payload["selection_turn"]["safe_answer"]["status"], "complete")
        self.assertIsNone(payload["repeated_selection"]["safe_answer"])
        self.assertEqual(payload["total_model_responses"], 3)

    def test_safe_demo_module_is_runnable_without_credentials(self):
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, "-B", "-m", "trading_companion.report_demo"],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="strict", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("000001.SZ", result.stdout)
        self.assertNotIn("离线脚本草稿", result.stdout)


if __name__ == "__main__":
    unittest.main()
