"""Public development and independent synthetic admission checks only; no sealed input."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v25_cases as cases


class AdmissionTests(unittest.TestCase):
    def test_public_corpus_and_multifield_parented_amendments(self):
        rows = cases.load_cases()
        original_effective, amendments, effective = cases.load_regression_amendments()
        self.assertEqual(len(rows), 78)
        self.assertEqual(len({r['family_id'] for r in rows}), 48)
        self.assertEqual(len(amendments['changes']), 39)
        parents = {r['id']: r for r in original_effective}
        revised = {r['id']: r for r in effective}
        self.assertEqual(parents['V2153']['gold']['research_kinds'], [])
        self.assertEqual(revised['V2153']['gold']['research_kinds'], ['technical'])
        self.assertEqual(revised['V2153']['gold']['indicator_mentions'], ['成交量'])
        self.assertEqual(revised['V2038']['gold']['objective_kind'], 'decision')
        for identifier in ('V2156', 'V2157', 'V2158', 'V2159', 'V2160'):
            self.assertEqual(revised[identifier], parents[identifier])

    def test_holdout_requires_explicit_hash_checked_before_decoding(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'independent.jsonl'
            path.write_bytes(b'not-json\n' * 96)
            with self.assertRaises(ValueError):
                cases.load_cases(path, split='holdout')
            with patch.object(cases, 'decode', side_effect=AssertionError('Must hash first')) as decoder:
                with self.assertRaisesRegex(ValueError, 'fingerprint'):
                    cases.load_cases(path, split='holdout', expected_sha256='0' * 64)
                decoder.assert_not_called()

    def test_public_newlines_are_portable_but_sealed_bytes_are_exact(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'public.jsonl'
            raw = cases.DEV.read_bytes().replace(b'\r\n', b'\n')
            path.write_bytes(raw.replace(b'\n', b'\r\n'))
            self.assertEqual(cases.load_cases(path, expected_sha256=cases.public_sha256(cases.DEV)), cases.load_cases())
            sealed = Path(folder) / 'independent-holdout.jsonl'
            sealed.write_bytes(b'not-json\n' * 96)
            original_sha = cases.file_sha256(sealed)
            sealed.write_bytes(sealed.read_bytes().replace(b'\n', b'\r\n'))
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                cases.load_cases(sealed, split='holdout', expected_sha256=original_sha)

    def test_amendment_rejects_duplicate_field_parent_drift_or_unapproved_change(self):
        value = cases.read_json(cases.AMENDMENTS)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'amendments.json'
            for mutation in ('duplicate', 'parent', 'approval', 'before', 'previous'):
                changed = deepcopy(value)
                if mutation == 'duplicate':
                    changed['changes'].append(deepcopy(changed['changes'][0]))
                elif mutation == 'previous':
                    changed['previous_amendments_sha256'] = '0' * 64
                else:
                    key, replacement = {'parent': ('parent_row_sha256', '0' * 64),
                        'approval': ('approval_message', 389), 'before': ('before', ['news'])}[mutation]
                    changed['changes'][0][key] = replacement
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    cases.load_regression_amendments(path)

    def test_context_preflight_never_interprets_target_or_accepts_gold(self):
        rows = cases.load_cases()
        old, _ = cases.load_corpus()
        setups, prefixes = cases.load_setups(), cases.core.prefix_library(old)
        original_builder = cases.build_context
        captured = []

        def capture(state, *args):
            captured.append(deepcopy(state))
            self.assertEqual(set(state), {'question', 'context', 'setup_id'})
            self.assertEqual(state['question'], '仅检查前置状态，不执行目标轮')
            return original_builder(state, *args)

        with patch.object(cases, 'build_context', side_effect=capture), \
                patch.object(cases.core, '_live_model', side_effect=AssertionError('No model factory')):
            report = cases.preflight(rows, setups, prefixes)
        self.assertEqual(len(captured), 78)
        self.assertEqual((report['buildable'], report['target_calls'], report['prediction_rows'], report['score_rows']),
                         (78, 0, 0, 0))
        altered = deepcopy(rows[0])
        altered['question'] = '与真实目标完全无关的替身问题'
        altered['gold'] = {'not': 'a scoring input'}
        self.assertEqual(cases.preflight([rows[0]], setups, prefixes)['rows'][0]['context_sha256'],
                         cases.preflight([altered], setups, prefixes)['rows'][0]['context_sha256'])
        with self.assertRaises(ValueError):
            cases.build_context(dict(cases.state_input(rows[0]), gold=rows[0]['gold']), setups, prefixes)

    def test_fault_contexts_use_native_objects_and_do_not_renew_age(self):
        rows = cases.load_cases()
        old, _ = cases.load_corpus()
        setups, prefixes = cases.load_setups(), cases.core.prefix_library(old)
        fault_rows = [r for r in rows if r['family_id'].startswith(('H14', 'H15'))]
        for row in fault_rows:
            context, calls = cases.build_context(cases.state_input(row), setups, prefixes)
            metadata = cases.core.setup_metadata(context)
            with self.subTest(case=row['id']):
                if row['setup_id'] == 'pending-index-star':
                    self.assertEqual(calls, 0)  # Native index horizon creation is entirely local.
                else:
                    self.assertGreater(calls, 0)
                self.assertTrue(metadata['pending_types'])
                self.assertEqual(metadata['pending_ages_seconds'],
                                 [row['context']['pending_age_seconds']] * len(metadata['pending_types']))
                if row['context']['pending'] == 'conflict':
                    self.assertEqual(metadata['fault_injection'], 'multi_pending')
                elif (row['context']['pending_age_seconds'] >= 900
                      or row['setup_id'] in ('pending-source', 'pending-mode', 'pending-identity')):
                    self.assertFalse(context.model_context()['pending']['valid'])

    def test_review_records_preserve_392_and_pending_cannot_be_admitted_as_frozen(self):
        rows = cases.load_cases()
        manifest = cases.load_dev_manifest(rows)
        for row in rows[:64]:
            review = manifest['reviews'][row['id']]
            self.assertEqual((review['status'], review['reviewer'], review['approval_message']),
                             ('approved', 'claude', 392))
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            invalid = deepcopy(manifest)
            invalid['reviews'][rows[0]['id']]['approval_message'] = 390
            path.write_text(json.dumps(invalid), encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'review'):
                cases.load_dev_manifest(rows, path)
        forged = deepcopy(cases.validate_freeze())
        forged.update(status='frozen')
        forged['review'].update(status='approved', reviewer='claude', approval_message=999)
        pending_manifest = deepcopy(manifest)
        pending_manifest['reviews'][rows[-1]['id']].update(status='pending', reviewer=None, approval_message=None)
        pending_count = sum(item['status'] == 'pending' for item in pending_manifest['reviews'].values())
        forged['review'].update(reviewed_dev_count=len(rows)-pending_count, pending_dev_count=pending_count)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'freeze.json'
            path.write_text(json.dumps(forged), encoding='utf8')
            with patch.object(cases, 'load_dev_manifest', return_value=pending_manifest), \
                    self.assertRaisesRegex(ValueError, 'pending development'):
                cases.validate_freeze(path, require_review=True)

    def test_rule_coverage_requires_a_real_public_witness_for_each_label(self):
        rows = cases.load_cases()
        report = cases.load_rule_coverage(rows)
        self.assertEqual(report['uncovered_rules'], 0)
        for label in ('strict_time_only_false', 'strict_negated_only_false', 'index_horizon_local_invalid',
                      'conflict_independent_new', 'pending_directory_changed', 'screening_refresh',
                      'memory_off_no_binding', 'unsupported_indicator'):
            self.assertTrue(report['rules'][label]['public_examples'])
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'coverage.json'
            for witnesses in ([], ['not-a-public-case'], [rows[0]['id']]):
                changed = deepcopy(report)
                changed['rules']['unknown_stock_identity']['public_examples'] = witnesses
                path.write_text(json.dumps(changed), encoding='utf8')
                with self.subTest(witnesses=witnesses), self.assertRaisesRegex(ValueError, 'witness'):
                    cases.load_rule_coverage(rows, path)

    def test_shape_guard_catches_subject_date_indicator_swaps(self):
        row = deepcopy(cases.load_cases()[0])
        row.update(question='研究甲公司近5天的EMA3', evidence=dict(mentions=['甲公司'], dates=['近5天'],
            horizon=[], vague_horizon=[], indicators=['EMA3'], exclusions=[]))
        swapped = deepcopy(row)
        swapped.update(question='研究乙公司近8天的EMA9', evidence=dict(mentions=['乙公司'], dates=['近8天'],
            horizon=[], vague_horizon=[], indicators=['EMA9'], exclusions=[]))
        self.assertEqual(cases.question_shape(row), cases.question_shape(swapped))

    def test_variant_group_cannot_be_split_even_if_wording_and_ids_change(self):
        dev = cases.load_cases()
        independent = []
        for number in range(96):
            row = deepcopy(dev[number % len(dev)])
            row.update(id=f'V25H{number+1:03d}', split='holdout')
            row['question'] += f'附加独立编号{number}'
            independent.append(row)
        legacy, _ = cases.load_corpus()
        with self.assertRaisesRegex(ValueError, 'Variant group'):
            cases.validate_partition(dev, independent, legacy)

    def test_review_pending_and_fingerprint_drift_block_frozen_admission(self):
        lock = cases.validate_freeze()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'freeze.json'
            pending = deepcopy(lock)
            pending['status'] = 'awaiting_claude_review'
            pending['review'].update(status='pending', reviewer=None)
            path.write_text(json.dumps(pending), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Claude'):
                cases.validate_freeze(path, require_review=True)
            changed = deepcopy(lock)
            first = next(iter(changed['public_files_sha256']))
            changed['public_files_sha256'][first] = '0' * 64
            path.write_text(json.dumps(changed), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'drift'):
                cases.validate_freeze(path)
        self.assertEqual(lock['thresholds']['all']['minimum_correct'], 92)
        self.assertTrue(all(value['n'] >= 4 for value in lock['thresholds']['families'].values()))
        self.assertEqual(lock['thresholds']['semantic']['minimum_correct'],
                         cases.ceil(.95 * lock['holdout']['summary']['semantic_calls']))


if __name__ == '__main__':
    unittest.main()
