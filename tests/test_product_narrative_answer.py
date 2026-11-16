"""Current production synthesis: local retention, bounded repair, fallback and frozen prices; plus the JSON parser and window evidence."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import patch
import unittest

from tests.test_product_decision_answer import decision_bundle
from trading_companion.financial_periods import quarterly_evidence
from trading_companion.narrative_answer_parser import parse_narrative
from trading_companion.product_answer import synthesize_product_answer
from trading_companion.product_research import ProductEvidence
from trading_companion.research_price_context import price_context_evidence
from trading_companion.tool_model import ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import (
    TurnControl,
    TurnLimits,
    bind_turn,
    TurnStopped,
    ControlledToolModel,
)


# ---- test_product_narrative_answer: Current production synthesis: local retention, bounded repair, fallback and frozen prices.

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

    def test_instruction_never_suggests_combined_evidence_or_level_prefixes(self):
        from trading_companion.product_narrative_answer import INSTRUCTION
        for combined in ('E/L', 'L:E', 'E:L'):
            with self.subTest(combined=combined):
                self.assertNotIn(combined, INSTRUCTION)

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

    def test_duplicate_drivers_are_removed_after_checks_without_repair(self):
        def change(payload, attempt, context):
            driver = payload['narratives'][0]['drivers'][0]
            driver['evidence_ids'] = [context['evidence'][0]['id']]
            payload['narratives'][0]['drivers'] += [deepcopy(driver),
                {**deepcopy(driver), 'text': '同一因素的另一种表述。'}]
        run, model = self.run_answer(change)
        self.assertEqual(run.answer.status, 'accepted')
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(run.answer.narratives[0]['drivers']), 2)
        quality = run.answer.quality
        self.assertEqual(quality['deduplicated_drivers'], 2)
        self.assertEqual(quality['deleted_sentences'], quality['deduplicated_sentences'])
        self.assertEqual(quality['reasons'], [])

    def test_driver_dedupe_preserves_distinct_factors_and_stocks(self):
        from trading_companion.product_narrative_answer import _deduplicate_drivers
        drivers = [dict(title='走势', text='收盘需要观察。', evidence_ids=['E1']),
                   dict(title='量能', text='成交需要观察。', evidence_ids=['E1']),
                   dict(title='走势', text='周线需要观察。', evidence_ids=['E2']),
                   dict(title='主要依据', text='另一种因素。', evidence_ids=['E1'])]
        groups = [dict(drivers=deepcopy(drivers)), dict(drivers=deepcopy(drivers))]
        self.assertEqual(_deduplicate_drivers(groups), (0, 0))
        self.assertEqual([len(g['drivers']) for g in groups], [4, 4])

    def test_per_draft_issue_counts_keep_all_errors_after_successful_repair(self):
        def change(payload, attempt, context):
            if attempt == 1:
                payload['narratives'][0]['current']['text'] = '收盘999元。涨幅999%。'
                payload['narratives'][0]['stance']['text'] = '倾向卖出。'
        with patch('trading_companion.turn_trace.trace_answer_validation') as record:
            run, model = self.run_answer(change)
        self.assertEqual(run.answer.status, 'accepted')
        self.assertEqual(len(model.calls), 2)
        metadata = record.call_args.args[0]
        counts = metadata['draft_issue_counts']
        self.assertEqual(metadata['issue_count_version'], 'answer-issues-v1')
        self.assertGreaterEqual(counts[0]['answer_numeric_unmatched'], 2)
        self.assertGreaterEqual(counts[0]['answer_stance_mismatch'], 1)
        self.assertEqual(counts[1], {})

    def test_driver_normalization_preserves_numeric_sign_and_decimal_point(self):
        from trading_companion.product_narrative_answer import _deduplicate_drivers
        groups = [dict(drivers=[dict(title='观察依据', text=text, evidence_ids=['E1'])
                               for text in ('涨幅-1%。', '涨幅1%。', '价格1.2元。', '价格12元。')])]
        self.assertEqual(_deduplicate_drivers(groups), (0, 0))
        self.assertEqual(len(groups[0]['drivers']), 4)

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

    def test_one_valid_reason_survives_and_reports_only_deleted_count_and_codes(self):
        def change(p,n,c): p['decisions'][0]['reasons'][0]={'text':'收盘999999元。'}
        run,_=self.run_answer(change)
        decision=run.answer.decisions[0]
        self.assertEqual(decision['status'],'available')
        self.assertEqual(len(decision['reasons']),1)
        self.assertEqual(decision['reason_review'],dict(retained=1,deleted=1,codes=['answer_numeric_unmatched']))
        self.assertNotIn('999999',json.dumps(run.answer.to_dict()))

    def test_invalid_invalidation_uses_matching_program_plan_and_evidence(self):
        def change(p,n,c): p['decisions'][0]['invalidation']={'text':'止损999999元。'}
        run,_=self.run_answer(change)
        d=run.answer.decisions[0]
        self.assertEqual(d['status'],'available')
        self.assertEqual(d['invalidation_origin'],'application')
        selected=next(p for p in run.answer.trade_plans if p.plan_id==d['plan_id'])
        self.assertIn(dict(selected.values)['reference_stop'],d['invalidation']['text'])
        ids={e.id for e in run.answer.evidence}
        self.assertTrue(set(d['invalidation']['evidence_ids'])<=ids)

    def test_program_invalidation_needs_usable_same_purpose_plan(self):
        for rating,direction,purpose,field in (('持有','震荡','wait','observe_below'),('卖出','偏空','holding_exit','exit_below')):
            def change(p,n,c):
                p['decisions'][0].update(rating=rating,direction=direction,
                    plan_id=next(x['plan_id'] for x in c['trade_plans'] if x['purpose']==purpose),invalidation={'text':'止损999999元。'})
                p['narratives'][0].update(summary={'text':'条件性参考。'},stance={'text':'条件性参考。'})
            with self.subTest(purpose=purpose):
                run,_=self.run_answer(change)
                d=run.answer.decisions[0]
                selected=next(p for p in run.answer.trade_plans if p.plan_id==d['plan_id'])
                self.assertEqual(d['invalidation_origin'],'application')
                self.assertIn(dict(selected.values)[field],d['invalidation']['text'])
        from trading_companion.product_narrative_answer import _Checker, _context
        bundle=decision_bundle()
        context=_context(bundle)
        context['evidence']=[e for e in context['evidence'] if not e.get('plan_id')]
        bundle=replace(bundle,companies=tuple(replace(c,trade_plans=()) for c in bundle.companies))
        checker=_Checker(bundle,context)
        # Valid prose without a usable program plan may not acquire a condition.
        raw=dict(status='formed',rating='买入',direction='偏多',horizon=bundle.plan.objective.horizon,
                 conviction='低',reasons=[{'text':'条件性参考。'}],invalidation=None,plan_id=None)
        self.assertEqual(checker.decision(raw,'000001.SZ')['status'],'unavailable')

    def test_repair_content_rejected_after_merge_does_not_count_as_added(self):
        def change(p,n,c):
            p['narratives'][0]['current']={'text':'收盘999999元。'}
            if n==1:
                p['narratives'][0]['summary']={'text':'收盘999999元。'}
            else:
                p['decisions'][0].update(rating='卖出',direction='偏空',
                    plan_id=next(x['plan_id'] for x in c['trade_plans'] if x['purpose']=='holding_exit'))
                p['narratives'][0].update(summary={'text':'建议卖出。'},stance={'text':'建议卖出。'})
        run,_=self.run_answer(change)
        self.assertEqual(run.answer.decisions[0]['rating'],'买入')
        self.assertIsNone(run.answer.narratives[0]['summary'])
        self.assertFalse(run.answer.quality['repair_content_added'])

    def test_action_synonyms_negation_and_conditional_exits(self):
        variants=(('持有','震荡','wait',('持有观察。','观望。','继续持有。','等待确认。')),
                  ('增持','偏多','conditional_entry',('分批买入。','小仓位参与。','不建议卖出。','如果跌破止损则卖出。','如果证据转弱，应考虑卖出。')),
                  ('减持','偏空','holding_exit',('逢高减仓。','降低仓位。','不要买入。')))
        for rating,direction,purpose,texts in variants:
            for text in texts:
                def change(p,n,c):
                    p['decisions'][0].update(rating=rating,direction=direction,plan_id=next(x['plan_id'] for x in c['trade_plans'] if x['purpose']==purpose))
                    p['narratives'][0]['summary']={'text':'本次提供条件性参考。'}
                    p['narratives'][0]['stance']={'text':text}
                with self.subTest(text=text):
                    run,_=self.run_answer(change)
                    self.assertEqual(run.answer.decisions[0]['status'],'available')
                    self.assertIsNotNone(run.answer.narratives[0]['stance'])
        run,_=self.run_answer(lambda p,n,c:p['narratives'][0].update(stance={'text':'建议卖出。'}))
        self.assertEqual(run.answer.decisions[0]['rating'],'买入')
        self.assertIsNone(run.answer.narratives[0]['stance'])

    def test_valid_decision_survives_bad_narrative_type_and_wrappers_are_locally_recovered(self):
        run,_=self.run_answer(lambda p,n,c:p.update(narratives='wrong-type'))
        self.assertEqual(run.answer.decisions[0]['status'],'available')
        self.assertEqual(run.answer.quality['state'],'partial')
        run,model=self.run_answer(lambda p,n,c:'以下为结果：\n```json\n'+json.dumps(p,ensure_ascii=False)+'\n```')
        self.assertEqual(len(model.calls),1)
        self.assertEqual(run.answer.status,'accepted')
        self.assertEqual(run.answer.quality['local_structure_recoveries'],1)
        self.assertEqual(run.answer.quality['structure_failures'],1)

    def test_repair_outcomes_separate_full_success_rating_restoration_and_added_content(self):
        def change(p,n,c):
            if n==1:
                p['decisions'][0]['horizon']='未来2个月'
            else:
                p['narratives'][0]['current']={'text':'收盘999999元。'}
        run,model=self.run_answer(change)
        self.assertEqual(run.answer.quality['state'],'partial')
        self.assertTrue(run.answer.quality['repair_rating_restored'])
        self.assertTrue(run.answer.quality['repair_content_added'])
        self.assertFalse(run.answer.quality['repair_all_passed'])
        feedback=json.loads(model.calls[1][-1].content.split('程序会保留已通过检查的内容：',1)[1])
        self.assertEqual(feedback[0]['section'],'decision')
        self.assertTrue(feedback[0]['instrument_id'])
        self.assertEqual(feedback[0]['path'],'decisions[0].horizon')
        def repaired_reason(p,n,c):
            if n==1: p['decisions'][0]['reasons'][0]={'text':'收盘999999元。'}
        run,_=self.run_answer(repaired_reason)
        self.assertEqual(len(run.answer.decisions[0]['reasons']),2)
        self.assertNotIn('reason_review',run.answer.decisions[0])
        self.assertTrue(run.answer.quality['repair_content_added'])
        self.assertFalse(run.answer.quality['repair_rating_restored'])
        self.assertTrue(run.answer.quality['repair_all_passed'])


# ---- test_narrative_answer_parser: Only JSON framing can be repaired; content and ambiguous objects cannot.

class NarrativeParserTests(unittest.TestCase):
    def setUp(self):
        self.payload = dict(version='product-research-answer-v3',narratives=[dict(instrument_id='000001.SZ',summary={'text':'原句10.01元。'})],decisions=[])
        self.raw = json.dumps(self.payload,ensure_ascii=False)

    def test_exact_and_fenced_json_preserve_every_decoded_value(self):
        for prefix,suffix,recovered in (('','',False),('```json\n','\n```',True),('以下为分析：\n```json\n','\n```\n仅供参考。',True)):
            with self.subTest(prefix=prefix):
                result=parse_narrative(prefix+self.raw+suffix)
                self.assertEqual(result.payload,self.payload)
                self.assertEqual(result.recovered,recovered)
                self.assertEqual(result.raw_invalid,recovered)

    def test_complete_prefix_fields_and_array_items_survive_truncated_outer_json(self):
        raw='{"version":"product-research-answer-v3","narratives":'+json.dumps(self.payload['narratives'])+',"decisions":[{"instrument_id":'
        result=parse_narrative(raw)
        self.assertTrue(result.partial)
        self.assertEqual(result.payload['narratives'],self.payload['narratives'])
        self.assertNotIn('decisions',result.payload)
        raw='{"version":"product-research-answer-v3","narratives":['+json.dumps(self.payload['narratives'][0])+',{"summary":'
        self.assertEqual(parse_narrative(raw).payload['narratives'],self.payload['narratives'])

    def test_duplicate_nonfinite_conflicting_and_unsafe_wrappers_are_refused(self):
        for raw in (self.raw+self.raw, '保证收益。'+self.raw,
                    self.raw[:-1]+',"broken":?}'+self.raw,
                    self.raw[:-1]+',"decisions":[{},BROKEN,{"version":"other"}]',
                    '```json\n```json\n'+self.raw+'\n```\n```',
                    self.raw[:-1]+',"version":"product-research-answer-v3"}',
                    self.raw[:-1]+',"bad":NaN}',
                    '{"version":"unknown","narratives":[{}],"truncated":',
                    '{"version":"product-research-answer-v3","narratives":[{"text":',
                    'x'*26001):
            with self.subTest(raw=raw[:50]),self.assertRaises((ValueError,RecursionError)):
                parse_narrative(raw)


# ---- test_narrative_evidence: Window/quarter facts are derived from frozen eligible data, without extra collection.

class NarrativeEvidenceTests(unittest.TestCase):
    def test_windows_need_full_samples_and_ties_use_latest_date(self):
        bundle = decision_bundle()
        result = bundle.companies[0].technical
        evidence = price_context_evidence(result)
        by_label = {e.label: e for e in evidence}
        self.assertEqual(by_label["近20日最高价"].date, result.bars[-1].trade_date.isoformat())
        self.assertEqual(by_label["近20日最低价"].date, result.bars[-1].trade_date.isoformat())
        self.assertEqual(Decimal(by_label["MA20"].raw_value), Decimal("10"))
        self.assertEqual(Decimal(by_label["近60日涨跌幅"].raw_value), 0)
        self.assertEqual(len(by_label["MA60"].dependencies), 60)
        # Validate the dependency identity against the already frozen bar dates.
        bars = {b.trade_date.isoformat(): b for b in result.bars}
        for e in evidence:
            self.assertEqual(dict(e.calculation_basis)["price_adjustment"], result.price_adjustment)
            for dependency in e.dependencies:
                request, scope, day, field = dependency.split("|")
                self.assertEqual((request, scope), (result.request.request_id, "raw_daily_bar"))
                self.assertIsNotNone(getattr(bars[day], field))

    def test_full_window_boundaries_are_not_shortened(self):
        from tests.test_product_research import MemorySourceFactory
        class ShortSource(MemorySourceFactory):
            def __call__(self, request):
                source = super().__call__(request)
                read = source.read_daily_bars
                source.read_daily_bars = lambda: read()[-self.count:]
                return source
        factory = ShortSource()
        for count in (5, 6, 19, 20, 60, 61):
            factory.count = count
            result = decision_bundle(source=factory).companies[0].technical
            labels = {e.label for e in price_context_evidence(result)}
            self.assertEqual("近5日涨跌幅" in labels, count >= 6)
            self.assertEqual("近60日涨跌幅" in labels, count >= 61)
            self.assertEqual("MA20" in labels, count >= 20)
            self.assertEqual("MA60" in labels, count >= 60)

    def test_financial_quarter_difference_dependencies_and_yoy(self):
        def record(period, amount, **extra):
            return ProductEvidence(period, "000001.SZ", "financial", "营业收入", "累计", "fixture",
                date="2026-08-20", report_period=period, raw_value=amount, unit="cny", **extra)
        records = [record("2026-06-30", 900), record("2026-03-31", 400),
                   record("2025-06-30", 700), record("2025-03-31", 300)]
        quarter, yoy = quarterly_evidence(records)
        self.assertEqual(Decimal(quarter.raw_value), 500)
        self.assertEqual(quarter.dependencies, ("2026-06-30", "2026-03-31"))
        self.assertEqual(Decimal(yoy.raw_value), 25)
        self.assertEqual(len(yoy.dependencies), 4)
        self.assertEqual(quarter.temporal_scope, "financial_quarter_derived")
        self.assertEqual(len(quarterly_evidence(records[:2])), 1)
        self.assertEqual(quarterly_evidence(records[:1]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], source="other")]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], instrument_id="600000.SH")]), ())
        self.assertEqual(quarterly_evidence([records[0], replace(records[1], unit="USD")]), ())
        self.assertEqual(quarterly_evidence([records[0], records[1], records[1]]), ())
        self.assertEqual(Decimal(quarterly_evidence([records[1]])[0].raw_value), 400)


if __name__ == '__main__':
    unittest.main()
