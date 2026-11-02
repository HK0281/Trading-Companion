"""Fixed v2.6.1 public archives are replayed without altering their observations."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from evaluation_tools import replay_research_intents_v262 as replay
from evaluation_tools import research_intent_v26_evaluation as evaluation
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample


class V262ReplayTests(OfflineTests):
    def test_replay_requires_v261_response_and_changes_only_private_version(self):
        saved = response().to_dict()
        with self.assertRaises(ValueError):
            replay.migrate_response(saved)
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        before = deepcopy(saved)
        migrated = replay.migrate_response(saved).to_dict()
        migrated['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        self.assertEqual(migrated, before)
        self.assertEqual(saved, before)

    def test_replay_preserves_invalid_attempts_and_missing_denominator_without_calls(self):
        row, missing = sample(), sample()
        missing['id'] = 'missing'
        saved = response().to_dict()
        saved['tool_calls'][0]['arguments']['version'] = replay.SOURCE_CONTRACT
        original = dict(evaluation._empty('invalid', 'original-failure'), response=saved, attempts=1,
                        path='semantic', attempt_history=[dict(response=saved, failure=None, call_ms=1)])
        archived = dict(plan=dict(contract_version=replay.SOURCE_CONTRACT, runtime_revision='v261-commit'),
                        paid_calls=1, rows=[dict(row, V2=original)])
        before = deepcopy(archived)
        with patch.object(evaluation, 'populations', return_value=([row, missing],
                {r['id']: deepcopy(r['gold']) for r in (row, missing)}, {}, {})), \
                patch.object(evaluation, 'build_context', return_value=(context(), 0)):
            report = replay.replay_report('dev', archived)
        self.assertEqual(report['rows'][0]['archived_observation'], original)
        self.assertEqual(report['rows'][1]['V2']['status'], 'not_measured')
        self.assertEqual((report['paid_calls'], report['new_model_results'], report['passed']), (0, False, None))
        self.assertFalse(report['eligible_for_switch'])
        self.assertEqual(report['source_revision'], 'v261-commit')
        self.assertEqual(report['plan']['source_contract'], replay.SOURCE_CONTRACT)
        self.assertIn('不是新成绩', report['notice'])
        self.assertEqual(archived, before)
        with self.assertRaises(ValueError):
            replay.replay_report('dev', dict(archived, plan=dict(contract_version='research-intent-v2.6')))

    def test_replay_has_only_fixed_public_cli_inputs(self):
        for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                replay.main(args)
