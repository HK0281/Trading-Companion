"""Source bindings use frozen inputs, including real-draft regressions and bad refs."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from trading_companion.answer_numeric_bindings import COUNTERS, atoms, BINDING_INVALID, BINDING_MISMATCH
from trading_companion.answer_validation import check_claim, HARD, NUMERIC
from tests.test_answer_validation import context
from tests.test_product_decision_answer import decision_bundle
from tests.test_product_narrative_answer import NarrativeModel
from trading_companion.product_narrative_answer import _context, _checked, synthesize_narrative_answer


class BindingTests(unittest.TestCase):
    def check(self, text, numbers=None, ctx=None, **extra):
        stats = {key: 0 for key in COUNTERS}
        result = check_claim(dict(text=text, **({'numbers': numbers} if numbers is not None else {}), **extra),
            instrument='000001.SZ', context=ctx or context(), section='current', index=0, stats=stats)
        return result, stats

    def test_bound_value_and_field_are_checked_without_role_window_or_date_guesses(self):
        text = '2026-09-24的20日前观察读数为29.7元。'
        result, stats = self.check(text, [{'text': '29.7元', 'ref': 'E1', 'field': 'latest_close'}])
        self.assertEqual(result[0], text)
        self.assertEqual((stats['numeric_tokens'], stats['bound_numbers'], stats['bound_matched']), (1, 1, 1))
        self.assertEqual(result[1], ['E1'])

    def test_resolved_wrong_values_and_foreign_bindings_do_not_fall_back(self):
        variants = [({'text': '29.7元', 'ref': 'E2'}, BINDING_MISMATCH),
                    ({'text': '29.7元', 'ref': 'E5'}, HARD)]
        for binding, code in variants:
            with self.subTest(binding=binding):
                result, stats = self.check('仍需观察。读数29.7元。', [binding])
                self.assertEqual(result[0], '仍需观察。')
                self.assertEqual(result[2][0].code, code)
                self.assertEqual(stats['bound_rejected'], 1)
                self.assertEqual(stats['unbound_matched'], 0)

    def test_invalid_ordinary_refs_fall_back_without_guessing_a_combined_id(self):
        ctx = context()
        ctx['evidence'].append({**ctx['evidence'][0], 'id': 'E49'})
        ctx['price_levels'] = [dict(id='L49', instrument_id='000001.SZ', price='29.7',
            evidence_id='E49', field='latest_close', label='最新收盘价', date='2026-09-25')]
        for valid in ('E49', 'L49'):
            self.assertEqual(self.check('读数29.7元。', [{'text': '29.7元', 'ref': valid}], ctx)[0][0], '读数29.7元。')
        for combined in ('L:E49', 'E:L49', 'E/L49'):
            with self.subTest(ref=combined):
                result, stats = self.check('仍需观察。读数29.7元。', [{'text': '29.7元', 'ref': combined}], ctx)
                self.assertEqual(result[0], '仍需观察。读数29.7元。')
                self.assertEqual(result[2], [])
                self.assertEqual(stats['invalid_binding_fallback_matched'], 1)
                self.assertEqual(stats['unbound_matched'], 0)

    def test_partial_bindings_still_check_every_other_number_in_the_sentence(self):
        result, stats = self.check('读数29.7元、另一个999元。', [{'text': '29.7元', 'ref': 'E1'}])
        self.assertFalse(result[0])
        self.assertEqual(stats['numeric_tokens'], 2)
        self.assertEqual((stats['bound_matched'], stats['unbound_rejected']), (1, 1))

    def test_text_locations_overlaps_arrays_and_unicode_are_not_silent_fallbacks(self):
        for numbers in (None, {}, [None], [{'text': '', 'ref': 'E1'}],
                        [{'text': 'absent99', 'ref': 'E1'}],
                        [{'text': '29.7元', 'ref': 'E1'}, {'text': '29.7', 'ref': 'E1'}]):
            with self.subTest(numbers=numbers):
                item = dict(text='读数29.7元。', numbers=numbers)
                self.assertFalse(check_claim(item, instrument='000001.SZ', context=context(), section='current', index=0)[0])
        result, _ = self.check('两处29.7元、29.7元。', [{'text': '29.7元', 'ref': 'E1'}])
        self.assertEqual(result[0], '两处29.7元、29.7元。')
        result, _ = self.check('ﬁ读数２９．７元。', [{'text': '２９．７元', 'ref': 'E1'}])
        self.assertTrue(result[0])

    def test_units_ranges_rounding_sign_and_periods_keep_fixed_boundaries(self):
        for text, fragment, ref, ok in (
                ('营业收入约53亿元。', '约53亿元', 'E2', True),
                ('营业收入5346000000元。', '5346000000元', 'E2', True),
                ('收盘约30元。', '约30元', 'E1', True),
                ('收盘30.5元。', '30.5元', 'E1', False),
                ('近20日跌5.2%。', '5.2%', 'E4', True),
                ('近20日跌+5.2%。', '+5.2%', 'E4', False),
                ('近20日涨-5.2%。', '-5.2%', 'E4', False),
                ('比例下降5.2个百分点。', '5.2个百分点', 'E4', False),
                ('1-9月读数29.7元。', '29.7元', 'E1', True)):
            with self.subTest(text=text):
                self.assertEqual(bool(self.check(text, [{'text': fragment, 'ref': ref}])[0][0]), ok)
        self.assertTrue(self.check('区间29.7—30元。', [{'text': '29.7—30元', 'ref': 'E1'}])[0][0])
        self.assertFalse(self.check('区间29.7—99元。', [{'text': '29.7—99元', 'ref': 'E1'}])[0][0])
        self.assertFalse(self.check('区间30—29.7元。')[0][0])

    def test_fallback_uses_all_supplied_own_records_and_records_actual_citation(self):
        result, stats = self.check('均线读数29.1元。', evidence_ids=['E2'])
        self.assertTrue(result[0])
        self.assertIn('E3', result[1])
        self.assertEqual(stats['unbound_matched'], 1)
        ctx = context()
        ctx['evidence'] = [r for r in ctx['evidence'] if r['id'] != 'E3']
        self.assertFalse(self.check('读数29.1元。', ctx=ctx)[0][0])
        self.assertFalse(self.check('读数99元。')[0][0])

    def test_exact_match_beats_tolerance_neighbour_but_ambiguity_still_rejects(self):
        ctx = context()
        ctx['evidence'].append({**ctx['evidence'][0], 'id': 'E6', 'raw_value': 29.8})
        self.assertTrue(self.check('读数29.7元。', ctx=ctx)[0][0])
        result, stats = self.check('读数29.75元。', ctx=ctx)
        self.assertFalse(result[0])
        self.assertEqual(stats['unbound_ambiguous'], 1)
        ctx['evidence'][-1]['raw_value'] = 29.7
        self.assertTrue(self.check('读数29.75元。', ctx=ctx)[0][0])

    def test_plan_fields_are_structured_and_explicit_labels_cannot_be_swapped(self):
        ctx = _context(decision_bundle())
        stop = next(r for r in ctx['price_levels'] if r['label'] == 'reference_stop')
        target = next(r for r in ctx['price_levels'] if r['label'] == 'target_2')
        for label, level, ok in (('止损', stop, True), ('止盈2', target, True), ('止损', target, False), ('止盈2', stop, False)):
            text = label + ' ' + level['price'] + '元。'
            numbers = [{'text': level['price']+'元', 'ref': level['id']}]
            self.assertEqual(bool(self.check(text, numbers, ctx)[0][0]), ok)
        for r in ctx['evidence']:
            if r.get('content_kind') == 'trade_plan':
                r['display'] = '止盈199999.00元；止盈288888.00元'
        self.assertFalse(self.check('读数99999元。', ctx=ctx)[0][0])
        self.assertTrue(self.check('读数'+target['price']+'元。', ctx=ctx)[0][0])

    def test_level_refs_expand_but_missing_nonprice_cross_stock_and_ambiguous_endpoints_do_not(self):
        from trading_companion.answer_numeric_bindings import level_ref, InvalidBinding
        ctx = _context(decision_bundle())
        level = next(r for r in ctx['price_levels'] if r['label']=='reference_stop')
        result, _ = self.check('风险仍需观察。',ctx=ctx,evidence_ids=[level['id']])
        self.assertEqual(result[1],[level['evidence_id']])
        single = next(r for r in ctx['price_levels'] if r.get('field')=='latest_close')
        self.assertEqual(level_ref(single['evidence_id'],ctx,'000001.SZ')['id'],single['id'])
        with self.assertRaises(InvalidBinding):
            level_ref(level['evidence_id'],ctx,'000001.SZ')
        with self.assertRaises(InvalidBinding):
            level_ref('E404',ctx,'000001.SZ')
        foreign = deepcopy(ctx)
        foreign['price_levels'][ctx['price_levels'].index(level)]['instrument_id']='600000.SH'
        result, _ = self.check('读数'+level['price']+'元。',
            [{'text':level['price']+'元','ref':level['id']}],foreign)
        self.assertEqual(result[2][0].code,HARD)
        trimmed = deepcopy(ctx)
        trimmed['evidence']=[r for r in trimmed['evidence'] if r['id']!=level['evidence_id']]
        self.assertFalse(self.check('读数'+level['price']+'元。',
            [{'text':level['price']+'元','ref':level['id']}],trimmed)[0][0])

    def test_title_numbers_are_typed_and_cited_as_title_evidence(self):
        ctx = context()
        ctx['evidence'].append(dict(id='E6', instrument_id='000001.SZ', kind='news',
            label='新闻标题', display='公司称单季利润下降25.93%', raw_value='标题', unit='text'))
        result, _ = self.check('新闻标题称单季利润下降25.93%。', [{'text':'25.93%', 'ref':'E6'}], ctx)
        self.assertTrue(result[0])
        self.assertIn('E6', result[1])
        self.assertTrue(all(a.source == 'title' for a in atoms(ctx['evidence'][-1])))
        self.assertFalse(self.check('新闻标题称单季利润下降35.93%。', [{'text':'35.93%', 'ref':'E6'}], ctx)[0][0])

    def test_distance_requires_ordered_supplied_single_prices_and_is_only_derived_type(self):
        ctx = context()
        ctx['evidence'][0]['raw_value'] = '11.54'
        ctx['evidence'][2]['raw_value'] = '11.20'
        result, _ = self.check('距离约2.9%。', [{'text':'约2.9%', 'derive':'distance', 'refs':['E1','E3']}],ctx)
        self.assertEqual(result[1], ['E1','E3'])
        for refs, derive, text, ok in ((['E1', 'E3'], 'distance', '约2.9%', True),
                                      (['E3', 'E1'], 'distance', '约2.9%', False),
                                      (['E1', 'E5'], 'distance', '约2.9%', False),
                                      (['E1', 'E404'], 'distance', '约2.9%', False),
                                      (['E1', 'E3'], 'sum', '约2.9%', False),
                                      (['E1', 'E3'], 'distance', '约3.9%', False)):
            with self.subTest(refs=refs, derive=derive, text=text):
                result, _ = self.check('距离'+text+'。', [{'text':text, 'derive':derive, 'refs':refs}], ctx)
                self.assertEqual(bool(result[0]), ok)
        ctx['evidence'][0]['raw_value'] = 0
        self.assertFalse(self.check('距离2.9%。', [{'text':'2.9%', 'derive':'distance', 'refs':['E1','E3']}], ctx)[0][0])


class BindingIntegrationTests(unittest.TestCase):
    def test_every_text_slot_accepts_bindings_and_public_answer_strips_them(self):
        def change(payload, attempt, ctx):
            close = next(e for e in ctx['evidence'] if e.get('field') == 'latest_close')
            claim = dict(text=close['raw_value']+'元。', numbers=[dict(text=close['raw_value']+'元', ref=close['id'])])
            n = payload['narratives'][0]
            for key in ('summary', 'current', 'stance'):
                n[key] = deepcopy(claim)
            n['drivers'] = [dict(title='依据', **deepcopy(claim))]
            n['scenarios'] = {key:deepcopy(claim) for key in ('base','better','worse')}
            n['levels'][0].update(deepcopy(claim))
            payload['decisions'][0].update(reasons=[deepcopy(claim)], invalidation=deepcopy(claim))
        run = synthesize_narrative_answer('分析走势', decision_bundle(), model=NarrativeModel(change))
        self.assertEqual(run.answer.quality['bound_matched'], 10)
        self.assertNotIn('"numbers"', json.dumps(run.answer.to_dict()))

    def test_correct_purpose_selection_rechecks_condition_and_does_not_change_rating_or_prices(self):
        b = decision_bundle()
        original_plans = deepcopy(b.companies[0].trade_plans)
        ctx = _context(b)
        from trading_companion.decision_demo import scripted_decision
        payload = scripted_decision(ctx)
        payload['version'] = 'product-research-answer-v3'
        payload['narratives'] = []
        raw = payload['decisions'][0]
        exit_plan = next(p for p in b.companies[0].trade_plans if p.purpose == 'holding_exit')
        raw.update(rating='持有', direction='震荡', plan_id=exit_plan.plan_id,
                   invalidation=dict(text='退出观察位'+dict(exit_plan.values)['exit_below']+'元。'))
        result = _checked(payload,b,ctx)[1][0]
        self.assertEqual((result['status'], result['rating'], result['direction']), ('available','持有','震荡'))
        self.assertTrue(result['plan_id'].endswith(':wait'))
        self.assertEqual(result['invalidation_origin'], 'application')
        self.assertEqual(b.companies[0].trade_plans, original_plans)
        for invalid in ('unknown', None):
            raw['plan_id'] = invalid
            self.assertEqual(_checked(payload,b,ctx)[1][0]['status'],'unavailable')
        raw['plan_id'] = exit_plan.plan_id
        wait = next(p for p in b.companies[0].trade_plans if p.purpose == 'wait')
        ambiguous = replace(b,companies=(replace(b.companies[0],trade_plans=(*b.companies[0].trade_plans,wait)),))
        self.assertEqual(_checked(payload,ambiguous,ctx)[1][0]['status'],'unavailable')
        foreign = replace(exit_plan,plan_id='foreign-plan',instrument_id='600000.SH')
        raw['plan_id'] = foreign.plan_id
        foreign_bundle = replace(b,companies=(replace(b.companies[0],trade_plans=(*b.companies[0].trade_plans,foreign)),))
        self.assertEqual(_checked(payload,foreign_bundle,ctx)[1][0]['status'],'unavailable')

    def test_real_draft_regressions_from_each_diagnostic_category(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/answer-numeric-a3.json').read_text(encoding='utf-8'))
        for example in fixture['examples']:
            with self.subTest(category=example['category'], source=example['source']):
                result = check_claim(example['claim'], instrument='000001.SZ',
                    context=fixture['contexts'][example['context_id']],section='current',index=0)
                # Frozen A3 fixture remains byte-identical; #243 intentionally supersedes this one negative.
                fallback = example['claim'].get('numbers') == [{'text': '11.54元', 'ref': 'E404'}]
                self.assertEqual(bool(result[0]), example['accepted'] or fallback)
