"""Synthetic archive provenance and version-only offline replay tests."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
import unittest
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v261 as replay
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, draft, response
from test_research_intent_v25_evaluation import OfflineTests, sample


def archive_response(value=None):
    saved = response(value or draft()).to_dict()
    saved['tool_calls'][0]['arguments']['version'] = 'research-intent-v2.6'
    return saved


class ReplayTests(OfflineTests):
    def test_migration_changes_only_version_on_copy_and_never_adds_reference(self):
        saved = archive_response()
        before = deepcopy(saved)
        migrated = replay.migrate_response(saved).to_dict()
        migrated['tool_calls'][0]['arguments']['version'] = 'research-intent-v2.6'
        self.assertEqual(migrated, before)
        self.assertEqual(saved, before)
        del saved['tool_calls'][0]['arguments']['subject']['reference']
        migrated = replay.migrate_response(saved)
        self.assertNotIn('reference', migrated.tool_calls[0].arguments['subject'])
        checked = evaluation.checked_result('研究平安银行', migrated, context())
        self.assertEqual(checked['status'], 'invalid')
        saved['tool_calls'][0]['arguments']['version'] = replay.intent.VERSION
        with self.assertRaises(ValueError):
            replay.migrate_response(saved)

    def run_replay(self, rows, archived):
        effective = {r['id']: deepcopy(r['gold']) for r in rows}
        with patch.object(evaluation, 'populations', return_value=(rows, effective, {}, {})), \
                patch.object(evaluation, 'build_context', return_value=(context(), 0)):
            return replay.replay_report('dev', archived)

    def test_missing_response_stays_unmeasured_and_every_archived_failure_is_retained(self):
        row, missing = sample(), sample()
        missing['id'] = 'missing-response'
        original = dict(evaluation._empty('invalid', 'reference_fields_conflict'), attempts=2,
                        response=archive_response(), path='semantic', format_retry=dict(attempted=True),
                        attempt_history=[dict(failure='tool_arguments_json_invalid', response=None),
                                         dict(response=archive_response(), failure=None)])
        archived = dict(plan=dict(contract_version='research-intent-v2.6', runtime_revision='old-commit'),
                        paid_calls=2, rows=[dict(row, V2=original)])
        before = deepcopy(archived)
        report = self.run_replay([row, missing], archived)
        self.assertEqual((report['paid_calls'], report['new_model_results'], report['passed']), (0, False, None))
        self.assertFalse(report['eligible_for_switch'])
        self.assertEqual(report['rows'][0]['archived_observation'], original)
        self.assertEqual(report['rows'][0]['V2']['raw']['status'], 'failed')
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual(report['telemetry']['attempts'], 0)
        self.assertIn('不是新成绩', report['title'])
        self.assertEqual(archived, before)

    def test_drift_duplicate_unknown_input_and_private_cli_are_rejected(self):
        row = sample()
        original = dict(row, V2=evaluation._empty('not_measured', 'missing'))
        for source in ([dict(original, question='changed')], [original, original], [dict(original, id='unknown')]):
            with self.assertRaises(ValueError):
                self.run_replay([row], dict(plan=dict(contract_version='research-intent-v2.6'), rows=source))
        for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                replay.main(args)


if __name__ == '__main__':
    unittest.main()
