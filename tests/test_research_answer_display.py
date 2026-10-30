"""Public synthesis regressions for display cleanup without changing price guards."""
from dataclasses import replace
from decimal import Decimal
import json
import unittest

from tests.test_answer_validation import context
from tests.test_product_decision_answer import decision_bundle
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import SECURITIES
from trading_companion.answer_validation import check_claim, STRUCTURE
from trading_companion.product_narrative_answer import synthesize_narrative_answer, _level_label


class AnswerDisplayTests(unittest.TestCase):
    def run_answer(self, change, *, bundle=None, repair=False):
        model = NarrativeModel(change)
        run = synthesize_narrative_answer('分析走势', bundle or decision_bundle(), model=model, repair_invalid=repair)
        return run.answer, model

    def test_all_program_plan_endpoints_have_chinese_labels_and_unchanged_prices(self):
        expected = dict(reference_stop='止损参考', reference_entry='入场参考', target_1='目标1', target_2='目标2',
                        reference_close='收盘参考', exit_below='退出观察位',
                        observe_below='下方观察位', observe_above='上方观察位')
        selected = []
        def change(p, attempt, ctx):
            levels = {r['field']: r for r in ctx['price_levels']}
            rows = []
            keys = list(expected)
            for i in range(0, len(keys), 2):
                low, high = sorted((levels[k] for k in keys[i:i+2]), key=lambda r: Decimal(r['price']))
                selected.append((low, high))
                rows.append(dict(lower_id=low['id'], upper_id=high['id'], text='条件满足时重新观察。'))
            p['narratives'][0]['levels'] = rows
        bundle = decision_bundle()
        answer, model = self.run_answer(change, bundle=bundle)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(answer.status, 'accepted')
        self.assertEqual(answer.trade_plans, tuple(p for c in bundle.companies for p in c.trade_plans))
        rows = answer.to_dict()['narratives'][0]['levels']
        for row, (low, high) in zip(rows, selected, strict=True):
            self.assertEqual((row['lower'], row['upper']), (f"{Decimal(low['price']):.2f}", f"{Decimal(high['price']):.2f}"))
            self.assertEqual((row['lower_label'], row['upper_label']), (expected[low['field']], expected[high['field']]))
        self.assertEqual(_level_label('latest_close'), '最新收盘价')
        self.assertEqual(_level_label('unknown_price_field'), '参考价位')
        self.assertEqual(_level_label('MA20'), 'MA20')
        self.assertEqual(_level_label('近20日最低价'), '近20日最低价')

    def test_cleared_references_and_already_empty_slots_are_rejected_locally(self):
        for text in ('自 E1 向上突破并站稳，才具备观察条件。', '至 E1 为上方参考目标区间。',
                     '自 向上突破并站稳，才具备观察条件。', '至 为上方参考目标区间。',
                     '从，继续观察。', '到。', '在。', '于。', '为。', '自 元向上突破。',
                     '后续收盘降至 E1。', '参考区间为（E1）。', '后续回落到 E1 附近。'):
            with self.subTest(text=text):
                result = check_claim({'text': text + '仍需观察。'}, instrument='000001.SZ', context=context(),
                                     section='levels', index=0)
                self.assertEqual(result[0], '仍需观察。')
                self.assertTrue(all(issue.code == STRUCTURE for issue in result[2]))

    def test_normal_prepositions_and_citations_do_not_look_like_empty_slots(self):
        for text in ('在条件满足时，继续观察。', '在 条件满足时，继续观察。', '为 提高判断把握，继续观察。',
                     '从 向上突破的形态看，仍有不确定性。', '自2026-09-25起继续观察。',
                     '收盘为29.7元（E1）。', '仍需观察公司行为。', '在上方压力附近观察。',
                     '仍需观察公司行为（E1）。', '在 E1 条件满足时，继续观察。'):
            with self.subTest(text=text):
                result = check_claim({'text': text}, instrument='000001.SZ', context=context(), section='current', index=0)
                self.assertEqual(result[0], text.replace('（E1）', '').replace('E1', '  '))
                self.assertEqual(result[2], [])

    def test_clause_recovery_cannot_retain_a_dangling_slot(self):
        from trading_companion.answer_clauses import independent_remainder
        self.assertIsNone(independent_remainder('至 为上方参考区间，读数999元。', ['至 为上方参考区间']))
        self.assertEqual(independent_remainder('在条件满足时继续观察，读数999元。', ['在条件满足时继续观察']),
                         '在条件满足时继续观察。')

    def test_empty_level_description_preserves_verified_row_and_repair_replaces_note(self):
        def change(p, attempt, ctx):
            if attempt == 1:
                row = p['narratives'][0]['levels'][0]
                row['text'] = f"自 {row['lower_id']} 向上突破并站稳，才具备观察条件。"
            else:
                p['narratives'][0]['levels'][0]['text'] = '条件满足时重新观察。'
        answer, model = self.run_answer(change)
        self.assertEqual(len(model.calls), 1)
        row = answer.to_dict()['narratives'][0]['levels'][0]
        self.assertEqual(row['text'], '程序计算的参考区间。')
        self.assertGreater(Decimal(row['lower']), 0)
        self.assertEqual(row['lower'], row['upper'])
        self.assertEqual(row['evidence_ids'], [])
        self.assertNotIn('_application', json.dumps(answer.to_dict()))
        self.assertEqual(answer.quality['retained_sentences'], answer.quality['generated_sentences'] - 1)
        answer, model = self.run_answer(change, repair=True)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(answer.narratives[0]['levels'][0]['text'], '条件满足时重新观察。')
        self.assertEqual(answer.quality['state'], 'complete')

    def test_bad_level_identity_does_not_get_a_program_row(self):
        def change(p, attempt, ctx):
            p['narratives'][0]['levels'][0].update(lower_id='L404', text='自 向上突破并站稳。')
        answer, _ = self.run_answer(change)
        self.assertEqual(answer.narratives[0]['levels'], [])

    def test_conflicting_confidence_is_removed_but_other_words_and_card_survive(self):
        def change(p, attempt, ctx):
            p['decisions'][0]['conviction'] = '低'
            n = p['narratives'][0]
            n['summary']['text'] = '总体偏多，**判断把握中等**，仍有回落风险。'
            n['current']['text'] = '把握**较高**，需要观察走势。'
            n['drivers'][0]['text'] = '走势需要观察，但把握为高。'
            n['drivers'][1]['text'] = '判断把握高。'
            n['scenarios']['base']['text'] = '判断把握为高。'
            n['stance']['text'] = '倾向买入，把握：中等。'
            n['levels'][0]['text'] = '判断把握中等。'
        answer, model = self.run_answer(change)
        n = answer.narratives[0]
        self.assertEqual(len(model.calls), 1)
        self.assertEqual((answer.decisions[0]['rating'], answer.decisions[0]['conviction']), ('买入', '低'))
        self.assertEqual(n['summary']['text'], '总体偏多，仍有回落风险。')
        self.assertEqual(n['current']['text'], '需要观察走势。')
        self.assertEqual(n['drivers'][0]['text'], '走势需要观察。')
        self.assertEqual(len(n['drivers']), 1)
        self.assertIsNone(n['scenarios']['base'])
        self.assertEqual(n['stance']['text'], '倾向买入。')
        self.assertEqual(n['levels'][0]['text'], '程序计算的参考区间。')
        self.assertNotIn('把握', answer.text)

    def test_matching_negated_hypothetical_and_nonassessment_words_are_preserved(self):
        for conviction, text in (('中', '**判断把握中等**。'), ('低', '判断把握较低。'),
                                 ('低', '判断把握不高。'), ('低', '并非判断把握高。'),
                                 ('低', '若判断把握高，则需要核对理由。'),
                                 ('低', '如果后续判断把握高，则需要核对理由。'),
                                 ('低', '不能说判断把握高。'),
                                 ('低', '仍需把握中长期节奏。'), ('低', '需要把握低位变化。')):
            def change(p, attempt, ctx):
                p['decisions'][0]['conviction'] = conviction
                p['narratives'][0]['summary']['text'] = text
            with self.subTest(text=text):
                answer, _ = self.run_answer(change)
                self.assertEqual(answer.narratives[0]['summary']['text'], text)

    def test_confidence_cleanup_preserves_bold_boundaries_around_retained_words(self):
        for text, expected in (('**判断把握中等，仍有风险**。', '**仍有风险**。'),
                               ('**总体偏多但判断把握中等**，仍有风险。', '**总体偏多**，仍有风险。'),
                               ('**判断把握中等**。**仍有风险**。', '**仍有风险**。'),
                               ('**总体偏多**，**判断把握**中等，仍有风险。', '**总体偏多**，仍有风险。')):
            def change(p, attempt, ctx):
                p['decisions'][0]['conviction'] = '低'
                p['narratives'][0]['summary']['text'] = text
            with self.subTest(text=text):
                answer, _ = self.run_answer(change)
                self.assertEqual(answer.narratives[0]['summary']['text'], expected)

    def test_confidence_is_compared_with_each_stock_card(self):
        def change(p, attempt, ctx):
            for i, (n, d) in enumerate(zip(p['narratives'], p['decisions'], strict=True)):
                d['conviction'] = ('低', '高')[i]
                n['summary']['text'] = '总体偏多，判断把握中等，仍需观察。'
                n['current']['text'] = f"判断把握{d['conviction']}。"
        answer, _ = self.run_answer(change, bundle=decision_bundle(securities=SECURITIES[:2]))
        self.assertEqual([n['summary']['text'] for n in answer.narratives], ['总体偏多，仍需观察。'] * 2)
        self.assertEqual([n['current']['text'] for n in answer.narratives], ['判断把握低。', '判断把握高。'])

    def test_confidence_uses_retained_first_card_after_repair_merge(self):
        def change(p, attempt, ctx):
            p['decisions'][0]['conviction'] = '低' if attempt == 1 else '高'
            p['narratives'][0]['summary']['text'] = '收盘999999元。' if attempt == 1 else '总体偏多，判断把握高，仍需观察。'
        answer, model = self.run_answer(change, repair=True)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(answer.decisions[0]['conviction'], '低')
        self.assertEqual(answer.narratives[0]['summary']['text'], '总体偏多，仍需观察。')

    def test_nondecision_has_no_card_confidence_to_impose(self):
        bundle = decision_bundle()
        bundle = replace(bundle, plan=replace(bundle.plan, objective=replace(bundle.plan.objective, kind='outlook')))
        answer, _ = self.run_answer(lambda p, a, c: p['narratives'][0]['summary'].update(text='判断把握中等。'), bundle=bundle)
        self.assertEqual(answer.narratives[0]['summary']['text'], '判断把握中等。')

    def test_unavailable_card_does_not_invent_a_confidence(self):
        def change(p, attempt, ctx):
            p['decisions'][0]['horizon'] = '未来2个月'
            p['narratives'][0]['summary']['text'] = '判断把握中等。'
        answer, _ = self.run_answer(change)
        self.assertEqual(answer.decisions[0]['status'], 'unavailable')
        self.assertEqual(answer.narratives[0]['summary']['text'], '判断把握中等。')
