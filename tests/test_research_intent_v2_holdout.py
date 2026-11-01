"""Independent synthetic data only; no real holdout predictions or provider calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from scripts import evaluate_research_intents_holdout as command
from scripts.verify import clean_environment, offline_network
from trading_companion import research_intent_v2_holdout as holdout
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.research_intent_v2 import VERSION, TOOL_NAME
from trading_companion.product_plan import ProductPlanContext
from trading_companion.research_session import PendingResearch
from trading_companion.tool_model import ToolCall, ToolModelResponse


def fields():
    return dict(task='stock_research', operation='new', objective_kind='analysis', wants_plan=False,
        subject_source='question', bound_subjects=['600000.SH'], horizon=None, date_expression=None,
        indicator_mentions=[], exclusions=[], research_kinds=[], strict_scope=False, outcome='research', zero_intent_calls=False)


def sample(identifier='independent'):
    return dict(id=identifier, split='holdout', question='请研究浦发银行',
        context=dict(has_company=False, has_technical=False, has_index=False, company_enabled=True,
            workflow_enabled=True, development=False, conversation_memory=True, latest=None, source='tencent',
            pending='none', pending_age_seconds=None, pending_binding=None, shortlist=[], history=[]),
        gold=fields(), tags=[], source=dict(kind='synthetic'), fault=None,
        probe=dict(steps=[], unmeasured_reason='independent synthetic only'))


def population():
    rows = [sample(f'independent-{i}') for i in range(82)] + [sample('V2203')]
    for identifier, flag in (('V2222', 'session_matches'), ('V2223', 'turn_matches')):
        row = sample(identifier)
        row['context'].update(pending='company_horizon', pending_age_seconds=0,
            pending_binding=dict(session_matches=True, turn_matches=True), history=['浦发银行能买吗'])
        row['context']['pending_binding'][flag] = False
        rows.append(row)
    return rows


def response():
    draft = dict(version=VERSION, task='stock_research', operation='new', objective_kind='analysis',
        objective_phrase=None, wants_plan=False, plan_phrase=None,
        subject=dict(source='question', mentions=['浦发银行'], selection=None), horizon_expression=None,
        date_expression=None, indicator_mentions=[], research_kinds=[], exclusions=[], strict_scope=False,
        scope_phrase=None, confidence='high', clarification_reason=None)
    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('independent', TOOL_NAME, draft),),
                             model='synthetic-only', is_mock=True, usage=TokenUsage(1, 2, 3))


class Model:
    def __init__(self, error=None):
        self.error, self.messages = error, []

    def complete(self, messages, *, tools):
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return response()


def plan(root):
    return dict(status='planned', contract_version=VERSION, candidate_revision='independent-candidate',
        runtime_revision='independent-runtime', lock_sha256='independent-lock', workspace_dirty=False,
        corpus_sha256='independent-corpus', split_sha256='independent-split',
        run_directory=str(Path(root)/VERSION/'holdout'), dataset_usage_record=str(Path(root)/'datasets/used.jsonl'),
        exposure=dict(version='research-intent-v2-exposure-v1', known_during_implementation={
            'V2203': dict(reason='independent known-row grouping test', unseen_holdout_eligible=False)}),
        population_revision=dict(not_applicable={key: dict(reason='independent architecture exclusion')
                                                 for key in ('V2222', 'V2223')}),
        run_available=True, ready_for_live=True, live_enabled=True)


def scored_rows():
    rows = population()
    for row in rows:
        row['A'] = dict(status='ok', fields=fields())
        row['V2'] = dict(status='ok', fields=fields(), attempts=1, setup_calls=0,
            response=response().to_dict(), call_ms=1, corrections=[], raw=dict(status='ok', fields=fields()))
        if row['id'] in ('V2222', 'V2223'):
            row['V2'].update(status='not_applicable', fields={}, attempts=0, response=None)
    return rows


class OfflineTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(clean_environment())
        stack.enter_context(offline_network())


class LockTests(OfflineTests):
    def test_archived_real_lock_rejects_current_candidate_before_credentials(self):
        # The v2.4 lock describes old source bytes, not the current v2.5 candidate.
        with patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')) as factory:
            for live in (False, True):
                with self.subTest(live=live), self.assertRaisesRegex(ValueError, 'Locked file mismatch'):
                    holdout.evaluate_holdout(live=live)
        factory.assert_not_called()

    def test_snapshot_plan_only_builds_contexts_without_prediction_scoring_or_credentials(self):
        # An independent snapshot tests orchestration only; actual admission above is never patched.
        lock = json.loads(holdout.LOCK_PATH.read_text(encoding='utf-8'))
        snapshot = dict(lock=lock, lock_sha256='independent-snapshot', runtime_revision='independent-revision',
                        workspace_dirty=False)
        with patch.object(holdout.core, 'predict', side_effect=AssertionError('no prediction')), \
                patch.object(holdout, 'validate_lock', return_value=snapshot), \
                patch.object(holdout.core, 'local_preflight', side_effect=AssertionError('no target interpretation')), \
                patch.object(holdout.core, 'metrics', side_effect=AssertionError('no scoring')), \
                patch.object(holdout, '_execute', side_effect=AssertionError('no run claim')), \
                patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')) as factory:
            result = holdout.evaluate_holdout()
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['paid_calls'], 0)
        self.assertFalse(result['live_enabled'])
        self.assertEqual(result['thresholds']['all']['minimum_correct'], 79)
        self.assertEqual(result['thresholds']['unseen_holdout']['minimum_correct'], 78)
        self.assertEqual(result['context_preflight']['buildable'], 83)
        self.assertEqual(result['context_preflight']['not_applicable'], ['V2222', 'V2223'])
        factory.assert_not_called()

    def test_incomplete_changed_candidate_runner_and_file_hashes_rejected_before_key(self):
        original = json.loads(holdout.LOCK_PATH.read_text(encoding='utf-8'))
        changes = [dict(status='candidate_locked_runner_pending'), dict(holdout_runner=None),
                   dict(holdout_live_admitted=False), dict(candidate_sha256='0'*64)]
        altered = deepcopy(original['candidate'])
        altered['model']['temperature'] = 1
        changes.append(dict(candidate=altered))
        for name, modification in (('thresholds', dict(all=dict(n=85, minimum_correct=80))),
                                   ('source_sha256_lf', {name: '0'*64 for name in holdout.RUNNER_FILES})):
            runner = deepcopy(original['holdout_runner'])
            runner[name] = modification
            changes.append(dict(holdout_runner=runner))
        with TemporaryDirectory() as folder, patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')):
            path = Path(folder)/'lock.json'
            for change in changes:
                path.write_text(json.dumps(dict(original, **change)), encoding='utf-8')
                with self.subTest(change=list(change)), patch.object(holdout, 'LOCK_PATH', path), self.assertRaises(ValueError):
                    holdout.evaluate_holdout(live=True)

    def test_extra_source_inventory_rejected(self):
        original = Path.rglob

        def files(path, pattern):
            yield from original(path, pattern)
            if path == holdout.ROOT/'src':
                yield path/'unreviewed_source.py'

        with patch.object(Path, 'rglob', autospec=True, side_effect=files), self.assertRaises(ValueError):
            holdout.validate_lock()

    def test_dirty_and_missing_repository_identity_fail_closed(self):
        for failed in ('dirty', 'ancestor'):
            def git(*args):
                if args[0] == 'status':
                    return ' M changed.py' if failed == 'dirty' else ''
                if args[0] == 'merge-base' and failed == 'ancestor':
                    raise ValueError('not ancestor')
                return 'independent-revision'
            with self.subTest(failed=failed), patch.object(holdout, '_git', side_effect=git), self.assertRaises(ValueError):
                holdout.validate_lock(require_clean=True)

    def test_non_boolean_live_and_unknown_cli_overrides_rejected(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                holdout.evaluate_holdout(live=value)
        for args in (['--live', '--plan-only'], ['--split', 'holdout'], ['--report-dir', 'elsewhere'],
                     ['--model', 'other'], ['--max-calls', '86'], ['--timeout-seconds', '90']):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                command.main(args)

    def test_cli_default_is_plan_and_invalid_input_does_not_echo_exception(self):
        with patch.object(command, 'evaluate_holdout', return_value=dict(status='planned')) as evaluate, redirect_stdout(StringIO()):
            self.assertEqual(command.main([]), 0)
        evaluate.assert_called_once_with(live=False)
        with patch.object(command, 'evaluate_holdout', side_effect=ValueError('private-exception-body')), redirect_stdout(StringIO()) as out:
            self.assertEqual(command.main(['--live']), 2)
        self.assertNotIn('private-exception-body', out.getvalue())


class ContextPreflightTests(OfflineTests):
    def prefixes(self):
        step = dict(question='浦发银行能买吗', script=dict(phases=['company_intent'],
                    mentions=['浦发银行'], kinds=['technical']))
        return {('浦发银行能买吗',): [step]}

    def pending_state(self, age=0, *, conflict=False, question='独立目标甲'):
        context = sample()['context']
        context.update(pending='conflict' if conflict else 'company_horizon', pending_age_seconds=age,
            pending_binding=dict(session_matches=True, turn_matches=True),
            history=[] if conflict else ['浦发银行能买吗'])
        return dict(question=question, context=context)

    def test_real_pending_clock_is_judged_by_unchanged_protocol(self):
        for age, valid in ((0, True), (899, True), (900, False), (901, False), (-60, False), (1800, False)):
            with self.subTest(age=age):
                context, calls = holdout.core.program_context(self.pending_state(age), self.prefixes())
                pending = context.session.product_pending
                self.assertIs(type(pending), ProductPlanContext)
                self.assertEqual(int((context.now-pending.created_at).total_seconds()), age)
                self.assertEqual(int((pending.expires_at-pending.created_at).total_seconds()), 900)
                self.assertEqual(context.model_context()['pending']['valid'], valid)
                self.assertEqual(calls, 1)
                if not valid:
                    self.assertIsNone(context.model_context()['pending']['original_question'])

    def test_conflict_uses_two_native_objects_and_only_reviewed_seed_conditions(self):
        contexts = []
        for question in ('独立目标甲', '完全不同的目标乙'):
            context, calls = holdout.core.program_context(self.pending_state(conflict=True, question=question), self.prefixes())
            contexts.append(context.model_context())
            self.assertIs(type(context.session.product_pending), ProductPlanContext)
            self.assertIs(type(context.session.pending), PendingResearch)
            product, technical = context.session.product_pending, context.session.pending
            self.assertEqual(technical.candidates, product.securities)
            self.assertEqual(technical.conditions.start_date, product.start_date)
            self.assertEqual(technical.conditions.as_of_date, product.end_date)
            self.assertEqual(technical.conditions.indicators, product.indicators)
            self.assertEqual(technical.user_question, product.original_question)
            self.assertEqual(technical.user_question, '浦发银行能买吗')
            self.assertEqual(context.history, ())
            self.assertEqual(context.model_context()['pending'], dict(valid=False, reason='conflict'))
            self.assertEqual(holdout.core.setup_metadata(context)['fault_injection'], 'multi_pending')
            self.assertEqual(calls, 1)
        self.assertEqual(*contexts)

    def test_missing_or_ambiguous_reviewed_seed_never_fabricates_pending(self):
        other = dict(question='研究浦发银行是否值得买', script=dict(phases=['company_intent'],
                     mentions=['浦发银行'], kinds=['technical']))
        ambiguous = self.prefixes()
        ambiguous[('研究浦发银行是否值得买',)] = [other]
        for prefixes in ({}, ambiguous):
            with self.subTest(prefixes=list(prefixes)), self.assertRaises(ValueError):
                holdout.core.program_context(self.pending_state(conflict=True), prefixes)

    def test_binding_fault_remains_unrepresentable(self):
        for flag in ('session_matches', 'turn_matches'):
            state = self.pending_state()
            state['context']['pending_binding'][flag] = False
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, '归属'):
                holdout.core.program_context(state, self.prefixes())

    def test_preflight_failure_prevents_ready_and_live_without_claiming_run(self):
        with TemporaryDirectory() as folder:
            selected = plan(folder)
            rows = population()
            broken = self.pending_state()['context']
            rows[0]['context'] = broken
            snapshot = dict(lock=dict(candidate=dict(revision=selected['candidate_revision'], model={},
                corpus_sha256=selected['corpus_sha256'], split_sha256=selected['split_sha256'],
                exposure=selected['exposure'])), runtime_revision=selected['runtime_revision'],
                lock_sha256=selected['lock_sha256'], workspace_dirty=False)
            with patch.object(holdout, 'validate_lock', return_value=snapshot), \
                    patch.object(holdout, '_paths', return_value=(Path(selected['run_directory']), Path(selected['dataset_usage_record']))), \
                    patch.object(holdout, '_population', return_value=selected['population_revision']), \
                    patch.object(holdout, '_holdout_inputs', return_value=(rows, {})), \
                    patch.object(holdout.core, 'local_preflight', side_effect=AssertionError('no target')), \
                    patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')):
                result = holdout.build_plan()
                self.assertFalse(result['ready_for_live'])
                self.assertEqual(result['context_preflight']['unbuildable'], ['independent-0'])
                with self.assertRaises(ValueError):
                    holdout.evaluate_holdout(live=True)
            self.assertFalse(Path(selected['run_directory']).exists())
            self.assertFalse(Path(selected['dataset_usage_record']).exists())


class ExecutionTests(OfflineTests):
    def execute(self, folder, *, rows=None, model=None, recheck=None, selected_plan=None):
        with patch.object(holdout.core, '_live_model', return_value=model or Model()):
            return holdout._execute(rows if rows is not None else population(),
                plan=selected_plan or plan(folder), prefixes={}, recheck=recheck or (lambda: None))

    def test_bounded_synthetic_run_persists_identity_times_and_original_gold(self):
        model = Model()
        with TemporaryDirectory() as folder, patch.object(holdout.core, 'revised_rows', side_effect=AssertionError('no amendments')):
            report = self.execute(folder, model=model)
            target = Path(plan(folder)['run_directory'])
            events = [json.loads(line) for line in (target/'attempts.jsonl').read_text(encoding='utf-8').splitlines()]
            usage = [json.loads(line) for line in Path(plan(folder)['dataset_usage_record']).read_text(encoding='utf-8').splitlines()]
            saved = json.loads((target/'report.json').read_text(encoding='utf-8'))
            receipt = json.loads((target/'completed.json').read_text(encoding='utf-8'))
            markdown = (target/'report.md').read_text(encoding='utf-8')
        self.assertEqual(len(model.messages), 83)
        self.assertEqual(sum(e['phase'] == 'reserved' for e in events), 83)
        self.assertEqual(report['telemetry']['attempts'], 83)
        self.assertEqual(report['provider_calls_forwarded'], 83)
        self.assertEqual(report['authenticity'], 'mock_experiment')
        self.assertFalse(report['thresholds_passed'])
        self.assertFalse(report['eligible_for_switch'])
        self.assertEqual(report['gold_amendments'], [])
        self.assertEqual(report['gates']['all']['correct'], 83)
        self.assertEqual(report['gates']['unseen_holdout']['n'], 82)
        self.assertEqual(report['reference_including_not_applicable']['all']['n'], 85)
        self.assertEqual(len(report['rows']), 85)
        self.assertEqual([r['V2']['status'] for r in report['rows'][-2:]], ['not_applicable']*2)
        self.assertEqual([r['status'] for r in usage], ['reserved', 'completed'])
        for value in (saved, receipt, usage[-1]):
            self.assertEqual(value['runtime_revision'], 'independent-runtime')
            self.assertEqual(value['lock_sha256'], 'independent-lock')
            self.assertLessEqual(value['started_at'], value['ended_at'])
        self.assertIn('不是防篡改证明', markdown)
        self.assertEqual(set(saved['score_views']), {'model_raw_original_gold', 'normalized_original_gold'})
        wire = json.dumps([m.to_dict() for m in model.messages[0]], ensure_ascii=False)
        self.assertNotIn('independent-0', wire)
        self.assertNotIn('unmeasured_reason', wire)
        self.assertNotIn('zero_intent_calls', wire)

    def test_version_directory_and_dataset_record_both_reject_reentry_before_key(self):
        with TemporaryDirectory() as folder:
            self.execute(folder)
            selected = plan(folder)
            other = dict(selected, run_directory=str(Path(folder)/'different-version/holdout'))
            with patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')) as factory:
                for current in (selected, other):
                    with self.subTest(directory=current['run_directory']), self.assertRaises(ValueError):
                        holdout._execute(population(), plan=current, prefixes={}, recheck=lambda: None)
                factory.assert_not_called()

    def test_empty_interrupted_directory_also_rejects(self):
        with TemporaryDirectory() as folder:
            Path(plan(folder)['run_directory']).mkdir(parents=True)
            with self.assertRaises(ValueError):
                self.execute(folder)

    def test_parallel_claim_only_one_can_run(self):
        with TemporaryDirectory() as folder:
            barrier = Barrier(2)
            model = Model()

            def run():
                barrier.wait(timeout=5)
                try:
                    return holdout._execute(population(), plan=plan(folder), prefixes={}, recheck=lambda: None)['status']
                except (ValueError, FileExistsError):
                    return 'rejected'

            with patch.object(holdout.core, '_live_model', return_value=model), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sorted(results), ['completed', 'rejected'])
        self.assertEqual(len(model.messages), 83)

    def test_transport_failure_is_not_retried_and_unknown_usage_is_preserved(self):
        with TemporaryDirectory() as folder:
            report = self.execute(folder, model=Model(ModelCallError('timeout', 'private-provider-body')))
            content = (Path(plan(folder)['run_directory'])/'report.json').read_text(encoding='utf-8')
        self.assertEqual(report['telemetry']['attempts'], 83)
        self.assertEqual(report['telemetry']['tokens']['total_tokens']['unknown_attempts'], 83)
        self.assertIsNone(report['telemetry']['tokens']['total_tokens']['complete_total'])
        self.assertEqual(len(report['failure_categories']['all']['failed']), 83)
        self.assertEqual(report['failure_categories']['all']['understanding_error'], [])
        self.assertNotIn('private-provider-body', content)
        self.assertFalse(report['thresholds_passed'])

    def test_interrupt_keeps_reservation_and_consumes_run(self):
        with TemporaryDirectory() as folder:
            report = self.execute(folder, model=Model(KeyboardInterrupt()))
            target = Path(plan(folder)['run_directory'])
            attempts = [json.loads(line) for line in (target/'attempts.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue((target/'aborted.json').exists())
            self.assertFalse((target/'completed.json').exists())
            with self.assertRaises(ValueError):
                self.execute(folder)
        self.assertEqual(report['reason'], 'interrupted')
        self.assertEqual(report['attempts_reserved'], 1)
        self.assertEqual(attempts[-1]['failure'], 'interrupted')

    def test_drift_between_budget_reservation_and_transport_is_sticky_and_stops_run(self):
        checks = 0

        def recheck():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise holdout.RunInvalidated('private-file-details')

        model = Model()
        with TemporaryDirectory() as folder:
            report = self.execute(folder, model=model, recheck=recheck)
        self.assertEqual(report['status'], 'aborted')
        self.assertEqual(report['reason'], 'lock_changed')
        self.assertEqual(report['attempts_reserved'], 1)
        self.assertEqual(report['provider_calls_forwarded'], 0)
        self.assertEqual(model.messages, [])
        self.assertFalse(report['thresholds_passed'])
        self.assertNotIn('private-file-details', json.dumps(report))

    def test_changed_revision_stops_before_next_row(self):
        model = Model()

        def recheck():
            if model.messages:
                raise holdout.RunInvalidated('revision changed')

        with TemporaryDirectory() as folder:
            report = self.execute(folder, model=model, recheck=recheck)
        self.assertEqual(report['reason'], 'lock_changed')
        self.assertEqual(report['attempts_reserved'], 1)
        self.assertEqual(len(report['rows']), 1)

    def test_late_drift_replaces_provisional_reports_and_never_writes_completion(self):
        with TemporaryDirectory() as folder:
            target = Path(plan(folder)['run_directory'])

            def recheck():
                if (target/'report.json').exists():
                    raise holdout.RunInvalidated('late drift')

            report = self.execute(folder, recheck=recheck)
            saved = json.loads((target/'report.json').read_text(encoding='utf-8'))
            markdown = (target/'report.md').read_text(encoding='utf-8')
            self.assertFalse((target/'completed.json').exists())
        self.assertEqual(report['reason'], 'lock_changed')
        self.assertEqual(saved['status'], 'aborted')
        self.assertFalse(saved['thresholds_passed'])
        self.assertIn('状态：aborted', markdown)

    def test_live_orchestration_uses_only_dev_prefixes_and_same_frozen_population(self):
        with TemporaryDirectory() as folder:
            selected_plan = plan(folder)
            selected = population()
            dev = dict(sample('independent-dev'), split='dev')
            manifest = dict(annotation_status='reviewed', corpus_sha256='independent-corpus', split_sha256='independent-split')
            current = dict(lock_sha256='independent-lock', runtime_revision='independent-runtime')
            with patch.object(holdout, 'build_plan', return_value=selected_plan), \
                    patch.object(holdout, 'load_corpus', return_value=([dev, *selected], manifest)), \
                    patch.object(holdout, 'validate_lock', return_value=current), \
                    patch.object(holdout.core, 'prefix_library', return_value={}) as prefixes, \
                    patch.object(holdout.core, '_live_model', return_value=Model()):
                result = holdout.evaluate_holdout(live=True)
            prefixes.assert_called_once_with([dev])
        self.assertEqual(result['gates']['all']['n'], 83)
        self.assertEqual(result['gates']['unseen_holdout']['n'], 82)

    def test_bad_population_is_rejected_before_credentials_or_claim(self):
        with TemporaryDirectory() as folder, patch.object(holdout, 'build_plan', return_value=plan(folder)), \
                patch.object(holdout.core, '_live_model', side_effect=AssertionError('no key')):
            for rows in (population()[:-1], [sample(f'independent-{i}') for i in range(85)]):
                manifest = dict(annotation_status='reviewed', corpus_sha256='independent-corpus', split_sha256='independent-split')
                with patch.object(holdout, 'load_corpus', return_value=(rows, manifest)), self.assertRaises(ValueError):
                    holdout.evaluate_holdout(live=True)
            self.assertFalse(Path(plan(folder)['run_directory']).exists())


class ScoringTests(OfflineTests):
    def report(self, rows):
        return holdout.make_report(rows, plan=plan('synthetic-no-write'), started_at='start', ended_at='end')

    def test_both_full_and_unseen_thresholds_are_fixed(self):
        rows = scored_rows()
        for row in rows[78:82]:
            row['V2']['fields']['operation'] = 'refresh'
        report = self.report(rows)
        self.assertTrue(report['gates']['all']['joint_passed'])
        self.assertTrue(report['gates']['unseen_holdout']['joint_passed'])
        rows[82]['V2']['fields']['operation'] = 'refresh'
        report = self.report(rows)
        self.assertFalse(report['gates']['all']['joint_passed'])
        self.assertTrue(report['gates']['unseen_holdout']['joint_passed'])
        self.assertEqual(report['failure_categories']['implementation_known']['understanding_error'], ['V2203'])

    def test_not_applicable_is_separate_with_unchanged_gold_and_reference_denominators(self):
        rows = scored_rows()
        original = deepcopy(rows)
        rows[0]['V2']['setup'] = dict(fault_injection='multi_pending')
        report = self.report(rows)
        self.assertEqual(set(report['not_applicable']), {'V2222', 'V2223'})
        self.assertEqual(report['gates']['all']['n'], 83)
        self.assertEqual(report['gates']['unseen_holdout']['n'], 82)
        reference = report['reference_including_not_applicable']
        self.assertEqual((reference['all']['correct'], reference['all']['n']), (83, 85))
        self.assertEqual((reference['unseen_holdout']['correct'], reference['unseen_holdout']['n']), (82, 84))
        self.assertFalse(reference['all']['participates_in_gate'])
        self.assertEqual(report['setup_fault_injections'], {'independent-0': dict(fault_injection='multi_pending')})
        self.assertEqual([r['gold'] for r in rows], [r['gold'] for r in original])
        for categories in report['failure_categories'].values():
            self.assertFalse(set(sum(categories.values(), [])) & {'V2222', 'V2223'})

    def test_missing_outputs_keep_rule_denominators_without_becoming_semantic_errors(self):
        rows = scored_rows()
        rows[0]['V2'].update(status='failed', fields={})
        rows[1]['V2'].update(status='not_measured', fields={}, attempts=0, response=None)
        rows[2]['V2']['fields']['operation'] = 'refresh'
        report = self.report(rows)
        horizon = report['common_comparison_A']['all']['fields']['horizon']
        self.assertEqual(horizon['n'], 83)
        self.assertEqual(horizon['candidate_correct'], 81)
        self.assertEqual((horizon['failure_missing'], horizon['unmeasured_missing'], horizon['understanding_errors']), (1, 1, 0))
        self.assertEqual(len(report['failure_categories']['all']['understanding_error']), 1)
        self.assertTrue(report['gates']['all']['joint_passed'])
        self.assertFalse(report['gates']['all']['common_fields_passed'])

    def test_unknown_rule_fields_are_not_evidence_of_parity(self):
        rows = scored_rows()
        for row in rows:
            row['A']['fields'] = {}
        report = self.report(rows)
        self.assertIsNone(report['common_comparison_A']['all']['fields']['task']['not_worse'])
        self.assertFalse(report['gates']['all']['common_fields_passed'])

    def test_mock_and_local_call_violation_cannot_pass_even_with_correct_fields(self):
        rows = scored_rows()
        report = self.report(rows)
        self.assertFalse(report['thresholds_passed'])
        for row in rows:
            if row['V2']['response'] is not None:
                row['V2']['response']['is_mock'] = False
        self.assertTrue(self.report(rows)['thresholds_passed'])
        rows[0]['gold']['zero_intent_calls'] = rows[0]['V2']['fields']['zero_intent_calls'] = True
        report = self.report(rows)
        self.assertFalse(report['thresholds_passed'])
        self.assertEqual(report['zero_tolerance']['local_intent_call']['violations'], 1)

    def test_large_correction_category_lists_each_case_and_does_not_change_gold(self):
        rows = scored_rows()
        original = deepcopy([r['gold'] for r in rows])
        for row in rows[:11]:
            row['V2']['corrections'] = [dict(kind='synthetic-correction', field='operation', before='update', after='new', source='synthetic')]
        report = self.report(rows)
        self.assertTrue(report['corrections']['synthetic-correction']['requires_individual_review'])
        self.assertEqual(len(report['corrections']['synthetic-correction']['ids']), 11)
        self.assertEqual([r['gold'] for r in rows], original)


if __name__ == '__main__':
    unittest.main()
