"""Approval, provenance and scoring boundaries for the public v2.6.2 annotation addendum."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from evaluation_tools import research_intent_v262_amendments as addendum
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v25_evaluation import OfflineTests
from trading_companion.intent_router_task import digest, read_json


class AddendumTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.original, _, self.baseline = evaluation.cases.load_amendments(require_approved=True)
        self.pending = read_json(addendum.AMENDMENTS)
        for item in [*self.pending['changes'], *self.pending['unchanged']]:
            item.update(status='pending', reviewer=None, approval_message=None)
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder)/'amendments.json'

    def load(self, document=None, **options):
        self.path.write_text(json.dumps(document or self.pending, ensure_ascii=False), encoding='utf-8')
        return addendum.load_amendments(self.path, **options)

    def approve(self, document, all_items=False):
        items = [*document['changes'], *document['unchanged']] if all_items else document['changes'][:1]
        for item in items:
            item.update(status='approved', reviewer='claude', approval_message=457)

    def test_pending_is_auditable_without_changing_any_effective_gold(self):
        document, effective = self.load()
        self.assertEqual(effective, self.baseline)
        self.assertEqual((len(document['changes']), len(document['unchanged'])), (12, 1))
        self.assertEqual(document['unchanged'][0]['id'], 'V2220')
        raw = {r['id']: r for r in self.original}
        baseline = {r['id']: r for r in self.baseline}
        for item in [*document['changes'], *document['unchanged']]:
            self.assertEqual(item['parent_row_sha256'], digest(raw[item['id']]))
            self.assertEqual(item['baseline_gold_sha256'], digest(baseline[item['id']]['gold']))
            self.assertEqual(item['field'], 'subject_source')
        with self.assertRaisesRegex(ValueError, 'Claude approval required'):
            self.load(require_approved=True)

    def test_only_individually_approved_changes_take_effect(self):
        document = deepcopy(self.pending)
        before = deepcopy(document)
        self.approve(document)
        _, effective = self.load(document)
        changed = document['changes'][0]['id']
        for base, result in zip(self.baseline, effective):
            expected = deepcopy(base)
            if base['id'] == changed:
                expected['gold']['subject_source'] = 'previous'
            self.assertEqual(result, expected)
        self.assertEqual(document['changes'][1:], before['changes'][1:])
        self.assertEqual(document['unchanged'], before['unchanged'])
        with self.assertRaisesRegex(ValueError, 'Claude approval required'):
            self.load(document, require_approved=True)

    def test_complete_approval_preserves_unchanged_case_and_every_other_field(self):
        document = deepcopy(self.pending)
        self.approve(document, all_items=True)
        _, effective = self.load(document, require_approved=True)
        with patch.object(addendum, 'read_json', return_value=deepcopy(document)):
            audit = addendum.summary()
        self.assertEqual((audit['v262']['pending_reviews'], audit['v262']['applied_changes']), (0, 12))
        changed = {i['id'] for i in document['changes']}
        for base, result in zip(self.baseline, effective):
            for key in base:
                if key != 'gold':
                    self.assertEqual(result[key], base[key])
            expected = dict(base['gold'], subject_source='previous') if base['id'] in changed else base['gold']
            self.assertEqual(result['gold'], expected)

    def test_fingerprint_inventory_schema_and_approval_tampering_are_rejected(self):
        mutations = [
            lambda d: d.update(version='unknown'),
            lambda d: d.update(request_message=True),
            lambda d: d['parents_sha256_lf'].update(extra='invalid'),
            lambda d: d['changes'][0].update(parent_row_sha256='0'*64),
            lambda d: d['changes'][0].update(baseline_gold_sha256='0'*64),
            lambda d: d['changes'][0].update(before='question'),
            lambda d: d['changes'][0].update(after='none'),
            lambda d: d['changes'][0].update(after=True),
            lambda d: d['changes'][0].update(field='horizon'),
            lambda d: d['changes'][0].update(rule='D'),
            lambda d: d['changes'][0].update(reason=''),
            lambda d: d['changes'][0].update(extra=True),
            lambda d: d['changes'][0].update(id='private-unknown'),
            lambda d: d['changes'][0].update(status='approved', reviewer='codex', approval_message=457),
            lambda d: d['changes'][0].update(status='approved', reviewer='claude', approval_message=True),
            lambda d: d['changes'][0].update(status='approved', reviewer='claude', approval_message=456),
            lambda d: d['changes'][0].update(status='pending', reviewer='claude', approval_message=457),
            lambda d: d['changes'].append(deepcopy(d['changes'][0])),
            lambda d: d['changes'].pop(),
            lambda d: d['unchanged'][0].update(after='previous'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                document = deepcopy(self.pending)
                mutate(document)
                with self.assertRaises(ValueError):
                    self.load(document)

    def test_pending_review_blocks_clean_live_run_before_credentials(self):
        plan = dict(workspace_dirty=False, context_preflight=dict(passed=True),
                    annotation_amendments=dict(v262=dict(pending_reviews=13)))
        with patch.object(evaluation, 'build_plan', return_value=plan), \
                patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials')) as factory, \
                patch.object(evaluation, 'assert_current') as revision_check:
            with self.assertRaisesRegex(ValueError, 'Claude approval required'):
                evaluation.evaluate(live=True)
            factory.assert_not_called()
            revision_check.assert_not_called()

    def test_pending_population_keeps_baseline_gold_and_original_denominators(self):
        with patch.object(addendum, 'read_json', return_value=deepcopy(self.pending)):
            rows, effective, _, _ = evaluation.populations('regression')
        self.assertEqual(len(rows), 414)
        self.assertEqual(len([r for r in rows if r['id'] not in evaluation.NOT_APPLICABLE]), 412)
        self.assertEqual(effective, {r['id']: r['gold'] for r in self.baseline})

    def report(self, effective):
        row = deepcopy(next(r for r in self.original if r['id'] == 'V2216'))
        row.update(split='regression', dataset='regression')
        fields = dict(row['gold'], subject_source='previous')
        row['effective_gold'] = deepcopy(effective)
        row['V2'] = dict(evaluation._empty('ok', None), fields=fields)
        row['A'] = dict(status='ok', fields=deepcopy(row['gold']))
        with patch.object(addendum, 'read_json', return_value=deepcopy(self.pending)):
            return evaluation.report_results([row], {row['id']: effective}, {})

    def test_report_lists_both_layers_pending_and_unchanged_without_model_improvement(self):
        gold = next(r['gold'] for r in self.baseline if r['id'] == 'V2216')
        report = self.report(gold)
        audit = report['annotation_amendments']
        self.assertFalse(audit['model_improvement'])
        self.assertEqual((audit['v262']['pending_reviews'], audit['v262']['applied_changes']), (13, 0))
        self.assertEqual(audit['previous']['changes'][0]['approval_message'], 434)
        self.assertEqual(audit['v262']['unchanged'][0]['id'], 'V2220')
        markdown = evaluation.markdown(report)
        self.assertIn('research-intent-v2.6.2-amendments-v1', markdown)
        self.assertIn('V2220', markdown)
        self.assertIn('pending', markdown)
        self.assertIn('不计模型改善', markdown)

    def test_only_effective_score_changes_while_both_original_views_remain_unchanged(self):
        gold = next(r['gold'] for r in self.baseline if r['id'] == 'V2216')
        for effective, expected in ((gold, 0), (dict(gold, subject_source='previous'), 1)):
            report = self.report(effective)
            for view in ('raw_original', 'normalized_original'):
                self.assertEqual(report['score_views'][view]['applicable']['joint_all_fields']['numerator'], 0)
            self.assertEqual(report['score_views']['normalized_approved']['applicable']['joint_all_fields']['numerator'], expected)
            self.assertFalse(report['passed'])
