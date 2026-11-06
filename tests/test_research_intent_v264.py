"""Independent synthetic v2.6.4 conflict, diagnostic-only and refusal boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import unicodedata

from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests
from test_research_intent_v26 import pending_context, previous_value, reference
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue


def conflict_context():
    ctx = context(previous=True)
    ctx.session.product_pending = pending_context().session.product_pending
    ctx.session._pending = object()
    return ctx


def horizon_value(**changes):
    return draft(task='clarify', operation='update', clarification_reason='conditions',
        subject=dict(source='none', mentions=[], selection=None), horizon_expression='中期', **changes)


class V264Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_prompt_excludes_complete_questions_from_all_four_public_corpora(self):
        root = Path(__file__).resolve().parents[1]/'data/evaluation'
        corpora = (('research-intent-v2.jsonl', 240), ('research-intent-v2.5-dev.jsonl', 78),
                   ('research-intent-v2.5-holdout.jsonl', 96), ('research-intent-v2.6-dev.jsonl', 64))
        rows = []
        for name, count in corpora:
            public = [json.loads(line) for line in (root/name).read_text(encoding='utf-8').splitlines() if line.strip()]
            self.assertEqual(len(public), count, name)
            rows.extend(public)
        self.assertEqual(len(rows), 478)
        self.assertEqual(len({row['id'] for row in rows}), 478)
        overlaps = []
        for row in rows:
            question = row['question'].strip()
            while question and (question[-1].isspace() or unicodedata.category(question[-1]).startswith('P')):
                question = question[:-1]
            if len(question) >= 5 and question in intent.INSTRUCTION:
                overlaps.append((row['id'], question))
        self.assertEqual(overlaps, [], '提示词不能包含完整公开题干；只读取四份固定公开文件')

    def test_independent_stock_and_index_bypass_conflict_using_only_current_evidence(self):
        for name, task, code in (('浦发银行', 'stock_research', '600000.SH'),
                                  ('沪深300', 'index_research', '000300.SH')):
            ctx = conflict_context()
            state = dict(ctx.session.__dict__)
            value = draft(task=task, subject=dict(source='question', mentions=[name], selection=None),
                          research_kinds=[dict(kind='risk', phrase='风险')])
            before, log = deepcopy(value), []
            result = self.check('请单开研究'+name+'的风险', value, ctx, corrections=log)
            self.assertEqual((result.task, result.operation, result.subject_source, result.bound_subjects, result.outcome),
                             (task, 'new', 'question', (code,), 'research'))
            self.assertEqual((result.horizon, result.wants_plan, result.objective_kind), (None, False, 'analysis'))
            self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
            self.assertIsNone(result.diagnostics['authorization']['plan_source'])
            self.assertNotIn('pending_conflict_priority', [item['kind'] for item in log])
            self.assertEqual(value, before)
            self.assertEqual(ctx.session.__dict__, state)

    def test_new_price_request_keeps_only_its_own_horizon_and_price_authorization(self):
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='未来十二天', horizon_expression='未来十二天',
            wants_plan=True, plan_phrase='入场价')
        result = self.check('浦发银行未来十二天的入场价', value, conflict_context())
        self.assertEqual((result.horizon, result.wants_plan, result.outcome), ('未来12天', True, 'research'))
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'question')
        self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
        value['plan_phrase'] = '参考价'
        with self.assertRaises(InputIssue):
            self.check('浦发银行未来十二天的入场价', value, conflict_context())

    def test_update_refresh_explain_and_previous_references_do_not_bypass(self):
        ctx = conflict_context()
        for operation in ('update', 'refresh', 'explain'):
            value = draft(operation=operation, subject=dict(source='question', mentions=['浦发银行'], selection=None))
            result = self.check('请研究浦发银行', value, ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))
        for ref in (reference(), reference('ordinal', index=1), reference(scope='pending'),
                    reference('ordinal', scope='shortlist', index=1)):
            result = self.check('请继续这项研究', previous_value(ref), ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))

    def test_question_source_cannot_bypass_with_an_ordinal_latest_or_inconsistent_scope(self):
        for ref in (reference(), reference('ordinal', index=1), reference('ordinal', scope='shortlist', index=1),
                    reference('none', scope='pending'), reference('none', scope=None, index=1)):
            value = draft(reference=ref, subject=dict(source='question', mentions=['浦发银行'], selection=None))
            result = self.check('浦发银行的风险', value, conflict_context())
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))

    def test_unverified_or_omitted_identity_never_bypasses_a_conflict(self):
        ctx = conflict_context()
        for name, task in (('示例公司', 'stock_research'), ('示例成长指数', 'index_research')):
            value = draft(task=task, subject=dict(source='question', mentions=[name], selection=None))
            result = self.check('请研究'+name, value, ctx)
            self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conflict', ()))
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None))
        for question in ('研究浦发银行和平安银行', '只研究平安银行'):
            with self.assertRaises(InputIssue):
                self.check(question, value, ctx)
        value = draft(task='unsupported', subject=dict(source='question', mentions=['沪深300ETF'], selection=None))
        self.assertEqual(self.check('研究沪深300ETF', value, ctx).bound_subjects, ())

    def test_single_pending_horizon_normalization_never_recovers_binding_or_authority(self):
        base = pending_context()
        for ctx in (base, replace(base, now=base.now+timedelta(seconds=900)),
                    replace(base, now=base.now+timedelta(seconds=901)),
                    replace(base, now=base.now-timedelta(seconds=1)),
                    replace(base, source='akshare_eastmoney'), replace(base, conversation_memory=False)):
            value, log = horizon_value(), []
            state, before = dict(ctx.session.__dict__), deepcopy(value)
            result = self.check('我打算选中期', value, ctx, corrections=log)
            raw = self.check('我打算选中期', value, ctx, normalize=False)
            self.assertEqual((result.task, result.subject_source, result.bound_subjects, result.wants_plan),
                             ('clarify', 'previous', (), False))
            self.assertEqual(result.horizon, '未来40个交易日')
            self.assertEqual(raw.subject_source, 'none')
            self.assertEqual(result.outcome, raw.outcome)
            self.assertEqual(result.diagnostics['authorization'], raw.diagnostics['authorization'])
            self.assertFalse(result.diagnostics['authorization']['same_task_pending'])
            self.assertIn(('subject', 'previous'), result.provenance)
            self.assertIn('pending_horizon_source', [item['kind'] for item in log])
            self.assertEqual(value, before)
            self.assertEqual(ctx.session.__dict__, state)

    def test_no_record_multiple_records_new_subject_and_extra_fields_do_not_gain_previous(self):
        for ctx in (context(), context(previous=True), conflict_context()):
            result = self.check('我打算选中期', horizon_value(), ctx)
            self.assertEqual((result.subject_source, result.bound_subjects), ('none', ()))
        for changes, question in ((dict(indicator_mentions=['RSI']), '中期用RSI'),
                (dict(research_kinds=[dict(kind='risk', phrase='风险')]), '中期分析风险'),
                (dict(exclusions=[dict(kind='plan', phrase='不要价位')]), '中期不要价位'),
                (dict(date_expression='上个月'), '中期回看上个月'),
                (dict(strict_scope=True, scope_phrase='只看风险'), '中期只看风险')):
            self.assertEqual(self.check(question, horizon_value(**changes), pending_context()).subject_source, 'none')
        self.assertEqual(self.check('浦发银行，中期', horizon_value(), pending_context()).subject_source, 'none')
        value = horizon_value()
        value['horizon_expression'] = '未来5000天'
        self.assertEqual(self.check('未来5000天', value, pending_context()).subject_source, 'none')

    def test_known_unsupported_index_objective_is_neutral_only_in_normalized_view(self):
        for task in ('index_research', 'clarify', 'unsupported'):
            value = draft(task=task, objective_kind='outlook', objective_phrase='后续走向',
                clarification_reason='identity' if task == 'clarify' else None,
                subject=dict(source='question', mentions=['中证500'], selection=None))
            before, log = deepcopy(value), []
            result = self.check('中证500后续走向', value, corrections=log)
            raw = self.check('中证500后续走向', value, normalize=False)
            self.assertEqual((result.task, result.objective_kind, result.outcome, result.bound_subjects),
                             ('unsupported', 'analysis', 'reject', ()))
            self.assertEqual(raw.objective_kind, 'outlook')
            self.assertIn('unsupported_index_objective', [item['kind'] for item in log])
            self.assertEqual(value, before)

    def test_etf_unknown_index_and_stock_background_do_not_get_neutral_index_objective(self):
        for name, task in (('中证500ETF', 'unsupported'), ('示例成长指数', 'index_research'),
                           ('浦发银行', 'stock_research')):
            value = draft(task=task, objective_kind='outlook', objective_phrase='后续走向',
                subject=dict(source='question', mentions=[name], selection=None))
            log = []
            result = self.check(name+'后续走向', value, corrections=log)
            self.assertNotEqual(result.objective_kind, 'analysis')
            self.assertNotIn('unsupported_index_objective', [item['kind'] for item in log])
        value = draft(task='knowledge', subject=dict(source='none', mentions=[], selection=None))
        self.assertEqual(self.check('中证500是什么', value).outcome, 'local')

    def test_price_veto_and_full_indicator_parameters_survive_candidate_checking(self):
        value = draft(subject=dict(source='question', mentions=['浦发银行'], selection=None),
            objective_kind='decision', objective_phrase='适合加仓么', wants_plan=True, plan_phrase='适合加仓么')
        result = self.check('浦发银行此时适合加仓么', value)
        self.assertEqual((result.wants_plan, result.outcome), (True, 'clarify_horizon'))
        value.update(wants_plan=False, plan_phrase=None, exclusions=[dict(kind='plan', phrase='别给价位')])
        result = self.check('浦发银行此时适合加仓么，但别给价位', value)
        self.assertEqual((result.wants_plan, result.exclusions), (False, ('plan',)))
        value = draft(task='index_research', subject=dict(source='question', mentions=['沪深300'], selection=None),
            indicator_mentions=['SMA3', '均线'], research_kinds=[dict(kind='technical', phrase='均线')])
        self.assertEqual(self.check('沪深300用SMA3与均线研究', value).indicator_mentions, ('SMA3', '均线'))
