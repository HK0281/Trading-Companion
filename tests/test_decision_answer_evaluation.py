"""Evaluate the evaluator's failure paths and denominators without paid or source network calls."""
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_decision_answers as cli
from scripts.verify import clean_environment, offline_network
from trading_companion import decision_answer_evaluation as evaluation
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.tool_model import ToolModelResponse

PRIVATE = 'private-evaluation-error-and-draft-never-export'


class DecisionEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluation.evaluate()

    def setUp(self):
        for guard in (clean_environment(), offline_network(),
            patch('trading_companion.local_env.load_model_environment',side_effect=AssertionError('No credentials')),
            patch('trading_companion.local_env._read_local_values',side_effect=AssertionError('No .env'))):
            guard.__enter__()
            self.addCleanup(guard.__exit__,None,None,None)

    def test_frozen_twenty_questions_and_no_preset_rating(self):
        cases,manifest = evaluation.load_corpus()
        self.assertEqual(len(cases),20)
        self.assertEqual(manifest['groups'],dict.fromkeys(('buy','sell','outlook','stop'),5))
        self.assertEqual([c['id'] for c in cases if c['requires_batch_b']],['D05','D20'])
        self.assertIn('中国巨石未来一个月走势',[c['question'] for c in cases])
        self.assertGreaterEqual(len({c['industry'] for c in cases}),6)
        self.assertFalse(any('rating' in c for c in cases))
        with TemporaryDirectory() as folder:
            corpus = Path(folder)/'questions.jsonl'
            corpus.write_bytes(evaluation.CORPUS.read_bytes()+b'\n')
            with self.assertRaises((ValueError,evaluation.PlanError)):
                evaluation.load_corpus(corpus)

    def test_offline_uses_real_message_entry_and_preserves_batch_b_failures(self):
        report=self.report
        self.assertEqual((report['status'],len(report['cases'])),('passed',20))
        self.assertEqual(report['metrics']['formed'],18)
        self.assertEqual(report['metrics']['formation_rate']['denominator'],20)
        self.assertEqual(report['metrics']['reached_answer_rate']['numerator'],18)
        self.assertEqual(report['metrics']['batch_b_groups']['true']['formed'],0)
        self.assertEqual(report['metrics']['batch_b_groups']['true']['questions'],2)
        self.assertEqual(report['accounting']['forwarded'],44)  # 18×2 + 2×(3 setup + 1 target).
        self.assertFalse(report['real_model_requested'])
        self.assertFalse(report['real_data_requested'])
        fixture = report['plan']['fixture']
        self.assertEqual(fixture['version'], 'decision-fixed-data-v1')
        self.assertEqual({s['instrument_id'] for s in fixture['securities']},
                         {c['instrument_id'] for c in report['cases']})
        descriptor = {k:v for k,v in fixture.items() if k != 'sha256'}
        self.assertEqual(fixture['sha256'],
                         sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=True).encode()).hexdigest())
        self.assertEqual(fixture['decision_implementation_sha256'], report['implementation_sha256'])
        self.assertEqual(report['quality_target_claim'],'not_evaluated_offline')
        self.assertTrue(all(c['trace_complete'] for c in report['cases']))
        for c in report['cases']:
            if c['requires_batch_b']:
                self.assertEqual(c['setup'][0]['status'],'completed')
                self.assertEqual(c['route'],'technical_research')
                self.assertFalse(c['formed'])
            else:
                self.assertEqual(c['company_research']['quality']['state'],'complete')
                self.assertTrue(all(e['data_authenticity']=='fixed_synthetic'
                                    for e in c['company_research']['evidence']))

    def test_preview_has_all_questions_including_setup_with_absolute_120_cap(self):
        with patch.object(evaluation,'build_real_model',side_effect=AssertionError('No model')):
            plan=evaluation.build_plan(mode='live-model',provider='deepseek',model='deepseek-flash',
                prices=evaluation.DEFAULT_PRICES,model_call_limit=120,assumed_input_tokens=1000,assumed_output_tokens=1000)
        self.assertEqual(plan['status'],'planned')
        self.assertEqual(len(plan['questions']),20)
        self.assertEqual(plan['planned_setup_turns'],2)
        self.assertEqual(plan['typical_calls_with_one_answer_repair'],66)
        self.assertEqual(plan['worst_case_model_calls'],120)
        self.assertEqual(plan['maximum_calls_per_question'],6)
        self.assertEqual(plan['cost_preview']['amount'],'1.200000')
        self.assertEqual(plan['max_output_tokens'],1500)
        self.assertEqual(plan['data_authenticity'],'fixed_synthetic')

    def test_invalid_config_is_rejected_before_credentials_or_model_creation(self):
        for options in ({'model_call_limit':121},{'model_call_limit':True},{'model_call_limit':0},
                        {'mode':'live','provider':'deepseek','model':'deepseek-flash'},
                        {'mode':'offline','provider':'deepseek'},
                        {'mode':'live-model','provider':'openai','model':'gpt-test'},
                        {'assumed_input_tokens':-1}):
            with self.subTest(options=options),patch.object(evaluation,'build_real_model') as factory:
                result=evaluation.evaluate(**options)
                self.assertEqual(result['status'],'invalid_request')
                factory.assert_not_called()

    def test_live_preview_checks_local_directory_without_constructing_sources(self):
        cases,_=evaluation.load_corpus()
        identities={c['instrument_id']:dict(instrument_id=c['instrument_id'],name=c['security_name']) for c in cases}
        with TemporaryDirectory() as folder:
            path=Path(folder)/'directory.json'
            payload=dict(source='offline-test-snapshot',retrieved_at=datetime.now(timezone.utc).isoformat(),
                         is_fixture=False,securities=list(identities.values()))
            def preview():
                path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                with (patch.object(evaluation,'build_real_model',side_effect=AssertionError('No model')),
                      patch.object(evaluation,'live_sources',side_effect=AssertionError('No sources'))):
                    return evaluation.build_plan(mode='live',provider='deepseek',model='deepseek-flash',
                        prices=evaluation.DEFAULT_PRICES,model_call_limit=120,directory=path)
            plan=preview()
            self.assertEqual(plan['status'],'planned')
            self.assertIn('sina_news',plan['sources'])
            self.assertIsNotNone(plan['directory_sha256'])
            payload['securities'][0]['name']='规范证券名称'
            payload['securities'][0]['aliases']=['平安银行']
            self.assertEqual(preview()['status'],'planned')
            payload['is_fixture']=True
            self.assertEqual(preview()['status'],'invalid_request')
            payload['is_fixture']=False
            payload['retrieved_at']=(datetime.now(timezone.utc)-timedelta(days=9)).isoformat()
            self.assertEqual(preview()['status'],'invalid_request')

    def test_one_call_budget_keeps_nineteen_unrun_questions_in_main_denominator(self):
        report=evaluation.evaluate(model_call_limit=1)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['accounting']['forwarded'],1)
        self.assertEqual(report['metrics']['formed'],0)
        self.assertEqual(report['metrics']['not_run'],19)
        self.assertEqual(report['metrics']['formation_rate']['denominator'],20)
        self.assertEqual(len(report['cases']),20)
        self.assertTrue(all(c['code']=='evaluation_budget_exhausted' for c in report['cases'][1:]))

    def test_threshold_boundary_uses_twenty_and_separately_scores_dependencies(self):
        rows=deepcopy(self.report['cases'])
        for formed,expected in ((17,False),(18,True),(19,True)):
            for index,c in enumerate(rows):
                c['formed']=index<formed
            metrics=evaluation.summarize(rows)
            self.assertEqual(metrics['target_met'],expected)
            self.assertEqual(metrics['formation_rate']['denominator'],20)
        with self.assertRaises(evaluation.PlanError):
            evaluation.summarize(rows[:-1])

    def test_wrong_identity_horizon_facts_and_missing_reasons_never_count(self):
        case=self.report['cases'][0]
        original=dict(status='completed',company_research=deepcopy(case['company_research']))
        self.assertTrue(evaluation.grade_case(case,original)['formed'])
        for change in ('identity','horizon','facts','reason','rating'):
            result=deepcopy(original)
            company=result['company_research']
            if change=='identity': company['decisions'][0]['instrument_id']='600036.SH'
            if change=='horizon': company['decisions'][0]['horizon']='未来2个月'
            if change=='facts': company['origin']='application'
            if change=='reason': company['decisions'][0]['reasons'][0]['evidence_ids']=['unknown']
            if change=='rating': company['decisions'][0]['status']='unavailable'
            self.assertFalse(evaluation.grade_case(case,result)['formed'],change)

    def test_acceptance_version_single_reason_and_two_reason_comparison_are_distinct(self):
        self.assertEqual(self.report['acceptance_version'],'decision-acceptance-v2')
        self.assertEqual(self.report['plan']['acceptance_version'],'decision-acceptance-v2')
        case=self.report['cases'][0]
        result=dict(status='completed',company_research=deepcopy(case['company_research']))
        result['company_research']['decisions'][0]['reasons']=result['company_research']['decisions'][0]['reasons'][:1]
        grade=evaluation.grade_case(case,result)
        self.assertTrue(grade['formed'])
        self.assertFalse(grade['formed_two_reasons'])
        rows=deepcopy(self.report['cases'])
        rows[0].update(grade)
        metrics=evaluation.summarize(rows)
        self.assertEqual(metrics['formation_rate']['numerator'],18)
        self.assertEqual(metrics['two_reason_comparison_rate']['numerator'],17)

    def test_repair_rates_separate_all_passed_rating_and_content_with_old_metadata_unknown(self):
        rows=deepcopy(self.report['cases'])
        for c in rows:
            c['quality']=None
        for row,state,rating,content in zip(rows,('complete','partial','partial'),(True,True,False),(True,False,True)):
            row['quality']=dict(state=state,repair_attempts=1,reasons=[],repair_all_passed=state=='complete',
                repair_rating_restored=rating,repair_content_added=content)
        metrics=evaluation.summarize(rows)
        for field,numerator in (('repair_all_passed_rate',1),('repair_rating_restored_rate',2),('repair_content_added_rate',2)):
            self.assertEqual(metrics[field]['numerator'],numerator)
            self.assertEqual(metrics[field]['denominator'],3)
        self.assertEqual(metrics['repair_success_rate'],metrics['repair_all_passed_rate'])
        del rows[0]['quality']['repair_rating_restored']
        self.assertEqual(evaluation.summarize(rows)['repair_rating_restored_rate']['unknown'],1)

    def test_structure_failure_repairs_once_and_never_exports_raw_draft(self):
        class BrokenFirst(evaluation.DecisionContractModel):
            def __init__(self):
                super().__init__()
                self.broken=False
            def complete(self,messages,*,tools):
                if not tools and 'product-research-answer-v3' in messages[0].content and self.case['id']=='D01' and not self.broken:
                    self.broken=True
                    return ToolModelResponse('complete',PRIVATE)
                return super().complete(messages,tools=tools)
        with patch.object(evaluation,'DecisionContractModel',BrokenFirst):
            report=evaluation.evaluate()
        self.assertEqual(report['metrics']['formed'],18)
        self.assertEqual(report['metrics']['structure_parse_failure_rate']['numerator'],1)
        self.assertEqual(report['metrics']['structure_parse_failure_rate']['denominator'],19)
        self.assertEqual(report['metrics']['repair_attempts'],1)
        self.assertEqual(report['metrics']['repair_success_rate']['value'],1)
        self.assertEqual(report['metrics']['generated_paragraphs_unknown_attempts'],1)
        self.assertNotIn(PRIVATE,json.dumps(report))

    def test_partial_prose_keeps_valid_rating_and_reports_deleted_sentences(self):
        class BadNumber(evaluation.DecisionContractModel):
            def complete(self,messages,*,tools):
                response=super().complete(messages,tools=tools)
                if not tools and 'product-research-answer-v3' in messages[0].content and self.case['id']=='D01':
                    payload=json.loads(response.text)
                    payload['narratives'][0]['current']['text']+='收盘999999元。'
                    return ToolModelResponse('complete',json.dumps(payload,ensure_ascii=False))
                return response
        with patch.object(evaluation,'DecisionContractModel',BadNumber):
            report=evaluation.evaluate()
        first=report['cases'][0]
        self.assertTrue(first['formed'])
        self.assertEqual(first['quality']['state'],'partial')
        self.assertGreater(first['quality']['deleted_sentences'],0)
        self.assertEqual(report['metrics']['partial_rate']['numerator'],1)
        self.assertNotIn('999999',json.dumps(first['company_research']))

    def test_configuration_and_provider_failures_keep_all_twenty_without_private_errors(self):
        with patch.object(evaluation,'DecisionContractModel',side_effect=RuntimeError(PRIVATE)):
            report=evaluation.evaluate()
        self.assertEqual(len(report['cases']),20)
        self.assertEqual(report['metrics']['not_run'],20)
        self.assertIsNone(report['cost']['estimated_total'])
        self.assertNotIn(PRIVATE,json.dumps(report))
        class ProviderError:
            def complete(self,*args,**kwargs):
                raise ModelCallError(PRIVATE,PRIVATE)
        with patch.object(evaluation,'build_real_model',return_value=ProviderError()):
            report=evaluation.evaluate(mode='live-model',provider='deepseek',model='deepseek-flash',
                                       prices=evaluation.DEFAULT_PRICES,model_call_limit=120)
        self.assertEqual(len(report['cases']),20)
        self.assertEqual(report['metrics']['formed'],0)
        self.assertIsNone(report['cost']['estimated_total'])
        self.assertNotIn(PRIVATE,json.dumps(report))

    def test_missing_usage_is_unknown_and_complete_identity_usage_gets_known_cost(self):
        self.assertIsNone(self.report['cost']['estimated_total'])
        self.assertIsNone(self.report['accounting']['token_usage']['reported_total_tokens'])
        class Metered(evaluation.DecisionContractModel):
            def complete(self,messages,*,tools):
                response=super().complete(messages,tools=tools)
                return replace(response,usage=TokenUsage(prompt_tokens=10,completion_tokens=20,total_tokens=30),
                               provider='deepseek',requested_model='deepseek-flash',model='deepseek-flash')
        with patch.object(evaluation,'build_real_model',return_value=Metered()) as factory:
            # The fake is explicitly injected into the live-model adapter boundary; no real API call.
            delegate=factory.return_value
            original=evaluation._send
            def send(service,credentials,question,directory,seen):
                case=next(c for c in evaluation.load_corpus()[0] if question==c['question'] or question in c['setup_questions'])
                delegate.start(case,seed=question in case['setup_questions'])
                return original(service,credentials,question,directory,seen)
            with patch.object(evaluation,'_send',side_effect=send):
                report=evaluation.evaluate(mode='live-model',provider='deepseek',model='deepseek-flash',
                                           prices=evaluation.DEFAULT_PRICES,model_call_limit=120)
        self.assertTrue(report['cost']['complete'])
        self.assertEqual(report['accounting']['token_usage']['reported_total_tokens'],44*30)
        self.assertEqual(report['cost']['estimated_total'],'0.007920')

    def test_case_cap_includes_setup_and_no_outer_retry(self):
        calls=[]
        class Greedy:
            def complete(self,*args,**kwargs):
                calls.append(True)
                raise ModelCallError('timeout','private')
        meter=evaluation.TimedMeter(Greedy(),120)
        local=evaluation.EvaluationModelMeter(meter,6)
        for _ in range(10):
            with self.assertRaises(ModelCallError):
                local.complete([],tools=())
        self.assertEqual((len(calls),local.blocked,meter.forwarded),(6,4,6))

    def test_report_has_all_rows_checked_evidence_and_pending_manual_judgments(self):
        with TemporaryDirectory() as folder:
            destination=Path(evaluation.write_report(self.report,folder))
            saved=json.loads((destination/'report.json').read_text(encoding='utf-8'))
            review=[json.loads(s) for s in (destination/'manual-review.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(saved['cases']),20)
            self.assertEqual(len(review),20)
            self.assertIsNone(review[0]['review']['evidence_support'])
            self.assertTrue(review[0]['company_research']['evidence'])
            markdown=(destination/'report.md').read_text(encoding='utf-8')
            self.assertIn('18/20',markdown)
            self.assertIn('D20',markdown)

    def test_cli_plan_and_output_path_validation_are_side_effect_free(self):
        output=io.StringIO()
        with patch.object(cli,'evaluate',side_effect=AssertionError('No execution')),redirect_stdout(output):
            code=cli.main(['--plan-only'])
        self.assertEqual(code,0)
        self.assertEqual(len(json.loads(output.getvalue())['questions']),20)
        with TemporaryDirectory() as folder:
            path=Path(folder)/'file'
            path.write_text('file',encoding='utf-8')
            with patch.object(cli,'evaluate') as run,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main(['--report-dir',str(path)])
                run.assert_not_called()
