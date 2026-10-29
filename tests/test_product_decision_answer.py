"""Fault injection for decisions: citations, purpose, numbers, repair and frozen evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.decision_demo import scripted_decision
from trading_companion.product_answer import synthesize_legacy_product_answer as synthesize_product_answer
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.research_objective import ResearchObjective
from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError
from trading_companion.tool_model import ToolModelResponse
from trading_companion.trade_plan import plan_display
from trading_companion.turn_control import TurnControl, TurnLimits, bind_turn, TurnStopped
from tests.test_product_research import plan, CompanyDataFixture, MemorySourceFactory, NOW, SECURITIES


def decision_bundle(*, securities=SECURITIES[:1], source=None):
    selected = replace(plan(securities=securities, kinds=("technical",)),
                       objective=ResearchObjective("decision", "未来20个交易日"), user_question="人工买卖决策")
    return collect_product_research(selected, data_service=CompanyDataFixture(), now=NOW,
                                    source_factory=source or MemorySourceFactory())


class DecisionModel:
    def __init__(self, change=None):
        self.calls, self.contexts, self.change = 0, [], change

    def complete(self, messages, *, tools):
        self.calls += 1
        context = json.loads(next(m.content.split("\n", 1)[1] for m in messages
                                  if m.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n")))
        self.contexts.append(context)
        payload = scripted_decision(context)
        if self.change:
            self.change(payload, self.calls, context)
        return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))


class ProductDecisionAnswerTests(unittest.TestCase):
    def setUp(self):
        self.bundle = decision_bundle()

    def run_answer(self, change=None, bundle=None):
        model = DecisionModel(change)
        run = synthesize_product_answer("现在能买吗", bundle or self.bundle, model=model, repair_invalid=True)
        return run, model

    def test_accepted_same_call_includes_all_program_prices(self):
        run, model = self.run_answer()
        payload = run.answer.to_dict()
        self.assertEqual((run.answer.status, model.calls), ("accepted", 1))
        self.assertEqual(payload["version"], "product-research-answer-v2")
        self.assertEqual(payload["decisions"][0]["rating"], "买入")
        self.assertEqual(payload["trade_plans"][0]["values"]["target_2"], "17.50")
        self.assertTrue(all(p["plan_id"] in {e.get("plan_id") for e in model.contexts[0]["evidence"]}
                            for p in model.contexts[0]["trade_plans"]))
        self.assertFalse(payload["trading_enabled"])

    def test_each_rating_must_select_matching_purpose(self):
        for rating, direction, purpose in (("持有", "震荡", "wait"), ("卖出", "偏空", "holding_exit"), ("增持", "偏多", "conditional_entry")):
            def change(payload, attempt, context):
                d = payload["decisions"][0]
                d.update(rating=rating, direction=direction, plan_id=next(p["plan_id"] for p in context["trade_plans"] if p["purpose"] == purpose))
            with self.subTest(rating=rating): self.assertEqual(self.run_answer(change)[0].answer.status, "accepted")

    def test_rejects_wrong_purpose_cross_turn_probability_and_direction_without_repair(self):
        for updates in ({"plan_id": "other-turn"}, {"conviction": 0.9}, {"direction": "偏空"}, {"rating": "卖出", "direction": "偏空"}):
            with self.subTest(updates=updates):
                run, model = self.run_answer(lambda p, n, c: p["decisions"][0].update(updates))
                self.assertEqual((run.answer.status, model.calls), ("fallback", 1))
                self.assertIsNone(run.answer.to_dict()["decisions"][0]["rating"])
                self.assertEqual(len(run.answer.trade_plans), 3)

    def test_missing_field_repaired_once_and_never_retried_indefinitely(self):
        def once(p, n, c):
            if n == 1: del p["decisions"][0]["conviction"]
        self.assertEqual(self.run_answer(once)[0].answer.status, "accepted")
        run, model = self.run_answer(lambda p, n, c: p["decisions"][0].pop("conviction"))
        self.assertEqual((run.answer.status, model.calls), ("fallback", 2))

    def test_wrong_stop_value_and_hidden_guarantee_are_not_repaired(self):
        def swap(p, n, c):
            e = next(e for e in c["evidence"] if e.get("plan_id") == p["decisions"][0]["plan_id"])
            p["decisions"][0]["invalidation"] = {"text": "参考止损14.50元。", "evidence_ids": [e["id"]]}
        for change in (swap, lambda p,n,c: p["decisions"][0]["reasons"][0].update(text="一定上涨，保证收益。")):
            run, model = self.run_answer(change)
            self.assertEqual((run.answer.status, model.calls), ("fallback", 1))

    def test_one_missing_field_cannot_hide_an_unsafe_paragraph(self):
        def bad(p, n, c):
            del p["decisions"][0]["conviction"]
            p["paragraphs"][0]["text"] = "保证收益。"
        self.assertEqual(self.run_answer(bad)[1].calls, 1)

    def test_top_level_error_cannot_make_a_guarantee_repairable(self):
        def bad(p, n, c):
            del p["version"]
            p["paragraphs"][0]["text"] = "保证收益。"
        self.assertEqual(self.run_answer(bad)[1].calls, 1)

    def test_companies_cannot_borrow_each_others_reasons_or_omit_one(self):
        bundle = decision_bundle(securities=SECURITIES[:2])
        self.assertEqual(self.run_answer(bundle=bundle)[0].answer.status, "accepted")
        def wrong(p,n,c):
            p["decisions"][0]["reasons"][0]["evidence_ids"] = p["decisions"][1]["reasons"][0]["evidence_ids"]
        self.assertEqual(self.run_answer(wrong, bundle)[0].answer.status, "fallback")
        self.assertEqual(self.run_answer(lambda p,n,c: p["decisions"].pop(), bundle)[1].calls, 2)

    def test_budget_cancellation_propagates_without_publishing_fallback(self):
        control = TurnControl(TurnLimits())
        control.start()
        model = DecisionModel(lambda p,n,c: control.cancel())
        with bind_turn(control), self.assertRaises(TurnStopped):
            synthesize_product_answer("能买吗", self.bundle, model=model)

    def test_snapshot_checks_new_plans_and_keeps_old_shapes(self):
        with TemporaryDirectory() as root:
            store = ResearchSnapshotStore(Path(root)/"test.sqlite3", clock=lambda: datetime.now(timezone.utc))
            try:
                key = store.save(self.bundle)
                self.assertEqual(len(store.load(key)["companies"][0]["trade_plans"]), 3)
                company = self.bundle.companies[0]
                bad = replace(company.trade_plans[0], values=(("reference_entry", "999"),))
                with self.assertRaises(SnapshotStoreError):
                    store.save(replace(self.bundle, companies=(replace(company, trade_plans=(bad,)),)))
            finally:
                store.close()

    def test_unavailable_atr_keeps_model_verdict_and_null_plan(self):
        source = MemorySourceFactory(fail=True)
        # A financial-only decision retains facts while explicitly declining a plan.
        selected = replace(plan(kinds=("financial",)), objective=ResearchObjective("decision", "未来20个交易日"))
        bundle = collect_product_research(selected, data_service=CompanyDataFixture(), now=NOW, source_factory=source)
        run, _ = self.run_answer(bundle=bundle)
        self.assertEqual(run.answer.status, "accepted")
        self.assertIsNone(run.answer.decisions[0]["plan_id"])
        self.assertTrue(all(p.status == "unavailable" for p in run.answer.trade_plans))

    def test_tampered_plan_cannot_leak_through_fallback(self):
        company = self.bundle.companies[0]
        bad = replace(company.trade_plans[0], values=(("reference_entry", "999"),))
        bundle = replace(self.bundle, companies=(replace(company, trade_plans=(bad,)),))
        run, model = self.run_answer(bundle=bundle)
        self.assertEqual(model.calls, 0)
        self.assertNotIn("999", run.answer.text)
        self.assertTrue(all(p.values == () for p in run.answer.trade_plans))

    def test_explicit_month_survives_without_trading_calendar(self):
        bundle = replace(self.bundle, plan=replace(self.bundle.plan, objective=ResearchObjective("decision", "未来1个月")))
        run, _ = self.run_answer(bundle=bundle)
        self.assertEqual(run.answer.decisions[0]["horizon"], "未来1个月")

    def test_three_stocks_keep_complete_plans_within_message_limits(self):
        bundle = decision_bundle(securities=SECURITIES[:3])
        run, model = self.run_answer(bundle=bundle)
        self.assertEqual(run.answer.status, "accepted")
        self.assertEqual(len(model.contexts[0]["trade_plans"]), 9)
        self.assertEqual(len(run.answer.decisions), 3)
        records = {e.get("plan_id"): e for e in model.contexts[0]["evidence"] if e.get("plan_id")}
        for company in bundle.companies:
            for candidate in company.trade_plans:
                self.assertEqual(records[candidate.plan_id]["display"], plan_display(candidate))
                self.assertEqual(records[candidate.plan_id]["date"], candidate.source_date)
