"""Independent v2.6.7 invalid-pending diagnosis and entry-decision boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

from evaluation_tools.research_intent_v264_request_lengths import measure_request
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests
from test_research_intent_v26 import pending_context
from test_research_intent_v264 import conflict_context
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.tool_model import ToolMessage


def expired_context():
    ctx = pending_context()
    return replace(ctx, now=ctx.now+timedelta(minutes=20))


def horizon_value(**changes):
    return draft(task='clarify', operation='update', objective_kind='analysis', clarification_reason='conditions',
        horizon_expression='中期', subject=dict(source='previous', mentions=[], selection=None,
            reference=dict(kind='none', index=None, scope=None)), **changes)


class V267Tests(OfflineTests):
    def check(self, question, value, ctx=None, **options):
        return intent.check_response(question, response(value), context=ctx or context(), **options)

    def test_model_conditions_survive_invalid_reference_without_binding_or_mutation(self):
        for change in (dict(now=pending_context().now+timedelta(minutes=20)),
                dict(now=pending_context().now-timedelta(seconds=1)), dict(source='akshare_eastmoney'),
                dict(development=True), dict(conversation_memory=False)):
            ctx = replace(pending_context(), **change)
            state, value, log = dict(ctx.session.__dict__), horizon_value(), []
            original = deepcopy(value)
            result = self.check('我的期限取中期', value, ctx, corrections=log)
            self.assertEqual((result.task, result.operation, result.outcome), ('clarify', 'update', 'clarify_conditions'))
            self.assertEqual((result.bound_subjects, result.wants_plan, result.horizon), ((), False, '未来40个交易日'))
            self.assertEqual(result.diagnostics['binding']['issue'], 'clarify_subject')
            self.assertIsNone(result.diagnostics['authorization']['plan_source'])
            self.assertIn('reference_unbound', [c['kind'] for c in log])
            self.assertIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
            self.assertEqual(value, original)
            self.assertEqual(ctx.session.__dict__, state)
            self.assertEqual(self.check('我的期限取中期', value, ctx, normalize=False).outcome, 'clarify_subject')

    def test_absent_valid_conflicting_or_wrong_stage_pending_cannot_use_priority_repair(self):
        wrong_stage = pending_context()
        wrong_stage.session.product_pending = replace(wrong_stage.session.product_pending, stage='identity')
        for ctx in (context(), pending_context(), conflict_context(), wrong_stage):
            log = []
            result = self.check('我的期限取中期', horizon_value(), ctx, corrections=log)
            self.assertEqual(result.bound_subjects, ())
            self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
            if ctx is not wrong_stage:
                self.assertNotEqual(result.outcome, 'clarify_conditions')

    def test_model_subject_diagnosis_and_unrelated_scope_or_selection_stay_unrepaired(self):
        base = horizon_value()
        changes = (dict(operation='new'), dict(clarification_reason='subject'), dict(horizon_expression=None),
            dict(horizon_expression='以后'), dict(objective_phrase='期限'),
            dict(subject=dict(source='previous', mentions=[], selection='它', reference=dict(kind='none', index=None, scope=None))),
            dict(subject=dict(source='previous', mentions=[], selection=None, reference=dict(kind='latest', index=None, scope='latest_research'))),
            dict(subject=dict(source='previous', mentions=[], selection=None, reference=dict(kind='none', index=1, scope=None))))
        for change in changes:
            log = []
            result = self.check('它的期限是中期，以后再说', dict(base, **change), expired_context(), corrections=log)
            self.assertEqual(result.outcome, 'clarify_subject')
            self.assertEqual(result.bound_subjects, ())
            self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])
        with self.assertRaises(InputIssue):
            self.check('没有期限片段', base, expired_context())

    def test_explicit_pending_scope_keeps_existing_conditions_without_new_repair(self):
        value = horizon_value()
        value['subject']['reference'] = dict(kind='latest', index=None, scope='pending')
        log = []
        result = self.check('我的期限取中期', value, expired_context(), corrections=log)
        self.assertEqual((result.outcome, result.bound_subjects), ('clarify_conditions', ()))
        self.assertNotIn('invalid_pending_horizon_priority', [c['kind'] for c in log])

    def test_entry_question_accepts_its_literal_marker_but_not_ratings_or_fabrication(self):
        phrase = '此时是否适宜介入'
        value = draft(objective_kind='decision', objective_phrase=phrase, wants_plan=True, plan_phrase=phrase)
        result = self.check('研究平安银行，'+phrase, value)
        self.assertTrue(result.wants_plan)
        self.assertEqual(result.outcome, 'clarify_horizon')
        self.assertEqual(result.diagnostics['authorization']['plan_source'], 'question')
        self.assertFalse(self.check('研究平安银行，'+phrase, dict(value, wants_plan=False, plan_phrase=None)).wants_plan)
        for phrase in ('给个介入评级', '说说介入方向'):
            with self.assertRaises(InputIssue):
                self.check('平安银行'+phrase, dict(value, objective_phrase=phrase, plan_phrase=phrase))
        with self.assertRaises(InputIssue):
            self.check('研究平安银行', value)

    def test_plan_cancellation_and_entry_veto_survive_even_a_positive_request(self):
        for negative in ('介入取消', '不要介入', '计划免了', '删去计划', '不用计划',
                         '不要价位', '不需要止损', '不必给参考价'):
            question = '平安银行'+negative+'，但给我入场参考价'
            value = draft(objective_kind='decision', objective_phrase='给我入场参考价', wants_plan=True,
                plan_phrase='给我入场参考价', exclusions=[dict(kind='plan', phrase=negative)])
            result = self.check(question, value)
            self.assertEqual((result.wants_plan, result.outcome, result.exclusions), (False, 'clarify_conflict', ('plan',)))
            self.assertTrue(result.diagnostics['authorization']['plan_veto'])
            if negative in ('介入取消', '不要介入'):
                without_exclusion = self.check(question, dict(value, exclusions=[]))
                self.assertFalse(without_exclusion.wants_plan)

    def test_prompt_and_full_target_fit_both_v266_size_measures_and_keep_current_version(self):
        self.assertIn('version='+intent.VERSION, intent.INSTRUCTION)
        self.assertLessEqual(len(intent.INSTRUCTION), 4014)
        self.assertLessEqual(len(json.dumps(ToolMessage('system', intent.INSTRUCTION).to_dict(), ensure_ascii=True)), 13980)
        measured = measure_request('平安银行'+'a'*1996, context())
        self.assertTrue(measured['valid'])
        self.assertLessEqual(measured['serialized_characters'], 19996)
