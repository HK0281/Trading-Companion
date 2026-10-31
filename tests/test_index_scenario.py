"""Independent geometry, purpose isolation, horizon state and archive contracts."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.atr_plan_math import atr_geometry
from trading_companion.decision_demo import DecisionDemoModel
from trading_companion.index_agent import run_index_research_turn
from trading_companion.index_answer import checked_answer, public_index, validate_answer, _price_relations_allowed
from trading_companion.index_contracts import IndexBar, IndexResearchRequest, IndexResearchResult
from trading_companion.index_scenario import candidates, selected_plan, plan_evidence
from trading_companion.index_scenario_answer import validate_scenario_blocks
from trading_companion.index_snapshot import snapshot_data, validate_snapshot
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.research_session import ResearchSession
from trading_companion.snapshot_store import ResearchSnapshotStore
from tests.test_index_research import NOW, REFERENCE_DATE
from trading_companion.development_fixtures import load_development_fixtures


def constant_result(horizon='未来10个交易日', *, start=date(2025, 9, 25), end=date(2026, 9, 25), **kwargs):
    request = IndexResearchRequest('000001.SH', start, end, (IndicatorSpec('atr', window=14),),
        source='csv', request_id='index-hand-calculated', horizon=horizon, scenario_requested=True, **kwargs)
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    bars = tuple(IndexBar(request.instrument_id, day, 100., 101., 99., 100.) for day in days if day.weekday() < 5)
    return IndexResearchResult(request, bars, NOW)


def live_result(raw):
    """Rebuild captured raw inputs without trusting a pre-review derived plan."""
    params = raw['parameters']
    request = IndexResearchRequest(params['instrument_id'], date.fromisoformat(params['start_date']),
        date.fromisoformat(params['as_of_date']), tuple(IndicatorSpec(s['kind'], **{k: v for k, v in s.items() if k != 'kind'})
            for s in params['indicators']), params['source'], raw['request_id'], raw['benchmark_defaulted'], raw['horizon'],
        **{k: raw[k] for k in ('scenario_requested', 'historical_explicit', 'indicators_strict', 'scenario_code')})
    bars = tuple(IndexBar(**{**b, 'trade_date': date.fromisoformat(b['trade_date'])}) for b in raw['bars'])
    return IndexResearchResult(request, bars, datetime.fromisoformat(raw['retrieved_at']))


class ScenarioProseReviewTests(unittest.TestCase):
    def block(self, result, direction, field, text, *, title='情景'):
        record = next(r for r in plan_evidence(selected_plan(result, direction)) if r['field'] == field)
        text = text.replace('VALUE', record['display_value']+'点')
        return dict(title=title, text=text, evidence_ids=[record['id']],
            numbers=[dict(text=record['display_value']+'点', ref=record['id'], field=field)])

    def test_compound_titles_use_first_category_and_keep_prose(self):
        result = constant_result()
        for title, category in [('持有退出情景', '情景'), ('偏空情景参考', '情景'), ('情景参考', '情景'),
                ('观察与情景', '观察重点'), ('关键点位与情景', '关键点位'), ('局限与情景', '局限')]:
            with self.subTest(title=title):
                block = self.block(result, '偏空', 'exit_below', '退出观察位为VALUE。', title=title)
                blocks, quality = validate_scenario_blocks([block], result, '偏空')
                self.assertEqual(blocks, ((category, block['text'], tuple(block['evidence_ids'])),))
                self.assertEqual((quality.generated_sentences, quality.retained_sentences, quality.partial), (1, 1, False))
        for title in ('结论与情景', '技术依据与观察', '未知标题', '情景\n参考', '情景' * 21):
            block = self.block(result, '偏空', 'exit_below', '退出观察位为VALUE。', title=title)
            blocks, quality = validate_scenario_blocks([block], result, '偏空')
            self.assertFalse(blocks, title)
            self.assertTrue(quality.partial, title)

    def test_adjacent_field_direction_words_cannot_change_use(self):
        result = constant_result()
        cases = [('偏空', 'exit_below', '退出观察位', ('跌破', '低于'), ('高于', '站上')),
                 ('偏多', 'reference_stop', '止损参考', ('跌破', '低于'), ('高于', '站上')),
                 ('偏多', 'target_1', '止盈1', ('高于', '站上'), ('跌破', '低于')),
                 ('偏多', 'target_2', '止盈2', ('高于', '站上'), ('跌破', '低于')),
                 ('震荡', 'observe_below', '下方观察位', ('跌破', '低于'), ('高于', '站上')),
                 ('震荡', 'observe_above', '上方观察位', ('高于', '站上'), ('跌破', '低于'))]
        for direction, field, label, good, bad in cases:
            for word in good + bad:
                with self.subTest(field=field, word=word):
                    block = self.block(result, direction, field, label+'为'+word+'VALUE。')
                    blocks, quality = validate_scenario_blocks([block], result, direction)
                    self.assertEqual(bool(blocks), word in good)
                    self.assertEqual(quality.partial, word in bad)
        # A prior valid label cannot authorize the nearest contradictory label,
        # or a number separated from its label by unrelated prose.
        for text in ('止损参考为止盈1为跌破VALUE。', '止盈1为止损参考为高于VALUE。',
                     '止盈1说明指数将涨至VALUE。', '止盈1为，指数将涨至VALUE。'):
            block = self.block(result, '偏多', 'target_1', text)
            self.assertFalse(validate_scenario_blocks([block], result, '偏多')[0], text)

    def test_unavailable_candidate_reason_precedes_unknown_direction(self):
        ready = constant_result()
        short = constant_result('未来120个交易日', start=date(2026, 8, 25))
        excluded = replace(ready, request=replace(ready.request, scenario_code='plan_excluded'))
        for result, code in [(ready, 'index_direction_unavailable'),
                (short, 'plan_long_history_insufficient'), (excluded, 'plan_excluded')]:
            for direction, origin in [('暂不判断', 'model'), ('偏多', 'program')]:
                with self.subTest(code=code, direction=direction, origin=origin):
                    plan = selected_plan(result, direction, origin=origin)
                    self.assertEqual(plan['code'], code)
                    self.assertIsNone(plan['purpose'])
                    self.assertIsNone(plan['values'])
                    self.assertFalse(plan_evidence(plan))
        self.assertIn('365自然日', selected_plan(short, '暂不判断')['reason'])

    def test_seven_real_raw_answers_keep_all_ten_scenario_sentences(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/index_scenario_live_20261030.json').read_text(encoding='utf-8'))
        self.assertEqual(fixture['source_capture_sha256'], 'f9bcae336eeab5ac43ab79f80c3e1f7c6b4082c688517da1c3a5ba02cfbb429c')
        self.assertEqual(len(fixture['samples']), 7)
        generated = retained = original_generated = original_deleted = 0
        with patch('socket.socket.connect', side_effect=AssertionError('Live plan replay must stay offline')):
            for sample in fixture['samples']:
                with self.subTest(question=sample['question']):
                    raw = sample['index_snapshot']
                    digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    self.assertEqual(digest, sample['index_snapshot_sha256'])
                    result = live_result(raw)
                    self.assertEqual(result.to_dict(), raw)
                    self.assertEqual(len(sample['responses']), 1)
                    response = sample['responses'][0]
                    self.assertEqual(sha256(response['text'].encode()).hexdigest(), response['text_sha256'])
                    payload = json.loads(response['text'])
                    answer = validate_answer(payload, result)
                    self.assertIsNotNone(answer)
                    published = checked_answer(result, answer)
                    self.assertEqual(checked_answer(result, published), published)
                    self.assertEqual(published.quality.public(), sample['captured_trend_quality'])
                    self.assertEqual([text for _, text, _ in published.scenario_blocks],
                                     [b['text'] for b in payload.get('scenario_blocks', [])])
                    self.assertFalse(published.scenario_quality.partial)
                    public = public_index(result, published)
                    self.assertEqual(public['scenario_plan']['quality'], published.scenario_quality.public())
                    self.assertNotIn('部分计划说明未通过核对', published.text(result))
                    self.assertEqual(validate_snapshot(snapshot_data(result, direction=answer.direction)), result)
                    if payload['direction'] == '暂不判断':
                        self.assertEqual(public['scenario_plan']['code'], 'plan_long_history_insufficient')
                        self.assertIsNone(public['scenario_plan']['values'])
                    generated += published.scenario_quality.generated_sentences
                    retained += published.scenario_quality.retained_sentences
                    original_generated += sample['captured_plan_quality']['generated_sentences']
                    original_deleted += sample['captured_plan_quality']['deleted_sentences']
        self.assertEqual((original_generated, original_deleted, generated, retained), (10, 9, 10, 10))


class ScenarioModel(DecisionDemoModel):
    def __init__(self, direction='偏多', mutate=None):
        super().__init__()
        self.direction, self.mutate = direction, mutate

    def complete(self, messages, *, tools):
        from trading_companion.tool_model import validate_tool_messages
        validate_tool_messages(messages, tools=tools)
        response = super().complete(messages, tools=tools)
        if not tools:
            data = json.loads(next(m.content.split('\n', 1)[1] for m in messages if m.role == 'assistant'
                                   and m.content.startswith('UNTRUSTED_INDEX_CONTEXT\n')))
            payload = json.loads(response.text)
            payload.update(direction=self.direction, conviction='低')
            payload['blocks'][0]['text'] = f'本轮技术观察{self.direction}，把握低。'
            purpose = {'偏多': 'conditional_entry', '震荡': 'wait', '偏空': 'holding_exit'}.get(self.direction)
            candidate = next((p for p in data.get('scenario_candidates', ()) if p['plan']['purpose'] == purpose
                              and p['plan']['status'] == 'available'), None)
            if candidate:
                records = [r for r in candidate['evidence'] if r['field'] not in ('atr', 'atr_multiple')]
                unit = lambda r: '倍' if r['unit'] == 'ratio' else '点'
                payload['scenario_blocks'] = [dict(title='情景',
                    text=''.join(f"指数{r['label']}为{r['display_value']}{unit(r)}。" for r in records),
                    evidence_ids=[r['id'] for r in records],
                    numbers=[dict(text=r['display_value']+unit(r), ref=r['id'], field=r['field']) for r in records])]
            if self.mutate:
                self.mutate(payload, data)
            return replace(response, text=json.dumps(payload, ensure_ascii=False))
        return response


class GeometryTests(unittest.TestCase):
    def test_independent_hand_table_for_three_profiles(self):
        table = [('未来10个交易日', '97.00', '104.50', '107.50', '1.500000', '2.500000'),
                 ('未来40个交易日', '95.00', '110.00', '115.00', '2.000000', '3.000000'),
                 ('未来120个交易日', '96.00', '108.00', '114.00', '2.000000', '3.500000')]
        for horizon, stop, t1, t2, r1, r2 in table:
            plan = candidates(constant_result(horizon))[0]
            v = plan['values']
            self.assertEqual(plan['status'], 'available')
            self.assertEqual([v[k] for k in ('reference_entry', 'atr', 'reference_stop', 'target_1', 'target_2',
                                            'reward_risk_1', 'reward_risk_2')],
                             ['100.00', '2.0000', stop, t1, t2, r1, r2])
            self.assertEqual(plan['atr_source_date'], '2026-09-18' if '120' in horizon else '2026-09-25')

    def test_rounding_uses_unrounded_atr_and_preserves_entry_precision(self):
        v = atr_geometry('10.4', '.80665', '1.5', ('1.5', '2.5'))
        self.assertEqual([v[k] for k in ('reference_stop', 'target_1', 'target_2', 'atr')], ['9.20', '12.20', '13.40', '0.8067'])
        v = atr_geometry('100.123', '2', '1.5', ('1.5', '2.5'))
        self.assertEqual(v['reference_entry'], '100.123')
        self.assertEqual((v['reference_stop'], v['target_1'], v['reward_risk_1']), ('97.13', '104.61', '1.499164'))
        from trading_companion.index_scenario import plan_text
        plan = selected_plan(constant_result(), '偏多')
        plan['values']['reward_risk_1'] = '1.005000'
        self.assertIn('实际盈亏比1：1.01倍', plan_text(plan))
        for atr in ('0', 'NaN', '1000'):
            with self.assertRaises((ValueError, ArithmeticError)):
                atr_geometry('100', atr, '1.5', ('1.5', '2.5'))

    def test_long_gates_no_daily_fallback(self):
        short = constant_result('未来120个交易日', start=date(2026, 8, 1))
        self.assertEqual(candidates(short)[0]['code'], 'plan_long_history_insufficient')
        sparse = replace(constant_result('未来120个交易日'), bars=constant_result().bars[-80:])
        self.assertEqual(candidates(sparse)[0]['code'], 'plan_long_history_insufficient')
        missing = replace(constant_result(), request=replace(constant_result().request, indicators=(IndicatorSpec('sma', window=3),)))
        self.assertEqual(candidates(missing)[0]['code'], 'risk_atr_not_requested')

    def test_identity_full_precision_ohlc_horizon_and_method_bind_plan(self):
        result = constant_result('未来120个交易日')
        first = candidates(result)[0]
        bar = replace(result.bars[25], open_price=100.000001)
        changed = replace(result, bars=(*result.bars[:25], bar, *result.bars[26:]))
        second = candidates(changed)[0]
        self.assertEqual(first['values'], second['values'])
        self.assertNotEqual(first['plan_id'], second['plan_id'])
        self.assertNotEqual(first['plan_id'], candidates(replace(result, request=replace(result.request, horizon='未来6个月')))[0]['plan_id'])

    def test_missing_close_relationship_does_not_raise_or_accept_relation(self):
        self.assertFalse(_price_relations_allowed('价格在日线中轨上方。', {'evidence': []}))
        self.assertTrue(_price_relations_allowed('本轮仅作技术观察。', {'evidence': []}))

    def test_stock_golden_full_payloads_from_parent_commit(self):
        from tests.test_trade_plan import market_result
        from tests.test_decision_horizon import HorizonPriceTests
        from trading_companion.trade_plan import build_trade_plans, HORIZON_METHOD
        expected = json.loads((Path(__file__).parent/'fixtures/stock_horizon_plans_shared_math.json').read_text(encoding='utf-8'))
        daily = market_result()
        daily = replace(daily, request=replace(daily.request, request_id='stock-math-daily'))
        actual = {'legacy': [p.to_dict() for p in build_trade_plans(daily, expected_request=daily.request)]}
        for name, horizon in [('short', '未来10个交易日'), ('medium', '未来40个交易日'), ('explicit', '未来4个月')]:
            actual[name] = [p.to_dict() for p in build_trade_plans(daily, expected_request=daily.request, method=HORIZON_METHOD, horizon=horizon)]
        weekly = HorizonPriceTests().long_bundle().companies[0].technical
        weekly = replace(weekly, request=replace(weekly.request, request_id='stock-math-weekly'))
        actual['long'] = [p.to_dict() for p in build_trade_plans(weekly, expected_request=weekly.request,
                                                              method=HORIZON_METHOD, horizon='未来120个交易日')]
        self.assertEqual(actual, expected)


class ScenarioFlowTests(unittest.TestCase):
    def setUp(self):
        self.source, self.directory = load_development_fixtures()
        self.session = ResearchSession()

    def send(self, question, model=None, **kwargs):
        return run_index_research_turn(question, model=model or ScenarioModel(), directory=self.directory,
            session=self.session, source='tencent', development_source=self.source,
            now=kwargs.pop('now', NOW), reference_date=REFERENCE_DATE, **kwargs)

    def test_horizon_gate_zero_calls_preserves_original_date_and_scope(self):
        first = self.send('上证指数最近一个月止盈止损')
        self.assertEqual((first.code, first.model_calls_attempted, first.analyses_used), ('index_horizon_required', 0, 0))
        self.assertEqual(self.session.to_dict()['pending']['kind'], 'index_horizon')
        second = self.send('中线')
        self.assertEqual(second.model_calls_attempted, 2)
        self.assertEqual(second.index_result.request.horizon, '未来40个交易日')
        self.assertTrue(second.index_result.request.historical_explicit)
        self.assertLess((second.index_result.request.as_of_date-second.index_result.request.start_date).days, 40)

    def test_purpose_by_direction_and_no_plan_for_unknown(self):
        for direction, purpose, fields in [('偏多', 'conditional_entry', {'reference_entry', 'reference_stop', 'target_1', 'target_2'}),
                ('震荡', 'wait', {'observe_below', 'observe_above'}), ('偏空', 'holding_exit', {'reference_close', 'exit_below'})]:
            self.session.clear()
            outcome = self.send('上证指数中线止盈止损', ScenarioModel(direction))
            p = outcome.index_research['scenario_plan']
            self.assertEqual(p['purpose'], purpose)
            self.assertTrue(fields <= p['values'].keys())
            self.assertEqual(p['quality']['deleted_sentences'], 0, outcome.display_answer)
            self.assertTrue(p['blocks'])
            self.assertEqual(outcome.model_calls_attempted, 2)
            self.assertEqual(checked_answer(outcome.index_result, outcome.index_answer), outcome.index_answer)
            if direction == '偏空':
                self.assertNotIn('target_1', json.dumps(p))
                self.assertIn('假如已持有跟踪该指数的基金', outcome.display_answer)
            self.assertEqual(self.send('查看上次结果').index_research['scenario_plan'], p)
        self.session.clear()
        unknown = self.send('上证指数中线止盈止损', ScenarioModel('暂不判断'))
        self.assertEqual(unknown.index_research['scenario_plan']['code'], 'index_direction_unavailable')
        self.assertIsNone(unknown.index_research['scenario_plan']['values'])

    def test_bearish_target_and_wrong_ref_unit_value_are_rejected(self):
        def inject(payload, context):
            record = context['scenario_candidates'][0]['evidence'][2]
            payload['scenario_blocks'] = [dict(title='情景', text=f"指数目标1为{record['display_value']}点。",
                evidence_ids=[record['id']], numbers=[dict(text=record['display_value']+'点', ref=record['id'])])]
        outcome = self.send('上证指数中线止盈止损', ScenarioModel('偏空', inject))
        self.assertEqual(outcome.index_research['scenario_plan']['blocks'], [])
        self.assertEqual(outcome.index_research['scenario_plan']['quality']['deleted_sentences'], 1)
        self.assertNotIn('目标1', outcome.display_answer)
        self.assertEqual(outcome.index_research['direction'], '偏空')
        for kind in ('wrong_value', 'fact_ref', 'unit', 'horizon', 'unlabelled'):
            def mutate(payload, context, kind=kind):
                b = payload['scenario_blocks'][0]
                if kind == 'wrong_value':
                    b['text'] = '指数入场参考为999999点。'
                elif kind == 'fact_ref':
                    b['numbers'][0]['ref'] = 'E1'
                elif kind == 'unit':
                    b['text'] = b['text'].replace('点', '元')
                elif kind == 'unlabelled':
                    record = context['scenario_candidates'][0]['evidence'][2]
                    b.update(text='指数将涨至'+record['display_value']+'。', numbers=[], evidence_ids=[record['id']])
                else:
                    b['text'] = '未来10个交易日，'+b['text']
            self.session.clear()
            invalid = self.send('上证指数中线止盈止损', ScenarioModel('偏多', mutate))
            self.assertTrue(invalid.index_research['scenario_plan']['quality']['partial'], kind)

    def test_identity_and_horizon_share_expiry_and_additional_conditions_not_discarded(self):
        self.assertEqual(self.send('科创板指数止盈止损').code, 'index_identity_required')
        middle = self.send('科创50', now=NOW+timedelta(minutes=14))
        self.assertEqual(middle.code, 'index_horizon_required')
        self.assertEqual(self.session.index_pending.created_at, NOW)
        refused = self.send('中线，改成近20日', now=NOW+timedelta(minutes=14))
        self.assertEqual(refused.model_calls_attempted, 0)
        self.assertIsNotNone(self.session.index_pending)
        expired = self.send('中线', now=NOW+timedelta(minutes=15))
        self.assertEqual(expired.code, 'index_clarification_expired')

    def test_strict_scope_no_dependency_or_horizon_and_pure_trend_no_plan(self):
        for question in ('上证指数止盈止损，只看SMA3', '上证指数止盈止损，不要ATR'):
            self.session.clear()
            outcome = self.send(question)
            self.assertNotEqual(outcome.code, 'index_horizon_required')
            self.assertFalse(any(s.kind == 'atr' for s in outcome.index_result.request.indicators))
            self.assertEqual(outcome.index_research['scenario_plan']['code'], 'risk_atr_not_requested')
        self.session.clear()
        pure = self.send('上证指数走势')
        self.assertNotIn('scenario_plan', pure.index_research)
        self.assertEqual(self.send('那止盈止损呢').code, 'index_horizon_required')
        resumed = self.send('短线')
        self.assertEqual(resumed.analyses_used, 0)
        self.assertEqual(resumed.model_calls_attempted, 1)

    def test_explicit_months_and_long_default_and_short_history(self):
        explicit = self.send('上证指数未来4个月止盈止损')
        self.assertEqual(explicit.index_research['scenario_plan']['horizon'], '未来4个月')
        self.assertEqual(explicit.index_research['scenario_plan']['horizon_profile'], 'medium')
        self.session.clear()
        long = self.send('上证指数长线止盈止损')
        r = long.index_result.request
        self.assertEqual((r.as_of_date-r.start_date).days+1, 366)
        self.assertEqual(long.index_research['scenario_plan']['atr_timeframe'], 'weekly')
        self.session.clear()
        short = self.send('上证指数最近一个月长线止盈止损')
        self.assertEqual(short.index_research['scenario_plan']['code'], 'plan_long_history_insufficient')

    def test_snapshot_v1_v2_recompute_tampering_and_no_unselected_targets(self):
        result = constant_result()
        data = snapshot_data(result, direction='偏空')
        self.assertEqual(data['version'], 'index-evidence-snapshot-v2')
        self.assertEqual(validate_snapshot(data), result)
        self.assertNotIn('target_1', json.dumps(data))
        for field, value in [('horizon', '未来4个月'), ('indicators_strict', True)]:
            bad = deepcopy(data)
            bad['index_snapshot'][field] = value
            with self.assertRaises(ValueError):
                validate_snapshot(bad)
        old = replace(result, request=replace(result.request, scenario_requested=False))
        self.assertEqual(snapshot_data(old)['version'], 'index-evidence-snapshot-v1')
        self.assertEqual(validate_snapshot(snapshot_data(old)), old)
        with ResearchSnapshotStore(':memory:', clock=lambda: NOW) as store:
            identifier = store.save_index(result, direction='偏空')
            self.assertEqual(store.load(identifier)['scenario_plan'], data['scenario_plan'])
            self.assertEqual(store.list()[0]['instrument_kind'], 'index')

    def test_http_session_gate_replay_and_memory_profile(self):
        from trading_companion.api import create_demo_service, create_app
        from fastapi.testclient import TestClient
        from trading_companion.conversation_memory import digest_turn
        service = create_demo_service()
        with TestClient(create_app(service=service), base_url='http://localhost') as client:
            credentials = client.post('/v1/sessions', json={}).json()
            path = '/v1/sessions/'+credentials['session_id']+'/messages'
            headers = {'Authorization': 'Bearer '+credentials['access_token']}
            first = client.post(path, headers=headers, json=dict(request_id='a'*32, question='现在A股能买吗')).json()['result']
            self.assertEqual(first['code'], 'index_horizon_required')
            self.assertEqual(first['model_calls_attempted'], 0)
            from trading_companion.decision_horizon import OPTIONS
            self.assertEqual(first['clarification_options'], [dict(value=v, label=l, reply=r) for v,l,r in OPTIONS])
            replay = client.post(path, headers=headers, json=dict(request_id='a'*32, question='现在A股能买吗')).json()
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['result'], first)
            body = dict(request_id='b'*32, question='中线')
            second = client.post(path, headers=headers, json=body).json()['result']
            self.assertEqual(second['index_research']['scenario_plan']['purpose'], 'wait')
            self.assertEqual(client.post(path, headers=headers, json=body).json()['result'], second)
            digest = digest_turn('中线', second).render()
            self.assertIn('中线', digest)
            self.assertNotIn(second['index_research']['scenario_plan']['values']['observe_above'], digest)
        service.close()

    def test_formal_session_with_history_validates_original_message_budget(self):
        from trading_companion.api_service import ResearchApiService
        from trading_companion.conversation_memory import HEADER
        captured = []
        class Observed(ScenarioModel):
            def complete(inner, messages, *, tools):
                captured.append(messages)
                return super().complete(messages, tools=tools)
        service = ResearchApiService(model_factory=Observed, directory_loader=lambda _: self.directory,
            data_mode='synthetic', development_source=self.source, reference_date=REFERENCE_DATE,
            conversation_context=True)
        try:
            s = service.create_session()
            service.message(s['session_id'], s['access_token'], 'first', '上证指数走势')
            question = '上证指数中线止盈止损，请解释理由'
            result = service.message(s['session_id'], s['access_token'], 'second', question)['result']
            self.assertEqual(result['index_research']['origin'], 'model')
            self.assertEqual(result['model_calls_attempted'], 2)
            self.assertEqual(result['index_research']['scenario_plan']['status'], 'available')
            for messages in captured:
                self.assertEqual(sum(m.role == 'system' for m in messages), 1)
            self.assertTrue(any(HEADER in (m[0].content or '') for m in captured[2:]))
        finally:
            service.close()

    def test_pending_configuration_change_clears_even_when_directory_is_unusable(self):
        self.send('上证指数止盈止损')
        outcome = run_index_research_turn('中线', model=ScenarioModel(), directory=self.directory,
            session=self.session, source='tencent', now=NOW, reference_date=REFERENCE_DATE)
        self.assertEqual(outcome.code, 'index_clarification_expired')
        self.assertIsNone(self.session.index_pending)
        self.assertEqual(outcome.model_calls_attempted, 0)

    def test_cancelled_plan_and_failed_conclusion_never_publish_candidates(self):
        from trading_companion.turn_control import TurnControl
        self.send('上证指数走势')
        before = self.session.index_saved
        control = TurnControl()
        class CancelAfterAnswer(ScenarioModel):
            def complete(inner, messages, *, tools):
                response = super().complete(messages, tools=tools)
                if not tools:
                    control.cancel()
                return response
        cancelled = self.send('深证成指中线止盈止损', CancelAfterAnswer(), control=control)
        self.assertEqual(cancelled.code, 'turn_cancelled')
        self.assertEqual(self.session.index_saved, before)
        self.assertFalse(self.session._subject_current)
        def invalid(payload, context):
            payload['blocks'][0]['text'] = '本轮收盘999999点。'
        self.session.clear()
        failed = self.send('上证指数中线止盈止损', ScenarioModel('偏多', invalid))
        self.assertEqual(failed.model_calls_attempted, 3)
        self.assertEqual(failed.index_research['scenario_plan']['code'], 'index_direction_unavailable')
        self.assertIsNone(failed.index_research['scenario_plan']['values'])

    def test_pure_strict_scope_is_inherited_and_new_horizon_does_not_renew_snapshot(self):
        self.send('上证指数只看SMA3')
        scoped = self.send('那止盈止损呢')
        self.assertEqual(scoped.index_research['scenario_plan']['code'], 'risk_atr_not_requested')
        self.session.clear()
        self.send('上证指数短线止盈止损')
        original = self.session.index_saved.saved_at
        revised = self.send('那未来4个月呢', now=NOW+timedelta(minutes=10))
        self.assertEqual(revised.index_research['scenario_plan']['horizon'], '未来4个月')
        self.assertEqual(self.session.index_saved.saved_at, original)
        self.assertEqual(revised.analyses_used, 0)
        self.assertEqual(self.send('为什么', now=NOW+timedelta(minutes=15)).code, 'index_current_unavailable')


if __name__ == '__main__':
    unittest.main()
