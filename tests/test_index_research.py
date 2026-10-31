"""Offline product and numeric contracts for index research; no live-quality claim."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch, Mock

from trading_companion.agent import run_natural_research_turn
from trading_companion.api import create_demo_service
from trading_companion.api_service import public_turn
from trading_companion.chart_intervals import bind_chart, capabilities
from trading_companion.decision_demo import DecisionDemoModel
from trading_companion.development_fixtures import load_development_fixtures, REFERENCE_DATE
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.index_agent import run_index_research_turn
from trading_companion.index_answer import validate_answer, answer_context
from trading_companion.index_chart import weekly_chart
from trading_companion.index_contracts import IndexBar, IndexResearchRequest, IndexResearchResult
from trading_companion.index_research import index_evidence, index_weeks
from trading_companion.index_source import parse_daily, synthetic_index_bars
from trading_companion.market_scope import INDICES, market_selection
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_session import ResearchSession
from trading_companion.tool_model import ToolCall, ToolModelResponse

NOW = datetime(2026, 9, 26, 4, tzinfo=timezone.utc)


class IndexResearchTests(unittest.TestCase):
    def setUp(self):
        self.source, self.directory = load_development_fixtures()
        self.session = ResearchSession()

    def run_index(self, question, *, session=None, model=None, **kwargs):
        return run_index_research_turn(question, model=model or DecisionDemoModel(), directory=self.directory,
            session=self.session if session is None else session, source='tencent', development_source=self.source,
            now=kwargs.pop('now', NOW), reference_date=REFERENCE_DATE, **kwargs)

    def test_five_indices_and_point_units(self):
        for index in INDICES:
            with self.subTest(index=index.name):
                outcome = self.run_index(index.name + '走势', session=ResearchSession())
                self.assertEqual(outcome.status, 'completed', outcome.message)
                self.assertEqual(outcome.model_calls_attempted, 2)
                self.assertEqual(outcome.analyses_used, 1)
                view = public_turn(outcome)
                self.assertEqual(view['index_research']['instrument_id'], index.instrument_id)
                self.assertEqual(view['chart']['version'], 'index-chart-v1')
                self.assertTrue(all(b['volume'] is None for b in view['chart']['bars']))
                self.assertTrue(any(s['unit'] == 'index_point' for s in view['chart']['series']))
                binding = bind_chart(view, 'synthetic')
                week = weekly_chart(binding)
                self.assertEqual(week['version'], 'index-chart-interval-v1')
                self.assertFalse(capabilities(binding, 'synthetic')['intervals']['60m']['enabled'])
                self.assertNotIn('元/股', outcome.display_answer)

    def test_default_benchmark_and_missing_recognition(self):
        for question in ('分析一下大盘指数的走势', '现在A股能买吗', 'A股市场走势'):
            outcome = self.run_index(question)
            self.assertEqual(outcome.index_result.request.instrument_id, '000001.SH')
            self.assertTrue(outcome.display_answer.startswith('本次以上证指数作为大盘观察基准。'))
        self.assertEqual(market_selection('科创板指数走势', directory=self.directory)[2], 'index_identity_required')
        self.assertEqual(self.run_index('科创板指数走势').model_calls_attempted, 0)
        resumed = self.run_index('科创50')
        self.assertEqual(resumed.index_result.request.instrument_id, '000688.SH')

    def test_vague_current_periods_use_default_and_quantified_periods_are_preserved(self):
        from trading_companion.index_agent import INTENT_NAME
        class DateModel(DecisionDemoModel):
            def __init__(inner, expression):
                inner.expression = expression
            def complete(inner, messages, *, tools):
                if tools and tools[0].name == INTENT_NAME:
                    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('date', INTENT_NAME,
                        dict(date_expression=inner.expression)),))
                return super().complete(messages, tools=tools)
        for q in ('创业板最近怎么样', '大盘最近走势', '上证指数近期如何', '目前上证指数走势', '现在上证指数如何'):
            for expression in (None, 'null', '', ' 最近 ', '近期', '目前', '现在'):
                with self.subTest(question=q, expression=expression):
                    result = self.run_index(q, model=DateModel(expression), session=ResearchSession())
                    self.assertEqual(result.status, 'completed', result.message)
                    self.assertEqual(result.model_calls_attempted, 2)
                    self.assertEqual((result.index_result.request.start_date, result.index_result.request.as_of_date),
                        (date(2025, 9, 26), date(2026, 9, 25)))
        for q, expression, days in [('最近一个月沪深300', '最近一个月', 30),
                                    ('科创50最近一个月走势', None, 30), ('大盘最近20日走势', '最近', 20),
                                    ('上证指数近20日走势', None, 20)]:
            result = self.run_index(q, model=DateModel(expression), session=ResearchSession())
            self.assertEqual(result.status, 'completed', result.message)
            self.assertEqual((result.index_result.request.as_of_date-result.index_result.request.start_date).days+1, days)
        self.run_index('上证指数走势')
        updated = self.run_index('那最近一个月呢', model=DateModel(None))
        self.assertEqual((updated.index_result.request.as_of_date-updated.index_result.request.start_date).days+1, 30)

    def test_date_normalization_cannot_authorize_invented_or_unsupported_dates(self):
        from trading_companion.index_agent import INTENT_NAME
        class InventedDate(DecisionDemoModel):
            def complete(inner, messages, *, tools):
                if tools and tools[0].name == INTENT_NAME:
                    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('date', INTENT_NAME,
                        dict(date_expression='2026-09-01至2026-09-25')),))
                return super().complete(messages, tools=tools)
        result = self.run_index('创业板最近怎么样', model=InventedDate())
        self.assertEqual(result.code, 'index_intent_ungrounded')
        self.assertEqual(result.analyses_used, 0)
        for q in ('创业板最近20个交易日怎么样', '大盘最近半个月走势', '上证指数近期如何，截至2026-09-26'):
            result = self.run_index(q, session=ResearchSession())
            self.assertIsNone(result.index_result, q)
            self.assertEqual(result.analyses_used, 0, q)

    def test_stock_and_fund_identity_are_not_stolen(self):
        for text in ('000001', '000001.SZ', '沪深300ETF走势', '相对强弱指数怎么计算', '创业板上市规则'):
            self.assertIsNone(market_selection(text, directory=self.directory)[2])
            self.assertIsNone(market_selection(text, directory=self.directory)[0])
        with self.assertRaises(InvalidRequestError):
            MultiIndicatorRequest('000001.SH', date(2026, 1, 1), date(2026, 9, 25))

    def test_followup_uses_same_snapshot_and_no_read(self):
        first = self.run_index('上证指数走势')
        with patch('trading_companion.index_agent.synthetic_index_bars', side_effect=AssertionError('read')):
            followup = self.run_index('那周线呢')
        self.assertEqual(followup.index_result, first.index_result)
        self.assertEqual(followup.model_calls_attempted, 1)
        self.assertEqual(followup.analyses_used, 0)
        self.assertEqual(followup.index_research['context'], 'saved')

    def test_parameter_edit_reuses_whole_reply_parser(self):
        first = self.run_index('上证指数只看SMA3')
        self.assertEqual(first.index_result.request.indicators, (IndicatorSpec('sma', window=3),))
        edited = self.run_index('把SMA3改成5日，其他不变')
        self.assertEqual(edited.index_result.request.indicators, (IndicatorSpec('sma', window=5),))
        self.assertNotEqual(edited.index_result.request.request_id, first.index_result.request.request_id)
        self.assertEqual(edited.model_calls_attempted, 1)

    def test_plan_request_is_explicitly_deferred(self):
        result = self.run_index('上证指数中线走势和止盈止损计划')
        self.assertIn('点位计划下一批提供', result.display_answer)
        self.assertIsNone(result.index_research.get('scenario_plan'))
        self.assertIn('点位计划下一批提供', self.run_index('查看上次结果').display_answer)

    def test_followup_explicit_horizon_is_kept_separate_from_history_window(self):
        original = self.run_index('上证指数走势').index_result.request
        updated = self.run_index('那未来4个月呢')
        self.assertEqual(updated.index_result.request.horizon, '未来4个月')
        self.assertEqual(updated.index_result.request.start_date, original.start_date)
        self.assertEqual(updated.index_result.request.as_of_date, original.as_of_date)
        self.assertIsNone(updated.index_research.get('scenario_plan'))

    def test_unknown_quantity_keeps_result_limited(self):
        outcome = self.run_index('上证指数只看成交量')
        self.assertEqual(outcome.status, 'limited')
        self.assertTrue(any('成交量单位未核实' in s for s in outcome.index_research['limitations']))

    def test_source_unsupported_never_switches(self):
        result = run_index_research_turn('上证指数走势', model=DecisionDemoModel(), directory=replace(self.directory, is_fixture=False),
            source='akshare_eastmoney', reference_date=REFERENCE_DATE, now=NOW, session=self.session)
        self.assertEqual(result.code, 'index_source_unsupported')
        self.assertEqual(result.model_calls_attempted, 0)

    def test_pending_does_not_extend_and_expires_at_boundary(self):
        self.run_index('科创板指数走势')
        original = self.session.index_pending
        self.run_index('科创板指数走势', now=NOW + timedelta(minutes=10))
        self.assertEqual(self.session.index_pending.created_at, original.created_at)
        expired = self.run_index('科创50', now=NOW + timedelta(minutes=15))
        self.assertEqual(expired.code, 'index_clarification_expired')
        self.assertIsNone(self.session.index_pending)
        self.assertEqual(expired.model_calls_attempted, 0)

    def test_failed_new_interpretation_invalidates_implicit_subject(self):
        self.run_index('上证指数走势')
        failed = self.run_index('深证成指走势', model=Mock(complete=Mock(return_value=ToolModelResponse('complete', text='{}'))))
        self.assertEqual(failed.code, 'index_intent_invalid')
        self.assertFalse(self.session._subject_current)
        self.assertEqual(self.run_index('为什么').code, 'index_current_unavailable')
        self.assertEqual(self.run_index('查看上次结果').index_result.request.instrument_id, '000001.SH')

    def test_source_and_mode_change_and_saved_ttl(self):
        self.run_index('上证指数走势')
        expired = self.run_index('为什么', now=NOW + timedelta(minutes=15))
        self.assertEqual(expired.code, 'index_current_unavailable')
        self.assertIsNone(self.session.index_saved)
        self.run_index('上证指数走势')
        changed = run_index_research_turn('为什么', model=DecisionDemoModel(), session=self.session,
            directory=self.directory, source='tencent', now=NOW, reference_date=REFERENCE_DATE)
        self.assertEqual(changed.code, 'directory_unusable')
        self.assertIsNone(self.session.index_saved)
        self.run_index('上证指数走势')
        changed = run_index_research_turn('为什么', model=DecisionDemoModel(), session=self.session,
            directory=replace(self.directory, is_fixture=False), source='akshare_eastmoney', now=NOW, reference_date=REFERENCE_DATE)
        self.assertEqual(changed.code, 'index_current_unavailable')
        self.assertIsNone(self.session.index_saved)

    def test_rejected_narratives_get_one_repair_then_program_facts(self):
        class BadNarrative(DecisionDemoModel):
            def complete(inner, messages, *, tools):
                response = super().complete(messages, tools=tools)
                if not tools:
                    payload = json.loads(response.text)
                    payload['blocks'][0]['text'] = '本轮收盘999999点，方向偏空。'
                    return replace(response, text=json.dumps(payload))
                return response
        result = self.run_index('上证指数走势', model=BadNarrative())
        self.assertEqual(result.model_calls_attempted, 3)
        self.assertEqual(result.status, 'limited')
        self.assertEqual(result.index_research['origin'], 'program')
        self.assertEqual(result.index_research['direction'], '暂不判断')
        self.assertNotIn('999999', result.display_answer)

    def test_partial_narrative_keeps_direction_status_and_saved_quality(self):
        class PartialNarrative(DecisionDemoModel):
            def complete(inner, messages, *, tools):
                response = super().complete(messages, tools=tools)
                if not tools:
                    payload = json.loads(response.text)
                    payload.update(direction='偏空', conviction='低')
                    payload['blocks'][0]['text'] = '本轮技术观察偏空，把握低。'
                    payload['blocks'][1]['text'] += '收盘999999点。'
                    return replace(response, text=json.dumps(payload, ensure_ascii=False))
                return response
        result = self.run_index('上证指数走势', model=PartialNarrative())
        self.assertEqual(result.model_calls_attempted, 2)
        self.assertEqual(result.status, 'limited')
        self.assertEqual(result.index_research['direction'], '偏空')
        self.assertEqual(result.index_research['quality']['deleted_sentences'], 1)
        self.assertIn('部分内容未通过核对已移除', result.display_answer)
        history = self.run_index('查看上次结果')
        self.assertEqual(history.model_calls_attempted, 0)
        self.assertEqual(history.index_research['quality'], result.index_research['quality'])
        self.assertEqual(history.index_research['direction'], '偏空')

    def test_narrative_rejects_cross_identity_unit_role_and_technical_claim(self):
        result = self.run_index('上证指数只看RSI2').index_result
        from trading_companion.index_demo import index_demo_response
        from trading_companion.tool_model import ToolMessage
        context = answer_context(result)
        payload = json.loads(index_demo_response((ToolMessage('assistant', 'UNTRUSTED_INDEX_CONTEXT\n' + json.dumps(context)),), ()).text)
        self.assertIsNotNone(validate_answer(payload, result, directory=self.directory))
        for text in ('沪深300走势偏多。', '完整日线收盘1000元。', '目标位1000点。',
                     '资金流正在改善。', '海外降息提振指数。', '平安银行值得持有。', 'RSI2高于100，显示超买。',
                     f"完整日线收盘{context['evidence'][1]['raw_value']}点。"):
            invalid = deepcopy(payload)
            invalid['blocks'][1]['text'] = text
            invalid['blocks'][1]['evidence_ids'] = [e['id'] for e in context['evidence']]
            answer = validate_answer(invalid, result, directory=self.directory)
            self.assertIsNotNone(answer, text)
            self.assertNotIn(text, answer.text(result))
            self.assertEqual(answer.quality.deleted_sentences, 1, text)
            self.assertTrue(answer.quality.partial)
            invalid['blocks'] = [dict(title='结论', text=text, evidence_ids=['E1'])]
            self.assertIsNone(validate_answer(invalid, result, directory=self.directory), text)

    def test_dimensionless_indicator_reading_is_not_an_index_price(self):
        result = self.run_index('上证指数只看RSI2').index_result
        from trading_companion.answer_validation import check_claim
        context = answer_context(result)
        reading = next(e for e in context['evidence'] if e['indicator_id'] == 'rsi(window=2)' and e['timeframe'] == 'daily')
        def check(text):
            return check_claim(dict(text=text, evidence_ids=[reading['id']]), instrument=result.request.instrument_id,
                context=context, section='summary', index=0)[2]
        self.assertFalse(check(f"日线RSI2为{reading['raw_value']}点。"))
        self.assertFalse(check(f"日线RSI2读数为{reading['raw_value']}。"))
        self.assertTrue(check(f"指数点位为{reading['raw_value']}点。"))

    def test_cancelled_late_answer_does_not_publish_new_index(self):
        self.run_index('上证指数走势')
        from trading_companion.turn_control import TurnControl
        control = TurnControl()
        class CancelAfterAnswer(DecisionDemoModel):
            def complete(inner, messages, *, tools):
                answer = super().complete(messages, tools=tools)
                if not tools:
                    control.cancel()
                return answer
        outcome = self.run_index('深证成指走势', model=CancelAfterAnswer(), control=control)
        self.assertEqual(outcome.code, 'turn_cancelled')
        self.assertFalse(self.session._subject_current)
        self.assertEqual(self.session.index_saved.result.request.instrument_id, '000001.SH')

    def test_week_boundaries_and_unverified_volume(self):
        request = IndexResearchRequest('000001.SH', date(2026, 9, 1), date(2026, 9, 25),
            (IndicatorSpec('sma', window=3), IndicatorSpec('volume', window=3)))
        result = IndexResearchResult(request, tuple(synthetic_index_bars(request)), NOW)
        completed = index_weeks(result)
        chart = index_weeks(result, completed_only=False)
        self.assertEqual([w[1].trade_date for w in completed], [date(2026, 9, 11), date(2026, 9, 18)])
        self.assertEqual(chart[-1][1].trade_date, date(2026, 9, 25))
        self.assertFalse(chart[-1][2])
        records, _, missing = index_evidence(result)
        self.assertTrue(any('成交量单位未核实' in m for m in missing))
        self.assertFalse(any(e['indicator_id'] == 'volume(window=3)' for e in records))
        rows = [b for b in result.bars if date(2026, 9, 7) <= b.trade_date <= date(2026, 9, 11)]
        bar = completed[0][1]
        self.assertEqual((bar.open_price, bar.high_price, bar.low_price, bar.close_price),
            (rows[0].open_price, max(b.high_price for b in rows), min(b.low_price for b in rows), rows[-1].close_price))

    def test_public_binding_rejects_tampering(self):
        result = self.run_index('上证指数走势')
        view = public_turn(result)
        view['chart']['bars'][0]['volume'] = 0
        from trading_companion.chart_intervals import ChartUnavailable
        with self.assertRaises(ChartUnavailable):
            bind_chart(view, 'synthetic')

    def test_index_archive_recomputes_evidence_and_keeps_model_text_out(self):
        from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError
        result = self.run_index('上证指数走势').index_result
        with ResearchSnapshotStore(':memory:', clock=lambda: NOW) as store:
            identifier = store.save_index(result)
            payload = store.load(identifier)
            self.assertEqual(payload['version'], 'index-evidence-snapshot-v1')
            self.assertNotIn('answer', payload)
            self.assertEqual(payload['evidence'], answer_context(result)['evidence'])
            payload['evidence'][0]['raw_value'] = -1
            self.assertGreater(store.load(identifier)['evidence'][0]['raw_value'], 0)
            self.assertEqual(store.list()[0]['instrument_kind'], 'index')
            self.assertEqual(store.save_index(result), identifier)
            with self.assertRaises(SnapshotStoreError):
                store.load(identifier, known_at=NOW - timedelta(seconds=1))
        from trading_companion.index_snapshot import snapshot_data, validate_snapshot
        data = snapshot_data(result)
        data['index_snapshot']['bars'][3]['close_price'] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(data)

    def test_http_public_index_chart_authorization_and_replay(self):
        from fastapi.testclient import TestClient
        from trading_companion.api import create_app
        service = create_demo_service()
        with TestClient(create_app(service=service), base_url='http://localhost') as client:
            credentials = client.post('/v1/sessions', json={}).json()
            path = '/v1/sessions/' + credentials['session_id']
            headers = {'Authorization': 'Bearer ' + credentials['access_token']}
            request_id = '1234567890abcdef1234567890abcdef'
            first = client.post(path + '/messages', headers=headers, json=dict(request_id=request_id, question='大盘走势'))
            self.assertEqual(first.status_code, 200)
            turn = first.json()['result']
            self.assertEqual(turn['index_research']['instrument_id'], '000001.SH')
            self.assertEqual(turn['chart']['price_unit'], 'index_point')
            replay = client.post(path + '/messages', headers=headers, json=dict(request_id=request_id, question='大盘走势'))
            self.assertEqual(replay.json()['result'], turn)
            chart_path = path + '/chart'
            params = dict(research_id=turn['report']['request_id'], interval='1w')
            self.assertNotEqual(client.get(chart_path, params=params).status_code, 200)
            week = client.get(chart_path, params=params, headers=headers)
            self.assertEqual(week.status_code, 200)
            self.assertEqual(week.json()['version'], 'index-chart-interval-v1')
            other = client.post('/v1/sessions', json={}).json()
            wrong = client.get(chart_path, params=params, headers={'Authorization': 'Bearer ' + other['access_token']})
            self.assertNotEqual(wrong.status_code, 200)
            client.post(path + '/reset', headers=headers, json={})
            self.assertNotEqual(client.get(chart_path, params=params, headers=headers).status_code, 200)
        service.close()

    def test_api_replay_history_week_and_reset(self):
        service = create_demo_service()
        credentials = service.create_session()
        sid, token = credentials['session_id'], credentials['access_token']
        first = service.message(sid, token, 'index1', '大盘走势')
        turn = first['result']
        self.assertEqual(turn['status'], 'completed', turn)
        replay = service.message(sid, token, 'index1', '大盘走势')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['result'], turn)
        week = service.chart(sid, token, turn['report']['request_id'], '1w')
        self.assertEqual(week['status'], 'available')
        history = service.history(sid, token)['result']
        self.assertEqual(history['model_calls_attempted'], 0)
        self.assertEqual(history['index_research']['context'], 'saved')
        service.reset(sid, token)
        self.assertEqual(service.history(sid, token)['result']['code'], 'no_saved_result')
        service.close()


class IndexSourceTests(unittest.TestCase):
    def test_bounded_fixed_origin_daily_transport_and_current_date_rejection(self):
        from trading_companion.index_source import TencentIndexSource, MAX_BYTES
        request = IndexResearchRequest('000001.SH', date(2026, 9, 1), date(2026, 9, 25))
        payload = {'code': 0, 'data': {'sh000001': {'day': [['2026-09-25', '3000', '3002', '3010', '2990']]}}}
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps(payload).encode()]
        with patch('requests.get', return_value=response) as get, patch('trading_companion.index_source._shanghai_today', return_value=date(2026, 9, 26)):
            self.assertEqual(len(TencentIndexSource(request).read_daily_bars()), 1)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(get.call_args.kwargs['stream'])
            self.assertEqual(get.call_args.kwargs['params']['param'], 'sh000001,day,2026-09-01,2026-09-25,640,')
            response.close.assert_called_once()
        response.iter_content.return_value = [b'x' * (MAX_BYTES + 1)]
        with patch('requests.get', return_value=response), self.assertRaises(DataValidationError):
            TencentIndexSource(request).read_daily_bars()
        with patch('requests.get') as get, patch('trading_companion.index_source._shanghai_today', return_value=request.as_of_date):
            with self.assertRaises(InvalidRequestError):
                TencentIndexSource(request).read_daily_bars()
            get.assert_not_called()

    def test_failure_on_any_required_year_does_not_return_partial_history(self):
        from trading_companion.index_source import TencentIndexSource
        from trading_companion.errors import DataSourceError
        request = IndexResearchRequest('000001.SH', date(2025, 12, 1), date(2026, 1, 30))
        first = Mock(status_code=200)
        first.iter_content.return_value = [json.dumps({'code': 0, 'data': {'sh000001': {'day': [['2025-12-31', '3', '3', '4', '2']]}}}).encode()]
        with patch('requests.get', side_effect=[first, Mock(status_code=503)]), self.assertRaises(DataSourceError):
            TencentIndexSource(request).read_daily_bars()

    def test_shapes_and_quantity_not_guessed(self):
        row = ['2026-09-25', '3000', '3002', '3010', '2990', '999', None, None, '888']
        payload = {'code': 0, 'data': {'sh000001': {'day': [row]}}}
        bars = parse_daily(payload, '000001.SH', date(2026, 9, 1), date(2026, 9, 25))
        self.assertEqual(bars[0].close_price, 3002)
        self.assertIsNone(bars[0].volume)
        self.assertIsNone(bars[0].amount)
        for changed in ([row, row], [['2026-09-26', *row[1:]]]):
            payload['data']['sh000001']['day'] = changed
            with self.assertRaises(DataValidationError):
                parse_daily(payload, '000001.SH', date(2026, 9, 1), date(2026, 9, 25))

    def test_old_row_filter_and_wrong_index(self):
        payload = {'code': 0, 'data': {'sh000001': {'day': [['2026-08-31', '3000', '3002', '3010', '2990']]}}}
        self.assertEqual(parse_daily(payload, '000001.SH', date(2026, 9, 1), date(2026, 9, 25)), [])
        with self.assertRaises(DataValidationError):
            parse_daily(payload, '000300.SH', date(2026, 9, 1), date(2026, 9, 25))


if __name__ == '__main__':
    unittest.main()
