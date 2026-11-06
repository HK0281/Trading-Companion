"""v2.6.3 archives stay immutable and cannot become new v2.6.4 model results."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v264 as replay
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample


class V264ReplayTests(OfflineTests):
    def test_only_fixed_public_v263_envelopes_are_migrated_and_only_the_version_changes(self):
        saved = response().to_dict()
        with self.assertRaises(ValueError):
            replay.migrate_response(saved)
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        original = deepcopy(saved)
        copied = replay.migrate_response(saved).to_dict()
        self.assertEqual(copied['tool_calls'][0]['arguments']['version'], 'research-intent-v2.6.5')
        copied['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        self.assertEqual(copied, original)
        self.assertEqual(saved, original)
        for split, source in replay.SOURCES.items():
            self.assertTrue(source.as_posix().endswith('research-intent-v2.6.3/public/'+split+'/report.json'))
        for version in ('research-intent-v2.6.2', 'research-intent-v2.6.4', 'other'):
            saved['tool_calls'][0]['arguments']['version'] = version
            with self.assertRaises(ValueError):
                replay.migrate_response(saved)

    def test_old_failures_attempts_and_missing_responses_remain_visible_without_model_calls(self):
        row, missing = sample(), sample()
        missing['id'] = 'missing'
        saved = response().to_dict()
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        old = dict(evaluation._empty('invalid', 'old_failure'), response=saved, attempts=2, path='semantic',
            format_retry=dict(attempted=True), attempt_history=[dict(failure='tool_arguments_json_invalid'),
                                                              dict(response=saved, failure=None)])
        archive = dict(plan=dict(contract_version=replay.SOURCE_CONTRACT, runtime_revision='reviewed-v263'),
                       paid_calls=2, rows=[dict(row, V2=old)])
        before = deepcopy(archive)
        with patch.object(evaluation, 'populations', return_value=([row, missing],
                {r['id']: deepcopy(r['gold']) for r in (row, missing)}, {}, {})), \
                patch.object(evaluation, 'build_context', return_value=(context(), 0)):
            report = replay.replay_report('dev', archive)
        self.assertEqual(report['rows'][0]['archived_observation'], old)
        self.assertEqual(report['rows'][0]['V2']['raw']['status'], 'failed')
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual((report['paid_calls'], report['new_model_results'], report['passed']), (0, False, None))
        self.assertFalse(report['eligible_for_switch'])
        self.assertEqual(report['source_revision'], 'reviewed-v263')
        self.assertIn('不是新成绩', report['notice'])
        self.assertEqual(archive, before)
        with self.assertRaises(ValueError):
            replay.replay_report('dev', dict(archive, plan=dict(contract_version='research-intent-v2.6.2')))

    def test_no_custom_sources_sealed_or_paid_options(self):
        for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                replay.main(args)
