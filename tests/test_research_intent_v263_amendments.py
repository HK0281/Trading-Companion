"""Approval, layer integrity and score provenance for the six public v2.6.3 reviews."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from evaluation_tools import research_intent_v263_amendments as addendum
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v25_evaluation import OfflineTests
from trading_companion.intent_router_task import digest, read_json


class V263AmendmentTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.original, _, _ = addendum.previous.cases.load_amendments(require_approved=True)
        _, self.baseline = addendum.previous.load_amendments(require_approved=True)
        self.pending = read_json(addendum.AMENDMENTS)
        for item in self.pending['changes']:
            item.update(status='pending', reviewer=None, approval_message=None)
        self.path = Path(self.enterContext(TemporaryDirectory()))/'amendments.json'

    def load(self, document=None, **options):
        self.path.write_text(json.dumps(document or self.pending, ensure_ascii=False), encoding='utf-8')
        return addendum.load_amendments(self.path, **options)

    def test_six_pending_reviews_preserve_earlier_approved_gold_and_record_both_fingerprints(self):
        document, effective = self.load()
        self.assertEqual(effective, self.baseline)
        self.assertEqual({i['id'] for i in document['changes']}, addendum.REVIEW_IDS)
        self.assertEqual(len(document['changes']), 6)
        self.assertEqual(document['unchanged'], [])
        raw, base = ({r['id']: r for r in rows} for rows in (self.original, self.baseline))
        for item in document['changes']:
            self.assertEqual(item['parent_row_sha256'], digest(raw[item['id']]))
            self.assertEqual(item['baseline_gold_sha256'], digest(base[item['id']]['gold']))
        for identifier in ('V25D056', 'V25H083'):
            self.assertEqual(base[identifier]['gold']['subject_source'], 'none')
            self.assertNotIn(identifier, addendum.REVIEW_IDS)
        with self.assertRaisesRegex(ValueError, 'Claude approval required'):
            self.load(require_approved=True)

    def test_partial_and_complete_approval_only_change_listed_subject_sources(self):
        document = deepcopy(self.pending)
        for count in (1, 6):
            for item in document['changes'][:count]:
                item.update(status='approved', reviewer='claude', approval_message=500)
            _, effective = self.load(document, require_approved=count == 6)
            changed = {i['id'] for i in document['changes'][:count]}
            for before, after in zip(self.baseline, effective):
                expected = deepcopy(before)
                if before['id'] in changed:
                    expected['gold']['subject_source'] = 'previous'
                self.assertEqual(after, expected)
            if count == 1:
                with self.assertRaisesRegex(ValueError, 'Claude approval required'):
                    self.load(document, require_approved=True)

    def test_parent_row_gold_inventory_and_approval_tampering_are_rejected(self):
        mutations = [lambda d: d.update(version='other'), lambda d: d.update(request_message=True),
            lambda d: d.update(request_message=458), lambda d: d['parents_sha256_lf'].update(extra='unknown'),
            lambda d: d['changes'][0].update(parent_row_sha256='0'*64),
            lambda d: d['changes'][0].update(baseline_gold_sha256='0'*64),
            lambda d: d['changes'][0].update(field='wants_plan'),
            lambda d: d['changes'][0].update(before='question'), lambda d: d['changes'][0].update(after='none'),
            lambda d: d['changes'][0].update(after=True), lambda d: d['changes'][0].update(rule='D'),
            lambda d: d['changes'][0].update(reason=''), lambda d: d['changes'][0].update(extra=True),
            lambda d: d['changes'][0].update(id='V25H083'), lambda d: d['changes'].pop(),
            lambda d: d['changes'].append(deepcopy(d['changes'][0])),
            lambda d: d['unchanged'].append(deepcopy(d['changes'][0])),
            lambda d: d['changes'][0].update(status='approved', reviewer='codex', approval_message=500),
            lambda d: d['changes'][0].update(status='approved', reviewer='claude', approval_message=True),
            lambda d: d['changes'][0].update(status='approved', reviewer='claude', approval_message=459),
            lambda d: d['changes'][0].update(status='pending', reviewer='claude', approval_message=500)]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                document = deepcopy(self.pending)
                mutate(document)
                with self.assertRaises(ValueError):
                    self.load(document)

    def test_pending_review_stops_clean_live_entry_before_revision_credentials_or_directory_creation(self):
        plan = dict(workspace_dirty=False, context_preflight=dict(passed=True), annotation_amendments=dict(
            v262=dict(pending_reviews=0), v263=dict(pending_reviews=6)))
        with TemporaryDirectory() as folder, patch.object(evaluation, 'ROOT', Path(folder)), \
                patch.object(evaluation, 'build_plan', return_value=plan), \
                patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials')) as factory, \
                patch.object(evaluation, 'assert_current') as revision_check:
            with self.assertRaisesRegex(ValueError, 'Claude approval required.*v2.6.3'):
                evaluation.evaluate(live=True)
            factory.assert_not_called()
            revision_check.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_effective_population_preserves_all_denominators_and_earlier_approvals(self):
        with patch.object(addendum, 'read_json', return_value=deepcopy(self.pending)):
            rows, effective, _, _ = evaluation.populations('regression')
        self.assertEqual(len(rows), 414)
        self.assertEqual(len([r for r in rows if r['id'] not in evaluation.NOT_APPLICABLE]), 412)
        self.assertEqual(effective, {r['id']: r['gold'] for r in self.baseline})
        self.assertEqual(effective['V2217']['subject_source'], 'previous')

    def report(self, document):
        _, effective = self.load(document)
        gold = next(r['gold'] for r in effective if r['id'] == 'V25D066')
        row = deepcopy(next(r for r in self.original if r['id'] == 'V25D066'))
        row.update(split='regression', dataset='regression', effective_gold=deepcopy(gold),
                   V2=dict(evaluation._empty('ok', None), fields=dict(row['gold'], subject_source='previous')),
                   A=dict(status='ok', fields=deepcopy(row['gold'])))
        with patch.object(addendum, 'read_json', return_value=deepcopy(document)):
            return evaluation.report_results([row], {row['id']: gold}, {})

    def test_report_lists_three_layers_and_does_not_credit_annotation_as_model_improvement(self):
        report = self.report(self.pending)
        audit = report['annotation_amendments']
        self.assertFalse(audit['model_improvement'])
        self.assertEqual((audit['v263']['pending_reviews'], audit['v263']['applied_changes']), (6, 0))
        self.assertEqual((audit['v262']['pending_reviews'], audit['v262']['applied_changes']), (0, 12))
        markdown = evaluation.markdown(report)
        for value in ('research-intent-v2.6.3-amendments-v1', 'V25D066', 'pending', '不计模型改善',
                      'research-intent-v2.6.2-amendments-v1', '458'):
            self.assertIn(value, markdown)
        document = deepcopy(self.pending)
        item = next(i for i in document['changes'] if i['id'] == 'V25D066')
        item.update(status='approved', reviewer='claude', approval_message=500)
        after = self.report(document)
        for view in ('raw_original', 'normalized_original'):
            self.assertEqual(report['score_views'][view], after['score_views'][view])
        self.assertEqual(after['annotation_amendments']['v263']['applied_changes'], 1)
        self.assertEqual(report['score_views']['normalized_approved']['applicable']['fields']['subject_source']['accuracy']['numerator'], 0)
        self.assertEqual(after['score_views']['normalized_approved']['applicable']['fields']['subject_source']['accuracy']['numerator'], 1)
        self.assertFalse(after['passed'])
