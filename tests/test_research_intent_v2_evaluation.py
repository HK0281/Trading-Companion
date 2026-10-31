"""Independent synthetic experiments only; never run the corpus or a real provider."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_research_intents as command
from scripts.verify import clean_environment, offline_network
from trading_companion import research_intent_v2_evaluation as experiment
from trading_companion.model import ModelCallError, ModelConfigurationError, TokenUsage
from trading_companion.research_intent_evaluation import format_report, write_report
from trading_companion.research_intent_evaluation_baseline import UNKNOWN
from trading_companion.research_intent_v2 import TOOL_NAME, VERSION
from trading_companion.tool_model import ToolCall, ToolModelResponse


def snapshot(**changes):
    value = dict(has_company=False, has_technical=False, has_index=False, company_enabled=True,
        workflow_enabled=True, development=False, conversation_memory=True, latest=None, source='tencent',
        pending='none', pending_age_seconds=None, pending_binding=None, shortlist=[], history=[])
    value.update(changes)
    return value


def draft(**changes):
    value = dict(version=VERSION, task='stock_research', operation='new', objective_kind='analysis',
        objective_phrase=None, wants_plan=False, plan_phrase=None,
        subject=dict(source='question', mentions=['浦发银行'], selection=None),
        horizon_expression=None, date_expression=None, indicator_mentions=[], research_kinds=[],
        exclusions=[], strict_scope=False, scope_phrase=None, confidence='high', clarification_reason=None)
    value.update(changes)
    return value


def response(value=None, **changes):
    return ToolModelResponse('tool_calls', tool_calls=(ToolCall('independent', TOOL_NAME, value or draft()),), **changes)


class RecordingModel:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.messages = result or response(), error, []

    def complete(self, messages, *, tools):
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return self.result


def state(question='请研究浦发银行', **context):
    return dict(question=question, context=snapshot(**context))


def expected_fields():
    return dict(task='stock_research', operation='new', objective_kind='analysis', wants_plan=False,
        subject_source='question', bound_subjects=['600000.SH'], horizon=None, date_expression=None,
        indicator_mentions=[], exclusions=[], research_kinds=[], strict_scope=False, outcome='research', zero_intent_calls=False)


def row(identifier='independent', **changes):
    value = dict(id=identifier, split='dev', question='请研究浦发银行', context=snapshot(), gold=expected_fields(),
        tags=[], source=dict(kind='synthetic'), fault=None, probe=dict(steps=[], unmeasured_reason='independent'))
    value.update(changes)
    return value


def registry():
    return dict(version='research-intent-v2-exposure-v1', known_during_implementation={})


def run(rows, model=None, **changes):
    arguments = dict(plan={}, model=model or RecordingModel(), maximum=155, timeout_seconds=45,
                     prefixes={}, baseline={}, registry=registry())
    arguments.update(changes)
    return experiment._run_rows(rows, **arguments)


class AdmissionTests(unittest.TestCase):
    def test_plan_and_default_never_load_credentials_or_construct_provider(self):
        with clean_environment(), offline_network(), \
                patch.object(experiment, '_live_model', side_effect=AssertionError('no live')), \
                patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('no key')):
            plan = experiment.evaluate_development()
            self.assertFalse(plan['live_enabled'])
            self.assertEqual(plan['prediction_rows'], 0)
            self.assertEqual(plan['max_dev_attempts'], 155)
            self.assertIn('V2203', plan['exposure']['known_during_implementation'])
            self.assertIn('data/evaluation/research-intent-v2-exposure.json', plan['contract_implementation_sha256_lf'])
            with redirect_stdout(StringIO()):
                self.assertEqual(command.main(['--v2-plan-only']), 0)

    def test_holdout_and_bad_budget_rejected_before_credentials(self):
        with patch.object(experiment, '_live_model', side_effect=AssertionError('no live')):
            for kwargs in (dict(split='holdout'), dict(maximum=0), dict(maximum=156), dict(maximum=True),
                           dict(timeout_seconds=91), dict(timeout_seconds=float('nan')), dict(timeout_seconds=True)):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    experiment.evaluate_development(live=True, **kwargs)
            with self.assertRaises(ValueError):
                experiment.evaluate_development(live=True)
        for arguments in (['--v2-live', '--split', 'holdout'], ['--v2-live', '--v2-plan-only']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                command.main(arguments)

    def test_cli_live_without_directory_has_no_attempt(self):
        with patch.object(experiment, '_live_model', side_effect=AssertionError('no key')), redirect_stdout(StringIO()) as out:
            self.assertEqual(command.main(['--v2-live']), 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'invalid_request')

    def test_live_factory_only_selects_flash_key_and_bounded_configuration(self):
        with patch('trading_companion.local_env.load_evaluation_key_environment', return_value={'DEEPSEEK_API_KEY': 'test-only'}) as key:
            model = experiment._live_model(23)
        key.assert_called_once_with('DEEPSEEK_API_KEY')
        self.assertEqual(model.config.model, 'deepseek-flash')
        self.assertEqual(model.config.max_output_tokens, 1500)
        self.assertEqual(model.config.timeout_seconds, 23)

    def test_explicit_live_orchestration_persists_independent_mock_without_network(self):
        sample = row()
        old = dict(plan=dict(corpus_sha256='independent', baseline={}),
                   rows=[dict(id='independent', A=dict(status='ok', fields={}), B=dict(status='not_measured', fields={}))])
        with TemporaryDirectory() as folder:
            baseline = Path(folder)/'baseline.json'
            baseline.write_text(json.dumps(old), encoding='utf-8')
            with clean_environment(), offline_network(), patch.object(experiment, 'BASELINE', baseline), \
                    patch.object(experiment, 'load_corpus', return_value=([sample], dict(annotation_status='reviewed'))), \
                    patch('trading_companion.research_intent_evaluation.build_v2_plan', return_value=dict(corpus_sha256='independent', baseline={})), \
                    patch.object(experiment, '_live_model', return_value=RecordingModel()) as factory:
                report = experiment.evaluate_development(live=True, report_dir=folder)
            factory.assert_called_once_with(45)
            self.assertEqual(report['authenticity'], 'mock_experiment')
            artifact = Path(report['artifacts'])
            self.assertTrue((artifact/'plan.json').exists())
            self.assertEqual(json.loads((artifact/'rows.jsonl').read_text(encoding='utf-8'))['id'], 'independent')
            self.assertEqual(json.loads((artifact/'report.json').read_text(encoding='utf-8'))['telemetry']['attempts'], 1)
            self.assertIn('模拟实验', (artifact/'report.md').read_text(encoding='utf-8'))


class ContextTests(unittest.TestCase):
    def test_target_never_receives_gold_id_script_tags_or_split(self):
        sample = row(gold=dict(expected_fields(), task='gold-private-sentinel'), id='id-private-sentinel',
            tags=['tags-private-sentinel'], probe=dict(steps='script-private-sentinel'))
        model = RecordingModel()
        result = run([sample], model)
        wire = json.dumps([m.to_dict() for m in model.messages[0]], ensure_ascii=False)
        for secret in ('gold-private-sentinel', 'id-private-sentinel', 'tags-private-sentinel', 'script-private-sentinel'):
            self.assertNotIn(secret, wire)
        self.assertEqual(result['rows'][0]['V2']['fields'], expected_fields())
        poisoned = state()
        poisoned['gold'] = expected_fields()
        with self.assertRaises(ValueError):
            experiment.program_context(poisoned, {})

    def test_snapshot_binding_preserves_order_source_mode_and_clock(self):
        latest = dict(kind='company', subjects=['600000.SH', '000001.SZ'], age_seconds=901,
                      source='tencent', development=False, current=True, horizon='未来40个交易日')
        ctx, calls = experiment.program_context(state(latest=latest, has_company=True,
            shortlist=['600036.SH', '600000.SH'], source='akshare_eastmoney'), {})
        self.assertEqual(calls, 0)
        self.assertEqual([s.instrument_id for s in ctx.session.latest_research.securities], latest['subjects'])
        self.assertEqual([s.instrument_id for s in ctx.session.screening_saved.securities], ['600036.SH', '600000.SH'])
        self.assertEqual(ctx.model_context()['latest']['age_seconds'], 901)
        self.assertEqual(ctx.source, 'akshare_eastmoney')
        self.assertEqual(ctx.session.latest_research.source, 'tencent')
        self.assertEqual(ctx.model_context()['latest']['horizon'], '未来40个交易日')
        self.assertEqual(ctx.model_context()['capabilities'], dict(company=True, workflow=True, memory=True))

    def pending_state(self, question='中线'):
        return state(question, pending='company_horizon', pending_age_seconds=0,
                     pending_binding=dict(session_matches=True, turn_matches=True), history=['浦发银行能买吗'])

    def prefixes(self):
        step = dict(question='浦发银行能买吗', script=dict(phases=['company_intent'], mentions=['浦发银行'], kinds=['technical']))
        return {('浦发银行能买吗',): [step]}

    def test_pending_is_replayed_by_real_service_and_detached_before_close(self):
        with clean_environment(), offline_network():
            ctx, calls = experiment.program_context(self.pending_state(), self.prefixes())
        self.assertEqual(calls, 1)
        self.assertIsNotNone(ctx.session.product_pending)
        self.assertIsNone(ctx.session._turn_parent)
        self.assertEqual(ctx.session.product_pending.securities[0].instrument_id, '600000.SH')
        result = experiment.local_preflight('中线', ctx)
        self.assertEqual(result.horizon, '未来40个交易日')
        self.assertTrue(result.wants_plan)
        self.assertTrue(result.zero_intent_calls)
        self.assertEqual(result.operation, 'update')
        self.assertIsNone(experiment.local_preflight('中线，改看招商银行', ctx))
        self.assertEqual(ctx.session.product_pending.objective.horizon, None)

    def test_pending_identity_never_invented_from_history_or_gold(self):
        model = RecordingModel()
        result = experiment.predict(self.pending_state(), prefixes={}, model=model, budget=experiment.AttemptBudget(2))
        self.assertEqual(result['status'], 'not_measured')
        self.assertEqual(model.messages, [])
        wrong = self.pending_state()
        wrong['context']['pending_binding']['session_matches'] = False
        result = experiment.predict(wrong, prefixes=self.prefixes(), model=model, budget=experiment.AttemptBudget(2))
        self.assertEqual(result['status'], 'not_measured')
        self.assertEqual(model.messages, [])

    def test_script_prefixes_exclude_holdout_and_conflicting_scripts(self):
        a = row(probe=dict(steps=[dict(question='前置甲', script=dict(phases=['knowledge']))]))
        b = row(split='holdout', probe=dict(steps=[dict(question='前置乙', script=dict(phases=['knowledge']))]))
        prefixes = experiment.prefix_library([a, b])
        self.assertIn(('前置甲',), prefixes)
        self.assertNotIn(('前置乙',), prefixes)
        other = deepcopy(a)
        other['probe']['steps'][0]['script']['phases'] = []
        self.assertIsNone(experiment.prefix_library([a, other])[('前置甲',)])


class PredictionTests(unittest.TestCase):
    def test_local_paths_have_no_model_and_cannot_truncate_extra_conditions(self):
        model = RecordingModel()
        budget = experiment.AttemptBudget(155)
        for question, task in (('取消研究', 'command'), ('替我下单买入浦发银行', 'unsupported'),
                               ('SMA是什么', 'knowledge'), ('回顾一下你上一轮的评级', 'recall')):
            result = experiment.predict(state(question, history=['历史问题']), prefixes={}, model=model, budget=budget)
            self.assertEqual(result['fields']['task'], task)
            self.assertEqual(result['attempts'], 0)
            self.assertTrue(result['fields']['zero_intent_calls'])
        self.assertEqual(model.messages, [])
        result = experiment.predict(state('取消，改研究浦发银行'), prefixes={}, model=model, budget=budget)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(len(model.messages), 1)

    def test_low_confidence_is_scored_as_checked_clarification(self):
        gold = dict(expected_fields(), outcome='clarify_conditions')
        report = run([row(gold=gold)], RecordingModel(response(draft(confidence='low'))))
        result = report['rows'][0]['V2']
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['low_confidence'])
        self.assertEqual(result['fields']['bound_subjects'], ['600000.SH'])
        self.assertEqual(report['metrics']['all']['joint_all_fields']['numerator'], 1)

    def test_timeout_unavailable_and_bad_format_have_one_attempt_no_retry(self):
        for model in (RecordingModel(error=ModelCallError('timeout', 'private-error-sentinel')),
                      RecordingModel(error=RuntimeError('private-error-sentinel')),
                      RecordingModel(response(draft(date_expression='not-in-input')))):
            with self.subTest(model=model):
                result = run([row()], model)
                self.assertEqual(len(model.messages), 1)
                self.assertNotEqual(result['rows'][0]['V2']['status'], 'ok')
                self.assertEqual(result['telemetry']['attempts'], 1)
                self.assertEqual(result['metrics']['all']['joint_all_fields']['denominator'], 1)
                self.assertNotIn('private-error-sentinel', json.dumps(result))

    def test_global_and_per_target_reservations_happen_before_transport(self):
        model, budget = RecordingModel(), experiment.AttemptBudget(1)
        target = budget.target(model)
        target.complete((), tools=())
        with self.assertRaises(ModelConfigurationError):
            target.complete((), tools=())
        with self.assertRaises(ModelConfigurationError):
            budget.target(model).complete((), tools=())
        self.assertEqual(len(model.messages), 1)
        report = run([row('one'), row('two'), row('three', question='取消')], maximum=1)
        self.assertEqual(report['telemetry']['attempts'], 1)
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual(report['rows'][2]['V2']['fields']['task'], 'command')
        self.assertEqual(report['metrics']['all']['joint_all_fields']['denominator'], 3)

    def test_late_response_is_retained_for_accounting_but_not_accepted(self):
        model = RecordingModel(response(usage=TokenUsage(3, 4, 7)))
        with patch('trading_companion.turn_control.TurnControl.finish', return_value='turn_timeout'):
            report = run([row()], model)
        result = report['rows'][0]['V2']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['fields'], {})
        self.assertEqual(result['response']['usage']['total_tokens'], 7)


class ReportingTests(unittest.TestCase):
    def test_failures_and_unmeasured_fields_keep_full_denominator(self):
        successful = run([row()])['rows'][0]
        failed, unmeasured, missing = (deepcopy(successful) for _ in range(3))
        failed['V2']['status'] = 'failed'
        unmeasured['V2']['status'] = 'not_measured'
        missing['V2']['fields'].pop('bound_subjects')
        metrics = experiment.metrics([successful, failed, unmeasured, missing], registry())['all']
        self.assertEqual(metrics['joint_all_fields'], dict(numerator=1, denominator=4, value=.25))
        self.assertEqual(metrics['fields']['bound_subjects']['measured'], 1)

    def test_returned_models_usage_and_nearest_rank_latency_are_actual(self):
        report = run([row()], RecordingModel(response(provider='deepseek', model='returned-version',
            requested_model='deepseek-flash', usage=TokenUsage(8, 5, 13), is_mock=False)))
        a = report['rows'][0]
        a['V2']['call_ms'] = 15
        b, c = deepcopy(a), deepcopy(a)
        b['V2']['response']['usage'], b['V2']['call_ms'] = None, 70
        c['V2']['response'], c['V2']['call_ms'] = None, 120
        stats = experiment.telemetry([a, b, c])
        self.assertEqual(stats['returned_models'], {'returned-version': 2})
        self.assertEqual(stats['latency']['p50_ms'], 70)
        self.assertEqual(stats['latency']['p95_ms'], 120)
        self.assertEqual(stats['tokens']['total_tokens']['reported_sum'], 13)
        self.assertEqual(stats['tokens']['total_tokens']['unknown_attempts'], 2)
        self.assertIsNone(stats['tokens']['total_tokens']['complete_total'])
        self.assertEqual(report['authenticity'], 'mock_experiment')
        self.assertEqual(report['paid_calls'], 0)
        a['V2']['response']['usage']['total_tokens'] = 100
        invalid = experiment.telemetry([a])
        self.assertEqual(invalid['inconsistent_usage_responses'], 1)
        self.assertIsNone(invalid['tokens']['total_tokens']['complete_total'])

    def test_known_holdout_is_separate_and_never_unseen(self):
        a = run([row('exposed', split='holdout')])['rows'][0]
        b = deepcopy(a)
        b['id'] = 'unseen'
        exposed = registry()
        exposed['known_during_implementation']['exposed'] = dict(reason='implementation exposure', unseen_holdout_eligible=False)
        scores = experiment.metrics([a, b], exposed)
        self.assertEqual(scores['implementation_known']['n'], 1)
        self.assertEqual(scores['unseen_holdout']['n'], 1)
        self.assertEqual(scores['all']['n'], 2)

    def test_baseline_only_compares_observed_common_fields(self):
        report = run([row()])
        actual = report['rows'][0]
        previous = dict(A=dict(status='ok', fields=dict(task='unsupported', wants_plan=UNKNOWN)),
                        B=dict(status='not_measured', fields={}))
        scores = experiment.common_comparison([actual], {'independent': previous})
        self.assertEqual(scores['A']['fields']['task']['n'], 1)
        self.assertEqual(scores['A']['fields']['task']['v2']['numerator'], 1)
        self.assertEqual(scores['A']['fields']['wants_plan']['n'], 0)
        self.assertEqual(scores['A']['fields']['bound_subjects']['n'], 0)
        self.assertEqual(scores['B']['fields']['outcome']['n'], 0)

    def test_mock_journal_artifacts_and_unmeasured_scope(self):
        journal = StringIO()
        report = run([row()], journal=journal)
        self.assertEqual(json.loads(journal.getvalue())['id'], 'independent')
        self.assertFalse(report['eligible_for_switch'])
        self.assertIn('跨对象证据借用', report['unmeasured'])
        self.assertEqual(report['zero_tolerance']['cross_object']['measured'], 0)
        self.assertEqual(report['zero_tolerance']['wrong_subject']['unmeasured'], 1)
        self.assertIn('模拟实验', format_report(report))
        with TemporaryDirectory() as folder:
            first = Path(write_report(report, folder))
            second = Path(write_report(report, folder))
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads((first/'report.json').read_text(encoding='utf-8'))['kind'], 'v2_development')


if __name__ == '__main__':
    unittest.main()
