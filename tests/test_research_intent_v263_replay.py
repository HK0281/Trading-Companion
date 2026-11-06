"""Only v2.6.2 public response archives may enter the v2.6.3 offline replay."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v263 as replay
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample


class V263ReplayTests(OfflineTests):
    def test_fixed_source_contract_and_private_version_only_migration(self):
        saved = response().to_dict()
        with self.assertRaises(ValueError):
            replay.migrate_response(saved)
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        before = deepcopy(saved)
        migrated = replay.migrate_response(saved).to_dict()
        self.assertEqual(migrated['tool_calls'][0]['arguments']['version'], 'research-intent-v2.6.5')
        migrated['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        self.assertEqual(migrated, before)
        self.assertEqual(saved, before)
        for split, source in replay.SOURCES.items():
            self.assertTrue(source.as_posix().endswith('research-intent-v2.6.2/public/'+split+'/report.json'))

    def test_saved_attempts_failures_missing_rows_and_original_archive_are_preserved_without_model_calls(self):
        row, missing = sample(), sample()
        missing['id'] = 'missing'
        saved = response().to_dict()
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        original = dict(evaluation._empty('invalid', 'old-failure'), response=saved, attempts=2, path='semantic',
            format_retry=dict(attempted=True), attempt_history=[dict(response=None, failure='tool_arguments_json_invalid'),
                                                              dict(response=saved, failure=None)])
        archive = dict(plan=dict(contract_version=replay.SOURCE_CONTRACT, runtime_revision='reviewed-v262'),
                       paid_calls=2, rows=[dict(row, V2=original)])
        before = deepcopy(archive)
        with patch.object(evaluation, 'populations', return_value=([row, missing],
                {r['id']: deepcopy(r['gold']) for r in (row, missing)}, {}, {})), \
                patch.object(evaluation, 'build_context', return_value=(context(), 0)):
            report = replay.replay_report('dev', archive)
        self.assertEqual(report['rows'][0]['archived_observation'], original)
        self.assertEqual(report['rows'][0]['V2']['raw']['status'], 'failed')
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual((report['paid_calls'], report['new_model_results'], report['passed']), (0, False, None))
        self.assertEqual(report['source_revision'], 'reviewed-v262')
        self.assertFalse(report['eligible_for_switch'])
        self.assertIn('不是新成绩', report['notice'])
        self.assertEqual(archive, before)
        with self.assertRaises(ValueError):
            replay.replay_report('dev', dict(archive, plan=dict(contract_version='research-intent-v2.6.1')))

    def test_no_custom_sealed_or_paid_cli_inputs(self):
        for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                replay.main(args)
