"""Target precedence, mutually exclusive facts and read-only routing boundaries."""

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

    def pending(self, field, **changes):
        return replace(self.context, session=replace(self.context.session, **{field: True}), **changes)

    def test_optional_knowledge_phase_keeps_workflow_history_and_identity_boundaries(self):
        self.assertTrue(routing.probe_general_knowledge("怎么用PE/PB看估值", workflow=True, history=False))
        for workflow, history, question in ((False, False, "怎么用PE/PB看估值"),
                (True, True, "怎么用PE/PB看估值"), (True, False, "RSI是什么")):
            self.assertFalse(routing.probe_general_knowledge(question, workflow=workflow, history=history))
        self.assertIsNone(routing.resolve_general_knowledge("怎么用PE/PB看估值", candidate=False, directory=None))
        decision = routing.resolve_general_knowledge("怎么用PE/PB看估值", candidate=True, directory=None)
        self.assertEqual((decision.kind, decision.intent, decision.rule_id),
            (routing.RouteKind.GENERAL_KNOWLEDGE, routing.IntentKind.GENERAL_KNOWLEDGE, "knowledge.general"))
        stale = replace(self.context.directory, retrieved_at=NOW - timedelta(days=8))
        with patch.object(type(stale), "assert_usable", side_effect=AssertionError("Hint is not authorization")):
            self.assertIsNone(routing.resolve_general_knowledge("PE低的平安银行值得买吗", candidate=True, directory=stale))

    def test_unique_intents_priorities_and_reasons_can_share_an_executor(self):
        self.assertEqual(tuple(spec.intent for spec in routing.ROUTE_SPECS), tuple(routing.IntentKind))
        self.assertEqual([spec.priority for spec in routing.ROUTE_SPECS], [0, 1, 5, 10, 11, 12, 20, 25, 30, 31, 32, 40, 50, 60, 70])
        self.assertGreater(sum(spec.kind == routing.RouteKind.TECHNICAL_RESEARCH for spec in routing.ROUTE_SPECS), 1)
        first, second = routing.ROUTE_SPECS[:2]
        for changed in (replace(second, priority=first.priority), replace(second, intent=first.intent),
                        replace(second, rule_id=first.rule_id)):
            with self.assertRaises(ValueError):
                routing._ordered_specs((first, changed))

    def test_sentence_context_matrix_and_exclusive_owner(self):
        rows = (
            ("把PE改为低于15", "screening_edit"), ("按市净率升序选3只", "screening_edit"),
            ("再加换手率大于2%", "screening_edit"), ("筛选条件把PE改为低于15", "screening_edit"),
            ("帮我选5只股票", "screening"), ("筛选PE在10到20之间的股票", "screening"),
            ("分析第一只的基本面", "screening_followup"),
            ("分析筛选名单中的第一只的基本面", "screening_followup"),
            ("把第一只和第二只比较一下", "screening_followup"),
            ("第一只只看新闻，不要技术指标", "screening_followup"),
            ("第一只不是只看新闻，还要基本面", "screening_followup"),
            ("按市盈率分析浦发银行的估值", "company_research"),
            ("把浦发银行和平安银行的市盈率比较一下", "company_research"),
            ("分析浦发银行上市后第一个交易日以来的走势", "company_research"),
            ("分析浦发银行第三个季度的基本面", "company_research"),
            ("PE和PB有什么区别", "general_knowledge"), ("MACD是什么", "indicator_knowledge"),
            ("解释浦发银行的PE", "company_research"),
            ("市盈率怎么看，帮我分析浦发银行", "company_research"),
            ("分析平安银行", "company_research"), ("平安银行只看SMA3", "technical_research"),
            ("平安银行技术分析", "technical_research"), ("平安银行技术研究", "technical_research"),
            ("平安银行周线", "technical_research"), ("分析平安银行是否出现背离", "technical_research"),
            ("平安银行历史回放", "technical_research"), ("再加RSI2", "technical_research"),
        )
        for question, intent in rows:
            for field, owner in ((None, None), ("product_pending", "company_clarification"),
                    ("pending", "technical_clarification"), ("pending_edit", "indicator_clarification")):
                context = self.context if field is None else self.pending(field)
                new_named = intent in ("company_research", "technical_research") and bool(self.context.directory.mentioned_spans(question))
                expected = intent if owner is None or new_named or intent in ("screening", "general_knowledge", "indicator_knowledge") else owner
                with self.subTest(question=question, pending=field):
                    self.assertEqual(self.route(question, context=context).intent.value, expected)
                    value = routing._MatchInput(question, context=context)
                    candidates = {spec.intent.value for spec in routing.ROUTE_SPECS
                        if spec.intent in (routing.IntentKind.SCREENING, routing.IntentKind.SCREENING_EDIT,
                            routing.IntentKind.COMPANY_CLARIFICATION, routing.IntentKind.TECHNICAL_CLARIFICATION,
                            routing.IntentKind.INDICATOR_CLARIFICATION) and spec.matches(value)}
                    self.assertFalse({"screening", "screening_edit"} <= candidates)
                    self.assertLessEqual(len(candidates & {"company_clarification", "technical_clarification", "indicator_clarification"}), 1)

    def test_commands_and_history_keep_existing_executor_ownership(self):
        for field in (None, "product_pending", "pending", "pending_edit"):
            context = self.context if field is None else self.pending(field)
            for question, intent in (("取消", "command"), ("新对话", "command"), ("查看上次结果", "history")):
                with self.subTest(field=field, question=question):
                    decision = self.route(question, context=context)
                    self.assertEqual(decision.intent.value, intent)
                    self.assertEqual(decision.kind.value, "company_research" if field == "product_pending" else "technical_research")
        saved = replace(self.context, session=replace(self.context.session, product_saved=True))
        self.assertEqual(self.route("查看上次结果", context=saved).kind, routing.RouteKind.COMPANY_RESEARCH)
        self.assertEqual(self.route("查看上一次结果", context=saved).kind, routing.RouteKind.TECHNICAL_RESEARCH)
        self.assertNotEqual(self.route("取消之后分析平安银行").intent, routing.IntentKind.COMMAND)

    def test_pending_conflict_allows_independent_knowledge_and_screening_but_not_bare_selection(self):
        for fields in (("pending", "product_pending"), ("pending_edit", "product_pending"), ("pending", "pending_edit")):
            context = replace(self.context, session=replace(self.context.session, **{field: True for field in fields}))
            self.assertTrue(routing.probe_general_knowledge("怎么用PE/PB看估值", workflow=True, history=False, session=context.session))
            for question, intent in (("MACD是什么", routing.IntentKind.INDICATOR_KNOWLEDGE),
                    ("怎么用PE/PB看估值", routing.IntentKind.GENERAL_KNOWLEDGE),
                    ("帮我选5只股票", routing.IntentKind.SCREENING), ("平安银行", routing.IntentKind.STATE_CONFLICT)):
                self.assertEqual(self.route(question, context=context).intent, intent)
            self.assertEqual(self.route("取消", context=context).kind, routing.RouteKind.TECHNICAL_RESEARCH)
            self.assertEqual(self.route("查看上次结果", context=context).intent, routing.IntentKind.HISTORY)

    def test_disabled_knowledge_is_local_without_company_hint_evaluation(self):
        with patch.object(routing, "wants_product_research", side_effect=AssertionError("No company interpretation")):
            context = replace(self.context, workflow=False)
            self.assertEqual(self.route("怎么用PE/PB看估值", context=context).intent, routing.IntentKind.KNOWLEDGE_DISABLED)
            self.assertEqual(self.route("RSI是什么", context=context).intent, routing.IntentKind.INDICATOR_KNOWLEDGE)

    def test_named_member_uses_late_clock_and_explicit_expired_ordinal_reaches_executor(self):
        context = self.shortlist()
        for delta, expected in ((899, routing.RouteKind.SCREENING_FOLLOWUP), (900, routing.RouteKind.COMPANY_RESEARCH)):
            clock = Mock(return_value=NOW + timedelta(seconds=delta))
            self.assertEqual(self.route("分析平安银行的基本面", context=context, clock=clock).kind, expected)
            clock.assert_called_once_with()
            self.assertEqual(context.session.screening_saved.saved_at, NOW)
        self.assertEqual(self.route("分析名单中第一只", context=context,
            clock=lambda: NOW + timedelta(minutes=15)).kind, routing.RouteKind.SCREENING_FOLLOWUP)
        self.assertEqual(self.quotes.mock_calls, [])

    def test_early_owner_does_not_evaluate_company_or_read_list_clock(self):
        with patch.object(routing, "wants_product_research", side_effect=AssertionError("Owner already selected")):
            clock = Mock(side_effect=AssertionError("No list clock for pending reply"))
            self.assertEqual(self.route("平安银行", context=self.pending("pending"), clock=clock).intent,
                             routing.IntentKind.TECHNICAL_CLARIFICATION)
            clock.assert_not_called()

    def test_state_projection_is_detached_and_has_no_mutators(self):
        self.session.product_pending = object()
        self.session._pending = object()
        self.session._pending_edit = object()
        self.session.screening_saved = self.shortlist().session.screening_saved
        view = routing.RoutingSessionView.capture(self.session)
        self.session.clear()
        self.assertEqual(view.pending_count, 3)
        for name in ("clear", "begin_research", "_publish_turn"):
            self.assertFalse(hasattr(view, name))
        with self.assertRaises(FrozenInstanceError):
            view.pending = None
        with self.assertRaises(FrozenInstanceError):
            self.route("RSI是什么").rule_id = "changed"

    def test_company_disabled_keeps_technical_fallback(self):
        with patch.object(routing, "wants_product_research", side_effect=AssertionError("Company disabled")):
            self.assertEqual(self.route("分析平安银行的基本面", context=replace(self.context, company_enabled=False)).kind,
                             routing.RouteKind.TECHNICAL_RESEARCH)


if __name__ == "__main__":
    unittest.main()
