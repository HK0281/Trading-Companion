from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.intent_router_task import (CORPUS, load_corpus, definition_digest, input_state, LABELS,
    CRITERIA, digest, row_digest, read_json, MANIFEST, sources_match, frozen_sources, SOURCE_REVISIONS)
from trading_companion.typesafe_jev import ClassifierError


class IntentTaskTests(unittest.TestCase):
    def test_b_source_version_preserves_historical_corpus_and_rejects_unknown_edits(self):
        from trading_companion.intent_rule_baseline import current_baseline, source_hashes
        historical=read_json(MANIFEST)['frozen_sources']
        self.assertTrue(sources_match(historical))
        current=source_hashes()
        self.assertEqual(current_baseline()['sources_sha256_lf'],current)
        for name in current:
            with self.subTest(name=name),patch('trading_companion.intent_rule_baseline.source_hashes',
                    return_value={**current,name:'0'*64}),self.assertRaises(ValueError):
                sources_match(historical)

    def test_authored_distributions_pairs_and_unreviewed_status(self):
        rows,manifest=load_corpus()
        self.assertEqual(len(rows),304)
        self.assertIn(manifest['annotation_status'],('draft','reviewed'))
        self.assertEqual(Counter(r['gold'] for r in rows if r['gold']),Counter({k:30 for k in LABELS}))
        self.assertEqual(sum(r['ablation_pair'] is not None for r in rows),48)
        self.assertEqual(manifest['ablation_design']['counts'],{'label_changes':6,'label_invariant':18})
        self.assertEqual(CRITERIA.keys(),set(LABELS))
        self.assertEqual(definition_digest(),manifest['definition_sha256'])
        for row in rows:
            state=input_state(row)
            self.assertEqual(state.keys(),{'question','context'})
            self.assertNotIn('gold',json.dumps(state))
            self.assertNotIn('latest_valid_research_kind',input_state(row,ablated=True)['context'])

    def test_review_159_decisions_names_and_natural_missing_subjects(self):
        rows,_=load_corpus()
        by_id={r['id']:r for r in rows}
        for start in (4,10,16,28,34,40):
            for number in range(start,start+3):
                row=by_id[f'I{number:03}']
                self.assertEqual(row['gold'],'company_research')
                self.assertEqual(row['context']['latest_valid_research_kind'],'technical')
                self.assertIn('可信交接',row['reason'])
        for number in (229,230,231):
            self.assertEqual(by_id[f'I{number:03}']['gold'],'company_research')
        for row in rows:
            if row['case_kind']!='semantic':
                continue
            for marker in ('没有保存','没有创建','没有提供','没有任何','没有对象','可接续上下文','有效研究','公司与技术记录','不修改任何既有','不让你看任何'):
                self.assertNotIn(marker,row['question'])
            if row['gold']=='clarify':
                context=row['context']
                self.assertTrue(context['has_multiple_research_subjects'] or not any(context[k] for k in
                    ('has_saved_company','has_saved_technical','has_shortlist')))

    def test_ablation_selection_cannot_disagree_with_frozen_pair_types(self):
        with self.assertRaises(ClassifierError):
            self.mutate(lambda r,m:m['ablation_design']['pairs'][0].update(kind='label_invariant'))

    def mutate(self,change):
        rows,manifest=load_corpus()
        change(rows,manifest)
        raw='\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n'
        manifest['corpus_sha256']=sha256(raw.encode()).hexdigest()
        manifest['split_sha256']=digest([(r['id'],r['family_id'],r['split']) for r in rows])
        for r in rows:
            manifest['reviews'][r['id']]['row_sha256']=row_digest(r)
        with TemporaryDirectory() as root:
            corpus,path=Path(root)/'a.jsonl',Path(root)/'manifest.json'
            corpus.write_text(raw,encoding='utf-8')
            path.write_text(json.dumps(manifest),encoding='utf-8')
            return load_corpus(corpus,path)

    def test_family_leak_extra_input_duplicate_or_illegal_label_rejected(self):
        changes=[lambda r,m:r[0].update(split='holdout'),lambda r,m:r[0]['context'].update(answer='company_research'),
                 lambda r,m:r[0].update(gold='command'),lambda r,m:r[1].update(question=r[0]['question'])]
        for change in changes:
            with self.subTest(change=change),self.assertRaises((ClassifierError,ValueError)):
                self.mutate(change)

    def test_reviewed_flag_cannot_bypass_per_row_review(self):
        def incomplete(rows,manifest):
            manifest.update(annotation_status='reviewed',reviewer='claude',reviewed_at='2026-10-29')
            manifest['reviews'][rows[0]['id']].update(status='pending',reviewer=None)
        with self.assertRaises(ClassifierError):
            self.mutate(incomplete)

    def test_json_rejects_duplicate_keys_constants_and_bom(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'test.json'
            for value in (b'{"a":1,"a":2}',b'{"a":NaN}',b'\xef\xbb\xbf{}'):
                path.write_bytes(value)
                with self.subTest(value=value),self.assertRaises((ClassifierError,ValueError)):
                    read_json(path)
