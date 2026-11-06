"""v2.6.8 prompt scope and existing recall/explanation authority boundaries."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

from evaluation_tools.research_intent_v264_request_lengths import measure_request
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests
from test_research_intent_v26 import previous_value
from trading_companion import research_intent_v2 as intent
from trading_companion.tool_model import ToolMessage


class V268Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_other_prompt_rules_are_byte_identical_and_both_size_limits_are_preserved(self):
        canonical = intent.INSTRUCTION.replace(intent.VERSION, 'research-intent-v2.6.7')
        start, end = canonical.index('recall复述'), canonical.index('recall、knowledge')
        unchanged = canonical[:start]+'<recall-boundary>'+canonical[end:]
        self.assertEqual(sha256(unchanged.encode('utf-8')).hexdigest(),
                         'b8f78cf91d6780086fd64565397dfc95ad03d92a5dd27b790e035ba29a342c68')
        self.assertIn('version='+intent.VERSION, intent.INSTRUCTION)
        self.assertLessEqual(len(intent.INSTRUCTION), 4014)
        self.assertLessEqual(len(json.dumps(ToolMessage('system', intent.INSTRUCTION).to_dict(), ensure_ascii=True)), 13785)
        measured = measure_request('平安银行'+'a'*1996, context())
        self.assertTrue(measured['valid'])
        self.assertLessEqual(measured['serialized_characters'], 19801)

    def test_recall_repeats_content_without_subject_authority_or_mutation(self):
        ctx = context(previous=True)
        value = previous_value(task='recall')
        before, state = deepcopy(value), dict(ctx.session.__dict__)
        result = self.check('把先前给的方向照着念一下', value, ctx)
        self.assertEqual((result.task, result.operation, result.objective_kind, result.wants_plan),
                         ('recall', 'new', 'analysis', False))
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome), ('none', (), 'local'))
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)

    def test_explanation_uses_checked_latest_research_without_new_plan_or_mutation(self):
        ctx, value = context(previous=True), previous_value(operation='explain')
        before, state = deepcopy(value), dict(ctx.session.__dict__)
        result = self.check('帮我讲透旧判断的论证', value, ctx)
        self.assertEqual((result.task, result.operation, result.objective_kind, result.wants_plan),
                         ('stock_research', 'explain', 'analysis', False))
        self.assertEqual((result.subject_source, result.bound_subjects, result.outcome),
                         ('previous', ('000001.SZ',), 'research'))
        self.assertEqual(result.diagnostics['reference']['scope'], 'latest_research')
        self.assertIsNone(result.diagnostics['authorization']['plan_source'])
        self.assertEqual(value, before)
        self.assertEqual(ctx.session.__dict__, state)

    def test_words_do_not_rewrite_task_or_operation_or_expand_local_grammar(self):
        ctx = context(previous=True)
        for question in ('帮我讲透旧判断的论证', '你为何作出先前那个判断', '请说明旧报告用了哪些依据'):
            self.assertIsNone(intent.local_preflight(question, ctx))
            # A mistaken model recall remains visible; this release only changes its prompt.
            value = draft(task='recall', subject=dict(source='none', mentions=[], selection=None))
            result = self.check(question, value, ctx)
            self.assertEqual((result.task, result.operation), ('recall', 'new'))
        result = self.check('沿用旧对象再作一个新判断', previous_value(operation='new'), ctx)
        self.assertEqual((result.task, result.operation), ('stock_research', 'new'))

    def test_explanation_cannot_recover_missing_or_unusable_latest_binding(self):
        base = context(previous=True)
        for ctx in (context(), context(previous=True, age=-1), replace(base, conversation_memory=False),
                    replace(base, source='akshare_eastmoney'), replace(base, development=True)):
            result = self.check('帮我讲透旧判断的论证', previous_value(operation='explain'), ctx)
            self.assertEqual((result.bound_subjects, result.wants_plan, result.outcome), ((), False, 'clarify_subject'))

    def test_explain_operation_still_revokes_new_price_authority(self):
        value = previous_value(operation='explain', objective_kind='decision', objective_phrase='给参考价',
            wants_plan=True, plan_phrase='给参考价', horizon_expression='中期')
        result = self.check('讲解旧结论，中期给参考价', value, context(previous=True))
        self.assertEqual(result.operation, 'explain')
        self.assertFalse(result.wants_plan)
        self.assertEqual(result.plan_blocked, 'explain_no_new_authorization')
