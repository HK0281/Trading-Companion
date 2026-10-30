from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from scripts.evaluate_intent_router import main
from trading_companion import intent_router_evaluation as evaluation
from trading_companion.intent_router_models import fixture_post
from trading_companion.intent_router_reporting import write_report
from trading_companion.intent_router_task import load_corpus, input_state, digest
from trading_companion.typesafe_jev import ClassifierError

PRICES = 'data/evaluation/model-prices.json'


def save(value, path):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return str(path)


class IntentEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev = evaluation.evaluate()
        cls.holdout = evaluation.evaluate(split='holdout')

    def test_complete_splits_and_protected_requests_never_enter_models(self):
        rows, _ = load_corpus()
        allowed = {digest(input_state(r)) for r in rows if r['split'] == 'dev'}
        forbidden = {digest(input_state(r)) for r in rows if r['case_kind'] == 'protected'}
        delegate = Mock()
        def classify(state):
            self.assertIn(digest(state), allowed)
            self.assertNotIn(digest(state), forbidden)
            return dict(prediction='clarify', confidence=.8, model='jev-latest', usage=None)
        delegate.classify.side_effect = classify
        with patch.object(evaluation, 'build_models', return_value={'jev': delegate}):
            report = evaluation.evaluate(classifiers=['jev'])
        self.assertEqual(delegate.classify.call_count, 144)
        self.assertEqual(report['protected']['model_calls'], 0)
        self.assertEqual(report['protected']['n'], 64)
        for report, n in ((self.dev, 144), (self.holdout, 96)):
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(len(report['rows']), n)
            self.assertEqual(report['accounting']['forwarded'], n*2)
            self.assertEqual(report['metrics']['providers']['rules']['non_clarify']['accuracy']['denominator'], n*7//8)
        self.assertEqual(self.holdout['cutover']['status'], 'not_demonstrated')

    def test_clarification_advantage_cannot_hide_worse_business_classification(self):
        from trading_companion.evaluation_pricing import DEFAULT_PRICES, load_prices
        from trading_companion.intent_router_metrics import metrics, cutover
        report=deepcopy(self.holdout)
        for row in report['rows']:
            row['results']['jev'].update(prediction=row['gold'],confidence=.99)
            row['results']['rules']['prediction']=row['gold'] if row['gold']!='clarify' else 'company_research'
        next(r for r in report['rows'] if r['gold']=='company_research')['results']['jev']['prediction']='screening'
        prices,_=load_prices(DEFAULT_PRICES)
        report['metrics']=metrics(report['rows'],report['plan']['classifiers'],report['plan']['models'],prices,
                                  simulated=True,threshold=.9,split='holdout')
        checks=cutover(report)['checks']
        self.assertTrue(checks['paraphrase_gain_all'])
        self.assertFalse(checks['paraphrase_gain_non_clarify'])
        self.assertFalse(checks['non_clarify_accuracy'])

    def test_caught_offline_network_attempt_still_invalidates_report(self):
        delegate=Mock()
        def classify(state):
            try:
                socket.getaddrinfo('example.invalid',443)
            except Exception:
                pass
            return dict(prediction='clarify',confidence=.8,model='jev-latest',usage=None)
        delegate.classify.side_effect=classify
        with patch.object(evaluation,'build_models',return_value={'jev':delegate}):
            report=evaluation.evaluate(classifiers=['jev'],call_limit=1)
        self.assertEqual(report['code'],'evaluation_boundary_or_initialization_error')
        self.assertEqual(report['status'],'partial')

    def test_preview_and_offline_do_not_read_environment_keys_or_network(self):
        original, environ_get = io.open, os.environ.get
        def guarded_open(path, *args, **kwargs):
            if isinstance(path, (str, Path)) and Path(path).name == '.env':
                raise AssertionError('No .env reads')
            return original(path, *args, **kwargs)
        def guarded_get(key, *args):
            if key.endswith('_API_KEY'):
                raise AssertionError('No credential reads')
            return environ_get(key, *args)
        with patch('io.open', side_effect=guarded_open), patch.object(os.environ, 'get', side_effect=guarded_get), \
             patch('requests.post', side_effect=AssertionError('No HTTP')), \
             patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')):
            self.assertEqual(evaluation.build_plan()['status'], 'planned')
            self.assertEqual(evaluation.evaluate(classifiers=['jev'], call_limit=1)['status'], 'partial')
            plan = evaluation.build_plan(mode='live', classifiers=['jev'], call_limit=144, prices=PRICES)
            self.assertEqual(plan['status'], 'planned')

    def test_suite_previews_and_assumed_costs_are_separate(self):
        plan = evaluation.build_plan(mode='live', classifiers=['jev','deepseek'], call_limit=288,
            prices=PRICES, deepseek_model='deepseek-flash', assumed_input_tokens=1000, assumed_output_tokens=100)
        suites = plan['suite_preview']
        self.assertEqual([suites[k]['requests_upper_bound'] for k in suites], [288,192,48,48,328])
        self.assertEqual(plan['all_suites_requests_upper_bound'], 904)
        self.assertFalse(suites['legacy-upper-bound']['implemented'])
        self.assertEqual(suites['dev']['cost_by_provider']['jev']['currency'], 'USD')
        self.assertEqual(suites['holdout']['cost_by_provider']['deepseek']['currency'], 'CNY')
        self.assertEqual(evaluation.build_plan(suite='legacy')['code'], 'intent_legacy_deferred')

    def test_budget_failures_repeats_and_deterministic_order_keep_all_denominators(self):
        a = evaluation.evaluate(repeat=2, call_limit=3)
        b = evaluation.evaluate(repeat=2, call_limit=3)
        self.assertEqual(a['execution_order'], b['execution_order'])
        self.assertEqual(len(a['rows']), 288)
        self.assertEqual(a['accounting']['forwarded'], 3)
        self.assertEqual(sum(r['results'][c]['status']=='not_run' for r in a['rows'] for c in ('jev','deepseek')), 573)
        self.assertEqual(a['metrics']['providers']['jev']['all']['accuracy']['denominator'], 288)
        self.assertGreater(a['metrics']['cascades'][0]['missing_required_results'], 0)
        delegate = Mock()
        delegate.classify.side_effect = ClassifierError('timeout')
        with patch.object(evaluation, 'build_models', return_value={'jev':delegate}):
            failed = evaluation.evaluate(classifiers=['jev'], call_limit=1)
        provider = failed['metrics']['providers']['jev']
        self.assertEqual(provider['cost']['unknown_requests'], 1)
        self.assertEqual(provider['latency']['timeout']['n'], 1)
        self.assertEqual(provider['all']['accuracy']['denominator'], 144)

    def test_invalid_plans_and_draft_live_stop_before_clients(self):
        rows,manifest=load_corpus()
        draft={**manifest,'annotation_status':'draft','reviewer':None}
        with patch.object(evaluation, 'build_models') as factory, \
             patch.object(evaluation, 'load_corpus', return_value=(rows,draft)), \
             patch.object(evaluation, 'load_evaluation_key_environment', side_effect=AssertionError('No keys')):
            for options in ({'classifiers':['jev','jev']}, {'repeat':0}, {'call_limit':289},
                            {'timeout_seconds':float('inf')}, {'mode':'live','classifiers':['jev']},
                            {'suite':'context-ablation','repeat':2}, {'mode':'live','classifiers':['deepseek'],'call_limit':1,'prices':PRICES}):
                self.assertEqual(evaluation.build_plan(**options)['status'], 'invalid_request')
            with redirect_stderr(io.StringIO()):
                result = evaluation.evaluate(mode='live', classifiers=['jev'], call_limit=1, prices=PRICES)
            self.assertEqual(result['code'], 'intent_annotations_not_reviewed')
            factory.assert_not_called()

    def test_live_mock_http_prints_plan_first_loads_only_selected_key_and_never_retries(self):
        rows, manifest = load_corpus()
        reviewed = {**manifest, 'annotation_status':'reviewed','reviewer':'claude'}
        for provider, key_name in (('jev','TYPESAFE_API_KEY'), ('deepseek','DEEPSEEK_API_KEY')):
            log = io.StringIO()
            def key_loader(name):
                self.assertIn('"status": "planned"', log.getvalue())
                self.assertEqual(name, key_name)
                return {name:'synthetic-private-test'}
            def post(url, **kwargs):
                self.assertFalse(kwargs['allow_redirects'])
                self.assertTrue(kwargs['stream'])
                self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-private-test')
                return fixture_post(provider)(url, **kwargs)
            with self.subTest(provider=provider), patch.object(evaluation, 'load_corpus', return_value=(rows,reviewed)), \
                 patch.object(evaluation, 'load_evaluation_key_environment', side_effect=key_loader) as loader, \
                 patch('requests.post', side_effect=post) as transport, redirect_stderr(log):
                report = evaluation.evaluate(mode='live',classifiers=[provider],call_limit=1,prices=PRICES,
                    deepseek_model='deepseek-flash' if provider=='deepseek' else None)
            self.assertEqual(report['code'], 'partial_results')
            loader.assert_called_once_with(key_name)
            transport.assert_called_once()
            self.assertEqual(report['metrics']['providers'][provider]['statuses']['ok'], 1)
            self.assertNotIn('synthetic-private-test', json.dumps(report)+log.getvalue())

    def test_live_timeout_and_redirect_leave_unknown_cost_and_one_attempt(self):
        rows, manifest = load_corpus()
        reviewed = {**manifest, 'annotation_status':'reviewed','reviewer':'claude'}
        def redirect(url, **kwargs):
            response = fixture_post('jev')(url, **kwargs)
            response.status_code = 302
            return response
        for transport in (Mock(side_effect=requests.Timeout()), Mock(side_effect=redirect)):
            with patch.object(evaluation,'load_corpus',return_value=(rows,reviewed)), \
                 patch.object(evaluation,'load_evaluation_key_environment',return_value={'TYPESAFE_API_KEY':'synthetic'}), \
                 patch('requests.post',transport), redirect_stderr(io.StringIO()):
                report=evaluation.evaluate(mode='live',classifiers=['jev'],call_limit=1,prices=PRICES)
            transport.assert_called_once()
            self.assertEqual(report['metrics']['providers']['jev']['statuses']['failed'],1)
            self.assertEqual(report['metrics']['providers']['jev']['cost']['unknown_requests'],1)

    def test_rule_versions_reject_old_locks_and_cross_rule_references(self):
        self.assertEqual(self.dev['plan']['rule_baseline_version'], 'decision-horizon-v1')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            source=save(self.dev,root/'dev.json')
            path=root/'lock.json'
            lock=evaluation.export_lock(source,path)
            self.assertEqual(lock['version'],'intent-router-threshold-lock-v2')
            self.assertEqual(lock['rule_baseline_version'],self.dev['plan']['rule_baseline_version'])
            old=deepcopy(lock)
            old['version']='intent-router-threshold-lock-v1'
            save(old,path)
            self.assertEqual(evaluation.build_plan(split='holdout',threshold_lock=path)['code'],'intent_lock_invalid')
            old=deepcopy(lock)
            old['rule_baseline_version']='technical-shared-narrative-v1'
            save(old,path)
            self.assertEqual(evaluation.build_plan(split='holdout',threshold_lock=path)['code'],'intent_lock_incompatible')
            old_report=deepcopy(self.dev)
            old_report['plan']['rule_baseline_version']='technical-shared-narrative-v1'
            self.assertEqual(evaluation.build_plan(suite='context-ablation',
                reference_report=save(old_report,root/'old.json'))['code'],'intent_reference_incompatible')
            with self.assertRaises(ValueError):
                evaluation.export_lock(root/'old.json',root/'old-lock.json')

    def test_dev_lock_roundtrip_holdout_and_tampering(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = save(self.dev, root/'dev.json')
            path = root/'lock.json'
            lock = evaluation.export_lock(source,path)
            self.assertEqual(lock['review_status'],'draft')
            holdout = evaluation.evaluate(split='holdout',threshold_lock=path)
            self.assertEqual(holdout['status'],'completed')
            self.assertEqual(holdout['metrics']['selective'][0]['threshold'],lock['selection']['threshold'])
            with self.assertRaises(FileExistsError):
                evaluation.export_lock(source,path)
            bad = deepcopy(lock)
            bad['selection']['threshold']=.123
            save(bad,path)
            self.assertEqual(evaluation.build_plan(split='holdout',threshold_lock=path)['code'],'intent_lock_invalid')
            bad['selection_sha256']=digest(bad['selection'])
            save(bad,path)
            self.assertEqual(evaluation.build_plan(split='holdout',threshold_lock=path)['code'],'intent_lock_invalid')
            save(lock,path)
            altered = deepcopy(self.dev)
            altered['rows'][0]['question']='tampered'
            save(altered,Path(source))
            self.assertEqual(evaluation.build_plan(split='holdout',threshold_lock=path)['code'],'intent_lock_incompatible')

    def test_lock_rejects_partial_holdout_duplicate_and_changed_models(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            duplicate=deepcopy(self.dev)
            duplicate['rows'][0]=deepcopy(duplicate['rows'][1])
            for report in (self.holdout,duplicate,{**self.dev,'status':'partial'}):
                with self.subTest(status=report['status']),self.assertRaises((ClassifierError,ValueError)):
                    evaluation.export_lock(save(report,root/'source.json'),root/'lock.json')
            source=save(self.dev,root/'source.json')
            evaluation.export_lock(source,root/'lock.json')
            self.assertEqual(evaluation.build_plan(split='holdout',classifiers=['jev'],threshold_lock=root/'lock.json')['code'],
                             'intent_lock_incompatible')
            with patch.object(evaluation,'build_models') as factory, redirect_stderr(io.StringIO()):
                result=evaluation.evaluate(mode='live',classifiers=['jev','deepseek'],deepseek_model='deepseek-flash',
                    call_limit=192,prices=PRICES,split='holdout',threshold_lock=root/'lock.json')
            self.assertEqual(result['code'],'intent_lock_incompatible')
            factory.assert_not_called()

    def test_reviewed_live_holdout_requires_lock_review_and_locked_model_identity(self):
        rows, manifest=load_corpus()
        reviewed={**manifest,'annotation_status':'reviewed','reviewer':'claude'}
        live_dev=deepcopy(self.dev)
        live_dev['plan']['mode']='live'
        live_dev['plan']['corpus']=reviewed
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            source=save(live_dev,root/'dev.json')
            lock=evaluation.export_lock(source,root/'lock.json')
            with patch.object(evaluation,'load_corpus',return_value=(rows,reviewed)), \
                 patch.object(evaluation,'build_models') as factory, redirect_stderr(io.StringIO()):
                options=dict(mode='live',classifiers=['jev','deepseek'],deepseek_model='offline-deepseek-v1',call_limit=192,
                             prices=PRICES,split='holdout')
                self.assertEqual(evaluation.evaluate(**options)['code'],'intent_lock_required')
                self.assertEqual(evaluation.evaluate(**options,threshold_lock=root/'lock.json')['code'],'intent_lock_not_reviewed')
                factory.assert_not_called()
                lock.update(review_status='approved',reviewer='claude')
                save(lock,root/'lock.json')
                self.assertEqual(evaluation.build_plan(**options,threshold_lock=root/'lock.json')['status'],'planned')

    def test_ablation_references_whole_same_split_main_report_and_preserves_pairs(self):
        self.assertEqual(evaluation.evaluate(suite='context-ablation')['code'],'intent_reference_required')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            for split,report in (('dev',self.dev),('holdout',self.holdout)):
                path=save(report,root/(split+'.json'))
                result=evaluation.evaluate(split=split,suite='context-ablation',reference_report=path)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(len(result['rows']),24)
                self.assertEqual(result['accounting']['forwarded'],48)
                self.assertEqual(result['ablation']['jev']['both_ends_correct']['denominator'],12)
                strata=result['ablation']['jev']['pair_strata']
                self.assertEqual(strata['label_changes']['pairs'],3)
                self.assertEqual(strata['label_invariant']['pairs'],9)
                self.assertEqual(strata['label_changes']['both_ends_correct_full']['denominator'],3)
                self.assertEqual(strata['label_invariant']['both_ends_correct_without_latest']['denominator'],9)
                self.assertEqual(result['plan']['reference_sha256'],digest(report))
                changed=deepcopy(result['rows'])
                for row in changed:
                    row['results']['jev']['model']='different-served-model'
                comparison=evaluation.ablation_summary(changed,report,['jev'])['jev']
                self.assertFalse(comparison['model_identity_matches_reference'])
                self.assertIsNone(comparison['accuracy_change'])
                original={r['id']:r for r in report['rows']}
                for row in result['rows']:
                    for key in ('prediction','rule_id'):
                        self.assertEqual(row['results']['rules'][key],original[row['id']]['results']['rules'][key])
            self.assertEqual(evaluation.build_plan(split='dev',suite='context-ablation',reference_report=path)['code'],
                             'intent_reference_incompatible')
            incomplete=deepcopy(self.dev)
            incomplete['rows']=incomplete['rows'][:-1]
            self.assertEqual(evaluation.build_plan(suite='context-ablation',reference_report=save(incomplete,root/'bad.json'))['code'],
                             'intent_reference_invalid')

    def test_cli_exit_codes_and_preview_does_not_write_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'new'
            with redirect_stdout(io.StringIO()) as output:
                code=main(['--plan-only','--report-dir',str(target)])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(output.getvalue())['status'],'planned')
            self.assertFalse(target.exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--call-limit','1']),1)
                self.assertEqual(main(['--call-limit','0']),2)
