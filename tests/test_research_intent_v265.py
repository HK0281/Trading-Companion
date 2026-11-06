"""Independent v2.6.5 revocation, goal, task and diagnostic boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from evaluation_tools import research_intent_v26_evaluation as evaluation
from evaluation_tools.research_intent_v264_request_lengths import measure_request
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from test_research_intent_v26 import pending_context, previous_value, reference
from test_research_intent_v264 import conflict_context
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.product_agent import SavedProductResearch
from trading_companion.product_plan import ProductPlan
from trading_companion.research_objective import ResearchObjective
from trading_companion.workflow_evaluation_fixtures import SECURITIES


def published_context(original='平安银行未来二十天能建仓吗'):
    ctx = context()
    plan = ProductPlan(status='ready', source='tencent', securities=SECURITIES[:1], kinds=('technical',),
        start_date=ctx.reference_date-timedelta(days=100), end_date=ctx.reference_date-timedelta(days=1),
        objective=ResearchObjective('decision', '未来20天'), user_question=original)
    ctx.session.product_saved = SavedProductResearch(ctx.now, plan, None, None)
    ctx.session.stage_research_subject(plan.securities, kind='company', source='tencent',
        development=False, now=ctx.now, scope=plan)
    return replace(ctx, history=(original,))


class V265Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_repeated_research_kinds_keep_first_grounded_phrase_and_raw_is_unchanged(self):
        for second in ('日线', '技术面'):
            value = draft(research_kinds=[dict(kind='technical', phrase='日线'),
                dict(kind='technical', phrase=second), dict(kind='risk', phrase='风险')])
            before, log = deepcopy(value), []
            result = self.check('平安银行日线技术面与风险分析', value, corrections=log)
            self.assertEqual(result.research_kinds, ('technical', 'risk'))
            correction, = log
            self.assertEqual(correction['kind'], 'research_kinds_deduplicated')
            self.assertEqual(correction['after'], [value['research_kinds'][0], value['research_kinds'][2]])
            self.assertEqual(value, before)
            with self.assertRaises(InputIssue):
                self.check('平安银行日线技术面与风险分析', value, normalize=False)

    def test_deduplication_cannot_hide_invalid_phrases_items_or_exclusions(self):
        first = dict(kind='technical', phrase='技术面')
        for second in (dict(kind='technical', phrase='不存在的原文'),
                       dict(kind='unlisted', phrase='技术面'), dict(first, extra=True)):
            with self.assertRaises(InputIssue):
                self.check('平安银行技术面', draft(research_kinds=[first, second]))
        with self.assertRaises(InputIssue):
            self.check('平安银行不要风险', draft(exclusions=[dict(kind='risk', phrase='不要风险')]*2))
        with self.assertRaises(InputIssue):
            self.check('平安银行技术面', draft(research_kinds=[first]*7))

    def test_published_original_evidence_is_revoked_with_no_history_authority_or_mutation(self):
        ctx, log = published_context(), []
        state = dict(ctx.session.__dict__)
        value = previous_value(operation='update', objective_kind='decision', objective_phrase='未来二十天能建仓吗',
            wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来二十天', indicator_mentions=['RSI'])
        before = deepcopy(value)
        result = self.check('同一对象添加RSI', value, ctx, corrections=log)
        self.assertEqual((result.objective_kind, result.wants_plan, result.horizon), ('analysis', False, None))
        self.assertEqual((result.bound_subjects, result.outcome), (('000001.SZ',), 'research'))
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])
        self.assertIn('published_evidence_revoked', [item['kind'] for item in log])
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)
        with self.assertRaises(InputIssue):
            self.check('同一对象添加RSI', value, ctx, normalize=False)

    def test_current_horizon_survives_old_goal_revocation_but_does_not_authorize_prices(self):
        value = previous_value(operation='update', objective_kind='decision', objective_phrase='能建仓吗',
            wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来十三天')
        result = self.check('同一对象未来十三天', value, published_context())
        self.assertEqual((result.horizon, result.objective_kind, result.wants_plan), ('未来13天', 'decision', False))

    def test_chat_only_stale_mismatched_or_consumed_publications_cannot_sanitize_evidence(self):
        native = published_context()
        chat = replace(context(previous=True), history=native.history)
        mismatch = published_context()
        mismatch.session.product_saved = replace(mismatch.session.product_saved,
            plan=replace(mismatch.session.product_saved.plan, user_question='别的原始研究'))
        consumed = published_context()
        consumed.session._subject_current = False
        for ctx in (chat, mismatch, consumed, replace(native, conversation_memory=False),
                    replace(native, now=native.now+timedelta(seconds=900)),
                    replace(native, now=native.now-timedelta(seconds=1)),
                    replace(native, source='akshare_eastmoney')):
            with self.subTest(ctx=ctx), self.assertRaises(InputIssue):
                self.check('同一对象添加RSI', previous_value(operation='update', indicator_mentions=['RSI'],
                    objective_kind='decision', objective_phrase='能建仓吗', wants_plan=True,
                    plan_phrase='能建仓吗', horizon_expression='未来二十天'), ctx)

    def test_invented_objective_plan_horizon_and_date_remain_invalid(self):
        for key in ('objective_phrase', 'plan_phrase', 'horizon_expression', 'date_expression'):
            value = previous_value(operation='update', objective_kind='decision', objective_phrase='能建仓吗',
                wants_plan=True, plan_phrase='能建仓吗', horizon_expression='未来二十天')
            value[key] = '虚构的新依据'
            with self.subTest(key=key), self.assertRaises(InputIssue):
                self.check('同一对象添加RSI', dict(value, indicator_mentions=['RSI']), published_context())

    def test_registered_pending_authorization_is_preserved_even_with_a_matching_publication(self):
        ctx = pending_context()
        published = published_context('平安银行给参考价')
        ctx.session.product_saved = published.session.product_saved
        ctx.session.stage_research_subject(published.session.product_saved.plan.securities, kind='company',
            source='tencent', development=False, now=ctx.now, scope=published.session.product_saved.plan)
        value = previous_value(reference(scope='pending'), operation='update', objective_kind='decision',
            objective_phrase='给参考价', wants_plan=True, plan_phrase='给参考价', horizon_expression='未来十三天')
        result = self.check('未来十三天', value, ctx)
        self.assertTrue(result.wants_plan)
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'program_pending')

    def test_price_goal_uses_extracted_evidence_before_unused_phrase_cleanup_and_never_grants(self):
        for task, mention in (('stock_research', '平安银行'), ('index_research', '沪深300')):
            for field in ('plan_phrase', 'objective_phrase'):
                value = draft(task=task, subject=dict(source='question', mentions=[mention], selection=None),
                    **{field: '给入场价'})
                before, log = deepcopy(value), []
                result = self.check(mention+'给入场价，但别给价位', value, corrections=log)
                self.assertEqual((result.objective_kind, result.wants_plan), ('decision', False))
                self.assertIsNone(result.diagnostics['authorization']['plan_source'])
                self.assertTrue(result.diagnostics['authorization']['plan_veto'])
                self.assertIn('objective_price_request', [item['kind'] for item in log])
                self.assertEqual(value, before)
                raw = self.check(mention+'给入场价，但别给价位', value, normalize=False)
                self.assertEqual(raw.objective_kind, 'analysis')

    def test_pure_negative_rating_direction_and_absent_price_fragments_do_not_change_goal(self):
        for phrase in ('不要价位', '不需要计划', '别给止损位', '不做交易计划', '没有价位',
                       '给个买卖评级', '退出评级', '说出方向'):
            for field in ('objective_phrase', 'plan_phrase'):
                result = self.check('平安银行'+phrase, draft(**{field: phrase}))
                self.assertEqual((result.objective_kind, result.wants_plan), ('analysis', False))
        with self.assertRaises(InputIssue):
            self.check('平安银行说明风险', draft(plan_phrase='给入场价'))

    def test_index_price_goal_with_current_horizon_remains_decision_when_prices_are_vetoed(self):
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
            plan_phrase='给退出点位', horizon_expression='未来十三天', wants_plan=False)
        result = self.check('沪深300未来十三天，给退出点位，但别给价位', value)
        self.assertEqual((result.objective_kind, result.horizon, result.wants_plan), ('decision', '未来13天', False))
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])

    def test_price_goal_does_not_change_explicit_permission_or_missing_identity(self):
        value = draft(task='clarify', clarification_reason='subject',
            subject=dict(source='none', mentions=[], selection=None), wants_plan=True, plan_phrase='给止损参考位')
        result = self.check('给止损参考位', value)
        self.assertEqual((result.task, result.objective_kind, result.wants_plan, result.bound_subjects, result.outcome),
                         ('clarify', 'decision', True, (), 'clarify_subject'))

    def test_recall_knowledge_and_screening_do_not_acquire_decision_objectives(self):
        for task in ('recall', 'knowledge', 'screening'):
            value = draft(task=task, objective_phrase='给过的参考价',
                subject=dict(source='none', mentions=[], selection=None))
            result = self.check('解释给过的参考价', value)
            self.assertEqual((result.objective_kind, result.wants_plan), ('analysis', False))

    def test_unknown_index_keeps_category_without_binding_and_known_alias_resolves_identity_only(self):
        for name, code in (('示例创新指数', None), ('大盘指数', '000001.SH')):
            value = draft(task='clarify', confidence='low', clarification_reason='identity',
                subject=dict(source='question', mentions=[name], selection=None))
            log = []
            result = self.check('考察'+name+'当前走势', value, corrections=log)
            self.assertEqual(result.task, 'index_research')
            self.assertEqual(result.bound_subjects, () if code is None else (code,))
            self.assertEqual(result.outcome, 'clarify_identity' if code is None else 'research')
            self.assertFalse(result.wants_plan)
            self.assertTrue(result.low_confidence)  # The model's original uncertainty remains reported.
        value['clarification_reason'] = 'conditions'
        result = self.check('考察大盘指数当前走势', value)
        self.assertEqual(result.outcome, 'clarify_conditions')

    def test_conflicting_previous_research_keeps_task_without_fixing_latest_or_granting(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        for ref, expected in ((reference('all'), tuple(s.instrument_id for s in SECURITIES[:2])),
                              (reference(), ())):
            value = previous_value(ref, task='clarify', clarification_reason='conflict', operation='update',
                research_kinds=[dict(kind='financial', phrase='财务')],
                exclusions=[dict(kind='financial', phrase='不看财务')])
            result = self.check('原对象只看财务但是不看财务', value, ctx)
            self.assertEqual((result.task, result.outcome, result.bound_subjects),
                             ('stock_research', 'clarify_conflict', expected))
            self.assertEqual(result.diagnostics['reference'], ref)
            self.assertFalse(result.wants_plan)
        result = self.check('继续', previous_value(task='clarify', clarification_reason='conflict'), conflict_context())
        self.assertEqual((result.task, result.bound_subjects), ('clarify', ()))

    def test_complete_product_mention_rejects_without_binding_underlying_stock_or_index(self):
        for name in ('那只债券基金', '沪深300ETF'):
            value = draft(task='unsupported', subject=dict(source='question', mentions=[name], selection=None))
            result = self.check('考察'+name+'当前表现', value)
            self.assertEqual((result.task, result.bound_subjects, result.outcome), ('unsupported', (), 'reject'))
        with self.assertRaises(InputIssue):
            self.check('考察沪深300ETF当前表现', draft(task='index_research',
                subject=dict(source='question', mentions=['沪深300'], selection=None)))

    def test_local_binding_proofs_use_validated_pending_and_saved_snapshot_without_session_changes(self):
        for ctx, question, scope in ((pending_context(), '中线', 'pending'),
                                      (published_context(), '查看上次结果', 'checked_saved_result')):
            state = dict(ctx.session.__dict__)
            checked = intent.local_preflight(question, ctx)
            proof = checked.diagnostics['binding']
            self.assertEqual(proof['scope'], scope)
            self.assertIsNone(proof['issue'])
            self.assertEqual(proof['scope_subjects'], list(checked.bound_subjects))
            self.assertEqual(ctx.session.__dict__, state)
        ctx = published_context()
        stale = intent.local_preflight('查看上次结果', replace(ctx, now=ctx.now+timedelta(seconds=900)))
        self.assertEqual(stale.bound_subjects, ())
        self.assertEqual(stale.diagnostics['binding']['scope_subjects'], [])
        self.assertIsNotNone(stale.diagnostics['binding']['issue'])
        self.assertIsNotNone(ctx.session.product_saved)

    def test_missing_proof_is_listed_without_becoming_measured_or_a_violation(self):
        row = sample()
        row['gold'].update(subject_source='previous', wants_plan=True)
        result = evaluation.redlines([dict(row, V2=evaluation._empty('not_measured', 'context_unavailable'))])
        self.assertTrue(result['passed'])
        for name in ('reference_scope', 'plan_authorization'):
            check = result['checks'][name]
            self.assertEqual((check['measured'], check['unmeasured'], check['violations']), (0, 1, 0))
            self.assertEqual(check['status'], 'unmeasured')
            self.assertEqual(check['unmeasured_cases'], [dict(id=row['id'], status='not_measured', reason='context_unavailable')])

    def test_full_length_target_uses_unchanged_shared_limit_and_no_model(self):
        question = '平安银行'+'a'*1996
        self.assertEqual(len(question), 2000)
        result = measure_request(question, context())
        self.assertTrue(result['valid'])
        self.assertGreaterEqual(result['headroom'], 0)
        self.assertEqual(result['headroom'], 20000-result['serialized_characters'])
