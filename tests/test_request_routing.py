"""Legacy precedence and phase boundaries, independently of the frozen API corpus."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from scripts.verify import offline_network
from trading_companion import request_routing as routing
from trading_companion.research_session import ResearchSession
from trading_companion.screening import ScreeningCondition, ScreeningRequest
from trading_companion.screening_context import SavedScreening
from tests.test_product_research import NOW, directory


class RequestRoutingTests(unittest.TestCase):
    def setUp(self):
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        self.session = ResearchSession()
        self.quotes = Mock()
        self.context = routing.RoutingContext(session=routing.RoutingSessionView.capture(self.session),
            directory=directory(), company_enabled=True, quote_source=self.quotes)

    def route(self, question, *, context=None, clock=None):
        return routing.route_research_request(question, context=context or self.context,
                                              clock=clock or (lambda: NOW))

    def shortlist(self):
        saved = SavedScreening(NOW, ScreeningRequest((ScreeningCondition("price", "gt", 0),), limit=3),
            self.context.directory.securities[:2], self.quotes)
        return replace(self.context, session=replace(self.context.session, screening_saved=saved))

    def test_knowledge_is_a_separate_optional_directory_phase(self):
        self.assertTrue(routing.probe_general_knowledge("怎么用PE/PB看估值", workflow=True, history=False))
        self.assertIsNone(routing.resolve_general_knowledge("怎么用PE/PB看估值", candidate=False, directory=None))
        decision = routing.resolve_general_knowledge("怎么用PE/PB看估值", candidate=True, directory=None)
        self.assertEqual(decision.kind, routing.RouteKind.GENERAL_KNOWLEDGE)
        self.assertEqual(decision.rule_id, "legacy.general_knowledge")

    def test_disabled_workflow_history_and_fixed_definitions_do_not_probe_a_hint(self):
        for workflow, history, question in ((False, False, "怎么用PE/PB看估值"),
            (True, True, "怎么用PE/PB看估值"), (True, False, "RSI是什么")):
            with self.subTest(question=question, workflow=workflow, history=history):
                with patch.object(routing, "is_general_knowledge_question", side_effect=AssertionError("Probe must short circuit")):
                    self.assertFalse(routing.probe_general_knowledge(question, workflow=workflow, history=history))

    def test_stale_hint_can_veto_knowledge_without_authorizing_research(self):
        stale = replace(self.context.directory, retrieved_at=NOW - timedelta(days=8))
        with patch.object(type(stale), "assert_usable", side_effect=AssertionError("Hint is not authorization")):
            self.assertIsNone(routing.resolve_general_knowledge("PE低的平安银行值得买吗", candidate=True, directory=stale))
            self.assertEqual(routing.resolve_general_knowledge("怎么用PE/PB看估值", candidate=True,
                directory=stale).kind, routing.RouteKind.GENERAL_KNOWLEDGE)

    def test_six_static_paths_have_fixed_unambiguous_priorities_and_reason_codes(self):
        self.assertEqual(tuple(spec.kind for spec in routing.ROUTE_SPECS), tuple(routing.RouteKind))
        self.assertEqual([spec.priority for spec in routing.ROUTE_SPECS], [10, 20, 30, 40, 50, 60])
        for question, kind in (("把PE改为低于15", routing.RouteKind.SCREENING_EDIT),
            ("帮我选5只股票", routing.RouteKind.SCREENING),
            ("分析第一只的基本面", routing.RouteKind.SCREENING_FOLLOWUP),
            ("分析平安银行的基本面", routing.RouteKind.COMPANY_RESEARCH),
            ("平安银行只看SMA3", routing.RouteKind.TECHNICAL_RESEARCH)):
            with self.subTest(question=question):
                decision = self.route(question)
                self.assertEqual(decision.kind, kind)
                self.assertEqual(decision.rule_id, "legacy." + kind.value)

    def test_duplicate_priorities_paths_or_reason_codes_are_configuration_errors(self):
        first, second = routing.ROUTE_SPECS[:2]
        for changed in (replace(second, priority=first.priority), replace(second, kind=first.kind),
                        replace(second, rule_id=first.rule_id)):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    routing._ordered_specs((first, changed))

    def test_company_candidate_is_computed_before_the_winning_edit_and_stops_later_matching(self):
        events = []
        with patch.object(routing, "wants_product_research", side_effect=lambda q: events.append("company") or True), \
             patch.object(routing, "is_screening_edit", side_effect=lambda q, **k: events.append("edit") or True), \
             patch.object(routing, "is_screening_request", side_effect=AssertionError("First winning route stops matching")):
            clock = Mock(side_effect=AssertionError("Edit must not read the followup clock"))
            self.assertEqual(self.route("把PE改为低于15", clock=clock).kind, routing.RouteKind.SCREENING_EDIT)
        self.assertEqual(events, ["company", "edit"])
        clock.assert_not_called()

    def test_company_pending_short_circuits_other_company_hints(self):
        context = replace(self.context, session=replace(self.context.session, product_pending=True))
        with patch.object(routing, "wants_product_research", side_effect=AssertionError("Pending already selected company")), \
             patch.object(routing, "is_product_followup", side_effect=AssertionError("No extra hint evaluation")):
            self.assertEqual(self.route("第一个", context=context).kind, routing.RouteKind.COMPANY_RESEARCH)

    def test_pending_states_block_edits_and_list_selection_without_consuming_them(self):
        for field in ("pending", "pending_edit", "product_pending"):
            context = replace(self.context, session=replace(self.context.session, **{field: True}))
            for question in ("把PE改为低于15", "分析第一只的基本面"):
                with self.subTest(field=field, question=question):
                    clock = Mock(side_effect=AssertionError("Pending must not reach list clock"))
                    decision = self.route(question, context=context, clock=clock)
                    self.assertNotIn(decision.kind, (routing.RouteKind.SCREENING_EDIT, routing.RouteKind.SCREENING_FOLLOWUP))
                    self.assertTrue(getattr(context.session, field))
                    clock.assert_not_called()

    def test_new_screening_keeps_its_legacy_override_even_during_history_or_pending(self):
        context = replace(self.context, history=True, session=replace(self.context.session, product_pending=True))
        clock = Mock(side_effect=AssertionError("New screening must not read followup clock"))
        self.assertEqual(self.route("帮我选5只股票", context=context, clock=clock).kind, routing.RouteKind.SCREENING)
        clock.assert_not_called()

    def test_fixed_definitions_keep_the_existing_company_pending_inconsistency(self):
        self.assertEqual(self.route("MACD是什么").kind, routing.RouteKind.TECHNICAL_RESEARCH)
        context = replace(self.context, session=replace(self.context.session, product_pending=True))
        self.assertEqual(self.route("MACD是什么", context=context).kind, routing.RouteKind.COMPANY_RESEARCH)

    def test_named_member_uses_the_late_clock_and_does_not_invalidate_the_snapshot(self):
        context = self.shortlist()
        for delta, expected in ((899, routing.RouteKind.SCREENING_FOLLOWUP), (900, routing.RouteKind.COMPANY_RESEARCH)):
            with self.subTest(seconds=delta):
                clock = Mock(return_value=NOW + timedelta(seconds=delta))
                self.assertEqual(self.route("分析平安银行的基本面", context=context, clock=clock).kind, expected)
                clock.assert_called_once_with()
                self.assertIsNotNone(context.session.screening_saved)
                self.assertEqual(context.session.screening_saved.saved_at, NOW)
        self.quotes.assert_not_called()
        self.assertEqual(self.quotes.mock_calls, [])

    def test_explicit_expired_ordinal_still_reaches_the_executor_for_rejection(self):
        context = self.shortlist()
        decision = self.route("分析名单中第一只", context=context, clock=lambda: NOW + timedelta(minutes=15))
        self.assertEqual(decision.kind, routing.RouteKind.SCREENING_FOLLOWUP)
        self.assertIsNotNone(context.session.screening_saved)

    def test_state_projection_is_detached_from_mutable_session_and_has_no_mutators(self):
        self.session.product_pending = object()
        self.session._pending = object()
        self.session._pending_edit = object()
        self.session.product_saved = object()
        self.session._confirmed = object()
        self.session.screening_saved = self.shortlist().session.screening_saved
        view = routing.RoutingSessionView.capture(self.session)
        self.assertIs(view.product_pending, True)
        self.assertIs(view.pending, True)
        self.assertIs(view.pending_edit, True)
        self.assertIs(view.product_saved, True)
        self.assertIs(view.confirmed, True)
        self.assertIs(view.screening_saved, self.session.screening_saved)
        self.session.clear()
        self.assertIs(view.product_pending, True)
        self.assertIsNotNone(view.screening_saved)
        for name in ("clear", "begin_research", "_publish_turn"):
            self.assertFalse(hasattr(view, name))
        with self.assertRaises(FrozenInstanceError):
            view.pending = None
        with self.assertRaises(FrozenInstanceError):
            self.context.history = True
        with self.assertRaises(FrozenInstanceError):
            self.route("RSI是什么").rule_id = "changed"

    def test_company_disabled_keeps_technical_fallback_without_evaluating_company_hints(self):
        with patch.object(routing, "wants_product_research", side_effect=AssertionError("Company service is disabled")):
            context = replace(self.context, company_enabled=False)
            self.assertEqual(self.route("分析平安银行的基本面", context=context).kind, routing.RouteKind.TECHNICAL_RESEARCH)


if __name__ == "__main__":
    unittest.main()
