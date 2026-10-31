"""Index answer recovery regressions, using explicitly artificial data and prose."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.answer_validation import check_claim
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.evidence_numbers import display_number, same_display_number
from trading_companion.index_answer import (INSTRUCTION, PARTIAL_NOTICE, checked_answer,
    public_index, synthesize_index_answer, validate_answer)
from trading_companion.index_contracts import IndexResearchRequest, IndexResearchResult
from trading_companion.index_research import answer_context, index_evidence
from trading_companion.index_source import synthetic_index_bars
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.tool_model import ToolModelResponse


class IndexAnswerTests(unittest.TestCase):
    def setUp(self):
        _, self.directory = load_development_fixtures()
        request = IndexResearchRequest('000001.SH', date(2026, 5, 1), date(2026, 9, 25),
            (IndicatorSpec('sma', window=20), IndicatorSpec('rsi', window=14)), source='csv')
        self.result = IndexResearchResult(request, tuple(synthetic_index_bars(request)),
            datetime(2026, 9, 26, tzinfo=timezone.utc))

    def payload(self, *blocks):
        return dict(version='index-answer-v1', instrument_id='000001.SH', direction='偏空',
            conviction='低', horizon=None, blocks=[dict(title='结论', text='本轮技术观察偏空，把握较低。', evidence_ids=['E1']),
            *[dict(title=t, text=s, evidence_ids=['E1']) for t, s in blocks]])

    def validate(self, value):
        return validate_answer(value, self.result, directory=self.directory)

    def test_single_valid_conclusion_is_sufficient_and_unknown_titles_are_kept(self):
        answer = self.validate(self.payload())
        self.assertEqual((answer.direction, answer.conviction), ('偏空', '低'))
        self.assertFalse(answer.quality.partial)
        value = self.payload(('结构说明', '本轮仅观察历史点位。'), ('情景与观察重点', '走强或走弱均需重新评估。'),
                             ('限制与反向证据', '本轮不含成交量、资金流、板块或宏观信息。'))
        answer = self.validate(value)
        self.assertEqual([b[0] for b in answer.blocks], ['结论', '结构说明', '情景', '局限'])
        value['blocks'][0]['title'] = '未分类说明'
        self.assertIsNone(self.validate(value))

    def test_compound_titles_follow_the_first_category_word(self):
        from trading_companion.index_answer import _title
        for title, expected in [('情景与反向条件', '情景'), ('情景与观察', '情景'), ('观察与情景', '观察重点'),
                                ('限制与情景', '局限'), ('反向证据与情景', '局限'), ('当前风险', '当前情况'),
                                ('结构说明', '结构说明')]:
            self.assertEqual(_title(title), expected)

    def test_subjectless_remainders_are_removed_but_complete_readings_survive(self):
        from trading_companion.answer_clauses import has_empty_slot, independent_remainder
        close = answer_context(self.result)['evidence'][0]['display_value']
        for orphan in ('处于低位读数区间。', '位于区间下沿。', '仅作区间参照。', '显示超买。'):
            answer = self.validate(self.payload(('技术依据', '收盘999999点，' + orphan)))
            self.assertNotIn(orphan, answer.text(self.result))
            self.assertTrue(answer.quality.partial)
            answer = self.validate(self.payload(('技术依据', orphan)))
            self.assertNotIn(orphan, answer.text(self.result))
            # Existing stock recovery semantics stay opt-in and unchanged.
            self.assertFalse(has_empty_slot(orphan))
        full = f'完整日线收盘{close}点。'
        self.assertIn(full, self.validate(self.payload(('当前情况', full))).text(self.result))
        self.assertIsNotNone(independent_remainder('读数999999，处于低位读数区间。', ['处于低位读数区间']))

    def test_bad_sentence_does_not_discard_valid_sentences_or_other_blocks(self):
        value = self.payload(('技术依据', '仅观察已完成日线。收盘999999点。后续变化需要重新研究。'),
                             ('局限', '未取得成交量数据，无法判断量能。'))
        answer = self.validate(value)
        self.assertEqual((answer.direction, answer.conviction), ('偏空', '低'))
        self.assertNotIn('999999', answer.text(self.result))
        self.assertIn('仅观察已完成日线。后续变化需要重新研究。', answer.text(self.result))
        self.assertEqual((answer.quality.generated_sentences, answer.quality.retained_sentences,
                          answer.quality.deleted_sentences), (5, 4, 1))
        self.assertIn(PARTIAL_NOTICE, answer.text(self.result))
        rechecked = checked_answer(self.result, answer)
        self.assertEqual(rechecked, answer)
        self.assertEqual(public_index(self.result, rechecked)['quality']['deleted_sentences'], 1)

    def test_invalid_paragraphs_and_titles_cannot_hide_in_valid_conclusion(self):
        for bad in [None, dict(title='技术依据', text='无效引用。', evidence_ids=['E999']),
                    dict(title='风险：收盘999999点', text='说明。', evidence_ids=['E1']),
                    dict(title='沪深300观察', text='说明。', evidence_ids=['E1']),
                    dict(title='<b>结论</b>', text='说明。', evidence_ids=['E1'])]:
            with self.subTest(bad=bad):
                value = self.payload()
                value['blocks'].append(bad)
                answer = self.validate(value)
                self.assertIsNotNone(answer)
                self.assertEqual(len(answer.blocks), 1)
                self.assertTrue(answer.quality.partial)

    def test_conclusion_must_survive_with_matching_explicit_direction(self):
        for text in ('偏多。', '后续需要观察。', '偏空，完整日线收盘999999点。'):
            value = self.payload(('当前情况', '只观察历史读数。'))
            value['blocks'][0]['text'] = text
            self.assertIsNone(self.validate(value), text)
        value['blocks'][0]['text'] = '当前偏空。后续震荡。'
        answer = self.validate(value)
        self.assertEqual(answer.blocks[0][1], '当前偏空。')
        self.assertEqual(answer.quality.deleted_sentences, 1)

    def test_scope_allows_limits_and_rejects_positive_assertions_sentence_by_sentence(self):
        allowed = ('本轮不含成交量、资金流、板块或宏观信息。', '未取得成交量数据，无法判断量能。',
                   '经济与政策不在本轮研究范围。', '本轮未研究宏观经济政策。',
                   '本轮无法判断资金流是否正在改善。',
                   '持有期限应由用户自行确定。', '采用多元视角核对历史点位。')
        forbidden = ('资金流正在改善。', '海外降息提振指数。', '建议持有。', '给予持有评级。',
                     '建议买入。', '完整日线收盘1000元。', '目标位1000点。',
                     '未取得成交量，但资金流改善。', '本轮不含宏观信息，政策宽松提振指数。',
                     '缺少成交量数据且资金流改善。', '沪深300技术面偏空。', '平安银行值得持有。')
        for text in allowed + forbidden:
            with self.subTest(text=text):
                value = self.payload(('局限', text + '仅观察历史点位。'))
                answer = self.validate(value)
                self.assertEqual(text in answer.text(self.result), text in allowed)
                self.assertIn('仅观察历史点位。', answer.text(self.result))

    def test_display_rounding_keeps_raw_bars_and_kernel_values_unchanged(self):
        before = self.result.to_dict()
        series = [dict(label='SMA20', indicator_id='sma(window=20)', field='sma', unit='index_point', values=[3735.8304999999955]),
                  dict(label='RSI14', indicator_id='rsi(window=14)', field='rsi', unit='index_points', values=[28.330692955698602])]
        with patch('trading_companion.index_research.index_series', return_value=series):
            context = answer_context(self.result)
        sma = next(e for e in context['evidence'] if e['indicator_id'] == 'sma(window=20)')
        rsi = next(e for e in context['evidence'] if e['indicator_id'] == 'rsi(window=14)')
        self.assertEqual((sma['raw_value'], sma['display_value']), (3735.83, '3735.83'))
        self.assertEqual((rsi['raw_value'], rsi['display_value']), (28.33, '28.33'))
        self.assertEqual(self.result.to_dict(), before)
        def check(text, binding):
            return check_claim(dict(text=text, evidence_ids=[sma['id']], numbers=[binding]),
                instrument='000001.SH', context=context, section='summary', index=0)
        good = dict(text='3735.8304999999955点', ref=sma['id'])
        self.assertFalse(check('日线SMA20为3735.83点。', good)[2])
        self.assertFalse(check('日线SMA20为3735.8305点。', good)[2])
        for text, binding in [('日线SMA20为3735.84点。', good),
                              ('日线SMA20为3735.83元。', good),
                              ('指数收盘为28.33点。', dict(text='28.330692955698602点', ref=rsi['id'])),
                              ('日线SMA20为3735.83点。', {**good, 'field': 'target_1'}),
                              ('日线SMA20为3735.83点。', {**good, 'ref': 'E999'})]:
            self.assertTrue(check(text, binding)[2], text)
        self.assertTrue(same_display_number(Decimal('3735.8305'), Decimal('3735.83')))
        self.assertFalse(same_display_number(Decimal('3735.8351'), Decimal('3735.83')))
        self.assertEqual(display_number(Decimal('2.345')), '2.34')

    def test_partial_answer_stops_without_repair_and_failed_conclusion_gets_one_repair(self):
        class Model:
            def __init__(inner, payloads):
                inner.payloads = iter(payloads)
                inner.calls = 0
            def complete(inner, messages, *, tools):
                inner.calls += 1
                return ToolModelResponse('complete', text=json.dumps(next(inner.payloads), ensure_ascii=False))
        partial = self.payload(('当前情况', '收盘999999点。'))
        model = Model([partial])
        answer, _ = synthesize_index_answer('上证指数走势', self.result, model=model, directory=self.directory)
        self.assertEqual(model.calls, 1)
        self.assertEqual(answer.origin, 'model')
        self.assertTrue(answer.quality.partial)
        invalid = deepcopy(partial)
        invalid['blocks'][0]['text'] = '方向偏多。'
        model = Model([invalid, self.payload()])
        answer, _ = synthesize_index_answer('上证指数走势', self.result, model=model, directory=self.directory)
        self.assertEqual((model.calls, answer.direction, answer.quality.repair_attempts), (2, '偏空', 1))
        model = Model([invalid, invalid])
        answer, _ = synthesize_index_answer('上证指数走势', self.result, model=model, directory=self.directory)
        self.assertEqual((model.calls, answer.origin, answer.direction), (2, 'program', '暂不判断'))
        self.assertEqual(checked_answer(self.result, answer), answer)
        self.assertIn('display_value exactly', INSTRUCTION)
        self.assertIn('局限', INSTRUCTION)

    def test_rounding_does_not_hide_raw_snapshot_changes(self):
        from trading_companion.index_snapshot import snapshot_data, validate_snapshot
        data = snapshot_data(self.result)
        data['index_snapshot']['bars'][3]['close_price'] += 0.00001
        with self.assertRaises(ValueError):
            validate_snapshot(data)

    def test_reconstructed_review_examples_keep_bearish_low_conviction(self):
        from trading_companion.index_snapshot import validate_snapshot
        fixture = json.loads((Path(__file__).parent / 'fixtures/index_answer_reconstructed.json').read_text(encoding='utf-8'))
        self.assertIn('人工重构', fixture['provenance'])
        result = validate_snapshot(fixture['snapshot'])
        for payload in fixture['answers']:
            answer = validate_answer(payload, result, directory=self.directory)
            self.assertIsNotNone(answer)
            self.assertEqual((answer.direction, answer.conviction, answer.origin), ('偏空', '低', 'model'))
            self.assertGreaterEqual(answer.quality.retained_sentences / answer.quality.generated_sentences, 0.8)
            self.assertEqual(checked_answer(result, answer), answer)

    def test_real_deepseek_capture_replays_without_network_and_keeps_actual_direction(self):
        from trading_companion.index_snapshot import validate_snapshot
        data = json.loads((Path(__file__).parent / 'fixtures/index_answer_live_20261030.json').read_text(encoding='utf-8'))
        self.assertIn('真实采集', data['provenance'])
        self.assertEqual(len(data['source_capture_sha256']), 64)
        result = validate_snapshot(data['snapshot'])
        self.assertEqual((result.request.source, result.authenticity, len(result.bars)), ('tencent', 'provider_reported', 242))
        class Replay:
            def __init__(inner):
                inner.calls = 0
            def complete(inner, messages, *, tools):
                raw = data['responses'][inner.calls]
                inner.calls += 1
                return ToolModelResponse(raw['status'], text=raw['text'])
        model = Replay()
        with patch('requests.get', side_effect=AssertionError('Fixture replay must stay offline')):
            answer, responses = synthesize_index_answer(data['question'], result, model=model)
        self.assertEqual((answer.direction, answer.conviction, answer.origin), ('偏空', '中', 'model'))
        self.assertLessEqual(model.calls, 2)
        self.assertEqual(len(responses), model.calls)
        self.assertGreaterEqual(answer.quality.retained_sentences, 8)
        self.assertEqual(checked_answer(result, answer), answer)
        self.assertIn('情景', [title for title, _, _ in answer.blocks])
        self.assertNotIn('处于低位读数区间。', answer.text(result))


if __name__ == '__main__':
    unittest.main()
