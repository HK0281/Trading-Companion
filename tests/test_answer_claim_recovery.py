"""Real misplaced-binding shapes still pass all ordinary source and value guards."""
import json
import unittest

from tests.test_answer_validation import context
from tests.test_product_decision_answer import decision_bundle
from tests.test_product_narrative_answer import NarrativeModel
from trading_companion.answer_validation import check_claim, HARD
from trading_companion.product_narrative_answer import synthesize_narrative_answer


class ClaimRecoveryTests(unittest.TestCase):
    def check(self, text, ctx=None, **extra):
        stats = {}
        result = check_claim(dict(text=text, **extra), instrument='000001.SZ', context=ctx or context(),
                             section='current', index=0, stats=stats)
        return result, stats

    def test_inline_arrays_recover_exact_text_and_use_normal_binding_validation(self):
        binding = dict(text='29.7元', ref='E1', field='latest_close')
        result, stats = self.check('收盘为' + json.dumps([binding], ensure_ascii=False) + '。')
        self.assertEqual(result[0], '收盘为29.7元。')
        self.assertEqual(result[1], ['E1'])
        self.assertEqual(stats['inline_binding_recoveries'], 1)
        self.assertEqual(stats['bound_matched'], 1)
        for change in (dict(ref='E404'), dict(ref='E5'), dict(text='99元'), dict(field='unknown'),
                       dict(extra=True), dict(ref='E2')):
            with self.subTest(change=change):
                broken = {**binding, **change}
                self.assertFalse(self.check('收盘' + json.dumps([broken], ensure_ascii=False) + '。')[0][0])

    def test_partial_duplicate_nested_and_ambiguous_bindings_are_not_repaired(self):
        variants = ['[{"text":"29.7元","ref":"E1"}',
            '[{"text":"29.7元","ref":"E404","ref":"E1"}]',
            '[{"text":"29.7元","ref":"E1"},null]',
            '[{"text":"29.7元","ref":"E1"}, {"text":"29.7元","ref":"E1"}]',
            '[{"text":"[{}]","ref":"E1"}]', '[{"text":"29.7元","ref":NaN}]']
        for value in variants:
            with self.subTest(value=value):
                self.assertFalse(self.check('收盘' + value + '。')[0][0])
        good = '[{"text":"29.7元","ref":"E1"}]'
        self.assertFalse(self.check('收盘29.7元，另为' + good + '。')[0][0])
        self.assertFalse(self.check('收盘' + good + '。', numbers=[dict(text='29.7元', ref='E1')])[0][0])
        self.assertEqual(self.check('收盘' + good + '<script>1</script>。')[0][2][0].code, HARD)

    def test_multiple_arrays_merge_existing_bindings_and_keep_unbound_checks(self):
        text = '收盘[{"text":"29.7元","ref":"E1"}]，均线[{"text":"29.1元","ref":"E3"}]。'
        result, stats = self.check(text)
        self.assertEqual(result[0], '收盘29.7元，均线29.1元。')
        self.assertEqual(stats['inline_binding_recoveries'], 2)
        self.assertFalse(self.check(text[:-1] + '，另有999元。')[0][0])
        result, stats = self.check(text, numbers=[dict(text='不存在', ref='E1')])
        self.assertFalse(result[0])

    def test_citation_markers_are_checked_cleaned_and_not_counted_as_data(self):
        for citation in ('（E1/E3）', '(E1/E3)', '（E3）', 'E1'):
            with self.subTest(citation=citation):
                result, stats = self.check('收盘29.7元' + citation + '。')
                self.assertEqual(result[0], '收盘29.7元。')
                self.assertEqual(stats['numeric_tokens'], 1)
        self.assertFalse(self.check('读数999元（E1）。')[0][0])
        self.assertFalse(self.check('读数29.7元（E404）。')[0][0])
        self.assertEqual(self.check('读数29.7元（E5）。')[0][2][0].code, HARD)
        self.assertFalse(self.check('读数RATE123。')[0][0])
        result, _ = self.check('（E1）。仍需观察。')
        self.assertEqual((result[0], result[3], result[4]), ('仍需观察。', 2, 1))
        result, _ = self.check('(E1/E3)')
        self.assertEqual((result[0], result[3], result[4]), ('', 1, 0))

    def test_level_markers_and_binding_positions_after_recovery(self):
        ctx = context()
        ctx['price_levels'] = [dict(id='L2', instrument_id='000001.SZ', evidence_id='E1',
                                    field='latest_close', price='29.7')]
        result, _ = self.check('（L2）收盘29.7元。', ctx, numbers=[dict(text='29.7元', ref='L2')])
        self.assertTrue(result[0])
        self.assertNotIn('L2', result[0])
        self.assertNotIn('(', result[0])

    def test_recovery_counts_survive_answer_and_trace_without_model_sidecar(self):
        def change(payload, attempt, ctx):
            self.assertNotIn('_technical_validation', ctx)
            close = next(e for e in ctx['evidence'] if e.get('field') == 'latest_close')
            payload['narratives'][0]['current'] = dict(text='收盘' + json.dumps([
                dict(text=str(close['raw_value'])+'元', ref=close['id'], field='latest_close')], ensure_ascii=False) + '。')
        run = synthesize_narrative_answer('分析走势', decision_bundle(), model=NarrativeModel(change))
        self.assertEqual(run.answer.quality['inline_binding_recoveries'], 1)
        self.assertEqual(run.answer.quality['repair_attempts'], 0)
        self.assertNotIn('[{', run.answer.narratives[0]['current']['text'])
        from trading_companion.turn_trace import safe_answer_validation
        self.assertEqual(safe_answer_validation(run.answer.quality)['inline_binding_recoveries'], 1)
