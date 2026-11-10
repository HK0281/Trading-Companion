"""Bounded diagnostic CLI tests; every provider response is synthetic."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from scripts import diagnose_general_knowledge as diagnostic
from scripts.verify import offline_network
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_VERSION


PRIVATE = 'synthetic-diagnostic-private-marker'


class GeneralKnowledgeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('No credentials')),
            patch('requests.get', side_effect=AssertionError('No source HTTP')),
            patch('requests.post', side_effect=AssertionError('No unmocked model HTTP')),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        tmp = TemporaryDirectory(dir=diagnostic.ROOT / '.agentbus/tmp')
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.output = self.path / 'run'

    def model(self):
        return DeepSeekToolModel(DeepSeekConfig(model='synthetic-answer', api_key=PRIVATE))

    def provider_response(self, question=None, *, invalid=False):
        etf = question == diagnostic.CASES[-1][1]
        answer = dict(version=GENERAL_KNOWLEDGE_VERSION,
                      paragraphs=[('【K1】' if invalid else '概念说明。' if etf else '【计算步骤】') + PRIVATE],
                      citations=[] if etf else [dict(paragraph=0, sources=['K1'])])
        result = Mock(status_code=200)
        result.json.return_value = dict(model='synthetic-answer', choices=[dict(finish_reason='stop',
            message=dict(role='assistant', content=json.dumps(answer, ensure_ascii=False)))],
            usage=dict(prompt_tokens=10, completion_tokens=20, total_tokens=30))
        return result

    def post(self, *args, **kwargs):
        return self.provider_response(kwargs['json']['messages'][1]['content'])

    def assert_private(self, output):
        for path in output.rglob('*'):
            if path.is_file():
                text = path.read_text(encoding='utf-8')
                for private in (PRIVATE, 'K1', '【计算步骤】', *(q for _, q, _ in diagnostic.CASES)):
                    self.assertNotIn(private, text, str(path))

    def test_default_plan_never_loads_configuration_or_writes(self):
        out = StringIO()
        with patch.object(diagnostic, 'load_live_model', side_effect=AssertionError('No model construction')), \
                patch.object(Path, 'mkdir', side_effect=AssertionError('No writes')), redirect_stdout(out):
            self.assertEqual(diagnostic.main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan['planned_attempts'], 14)
        self.assertEqual(plan['approval_message_id'], 543)
        self.assertIsNone(plan['max_calls'])
        self.assertEqual(sum(c['repeats'] for c in plan['cases']), 14)
        self.assertFalse(plan['retry'])
        self.assertTrue(plan['rewrite'])

    def test_live_requires_explicit_approved_ceiling_and_output_before_configuration(self):
        with patch.object(diagnostic, 'load_live_model', side_effect=AssertionError('No model construction')), \
                patch.object(Path, 'mkdir', side_effect=AssertionError('No writes')), redirect_stderr(StringIO()):
            for args in (['--live'], ['--live', '--max-calls', '14'], ['--live', '--output', str(self.output)]):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    diagnostic.main(args)
            for limit in ('0', '-1', '15'):
                self.assertEqual(diagnostic.main(['--live', '--max-calls', limit, '--output', str(self.output)]), 2)
        for limit in (None, 0, -1, 15, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                diagnostic.run_live(max_calls=limit, output=self.output, model_factory=self.model)

    def test_full_batch_uses_production_guards_and_counts_reasons_by_case_without_text(self):
        with patch('requests.post', side_effect=self.post) as post:
            report = diagnostic.run_live(max_calls=14, output=self.output, model_factory=self.model)
        self.assertEqual(post.call_count, 14)
        self.assertEqual(report['status'], 'observations_recorded')
        self.assertEqual(len(report['rows']), 14)
        self.assertEqual(report['summary']['status_counts'], dict(accepted=14))
        self.assertEqual(report['summary']['acceptance_counts'], dict(first_pass=14, after_rewrite=0))
        self.assertEqual(report['summary']['model_calls_attempted'], 14)
        self.assertEqual(report['summary']['provider_attempts'], 14)
        self.assertEqual(report['summary']['wire_started'], 14)
        self.assertEqual(report['summary']['responses_received'], 14)
        self.assertEqual(report['summary']['reason_by_case'], [])
        self.assertEqual(report['summary']['first_reason_by_case'], [])
        self.assertTrue(all(r['usage'] == dict(prompt_tokens=10, completion_tokens=20, total_tokens=30)
                            and r['usage_complete'] for r in report['rows']))
        for row in report['rows']:
            self.assertEqual(set(row), {'case_id', 'repeat', 'status', 'reason', 'code', 'diagnostics',
                                       'model_calls_attempted', 'responses_received', 'elapsed_ms', 'usage',
                                       'usage_complete', 'run_id'})
            self.assertEqual(set(row['diagnostics']), {'version', 'state', 'reason', 'first_reason',
                                                     'rewrite_attempts', 'acceptance', 'adjustments'})
        self.assertEqual(json.loads((self.output / 'report.json').read_text(encoding='utf-8')), report)
        self.assert_private(self.output)

    def test_rewrites_share_global_budget_and_keep_first_rejection_categories(self):
        with patch('requests.post', side_effect=[self.provider_response(invalid=True), self.provider_response(),
                self.provider_response(), self.provider_response(invalid=True), self.provider_response(invalid=True)]) as post:
            report = diagnostic.run_live(max_calls=5, output=self.output, model_factory=self.model)
        self.assertEqual(post.call_count, 5)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['summary']['status_counts'], dict(accepted=2, failed=1, not_measured=11))
        self.assertEqual(report['summary']['acceptance_counts'], dict(first_pass=1, after_rewrite=1))
        self.assertEqual(report['summary']['first_reason_by_case'],
                         [dict(reason='knowledge_bracket_reference', case_id='macd_base', count=2)])
        self.assertEqual(report['summary']['reason_by_case'],
                         [dict(reason='knowledge_bracket_reference', case_id='macd_base', count=1)])
        self.assertEqual(report['summary']['responses_received'], 5)
        self.assertEqual(report['summary']['provider_attempts'], 5)
        self.assertEqual([r['model_calls_attempted'] for r in report['rows'][:3]], [2, 1, 2])
        self.assertEqual([r['usage']['total_tokens'] for r in report['rows'][:3]], [60, 30, 60])
        for request in post.call_args_list:
            self.assertTrue(all(PRIVATE not in m['content'] for m in request.kwargs['json']['messages']))
        self.assert_private(self.output)

    def test_last_budget_slot_keeps_failure_without_starting_a_rewrite(self):
        with patch('requests.post', return_value=self.provider_response(invalid=True)) as post:
            report = diagnostic.run_live(max_calls=1, output=self.output, model_factory=self.model)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(report['rows'][0]['diagnostics']['reason'], 'knowledge_bracket_reference')
        self.assertEqual(report['rows'][0]['diagnostics']['rewrite_attempts'], 0)
        self.assertEqual(report['summary']['status_counts'], dict(failed=1, not_measured=13))
        self.assert_private(self.output)

    def test_failed_transport_attempts_count_toward_cap_and_are_never_retried(self):
        with patch('requests.post', side_effect=requests.exceptions.Timeout(PRIVATE)) as post:
            report = diagnostic.run_live(max_calls=2, output=self.output, model_factory=self.model)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(report['summary']['model_calls_attempted'], 2)
        self.assertEqual(report['summary']['provider_attempts'], 2)
        self.assertEqual(report['summary']['wire_started'], 2)
        self.assertEqual(report['summary']['responses_received'], 0)
        self.assertEqual(report['summary']['status_counts'], dict(failed=2, not_measured=12))
        for row in report['rows'][:2]:
            self.assertEqual(row['code'], 'general_knowledge_model_unavailable')
            self.assertEqual(row['diagnostics']['state'], 'not_run')
            self.assertEqual(row['model_calls_attempted'], 1)
            self.assertFalse(row['usage_complete'])
            self.assertEqual(row['usage'], dict(prompt_tokens=None, completion_tokens=None, total_tokens=None))
        self.assertTrue(all(r['reason'] == 'global_call_limit' for r in report['rows'][2:]))
        self.assert_private(self.output)

    def test_interruption_preserves_completed_and_unmeasured_rows_without_error_text(self):
        with patch('requests.post', side_effect=[self.provider_response(), KeyboardInterrupt(PRIVATE)]) as post:
            report = diagnostic.run_live(max_calls=14, output=self.output, model_factory=self.model)
        self.assertEqual(post.call_count, 2)
        self.assertEqual((report['status'], report['reason']), ('incomplete', 'interrupted'))
        self.assertEqual(len(report['rows']), 14)
        self.assertEqual(report['rows'][0]['diagnostics']['acceptance'], 'first_pass')
        self.assertEqual(report['summary']['model_calls_attempted'], 2)
        self.assertEqual(report['summary']['status_counts'], dict(accepted=1, not_measured=13))
        self.assertTrue(all(r['reason'] == 'interrupted' for r in report['rows'][1:]))
        self.assert_private(self.output)

    def test_configuration_failure_writes_only_fixed_failure_category(self):
        out, err = StringIO(), StringIO()
        with patch.object(diagnostic, 'load_live_model', side_effect=RuntimeError(PRIVATE)), \
                redirect_stdout(out), redirect_stderr(err):
            code = diagnostic.main(['--live', '--max-calls', '14', '--output', str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, out.getvalue() + err.getvalue())
        report = json.loads((self.output / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['reason'], 'diagnostic_unavailable')
        self.assertEqual(report['summary']['status_counts'], dict(not_measured=14))
        self.assertEqual(report['summary']['wire_started'], 0)
        self.assert_private(self.output)

    def test_paths_cannot_escape_or_overwrite_previous_runs(self):
        for path in (self.path, diagnostic.ROOT / 'data', self.path / '..' / '..' / '..' / 'outside'):
            with self.subTest(path=path), self.assertRaises((ValueError, FileExistsError)):
                diagnostic.run_live(max_calls=14, output=path, model_factory=Mock(side_effect=AssertionError('No models')))
        with patch.object(Path, 'resolve', return_value=diagnostic.ROOT / 'outside'):
            with self.assertRaises(ValueError):
                diagnostic.run_live(max_calls=14, output=self.output, model_factory=Mock())

    def test_historical_comparison_requires_exact_diagnostics_and_keeps_all_old_fields(self):
        from tests.knowledge_diagnostics_baseline_support import compare_observations
        from tests.routing_baseline_support import read_baseline
        from tests.routing_targets_support import TARGET, observe_target_scenario
        from tests.routing_horizon_support import overlay
        scenario = next(s for s in read_baseline(TARGET)['scenarios'] if s['id'] == 'knowledge-ready')
        observed, _ = observe_target_scenario(scenario)
        original = deepcopy(observed)
        expected = dict(id=scenario['id'], observed=overlay(scenario['id'], scenario['observed']))
        compare_observations(expected, observed)
        self.assertEqual(observed, original)
        for mutation in ('missing', 'extra', 'wrong_state', 'answer'):
            changed = deepcopy(observed)
            result = changed[0]['outcome']['response']['result']
            details = result['knowledge']['diagnostics']
            if mutation == 'missing':
                del result['knowledge']['diagnostics']
            elif mutation == 'extra':
                details['extra'] = PRIVATE
            elif mutation == 'wrong_state':
                details['state'] = 'not_run'
            else:
                result['answer'] += 'changed'
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                compare_observations(expected, changed, diagnostic_dir=self.path)


if __name__ == '__main__':
    unittest.main()
