"""Gold amendments cannot change frozen input, reach holdout or approve themselves."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_companion.intent_router_task import digest
from trading_companion.research_intent_evaluation_cases import load_corpus
from trading_companion.research_intent_v2_annotations import AMENDMENTS, load_amendments, revised_rows


class AmendmentTests(unittest.TestCase):
    def test_exact_three_reviewed_changes_leave_inputs_and_original_gold_unchanged(self):
        rows, manifest = load_corpus()
        original = digest(rows)
        changes = load_amendments(rows, manifest['corpus_sha256'])
        self.assertEqual(set(changes), {'V2020', 'V2038', 'V2089'})
        self.assertEqual(changes['V2038']['after'], 'decision')
        for change in changes.values():
            self.assertEqual(change['reviewer'], 'claude')
            self.assertEqual(change['approval_message'], 366)
        revised = revised_rows(rows, changes)
        self.assertEqual(digest(rows), original)
        for parent, child in zip(rows, revised):
            for key in parent.keys() - {'gold'}:
                self.assertEqual(parent[key], child[key])
            if parent['id'] not in changes:
                self.assertEqual(parent['gold'], child['gold'])

    def test_amendments_reject_unreviewed_wrong_parent_extra_input_and_invalid_labels(self):
        rows, manifest = load_corpus()
        value = json.loads(AMENDMENTS.read_text(encoding='utf-8'))
        variants = []
        for key, invalid in (('status', 'draft'), ('reviewer', 'codex'), ('parent_row_sha256', 'wrong'),
                             ('question', 'changed input'), ('field', 'bound_subjects'), ('after', 42)):
            variant = deepcopy(value)
            variant['changes'][0][key] = invalid
            variants.append(variant)
        duplicate = deepcopy(value)
        duplicate['changes'].append(duplicate['changes'][0])
        variants.append(duplicate)
        with TemporaryDirectory() as folder:
            path = Path(folder)/'amendments.json'
            for variant in variants:
                path.write_text(json.dumps(variant), encoding='utf-8')
                with self.subTest(variant=variant['changes'][0]), self.assertRaises(ValueError):
                    load_amendments(rows, manifest['corpus_sha256'], path)
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_amendments(rows, 'different corpus', path)

    def test_holdout_label_cannot_be_changed_even_with_matching_parent_digest(self):
        # Reclassify a development parent synthetically; no real holdout wording is inspected.
        rows, manifest = load_corpus()
        rows = [deepcopy(r) for r in rows if r['split'] == 'dev']
        value = json.loads(AMENDMENTS.read_text(encoding='utf-8'))
        change = value['changes'][0]
        parent = next(r for r in rows if r['id'] == change['id'])
        parent['split'] = 'holdout'
        change['parent_row_sha256'] = digest(parent)
        with TemporaryDirectory() as folder:
            path = Path(folder)/'amendments.json'
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_amendments(rows, manifest['corpus_sha256'], path)
        with self.assertRaises(ValueError):
            revised_rows([parent], {change['id']: change})


if __name__ == '__main__':
    unittest.main()
