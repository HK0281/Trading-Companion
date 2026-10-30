"""Real-entry conflict regressions; fake models are not a language-quality evaluation."""

from datetime import timedelta
import unittest
from unittest.mock import patch

from trading_companion.agent import run_routing_reply_turn
from trading_companion.api_service import ApiProblem
from trading_companion.errors import DataSourceError
from trading_companion.request_routing import IntentKind, ROUTE_SPECS
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.routing_baseline_support import observe_scenario
from tests.test_screening_followup_api import ScreeningJourneyFixture
from tests.test_screening_agent import Model as ScreeningModel
from tests.test_product_research import NOW


class RoutingConflictsApiTests(ScreeningJourneyFixture, unittest.TestCase):
    def counts(self):
        return tuple(len(value) for value in (self.model.calls, self.quotes.calls, self.data.calls, self.daily.reads))

    def test_company_pending_accepts_knowledge_without_consumption_or_renewal(self):
        self.screen()
        self.assertEqual(self.send("分析平安的基本面")["result"]["code"], "product_instrument_needs_clarification")
        pending, saved = self.session.product_pending, self.session.screening_saved
        counts = self.counts()
        self.wall += timedelta(minutes=3)
        turn = self.send("MACD是什么")["result"]
        self.assertEqual(turn["code"], "indicator_knowledge_answered")
        self.assertEqual(self.counts(), counts)
        self.assertIs(self.session.product_pending, pending)
        self.assertIs(self.session.screening_saved, saved)
        self.assertEqual(self.send("平安银行")["result"]["code"], "company_research_completed")
        self.assertIsNone(self.session.product_pending)

    def test_general_knowledge_insertion_retains_all_three_pending_owners(self):
        for start in ("分析平安的基本面", "平安只看SMA3"):
            with self.subTest(start=start):
                self.install()
                self.send(start)
                before = self.session.product_pending or self.session.pending
                counts = self.counts()
                self.assertEqual(self.send("怎么用PE/PB看估值")["result"]["code"], "general_knowledge_answered")
                self.assertIs(self.session.product_pending or self.session.pending, before)
                self.assertEqual(self.counts(), (counts[0] + 1, *counts[1:]))

    def test_technical_short_name_and_code_preserve_original_request(self):
        for reply in ("平安银行", "000001.SZ", "中国平安"):
            with self.subTest(reply=reply):
                self.install()
                self.screen()
                self.send("平安只看SMA3")
                pending = self.session.pending
                counts = self.counts()
                self.model.technical.position = 0
                turn = self.send(reply)["result"]
                self.assertEqual(turn["code"], "research_completed", turn)
                self.assertEqual(self.counts(), (counts[0] + 2, counts[1], counts[2], counts[3] + 1))
                self.assertEqual(self.session.confirmed.conditions, pending.conditions)
                self.assertEqual([(item.kind, item.parameters) for item in self.session.confirmed.request.indicators],
                                 [("sma", {"window": 3})])
                self.assertEqual(self.session.confirmed.security.name, "中国平安" if reply == "中国平安" else "平安银行")
                self.assertIsNone(self.session.product_saved)

    def test_invalid_and_expired_short_reply_stays_with_technical_owner(self):
        for reply, delta in (("第九个", 0), ("贵州茅台", 0), ("平安银行", 900)):
            with self.subTest(reply=reply, delta=delta):
                self.install()
                self.screen()
                self.send("平安只看SMA3")
                counts = self.counts()
                self.wall = NOW + timedelta(seconds=delta)
                turn = self.send(reply)["result"]
                self.assertEqual(turn["status"], "needs_clarification", turn)
                self.assertEqual(self.counts(), counts)
                self.assertIsNone(self.session.product_pending)
                self.assertIsNone(self.session.product_saved)
                self.wall = NOW

    def test_indicator_pending_company_or_list_reply_does_not_start_new_task(self):
        for reply in ("平安银行", "分析名单中第一只"):
            scenario = {"steps": [
                {"question": "平安银行只看SMA3和SMA5", "technical_indicators": ["SMA3", "SMA5"]},
                {"question": "把均线改成10日"}, {"question": reply}, {"question": "SMA3"},
            ]}
            with self.subTest(reply=reply):
                observed = observe_scenario(scenario)
                blocked = observed[2]
                self.assertEqual(blocked["route"], "technical_research")
                self.assertEqual(blocked["calls"], dict(model_forwards=0, quote_reads=0, company_reads=0, daily_reads=0))
                self.assertEqual(blocked["before"]["pending_edit"], blocked["after"]["pending_edit"])
                self.assertEqual(observed[3]["outcome"]["response"]["result"]["code"], "research_completed")

    def test_complete_company_task_replaces_indicator_pending(self):
        observed = observe_scenario({"steps": [
            {"question": "平安银行只看SMA3和SMA5", "technical_indicators": ["SMA3", "SMA5"]},
            {"question": "把均线改成10日"}, {"question": "分析平安银行的基本面"}, {"question": "SMA3"},
        ]})
        self.assertEqual(observed[2]["route"], "company_research")
        self.assertIsNone(observed[2]["after"]["pending_edit"])
        self.assertEqual(observed[3]["outcome"]["response"]["result"]["code"], "no_pending_indicator_edit")

    def test_workflow_disabled_is_local_and_preserves_existing_company_pending(self):
        self.install(workflow_enabled=False)
        turn = self.send("怎么用PE/PB看估值")["result"]
        self.assertEqual(turn["code"], "general_knowledge_disabled")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertIsNone(self.session.product_pending)
        self.send("分析平安的基本面")
        pending, counts = self.session.product_pending, self.counts()
        self.assertEqual(self.send("怎么用PE/PB看估值")["result"]["code"], "general_knowledge_disabled")
        self.assertEqual(self.counts(), counts)
        self.assertIs(self.session.product_pending, pending)
        self.assertEqual(self.send("RSI是什么")["result"]["code"], "indicator_knowledge_answered")
        self.assertIs(self.session.product_pending, pending)

    def test_local_reply_and_fixed_knowledge_keep_original_mandatory_directory_gate(self):
        for workflow in (False, True):
            self.install(workflow_enabled=workflow,
                directory_loader=lambda now: (_ for _ in ()).throw(DataSourceError("offline")))
            for question in (("RSI是什么", "怎么用PE/PB看估值") if not workflow else ("RSI是什么",)):
                with self.assertRaises(ApiProblem) as caught:
                    self.send(question)
                self.assertEqual(caught.exception.code, "directory_unusable")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_edit_prefix_does_not_replace_pending_but_explicit_new_screening_does(self):
        for start in ("分析平安的基本面", "平安只看SMA3"):
            with self.subTest(start=start):
                self.install()
                self.screen()
                self.send(start)
                quotes = len(self.quotes.calls)
                turn = self.send("筛选条件把PE改为低于15")["result"]
                self.assertNotEqual(turn["stage"], "screening")
                self.assertEqual(len(self.quotes.calls), quotes)
                self.assertIsNotNone(self.session.product_pending or self.session.pending)
                self.assertEqual(self.send("帮我选5只股票")["result"]["code"], "sample_conditions_evaluated")
                self.assertIsNone(self.session.product_pending)
                self.assertIsNone(self.session.pending)

    def conflicting_session(self):
        self.send("平安只看SMA3")
        pending = self.session.pending
        self.send("取消")
        self.send("分析平安的基本面")
        self.session._pending = pending

    def test_conflicting_pending_is_unchanged_and_replay_and_recovery_are_safe(self):
        self.conflicting_session()
        pending, product = self.session.pending, self.session.product_pending
        counts = self.counts()
        for question in ("平安银行",):
            result = self.send(question)["result"]
            self.assertEqual((result["status"], result["code"]), ("needs_clarification", "routing_state_conflict"))
            self.assertIs(self.session.pending, pending)
            self.assertIs(self.session.product_pending, product)
        first = self.send("平安银行", request_id="conflict-replay")
        second = self.send("平安银行", request_id="conflict-replay")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.send("取消")["result"]["code"], "research_cancelled")
        self.assertIsNone(self.session.pending)
        self.assertIsNone(self.session.product_pending)
        self.assertEqual(self.counts(), counts)

    def test_conflicting_pending_allows_knowledge_and_complete_new_screening(self):
        self.conflicting_session()
        pending, product = self.session.pending, self.session.product_pending
        counts = self.counts()
        for question, code in (("MACD是什么", "indicator_knowledge_answered"),
                               ("怎么用PE/PB看估值", "general_knowledge_answered")):
            self.assertEqual(self.send(question)["result"]["code"], code)
            self.assertIs(self.session.pending, pending)
            self.assertIs(self.session.product_pending, product)
        self.assertEqual(self.counts(), (counts[0] + 1, *counts[1:]))
        self.assertEqual(self.send("帮我选5只股票")["result"]["code"], "sample_conditions_evaluated")
        self.assertIsNone(self.session.pending)
        self.assertIsNone(self.session.product_pending)

    def test_valid_empty_screening_still_completes_replacement_of_old_pending(self):
        self.conflicting_session()
        self.model.screening = ScreeningModel((dict(field='pe_dynamic', operator='lt', value=0.01, phrase='PE低于0.01'),))
        result = self.send('筛选PE低于0.01')['result']
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(result['screening']['status'], 'empty')
        self.assertIsNone(self.session.pending)
        self.assertIsNone(self.session.product_pending)

    def test_cancel_preserves_saved_list_and_reset_clears_it_for_both_owners(self):
        for start in ("分析平安的基本面", "平安只看SMA3"):
            for command in ("取消", "新对话"):
                with self.subTest(start=start, command=command):
                    self.install()
                    self.screen()
                    saved = self.session.screening_saved
                    self.send(start)
                    counts = self.counts()
                    self.send(command)
                    self.assertEqual(self.counts(), counts)
                    self.assertIsNone(self.session.product_pending)
                    self.assertIsNone(self.session.pending)
                    self.assertIs(self.session.screening_saved, saved if command == "取消" else None)

    def test_local_reply_uses_control_and_rejects_late_publication(self):
        self.install(workflow_enabled=False,
            control_factory=lambda: TurnControl(TurnLimits(), clock=lambda: 10.0))
        original = run_routing_reply_turn

        def cancel_before_reply(*args, **kwargs):
            kwargs["control"].cancel()
            return original(*args, **kwargs)

        with patch("trading_companion.api_service.run_routing_reply_turn", cancel_before_reply):
            result = self.send("怎么用PE/PB看估值")["result"]
        self.assertEqual(result["code"], "turn_cancelled")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertIsNone(self.session.product_pending)

    def test_new_codes_have_generic_message_payload_without_research_attachments(self):
        self.install(workflow_enabled=False)
        results = [self.send("怎么用PE/PB看估值")["result"]]
        self.conflicting_session()
        results.append(self.send("平安银行")["result"])
        for result in results:
            self.assertEqual(result["status"], "needs_clarification")
            self.assertTrue(result["message"])
            self.assertEqual(result["answer"] or result["message"], result["message"])
            self.assertEqual(result["candidates"], [])
            for key in ("report", "parameters", "chart", "market_context", "company_research", "presentation", "conversation"):
                self.assertIsNone(result.get(key), key)

    def test_mutating_technical_owner_is_detected_by_real_entry_counterexample(self):
        from trading_companion import request_routing as routing
        bad_specs = tuple(spec for spec in ROUTE_SPECS if spec.intent != IntentKind.TECHNICAL_CLARIFICATION)
        self.send("平安只看SMA3")
        with patch.object(routing, "ROUTE_SPECS", bad_specs):
            result = self.send("平安银行")["result"]
        self.assertNotEqual(result["code"], "research_completed")
        self.assertGreater(len(self.data.calls), 0)


if __name__ == "__main__":
    unittest.main()
