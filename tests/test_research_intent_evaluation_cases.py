"""Corpus integrity and review admission, without scoring the held-out answers."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion import research_intent_evaluation_cases as cases
from trading_companion import research_intent_evaluation as evaluation


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.manifest = cases.load_corpus()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def load_changed(self, rows=None, manifest=None, rehash=False):
        rows = self.rows if rows is None else rows
        manifest = deepcopy(self.manifest if manifest is None else manifest)
        raw = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode()
        if rehash:
            manifest['corpus_sha256'] = sha256(raw).hexdigest()
            manifest['split_sha256'] = cases.digest([(r['id'], r['family_id'], r['split']) for r in rows])
        root = Path(self.temporary.name)
        (root / 'cases.jsonl').write_bytes(raw)
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return cases.load_corpus(root / 'cases.jsonl', root / 'manifest.json')

    def test_corpus_counts_and_family_splits(self):
        self.assertEqual(Counter(r['split'] for r in self.rows), dict(dev=155, holdout=85))
        families = defaultdict(set)
        for row in self.rows:
            families[row['family_id']].add(row['split'])
        self.assertEqual(len(families), 48)
        self.assertTrue(all(len(s) == 1 for s in families.values()))
        self.assertEqual(sum(line.startswith('| V2') for line in cases.annotation_review(self.rows, self.manifest).splitlines()), 240)

    def test_price_followup_coverage_exists_on_both_sides_without_predicting_holdout(self):
        for split in ('dev', 'holdout'):
            group = [r for r in self.rows if r['split'] == split and r['gold']['task'] == 'stock_research'
                     and r['gold']['wants_plan'] and r['gold']['subject_source'] == 'previous'
                     and r['context']['latest'] is not None]
            self.assertTrue(any(n >= 5 for n in Counter(r['family_id'] for r in group).values()), split)

    def test_all_public_failure_inputs_present_without_using_holdout_predictions(self):
        questions = {r['question'] for r in self.rows if r['source']['kind'] == 'reported_failure'}
        self.assertTrue({'如果我近期考虑建仓 能否给出几个价位', '平安银行',
            '平安银行近期考虑建仓 能否给出几个价位', '分析一下大盘指数的走势',
            '创业板最近怎么样', '现在A股能买吗', '科创50最近一个月走势', '你刚才给的评级是什么',
            '现在这个价位值得购买吗'} <= questions)

    def test_vague_evidence_is_not_simultaneously_a_confirmed_horizon(self):
        changed = deepcopy(self.rows[0])
        changed['evidence']['horizon'] = changed['evidence']['vague_horizon'] = ['近期']
        with self.assertRaises(ValueError):
            cases.valid_row(changed)

    def test_gold_reason_split_and_scripts_never_reach_adapters(self):
        row = self.rows[0]
        state = cases.input_state(row)
        self.assertEqual(set(state), {'question', 'context'})
        state['context']['history'].append('mutated')
        self.assertNotEqual(state['context'], row['context'])
        changed = deepcopy(row)
        for key in ('gold', 'reason', 'split', 'probe', 'source'):
            changed[key] = 'poisoned'
        self.assertEqual(cases.input_state(changed), cases.input_state(row))

    def test_row_edit_invalidates_review_even_with_top_corpus_hash_updated(self):
        changed = deepcopy(self.rows)
        changed[0]['reason'] += ' 修订'
        with self.assertRaises(ValueError):
            self.load_changed(rows=changed, rehash=True)

    def test_top_review_switch_cannot_skip_individual_reviews(self):
        manifest = deepcopy(self.manifest)
        manifest.update(annotation_status='reviewed', reviewer='claude', reviewed_at=date.today().isoformat())
        manifest['reviews'][self.rows[0]['id']].update(status='pending', reviewer=None)
        with self.assertRaises(ValueError):
            self.load_changed(manifest=manifest, rehash=True)

    def test_cross_split_family_and_duplicate_input_rejected_before_scoring(self):
        changed = deepcopy(self.rows)
        changed[-1]['family_id'] = changed[0]['family_id']
        with self.assertRaises(ValueError):
            self.load_changed(rows=changed, rehash=True)
        changed = deepcopy(self.rows)
        changed[1]['question'], changed[1]['context'] = changed[0]['question'], changed[0]['context']
        with self.assertRaises(ValueError):
            self.load_changed(rows=changed, rehash=True)

    def test_changed_rule_sources_are_rejected(self):
        with patch.object(cases, 'current_baseline', side_effect=ValueError('mismatch')):
            with self.assertRaises(ValueError):
                cases.load_corpus()

    def test_draft_can_preview_but_cannot_score_or_call_models(self):
        self.assertEqual(evaluation.build_plan()['planned_A'], 155)
        draft = deepcopy(self.manifest)
        draft.update(annotation_status='draft', reviewer=None, reviewed_at=None)
        with patch.object(evaluation, 'load_corpus', return_value=(self.rows, draft)), \
                patch.object(evaluation, 'rule_observation') as rule, patch.object(evaluation, 'fixed_execution') as fixed:
            self.assertEqual(evaluation.evaluate()['code'], 'intent_annotations_not_reviewed')
            rule.assert_not_called()
            fixed.assert_not_called()
        with self.assertRaises(ValueError):
            evaluation.build_plan(split='holdout')


if __name__ == '__main__':
    unittest.main()
