"""Offline public-only admission, metering and reporting for the v2.6 candidate."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from evaluation_tools import evaluate_research_intents_v26 as command
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from trading_companion.model import ModelCallError, ModelConfigurationError


class Model:
    def __init__(self, fail_first=False):
        self.messages = []
        self.fail_first = fail_first

    def complete(self, messages, *, tools):
        self.messages.append(deepcopy((messages, tools)))
        if self.fail_first and len(self.messages) == 1:
            raise ModelCallError('tool_arguments_json_invalid', 'synthetic')
        return response()


class PublicEvaluationTests(OfflineTests):
    def test_inventory_and_approved_gold_keep_all_frozen_denominators(self):
        for split, count, buildable in (('dev', 64, 64), ('regression', 414, 411)):
            rows, gold, _, _ = evaluation.populations(split)
            self.assertEqual(len(rows), count)
            self.assertEqual(len(gold), count)
            preflight = evaluation.cases.preflight(rows)
            self.assertTrue(preflight['passed'])
            self.assertEqual(preflight['buildable'], buildable)
            self.assertEqual(preflight['target_calls'], 0)
            if split == 'regression':
                self.assertEqual(preflight['applicable'], 412)
                self.assertEqual(preflight['unbuildable'], ['V2002'])
        freeze = evaluation.cases.validate_freeze(require_review=True)
        self.assertFalse(freeze['candidate']['eligible_for_switch'])

    def test_plan_is_offline_and_pins_new_approval_and_split_caps(self):
        with patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials')):
            plan = evaluation.evaluate(split='dev')
        self.assertEqual((plan['max_calls'], plan['max_attempts_per_target'], plan['paid_approval_message']), (80, 2, 439))
        self.assertEqual(plan['approved_call_caps'], dict(dev=80, regression=520))
        self.assertEqual(plan['tool_arguments_policy']['approval_messages'], [439])
        self.assertEqual((plan['paid_calls'], plan['input_usage'], plan['cost']), (0, 'unknown', 'unknown'))
        self.assertFalse(plan['live_enabled'])
        self.assertFalse(plan['eligible_for_switch'])
        self.assertEqual(plan['context_preflight']['prediction_rows'], 0)
        for bad in (True, 0, 81, 520):
            with self.assertRaises(ValueError):
                evaluation.build_plan('dev', bad)

    def test_only_public_split_choices_are_admitted_before_any_input_or_credentials(self):
        with patch.object(evaluation, 'build_plan', side_effect=AssertionError('No planning')):
            for args in (['--split', 'holdout'], ['--split', 'all'], ['--corpus', 'private'],
                         ['--sealed-corpus', 'private'], ['--li']):
                with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    command.main(args)
        for split in ('all', 'holdout', 'sealed', '../regression'):
            with self.assertRaises(ValueError):
                evaluation.populations(split)

    def test_context_builder_only_receives_neutral_state_without_target_or_gold(self):
        row = sample()
        row['setup_id'] = 'independent-snapshot'
        with patch.object(evaluation.cases, 'build_context', return_value=(context(), 0)) as builder:
            evaluation.build_context(row, {}, {})
        state = builder.call_args.args[0]
        self.assertEqual(set(state), {'question', 'context', 'setup_id'})
        self.assertNotEqual(state['question'], row['question'])
        self.assertNotIn('gold', state)
        self.assertNotIn('id', state)

    def test_budgets_count_failures_retry_once_and_do_not_borrow_other_split(self):
        for maximum, split in ((81, 'dev'), (521, 'regression'), (True, 'dev'), (0, 'dev')):
            with self.assertRaises(ValueError):
                evaluation.AttemptBudget(maximum, split=split)
        model, events = Model(fail_first=True), []
        budget = evaluation.AttemptBudget(2, events.append)
        row = sample()
        result = evaluation.predict(row, prefixes={}, setups={}, model=model, budget=budget)
        self.assertEqual((result['status'], result['attempts']), ('ok', 2))
        self.assertEqual(model.messages[0], model.messages[1])
        self.assertEqual(result['raw']['status'], 'failed')
        self.assertEqual(len(result['attempt_history']), 2)
        self.assertIsNone(result['attempt_history'][0]['response'])
        self.assertEqual([e['phase'] for e in events], ['reserved', 'finished', 'reserved', 'finished'])
        with self.assertRaises(ValueError):
            budget.target(model, row['id'])
        with self.assertRaises(ModelConfigurationError):
            budget.target(model, 'other').complete((), tools=())

    def test_journal_failure_prevents_transport_and_propagates_run_invalidation(self):
        def broken(event):
            raise OSError('synthetic journal failure')
        model = Model()
        budget = evaluation.AttemptBudget(1, broken)
        with self.assertRaises(evaluation.RunInvalidated):
            evaluation.predict(sample(), prefixes={}, setups={}, model=model, budget=budget)
        self.assertEqual(model.messages, [])

    def test_local_and_failed_targets_do_not_hide_usage_or_shrink_reports(self):
        model = Model()
        row = sample()
        row['question'] = '查看上次结果'
        result = evaluation.predict(row, prefixes={}, setups={}, model=model, budget=evaluation.AttemptBudget(1))
        self.assertEqual(result['attempts'], 0)
        self.assertEqual(model.messages, [])
        row = sample()
        failed = evaluation._empty('not_measured', 'budget exhausted')
        scored = dict(row, V2=failed, A=dict(status='ok', fields=deepcopy(row['gold'])), effective_gold=row['gold'])
        report = evaluation.report_results([scored], {row['id']: row['gold']}, {})
        self.assertEqual(report['score_views']['normalized_approved']['all']['n'], 1)
        self.assertEqual(report['score_views']['normalized_approved']['all']['joint_all_fields']['numerator'], 0)
        self.assertFalse(report['passed'])

    def test_reference_diagnostics_do_not_change_the_fourteen_field_score(self):
        row, model = sample(), Model()
        result = evaluation.predict(row, prefixes={}, setups={}, model=model, budget=evaluation.AttemptBudget(1))
        scored = dict(row, V2=result, A=dict(status='ok', fields=deepcopy(row['gold'])), effective_gold=row['gold'],
                      reference_expectation=dict(reference=dict(kind='all', scope='shortlist', index=None)))
        report = evaluation.report_results([scored], {row['id']: row['gold']}, {})
        self.assertEqual(report['score_views']['normalized_approved']['all']['joint_all_fields']['numerator'], 1)
        self.assertFalse(report['reference_comparison'][0]['matches'])
        self.assertFalse(report['passed'])  # A synthetic response and partial population cannot pass.

    def test_dirty_or_invalid_preflight_live_run_stops_before_credentials(self):
        with patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials')) as factory:
            with patch.object(evaluation, 'build_plan', return_value=dict(workspace_dirty=True)):
                with self.assertRaises(ValueError):
                    evaluation.evaluate(live=True)
            for timeout in (True, float('nan'), 0, 46):
                with self.assertRaises(ValueError):
                    evaluation.evaluate(timeout_seconds=timeout)
            factory.assert_not_called()

    def test_late_drift_removes_passing_report_and_keeps_run_claim(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = root/'.agentbus/evaluation-runs/research-intent-v2.6/public/dev'
            plan = dict(workspace_dirty=False, context_preflight=dict(passed=True), runtime_revision='synthetic', max_calls=1)
            def check(_):
                if (target/'report.json').exists():
                    raise evaluation.RunInvalidated('synthetic late drift')
            with patch.object(evaluation, 'ROOT', root), patch.object(evaluation, 'build_plan', return_value=plan), \
                    patch.object(evaluation, 'assert_current', side_effect=check), \
                    patch.object(evaluation, 'populations', return_value=([], {}, {}, {})), \
                    patch.object(evaluation, '_live_model', return_value=Model()) as factory, \
                    patch.object(evaluation, 'report_results', return_value=dict(passed=True)), \
                    patch.object(evaluation, 'markdown', return_value='synthetic'):
                with self.assertRaises(evaluation.RunInvalidated):
                    evaluation.evaluate(live=True)
                self.assertFalse((target/'report.json').exists())
                self.assertTrue((target/'invalidated.json').exists())
                with self.assertRaises(FileExistsError):
                    evaluation.evaluate(live=True)
                self.assertEqual(factory.call_count, 1)

    def test_new_revision_cannot_reopen_same_version_split_or_reload_credentials(self):
        with TemporaryDirectory() as folder:
            plan = dict(workspace_dirty=False, context_preflight=dict(passed=True), runtime_revision='first', max_calls=1)
            with patch.object(evaluation, 'ROOT', Path(folder)), patch.object(evaluation, 'build_plan', return_value=plan), \
                    patch.object(evaluation, 'assert_current'), \
                    patch.object(evaluation, 'populations', return_value=([], {}, {}, {})), \
                    patch.object(evaluation, '_live_model', return_value=Model()) as factory, \
                    patch.object(evaluation, 'report_results', return_value=dict(passed=False)), \
                    patch.object(evaluation, 'markdown', return_value='synthetic'):
                evaluation.evaluate(live=True)
                plan['runtime_revision'] = 'another-commit'
                with self.assertRaises(FileExistsError):
                    evaluation.evaluate(live=True)
                factory.assert_called_once()

    def test_missing_reference_or_authorization_proof_is_unmeasured_and_cannot_pass(self):
        row = sample()
        row['gold'] = dict(row['gold'], subject_source='previous', wants_plan=True)
        observation = dict(evaluation._empty('ok', None), fields=deepcopy(row['gold']))
        report = evaluation.redlines([dict(row, V2=observation)])
        for name in ('reference_scope', 'plan_authorization'):
            self.assertEqual(report['checks'][name]['status'], 'unmeasured')
            self.assertEqual(report['checks'][name]['measured'], 0)
        self.assertFalse(report['passed'])
        observation['metadata'] = dict(diagnostics=dict(
            binding=dict(issue=None, scope_subjects=['600000.SH']),
            authorization=dict(plan_source='program_pending', same_task_pending=False, plan_veto=False)))
        report = evaluation.redlines([dict(row, V2=observation)])
        self.assertEqual(report['checks']['reference_scope']['violations'], 1)
        self.assertEqual(report['checks']['plan_authorization']['violations'], 1)
        self.assertFalse(report['passed'])


if __name__ == '__main__':
    unittest.main()
