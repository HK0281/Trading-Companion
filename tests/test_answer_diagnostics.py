"""Raw drafts are opt-in local artifacts, never product/report/trace fields."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.answer_diagnostics import (ROOT,AnswerDiagnostics,bind_diagnostics,check_diagnostics_root)
from trading_companion.product_answer import synthesize_product_answer
from trading_companion.turn_trace import JsonlTraceSink,trace_message
from trading_companion.decision_answer_evaluation import evaluate,build_plan
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_decision_answer import decision_bundle

PRIVATE='private-raw-draft-999999'


class AnswerDiagnosticTests(unittest.TestCase):
    def run_bad(self):
        def change(payload,n,context):
            payload['narratives'][0]['current']={'text':PRIVATE+'元。'}
        return synthesize_product_answer('分析走势',decision_bundle(),model=NarrativeModel(change),repair_invalid=True)

    def test_disabled_does_not_construct_files_and_public_output_never_contains_raw(self):
        with patch.object(AnswerDiagnostics,'emit',side_effect=AssertionError('No collector')):
            run=self.run_bad()
        self.assertNotIn(PRIVATE,json.dumps(run.answer.to_dict()))

    def test_enabled_captures_each_draft_feedback_and_real_numeric_rejections_only_locally(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=ROOT) as folder,TemporaryDirectory() as trace:
            collector=AnswerDiagnostics(folder)
            with bind_diagnostics(collector),trace_message(JsonlTraceSink(trace),mode='demo',provider='fake',history=False):
                answer=self.run_bad().answer
            summary=collector.summary()
            self.assertTrue(summary['complete'])
            records=[json.loads(s) for s in (Path(summary['directory'])/'records.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(r['kind']=='model_draft' for r in records),2)
            rejected=next(r for r in records if r['kind']=='rejected_sentence' and r['code']=='answer_numeric_unmatched')
            self.assertIn(PRIVATE,rejected['sentence'])
            self.assertTrue(rejected['numeric'])
            numbers=[t for t in rejected['numeric'] if 'candidates' in t]
            self.assertTrue(numbers[0]['candidates'])
            self.assertTrue(all('reason' in c and 'difference' in c for c in numbers[0]['candidates']))
            self.assertNotIn(PRIVATE,json.dumps(answer.to_dict()))
            self.assertNotIn(PRIVATE,''.join(p.read_text(encoding='utf-8') for p in Path(trace).glob('*.jsonl')))

    def test_limits_and_io_failure_are_explicit(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=ROOT) as folder:
            collector=AnswerDiagnostics(folder,max_records=1,max_bytes=100)
            collector.emit('one',text='x')
            collector.emit('two',text='x')
            self.assertFalse(collector.summary()['complete'])
            self.assertEqual(collector.summary()['dropped'],1)
            collector=AnswerDiagnostics(folder)
            with patch.object(Path,'mkdir',side_effect=OSError('private')):
                collector.emit('one',text='x')
            self.assertFalse(collector.summary()['complete'])
            self.assertNotIn('private',json.dumps(collector.summary()))
            collector=AnswerDiagnostics(folder)
            collector.emit('bad',value=float('nan'))
            self.assertFalse(collector.summary()['complete'])

    def test_supplied_context_and_plan_correction_stay_in_explicit_local_diagnostics(self):
        bundle=decision_bundle()
        exit_plan=next(p for p in bundle.companies[0].trade_plans if p.purpose=='holding_exit')
        def change(payload,n,context):
            payload['decisions'][0].update(rating='持有',direction='震荡',plan_id=exit_plan.plan_id,
                invalidation=dict(text='退出观察位'+dict(exit_plan.values)['exit_below']+'元。'))
        model=NarrativeModel(change)
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=ROOT) as folder,TemporaryDirectory() as trace:
            collector=AnswerDiagnostics(folder)
            with bind_diagnostics(collector),trace_message(JsonlTraceSink(trace),mode='demo',provider='fake',history=False):
                answer=synthesize_product_answer('分析走势',bundle,model=model,repair_invalid=False).answer
            records=[json.loads(s) for s in (collector.directory/'records.jsonl').read_text(encoding='utf-8').splitlines()]
            snapshot=next(r['context'] for r in records if r['kind']=='model_context')
            self.assertEqual(snapshot,json.loads(model.calls[0][2].content.split('\n',1)[1]))
            correction=next(r for r in records if r['kind']=='plan_purpose_corrected')
            self.assertEqual(correction['original_plan_id'],exit_plan.plan_id)
            self.assertEqual(correction['selected_plan_id'],answer.decisions[0]['plan_id'])
            self.assertEqual(answer.decisions[0]['rating'],'持有')
            self.assertEqual(answer.decisions[0]['invalidation_origin'],'application')
            public=json.dumps(answer.to_dict())
            ordinary=''.join(p.read_text(encoding='utf-8') for p in Path(trace).glob('*.jsonl'))
            for private_field in ('model_context','plan_purpose_corrected','original_plan_id','"numbers"'):
                self.assertNotIn(private_field,public)
                self.assertNotIn(private_field,ordinary)
            self.assertTrue(collector.summary()['complete'])

    def test_resolved_links_cannot_escape_allowed_tree_and_bad_diagnostics_fail_evaluation(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory() as outside,TemporaryDirectory(dir=ROOT) as folder:
            # Simulate the result of resolving a symlink/junction without requiring
            # Windows symlink privileges or skipping the security check.
            with patch.object(Path,'resolve',return_value=Path(outside)):
                with self.assertRaises(ValueError):
                    check_diagnostics_root(Path(folder)/'link')
            collector=AnswerDiagnostics(folder)
            collector.failed=True
            with patch('trading_companion.decision_answer_evaluation.AnswerDiagnostics',return_value=collector):
                result=evaluate(diagnostics_dir=folder,model_call_limit=2)
            self.assertEqual(result['code'],'evaluation_diagnostics_incomplete')
            self.assertFalse(result['diagnostics']['complete'])

    def test_invalid_reference_rejection_has_local_field_location(self):
        from trading_companion.answer_validation import check_claim
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=ROOT) as folder:
            collector=AnswerDiagnostics(folder)
            with bind_diagnostics(collector):
                check_claim(dict(text=PRIVATE,evidence_ids=['unknown']),instrument='000001.SZ',section='current',index=2,
                            context=dict(evidence=[dict(id='e1',instrument_id='000001.SZ')]))
            record=json.loads((collector.directory/'records.jsonl').read_text(encoding='utf-8'))
            self.assertEqual((record['kind'],record['section'],record['item_index']),('rejected_field','current',2))
            self.assertEqual(record['sentence'],PRIVATE)

    def test_path_and_preview_validation_precede_execution(self):
        with TemporaryDirectory() as outside:
            with self.assertRaises(ValueError):
                check_diagnostics_root(outside)
            self.assertEqual(build_plan(diagnostics_dir=outside)['status'],'invalid_request')
        target=ROOT/'a2-preview-never-created'
        self.assertFalse(target.exists())
        plan=build_plan(diagnostics_dir=target)
        self.assertEqual(plan['status'],'planned')
        self.assertTrue(plan['diagnostics_enabled'])
        self.assertFalse(target.exists())

    def test_evaluation_binds_case_ids_but_keeps_raw_out_of_formal_report(self):
        from trading_companion.decision_answer_evaluation import DecisionContractModel
        class BadModel(DecisionContractModel):
            def complete(self,messages,*,tools):
                response=super().complete(messages,tools=tools)
                if not tools and 'product-research-answer-v3' in messages[0].content:
                    payload=json.loads(response.text)
                    payload['narratives'][0]['current']={'text':PRIVATE+'元。'}
                    from trading_companion.tool_model import ToolModelResponse
                    return ToolModelResponse('complete',json.dumps(payload,ensure_ascii=False))
                return response
        ROOT.mkdir(parents=True,exist_ok=True)
        with TemporaryDirectory(dir=ROOT) as folder,patch('trading_companion.decision_answer_evaluation.DecisionContractModel',BadModel):
            result=evaluate(diagnostics_dir=folder,model_call_limit=3)
            diagnostic=result['diagnostics']
            self.assertTrue(diagnostic['complete'])
            rows=[json.loads(s) for s in (Path(diagnostic['directory'])/'records.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(all(r.get('case_id')=='D01' for r in rows))
            self.assertNotIn(PRIVATE,json.dumps(result))
