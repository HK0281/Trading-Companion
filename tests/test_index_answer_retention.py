"""Same real seven-question drafts, plus paired safeguards for index-only recovery."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.answer_validation import check_claim
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.index_answer import checked_answer, public_index, validate_answer
from trading_companion.index_research import answer_context
from trading_companion.index_snapshot import validate_snapshot


class IndexAnswerRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / 'fixtures/index_answer_retention_live_20261030.json').read_text(encoding='utf-8'))
        cls.result = validate_snapshot(cls.fixture['samples'][3]['snapshot'])
        cls.context = answer_context(cls.result)
        _, cls.directory = load_development_fixtures()

    def claim(self, text, *, numbers=None, context=None, evidence_ids=None):
        return check_claim(dict(text=text, numbers=numbers or [], evidence_ids=evidence_ids or []),
            context=context or self.context, instrument='000001.SH', section='summary', index=0)

    def answer(self, text, *, title='技术依据', numbers=None):
        return validate_answer(dict(version='index-answer-v1', instrument_id='000001.SH',
            direction='偏空', conviction='中', horizon=None, blocks=[
                dict(title='结论', text='本轮技术观察偏空，把握中。', evidence_ids=['E1']),
                dict(title=title, text=text, evidence_ids=['E1'], numbers=numbers or [])]),
            self.result, directory=self.directory)

    def test_same_eight_raw_drafts_and_original_selected_seventy_three_sentences(self):
        self.assertIn('真实采集', self.fixture['provenance'])
        self.assertEqual(self.fixture['source_capture_sha256'], 'b746fc1da9df117b97c1c701a62fa0f185278a3938c4d60e6d3d9206de9bd6a9')
        generated = retained = original_generated = original_deleted = drafts = 0
        with patch('socket.socket.connect', side_effect=AssertionError('Live fixture replay must stay offline')):
            for sample in self.fixture['samples']:
                result = validate_snapshot(sample['snapshot'])
                for response in sample['responses']:
                    drafts += 1
                    self.assertEqual(sha256(response['text'].encode('utf-8')).hexdigest(), response['text_sha256'])
                    answer = validate_answer(json.loads(response['text']), result, directory=self.directory)
                    self.assertIsNotNone(answer, sample['question'])
                    published = checked_answer(result, answer)
                    self.assertEqual(checked_answer(result, published), published)
                    self.assertEqual(public_index(result, published)['quality'], published.quality.public())
                    if response['call_index'] == sample['captured_selected_call_index']:
                        generated += published.quality.generated_sentences
                        retained += published.quality.retained_sentences
                original_generated += sample['captured_quality']['generated_sentences']
                original_deleted += sample['captured_quality']['deleted_sentences']
        self.assertEqual((drafts, original_generated, original_deleted, generated), (8, 73, 22, 73))
        self.assertLess((generated-retained)/generated, .15)

    def test_dimensionless_readings_and_motion_keep_parameter_period_and_unit_checks(self):
        good = ('日线RSI(14)为28.33、周线RSI(14)为32.27，均偏弱。',
                '日线RSI自28.33回升需要后续观察。', '日线RSI(14)的28.33继续走低需要观察。')
        bad = ('日线RSI(14)为32.27、周线RSI(14)为28.33。',
               '日线RSI(6)为28.33。', '日线RSI(14)为28.33元。',
               '日线RSI(14)为28.34。', '日线RSI(14)为28.33、周线RSI(14)为32.27，均超卖。')
        for text in good + bad:
            with self.subTest(text=text):
                self.assertEqual(bool(self.claim(text)[0]), text in good)

    def test_verified_unused_annotations_never_authorize_wrong_values_or_refs(self):
        text = '日线MACD柱值为负。'
        valid = dict(text='-15.79点', ref='E9')
        self.assertEqual(self.claim(text, numbers=[valid])[0], text)
        for invalid in ({'text': '-15.80点', 'ref': 'E9'}, {**valid, 'ref': 'E999'},
                        {**valid, 'field': 'target_1'}, {**valid, 'derive': 'distance'}):
            with self.subTest(binding=invalid):
                self.assertFalse(self.claim(text, numbers=[invalid])[0])
        foreign = deepcopy(self.context)
        foreign['evidence'].append(dict(self.context['evidence'][8], id='E99', instrument_id='000300.SH'))
        self.assertFalse(self.claim(text, numbers=[dict(valid, ref='E99')], context=foreign)[0])
        # All original stock semantics remain opt-in: do not ignore unused prose bindings there.
        stock = deepcopy(self.context)
        stock.pop('instrument_kind')
        self.assertFalse(self.claim(text, numbers=[valid], context=stock)[0])

    def test_misplaced_refs_recover_only_exact_named_same_turn_fields(self):
        # BOLL middle and SMA20 happen to share a value; reuse of one citation
        # for all identical literals must not erase the true SMA reading.
        self.assertTrue(self.claim('日线SMA(20)的3735.83点是历史读数。',
            numbers=[dict(text='3735.83点', ref='E11')])[0])
        self.assertTrue(self.claim('收盘3599.02点。', numbers=[dict(text='3599.02点', ref='E13')])[0])
        for text, binding in [
            ('日线SMA(60)为3735.83点。', dict(text='3735.83点', ref='E11')),
            ('周线SMA(20)为3735.83点。', dict(text='3735.83点', ref='E11')),
            ('收盘3596.84点。', dict(text='3596.84点', ref='E13')),
            ('日线SMA(20)为3735.83点。', dict(text='3735.83点', ref='E999')),
            ('日线SMA(20)为3735.83点。', dict(text='3735.83点', ref='E11', field='middle')),
            ('日线SMA(20)为3735.83元。', dict(text='3735.83元', ref='E11'))]:
            with self.subTest(text=text, binding=binding):
                self.assertFalse(self.claim(text, numbers=[binding])[0])

    def test_only_program_declared_window_cap_is_metadata(self):
        good = '研究窗口内最近至多120条日线最低点为3574.27点。'
        self.assertEqual(self.claim(good)[0], good)
        for text in ('研究窗口内最近至多121条日线最低点为3574.27点。',
                     '研究窗口实际有120条日线。', '收盘120点。',
                     '研究窗口内最近至多120条日线最低点为4258.86点。'):
            self.assertFalse(self.claim(text)[0], text)

    def test_disclaimers_and_judgement_limits_cannot_hide_trading_or_positive_facts(self):
        good = ('本轮未提供成交量、资金流、板块轮动与宏观数据，无法据此判断；指数不可直接交易，不构成交易建议或目标价。',
                '本轮未提供成交量、资金流、板块或宏观数据，无法对量能与外部驱动作出判断。',
                '日线ATR(14)为53.03点，仅作波动参照，而非目标位。')
        bad = ('不构成交易建议，目标位3735.83点。', '不构成交易建议或目标价，建议买入。',
               '而非目标位3735.83点。', '不构成买入建议。',
               '未提供成交量，但资金流已经回流。', '无法对量能作出判断，政策已经推动指数上涨。')
        for text in good + bad:
            with self.subTest(text=text):
                answer = self.answer(text, title='局限')
                self.assertEqual(text in answer.text(self.result), text in good)

    def test_nonnumeric_limitations_recheck_without_twenty_four_fallback_refs(self):
        answer = self.answer('本轮未提供成交量，无法判断量能。周线SMA(60)因历史不足不可用。', title='局限')
        self.assertGreater(len(self.context['evidence']), 20)
        self.assertLessEqual(len(answer.blocks[-1][2]), 20)
        self.assertEqual(checked_answer(self.result, answer), answer)
        self.assertEqual(answer.quality.deleted_sentences, 0)

    def test_deleted_context_does_not_leave_orphan_contrast_or_causal_assertions(self):
        for transition in ('不过，', '然而，', '但是，'):
            text = '收盘999999点。' + transition + '日线RSI(14)为28.33。'
            answer = self.answer(text)
            self.assertIn('日线RSI(14)为28.33。', answer.text(self.result))
            self.assertNotIn(transition, answer.text(self.result))
            self.assertEqual(answer.quality.deleted_sentences, 1)
            self.assertEqual(checked_answer(self.result, answer), answer)
        for dependent in ('不过，这说明趋势转强。', '因此，日线趋势已经转强。', '不过，处于低位读数区间。'):
            answer = self.answer('收盘999999点。' + dependent)
            self.assertNotIn(dependent, answer.text(self.result))
            self.assertEqual(answer.quality.deleted_sentences, 2)
        # An intact contrast still has its preceding independent assertion.
        intact = '完整日线收盘3599.02点。不过，日线RSI(14)为28.33。'
        self.assertIn(intact, self.answer(intact).text(self.result))

    def test_current_price_relations_and_weekly_close_cannot_borrow_daily_fact(self):
        good = '价格位于周线中轨下方、下轨下方。'
        bad = ('价格位于周线中轨下方、下轨上方。', '周线收盘参考点位3599.02点。')
        for text in (good, *bad):
            self.assertEqual(text in self.answer(text).text(self.result), text == good)
        conditional = '若价格位于周线下轨上方，则需要重新研究。'
        self.assertIn(conditional, self.answer(conditional).text(self.result))
        conditional = '若收盘回升，价格位于周线下轨上方，则需要重新研究。'
        self.assertIn(conditional, self.answer(conditional).text(self.result))
        self.assertNotIn('价格位于周线下轨上方', self.answer('价格位于周线下轨上方；若收盘回升，则重新研究。').text(self.result))


if __name__ == '__main__':
    unittest.main()
