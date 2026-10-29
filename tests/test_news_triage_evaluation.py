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

from scripts.evaluate_news_triage import main
from trading_companion.news_triage import load_corpus
from trading_companion.news_triage_evaluation import build_plan, evaluate, network_boundary
from trading_companion.news_triage_models import fixture_post
from trading_companion.news_triage_reporting import annotation_review, format_report, write_report
from trading_companion.typesafe_jev import ClassifierError


class EvaluationTests(unittest.TestCase):
    def test_whole_corpus_offline_uses_both_protocols_and_144_calls(self):
        report=evaluate()
        self.assertEqual(report['status'],'completed')
        self.assertEqual(report['accounting']['forwarded'],144)
        self.assertEqual(len(report['rows']),72)
        self.assertEqual(len(report['metrics']['cascade']),10)
        for c in ('jev','deepseek'):
            self.assertEqual(report['metrics']['providers'][c]['statuses']['ok'],72)
            self.assertLess(report['metrics']['providers'][c]['combined_accuracy']['value'],1)
            self.assertEqual(report['metrics']['providers'][c]['cost']['scope'],'synthetic_usage_scenario_not_actual_spend')
        self.assertEqual(len(report['disagreements'])>0,True)

    def test_preview_and_offline_do_not_read_env_credentials_or_attempt_network(self):
        original=io.open
        environ_get=os.environ.get
        def guarded_open(path,*args,**kwargs):
            if isinstance(path,(str,Path)) and Path(path).name == '.env':
                raise AssertionError('Must not read .env')
            return original(path,*args,**kwargs)
        def guarded_get(key,*args):
            if key.endswith('_API_KEY'):
                raise AssertionError('Must not read credentials')
            return environ_get(key,*args)
        with patch('io.open',side_effect=guarded_open), patch.object(os.environ,'get',side_effect=guarded_get), \
             patch('requests.post',side_effect=AssertionError('Unexpected POST')), \
             patch('socket.getaddrinfo',side_effect=AssertionError('Unexpected DNS')):
            self.assertEqual(build_plan()['status'],'planned')
            self.assertEqual(evaluate(selected_items=['N001'])['status'],'completed')
            self.assertEqual(build_plan(mode='live',classifiers=['jev'],call_limit=1,
                                       selected_items=['N001'],prices='data/evaluation/model-prices.json')['status'],'planned')

    def test_budget_failure_repeats_and_stable_schedule(self):
        a=evaluate(selected_items=['N001','N002'],repeat=2,call_limit=3)
        b=evaluate(selected_items=['N001','N002'],repeat=2,call_limit=3)
        self.assertEqual(a['status'],'partial')
        self.assertEqual(a['accounting']['forwarded'],3)
        self.assertEqual(a['execution_order'],b['execution_order'])
        self.assertEqual(len(a['rows']),4)
        self.assertEqual(sum(r['results'][c]['status']=='not_run' for r in a['rows'] for c in ('jev','deepseek')),5)
        self.assertEqual(a['metrics']['providers']['jev']['combined_accuracy']['denominator'],12)

    def test_invalid_plan_precedes_clients_and_draft_blocks_live(self):
        rows, manifest=load_corpus()
        manifest.update(annotation_status='draft',reviewer=None)
        with patch('trading_companion.news_triage_evaluation.build_models') as factory, \
             patch('trading_companion.local_env._read_local_values',side_effect=AssertionError('Draft must not read .env')), \
             patch('trading_companion.news_triage_evaluation.load_corpus',return_value=(rows,manifest)):
            for options in ({'classifiers':['jev','jev']},{'repeat':0},{'timeout_seconds':float('inf')},
                            {'selected_items':['bad']},{'call_limit':145},{'mode':'live','classifiers':['jev']},
                            {'mode':'live','classifiers':['deepseek'],'call_limit':72,'prices':'data/evaluation/model-prices.json'}):
                self.assertEqual(build_plan(**options)['status'],'invalid_request')
            with redirect_stderr(io.StringIO()):
                report=evaluate(mode='live',classifiers=['jev'],call_limit=72,prices='data/evaluation/model-prices.json')
            self.assertEqual(report['code'],'triage_annotations_not_reviewed')
            factory.assert_not_called()

    def test_live_loads_only_selected_file_key_after_printing_plan_and_review_gate(self):
        from trading_companion import local_env
        rows, manifest = load_corpus()
        reviewed = {**manifest, 'annotation_status':'reviewed', 'reviewer':'claude'}
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp)/'.env'
            env_file.write_text('TYPESAFE_API_KEY=synthetic-jev\nDEEPSEEK_API_KEY=synthetic-deepseek\n'
                                'DEEPSEEK_MODEL=must-not-select-this-model\nOPENAI_API_KEY=must-not-use\n', encoding='utf-8')
            for provider, key in (('jev','synthetic-jev'), ('deepseek','synthetic-deepseek')):
                log = io.StringIO()
                original_read = local_env._read_local_values
                def read():
                    self.assertIn('"status": "planned"', log.getvalue())
                    self.assertIn('"live_annotation_ready": true', log.getvalue())
                    return original_read()
                def post(url, **kwargs):
                    self.assertEqual(kwargs['headers']['Authorization'], 'Bearer '+key)
                    if provider == 'deepseek':
                        self.assertEqual(kwargs['json']['model'], 'explicit-test-model')
                    return fixture_post(provider)(url, **kwargs)
                with self.subTest(provider=provider), patch.dict(os.environ, {}, clear=True), \
                     patch.object(local_env,'LOCAL_ENV_FILE',env_file), \
                     patch.object(local_env,'_read_local_values',side_effect=read) as reader, \
                     patch('trading_companion.news_triage_evaluation.load_evaluation_key_environment',
                           wraps=local_env.load_evaluation_key_environment) as loader, \
                     patch('trading_companion.news_triage_evaluation.load_corpus',return_value=(rows,reviewed)), \
                     patch('requests.post',side_effect=post), redirect_stderr(log):
                    result = evaluate(mode='live',classifiers=[provider],selected_items=['N001'],call_limit=1,
                                      prices='data/evaluation/model-prices.json',
                                      deepseek_model='explicit-test-model' if provider=='deepseek' else None)
                    self.assertEqual(dict(os.environ), {})
                self.assertEqual(result['status'],'completed')
                reader.assert_called_once()
                loader.assert_called_once_with('TYPESAFE_API_KEY' if provider=='jev' else 'DEEPSEEK_API_KEY')
                self.assertNotIn(key,json.dumps(result)+log.getvalue())

    def test_live_selected_adapter_with_mock_http_plan_first_no_retry(self):
        rows, manifest=load_corpus()
        reviewed={**manifest,'annotation_status':'reviewed','reviewer':'claude'}
        log=io.StringIO()
        def post(url,**kwargs):
            self.assertIn('"status": "planned"',log.getvalue())
            return fixture_post('jev')(url,**kwargs)
        with patch('trading_companion.news_triage_evaluation.load_corpus',return_value=(rows,reviewed)), \
             patch.dict(os.environ,{'TYPESAFE_API_KEY':'dummy-test-key'},clear=True), \
             patch('requests.post',side_effect=post) as transport, redirect_stderr(log):
            report=evaluate(mode='live',classifiers=['jev'],selected_items=['N001'],call_limit=1,prices='data/evaluation/model-prices.json')
        self.assertEqual(report['status'],'completed')
        self.assertIsNone(report['metrics']['cascade'])
        transport.assert_called_once()
        self.assertNotIn('dummy-test-key',json.dumps(report))

    def test_failures_keep_unknown_cost_and_all_planned_denominators(self):
        delegate=Mock()
        delegate.classify.side_effect=ClassifierError('rate_limited')
        with patch('trading_companion.news_triage_evaluation.build_models',return_value={'jev':delegate}):
            report=evaluate(classifiers=['jev'],selected_items=['N001','N002'])
        self.assertEqual(delegate.classify.call_count,2)
        self.assertEqual(report['status'],'partial')
        provider=report['metrics']['providers']['jev']
        self.assertEqual(provider['combined_accuracy']['denominator'],6)
        self.assertEqual(provider['cost']['unknown_requests'],2)
        self.assertEqual(provider['latency']['failed']['n'],2)

    def test_both_live_adapters_remain_simulated_and_bill_one_request_per_title(self):
        rows, manifest=load_corpus()
        manifest.update(annotation_status='reviewed',reviewer='claude')
        def post(url,**kwargs):
            return fixture_post('jev' if 'typesafe.ai' in url else 'deepseek')(url,**kwargs)
        with patch('trading_companion.news_triage_evaluation.load_corpus',return_value=(rows,manifest)), \
             patch.dict(os.environ,{'TYPESAFE_API_KEY':'test-jev','DEEPSEEK_API_KEY':'test-deepseek'},clear=True), \
             patch('requests.post',side_effect=post) as transport, redirect_stderr(io.StringIO()):
            result=evaluate(mode='live',classifiers=['jev','deepseek'],deepseek_model='test-ds',selected_items=['N001'],
                            call_limit=2,prices='data/evaluation/model-prices.json')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(transport.call_count,2)
        self.assertEqual(result['accounting']['forwarded'],2)
        self.assertEqual(len(result['metrics']['cascade']),10)

    def test_network_boundary_blocks_caught_attempts_and_other_providers(self):
        with network_boundary(['jev']) as attempts:
            with self.assertRaises(ClassifierError):
                requests.post('https://example.invalid',stream=True,allow_redirects=False)
            with self.assertRaises(ClassifierError):
                socket.getaddrinfo('example.invalid',443)
        self.assertEqual(len(attempts),2)

    def test_reports_do_not_overwrite_and_annotation_checklist_has_72_rows(self):
        report=evaluate(selected_items=['N001','N002'])
        with tempfile.TemporaryDirectory() as tmp:
            paths=write_report(report,tmp)
            snapshot={k:Path(p).read_bytes() for k,p in paths.items()}
            paths2=write_report(report,tmp)
            self.assertNotEqual(paths,paths2)
            self.assertEqual(snapshot,{k:Path(p).read_bytes() for k,p in paths.items()})
            self.assertEqual(json.loads(Path(paths['json']).read_text(encoding='utf-8'))['status'],'completed')
        corpus,_=load_corpus()
        review=annotation_review(corpus)
        self.assertEqual(sum(line.startswith('| N') for line in review.splitlines()),72)
        self.assertIn('Claude',review)
        report['rows'][0]['title']='<script>alert(1)</script>|`'
        self.assertNotIn('<script>',format_report(report))
        self.assertIn('规则与题库由同一作者编写，规则分数可能偏高',format_report(report))

    def test_cli_exit_status_is_not_accuracy_and_preview_writes_no_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new'
            with redirect_stdout(io.StringIO()) as output:
                code=main(['--item','N001','--report-dir',str(root),'--plan-only'])
            self.assertEqual(code,0)
            self.assertFalse(root.exists())
            self.assertEqual(json.loads(output.getvalue())['status'],'planned')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--item','N001']),0)
                self.assertEqual(main(['--item','N001','--call-limit','1']),1)
                self.assertEqual(main(['--call-limit','0']),2)

    def test_live_plan_only_uses_verified_prices_and_explicit_token_assumptions_without_credentials(self):
        with patch('trading_companion.news_triage_evaluation.build_models',side_effect=AssertionError('No clients')), \
             patch('trading_companion.local_env._read_local_values',side_effect=AssertionError('No .env')), \
             patch('requests.post',side_effect=AssertionError('No requests')), \
             patch('socket.getaddrinfo',side_effect=AssertionError('No DNS')), redirect_stdout(io.StringIO()) as output:
            code=main(['--mode','live','--classifier','jev','--classifier','deepseek','--deepseek-model','deepseek-flash',
                       '--call-limit','144','--prices','data/evaluation/model-prices.json',
                       '--assumed-input-tokens','1000','--assumed-output-tokens','100','--plan-only'])
        self.assertEqual(code,0)
        plan=json.loads(output.getvalue())
        self.assertEqual(plan['cost_preview']['jev']['amount'],'0.003024')
        self.assertEqual(plan['cost_preview']['jev']['currency'],'USD')
        self.assertEqual(plan['cost_preview']['deepseek']['amount'],'0.201600')
        self.assertEqual(plan['cost_preview']['deepseek']['currency'],'CNY')
        self.assertEqual(plan['cost_preview']['deepseek']['assumptions']['calls'],72)
        self.assertIsNone(plan['cost_preview']['deepseek']['unknown_reason'])

    def test_model_identity_mismatch_stays_unknown_and_is_visible_before_raw_report(self):
        rows, manifest=load_corpus()
        reviewed={**manifest,'annotation_status':'reviewed','reviewer':'claude'}
        def post(url,**kwargs):
            response=fixture_post('jev')(url,**kwargs)
            payload=json.loads(response.raw)
            payload['model']='jev-served-20261028'
            response.raw=json.dumps(payload).encode('utf-8')
            return response
        with patch('trading_companion.news_triage_evaluation.load_corpus',return_value=(rows,reviewed)), \
             patch.dict(os.environ,{'TYPESAFE_API_KEY':'synthetic-test-key'},clear=True), \
             patch('requests.post',side_effect=post), redirect_stderr(io.StringIO()):
            report=evaluate(mode='live',classifiers=['jev'],selected_items=['N001'],call_limit=1,
                            prices='data/evaluation/model-prices.json')
        self.assertEqual(report['status'],'completed')
        cost=report['metrics']['providers']['jev']['cost']
        self.assertIsNone(cost['estimated_total'])
        self.assertIsNone(cost['known_subtotal'])
        self.assertEqual(cost['unknown_reasons'],{'model_identity_unknown_or_mismatch':1})
        visible=format_report(report).split('## 完整指标与逐条结果')[0]
        self.assertIn('## 费用估算与模型名称',visible)
        self.assertIn('model_identity_unknown_or_mismatch',visible)
        self.assertIn('| jev | jev-latest | jev-served-20261028 | 1 | {} |',visible)
        self.assertIn('费用上限',visible)
