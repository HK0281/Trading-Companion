"""Horizon gate through the real orchestrator, exact prices, and saved evidence isolation."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import unittest

from trading_companion.agent_input import InputIssue
from trading_companion.daily_bar import DailyBar
from trading_companion.decision_horizon import canonical, profile
from trading_companion.horizon_trade_plan import weekly_atr_evidence
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_decision_answer import _context
from trading_companion.product_research import collect_product_research
from trading_companion.research_objective import extract_outlook, ResearchObjective
from trading_companion.research_session import ResearchSession
from trading_companion.snapshot_store import _public_data, SnapshotStoreError
from trading_companion.trade_plan import build_trade_plans, HORIZON_METHOD
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import NOW, directory, plan, CompanyDataFixture, MemorySourceFactory, MemoryDailySource
from tests.test_trade_plan import market_result


class HorizonTests(unittest.TestCase):
    def setUp(self):
        self.session, self.model = ResearchSession(), ProductModelFixture()
        self.factory, self.data = MemorySourceFactory(), CompanyDataFixture()

    def send(self, question, *, now=NOW, **kwargs):
        return run_product_research_turn(question, session=self.session, model=self.model,
            directory=kwargs.pop('directory', directory()), now=now, source=kwargs.pop('source', 'tencent'),
            data_service=self.data, source_factory=self.factory, **kwargs)

    def test_missing_horizon_collects_nothing_then_resumes_original_without_interpretation(self):
        first = self.send('平安银行现在能买吗')
        self.assertEqual(first.code, 'decision_horizon_required')
        self.assertEqual(first.product_plan.candidates, ())
        self.assertEqual(first.product_plan.objective.horizon, None)
        self.assertEqual((len(self.factory.reads), len(self.data.calls), len(self.model.calls)), (0, 0, 1))
        self.assertEqual([o['value'] for o in first.product_plan.to_dict()['clarification_options']], ['short', 'medium', 'long'])
        second = self.send('中线')
        self.assertEqual(second.code, 'company_research_completed')
        self.assertEqual(second.product_plan.user_question, '平安银行现在能买吗')
        self.assertEqual(second.model_calls_attempted, 1)
        self.assertEqual(second.product_plan.objective.horizon, '未来40个交易日')
        self.assertIn('horizon_source:clarification', second.product_plan.applied_defaults)
        self.assertEqual(dict(second.product_bundle.companies[0].trade_plans[0].values)['reference_stop'], '5.00')

    def test_explicit_horizon_and_equal_stock_inheritance(self):
        first = self.send('平安银行现在能买吗，未来4个月')
        self.assertEqual(first.code, 'company_research_completed')
        self.assertEqual(first.product_plan.objective.horizon, '未来4个月')
        self.assertEqual(first.product_bundle.companies[0].trade_plans[0].horizon_profile, 'medium')
        inherited = self.send('平安银行现在能买吗')
        self.assertEqual(inherited.product_plan.objective.horizon, '未来4个月')
        self.assertIn('horizon_source:inherited', inherited.product_plan.applied_defaults)
        explicit = self.send('那现在能买吗，短线')
        self.assertEqual(explicit.product_plan.objective.horizon, '未来10个交易日')
        self.assertIn('horizon_source:explicit', explicit.product_plan.applied_defaults)

    def test_new_stock_does_not_inherit_horizon(self):
        self.send('平安银行现在能买吗，中线')
        self.model.mentions = ('浦发银行',)
        result = self.send('浦发银行现在能买吗')
        self.assertEqual(result.code, 'decision_horizon_required')
        self.assertEqual(result.product_plan.securities[0].instrument_id, '600000.SH')

    def test_expired_research_does_not_inherit_horizon(self):
        self.send('平安银行现在能买吗，中线')
        result = self.send('那现在能买吗', now=NOW+timedelta(minutes=15))
        self.assertEqual(result.code, 'decision_horizon_required')
        self.assertEqual(len(self.factory.reads), 1)

    def test_identity_then_horizon_share_original_expiry(self):
        self.model.mentions = ('平安',)
        first = self.send('平安现在能买吗')
        self.assertEqual(first.code, 'product_instrument_needs_clarification')
        second = self.send('平安银行', now=NOW+timedelta(minutes=14))
        self.assertEqual(second.code, 'decision_horizon_required')
        self.assertEqual(self.session.product_pending.expires_at, NOW+timedelta(minutes=15))
        final = self.send('中线', now=NOW+timedelta(minutes=15))
        self.assertEqual(final.code, 'product_clarification_expired')
        self.assertEqual(len(self.factory.reads), 0)

    def test_horizon_reply_is_bound_to_identity_source_and_directory(self):
        self.send('平安银行现在能买吗')
        wrong = self.send('浦发银行，中线')
        self.assertEqual(wrong.code, 'decision_horizon_instrument_mismatch')
        changed = self.send('中线', source='akshare_eastmoney')
        self.assertEqual(changed.code, 'product_clarification_expired')
        self.assertEqual(len(self.factory.reads), 0)

    def test_cancellation_reset_and_conflict_do_not_read(self):
        self.send('平安银行现在能买吗')
        self.assertEqual(self.send('短线还是中线').code, 'conflicting_outlook_horizons')
        self.assertEqual(self.send('取消').code, 'research_cancelled')
        self.assertIsNone(self.session.product_pending)
        self.send('平安银行现在能买吗')
        self.session.clear()
        self.assertEqual(len(self.factory.reads), 0)
        self.assertIsNone(self.session.product_pending)

    def test_identity_reply_can_confirm_horizon_and_keep_complete_question(self):
        self.model.mentions = ('平安',)
        self.send('分析平安基本面和技术面，现在能买吗')
        result = self.send('平安银行，中线')
        self.assertEqual(result.code, 'company_research_completed')
        self.assertEqual(result.product_plan.objective.horizon, '未来40个交易日')
        self.assertIn('基本面和技术面', result.product_plan.user_question)
        self.assertEqual(len(self.model.calls), 2)

    def test_new_complete_question_replaces_pending_without_using_old_horizon(self):
        self.send('平安银行现在能买吗')
        self.model.mentions = ('浦发银行',)
        result = self.send('分析浦发银行的新闻')
        self.assertEqual(result.code, 'company_research_completed')
        self.assertEqual(result.product_plan.securities[0].instrument_id, '600000.SH')
        self.assertIsNone(self.session.product_pending)
        self.assertIsNone(result.product_plan.objective.horizon)
        self.assertEqual(len(self.factory.reads), 0)

    def test_long_default_expands_to_366_but_explicit_short_history_is_retained(self):
        default = self.send('平安银行现在能买吗，长线')
        self.assertEqual((default.product_plan.end_date-default.product_plan.start_date).days+1, 366)
        self.session.clear()
        self.model.date_expression = '近60日'
        explicit = self.send('平安银行近60日现在能买吗，长线')
        self.assertEqual((explicit.product_plan.end_date-explicit.product_plan.start_date).days+1, 60)
        plan = explicit.product_bundle.companies[0].trade_plans[0]
        self.assertEqual((plan.code, plan.values), ('plan_long_history_insufficient', ()))

    def test_analysis_direction_and_disabled_prices_do_not_ask(self):
        for question in ('分析平安银行最近的走势', '预测平安银行接下来的走势',
                         '平安银行现在能买吗，不要价位', '平安银行现在能买吗，排除计划',
                         '平安银行现在能买吗，只看SMA3', '平安银行现在能买吗，不要ATR',
                         '平安银行现在能买吗，不看技术面'):
            with self.subTest(question=question):
                self.session.clear()
                result = self.send(question)
                self.assertNotEqual(result.code, 'decision_horizon_required')
                self.assertIsNone(self.session.product_pending)
                if '走势' in question:
                    self.assertTrue(all(p.values == () for p in result.product_bundle.companies[0].trade_plans))
                else:
                    self.assertTrue(all(p.status == 'unavailable' for p in result.product_bundle.companies[0].trade_plans))

    def test_direction_without_horizon_accepts_only_explicit_null_from_model(self):
        from trading_companion.product_answer import synthesize_product_answer
        from tests.test_product_narrative_answer import NarrativeModel
        result = self.send('预测平安银行接下来的走势')
        for value in (None, {}, False, '', '未来40个交易日'):
            with self.subTest(value=value):
                def change(payload, attempt, context):
                    payload['decisions'][0]['horizon'] = value
                answer = synthesize_product_answer('预测走势', result.product_bundle,
                    model=NarrativeModel(change), repair_invalid=False).answer
                self.assertEqual(answer.decisions[0]['status'], 'available' if value is None else 'unavailable')

    def test_risk_followup_asks_and_uses_the_selected_formula(self):
        self.send('分析平安银行的技术面')
        self.assertEqual(self.send('那止盈止损呢').code, 'decision_horizon_required')
        final = self.send('短线')
        risk = final.product_bundle.companies[0].risk
        self.assertEqual(risk.to_dict()['version'], 'risk-scenario-v2')
        self.assertEqual((risk.values.reference_stop, risk.values.reference_target), ('7.00', '14.50'))

    def test_strict_risk_entry_reports_missing_atr_instead_of_asking_horizon(self):
        result = self.send('分析平安银行止盈止损，只看SMA3')
        self.assertNotEqual(result.code, 'decision_horizon_required')
        self.assertIsNone(self.session.product_pending)
        risk = result.product_bundle.companies[0].risk
        self.assertEqual((risk.code,risk.values), ('risk_atr_not_requested',None))

    def test_numeric_horizons_do_not_change_the_historical_request(self):
        values = [('短线', '未来10个交易日', 'short'), ('1-2周', '未来10个交易日', 'short'),
                  ('中线', '未来40个交易日', 'medium'), ('1-3个月', '未来40个交易日', 'medium'),
                  ('长线', '未来120个交易日', 'long'), ('半年以上', '未来120个交易日', 'long'),
                  ('持有半年', '未来6个月', 'long'), ('未来4个月', '未来4个月', 'medium'),
                  ('未来14天', '未来14天', 'short'), ('未来15天', '未来15天', 'medium'),
                  ('未来180天', '未来180天', 'long')]
        for text, expected, selected in values:
            with self.subTest(text=text):
                horizon = canonical(extract_outlook(text).horizon)
                self.assertEqual((horizon, profile(horizon)), (expected, selected))
        with self.assertRaises(InputIssue):
            extract_outlook('未来两年')


class HorizonPriceTests(unittest.TestCase):
    def test_daily_formulas_and_v1_identity_remain_separate(self):
        result = market_result()
        old = build_trade_plans(result, expected_request=result.request)[0]
        short = build_trade_plans(result, expected_request=result.request, method=HORIZON_METHOD, horizon='未来10个交易日')[0]
        medium = build_trade_plans(result, expected_request=result.request, method=HORIZON_METHOD, horizon='未来40个交易日')[0]
        self.assertEqual([dict(p.values)['reference_stop'] for p in (short, medium)], ['7.00', '5.00'])
        self.assertEqual([dict(p.values)['target_2'] for p in (short, medium)], ['17.50', '25.00'])
        self.assertEqual((old.to_dict()['method'], short.to_dict()['method']), ('atr_reference_plan_v1', HORIZON_METHOD))
        self.assertEqual(len({p.plan_id for p in (old, short, medium)}), 3)

    def long_bundle(self):
        class YearSource(MemoryDailySource):
            def read_daily_bars(self):
                return [DailyBar(self.request.instrument_id, self.request.start_date+timedelta(days=i),
                    100., 101., 99., 100., 10000, 1000000.) for i in range(366)]
        selected = replace(plan(kinds=('technical',)), start_date=date(2025,9,25),
            user_question='平安银行现在能买吗',
            objective=ResearchObjective('decision', '未来120个交易日'),
            indicators=(IndicatorSpec('atr', window=14),), applied_defaults=('plan_method:'+HORIZON_METHOD,))
        return collect_product_research(selected, data_service=CompanyDataFixture(), now=NOW,
            source_factory=lambda request: YearSource(request, MemorySourceFactory()))

    def test_weekly_atr_has_complete_weeks_daily_dependencies_and_distinct_dates(self):
        bundle = self.long_bundle()
        company = bundle.companies[0]
        p = company.trade_plans[0]
        self.assertEqual((p.status, p.atr_timeframe, p.source_date, p.atr_source_date),
                         ('available', 'weekly', '2026-09-25', '2026-09-20'))
        self.assertEqual(dict(p.values)['reference_stop'], '96.00')
        self.assertEqual((dict(p.values)['target_1'], dict(p.values)['target_2']), ('108.00', '114.00'))
        weekly = next(e for e in company.evidence if e.temporal_scope == 'completed_week_atr_derived')
        self.assertEqual(len(weekly.dependencies) % 4, 0)
        self.assertTrue(all('daily_ohlc:' in key for key in weekly.dependencies))
        self.assertNotIn('2026-09-21', ''.join(weekly.dependencies))
        self.assertNotIn('2025-09-25', ''.join(weekly.dependencies))
        _context(bundle)
        _public_data(bundle)
        from trading_companion.answer_technical_validation import technical_context
        from trading_companion.product_narrative_answer import _context as narrative_context
        context = narrative_context(bundle)
        sidecar = technical_context(bundle, context)['000001.SZ']['values']
        self.assertTrue(any(v['indicator_id'] == 'atr(window=14)' and v['timeframe'] == 'weekly'
                            and v['date'] == '2026-09-20' for v in sidecar))

    def test_long_history_and_week_count_never_fall_back_to_daily(self):
        result = market_result()
        p = build_trade_plans(result, expected_request=result.request, method=HORIZON_METHOD, horizon='未来120个交易日')[0]
        self.assertEqual((p.status, p.code, p.values), ('unavailable', 'plan_weekly_atr_unavailable', ()))
        # A wide requested window with only a few observed days is also unavailable.
        self.assertNotEqual(p.atr_timeframe, 'daily')

    def test_horizon_prices_and_weekly_dependency_tampering_are_rejected(self):
        bundle = self.long_bundle()
        company = bundle.companies[0]
        variants = [replace(company, trade_plans=(replace(company.trade_plans[0], horizon='未来40个交易日'), *company.trade_plans[1:])),
                    replace(company, evidence=tuple(replace(e, dependencies=e.dependencies[:-1])
                        if e.temporal_scope == 'completed_week_atr_derived' else e for e in company.evidence))]
        for changed in variants:
            with self.subTest(changed=changed.instrument_id):
                polluted = replace(bundle, companies=(changed,))
                with self.assertRaises(ValueError): _context(polluted)
                with self.assertRaises(SnapshotStoreError): _public_data(polluted)


if __name__ == '__main__':
    unittest.main()
