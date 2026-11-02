"""Independent archive fixtures; never read sealed or live provider materials."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v25 as replay
from evaluation_tools import research_intent_v25_evaluation as experiment
from test_research_intent_v2 import draft, response
from test_research_intent_v25_evaluation import sample


class ReplayTests(unittest.TestCase):
    def test_migration_changes_only_version_in_a_copy_and_rejects_other_versions(self):
        raw = response(draft(version='research-intent-v2.4')).to_dict()
        original = deepcopy(raw)
        migrated = replay.migrate_response(raw, 'v2.4')
        self.assertEqual(raw, original)
        self.assertEqual(migrated.tool_calls[0].arguments['version'], 'research-intent-v2.6')
        for version in ('research-intent-v2.3', 'research-intent-v2.5.3'):
            raw['tool_calls'][0]['arguments']['version'] = version
            with self.assertRaises(ValueError):
                replay.migrate_response(raw, 'v2.4')

    def test_no_model_replay_preserves_original_missing_failures_and_marks_every_table(self):
        row = sample()
        second = dict(deepcopy(row), id='missing-archive')
        raw = response(draft(version='research-intent-v2.4')).to_dict()
        archived = dict(row, V2=dict(status='ok', attempts=1, response=raw, fields=row['gold']))
        original = deepcopy(archived)
        with patch.object(experiment, 'populations', return_value=([row, second],
                {r['id']: r['gold'] for r in (row, second)}, {}, {})), \
                patch.object(experiment.core, '_live_model', side_effect=AssertionError('No model')):
            result = replay.replay_reports([dict(rows=[archived])], 'v2.4')
        self.assertEqual(archived, original)
        self.assertEqual(result['paid_calls'], 0)
        self.assertFalse(result['new_model_results'])
        self.assertIsNone(result['passed'])
        self.assertIsNone(result['thresholds'])
        self.assertEqual(result['score_views']['normalized_approved']['all']['joint_all_fields']['denominator'], 2)
        self.assertEqual(result['rows'][1]['V2']['status'], 'not_measured')
        rendered = experiment.markdown(result)
        self.assertIn(experiment.REPLAY_NOTICE, rendered.splitlines()[0])
        headers = [line for line in rendered.splitlines() if line.startswith('| ') and
                   ('视图 |' in line or '逐题 |' in line)]
        self.assertEqual(len(headers), 2)
        self.assertTrue(all(experiment.REPLAY_NOTICE in line for line in headers))

    def test_archive_context_mismatch_and_duplicate_are_rejected(self):
        row = sample()
        bad = dict(row, question='a different question', V2={})
        with patch.object(experiment, 'populations', return_value=([row], {row['id']: row['gold']}, {}, {})):
            with self.assertRaises(ValueError):
                replay.replay_reports([dict(rows=[bad])])
            with self.assertRaises(ValueError):
                replay.replay_reports([dict(rows=[dict(row, V2={}), dict(row, V2={})])])


if __name__ == '__main__':
    unittest.main()
