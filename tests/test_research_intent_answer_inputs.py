"""Compare complete answer/repair requests through both APIs with fixed offline data."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from scripts.verify import clean_environment, offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.conversation_memory import TurnDigest
from trading_companion.multi_contracts import DEFAULT_INDICATORS
from trading_companion.product_answer import synthesize_product_answer
from tests.test_product_agent import ProductModelFixture
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory
from tests.test_research_intent_adapter import IntentFixture
from tests.test_research_intent_v2 import draft


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def force_one_repair(payload, attempt, context):
    if attempt == 1:
        payload['narratives'][0]['current']['text'] = '收盘999元。'


class AnswerInputModel:
    def __init__(self):
        self.legacy = ProductModelFixture()
        self.answer = NarrativeModel(force_one_repair)

    def complete(self, messages, *, tools):
        return self.legacy.complete(messages, tools) if tools else self.answer.complete(messages, tools=tools)


def observe(questions, values, *, enabled, history=()):
    """Use the real session/history/adapter/executor path, never a model or data service online."""
    model, intent, factory = AnswerInputModel(), IntentFixture(deepcopy(values)), MemorySourceFactory()
    rows, captures = [], []

    def capture(question, bundle, **kwargs):
        captures.append(dict(question=question, plan=bundle.plan, bundle=bundle))
        return synthesize_product_answer(question, bundle, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())
        stack.enter_context(patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('No .env')))
        stack.enter_context(patch('trading_companion.product_research._build_source', side_effect=factory))
        # Random snapshot IDs normally differ even for identical evidence. Fix the
        # ID here, rather than deleting any part of the compared model messages.
        stack.enter_context(patch('trading_companion.multi_contracts.uuid4', return_value=UUID(int=1234)))
        stack.enter_context(patch('trading_companion.product_answer.synthesize_product_answer', side_effect=capture))
        for module in ('multi_analysis', 'analysis', 'research_tool'):
            stack.enter_context(patch(f'trading_companion.{module}.datetime', FixedDatetime))
        service = ResearchApiService(model_factory=lambda: model, provider='deepseek',
            intent_model_factory=lambda: intent, directory_loader=lambda _: directory(), data_mode='tencent',
            wall_clock=lambda: NOW, reference_date=NOW.date(), product_data_service=CompanyDataFixture(),
            research_intent_v2_enabled=enabled, conversation_context=True)
        stack.callback(service.close)
        binding = service.create_session()
        entry = service._entries[binding['session_id']]
        entry.digests = list(history)
        for index, question in enumerate(questions):
            captures.clear()
            model.answer.calls.clear()
            result = service.message(binding['session_id'], binding['access_token'], str(index), question)['result']
            if enabled and model.legacy.calls:
                raise AssertionError('New-path fixture unexpectedly used legacy interpretation')
            rows.append(dict(captures=list(captures), result=result,
                requests=deepcopy(model.answer.calls), reads=list(factory.reads)))
    return rows


def profile_draft():
    return draft(research_kinds=[dict(kind='profile', phrase='公司资料')],
                 strict_scope=True, scope_phrase='只研究平安银行公司资料')


def price_draft(phrase='建仓价位'):
    return draft(objective_kind='decision', objective_phrase=phrase, wants_plan=True,
                 plan_phrase=phrase, horizon_expression='短线')


def answer_context(row):
    return json.loads(row['requests'][0][2].content.split('\n', 1)[1])


class AnswerInputTests(unittest.TestCase):
    def pair(self, questions, values, **kwargs):
        return (observe(questions, values, enabled=False, **kwargs),
                observe(questions, values, enabled=True, **kwargs))

    def test_broad_analysis_and_repair_messages_match_with_and_without_history(self):
        for history in ((), (TurnDigest('此前只做知识说明', 'knowledge', 'completed'),)):
            with self.subTest(history=bool(history)):
                old, new = self.pair(['分析平安银行', '分析平安银行'], [draft(), draft()], history=history)
                for left, right in zip(old, new, strict=True):
                    self.assertEqual(len(left['requests']), 2)
                    self.assertEqual(left['requests'], right['requests'])
                    self.assertEqual(left['captures'][0]['bundle'].evidence, right['captures'][0]['bundle'].evidence)
                    self.assertEqual(left['captures'][0]['plan'].objective, right['captures'][0]['plan'].objective)
                    self.assertEqual(left['captures'][0]['plan'].applied_defaults,
                                     right['captures'][0]['plan'].applied_defaults)
                    self.assertEqual(left['result']['company_research']['quality'], right['result']['company_research']['quality'])
                self.assertIn('本会话此前几轮的摘要', new[-1]['requests'][0][0].content)
                if history:
                    self.assertIn('此前只做知识说明', new[0]['requests'][0][0].content)

    def test_same_price_scope_has_identical_initial_and_repair_inputs(self):
        old, new = self.pair(['平安银行现在能买吗，短线'], [price_draft('能买吗')])
        self.assertEqual(len(old[0]['requests']), 2)
        self.assertEqual(old[0]['requests'], new[0]['requests'])
        self.assertEqual(answer_context(new[0])['objective'], dict(kind='decision', horizon='未来10个交易日'))

    def test_saved_explanation_uses_identical_question_context_budget_history_and_repair(self):
        value = draft(operation='explain', subject=dict(source='previous', mentions=[], selection=None),
            reference=dict(kind='latest', index=None, scope='latest_research'))
        old, new = self.pair(['分析平安银行', '解释上一份研究结论的依据'], [draft(), value])
        for rows in (old, new):
            self.assertEqual(len(rows[0]['reads']), len(rows[1]['reads']))
            self.assertEqual(rows[1]['result']['code'], 'product_followup_answered')
            self.assertEqual(rows[1]['captures'][0]['question'],
                '基于刚才保存的研究资料，未重新取数。解释上一份研究结论的依据')
        self.assertEqual(len(new[1]['requests']), 2)
        self.assertEqual(old[1]['requests'], new[1]['requests'])

    def test_profile_only_keeps_intentional_domain_difference_without_changing_instruction(self):
        old, new = self.pair(['只研究平安银行公司资料'], [profile_draft()])
        old, new = old[0], new[0]
        self.assertEqual(old['captures'][0]['plan'].kinds, ('profile', 'financial'))
        self.assertEqual(new['captures'][0]['plan'].kinds, ('profile',))
        self.assertEqual({e.kind for e in new['captures'][0]['bundle'].evidence}, {'profile'})
        self.assertEqual(len(new['requests']), 2)
        self.assertEqual(old['requests'][0][:2], new['requests'][0][:2])
        self.assertEqual(old['requests'][0][-1], new['requests'][0][-1])
        self.assertNotEqual(answer_context(old), answer_context(new))

    def test_profile_then_ema_uses_checked_scope_without_inheriting_price_permission(self):
        followup = draft(operation='update', subject=dict(source='previous', mentions=[], selection=None),
            reference=dict(kind='latest', index=None, scope='latest_research'), indicator_mentions=['EMA13'])
        old, new = self.pair(['只研究平安银行公司资料', '上一份补EMA13'], [profile_draft(), followup])
        self.assertEqual(old[-1]['captures'][0]['plan'].kinds, ())
        self.assertEqual(old[-1]['requests'], [])
        plan = new[-1]['captures'][0]['plan']
        self.assertEqual(plan.kinds, ('profile', 'technical'))
        self.assertFalse(plan.checked_prices)
        self.assertIsNone(plan.objective.horizon)
        self.assertEqual(new[-1]['captures'][0]['question'], '上一份补EMA13')
        self.assertTrue(any(s.kind == 'ema' and s.parameters['window'] == 13 for s in new[-1]['reads'][-1].indicators))
        self.assertEqual(len(new[-1]['requests']), 2)
        self.assertEqual(plan.indicators[:-1], DEFAULT_INDICATORS)
        self.assertEqual(plan.answer_priority_indicators, ('ema(window=13)',))
        original = next(e for e in new[-1]['captures'][0]['bundle'].evidence if e.label == 'EMA(13)')
        for request in new[-1]['requests']:
            context = json.loads(request[2].content.split('\n', 1)[1])
            with self.subTest(repair=request is new[-1]['requests'][-1]):
                supplied = next((e for e in context['evidence'] if e['label'] == 'EMA(13)'), None)
                self.assertIsNotNone(supplied, 'The actual answer request must contain the daily EMA13 reading')
                self.assertEqual(supplied['raw_value'], original.raw_value)
                self.assertEqual(supplied['date'], original.date)
                self.assertEqual(supplied['temporal_scope'], 'daily_date_filtered_not_versioned_pit')

    def test_risk_only_diff_is_bounded_evidence_without_new_price_or_prompt(self):
        value = draft(research_kinds=[dict(kind='risk', phrase='风险')],
                      exclusions=[dict(kind='plan', phrase='不要价位')])
        old, new = self.pair(['平安银行风险大吗，不要价位'], [value])
        old, new = old[0], new[0]
        self.assertEqual(old['captures'][0]['plan'].kinds, ('profile', 'financial', 'news', 'announcements', 'technical'))
        self.assertEqual(new['captures'][0]['plan'].kinds, ())
        self.assertTrue(new['captures'][0]['plan'].risk_dependencies_only)
        self.assertFalse(new['captures'][0]['plan'].checked_prices)
        self.assertTrue(all(s.kind in ('atr', 'boll') for s in new['reads'][0].indicators))
        self.assertFalse(answer_context(new).get('trade_plans'))
        self.assertEqual(old['requests'][0][:2], new['requests'][0][:2])

    def test_explicit_reading_survives_final_trimming_with_more_company_background(self):
        followup = draft(operation='update', subject=dict(source='previous', mentions=[], selection=None),
            reference=dict(kind='latest', index=None, scope='latest_research'), indicator_mentions=['EMA13'])
        row = observe(['只研究平安银行公司资料', '上一份补EMA13'], [profile_draft(), followup], enabled=True)[-1]
        bundle = row['captures'][0]['bundle']
        company = bundle.companies[0]
        profile = next(e for e in company.evidence if e.kind == 'profile')
        text = '人工公司背景说明。' * 30
        extra = tuple(replace(profile, id=f'{profile.id}:{i}', label=f'背景字段{i}', display=text, raw_value=text)
                      for i in range(7))
        crowded = replace(bundle, companies=(replace(company, evidence=(*extra, *company.evidence)),))
        model = AnswerInputModel().answer
        synthesize_product_answer('上一份补EMA13', crowded, model=model, repair_invalid=True)
        self.assertEqual(len(model.calls), 2)
        for request in model.calls:
            context = json.loads(request[2].content.split('\n', 1)[1])
            supplied = next((e for e in context['evidence'] if e['label'] == 'EMA(13)'), None)
            self.assertIsNotNone(supplied)
            self.assertEqual(supplied['raw_value'], 10.0)
            self.assertGreater(context['omitted_from_model'], 0)

    def test_new_price_wording_intentionally_adds_decision_and_program_plans(self):
        old, new = self.pair(['平安银行短线建仓价位'], [price_draft()])
        old, new = old[0], new[0]
        self.assertEqual(answer_context(old)['objective'], dict(kind='outlook', horizon='短线'))
        self.assertEqual(answer_context(new)['objective'], dict(kind='decision', horizon='未来10个交易日'))
        self.assertFalse(answer_context(old).get('trade_plans'))
        self.assertEqual(len(answer_context(new)['trade_plans']), 3)
        self.assertEqual(old['requests'][0][:2], new['requests'][0][:2])


if __name__ == '__main__':
    unittest.main()
