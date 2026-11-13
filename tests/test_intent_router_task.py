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
from trading_companion import intent_rule_baseline as baselines


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


class HistoricalBaselineTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(baselines.DESCRIPTION.read_text(encoding='utf-8'))
        self.version = 'index-scenario-ui-v1'
        self.historical = next(r for r in self.registry['previous_rules']
                               if r['rule_baseline_version'] == self.version)
        self.expected = self.historical['source_digest']
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'registry.json'

    def lookup(self, *, version=None, expected=None):
        self.path.write_text(json.dumps(self.registry), encoding='utf-8')
        with patch.object(baselines, 'DESCRIPTION', self.path):
            return baselines.historical_baseline(version=version or self.version,
                                                  expected_digest=expected or self.expected)

    def test_current_identity_is_upgraded_and_previous_identity_is_preserved(self):
        current = baselines.current_baseline()
        self.assertEqual(current['rule_baseline_version'], 'personal-memory-review-v1')
        self.assertEqual(current['parent_version'], 'personal-memory-preferences-v1')
        previous = next(r for r in self.registry['previous_rules']
                        if r['rule_baseline_version'] == current['parent_version'])
        self.assertEqual(previous['source_digest'], 'e49ce64cb33d182db0f5c976b1a66861b6fa1794fb65a5b77ebfb37853025d27')
        watchlist = next(r for r in self.registry['previous_rules']
                         if r['rule_baseline_version'] == 'personal-memory-watchlist-v1')
        self.assertEqual(watchlist['source_digest'], 'fe108e492a59bfee15cac6e7fc80e8b83e342c3ce6368ae9f96e569acdc37a03')
        self.assertEqual(sum(r['rule_baseline_version'] == current['parent_version']
                             for r in self.registry['previous_rules']), 1)
        document_v3 = next(r for r in self.registry['previous_rules']
                           if r['rule_baseline_version'] == 'intent-research-documents-v3')
        self.assertEqual(document_v3['source_digest'], 'a40062305685adfb04ab627e4cdb79c132d3e6df2fc1bf4822114d982059e965')
        document_v2 = next(r for r in self.registry['previous_rules']
                           if r['rule_baseline_version'] == 'intent-research-documents-v2')
        self.assertEqual(document_v2['source_digest'], '02a4294b8be3b13a3a21ddf42d6a136246cdaf543c0491ff5a8718a78d572151')
        document_v1 = next(r for r in self.registry['previous_rules']
                           if r['rule_baseline_version'] == 'intent-research-documents-v1')
        self.assertEqual(document_v1['source_digest'], '266b80b029b680d26998fc95af74d79ce5d37ea561295e71b74e8915aa55d057')
        paired = next(r for r in self.registry['previous_rules']
                      if r['rule_baseline_version'] == 'intent-news-triage-paired-live-v2')
        self.assertEqual(paired['source_digest'], 'a4dbdf341bf7c6449cbb8743a52234e971d2d6f150ba16a96350e70454f017ba')
        older = next(r for r in self.registry['previous_rules']
                     if r['rule_baseline_version'] == 'intent-news-triage-paired-live-v1')
        self.assertEqual(older['source_digest'], 'f3578c6ae38a4db0474c07f8170330b083133d647ab0fd1de222bef4affa9a57')
        original = next(r for r in self.registry['previous_rules']
                        if r['rule_baseline_version'] == 'intent-integration-default-on-v1')
        self.assertEqual(original['source_digest'], '3a7455512339f92fda14c35ca79e6d87855e629e071188aff7ebb58b5531f1b1')
        with patch.object(baselines, 'current_baseline', wraps=baselines.current_baseline) as checked:
            self.assertEqual(self.lookup(), self.historical)
        checked.assert_called_once_with()

    def test_exact_version_and_digest_are_both_required(self):
        for version, expected in ((self.version, '0'*64), ('unregistered', self.expected),
                                  (baselines.VERSION, self.registry['current']['source_digest'])):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.lookup(version=version, expected=expected)

    def test_historical_sources_and_registered_digest_are_recomputed(self):
        name = next(iter(self.historical['sources_sha256_lf']))
        self.historical['sources_sha256_lf'][name] = '0'*64
        with self.assertRaisesRegex(ValueError, 'Historical rule baseline source mismatch'):
            self.lookup()

    def test_even_identical_or_differently_hashed_duplicate_versions_are_rejected(self):
        duplicate = deepcopy(self.historical)
        self.registry['previous_rules'].append(duplicate)
        for digest_value in (self.expected, '0'*64):
            duplicate['source_digest'] = digest_value
            with self.subTest(digest=digest_value), self.assertRaisesRegex(ValueError, 'exactly once'):
                self.lookup()

    def test_missing_history_is_rejected(self):
        self.registry['previous_rules'].remove(self.historical)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.lookup()

    def test_current_source_drift_is_rejected_even_when_history_matches(self):
        sources = baselines.source_hashes()
        name = next(iter(sources))
        with patch.object(baselines, 'source_hashes', return_value={**sources, name: '0'*64}):
            with self.assertRaisesRegex(ValueError, 'Current rule baseline source mismatch'):
                self.lookup()
