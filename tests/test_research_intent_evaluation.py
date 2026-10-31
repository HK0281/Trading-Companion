"""Independent small examples test accounting and real entry points; not corpus scores."""

from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_research_intents as command
from trading_companion import research_intent_evaluation as evaluation
from trading_companion.research_intent_evaluation_baseline import rule_observation, UNKNOWN
from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS
from trading_companion.research_intent_evaluation_fixtures import fixed_execution


def context(**changes):
    value = dict(has_company=False, has_technical=False, has_index=False, company_enabled=True, workflow_enabled=True,
        development=False, conversation_memory=True, latest=None, source='tencent', pending='none',
        pending_age_seconds=None, pending_binding=None, shortlist=[], history=[])
    value.update(changes)
    return value


def step(question, phases, mentions=None):
    script = dict(phases=phases)
    if mentions is not None:
        script.update(mentions=mentions, kinds=['technical'])
    return dict(question=question, script=script)


def probe(question, phases=None, prefix=None, state=None):
    state = state or context()
    steps = [*(prefix or []), step(question, phases or [], ['平安银行'] if 'company_intent' in (phases or []) else None)]
    return fixed_execution(dict(question=question, context=state), dict(steps=steps, unmeasured_reason=''))


class RuleTests(unittest.TestCase):
    def test_actual_router_called_and_missing_fields_not_fabricated(self):
        from trading_companion import request_routing
        with patch.object(request_routing, 'route_research_request', wraps=request_routing.route_research_request) as route:
            result = rule_observation(dict(question='浦发银行的基本面如何', context=context()))
        route.assert_called_once()
        self.assertEqual(result['fields']['task'], 'stock_research')
        self.assertEqual(result['fields']['bound_subjects'], UNKNOWN)
        self.assertEqual(result['fields']['outcome'], UNKNOWN)
        with self.assertRaises(ValueError):
            rule_observation(dict(question='x', context=context(), gold='poison'))

    def test_direction_and_veto_keep_raw_rules_even_when_semantic_labels_differ(self):
        result = rule_observation(dict(question='平安银行未来走势如何', context=context()))
        self.assertTrue(result['raw']['wants_decision'])
        self.assertEqual(result['fields']['objective_kind'], 'decision')
        self.assertFalse(result['fields']['wants_plan'])
        veto = rule_observation(dict(question='平安银行能买吗，不要计划', context=context()))
        self.assertFalse(veto['fields']['wants_plan'])
        self.assertTrue(veto['raw']['excludes_plan'])

    def test_technical_and_company_routes_share_one_task_without_rewriting_raw_intent(self):
        company = rule_observation(dict(question='浦发银行的基本面如何', context=context()))
        technical = rule_observation(dict(question='浦发银行只看技术面', context=context()))
        self.assertEqual(company['fields']['task'], technical['fields']['task'])
        self.assertEqual(technical['fields']['task'], 'stock_research')
        self.assertNotEqual(company['raw']['intent'], technical['raw']['intent'])

    def test_old_current_subject_identity_is_not_expired_with_cached_research(self):
        from datetime import timedelta
        from trading_companion.research_session import ResearchSession
        from trading_companion.routing_context import checked_subject
        from trading_companion.workflow_evaluation_fixtures import SECURITIES, NOW, REFERENCE_DATE
        from trading_companion.security_directory import SecurityDirectory
        session = ResearchSession()
        session.stage_research_subject(SECURITIES[:1], kind='company', source='tencent', development=False,
                                       now=NOW - timedelta(seconds=901))
        directory = SecurityDirectory(SECURITIES, 'review-fixed-directory', NOW, False)
        subjects, issue = checked_subject(session, directory=directory, reference_date=REFERENCE_DATE,
                                          development=False, now=NOW)
        self.assertIsNone(issue)
        self.assertEqual(subjects, SECURITIES[:1])


class ExecutionTests(unittest.TestCase):
    def test_failed_target_retains_unexpected_model_attempt_without_fabricating_success(self):
        result = probe('什么是市净率', [])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'research_internal_error')
        self.assertEqual(result['failed_step']['model_calls'], 1)
        self.assertEqual(result['failed_step']['intent_calls'], 0)
        self.assertEqual(result['failed_step']['error_type'], 'AssertionError')
        self.assertTrue(result['failed_step']['fixture_error'])
        self.assertEqual(result['fields'], {})
        self.assertNotIn('traceback', json.dumps(result))

    def test_new_protocol_plan_is_preparation_without_predictions_or_live_model(self):
        plan = evaluation.build_v2_plan()
        self.assertEqual(plan['requested_model'], 'deepseek-flash')
        self.assertEqual(plan['max_attempts_per_target'], 1)
        self.assertEqual(plan['paid_calls'], 0)
        self.assertEqual(plan['prediction_rows'], 0)
        self.assertFalse(plan['live_enabled'])
        self.assertFalse(plan['eligible_for_switch'])
        self.assertEqual(len(plan['tool_sha256']), 64)

    def test_network_attempt_is_blocked_even_if_executor_swallows_it(self):
        import socket
        def swallowed(*args):
            try:
                socket.getaddrinfo('offline-test.invalid', 443)
            except Exception:
                pass
            return dict(status='ok', fields={})
        with patch('trading_companion.research_intent_evaluation_fixtures._fixed_execution', side_effect=swallowed):
            self.assertEqual(probe('取消')['status'], 'failed')

    def test_local_credential_file_reads_are_forbidden(self):
        from trading_companion import local_env
        with patch('trading_companion.research_intent_evaluation_fixtures._fixed_execution',
                   side_effect=lambda *args: local_env._read_local_values()):
            self.assertEqual(probe('取消')['status'], 'failed')

    def previous(self, question, kind='technical', horizon=None):
        return context(has_company=True, history=[question], latest=dict(kind=kind, subjects=['000001.SZ'],
            age_seconds=0, source='tencent', development=False, current=True, horizon=horizon))

    def test_trusted_previous_subject_is_bound_by_real_executor(self):
        first = '分析平安银行的技术面'
        result = probe('它短线能买吗', ['company_intent', 'company_answer'],
            prefix=[step(first, ['company_intent', 'company_answer'], ['平安银行'])], state=self.previous(first))
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['fields']['bound_subjects'], ['000001.SZ'])
        self.assertEqual(result['fields']['horizon'], '未来10个交易日')

    def test_public_entry_failure_is_reproduced_without_repairing_keywords(self):
        first = '分析平安银行最近的走势，关注趋势与动量'
        follow = '如果我近期考虑建仓 能否给出几个价位'
        fixed = dict(steps=[step(first, ['company_intent', 'company_answer'], ['平安银行']),
                           step(follow, ['technical_intent'], ['平安银行'])], unmeasured_reason='')
        result = fixed_execution(dict(question=follow, context=self.previous(first, 'company')), fixed)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['steps'][-1]['code'], 'ungrounded_intent')
        self.assertEqual(result['fields']['outcome'], 'clarify_subject')

    def test_recall_uses_program_memory_with_zero_model_calls(self):
        first = '平安银行短线能买吗'
        result = probe('你刚才给的评级是什么', [],
            prefix=[step(first, ['company_intent', 'company_answer'], ['平安银行'])], state=self.previous(first, 'company', '未来10个交易日'))
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['steps'][-1]['model_calls'], 0)
        self.assertEqual(result['fields']['outcome'], 'local')

    def test_formal_session_asks_horizon_then_button_needs_no_understanding_call(self):
        first = '平安银行能买吗'
        pending = context(pending='company_horizon', pending_age_seconds=0,
            pending_binding=dict(session_matches=True, turn_matches=True), history=[first])
        result = probe('短线', ['company_answer'], prefix=[step(first, ['company_intent'], ['平安银行'])], state=pending)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['steps'][0]['outcome'], 'clarify_horizon')
        final = result['steps'][-1]
        self.assertEqual(final['bound_subjects'], ['000001.SZ'])
        self.assertEqual(final['horizon'], '未来10个交易日')
        self.assertEqual(final['intent_calls'], 0)
        self.assertTrue(final['trade_plans'])

    def test_no_plan_and_supported_atr_exclusion_observe_program_state(self):
        for question, indicator in (('平安银行短线能买吗，不要计划', None), ('平安银行短线能买吗，不用ATR', 'atr'),
                                     ('平安银行短线能买吗，只看SMA3', 'atr')):
            with self.subTest(question=question):
                result = probe(question, ['company_intent', 'company_answer'])
                self.assertEqual(result['status'], 'ok', result)
                final = result['steps'][-1]
                if indicator is None:
                    self.assertTrue(all(p['status'] == 'unavailable' and p['values'] is None for p in final['trade_plans']))
                else:
                    self.assertNotIn(indicator, final['indicators'])

    def test_existing_scope_wording_failures_are_counted_without_changing_rules(self):
        result = probe('平安银行短线能买吗，只用SMA3', ['company_intent', 'company_answer'])
        row = dict(B=result, gold=dict(exclusions=[], zero_intent_calls=False, bound_subjects=['000001.SZ'],
            strict_scope=True, indicator_mentions=['SMA3'], research_kinds=['technical']))
        self.assertTrue(evaluation.safety_checks(row)['strict_scope'])

    def test_unscripted_and_wrong_precondition_are_unmeasured(self):
        state = dict(question='继续', context=context())
        self.assertEqual(fixed_execution(state, dict(steps=[], unmeasured_reason='未覆盖'))['status'], 'not_measured')
        changed = context(latest=dict(kind='company', subjects=['000001.SZ'], age_seconds=0, source='tencent',
            development=False, current=True, horizon=None), has_company=True)
        result = probe('取消', state=changed)
        self.assertEqual(result['status'], 'not_measured')

    def test_unexpected_call_is_failed_not_correct_clarification(self):
        result = probe('平安银行的技术面如何', [])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['fields'], {})


class AccountingTests(unittest.TestCase):
    def sample(self, status='ok'):
        gold = dict.fromkeys(GOLD_FIELDS, None)
        gold.update(task='stock_research', operation='new', objective_kind='decision', wants_plan=True,
            subject_source='question', bound_subjects=['000001.SZ'], indicator_mentions=[], exclusions=[],
            research_kinds=[], strict_scope=False, outcome='research', zero_intent_calls=False)
        return dict(gold=gold, fault=None, A=dict(status=status, fields=deepcopy(gold)), B=dict(status='not_measured', fields={}, steps=[]),
                    context=dict(history=[]), tags=[], source=dict(kind='synthetic'))

    def test_missing_failed_and_unrun_stay_in_denominator(self):
        good, failed, missing, unrun = self.sample(), self.sample('failed'), self.sample(), self.sample('not_run')
        missing['A']['fields']['bound_subjects'] = UNKNOWN
        result = evaluation.field_scores([good, failed, missing, unrun], 'A')
        self.assertEqual(result['joint_all_fields'], dict(numerator=1, denominator=4, value=.25))
        self.assertEqual(result['fields']['bound_subjects']['measured'], 1)
        report = evaluation.metrics([good, failed, missing, unrun])
        self.assertFalse(report['eligible_for_switch'])
        self.assertEqual(report['zero_tolerance']['cross_object']['unmeasured'], 4)

    def test_failed_clarification_is_not_counted_correct(self):
        row = self.sample('failed')
        row['gold']['outcome'] = row['A']['fields']['outcome'] = 'clarify_subject'
        self.assertEqual(evaluation.field_scores([row], 'A')['correct_clarification']['numerator'], 0)

    def test_fault_metadata_does_not_penalize_correct_semantics_or_score_as_normal_execution(self):
        row = self.sample()
        row.update(id='independent-fault', fault='timeout')
        result = evaluation.metrics([row])
        self.assertEqual(result['layers']['A']['semantic']['joint_all_fields']['numerator'], 1)
        self.assertEqual(result['layers']['B']['all']['joint_all_fields']['denominator'], 0)
        self.assertEqual(result['fault_handling'][0]['status'], 'not_measured')
        self.assertFalse(result['fault_handling'][0]['expected']['allow_research'])
        self.assertFalse(result['fault_handling'][0]['expected']['allow_plan'])

    def test_no_paid_or_holdout_cli_and_no_draft_artifacts(self):
        for args in (['--split', 'holdout'], ['--mode', 'live']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                command.main(args)
        rows, manifest = evaluation.load_corpus()
        draft = deepcopy(manifest)
        draft.update(annotation_status='draft', reviewer=None, reviewed_at=None)
        with TemporaryDirectory() as folder, redirect_stdout(StringIO()), \
                patch.object(evaluation, 'load_corpus', return_value=(rows, draft)):
            self.assertEqual(command.main(['--report-dir', folder]), 2)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_runner_counts_adapter_errors_in_independent_sample(self):
        row = self.sample()
        row.update(id='example', family_id='independent', question='独立样例', split='dev', probe=dict(steps=[]))
        manifest = dict(annotation_status='reviewed', corpus_sha256='example', split_sha256='example', baseline={})
        with patch.object(evaluation, 'load_corpus', return_value=([row], manifest)), \
                patch.object(evaluation, 'rule_observation', side_effect=TimeoutError), \
                patch.object(evaluation, 'fixed_execution', return_value=row['B']):
            report = evaluation.evaluate()
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['metrics']['layers']['A']['all']['joint_all_fields']['denominator'], 1)
        self.assertEqual(report['metrics']['layers']['A']['all']['joint_all_fields']['numerator'], 0)

    def test_script_entry_point_loads_offline_guards_without_root_on_pythonpath(self):
        row = self.sample()
        row.update(id='independent-cancel', family_id='independent', split='dev', question='取消',
                   context=context(), tags=['local'], probe=dict(steps=[step('取消', [])], unmeasured_reason=''))
        script = Path(__file__).resolve().parents[1] / 'scripts/evaluate_research_intents.py'
        bootstrap = '''
import json, runpy, sys
from pathlib import Path
script = Path(sys.argv[1])
root = script.parent.parent
# Reproduce direct script startup without the repository root provided by a test runner.
sys.path = [str(script.parent), str(root / 'src')] + [p for p in sys.path if p and Path(p).resolve() != root]
namespace = runpy.run_path(str(script))
from trading_companion import research_intent_evaluation as evaluation
row = json.load(sys.stdin)
manifest = dict(annotation_status='reviewed', corpus_sha256='independent', split_sha256='independent', baseline={})
evaluation.load_corpus = lambda: ([row], manifest)
raise SystemExit(namespace['main'](['--split', 'dev']))
'''
        environment = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        with TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', bootstrap, str(script)],
                input=json.dumps(row), cwd=folder, env=environment, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        execution = json.loads(result.stdout)['rows'][0]['B']
        self.assertEqual(execution['status'], 'ok', execution)
        self.assertEqual(execution['fields']['outcome'], 'local')
        self.assertTrue(execution['fields']['zero_intent_calls'])


if __name__ == '__main__':
    unittest.main()
