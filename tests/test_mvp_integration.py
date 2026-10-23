"""MVP views share checked evidence without extra model calls or market reads."""

from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verify import offline_network
from trading_companion.agent import run_natural_research_turn, run_research_turn
from trading_companion.api_service import ResearchApiService, public_turn
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.mvp_evaluation import FixtureModel, NOW
from trading_companion.mvp_evaluation_cases import SCENARIOS
from trading_companion.research_followup import render_saved_answer
from trading_companion.research_session import ResearchSession
from trading_companion.turn_control import TurnControl, TurnLimits


class MvpIntegrationTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No online data")),
                      patch("requests.post", side_effect=AssertionError("No paid model")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                            side_effect=AssertionError("No credentials")),
                      patch("trading_companion.research_tool._build_source",
                            side_effect=AssertionError("No online source"))):
            guard.start()
            self.addCleanup(guard.stop)
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.source, self.directory = load_development_fixtures()
        self.session, self.model = ResearchSession(), FixtureModel()

    def step(self, case, index=0):
        return next(item for item in SCENARIOS if item.name == case).steps[index]

    def turn(self, step, **options):
        self.model.step = step
        if step.input_mode == "explicit":
            return run_research_turn(step.question, model=self.model, development_source=self.source, **options)
        return run_natural_research_turn(step.question, model=self.model, development_source=self.source,
            directory=self.directory, reference_date=REFERENCE_DATE, current_time=NOW, session=self.session, **options)

    def snapshot(self):
        return self.session.saved_result(directory=self.directory, source="csv", development_source=self.source,
            reference_date=REFERENCE_DATE, now=NOW, allow_history=True).snapshot

    def test_normal_answer_and_api_projection_include_the_same_numeric_synthesis(self):
        original = CsvDailyBarSource.read_daily_bars
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=original) as read:
            result = self.turn(self.step("complete_combination"))
            answer = result.display_answer
            payload = public_turn(result)
            debug = result.to_dict()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.research_synthesis.status, "complete")
        for title in ("趋势参考", "动量参考", "波动尺度", "量能参考", "跨维度核对", "条件化观察"):
            self.assertIn(title, answer)
        self.assertIn("程序生成，不含模型自由文本", answer)
        self.assertEqual(payload["answer"], answer)
        self.assertEqual(debug["research_synthesis"]["origin"], "application")
        self.assertNotIn("model_draft", payload)
        self.assertNotIn(result.model_draft, answer)
        self.assertEqual((result.model_calls_attempted, result.analyses_used, self.model.serial), (2, 1, 2))
        read.assert_called_once()

    def test_model_failure_stays_visible_even_when_program_comparisons_are_available(self):
        result = self.turn(self.step("complete_combination"))
        changed = replace(result, status="limited", code="explanation_rejected",
                          model_draft="private-unchecked-claims", failure_code="explanation_invalid_json")
        self.assertEqual(changed.explanation_validation.status, "rejected")
        self.assertEqual(changed.research_synthesis.status, "complete")
        self.assertIn("未取得通过检查的模型解读", changed.display_answer)
        self.assertIn("跨指标现状解读", changed.display_answer)
        self.assertNotIn("private-unchecked-claims", changed.display_answer)
        self.assertEqual(changed.status, "limited")
        self.assertEqual(self.model.serial, 2)

    def test_partial_history_keeps_missing_information_unknown_not_votes(self):
        result = self.turn(self.step("partial_history"))
        self.assertEqual(result.research_synthesis.status, "limited")
        self.assertIn("还缺15条记录", result.display_answer)
        self.assertEqual(result.research_synthesis.cross_dimension.state, "incomplete")
        self.assertIn("同族多参数实例分别展示但不计票", result.display_answer)

    def test_no_available_indicator_does_not_trigger_cross_indicator_claims(self):
        result = self.turn(self.step("default_indicators"))
        self.assertEqual(result.code, "research_not_ready")
        self.assertIsNone(result.research_synthesis)
        self.assertNotIn("跨指标现状解读", result.display_answer)
        self.assertIn("历史记录不足", result.display_answer)

    def test_changed_request_cannot_reuse_a_successful_synthesis(self):
        result = self.turn(self.step("complete_combination"))
        for changes in ({"instrument_id": "600519.SH"}, {"request_id": "different_research"}, {"source": "tencent"}):
            with self.subTest(changes=changes):
                changed = replace(result, confirmed_request=replace(result.confirmed_request, **changes))
                self.assertIsNone(changed.research_synthesis)
                self.assertNotIn("跨指标现状解读", changed.display_answer)

    def test_prior_numeric_fact_mutation_is_caught_when_latest_fact_report_still_passes(self):
        result = self.turn(self.step("complete_combination"))
        prior = result.research_result.bars[-2].trade_date
        facts = tuple(replace(item, value=999.0) if item.trade_date == prior and item.field == "histogram" else item
                      for item in result.research_result.facts)
        self.assertNotEqual(facts, result.research_result.facts)
        changed = replace(result, research_result=replace(result.research_result, facts=facts))
        self.assertEqual(changed.safe_answer.status, "complete")
        self.assertEqual(changed.research_synthesis.status, "blocked")
        self.assertNotIn("跨指标现状解读", changed.display_answer)
        self.assertIn("未通过请求或证据检查", changed.display_answer)

    def test_analysis_budget_stop_keeps_facts_but_has_no_synthesis(self):
        result = self.turn(self.step("complete_combination"), control=TurnControl(TurnLimits(model_calls=1)))
        self.assertEqual(result.code, "model_call_budget_exhausted")
        self.assertIsNotNone(result.safe_answer)
        self.assertIsNone(result.research_synthesis)
        self.assertIn("不生成综合解读", result.display_answer)
        self.assertNotIn("跨指标现状解读", result.display_answer)
        self.assertIsNone(result.to_dict()["research_synthesis"])

    def test_cancellation_discards_result_and_synthesis(self):
        control = TurnControl()
        control.cancel()
        result = self.turn(self.step("complete_combination"), control=control)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.display_answer)
        self.assertIsNone(result.research_synthesis)
        self.assertEqual(self.model.serial, 0)

    def test_stopped_runtime_alone_cannot_promote_retained_facts_to_synthesis(self):
        result = self.turn(self.step("complete_combination"))
        changed = replace(result, runtime={"stop_code": "turn_deadline_exceeded"})
        self.assertIsNone(changed.research_synthesis)
        self.assertNotIn("跨指标现状解读", changed.display_answer)

    def test_saved_overview_rechecks_original_scope_with_zero_model_or_data_calls(self):
        initial = self.turn(self.step("explicit_history"))
        serial = self.model.serial
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No reread")):
            shown = self.turn(self.step("explicit_history", 1))
        self.assertIn("跨指标现状解读", shown.display_answer)
        self.assertIn("历史查看", shown.display_answer)
        self.assertIn("2026-09-21 至 2026-09-25", shown.display_answer)
        self.assertEqual(shown.followup_answer.request_id, initial.confirmed_request.request_id)
        self.assertEqual((shown.model_calls_attempted, shown.analyses_used, self.model.serial), (0, 0, serial))

    def test_single_indicator_followup_does_not_expand_to_a_full_synthesis(self):
        self.turn(self.step("saved_followup"))
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No reread")):
            shown = self.turn(self.step("saved_followup", 1))
        self.assertEqual(shown.followup_answer.indicator_ids, (self.step("saved_followup").scope.indicators[1].indicator_id,))
        self.assertNotIn("跨指标现状解读", shown.display_answer)
        self.assertEqual((shown.model_calls_attempted, shown.analyses_used), (1, 0))

    def test_mutated_saved_scope_cannot_generate_a_historical_synthesis(self):
        self.turn(self.step("explicit_history"))
        snapshot = self.snapshot()
        changed = replace(snapshot, request=replace(snapshot.request, instrument_id="600519.SH"))
        answer = render_saved_answer(changed, historical=True)
        self.assertEqual(answer.code, "saved_result_integrity_failed")
        self.assertNotIn("跨指标现状解读", answer.text)

    def test_synthetic_refresh_notice_is_part_of_default_safe_answer(self):
        self.turn(self.step("latest_refresh"))
        result = self.turn(self.step("latest_refresh", 1))
        for phrase in ("上海前一自然日", "不是实时行情", "区间未变化", "不代表发现了新行情", "刷新只重跑样例"):
            self.assertIn(phrase, result.display_answer)
        self.assertIn("跨指标现状解读", result.display_answer)
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (2, 1))

    def test_relative_date_convention_is_visible_instead_of_only_in_debug_metadata(self):
        self.turn(self.step("relative_range_update"))
        result = self.turn(self.step("relative_range_update", 1))
        self.assertIn("月按30天、年按365天", result.display_answer)
        self.assertIn("2026-08-27至2026-09-25", result.display_answer)
        self.assertIn("上海前一自然日", result.display_answer)

    def test_service_response_and_history_share_synthesis_without_extra_reads(self):
        step = self.step("explicit_history")
        self.model.step = step
        service = ResearchApiService(model_factory=lambda: self.model, directory_loader=lambda _: self.directory,
            development_source=self.source, reference_date=REFERENCE_DATE, wall_clock=lambda: NOW)
        self.addCleanup(service.close)
        credentials = service.create_session()
        session_id, token = credentials["session_id"], credentials["access_token"]
        response = service.message(session_id, token, uuid4().hex, step.question)
        self.assertIn("跨指标现状解读", response["result"]["answer"])
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No reread")):
            shown = service.history(session_id, token)
        self.assertIn("跨指标现状解读", shown["result"]["answer"])
        self.assertEqual(shown["result"]["model_calls_attempted"], 0)
        self.assertNotIn("model_draft", json.dumps(shown))


if __name__ == "__main__":
    unittest.main()
