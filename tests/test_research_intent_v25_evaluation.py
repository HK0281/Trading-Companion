"""Offline budget, public population, state, failure and report admission checks."""

from contextlib import ExitStack, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation_tools import evaluate_research_intents_v25 as command
from evaluation_tools import research_intent_v25_evaluation as experiment
from scripts.verify import clean_environment, offline_network
from test_research_intent_v2 import context, draft, response
from trading_companion import research_intent_v2 as intent
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.research_intent_evaluation import same


def sample():
    return dict(id='independent-public', family_id='independent', split='dev', dataset='dev', question='研究平安银行',
        context=dict(has_company=False, has_technical=False, has_index=False, company_enabled=True, workflow_enabled=True,
            development=False, conversation_memory=True, latest=None, source='tencent', pending='none',
            pending_age_seconds=None, pending_binding=None, shortlist=[], history=[]),
        gold=intent.check_response('研究平安银行', response(), context=context()).fields())


class Model:
    def __init__(self, error=None):
        self.error, self.messages = error, []

    def complete(self, messages, *, tools):
        self.messages.append(messages)
        if self.error:
            raise self.error
        return response()


class OfflineTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())


class EvaluationTests(OfflineTests):
    def test_complete_public_preflight_keeps_known_gap_and_exclusions(self):
        rows, effective, prefixes, setups = experiment.populations()
        self.assertEqual(len(rows), 318)
        self.assertEqual(sum(r['dataset'] == 'dev' for r in rows), 233)
        self.assertEqual(sum(r['dataset'] == 'regression' for r in rows), 85)
        self.assertNotIn('holdout', {r['split'] for r in rows})
        result = experiment.preflight(rows, prefixes, setups)
        self.assertTrue(result['passed'])
        self.assertEqual(result['buildable'], 315)
        self.assertEqual(result['unbuildable'], ['V2002'])
        self.assertEqual(set(result['not_applicable']), {'V2222', 'V2223'})
        self.assertEqual((result['target_calls'], result['prediction_rows'], result['score_rows']), (0, 0, 0))
        # Gold chooses which public contract to assert, never a model input or program branch.
        for row in rows:
            if row['id'] in experiment.NOT_APPLICABLE or not effective[row['id']]['zero_intent_calls']:
                continue
            with self.subTest(case=row['id']):
                ctx, _ = experiment.build_context(row, prefixes, setups)
                before = dict(ctx.session.__dict__)
                local = intent.local_preflight(row['question'], ctx)
                self.assertIsNotNone(local)
                self.assertEqual(ctx.session.__dict__, before)
                for key, value in effective[row['id']].items():
                    self.assertTrue(same(local.fields()[key], value), (key, local.fields()[key], value))

    def test_budgets_reserve_before_transport_and_forbid_retry(self):
        for invalid in (0, 234, True, 1.5):
            with self.assertRaises(ValueError):
                experiment.AttemptBudget(invalid)
        events = []
        class ObservedModel(Model):
            def complete(self, messages, *, tools):
                self.assert_reservation = events[-1]['phase']
                return super().complete(messages, tools=tools)
        model = ObservedModel()
        budget = experiment.AttemptBudget(1, events.append)
        target = budget.target(model, 'unique')
        target.complete((), tools=())
        self.assertEqual(model.assert_reservation, 'reserved')
        self.assertEqual([e['phase'] for e in events], ['reserved', 'finished'])
        with self.assertRaises(ModelConfigurationError):
            target.complete((), tools=())
        with self.assertRaises(ValueError):
            budget.target(model, 'unique')
        with self.assertRaises(ModelConfigurationError):
            budget.target(model, 'other').complete((), tools=())
        self.assertEqual(len(model.messages), 1)

    def test_one_semantic_attempt_without_ids_or_gold_in_model_input(self):
        model, row = Model(), sample()
        result = experiment.predict(row, prefixes={}, setups={}, model=model, budget=experiment.AttemptBudget(1))
        self.assertEqual((result['status'], result['attempts']), ('ok', 1))
        encoded = json.dumps([m.to_dict() for m in model.messages[0]], ensure_ascii=False)
        self.assertNotIn(row['id'], encoded)
        self.assertNotIn('"gold"', encoded)
        self.assertNotIn('"effective_gold"', encoded)
        self.assertEqual(len(model.messages), 1)

    def test_timeout_is_not_retried_and_does_not_serialize_provider_text(self):
        model = Model(ModelCallError('timeout', 'private-provider-body'))
        result = experiment.predict(sample(), prefixes={}, setups={}, model=model, budget=experiment.AttemptBudget(1))
        self.assertEqual((result['status'], result['attempts']), ('failed', 1))
        self.assertNotIn('private-provider-body', json.dumps(result))
        self.assertEqual(len(model.messages), 1)

    def test_zero_call_preflight_never_touches_the_model(self):
        row, model = sample(), Model(AssertionError('No model calls'))
        row['question'] = '查看上次结果'
        result = experiment.predict(row, prefixes={}, setups={}, model=model, budget=experiment.AttemptBudget(1))
        self.assertEqual((result['fields']['task'], result['attempts']), ('history', 0))
        self.assertEqual(model.messages, [])

    def test_bad_cli_and_dirty_live_admission_fail_before_credentials(self):
        with patch.object(experiment.core, '_live_model', side_effect=AssertionError('No credentials')) as factory:
            for split in ('holdout', 'sealed', '../regression'):
                with self.subTest(split=split), self.assertRaises(ValueError):
                    experiment.evaluate(split=split, live=True)
            with patch.object(experiment, 'build_plan', return_value=dict(workspace_dirty=True)):
                with self.assertRaises(ValueError):
                    experiment.evaluate(split='dev', live=True)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                command.main(['--split', 'holdout'])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                command.main(['--live'])
        factory.assert_not_called()

    def test_live_fixed_run_claim_cannot_resume_or_choose_a_new_report_directory(self):
        row = sample()
        planned = dict(workspace_dirty=False, context_preflight=dict(passed=True), runtime_revision='reviewed-test',
                       max_calls=1, split='dev', source_sha256_lf={})
        model = Model()
        with TemporaryDirectory() as folder, patch.object(experiment, 'ROOT', Path(folder)), \
                patch.object(experiment, 'build_plan', side_effect=lambda *a: deepcopy(planned)), \
                patch.object(experiment, 'assert_current'), \
                patch.object(experiment, 'populations', return_value=([row], {row['id']: row['gold']}, {}, {})), \
                patch.object(experiment.core, '_live_model', return_value=model) as factory:
            first = experiment.evaluate(split='dev', live=True, maximum=1)
            self.assertEqual(first['paid_calls'], 1)
            self.assertEqual(first['authenticity'], 'mock_experiment')
            self.assertFalse(first['passed'])
            events = [json.loads(s) for s in (Path(first['artifacts'])/'attempts.jsonl').read_text().splitlines()]
            self.assertEqual([e['phase'] for e in events], ['reserved', 'finished'])
            with self.assertRaises(FileExistsError):
                experiment.evaluate(split='dev', live=True, maximum=1)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(len(model.messages), 1)

    def test_runtime_drift_fails_closed(self):
        plan = dict(runtime_revision='reviewed', source_sha256_lf={'original.py': 'original'})
        with patch.object(experiment, '_git', side_effect=lambda *a: 'reviewed' if a[0] == 'rev-parse' else ''), \
                patch.object(experiment, 'fingerprints', return_value={'original.py': 'changed'}), \
                self.assertRaises(experiment.RunInvalidated):
            experiment.assert_current(plan)

    def test_failure_keeps_denominator_and_partial_pass_does_not_switch_product(self):
        row = sample()
        rows = [dict(row, effective_gold=row['gold'], V2=experiment._empty('failed', 'timeout'),
                     A=dict(status='ok', fields=row['gold']))]
        report = experiment.report_results(rows, {row['id']: row['gold']}, {})
        score = report['score_views']['normalized_approved']['all']['joint_all_fields']
        self.assertEqual((score['numerator'], score['denominator']), (0, 1))
        self.assertFalse(report['passed'])
        self.assertFalse(report['eligible_for_switch'])
        self.assertFalse(report['common_comparison']['passed'])


if __name__ == '__main__':
    unittest.main()
