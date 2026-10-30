"""Natural technical research shares checked narratives without widening user scope."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verify import offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.product_agent import run_product_research_turn
from trading_companion.request_routing import RoutingContext, RoutingSessionView, route_research_request
from trading_companion.research_session import ResearchSession
from trading_companion.product_research import collect_product_research
from trading_companion.product_scope import ProductScope
from trading_companion.tool_model import ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.turn_trace import JsonlTraceSink
from tests.test_product_agent import ProductModelFixture
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory


class SharedTechnicalTests(unittest.TestCase):
    def setUp(self):
        self.session, self.data, self.factory = ResearchSession(), CompanyDataFixture(), MemorySourceFactory()
        self.now = NOW
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def turn(self, question, model=None):
        return run_product_research_turn(question, session=self.session, model=model or ProductModelFixture(),
            directory=directory(), now=self.now, source='tencent', data_service=self.data, source_factory=self.factory)

    def test_strict_sma_has_narrative_without_extra_indicators_or_means(self):
        model = ProductModelFixture()
        result = self.turn('分析平安银行最近的走势，只看SMA3', model)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([s.indicator_id for s in result.product_plan.indicators], ['sma(window=3)'])
        self.assertEqual(self.data.calls, [])
        self.assertEqual(result.product_answer.to_dict()['version'], 'product-research-answer-v3')
        self.assertTrue(result.product_answer.narratives)
        labels = {e.label for e in result.product_bundle.evidence}
        self.assertFalse({'MA20', 'MA60'} & labels)
        self.assertIn('最新收盘价', labels)
        self.assertFalse(any('MA20' in json.dumps(c, ensure_ascii=False) for c in model.contexts))

    def test_only_technical_synonyms_block_company_collection(self):
        for phrase in ('只看技术面', '仅技术', '纯技术面', '仅看技术指标'):
            with self.subTest(phrase=phrase):
                result = self.turn('分析平安银行最近的走势，' + phrase)
                self.assertEqual(result.product_plan.kinds, ('technical',))
                self.assertEqual(self.data.calls, [])

    def test_not_only_correction_broadens_domains(self):
        result = self.turn('分析平安银行，不是只看技术面，还要基本面')
        self.assertIn('financial', result.product_plan.kinds)
        self.assertTrue(self.data.calls)

    def test_add_replace_remove_preserve_dates_identity_scope_and_new_evidence(self):
        first = self.turn('分析平安银行近60日，只看SMA3')
        original = first.product_plan
        for question, expected in (
            ('再加RSI2', ['sma(window=3)', 'rsi(window=2)']),
            ('把RSI改成RSI6', ['sma(window=3)', 'rsi(window=6)']),
            ('删除RSI6', ['sma(window=3)']),
        ):
            with self.subTest(question=question):
                result = self.turn(question)
                self.assertEqual(result.status, 'completed')
                self.assertEqual([s.indicator_id for s in result.product_plan.indicators], expected)
                self.assertEqual(result.product_plan.start_date, original.start_date)
                self.assertEqual(result.product_plan.end_date, original.end_date)
                self.assertEqual(result.product_plan.securities, original.securities)
                self.assertEqual(result.product_plan.kinds, ('technical',))
                self.assertEqual(result.model_calls_attempted, 1)
        ids = [r.request_id for r in self.factory.reads]
        self.assertEqual(len(ids), len(set(ids)))

    def test_first_indicator_addition_to_news_does_not_activate_dormant_defaults(self):
        first = self.turn('平安银行只看新闻，不要技术面')
        result = self.turn('再加RSI2')
        self.assertEqual(result.status, 'completed')
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ['rsi(window=2)'])
        self.assertEqual(result.product_plan.kinds, ('news', 'technical'))
        self.assertEqual(result.product_plan.end_date, first.product_plan.end_date)
        self.assertNotIn('technical', result.product_plan.scope.excluded)
        self.assertTrue(all(kinds == ('news',) for _, kinds in self.data.calls))

    def test_ambiguous_edit_stays_product_pending_and_does_not_read(self):
        self.turn('平安银行只看SMA3和SMA5')
        count = len(self.factory.reads)
        edit = self.turn('把均线改成10日')
        self.assertEqual(edit.code, 'indicator_edit_ambiguous')
        self.assertEqual(len(self.factory.reads), count)
        self.assertIsNotNone(self.session.product_pending)
        self.assertIsNone(self.session.pending_edit)
        wrong = self.turn('SMA3，再加RSI2')
        self.assertEqual(wrong.code, 'indicator_edit_reply_not_specific')
        self.assertEqual(len(self.factory.reads), count)
        chosen = self.turn('SMA3')
        self.assertEqual(chosen.status, 'completed')
        self.assertEqual([s.indicator_id for s in chosen.product_plan.indicators], ['sma(window=10)', 'sma(window=5)'])
        self.assertIsNone(self.session.product_pending)

    def test_invalid_edits_preserve_original_plan_and_do_not_read(self):
        self.turn('平安银行只看SMA3')
        for question in ('再加SMA3', '把SMA3改成SMA999', '再加RSI2，然后研究新闻', '删除SMA3'):
            with self.subTest(question=question):
                count = len(self.factory.reads)
                result = self.turn(question)
                self.assertEqual(result.status, 'needs_clarification')
                self.assertEqual(len(self.factory.reads), count)
                self.assertEqual(self.session.product_saved.plan.indicators[0].indicator_id, 'sma(window=3)')

    def test_strict_direction_has_valid_decision_without_atr_plan(self):
        result = self.turn('平安银行只看SMA3，未来一个月能买吗')
        self.assertEqual(result.product_plan.objective.kind, 'decision')
        self.assertEqual([s.kind for s in result.product_plan.indicators], ['sma'])
        self.assertTrue(all(p.code == 'risk_atr_not_requested' for p in result.product_bundle.companies[0].trade_plans))
        self.assertEqual(result.product_answer.decisions[0]['status'], 'available')

    def test_followup_inherits_strict_scope_without_atr(self):
        self.turn('平安银行近60日只看SMA3')
        result = self.turn('那现在能买吗')
        self.assertEqual(result.product_plan.kinds, ('technical',))
        self.assertEqual([s.kind for s in result.product_plan.indicators], ['sma'])
        self.assertEqual(result.product_plan.scope.indicator_ids, ('sma(window=3)',))

    def test_expired_result_researches_with_checked_scope_not_old_values(self):
        first = self.turn('平安银行近60日只看SMA3')
        self.now += timedelta(minutes=16)
        result = self.turn('再加RSI2')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.product_plan.start_date, first.product_plan.start_date)
        self.assertEqual(len(self.factory.reads), 2)

    def test_scope_tampering_is_blocked_before_any_source_reads(self):
        from trading_companion.multi_contracts import IndicatorSpec
        from trading_companion.errors import InvalidRequestError
        from trading_companion.turn_control import bind_turn
        first = self.turn('平安银行只看SMA3')
        reads = len(self.factory.reads)
        control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"))
        control.start()
        changed = replace(first.product_plan, indicators=first.product_plan.indicators + (IndicatorSpec('atr', window=14),))
        with bind_turn(control), self.assertRaises(InvalidRequestError):
            collect_product_research(changed, data_service=self.data, now=self.now, source_factory=self.factory)
        self.assertEqual(len(self.factory.reads), reads)

    def test_edit_cancellation_does_not_publish_a_replacement_result(self):
        first = self.turn('平安银行只看SMA3')
        subject = self.session.latest_research
        control = TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"))
        model = ProductModelFixture(on_synthesis=control.cancel)
        result = run_product_research_turn('再加RSI2', session=self.session, model=model,
            directory=directory(), now=self.now, source='tencent', data_service=self.data,
            source_factory=self.factory, control=control)
        self.assertEqual(result.status, 'cancelled')
        self.assertIsNone(result.product_answer)
        self.assertIsNone(self.session.product_saved)
        self.assertEqual(self.session.latest_research, subject)
        self.assertFalse(self.session._subject_current)

    def test_edit_pending_expires_or_source_changes_without_collection(self):
        for source_changed in (False, True):
            self.session = ResearchSession()
            self.now = NOW
            self.turn('平安银行只看SMA3和SMA5')
            self.turn('把均线改成10日')
            reads = len(self.factory.reads)
            if not source_changed:
                self.now += timedelta(minutes=15)
            result = run_product_research_turn('SMA3', session=self.session, model=ProductModelFixture(),
                directory=directory(), now=self.now, source='akshare_eastmoney' if source_changed else 'tencent',
                data_service=self.data, source_factory=self.factory)
            self.assertEqual(result.status, 'needs_clarification')
            self.assertEqual(len(self.factory.reads), reads)

    def test_naked_stop_price_rejected_even_if_it_matches_latest_close(self):
        first = self.turn('平安银行只看SMA3，未来一个月能买吗')
        from trading_companion.product_answer import synthesize_product_answer
        def change(payload, attempt, context):
            self.assertFalse(any(e.get('plan_id') for e in context['evidence']))
            payload['narratives'][0]['current']['text'] = '需要观察趋势。止损10元。'
        run = synthesize_product_answer('技术面能买吗', first.product_bundle, model=NarrativeModel(change), repair_invalid=True)
        self.assertNotIn('止损10元', run.answer.text)
        self.assertIn('需要观察趋势', run.answer.text)
        self.assertEqual(run.answer.decisions[0]['status'], 'available')

    def test_trace_records_technical_intent_and_shared_executor_and_excluded_are_not_missing(self):
        from tests.test_turn_trace import read_records
        from trading_companion.trace_cli import parse_record
        with TemporaryDirectory() as root, patch('trading_companion.product_research._build_source', side_effect=self.factory):
            sink = JsonlTraceSink(root)
            service = ResearchApiService(model_factory=ProductModelFixture, directory_loader=lambda now: directory(),
                data_mode='tencent', product_data_service=self.data, reference_date=NOW.date(),
                clock=lambda: 100., wall_clock=lambda: NOW, trace_sink=sink)
            self.addCleanup(service.close)
            credentials = service.create_session()
            result = service.message(credentials['session_id'], credentials['access_token'], uuid4().hex,
                                     '平安银行只看SMA3')['result']
            company = result['company_research']
            self.assertIn('financial', company['excluded_domains'])
            self.assertFalse(any(s['kind'] == 'financial' for c in company['coverage'] for s in c['sections']))
            record = read_records(root)[0]
            self.assertEqual(record['route']['kind'], 'company_research')
            self.assertEqual(record['route']['intent'], 'technical_research')
            row = parse_record(json.dumps(record).encode('utf-8'))
            self.assertEqual((row['intent'], row['route']), ('technical_research', 'company_research'))


if __name__ == '__main__':
    unittest.main()
