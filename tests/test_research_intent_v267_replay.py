"""v2.6.6 is the only archive source for the v2.6.7 offline entry point."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v267 as replay
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample


class V267ReplayTests(OfflineTests):
    def test_fixed_v266_source_and_version_only_migration(self):
        archived = response().to_dict()
        archived['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        before = deepcopy(archived)
        migrated = replay.migrate_response(archived).to_dict()
        self.assertEqual(migrated['tool_calls'][0]['arguments'].pop('version'), 'research-intent-v2.6.7')
        migrated['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        self.assertEqual(migrated, before)
        self.assertEqual(archived, before)
        for split, source in replay.SOURCES.items():
            self.assertTrue(source.as_posix().endswith('research-intent-v2.6.6/public/'+split+'/report.json'))
        for version in ('research-intent-v2.6.5', 'research-intent-v2.6.7', 'private'):
            archived['tool_calls'][0]['arguments']['version'] = version
            with self.assertRaises(ValueError):
                replay.migrate_response(archived)

    def test_old_attempts_and_failures_stay_visible_and_are_not_new_results(self):
        row, missing = sample(), sample()
        missing['id'] = 'no-response'
        response_data = response().to_dict()
        response_data['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        old = dict(evaluation._empty('invalid', 'archived_failure'), response=response_data, attempts=2,
            path='semantic', format_retry=dict(attempted=True), attempt_history=[dict(failure='format'), dict(response=response_data)])
        source = dict(plan=dict(contract_version=replay.SOURCE_CONTRACT, runtime_revision='reviewed-v266'),
                      paid_calls=2, rows=[dict(row, V2=old)])
        before = deepcopy(source)
        with patch.object(evaluation, 'populations', return_value=([row, missing],
                {r['id']: deepcopy(r['gold']) for r in (row, missing)}, {}, {})), \
                patch.object(evaluation, 'build_context', return_value=(context(), 0)):
            report = replay.replay_report('dev', source)
        self.assertEqual(report['rows'][0]['archived_observation'], old)
        self.assertEqual(report['rows'][0]['V2']['raw']['status'], 'failed')
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual((report['new_model_results'], report['paid_calls'], report['passed']), (False, 0, None))
        self.assertFalse(report['eligible_for_switch'])
        self.assertIn('不是新成绩', report['notice'])
        self.assertEqual(source, before)

    def test_reject_custom_private_and_paid_options(self):
        for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                replay.main(args)
