"""Synthetic sealed-like fixtures only. No real sealed path, corpus, or run directory is used."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from math import ceil
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluation_tools import evaluate_research_intents_v25_holdout as command
from evaluation_tools import research_intent_v25_holdout as holdout
from evaluation_tools import research_intent_v25_evaluation as evaluation
from test_deepseek_tool_model import provider_call, provider_payload
from test_research_intent_v2 import draft, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.intent_router_task import digest
from trading_companion.model import ModelCallError
from trading_companion.typesafe_jev import ClassifierError


def synthetic_rows(n=2, semantic=None):
    semantic = n if semantic is None else semantic
    rows = []
    for index in range(1, n+1):
        base = sample()
        base.pop('dataset')
        base.update(id=f'V25H{index:03}', split='holdout', family_id='independent-synthetic',
            question='请研究平安银行'+('，观察一下' if index%2 else ''),
            evidence=dict(mentions=['平安银行'], horizon=[], vague_horizon=[], dates=[], indicators=[], exclusions=[]),
            reason='独立合成入口测试，不来自检验题', tags=[], source=dict(kind='synthetic', reference='independent-test'),
            probe=dict(steps=[], unmeasured_reason='合成题库只检查运行机制'), fault=None,
            variant_group='independent-'+str(index), coverage={}, setup_id='independent-snapshot')
        if index > semantic:
            base['gold']['zero_intent_calls'] = True  # Used only by independent scoring tests.
        rows.append(base)
    return rows


def synthetic_thresholds(rows):
    summary = holdout.cases.population_summary(rows)
    return dict(all=dict(n=len(rows), minimum_correct=ceil(.95*len(rows))),
        implementation_unseen=dict(n=len(rows), minimum_fraction=.95),
        semantic=dict(n=summary['semantic_calls'], minimum_correct=ceil(.95*summary['semantic_calls'])),
        families={f:dict(n=n,minimum_correct=ceil(.8*n)) for f,n in summary['families'].items() if n>=4},
        small_family_rule='小族只报告', common_fields_not_worse=True, measurable_red_line_violations=0,
        failed_unmeasured_invalid_in_denominator=True, maximum_target_attempts=len(rows),
        output_views=['raw_original','normalized_original','normalized_approved'])


class SyntheticModel:
    def __init__(self, *, live_envelope=False, fail_first=False, root=None):
        self.calls, self.live_envelope, self.fail_first, self.root = 0, live_envelope, fail_first, root

    def complete(self, messages, *, tools):
        self.calls += 1
        if self.root:
            path = self.root/'.agentbus/evaluation-runs/research-intent-v2.5/holdout/attempts.jsonl'
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            assert events[-1]['phase']=='reserved' and events[-1]['attempt_number']==self.calls
        if self.fail_first and self.calls%2:
            raise ModelCallError('tool_arguments_json_invalid', 'synthetic')
        return response(**(dict(provider='deepseek',requested_model='deepseek-flash',model='synthetic-only',is_mock=False)
                           if self.live_envelope else {}))


@contextmanager
def synthetic_fixture(rows=None, raw=None):
    """Use a temporary independent repository snapshot and synthetic lock/hash, never a production bypass flag."""
    rows = deepcopy(rows or synthetic_rows())
    with TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder)
        original_name = 'src/independent.py'
        for name in (*holdout.RUNNER_FILES, original_name):
            path = root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('independent synthetic file\n',encoding='utf-8')
        frozen = dict(public_files_sha256={original_name:holdout._hash(root/original_name)},
            holdout=dict(summary=holdout.cases.population_summary(rows)), thresholds=synthetic_thresholds(rows))
        raw = raw if raw is not None else b''.join(json.dumps(r,ensure_ascii=False).encode()+b'\n' for r in rows)
        frozen['holdout']['sha256'] = sha256(raw).hexdigest()
        (root/holdout.FREEZE_NAME).parent.mkdir(parents=True,exist_ok=True)
        (root/holdout.FREEZE_NAME).write_text(json.dumps(frozen,ensure_ascii=False),encoding='utf-8')
        evidence=root/'public-evidence.json'
        evidence.write_text('{}',encoding='utf-8')
        candidate=dict(revision=holdout.CANDIDATE_REVISION,contract_version=holdout.intent.VERSION,
            guard_version=holdout.intent.GUARD_VERSION,
            prompt=dict(text=holdout.intent.INSTRUCTION,sha256_utf8=sha256(holdout.intent.INSTRUCTION.encode()).hexdigest()),
            tool=dict(definition=holdout.intent.TOOL.to_dict(),sha256=digest(holdout.intent.TOOL.to_dict())),
            model=deepcopy(holdout.MODEL_POLICY), source_sha256_lf={original_name:holdout._hash(root/original_name)},
            freeze=dict(path=holdout.FREEZE_NAME,sha256_lf=holdout._hash(root/holdout.FREEZE_NAME),
                public_files_sha256=frozen['public_files_sha256'],holdout_sha256=sha256(raw).hexdigest(),
                summary=frozen['holdout']['summary'],thresholds=frozen['thresholds']),
            exposure=dict(implementation_target_ids=[],reference_excluded_ids=list(holdout.REFERENCE_EXCLUDED),
                approval_message=420,isolation_statement=holdout.ISOLATION,automatic_status_exception=holdout.STATUS_EXCEPTION),
            development_evidence=dict(reports={'synthetic':dict(path='public-evidence.json',sha256=sha256(b'{}').hexdigest())}))
        lock=dict(version=holdout.LOCK_VERSION,status='locked',candidate=candidate,candidate_sha256=digest(candidate),
            runner=dict(version=holdout.RUNNER_VERSION,plan_approval_message=422,
                source_sha256_lf={p:holdout._hash(root/p) for p in holdout.RUNNER_FILES}),live_admitted=True)
        (root/holdout.LOCK_NAME).write_text(json.dumps(lock,ensure_ascii=False),encoding='utf-8')
        path=root/'synthetic.jsonl'
        path.write_bytes(raw)
        state=SimpleNamespace(root=root,rows=rows,raw=raw,path=path,sha=sha256(raw).hexdigest(),
            candidate=candidate,lock=lock,freeze=frozen,dirty=False,revision='independent-reviewed-runtime')
        def git(*args):
            if args[0]=='rev-parse':
                return state.revision
            if args[0]=='status':
                return ' M changed' if state.dirty else ''
            if args[0]=='merge-base':
                return ''
            raise AssertionError(args)
        for name,value in dict(ROOT=root,COUNT=len(rows),SEMANTIC_COUNT=frozen['holdout']['summary']['semantic_calls'],
                               SEALED_SHA256=state.sha,CANDIDATE_SHA256=digest(candidate)).items():
            stack.enter_context(patch.object(holdout,name,value))
        stack.enter_context(patch.object(holdout,'_git',side_effect=git))
        stack.enter_context(patch.object(holdout.cases,'validate_freeze',return_value=frozen))
        stack.enter_context(patch.object(holdout,'load_corpus',return_value=([],None)))
        stack.enter_context(patch.object(evaluation.core,'prefix_library',return_value={}))
        stack.enter_context(patch.object(holdout.cases,'load_setups',return_value=dict(entries={
            'independent-snapshot':dict(kind='snapshot',history=[],note='独立合成状态')})))
        state.run=lambda **kwargs:holdout.evaluate_holdout(sealed_corpus=path,expected_sha256=state.sha,**kwargs)
        state.plan=lambda:holdout.build_plan(sealed_corpus=path,expected_sha256=state.sha)
        yield state


class AdmissionTests(OfflineTests):
    def test_real_public_lock_rejects_post_lock_workspace_without_opening_a_sealed_path(self):
        # V2.6 adds source files after the one-use v2.5 run. Keep that historical
        # lock intact and verify rejection; synthetic_fixture still checks valid
        # admission against an independent, internally consistent snapshot.
        lock=holdout.read_json(holdout.ROOT/holdout.LOCK_NAME,maximum=500_000)
        self.assertEqual(digest(lock['candidate']),holdout.CANDIDATE_SHA256)
        self.assertEqual(lock['candidate']['revision'],holdout.CANDIDATE_REVISION)
        self.assertEqual(lock['candidate']['model']['maximum_target_attempts'],120)
        self.assertEqual(lock['candidate']['freeze']['thresholds']['maximum_target_attempts'],96)
        self.assertEqual(len(lock['candidate']['freeze']['public_files_sha256']),18)
        self.assertEqual(lock['candidate']['freeze']['summary']['semantic_calls'],79)
        with patch.object(holdout,'_sealed_bytes',side_effect=AssertionError('No sealed access')), \
                patch.object(holdout,'_live_model',side_effect=AssertionError('No credentials')), \
                self.assertRaisesRegex(ValueError,r'Locked (?:file|source inventory) mismatch'):
            holdout.validate_lock()

    def test_default_plan_hashes_even_nonjson_bytes_without_decoding_constructing_or_claiming(self):
        with synthetic_fixture(raw=b'not-json\nnot-json\n') as f, \
                patch.object(holdout,'_decode_rows',side_effect=AssertionError('No decode')), \
                patch.object(holdout,'_preflight',side_effect=AssertionError('No construction')), \
                patch.object(holdout,'_live_model',side_effect=AssertionError('No credentials')), \
                patch.object(holdout.intent,'local_preflight',side_effect=AssertionError('No target')):
            plan=f.run()
            self.assertEqual(plan['physical_lines'],2)
            self.assertFalse(plan['sealed_json_decoded'])
            self.assertEqual(plan['context_preflight']['status'],'not_run')
            self.assertFalse(plan['ready_for_live'])
            self.assertEqual(plan['output_token_upper_bound'],180000)
            self.assertIsNone(plan['input_token_upper_bound'])
            self.assertIsNone(plan['estimated_cost'])
            self.assertIn('输入 token 上限未知',plan['cost_estimation'])
            self.assertFalse((f.root/'.agentbus').exists())

    def test_hash_line_count_size_and_explicit_path_fail_before_decoding(self):
        with synthetic_fixture() as f, patch.object(holdout,'_decode_rows',side_effect=AssertionError('No decode')):
            for args in (dict(sealed_corpus=f.path,expected_sha256='0'*64),
                         dict(sealed_corpus=None,expected_sha256=f.sha)):
                with self.assertRaises(ValueError):
                    holdout.evaluate_holdout(**args,live=True)
            f.path.write_bytes(f.raw.replace(b'\n',b'\r\n'))
            with self.assertRaisesRegex(ValueError,'SHA256'):
                f.run(live=True)
        with synthetic_fixture(raw=b'one-line\n') as f:
            with self.assertRaisesRegex(ValueError,'line count'):
                f.run()
        with synthetic_fixture() as f, patch.object(holdout,'MAX_CORPUS_BYTES',10):
            with self.assertRaisesRegex(ValueError,'size limit'):
                f.run()

    def test_dirty_repository_only_blocks_live_and_source_runner_freeze_evidence_changes_block_both(self):
        with synthetic_fixture() as f:
            f.dirty=True
            self.assertFalse(f.plan()['metadata_ready'])
            with self.assertRaisesRegex(ValueError,'clean'):
                f.run(live=True)
        for name in ('src/independent.py',holdout.RUNNER_FILES[0],holdout.FREEZE_NAME):
            with synthetic_fixture() as f:
                (f.root/name).write_text('changed',encoding='utf-8')
                with self.subTest(name=name),self.assertRaises(ValueError):
                    f.plan()
        with synthetic_fixture() as f:
            (f.root/'src/extra.py').write_text('new',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'inventory'):
                f.plan()
        with synthetic_fixture() as f:
            (f.root/'public-evidence.json').unlink()
            self.assertEqual(f.plan()['status'],'planned')  # Qualification is pinned in the reviewed lock.

    def test_changed_lock_threshold_policy_exposure_and_nonancestor_are_rejected(self):
        for field,value in (('model',dict(holdout.MODEL_POLICY,maximum_target_attempts=121)),
                            ('exposure',dict(implementation_target_ids=['V25H001'])),
                            ('contract_version','other')):
            with synthetic_fixture() as f:
                f.lock['candidate'][field]=value
                (f.root/holdout.LOCK_NAME).write_text(json.dumps(f.lock),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'candidate lock'):
                    f.plan()
        with synthetic_fixture() as f, patch.object(holdout,'_git',side_effect=subprocess.CalledProcessError(1,['git'])):
            with self.assertRaises(subprocess.CalledProcessError):
                f.plan()

    def test_explicit_cli_has_no_override_or_abbreviation_and_plan_only_is_byte_only(self):
        with synthetic_fixture() as f, redirect_stdout(StringIO()) as output:
            result=command.main(['--sealed-corpus',str(f.path),'--expected-sha256',f.sha])
            self.assertEqual(result,0)
            self.assertFalse(json.loads(output.getvalue())['sealed_json_decoded'])
        for args in ([],['--sealed','x'],['--sealed-corpus','x','--expected-sha256','a','--report-dir','x'],
                     ['--sealed-corpus','x','--expected-sha256','a','--live','--plan-only']):
            with redirect_stderr(StringIO()),self.assertRaises(SystemExit) as caught:
                command.main(args)
            self.assertEqual(caught.exception.code,2)
        with synthetic_fixture() as f, redirect_stdout(StringIO()) as output:
            self.assertEqual(command.main(['--sealed-corpus',str(f.path),'--expected-sha256','0'*64]),2)
            self.assertEqual(json.loads(output.getvalue())['paid_calls'],0)

    def test_live_checks_population_and_all_contexts_before_claim_or_factory_and_never_uses_gold_to_build(self):
        with synthetic_fixture(raw=b'not-json\nnot-json\n') as f,patch.object(holdout,'_live_model') as factory:
            with self.assertRaises(ClassifierError):
                f.run(live=True)
            factory.assert_not_called()
            self.assertFalse((f.root/'.agentbus').exists())
        with synthetic_fixture() as f,patch.object(evaluation,'build_context',side_effect=ValueError('synthetic failure')), \
                patch.object(holdout,'_live_model') as factory:
            with self.assertRaisesRegex(ValueError,'All frozen'):
                f.run(live=True)
            factory.assert_not_called()
            self.assertFalse((f.root/'.agentbus').exists())
        with synthetic_fixture() as f:
            builder=holdout.cases.build_context
            captured=[]
            def capture(state,*args):
                captured.append(deepcopy(state))
                return builder(state,*args)
            with patch.object(holdout.cases,'build_context',side_effect=capture), \
                    patch.object(holdout,'_live_model',return_value=SyntheticModel(root=f.root)):
                report=f.run(live=True)
            self.assertEqual(report['status'],'completed')
            self.assertEqual(report['plan']['context_preflight']['buildable'],2)
            for state in captured[:2]:
                self.assertEqual(set(state),{'question','context','setup_id'})
                self.assertNotIn(state['question'],[r['question'] for r in f.rows])

    def test_population_ids_and_summary_must_match_and_drift_after_preflight_never_claims(self):
        with synthetic_fixture() as f:
            wrong=deepcopy(f.rows)
            wrong[1]['id']=wrong[0]['id']
            with self.assertRaisesRegex(ValueError,'population'):
                holdout._check_population(wrong,f.candidate)
            wrong=deepcopy(f.rows)
            wrong[0]['gold']['task']='clarify'
            with self.assertRaisesRegex(ValueError,'population'):
                holdout._check_population(wrong,f.candidate)
            original=holdout._preflight
            def change(*args):
                result=original(*args)
                (f.root/'src/independent.py').write_text('changed',encoding='utf-8')
                return result
            with patch.object(holdout,'_preflight',side_effect=change),patch.object(holdout,'_live_model') as factory:
                with self.assertRaises(holdout.RunInvalidated):
                    f.run(live=True)
                factory.assert_not_called()
            self.assertFalse((f.root/'.agentbus').exists())


class ExecutionTests(OfflineTests):
    def test_success_is_one_use_and_every_request_has_a_durable_reservation(self):
        with synthetic_fixture() as f:
            model=SyntheticModel(root=f.root)
            with patch.object(holdout,'_live_model',return_value=model) as factory:
                result=f.run(live=True)
                self.assertEqual((result['status'],result['paid_calls'],model.calls),('completed',2,2))
                self.assertFalse(result['passed'])
                self.assertEqual(result['authenticity'],'mock_experiment')
                with self.assertRaisesRegex(ValueError,'already used'):
                    f.run(live=True)
                factory.assert_called_once()
            folder=Path(result['artifacts'])
            self.assertTrue((folder/'reserved.json').exists())
            self.assertTrue((folder/'completed.json').exists())
            events=[json.loads(s) for s in (folder/'attempts.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([e['phase'] for e in events],['reserved','finished','reserved','finished'])
            self.assertEqual(result['plan']['context_preflight']['target_calls'],0)
            self.assertIn('#396', (folder/'report.md').read_text(encoding='utf-8'))

    def test_second_format_attempt_reuses_messages_and_can_only_repair_a_single_bare_scalar(self):
        valid=json.dumps(draft())
        repair=valid.replace('"objective_kind": "analysis"','"objective_kind": analysis')
        def payload(text):
            return provider_payload(finish='tool_calls',content=None,calls=[provider_call(name=holdout.intent.TOOL_NAME,arguments=text)])
        for second,expected in ((repair,'ok'),('```json\n'+valid+'\n```','failed')):
            with synthetic_fixture(synthetic_rows(1)) as f:
                model=DeepSeekToolModel(DeepSeekConfig('deepseek-flash','synthetic-only'),temperature=0,
                    response_diagnostics=True,repair_tool_arguments=True,argument_diagnostic_sink=evaluation._argument_diagnostic,
                    evaluation_scope='sealed',save_failed_arguments=True)
                with patch.object(holdout,'_live_model',return_value=model), \
                        patch('trading_companion.deepseek_tool_model._post_chat',side_effect=[payload('{"x":'),payload(second)]) as post:
                    report=f.run(live=True)
                result=report['rows'][0]['V2']
                self.assertEqual((result['status'],result['attempts'],post.call_count),(expected,2,2))
                self.assertEqual(post.call_args_list[0],post.call_args_list[1])
                self.assertEqual(result['raw']['status'],'failed')
                self.assertEqual(report['telemetry']['missing_responses'],1 if expected=='ok' else 2)
                self.assertIsNone(report['telemetry']['tokens']['total_tokens']['complete_total'])
                if expected=='ok':
                    self.assertEqual(report['tool_arguments_repairs']['by_category']['unquoted_scalar']['count'],1)

    def test_network_semantic_and_timeout_failures_never_format_retry(self):
        for failure in ('timeout','connection_error','refused'):
            with synthetic_fixture(synthetic_rows(1)) as f:
                model=Mock(complete=Mock(side_effect=ModelCallError(failure,'synthetic-private-error')))
                with patch.object(holdout,'_live_model',return_value=model):
                    report=f.run(live=True)
                self.assertEqual((model.complete.call_count,report['paid_calls'],report['format_retries']['total']),(1,1,0))
                self.assertNotIn('synthetic-private-error',json.dumps(report))
        with synthetic_fixture(synthetic_rows(1)) as f:
            model=Mock(complete=Mock(return_value=response(draft(objective_phrase='fabricated'))))
            with patch.object(holdout,'_live_model',return_value=model):
                report=f.run(live=True)
            self.assertEqual(report['rows'][0]['V2']['status'],'invalid')
            model.complete.assert_called_once()

    def test_global_120_cap_includes_retries_and_no_third_attempt_or_denominator_shrink(self):
        with synthetic_fixture(synthetic_rows(61)) as f:
            model=SyntheticModel(fail_first=True,root=f.root)
            with patch.object(holdout,'_live_model',return_value=model):
                report=f.run(live=True)
            self.assertEqual((model.calls,report['paid_calls'],len(report['rows'])),(120,120,61))
            self.assertEqual(report['rows'][-1]['V2']['status'],'not_measured')
            self.assertTrue(all(r['V2']['attempts']<=2 for r in report['rows']))
            self.assertEqual(report['format_retries']['total'],60)
            self.assertEqual(report['score_views']['normalized_approved']['all']['n'],61)

    def test_factory_failure_and_interrupt_are_permanent_claims_with_safe_error_and_full_denominator(self):
        for error in (ValueError('credential-private-details'),KeyboardInterrupt()):
            with synthetic_fixture() as f,patch.object(holdout,'_live_model',side_effect=error):
                report=f.run(live=True)
                self.assertEqual((report['status'],report['paid_calls'],len(report['rows'])),('aborted',0,2))
                self.assertFalse(report['passed'])
                self.assertTrue(all(r['V2']['status']=='not_measured' for r in report['rows']))
                self.assertNotIn('credential-private-details',json.dumps(report))
                with self.assertRaisesRegex(ValueError,'already used'):
                    f.run(live=True)

    def test_preexisting_dataset_record_blocks_a_new_version_directory_and_default_remains_read_only(self):
        with synthetic_fixture() as f:
            folder,usage=holdout._paths(f.candidate)
            usage.parent.mkdir(parents=True)
            usage.write_text('{}\n',encoding='utf-8')
            with patch.object(holdout,'_live_model') as factory:
                with self.assertRaisesRegex(ValueError,'already used'):
                    f.run(live=True)
                factory.assert_not_called()
            self.assertFalse(folder.exists())
            plan=f.plan()
            self.assertFalse(plan['run_available'])
            self.assertFalse(plan['sealed_json_decoded'])

    def test_revision_drift_before_retry_stops_the_run_without_forwarding_second_request(self):
        with synthetic_fixture() as f:
            def fail(*args,**kwargs):
                f.revision='changed'
                raise ModelCallError('tool_arguments_json_invalid','synthetic')
            model=Mock(complete=Mock(side_effect=fail))
            with patch.object(holdout,'_live_model',return_value=model):
                report=f.run(live=True)
            self.assertEqual(report['status'],'aborted')
            self.assertEqual(model.complete.call_count,1)
            self.assertEqual(report['provider_calls_forwarded'],1)
            self.assertEqual(report['rows'][0]['V2']['attempts'],2)
            self.assertFalse(report['passed'])
            self.assertFalse((Path(report['artifacts'])/'completed.json').exists())

    def test_late_revision_drift_replaces_an_already_passing_report(self):
        with synthetic_fixture() as f:
            original_git=holdout._git
            saw_passing=[]
            def git(*args):
                path=f.root/'.agentbus/evaluation-runs/research-intent-v2.5/holdout/report.json'
                if args[0]=='rev-parse' and path.exists():
                    saw_passing.append(json.loads(path.read_text(encoding='utf-8'))['passed'])
                    return 'changed'
                return original_git(*args)
            with patch.object(holdout,'_git',side_effect=git), \
                    patch.object(holdout,'_live_model',return_value=SyntheticModel(live_envelope=True)):
                report=f.run(live=True)
            self.assertTrue(saw_passing[0])
            self.assertEqual(report['status'],'aborted')
            saved=json.loads((Path(report['artifacts'])/'report.json').read_text(encoding='utf-8'))
            self.assertFalse(saved['passed'])
            self.assertIn('"status": "aborted"',(Path(report['artifacts'])/'report.md').read_text(encoding='utf-8'))

    def test_attempt_journal_write_failure_cannot_be_swallowed_into_an_ordinary_failed_row(self):
        with synthetic_fixture() as f:
            append=holdout._append
            failed=[]
            def write(stream,value):
                if value.get('phase')=='finished' and not failed:
                    failed.append(True)
                    raise OSError('synthetic-write-failure')
                return append(stream,value)
            with patch.object(holdout,'_append',side_effect=write), \
                    patch.object(holdout,'_live_model',return_value=SyntheticModel()) as factory:
                report=f.run(live=True)
            self.assertEqual(report['status'],'aborted')
            self.assertEqual(report['attempts_reserved'],1)
            self.assertEqual(factory.return_value.calls,1)

    def test_reservation_write_failure_forwards_no_request_and_remains_occupied(self):
        with synthetic_fixture() as f:
            append=holdout._append
            failed=[]
            def write(stream,value):
                if value.get('phase')=='reserved' and not failed:
                    failed.append(True)
                    raise OSError('synthetic-reservation-failure')
                return append(stream,value)
            model=SyntheticModel()
            with patch.object(holdout,'_append',side_effect=write),patch.object(holdout,'_live_model',return_value=model):
                report=f.run(live=True)
            self.assertEqual((report['status'],model.calls,report['attempts_reserved']),('aborted',0,1))
            self.assertEqual(report['rows'][0]['V2']['attempts'],1)
            self.assertEqual(report['unfinished_attempts'],1)

    def test_concurrent_claims_allow_only_one_execution_and_factory(self):
        with synthetic_fixture() as f:
            plan=f.plan()
            barrier=Barrier(2)
            def run():
                barrier.wait(timeout=5)
                try:
                    return holdout._execute(f.rows,plan=plan,candidate=f.candidate,prefixes={},
                        setups=dict(entries={'independent-snapshot':dict(kind='snapshot',history=[],note='合成')}),recheck=lambda:None)['status']
                except (ValueError,FileExistsError):
                    return 'rejected'
            with patch.object(holdout,'_live_model',return_value=SyntheticModel()) as factory,ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda _:run(),range(2)))
            self.assertEqual(sorted(results),['completed','rejected'])
            factory.assert_called_once()

    def test_sealed_factory_uses_exact_candidate_settings_and_is_not_a_public_scope_factory(self):
        with patch('trading_companion.local_env.load_evaluation_key_environment',
                   return_value={'DEEPSEEK_API_KEY':'synthetic-only'}):
            model=holdout._live_model()
        self.assertEqual((model.evaluation_scope,model.temperature,model.config.max_output_tokens),('sealed',0,1500))
        self.assertTrue(model.save_failed_arguments)
        self.assertTrue(model.repair_tool_arguments)


def scored_rows(rows, *, mock=False):
    envelope=response(provider='deepseek',requested_model='deepseek-flash',model='synthetic-only',is_mock=mock).to_dict()
    return [dict(deepcopy(r),dataset='holdout',effective_gold=deepcopy(r['gold']),A=dict(status='ok',fields=deepcopy(r['gold'])),
        V2=dict(evaluation._empty('ok',None),fields=deepcopy(r['gold']),raw=dict(status='ok',fields=deepcopy(r['gold'])),
                attempts=0 if r['gold']['zero_intent_calls'] else 1,
                response=None if r['gold']['zero_intent_calls'] else deepcopy(envelope))) for r in rows]


class ScoringTests(OfflineTests):
    def report(self,fixture,rows):
        return holdout.make_report(rows,plan=fixture.plan(),candidate=fixture.candidate,started_at='synthetic-start',ended_at='synthetic-end')

    def test_96_79_87_denominators_three_views_and_frozen_family_gates(self):
        with synthetic_fixture(synthetic_rows(96,79)) as f:
            report=self.report(f,scored_rows(f.rows))
            self.assertTrue(report['passed'])
            self.assertEqual((report['gates']['all']['n'],report['gates']['all']['minimum_correct']),(96,92))
            self.assertEqual((report['gates']['semantic']['n'],report['gates']['semantic']['minimum_correct']),(79,76))
            self.assertEqual(report['reference']['n'],87)
            self.assertFalse(report['reference']['participates_in_gate'])
            self.assertEqual(set(report['score_views']),{'raw_original','normalized_original','normalized_approved'})
            self.assertIn('#396',report['automatic_status_exception'])
            self.assertIn('独立 Codex 会话',report['isolation_statement'])

    def test_reference_exclusion_never_changes_main_gates_and_failures_stay_in_denominators(self):
        with synthetic_fixture(synthetic_rows(96,79)) as f:
            rows=scored_rows(f.rows)
            for row in rows:
                if row['id'] in holdout.REFERENCE_EXCLUDED:
                    row['V2']=evaluation._empty('failed','synthetic')
            report=self.report(f,rows)
            self.assertFalse(report['passed'])
            self.assertEqual(report['gates']['all']['n'],96)
            self.assertEqual(report['score_views']['normalized_approved']['reference_without_reviewer_mentions']['joint_all_fields']['numerator'],87)
            self.assertFalse(report['gates']['all']['passed'])
            self.assertEqual(report['gates']['semantic']['n'],79)

    def test_semantic_gate_is_fixed_and_raw_format_failure_is_not_hidden(self):
        with synthetic_fixture(synthetic_rows(96,79)) as f:
            rows=scored_rows(f.rows)
            for row in rows[:4]:
                row['V2']=evaluation._empty('not_measured','synthetic')
            report=self.report(f,rows)
            self.assertTrue(report['gates']['all']['passed'])
            self.assertFalse(report['gates']['semantic']['passed'])
            self.assertFalse(report['passed'])
            rows=scored_rows(f.rows)
            rows[0]['V2']['raw']=dict(status='failed',fields={})
            report=self.report(f,rows)
            self.assertEqual(report['score_views']['raw_original']['all']['joint_all_fields']['numerator'],95)
            self.assertEqual(report['score_views']['normalized_approved']['all']['joint_all_fields']['numerator'],96)

    def test_family_at_least_four_uses_ceil_eighty_percent_and_smaller_families_are_report_only(self):
        rows=synthetic_rows(96,79)
        for index,row in enumerate(rows):
            row['family_id']='four-case-family' if index<4 else 'three-case-family' if index<7 else 'large-family'
        with synthetic_fixture(rows) as f:
            scored=scored_rows(f.rows)
            scored[0]['V2']=evaluation._empty('failed','synthetic')
            report=self.report(f,scored)
            self.assertTrue(report['gates']['all']['passed'])
            self.assertTrue(report['gates']['semantic']['passed'])
            self.assertEqual(report['gates']['family:four-case-family']['minimum_correct'],4)
            self.assertFalse(report['gates']['family:four-case-family']['passed'])
            self.assertNotIn('family:three-case-family',report['gates'])
            self.assertEqual(report['score_views']['normalized_approved']['family:three-case-family']['n'],3)

    def test_common_field_regression_redline_mock_and_no_provider_observation_never_pass(self):
        with synthetic_fixture(synthetic_rows(96,79)) as f:
            rows=scored_rows(f.rows)
            rows[0]['V2']['fields']['task']='clarify'
            self.assertFalse(self.report(f,rows)['passed'])
            rows=scored_rows(f.rows)
            rows[0]['V2']['fields']['bound_subjects']=['wrong-subject']
            self.assertFalse(self.report(f,rows)['zero_tolerance']['passed'])
            self.assertFalse(self.report(f,scored_rows(f.rows,mock=True))['passed'])
            rows=scored_rows(f.rows)
            for row in rows:
                row['V2']['response']=None
            self.assertEqual(self.report(f,rows)['authenticity'],'no_provider_observation')
            self.assertFalse(self.report(f,rows)['passed'])


if __name__=='__main__':
    unittest.main()
