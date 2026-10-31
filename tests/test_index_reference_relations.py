"""Verify both sides of explicit BOLL references without turning scenarios into facts."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import unittest

from trading_companion.api_service import public_turn
from trading_companion.decision_horizon import OPTIONS
from trading_companion.index_answer import _price_relations_allowed, checked_answer, public_index, validate_answer
from trading_companion.index_research import answer_context
from tests.test_index_scenario import live_result


class IndexReferenceRelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent/'fixtures/index_reference_live_20261031.json').read_text(encoding='utf-8'))
        cls.result = live_result(cls.fixture['index_snapshot'])
        cls.context = answer_context(cls.result)

    def test_real_raw_draft_rejects_wrong_weekly_lower_reference_without_losing_direction(self):
        text = self.fixture['response_text']
        self.assertEqual(sha256(text.encode()).hexdigest(), self.fixture['response_text_sha256'])
        raw_snapshot = json.dumps(self.fixture['index_snapshot'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(sha256(raw_snapshot.encode()).hexdigest(), self.fixture['index_snapshot_sha256'])
        raw = json.loads(text)
        bad = '日线BOLL下轨3596.84点与周线BOLL下轨3688.19点为下方观察参考'
        self.assertIn(bad, text)
        answer = validate_answer(raw, self.result)
        self.assertIsNotNone(answer)
        self.assertEqual(answer.direction, raw['direction'])
        self.assertNotIn(bad, answer.text(self.result))
        old = self.fixture['captured_trend_quality']
        self.assertEqual(answer.quality.generated_sentences, old['generated_sentences'])
        self.assertEqual(answer.quality.deleted_sentences, old['deleted_sentences']+1)
        self.assertEqual(checked_answer(self.result, answer), answer)
        self.assertEqual(public_index(self.result, answer)['quality'], answer.quality.public())

    def test_daily_weekly_references_check_each_side_field_and_literal_value(self):
        good = ('日线BOLL下轨3596.84点为下方观察参考。',
                '周线BOLL下轨3688.19点为上方观察参考。',
                '日线BOLL下轨3596.84点为下方观察参考，周线BOLL下轨3688.19点为上方观察参考。')
        bad = ('日线BOLL下轨3596.84点与周线BOLL下轨3688.19点为下方观察参考。',
               '日线BOLL下轨3596.84点与周线BOLL下轨3688.19点为上方观察参考。',
               '周线BOLL下轨3596.84点为下方观察参考。',
               '日线BOLL上轨3596.84点为下方观察参考。',
               '日线BOLL下轨3596.85点为下方观察参考。')
        for text in (*good, *bad):
            with self.subTest(text=text):
                self.assertEqual(_price_relations_allowed(text, self.context), text in good)

    def test_missing_close_or_indicator_and_foreign_evidence_never_authorize_position(self):
        text = '日线BOLL下轨3596.84点为下方观察参考。'
        for remove in ('close_price', 'lower'):
            context = deepcopy(self.context)
            context['evidence'] = [e for e in context['evidence'] if e['field'] != remove]
            self.assertFalse(_price_relations_allowed(text, context))
        context = deepcopy(self.context)
        for record in context['evidence']:
            if record['field'] == 'lower':
                record['instrument_id'] = '000300.SH'
        self.assertFalse(_price_relations_allowed(text, context))

    def test_equal_close_is_not_above_or_below_and_shared_period_checks_all_levels(self):
        context = deepcopy(self.context)
        for record in context['evidence']:
            if record['field'] == 'close_price':
                record['raw_value'] = 3596.84
        self.assertFalse(_price_relations_allowed('日线BOLL下轨3596.84点为下方观察参考。', context))
        self.assertFalse(_price_relations_allowed('日线BOLL下轨3596.84点为上方观察参考。', context))
        self.assertFalse(_price_relations_allowed('日线BOLL下轨3596.84点、中轨3735.83点为下方观察参考。', self.context))

    def test_conditional_sides_remain_hypothetical_and_semicolon_ends_condition(self):
        bad = '周线BOLL下轨3688.19点为下方观察参考。'
        for prefix in ('若收盘回升，', '如果未来位置改变，', '假如后续价格走强，'):
            self.assertTrue(_price_relations_allowed(prefix+bad, self.context))
        self.assertFalse(_price_relations_allowed('若收盘回升，则重新研究；'+bad, self.context))


class IndexHorizonProjectionTests(unittest.TestCase):
    def test_only_real_horizon_reason_gets_shared_fixed_options_and_original_expiry(self):
        from tests.test_index_scenario import ScenarioFlowTests
        flow = ScenarioFlowTests()
        flow.setUp()
        result = flow.send('现在A股能买吗')
        view = public_turn(result)
        self.assertEqual(view['clarification_options'], [dict(value=v, label=l, reply=r) for v,l,r in OPTIONS])
        self.assertEqual(view['clarification_expires_at'], result.index_pending_expires_at)
        self.assertEqual((view['model_calls_attempted'], view['analyses_used']), (0, 0))
        self.assertIsNone(view['parameters'])
        for changes in (dict(code='index_identity_required'), dict(status='failed'), dict(code='plan_excluded')):
            self.assertNotIn('clarification_options', public_turn(replace(result, **changes)))


if __name__ == '__main__':
    unittest.main()
