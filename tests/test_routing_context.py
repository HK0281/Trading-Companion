"""Context handover and transactional pending changes through controlled executors."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verify import offline_network
from trading_companion.agent import run_natural_research_turn
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_plan import prepare_product_plan
from trading_companion.research_session import ResearchSession
from trading_companion.routing_context import checked_subject, independent_task
from trading_companion.security_directory import Security
from trading_companion.tool_model import FakeToolModel
from trading_companion.turn_control import company_turn_control
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.model import ModelCallError
from tests.test_product_agent import ProductModelFixture
from tests.test_product_plan import _arguments, _response
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory
from tests.test_research_session import GuardedTests, intent, proposal
from trading_companion.explanation_demo_model import ExplanationDemoModel


class SubjectPublicationTests(unittest.TestCase):
    def setUp(self):
        self.session, self.factory = ResearchSession(), MemorySourceFactory()
        for guard in (offline_network(), patch('trading_companion.product_research._build_source', self.factory)):
            guard.__enter__()
            self.addCleanup(guard.__exit__, None, None, None)

    def company(self, question='分析平安银行的基本面', *, names=('平安银行',), now=NOW, model=None, **options):
        return run_product_research_turn(question, session=self.session, model=model or ProductModelFixture(names),
            directory=directory(), now=now, source='tencent', data_service=CompanyDataFixture(), **options)

    def test_published_order_advances_only_with_new_research(self):
        self.company()
        first = self.session.latest_research
        self.company('查看上次结果')
        self.assertEqual(self.session.latest_research, first)
        self.company('分析浦发银行基本面', names=('浦发银行',))
        self.assertGreater(self.session.latest_research.sequence, first.sequence)
        self.assertEqual(self.session.latest_research.published_at, first.published_at)
        self.assertEqual(self.session.latest_research.securities[0].instrument_id, '600000.SH')

    def test_expired_cache_keeps_identity_but_collects_again(self):
        self.company()
        old = self.session.product_saved
        self.company('那新闻呢', now=NOW+timedelta(minutes=16))
        self.assertIsNot(self.session.product_saved, old)
        self.assertEqual(self.session.product_saved.saved_at, NOW+timedelta(minutes=16))
        self.assertEqual(self.session.product_saved.plan.securities, old.plan.securities)

    def test_company_selection_with_indicator_condition_reaches_daily_executor(self):
        self.company('分析平安的基本面', names=('平安',))
        result = self.company('第一个，添加RSI2')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.factory.requests[-1].indicators[-1].parameters, {'window': 2})
        self.assertEqual(self.factory.requests[-1].indicators[-1].kind, 'rsi')
        self.assertEqual(len(self.factory.reads), 1)

    def test_cancelled_new_subject_is_not_published(self):
        self.company()
        old = self.session.latest_research
        control = company_turn_control()
        result = self.company('分析浦发银行基本面', names=('浦发银行',), control=control,
            model=ProductModelFixture(('浦发银行',), on_synthesis=control.cancel))
        self.assertEqual(result.code, 'turn_cancelled')
        self.assertEqual(self.session.latest_research, old)
        self.assertFalse(self.session._subject_current)

    def test_directory_identity_must_still_match_and_new_dialog_clears_it(self):
        self.company()
        changed = replace(directory(), securities=(Security('000001.SZ', '另一公司'),))
        securities, code = checked_subject(self.session, directory=changed, reference_date=NOW.date(),
                                          development=False, now=NOW)
        self.assertFalse(securities)
        self.assertEqual(code, 'research_subject_identity_changed')
        self.company('新对话')
        self.assertIsNone(self.session.latest_research)

    def test_multiple_subjects_require_explicit_plural_or_name(self):
        self.company('比较平安银行和浦发银行基本面', names=('平安银行', '浦发银行'))
        self.assertEqual(self.company('那现在能买吗').code, 'decision_instrument_required')
        result = self.company('这两只的新闻呢', names=('平安银行', '浦发银行'))
        self.assertEqual(len(result.product_plan.securities), 2)

    def test_pending_replacement_failure_and_cancellation_keep_original(self):
        self.company('分析平安基本面', names=('平安',))
        old = self.session.product_pending
        self.assertIsNotNone(old)
        with patch('trading_companion.product_agent.collect_product_research', side_effect=ValueError('invalid')):
            self.assertEqual(self.company('分析浦发银行基本面', names=('浦发银行',)).status, 'failed')
        self.assertEqual(self.session.product_pending, old)
        control = company_turn_control()
        self.company('分析浦发银行基本面', names=('浦发银行',), control=control,
                     model=ProductModelFixture(('浦发银行',), on_synthesis=control.cancel))
        self.assertEqual(self.session.product_pending, old)
        self.company('分析浦发银行基本面', names=('浦发银行',))
        self.assertIsNone(self.session.product_pending)
        self.assertEqual(self.session.latest_research.securities[0].instrument_id, '600000.SH')

    def test_model_failure_and_budget_stop_do_not_consume_replaced_pending(self):
        self.company('分析平安基本面', names=('平安',))
        old = self.session.product_pending
        with patch.object(ProductModelFixture, 'complete', side_effect=ModelCallError('model_unavailable', 'offline')):
            result = self.company('分析浦发银行基本面', names=('浦发银行',))
        self.assertNotEqual(result.status, 'completed')
        self.assertIs(self.session.product_pending, old)
        result = self.company('分析浦发银行基本面', names=('浦发银行',),
                              control=TurnControl(TurnLimits(model_calls=1)))
        self.assertEqual(result.runtime['stop_code'], 'model_call_budget_exhausted')
        self.assertIs(self.session.product_pending, old)
        self.assertIsNone(self.session.latest_research)

    def test_source_change_rechecks_identity_and_does_not_inherit_old_scope(self):
        self.company('分析平安银行最近一个月的基本面', model=ProductModelFixture(date_expression='最近一个月'))
        old = self.session.latest_research
        result = run_product_research_turn('那新闻呢', session=self.session, model=ProductModelFixture(),
            directory=directory(), now=NOW, source='akshare_eastmoney', data_service=CompanyDataFixture())
        self.assertIsNotNone(result.product_plan, (result.status, result.code, result.message))
        self.assertEqual(result.product_plan.securities, old.securities)
        self.assertEqual(result.product_plan.source, 'akshare_eastmoney')
        self.assertNotEqual(result.product_plan.start_date, old.scope.start_date)
        self.assertNotIn('scope:previous_research', result.product_plan.applied_defaults)


class PendingSelectionTests(GuardedTests):
    def test_ordinal_selection_uses_displayed_order(self):
        self.begin()
        result = self.turn('第一个', model=ExplanationDemoModel([proposal()]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.confirmed_request.instrument_id, '000001.SZ')

    def test_candidate_and_date_update_are_both_used(self):
        self.begin()
        result = self.turn('平安银行，改看近60日', model=ExplanationDemoModel([
            proposal(start_date='2026-07-28')]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.confirmed_request.start_date.isoformat(), '2026-07-28')
        self.assertIsNone(self.session.pending)

    def test_complete_new_technical_research_bypasses_old_pending(self):
        self.begin()
        old = self.session.pending
        result = self.turn('平安银行最近一个月只看SMA3和RSI2',
            model=ExplanationDemoModel([intent(['平安银行']), proposal()]))
        self.assertEqual(result.status, 'completed')
        self.assertIsNone(self.session.pending)
        self.assertEqual(self.session.latest_research.kind, 'technical')
        self.assertNotEqual(self.session.latest_research.securities, old.candidates)

    def test_company_candidate_conditions_retain_domains_and_apply_dates(self):
        first = prepare_product_plan('分析平安的基本面', model=FakeToolModel([_response(_arguments(['平安']))]),
            directory=self.directory, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        plan = prepare_product_plan('平安银行，改看近60日', model=FakeToolModel([]), context=first.context,
            directory=self.directory, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        self.assertEqual(plan.status, 'ready')
        self.assertEqual(plan.kinds, first.kinds)
        self.assertEqual(plan.start_date.isoformat(), '2026-07-28')

    def test_company_candidate_indicator_update_adds_the_requested_domain(self):
        first = prepare_product_plan('分析平安的基本面', model=FakeToolModel([_response(_arguments(['平安']))]),
            directory=self.directory, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        plan = prepare_product_plan('第一个，添加RSI2', model=FakeToolModel([]), context=first.context,
            directory=self.directory, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        self.assertEqual(plan.status, 'ready')
        self.assertIn('technical', plan.kinds)
        self.assertTrue(plan.indicators_explicit)
        self.assertEqual(plan.indicators[-1].kind, 'rsi')
        self.assertEqual(plan.indicators[-1].parameters, {'window': 2})

    def test_removed_company_candidate_does_not_shift_ordinal_positions(self):
        first = prepare_product_plan('分析平安的基本面', model=FakeToolModel([_response(_arguments(['平安']))]),
            directory=self.directory, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        changed = replace(self.directory, securities=tuple(s for s in self.directory.securities
                          if s.instrument_id != first.context.candidate_ids[0]))
        plan = prepare_product_plan('第一个', model=FakeToolModel([]), context=first.context,
            directory=changed, reference_date=NOW.date(), now=NOW, source='csv', development=True)
        self.assertEqual(plan.code, 'product_identity_changed')
        self.assertNotEqual(plan.status, 'ready')

    def test_second_identity_in_selection_suffix_is_not_silently_ignored(self):
        self.begin()
        old = self.session.pending
        result = self.turn('平安银行，换成中国平安', model=FakeToolModel([]))
        self.assertEqual(result.code, 'condition_update_not_supported')
        self.assertIs(self.session.pending, old)

    def test_bare_outside_candidate_is_not_a_new_task(self):
        self.assertFalse(independent_task('贵州茅台', self.directory))
        self.assertTrue(independent_task('贵州茅台未来一个月怎么看', self.directory))


class CrossSubjectApiTests(unittest.TestCase):
    def test_both_cross_kind_orders_choose_last_published_identity_at_same_time(self):
        from trading_companion.api_service import ResearchApiService
        from trading_companion.decision_answer_evaluation import DecisionContractModel, DecisionFixedSources, load_corpus
        from trading_companion.workflow_evaluation_fixtures import NOW as API_NOW, REFERENCE_DATE

        cases, _ = load_corpus()
        for kinds in (('company', 'technical'), ('technical', 'company')):
            with self.subTest(kinds=kinds), offline_network(), DecisionFixedSources(cases, 3) as sources:
                model = DecisionContractModel()
                service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda _: sources.directory,
                    data_mode='tencent', reference_date=REFERENCE_DATE, product_data_service=sources.company,
                    wall_clock=lambda: API_NOW)
                self.addCleanup(service.close)
                credentials = service.create_session()

                def send(question):
                    return service.message(credentials['session_id'], credentials['access_token'], uuid4().hex, question)['result']

                for case, kind in zip(cases[:2], kinds):
                    model.start(case, seed=kind == 'technical')
                    result = send(case['security_name'] + ('最近只看SMA3' if kind == 'technical' else '现在能买吗'))
                    self.assertIn(result['status'], ('completed', 'limited'), (kind, result['code'], result['message']))
                session = service._entries[credentials['session_id']].session
                subject = session.latest_research
                self.assertEqual((subject.sequence, subject.kind, subject.published_at), (2, kinds[1], API_NOW))
                if kinds[1] == 'technical':
                    session._confirmed = replace(session.confirmed, confirmed_at=API_NOW-timedelta(minutes=16))
                    session._last_result = replace(session._last_result, saved_at=API_NOW-timedelta(minutes=16))
                reads = sources.daily_reads
                model.start(cases[1])
                result = send('那现在能买吗')
                self.assertEqual(result['code'], 'company_research_completed')
                self.assertEqual(result['company_research']['securities'][0]['instrument_id'], cases[1]['instrument_id'])
                self.assertEqual(session.latest_research.sequence, 3)
                self.assertEqual(session.product_saved.saved_at, API_NOW)
                self.assertGreater(sources.daily_reads, reads)
                other = service.create_session()
                empty = service.message(other['session_id'], other['access_token'], uuid4().hex, '那现在能买吗')['result']
                self.assertEqual(empty['code'], 'decision_instrument_required')
