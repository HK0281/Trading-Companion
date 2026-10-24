"""Offline, request-bound compact views do not change the Agent's underlying answers."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from trading_companion.agent import AgentTurnResult, run_natural_research_turn
from trading_companion.api_service import public_turn
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel, QUESTION, FOLLOWUP
from trading_companion.research_session import ResearchSession


class AnswerPresentationTests(unittest.TestCase):
    def setUp(self):
        self.source, self.directory = load_development_fixtures()
        self.model, self.session = ConversationDemoModel(), ResearchSession()
        self.now = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)

    def run_turn(self, question=QUESTION):
        return run_natural_research_turn(question, model=self.model, directory=self.directory,
            source="tencent", development_source=self.source, reference_date=REFERENCE_DATE,
            session=self.session, current_time=self.now)

    def saved(self):
        return self.session.saved_result(directory=self.directory, source="csv", development_source=self.source,
            reference_date=REFERENCE_DATE, now=self.now, allow_history=True).snapshot

    def test_fresh_view_is_bound_to_the_checked_report(self):
        result = self.run_turn()
        value = public_turn(result)
        view = value["presentation"]
        self.assertEqual((view["version"], view["kind"]), ("answer-presentation-v1", "research"))
        self.assertEqual(view["request_id"], value["report"]["request_id"])
        self.assertEqual(view["instrument_id"], value["parameters"]["instrument_id"])
        self.assertEqual(view["trade_date"], "2026-09-25")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (3, 1))

    def test_only_observed_dimensions_are_shown_not_unrequested_filler(self):
        view = public_turn(self.run_turn())["presentation"]
        self.assertEqual([item["id"] for item in view["sections"]], ["trend", "momentum"])
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("未请求", text)
        self.assertNotIn("限制：", text)
        self.assertNotIn("买入", view["headline"])

    def test_projection_does_not_change_the_full_answer_or_fact_precision(self):
        result = self.run_turn()
        answer = result.display_answer
        value = public_turn(result)
        self.assertEqual(value["answer"], answer)
        self.assertEqual(value["report"], result.safe_answer.to_dict())
        self.assertIn("point-in-time", value["answer"])

    def test_concise_observations_keep_the_checked_meaning_without_repeated_boilerplate(self):
        value = public_turn(self.run_turn())
        items = [line for section in value["presentation"]["sections"] for line in section["items"]]
        self.assertEqual(items, ["收盘价高于SMA(3)。", "SMA(3)较前一条记录增加。", "RSI(2)高于70，处于高位参考区。"])
        self.assertIn("不能", value["answer"])

    def test_projection_does_not_read_data_or_call_model_again(self):
        result = self.run_turn()
        with patch.object(self.model, "complete", side_effect=AssertionError("No model request")), \
                patch.object(type(self.source), "read_daily_bars", side_effect=AssertionError("No market read")):
            self.assertIsNotNone(public_turn(result)["presentation"])

    def test_presentation_collections_are_detached(self):
        result = self.run_turn()
        value = public_turn(result)
        value["presentation"]["sections"][0]["items"].append("untrusted-mutated-text")
        self.assertNotIn("untrusted-mutated-text", json.dumps(public_turn(result)))

    def test_knowledge_has_no_stock_scope_or_model_call(self):
        result = self.run_turn("解释一下MACD里的DIF和DEA")
        value = public_turn(result)
        self.assertEqual(value["presentation"]["kind"], "knowledge")
        self.assertEqual([item["title"] for item in value["presentation"]["sections"]], ["DIF", "DEA"])
        self.assertIsNone(value["presentation"]["request_id"])
        self.assertIsNone(value["report"])
        self.assertEqual(result.model_calls_attempted, 0)

    def test_followup_projects_only_accepted_definitions_and_checked_relations(self):
        self.run_turn()
        result = self.run_turn(FOLLOWUP)
        view = public_turn(result, saved=self.saved())["presentation"]
        self.assertEqual(view["kind"], "followup")
        self.assertEqual(view["indicator_ids"], ["rsi(window=2)"])
        self.assertEqual(view["request_id"], result.followup_answer.request_id)
        self.assertEqual(view["sections"][0]["id"], "rsi")
        self.assertEqual(view["sections"][-1]["items"], list(result.followup_answer.relation_texts))

    def test_history_overview_can_use_only_the_matching_saved_result(self):
        self.run_turn()
        result = self.run_turn("查看上次结果")
        value = public_turn(result, saved=self.saved())
        self.assertEqual(value["presentation"]["kind"], "research")
        self.assertEqual(value["chart"]["context"], "saved")
        self.assertEqual(result.model_calls_attempted, 0)
        self.assertEqual(value["answer"], result.followup_answer.text)

    def test_mismatched_followup_snapshot_has_no_compact_view(self):
        self.run_turn()
        result = self.run_turn(FOLLOWUP)
        snapshot = self.saved()
        other = replace(snapshot, request=replace(snapshot.request, request_id="another-research"))
        self.assertIsNone(public_turn(result, saved=other)["presentation"])
        self.assertEqual(public_turn(result, saved=other)["answer"], result.display_answer)

    def test_stopped_result_does_not_generate_synthesis_cards(self):
        result = replace(self.run_turn(), status="cancelled", failure_code="turn_cancelled")
        view = public_turn(result)["presentation"]
        self.assertEqual(view["headline"], "研究已停止，保留已核对的数据")
        self.assertEqual(view["sections"], [])

    def test_unverified_request_scope_cannot_get_a_compact_view(self):
        result = self.run_turn()
        result = replace(result, confirmed_request=replace(result.confirmed_request, request_id="another-research"))
        self.assertIsNone(public_turn(result)["presentation"])

    def test_no_report_no_compact_research(self):
        self.assertIsNone(public_turn(AgentTurnResult())["presentation"])

    def test_optional_view_failure_preserves_answer_and_hides_exception(self):
        result = self.run_turn()
        with patch("trading_companion.api_service.build_answer_presentation", side_effect=RuntimeError("private-marker")):
            value = public_turn(result)
        self.assertIsNone(value["presentation"])
        self.assertEqual(value["answer"], result.display_answer)
        self.assertNotIn("private-marker", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
