"""Current production synthesis: local retention, bounded repair, fallback and frozen prices."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from trading_companion.product_answer import synthesize_product_answer
from trading_companion.tool_model import ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits, bind_turn, TurnStopped, ControlledToolModel
from tests.test_product_decision_answer import decision_bundle


class NarrativeModel:
    def __init__(self, change=None):
        self.calls, self.change = [], change

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        self.calls.append(messages)
        context = json.loads(messages[2].content.split("\n", 1)[1])
        from trading_companion.decision_demo import scripted_decision
        decisions = scripted_decision(context)["decisions"] if context["objective"]["kind"] == "decision" else []
        groups = []
        for s in context["securities"]:
            levels = [r for r in context["price_levels"] if r["instrument_id"] == s["instrument_id"]]
            claim = lambda text: dict(text=text)
            groups.append(dict(instrument_id=s["instrument_id"], summary=claim((context["objective"].get("horizon") or "近期") + "偏多，但仍存在回落风险。"),
                current=claim("目前走势仍需结合财务核对。"),
                drivers=[dict(title="走势依据", text="历史波动提供观察范围。"), dict(title="相反证据", text="现有信息仍有局限。")],
                levels=[dict(lower_id=levels[0]["id"], upper_id=levels[0]["id"], text="需要结合后续价格重新观察。") ] if levels else [],
                scenarios=dict(base=claim("趋势延续但不排除震荡。"), better=claim("若盈利改善则增强依据。"), worse=claim("若证据转弱则重评。")),
                stance=claim("倾向买入，仅供参考。")))
        payload = dict(version="product-research-answer-v3", narratives=groups, decisions=decisions)
        if self.change:
            value = self.change(payload, len(self.calls), context)
            if isinstance(value, BaseException): raise value
            if isinstance(value, str): return ToolModelResponse("complete", value)
        return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))


class NarrativeTests(unittest.TestCase):
    def run_answer(self, change=None, bundle=None, repair=True):
        model = NarrativeModel(change)
        bundle = bundle or decision_bundle()
        run = synthesize_product_answer("分析走势", bundle, model=model, repair_invalid=repair)
        return run, model

    def test_current_entry_returns_sections_with_independent_verdict_and_prices(self):
        run, model = self.run_answer()
        self.assertEqual((run.answer.status, len(model.calls)), ("accepted", 1))
        public = run.answer.to_dict()
        self.assertEqual(public["version"], "product-research-answer-v3")
        self.assertEqual(public["decisions"][0]["rating"], "买入")
        self.assertTrue(public["narratives"][0]["levels"])
        self.assertFalse(public["trading_enabled"])
        public["quality"]["code"] = "tampered"
        self.assertEqual(run.answer.quality["code"], "answer_ready")

    def test_five_natural_reproductions_and_horizon_alias_pass(self):
        variants = ("若后续收盘跌破止损9.19元，应重新评估。", "收盘价跌破20日均线时重评。",
                    "近一个月股价回落，需要核对方向。", "目标价可参考计划表中的止盈1。")
        for text in variants:
            def change(p,n,c):
                p["decisions"][0]["invalidation"] = dict(text=text.replace("9.19", next(
                    str(r["price"]) for r in c["price_levels"] if r["label"] == "reference_stop")))
                p["decisions"][0]["horizon"] = "一个月"
            b = decision_bundle()
            b = replace(b, plan=replace(b.plan, objective=replace(b.plan.objective, horizon="未来1个月")))
            with self.subTest(text=text):
                self.assertEqual(self.run_answer(change, b)[0].answer.status, "accepted")

    def test_wrong_numeric_sentence_is_deleted_but_model_verdict_survives(self):
        def change(p,n,c): p["narratives"][0]["current"]["text"] = "仍需观察走势。收盘999元。"
        run, model = self.run_answer(change)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(run.answer.decisions[0]["status"], "available")
        self.assertIn("仍需观察走势", run.answer.text)
        self.assertNotIn("999", run.answer.text)
        self.assertGreater(run.answer.quality["deleted_sentences"], 0)
        self.assertIn("收盘999元", model.calls[1][-1].content)

    def test_bad_verdict_keeps_narrative(self):
        def change(p,n,c): p["decisions"][0]["horizon"] = "两个月"
        run, _ = self.run_answer(change)
        self.assertEqual(run.answer.status, "limited")
        self.assertIsNone(run.answer.decisions[0]["rating"])
        self.assertIn("历史波动", run.answer.text)

    def test_one_structure_repair_and_complete_failure_have_facts(self):
        run, model = self.run_answer(lambda p,n,c: "not json" if n == 1 else None)
        self.assertEqual((run.answer.status, len(model.calls)), ("accepted", 2))
        self.assertEqual(run.answer.quality["structure_failures"], 1)
        run, model = self.run_answer(lambda p,n,c: "not json")
        self.assertEqual((run.answer.status, len(model.calls)), ("fallback", 2))
        self.assertTrue(run.answer.narratives[0]["current"])
        self.assertTrue(run.answer.trade_plans)
        self.assertIsNone(run.answer.decisions[0]["rating"])

    def test_hard_sentence_is_not_replayed_and_valid_reasons_remain(self):
        def change(p,n,c): p["narratives"][0]["current"]["text"] = "一定上涨。仍需观察趋势。"
        run, model = self.run_answer(change)
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn("一定上涨", run.answer.text)
        self.assertIn("仍需观察趋势", run.answer.text)

    def test_repair_exception_retains_valid_first_answer(self):
        def change(p,n,c):
            if n == 2: return RuntimeError("private-provider-failure")
            p["narratives"][0]["current"]["text"] = "收盘999元。"
        run, model = self.run_answer(change)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(run.answer.decisions[0]["status"], "available")
        self.assertNotIn("private-provider", str(run.answer.to_dict()))

    def test_budget_and_cancel_remain_stops(self):
        control = TurnControl(TurnLimits(model_calls=1))
        control.start()
        model = NarrativeModel(lambda p,n,c: "bad json")
        with bind_turn(control), self.assertRaises(TurnStopped):
            synthesize_product_answer("分析走势", decision_bundle(), model=ControlledToolModel(model, control), repair_invalid=True)

    def test_multi_company_omission_and_cross_company_level_do_not_contaminate(self):
        from tests.test_product_research import SECURITIES
        b = decision_bundle(securities=SECURITIES[:2])
        def change(p,n,c): p["narratives"][0]["levels"][0]["lower_id"] = next(r["id"] for r in c["price_levels"] if r["instrument_id"] != p["narratives"][0]["instrument_id"])
        run, _ = self.run_answer(change, b)
        self.assertEqual(len(run.answer.decisions), 2)
        self.assertEqual(run.answer.narratives[0]["levels"], [])

    def test_three_companies_fit_both_message_budgets(self):
        from tests.test_product_research import SECURITIES
        run, model = self.run_answer(bundle=decision_bundle(securities=SECURITIES[:3]))
        self.assertEqual((run.answer.status, len(model.calls)), ("accepted", 1))
        self.assertEqual(len(run.answer.decisions), 3)

    def test_repair_fills_slots_without_replacing_the_original_valid_driver(self):
        def change(p,n,c):
            if n == 1:
                p["narratives"][0]["drivers"][1]["text"] = "收盘999元。"
            else:
                p["narratives"][0]["summary"]["text"] = "修正版改写结论。"
                p["narratives"][0]["drivers"][0]["text"] = "修正版替换文字。"
        run, model = self.run_answer(change)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("历史波动提供观察范围", run.answer.text)
        self.assertIn("现有信息仍有局限", run.answer.text)
        self.assertNotIn("修正版替换文字", run.answer.text)
        self.assertNotIn("修正版改写结论", run.answer.text)
        self.assertNotIn("_slot", str(run.answer.to_dict()))
        self.assertNotIn("_incomplete", str(run.answer.to_dict()))

    def test_merged_stance_is_checked_against_retained_first_verdict(self):
        def change(p,n,c):
            if n == 1:
                p["narratives"][0]["stance"] = {"text": "收盘999元。"}
            else:
                p["decisions"][0].update(rating="卖出", direction="偏空", plan_id=next(
                    p["plan_id"] for p in c["trade_plans"] if p["purpose"] == "holding_exit"))
                p["narratives"][0]["stance"] = {"text": "建议卖出。"}
        run, _ = self.run_answer(change)
        self.assertEqual(run.answer.decisions[0]["rating"], "买入")
        self.assertIsNone(run.answer.narratives[0]["stance"])
        self.assertNotIn("建议卖出", run.answer.text)

    def test_public_decision_mutation_cannot_change_saved_answer(self):
        run, _ = self.run_answer()
        payload = run.answer.to_dict()
        payload["decisions"][0]["reasons"][0]["text"] = "changed"
        self.assertNotEqual(run.answer.decisions[0]["reasons"][0]["text"], "changed")

    def test_prose_horizon_is_checked_separately_from_verdict(self):
        def change(p,n,c): p["narratives"][0]["summary"]["text"] = "未来两个月偏多。"
        run, _ = self.run_answer(change)
        self.assertIsNone(run.answer.narratives[0]["summary"])
        self.assertEqual(run.answer.decisions[0]["status"], "available")
        self.assertNotIn("两个月", run.answer.text)

    def test_no_evidence_skips_model_and_missing_company_gets_facts(self):
        from tests.test_product_research import SECURITIES
        b = decision_bundle(securities=SECURITIES[:2])
        def change(p,n,c): p["narratives"] = p["narratives"][:1]
        run, _ = self.run_answer(change, b)
        self.assertEqual(len(run.answer.narratives), 2)
        self.assertEqual(run.answer.narratives[1]["origin"], "application")
        empty = replace(b, companies=tuple(replace(c, evidence=(), trade_plans=(), technical=None) for c in b.companies))
        # Corrupt decision context is refused before synthesis, without forwarding any prices.
        model = NarrativeModel()
        answer = synthesize_product_answer("分析走势", empty, model=model).answer
        self.assertEqual(model.calls, [])
        self.assertFalse(answer.trade_plans)

    def test_missing_structured_field_receives_one_specific_repair(self):
        def change(p,n,c):
            if n == 1:
                del p["narratives"][0]["current"]
        run, model = self.run_answer(change)
        self.assertEqual((run.answer.status, len(model.calls)), ("accepted", 2))
        self.assertIn("current", model.calls[1][-1].content)
        self.assertIsNotNone(run.answer.narratives[0]["current"])

