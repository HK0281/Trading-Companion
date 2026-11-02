"""Public cases and synthetic mutations only; never open a new sealed packet."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v26_cases as cases


class V26AdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev=cases.load_cases()

    def write_json(self, folder, value, name='independent.json'):
        path=Path(folder)/name
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
        return path

    def pending_amendments(self):
        value=cases.read_json(cases.AMENDMENTS)
        for item in value['changes']:
            item.update(status='pending',reviewer=None,approval_message=None)
        return value

    def test_public_inventory_has_all_families_and_fourteen_scoring_fields(self):
        self.assertEqual(len(self.dev),64)
        self.assertEqual(len({r['family_id'] for r in self.dev}),48)
        for row in self.dev:
            self.assertEqual(set(row['gold']),set(cases.GOLD_FIELDS))
            self.assertNotIn('reference',row['gold'])
            cases.valid_new_row(row)
        self.assertEqual(cases.load_manifest(self.dev)['review'],
                         dict(status='approved',reviewed_count=64,pending_count=0))

    def test_reference_combinations_reject_bool_negative_unknown_and_extra_fields(self):
        for value in (
            dict(kind='none',index=1,scope=None),dict(kind='none',index=None,scope='pending'),
            dict(kind='ordinal',index=True,scope='shortlist'),dict(kind='ordinal',index=0,scope='shortlist'),
            dict(kind='ordinal',index=-1,scope='shortlist'),dict(kind='ordinal',index=1.0,scope='shortlist'),
            dict(kind='latest',index=1,scope='latest_research'),dict(kind='all',index=None,scope=None),
            dict(kind='latest',index=None,scope='history'),dict(kind='none',index=None,scope=None,code='000001.SZ'),
        ):
            with self.subTest(value=value),self.assertRaises(ValueError):
                cases.valid_reference(value)
        for kind in ('latest','all','ordinal'):
            for scope in ('latest_research','shortlist','pending'):
                cases.valid_reference(dict(kind=kind,index=1 if kind=='ordinal' else None,scope=scope))
        cases.valid_reference(dict(kind='none',index=None,scope=None))

    def test_reference_and_phrase_evidence_are_not_silently_repaired(self):
        for mutation in ('missing_reference','source','selection','phrase','domain','exclusion'):
            row=deepcopy(self.dev[6])
            if mutation=='missing_reference':
                row.pop('reference_expectation')
            elif mutation=='source':
                row['reference_expectation']['reference']=dict(kind='latest',index=None,scope='pending')
            elif mutation=='selection':
                row['reference_expectation']['selection']='杜撰的指代'
            elif mutation=='phrase':
                row['intent_evidence']['plan_phrase']='历史里才有的价位要求'
            elif mutation=='domain':
                row['intent_evidence']['research_kind_phrases']={'news':'新闻'}
            else:
                row['evidence']['exclusions']=['浦发银行']
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                cases.valid_new_row(row)

    def test_short_negation_markers_do_not_include_single_shao(self):
        self.assertNotIn('少',cases.NEGATION_MARKERS)
        self.assertFalse(any(mark in '多少' for mark in cases.NEGATION_MARKERS))
        for phrase in ('ATR取消','新闻这一栏无需写','财务少看','减少新闻'):
            self.assertTrue(any(mark in phrase for mark in cases.NEGATION_MARKERS))

    def test_local_audit_requires_whole_grammar_and_keeps_search_guards(self):
        positive={'command':'取消研究。','history':'查看上一次结果！','recall':'你上一轮给出的方向是什么？',
            'horizon':'未来十二个交易日？','knowledge':'介绍MACD中的DIF和DEA。',
            'guard':'研究财务，然后执行一笔订单'}
        for expected,question in positive.items():
            with self.subTest(question=question):
                self.assertEqual(cases.local_syntax(question),expected)
        for question in ('取消研究，再分析另一家','查看上次结果，并补财报','未来5天，再加MACD',
            '中线是什么','你之前说的评级是什么，再判断明天','ATR是什么，再研究银行',
            'ETF的跟踪误差是什么意思','研究中证1000指数','未来0天','未来99999天',
            '你 刚才说的方向是什么','解释MACD中的RSI'):
            with self.subTest(question=question):
                self.assertIsNone(cases.local_syntax(question))

    def test_public_newlines_and_hash_are_checked_before_decoding(self):
        raw=cases.DEV.read_bytes().replace(b'\r\n',b'\n')
        with TemporaryDirectory() as folder:
            path=Path(folder)/'dev.jsonl'
            path.write_bytes(raw.replace(b'\n',b'\r\n'))
            self.assertEqual(cases.load_cases(path,expected_sha256=cases.public_sha256(cases.DEV)),self.dev)
            with patch.object(cases,'decode',side_effect=AssertionError('hash first')):
                with self.assertRaisesRegex(ValueError,'fingerprint'):
                    cases.load_cases(path,expected_sha256='0'*64)

    def test_public_reader_rejects_holdout_rows(self):
        independent=deepcopy(self.dev)
        for row in independent:
            row['split']='holdout'
            row['id']=row['id'].replace('V26D','V26H')
        with TemporaryDirectory() as folder:
            path=Path(folder)/'independent.jsonl'
            path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in independent),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'only accepts dev'):
                cases.load_cases(path)

    def test_pending_amendments_are_auditable_but_never_apply(self):
        with TemporaryDirectory() as folder:
            path=self.write_json(folder,self.pending_amendments())
            raw,record,effective=cases.load_amendments(path)
            with self.assertRaisesRegex(ValueError,'approval required'):
                cases.load_amendments(path,require_approved=True)
        _,baseline=cases.legacy_rows()
        self.assertEqual(len(raw),414)
        self.assertEqual(effective,baseline)
        self.assertTrue(record['changes'])
        self.assertTrue(all(x['status']=='pending' for x in record['changes']))
        self.assertEqual(next(x for x in record['changes'] if x['id']=='V25H007' and x['field']=='zero_intent_calls')['after'],False)

    def test_approved_amendments_apply_only_the_reviewed_fields(self):
        raw,record,effective=cases.load_amendments(require_approved=True)
        _,baseline=cases.legacy_rows()
        self.assertEqual(len(raw),414)
        self.assertEqual(len(record['changes']),76)
        self.assertEqual(len({item['id'] for item in record['changes']}),60)
        expected={row['id']:deepcopy(row['gold']) for row in baseline}
        for item in record['changes']:
            self.assertEqual((item['status'],item['reviewer'],item['approval_message']),
                             ('approved','claude',434))
            expected[item['id']][item['field']]=deepcopy(item['after'])
        self.assertEqual({row['id']:row['gold'] for row in effective},expected)

    def test_amendments_reject_parent_before_duplicate_type_and_approval_drift(self):
        record=self.pending_amendments()
        for mutation in ('parent','row','before','baseline','duplicate','approval','after_type'):
            value=deepcopy(record)
            if mutation=='parent':
                value['parents_sha256_lf'][next(iter(value['parents_sha256_lf']))]='0'*64
            elif mutation=='duplicate':
                value['changes'].append(deepcopy(value['changes'][0]))
            else:
                key,replacement={'row':('parent_row_sha256','0'*64),'before':('before','wrong'),
                    'baseline':('baseline_gold_sha256','0'*64),'approval':('reviewer','claude'),
                    'after_type':('after',{'arbitrary':True})}[mutation]
                value['changes'][0][key]=replacement
            with TemporaryDirectory() as folder:
                path=self.write_json(folder,value)
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                    cases.load_amendments(path)

    def test_one_approved_change_applies_without_pending_siblings(self):
        value=self.pending_amendments()
        item=next(x for x in value['changes'] if x['id']=='V25H007' and x['field']=='zero_intent_calls')
        item.update(status='approved',reviewer='claude',approval_message=1000)
        with TemporaryDirectory() as folder:
            _,_,effective=cases.load_amendments(self.write_json(folder,value))
        row=next(r for r in effective if r['id']=='V25H007')
        self.assertFalse(row['gold']['zero_intent_calls'])
        self.assertEqual(row['gold']['operation'],'local')

    def test_context_construction_never_receives_gold_or_target_text(self):
        rows=[self.dev[i-1] for i in (1,12,19,20,24,44,62)]
        original=cases.build_context
        captured=[]

        def capture(state,*args):
            captured.append(deepcopy(state))
            self.assertEqual(set(state),{'question','context','setup_id'})
            self.assertEqual(state['question'],'仅检查前置状态，不执行目标轮')
            return original(state,*args)

        with patch.object(cases,'build_context',side_effect=capture):
            report=cases.preflight(rows)
        self.assertEqual(len(captured),len(rows))
        self.assertTrue(report['passed'])
        self.assertEqual((report['target_calls'],report['prediction_rows'],report['score_rows']),(0,0,0))
        self.assertTrue(all(r['local_contract_matches'] for r in report['rows']))
        changed=deepcopy(rows[0])
        changed['question']='不向构建器传递的目标问题'
        changed['gold']={'zero_intent_calls':False}
        changed['id']='independent'
        a=cases.preflight([rows[0]])['rows'][0]
        b=cases.preflight([changed])['rows'][0]
        self.assertEqual(a['context_sha256'],b['context_sha256'])
        with self.assertRaises(ValueError):
            old,_=cases.previous.load_corpus()
            cases.build_context(dict(cases.state_input(rows[0]),gold=rows[0]['gold']),cases.load_setups(),cases.previous.core.prefix_library(old))

    def test_invalid_pending_does_not_become_local_just_because_grammar_matches(self):
        valid=deepcopy(self.dev[18])
        invalid=deepcopy(valid)
        invalid['context']['pending_age_seconds']=900
        invalid['gold']['zero_intent_calls']=False
        report=cases.preflight([valid,invalid])
        self.assertTrue(report['passed'])
        self.assertEqual([r['expected_zero_intent_calls'] for r in report['rows']],[True,False])
        self.assertEqual(report['rows'][1]['setup']['pending_ages_seconds'],[900.0])

    def test_coverage_has_real_public_witnesses_and_g_is_not_scored(self):
        coverage=cases.load_coverage(self.dev)
        for kind in ('command','history','recall','horizon','knowledge'):
            for suffix in ('hit','suffix'):
                self.assertIn('D.'+kind+'.'+suffix,coverage['rules'])
        self.assertIn('D.guard.hit',coverage['rules'])
        self.assertIn('D.guard.miss',coverage['rules'])
        self.assertFalse(coverage['deferred_checks']['G']['scored_in_language_accuracy'])
        broken=deepcopy(coverage)
        broken['rules']['E.ordinal_shortlist']['public_examples'][0]['id']='unavailable'
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'witness'):
                cases.load_coverage(self.dev,self.write_json(folder,broken))

    def test_shape_guard_detects_name_number_and_punctuation_only_changes(self):
        first=deepcopy(self.dev[15])
        second=deepcopy(first)
        second['question']=second['question'].replace('平安银行','招商银行').replace('未来12天','未来31天').replace('。','！')
        second['evidence']['mentions']=['招商银行']
        second['evidence']['horizon']=['未来31天']
        self.assertEqual(cases.question_shape(first),cases.question_shape(second))
        self.assertNotEqual(cases.question_shape(self.dev[18]),cases.question_shape(self.dev[61]))

    def test_thresholds_use_frozen_semantic_count_and_keep_regression_denominator(self):
        for semantic in (79,87,96):
            summary=dict(semantic_calls=semantic,families={'large':6,'small':3})
            result=cases.thresholds(summary)
            self.assertEqual(result['semantic']['minimum_correct'],cases.ceil(.95*semantic))
            self.assertEqual(result['public_dev'],dict(n=64,minimum_correct=61))
            self.assertEqual(result['public_regression'],dict(n=412,minimum_correct=392,full_reference_n=414))
            self.assertEqual(result['families'],{'large':dict(n=6,minimum_correct=5)})
            self.assertTrue(result['failed_unmeasured_invalid_in_denominator'])

    def test_partition_checks_use_synthetic_rows_and_reject_group_or_shape_leaks(self):
        from collections import Counter
        allocation=cases.read_json(cases.ROOT/'data/evaluation/research-intent-v2.5-freeze.json')['holdout']['summary']['families']
        public_by_family={r['family_id']:r for r in self.dev}
        independent=[]
        for family,n in allocation.items():
            for _ in range(n):
                row=deepcopy(public_by_family[family])
                row.update(id=f'V26H{len(independent)+1:03d}',split='holdout',
                    question=row['question']+f'合成分区自检{len(independent)}',
                    variant_group=f'independent-synthetic-{len(independent)}')
                independent.append(row)
        self.assertEqual(Counter(r['family_id'] for r in independent),allocation)
        old,_=cases.legacy_rows()
        self.assertEqual(cases.validate_partition(self.dev,independent,old)['cross_split_group_leaks'],0)
        broken=deepcopy(independent)
        broken[0]['variant_group']=self.dev[0]['variant_group']
        with self.assertRaisesRegex(ValueError,'Variant group'):
            cases.validate_partition(self.dev,broken,old)
        broken=deepcopy(independent)
        broken[0]=dict(deepcopy(self.dev[0]),id='V26H999',split='holdout',
                       question=self.dev[0]['question']+'！',variant_group='synthetic-shape-collision')
        with self.assertRaisesRegex(ValueError,'shape leaks'):
            cases.validate_partition(self.dev,broken,old)

    def test_public_cli_has_no_sealed_or_live_options(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from evaluation_tools import preflight_research_intents_v26 as command
        for args in (['--split','holdout','--plan-only'],['--corpus','private.jsonl','--plan-only'],['--live','--plan-only']):
            with patch('sys.argv',['preflight',*args]),redirect_stderr(StringIO()), \
                    patch.object(cases,'validate_freeze',side_effect=AssertionError('parser must reject first')):
                with self.assertRaises(SystemExit) as error:
                    command.main()
                self.assertEqual(error.exception.code,2)

    def test_frozen_preflight_and_review_counts_cannot_claim_completion(self):
        freeze=cases.validate_freeze()
        for mutation in ('review_count','build_count','scoring','denominator','isolation'):
            value=deepcopy(freeze)
            if mutation=='review_count':
                value['review']['reviewed_dev_count']=63
            elif mutation=='build_count':
                value['context_preflight']['holdout']['buildable']=95
            elif mutation=='scoring':
                value['context_preflight']['holdout']['target_calls']=1
            elif mutation=='denominator':
                value['regression']['applicable']=411
            else:
                value['isolation']['author_session_id']='different-session'
            with TemporaryDirectory() as folder:
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                    cases.validate_freeze(self.write_json(folder,value))

    def test_freeze_requires_approval_and_isolation_and_rejects_drift(self):
        freeze=cases.validate_freeze()
        self.assertEqual(freeze['status'],'frozen')
        self.assertTrue(freeze['frozen_at'])
        self.assertEqual((freeze['review']['status'],freeze['review']['reviewer'],
                          freeze['review']['approval_message']),('approved','claude',434))
        self.assertFalse(freeze['candidate']['eligible_for_switch'])
        with TemporaryDirectory() as folder:
            # An independent snapshot exercises the gate without claiming actual isolation.
            independent=deepcopy(freeze)
            independent['isolation']['materials_moved_outside_repository']=True
            cases.validate_freeze(self.write_json(folder,independent),require_review=True)
            for mutation in ('status','review','isolation'):
                value=deepcopy(independent)
                if mutation=='status':
                    value['status']='awaiting_claude_review'
                elif mutation=='review':
                    value['review'].update(status='pending',reviewer=None,approval_message=None)
                else:
                    value['isolation']['materials_moved_outside_repository']=False
                with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'approval|required'):
                    cases.validate_freeze(self.write_json(folder,value),require_review=True)
            value=deepcopy(freeze)
            value['public_files_sha256_lf'][next(iter(value['public_files_sha256_lf']))]='0'*64
            with self.assertRaisesRegex(ValueError,'drift'):
                cases.validate_freeze(self.write_json(folder,value))


if __name__=='__main__':
    unittest.main()
