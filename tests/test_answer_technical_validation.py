"""Indicator configuration and RSI predicates cannot borrow unrelated matching numbers."""
from copy import deepcopy
import unittest

from tests.test_answer_validation import context
from trading_companion.answer_validation import check_claim


def technical_context(value=42, *, window=14, timeframe='daily'):
    ctx = context()
    spec = f'rsi(window={window})'
    ctx['evidence'].append(dict(id='E6', instrument_id='000001.SZ', label='RSI',
        raw_value=value, unit='index_points', date='2026-09-25', content_kind='fact'))
    ctx['_technical_validation']['000001.SZ'] = dict(
        confirmed=[spec, 'sma(window=3)', 'macd(fast=12,slow=26,signal=9)'],
        values=[dict(indicator_id=spec, timeframe=timeframe, field='value', value=str(value), date='2026-09-25', ref='E6')])
    return ctx


class TechnicalValidationTests(unittest.TestCase):
    def check(self, text, ctx=None):
        return check_claim(text, instrument='000001.SZ', context=ctx or technical_context(), section='current', index=0)

    def test_confirmed_parameters_are_excluded_but_unconfirmed_are_rejected(self):
        for text in ('RSI(14)低于50中线。', 'MACD(12,26,9)尚需观察。',
                     'SMA3尚需观察。', 'SMA(3)尚需观察。', 'sma(window=3)尚需观察。',
                     '3日均线尚需观察。', '14日RSI低于50中线。'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text)[0], text)
        # 29.7 / 30 / 99 can match price evidence; they still cannot be indicator parameters.
        for text in ('RSI6尚需观察。', 'RSI(29.7)尚需观察。', 'SMA30尚需观察。',
                     'MACD(12,26,8)尚需观察。', 'MACD(12,26)尚需观察。', 'RSI(14,6)尚需观察。',
                     'RSI(14尚需观察。', '周线SMA(3)尚需观察。', 'SMA3元。',
                     'SMA(3)29.7元。', 'SMA3(29.7)。', 'SMA(3)(29.7)。',
                     'SMA3(29.7元。', '周线方面RSI(14)低于50中线。',
                     '6日RSI低于50中线。', '30日均线提供支撑。', '3周SMA尚需观察。',
                     '-3日SMA尚需观察。', '3.0日SMA尚需观察。'):
            with self.subTest(text=text):
                self.assertFalse(self.check(text)[0])

    def test_direction_range_and_extremes_check_each_latest_predicate(self):
        for value, text, passed in (
            (45, 'RSI低于30。', False), (42, 'RSI低于50中线。', True),
            (42, 'RSI低于50中轴。', True), (42, 'RSI低于50分界。', True),
            (42, 'RSI仍处30至70区间。', True), (75, 'RSI仍处30至70区间。', False),
            (42, 'RSI低于50中线，同时仍在30至70之间。', True),
            (75, 'RSI低于50中线，同时仍在30至70之间。', False),
            (25, 'RSI超卖（低于30）。', True), (45, 'RSI超卖（低于30）。', False),
            (75, 'RSI超买（高于70）。', True), (70, 'RSI超买（高于70）。', False),
            (50, 'RSI低于50中线。', False), (30, 'RSI低于30。', False),
            (30, 'RSI在30至70区间。', True), (70, 'RSI在30至70区间。', True),
            (42, 'RSI在70至30区间。', False), (42, '若RSI低于50则观察。', False),
            (42, 'RSI不低于50。', False), (42, 'RSI在30至70区间外。', False),
            (42, 'RSI在30至70区间之外。', False), (42, 'RSI向30靠拢。', False)):
            with self.subTest(value=value, text=text):
                self.assertEqual(bool(self.check(text, technical_context(value))[0]), passed)

    def test_relation_requires_supplied_same_stock_same_period_latest_rsi(self):
        base = technical_context()
        variants = []
        absent = deepcopy(base); absent['_technical_validation'] = {}; variants.append(absent)
        omitted = deepcopy(base); omitted['evidence'].pop(); variants.append(omitted)
        foreign = deepcopy(base); foreign['evidence'][-1]['instrument_id'] = '600000.SH'; variants.append(foreign)
        old = deepcopy(base); old['evidence'][-1]['date'] = '2026-09-24'; variants.append(old)
        wrong = deepcopy(base); wrong['evidence'][-1]['raw_value'] = 30; variants.append(wrong)
        week = technical_context(timeframe='weekly'); variants.append(week)
        for ctx in variants:
            with self.subTest(ctx=ctx):
                self.assertFalse(self.check('日线RSI(14)低于50中线。', ctx)[0])
        self.assertFalse(self.check('RSI6低于50中线。', base)[0])
        self.assertTrue(self.check('周线RSI(14)低于50中轴。', week)[0])
        self.assertFalse(self.check('RSI(14)低于50中线。', technical_context(window=6))[0])
        ambiguous = deepcopy(base)
        ambiguous['_technical_validation']['000001.SZ']['values'].append({
            **ambiguous['_technical_validation']['000001.SZ']['values'][0], 'indicator_id': 'rsi(window=6)'})
        self.assertFalse(self.check('RSI低于50中线。', ambiguous)[0])
        both_periods = deepcopy(base)
        both_periods['_technical_validation']['000001.SZ']['values'].append({
            **both_periods['_technical_validation']['000001.SZ']['values'][0], 'timeframe': 'weekly'})
        self.assertFalse(self.check('RSI低于50中线。', both_periods)[0])
        self.assertTrue(self.check('日线RSI低于50中线。', both_periods)[0])

    def test_real_numeric_values_and_other_thresholds_keep_normal_checks(self):
        self.assertTrue(self.check('RSI值为42。')[0])
        self.assertFalse(self.check('RSI值为99。')[0])
        self.assertFalse(self.check('RSI低于50中线，收盘999元。')[0])
        self.assertFalse(self.check('ADX低于50中线。')[0])
        self.assertFalse(self.check('RSI低于60中线。')[0])
        self.assertIn('E6', self.check('RSI低于50中线。')[1])
        coincidence = technical_context()
        coincidence['evidence'][0]['raw_value'] = 30
        self.assertFalse(self.check('RSI低于30。', coincidence)[0])
        self.assertFalse(self.check('RSI使用30/70参考阈值。', coincidence)[0])

    def test_unit_bearing_parentheses_are_readings_not_indicator_parameters(self):
        ctx = technical_context()
        ctx['evidence'].append(dict(id='E7', instrument_id='000001.SZ', label='ATR参考计划',
            content_kind='trade_plan', plan_id='plan-1', values={'atr_multiple': '1.5'}))
        text = '按ATR（1.5倍）规则计算条件止损位。'
        self.assertEqual(self.check(text, ctx)[0], text)
        self.assertIn('E7', self.check(text, ctx)[1])
        self.assertFalse(self.check('按ATR（9倍）规则计算条件止损位。', ctx)[0])
        ctx['evidence'].pop()
        # Matching a price number is insufficient for a plan multiplier.
        ctx['evidence'][0]['raw_value'] = 1.5
        self.assertFalse(self.check(text, ctx)[0])

    def test_cross_timeframe_alignment_checks_direction_and_keeps_weekly_rsi_separate(self):
        ctx = technical_context()
        sidecar = ctx['_technical_validation']['000001.SZ']
        sidecar['confirmed'].append('ema(window=20)')
        ctx['evidence'].append(dict(id='E7', instrument_id='000001.SZ', label='日周方向核对',
            raw_value='aligned_below', date='2026-09-25', content_kind='relation'))
        sidecar['values'].append(dict(indicator_id='ema(window=20)', timeframe='cross_timeframe',
            field='alignment', value='aligned_below', date='2026-09-25', ref='E7'))
        text = '日线与最近完整周线均在EMA20下方，方向一致。'
        self.assertEqual(self.check(text, ctx)[0], text)
        self.assertIn('E7', self.check(text, ctx)[1])
        for wrong in ('日线与最近完整周线均在EMA20上方，方向一致。',
                      '若日线与最近完整周线均在EMA20下方，需观察。',
                      '日线与最近完整周线均在EMA20下方之外。',
                      '周线EMA20需观察。', '周线RSI(14)低于50中线。'):
            with self.subTest(text=wrong):
                self.assertFalse(self.check(wrong, ctx)[0])
