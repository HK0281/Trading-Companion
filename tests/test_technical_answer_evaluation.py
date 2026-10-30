"""Draft corpus, source-entry auditing, budgets, boundaries and complete report contracts."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_technical_answers as cli
from trading_companion import technical_answer_evaluation as evaluation
from trading_companion.company_research_contracts import ResearchDataRequest
from trading_companion.model import ModelCallError
from trading_companion.technical_answer_evaluation_fixtures import NOW, TechnicalContractModel, TechnicalFixedSources
from trading_companion.tool_model import ToolModelResponse


class TechnicalEvaluationTests(unittest.TestCase):
    def test_horizon_clarifications_are_separate_and_strict_sma_never_supplements(self):
        metrics = self.report['metrics']
        self.assertEqual((metrics['original_turns'], metrics['supplemental_turns'], metrics['actual_turns']), (14,1,15))
        t06 = next(r for r in self.report['cases'] if r['id'] == 'T06')['turns'][-1]
        t07 = next(r for r in self.report['cases'] if r['id'] == 'T07')['turns'][-1]
        self.assertEqual(t06['original_turn']['code'], 'decision_horizon_required')
        self.assertEqual(t06['clarification_turns'][0]['question'], '中线')
        self.assertEqual(t07['clarification_turns'], [])
        self.assertNotEqual(t07['original_turn']['code'], 'decision_horizon_required')

    @classmethod
    def setUpClass(cls):
        cls.report = evaluation.evaluate()

    def test_ten_frozen_draft_questions_four_multiturn_cases_and_volume_profile(self):
        cases, manifest = evaluation.load_corpus()
        self.assertEqual((len(cases), sum(len(c['turns']) for c in cases)), (10, 14))
        self.assertEqual(manifest['multi_turn_cases'], ['T06', 'T07', 'T08', 'T10'])
        self.assertIn('volume(window=20)', cases[8]['turns'][0]['required_indicators'])
        self.assertEqual(manifest['review_status'], 'draft_requires_manual_review')
        old = json.loads((evaluation.ROOT / 'data/evaluation/decision-answer-v1.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertNotIn(old['instrument_id'], {c['instrument_id'] for c in cases})
        with TemporaryDirectory() as root:
            corpus = Path(root) / 'tampered.jsonl'
            corpus.write_bytes(evaluation.CORPUS.read_bytes() + b'\n')
            with self.assertRaises((ValueError, evaluation.PlanError)):
                evaluation.load_corpus(corpus)

    def test_offline_real_entry_forms_ten_and_observes_all_fourteen_turns(self):
        report = self.report
        self.assertEqual((report['status'], report['metrics']['formed']), ('passed', 10))
        self.assertEqual(report['accounting']['forwarded'], 26)
        self.assertEqual(report['metrics']['scope_violations'], 0)
        self.assertEqual(report['metrics']['scope_unknown_turns'], 0)
        self.assertEqual(report['metrics']['all_turns']['output_truncations'], 0)
        self.assertEqual(report['quality_target_claim'], 'not_evaluated_offline')
        self.assertEqual(report['offline_credential_attempts'], 0)
        for row in report['cases']:
            self.assertTrue(row['formed'])
            self.assertTrue(all(t['trace_complete'] and t['script_complete'] for t in row['turns']))
            self.assertIn('target', row['metrics'])
            self.assertLessEqual(row['model_calls'], 6 * len(row['turns']))
            for turn in row['turns']:
                self.assertLessEqual(turn['model_calls'], 6)
                self.assertTrue(turn['issue_counts_known'])
                self.assertTrue(any(call['kind'] == 'technical' for call in turn['source_calls']))
                self.assertEqual(turn['route']['kind'], 'company_research')
        self.assertTrue(report['metrics']['decision_checks_passed'])

    def test_preview_accepts_requested_90_but_exposes_effective_84_without_model(self):
        with patch.object(evaluation, 'build_real_model', side_effect=AssertionError('No model')):
            plan = evaluation.build_plan(mode='live-model', provider='deepseek', model='deepseek-flash',
                prices=evaluation.DEFAULT_PRICES, model_call_limit=90)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual((plan['requested_model_call_limit'], plan['model_call_limit']), (90, 84))
        self.assertEqual(plan['maximum_calls_per_question']['T06'], 12)
        self.assertEqual(plan['maximum_calls_per_question']['T01'], 6)
        self.assertEqual(plan['maximum_calls_per_turn'], 6)

    def test_budget_failure_keeps_ten_denominator_and_is_separate_from_quality(self):
        report = evaluation.evaluate(model_call_limit=1)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['cases']), 10)
        self.assertEqual(report['accounting']['forwarded'], 1)
        self.assertEqual(report['metrics']['formation_rate']['denominator'], 10)
        self.assertEqual(report['metrics']['budget_exhausted_questions'], 10)
        self.assertEqual(report['metrics']['quality_failed_questions'], 0)
        self.assertIsNone(report['metrics']['all_turns']['output_truncations'])

    def test_formation_threshold_and_setup_scope_are_independent(self):
        for formed, target in ((8, False), (9, True), (10, True)):
            rows = deepcopy(self.report['cases'])
            for row in rows[formed:]:
                row['formed'] = False
            self.assertEqual(evaluation.summarize(rows)['formation_target_met'], target)
        rows = deepcopy(self.report['cases'])
        rows[5]['turns'][0]['scope']['violations'].append({'code': 'injected_setup_scope_violation'})
        metrics = evaluation.summarize(rows)
        self.assertEqual(metrics['formed'], 10)
        self.assertFalse(metrics['scope_target_met'])

    def test_aggregate_rates_use_sentence_totals_and_unknowns_stay_unknown(self):
        rows = deepcopy(self.report['cases'])
        targets = [row['turns'][-1] for row in rows]
        targets[0].update(generated_sentences=100, deleted_sentences=10)
        counts = evaluation.aggregate_turns(targets)
        self.assertEqual(counts['sentence_deletion_rate']['numerator'], sum(t['deleted_sentences'] for t in targets))
        targets[0]['issue_counts_known'] = False
        targets[0]['generated_sentences'] = None
        targets[0]['output_truncations'] = None
        counts = evaluation.aggregate_turns(targets)
        self.assertIsNone(counts['sentence_deletion_rate'])
        self.assertIsNone(counts['draft_failure_counts']['all_drafts']['answer_stance_mismatch'])
        self.assertIsNone(counts['output_truncations'])

    def test_source_entry_records_failed_attempt_before_no_evidence_return(self):
        cases = evaluation.load_corpus()[0]
        with TechnicalFixedSources(cases, 1) as sources:
            with patch.object(sources.company, 'collect_many', side_effect=RuntimeError('private-fetch-failure')):
                with evaluation.CollectionAudit(sources) as audit:
                    request = ResearchDataRequest(cases[0]['instrument_id'], cases[0]['security_name'], NOW)
                    with self.assertRaises(RuntimeError):
                        sources.company.fetch_section(request, 'news')
            self.assertEqual(audit.calls, [dict(instrument_id=cases[0]['instrument_id'], kind='news', status='failed')])

    def test_model_initialization_failure_retains_rows_without_private_errors(self):
        with patch.object(evaluation, 'TechnicalContractModel', side_effect=RuntimeError('PRIVATE-ERROR')):
            report = evaluation.evaluate()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['cases']), 10)
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertIsNone(report['cost']['estimated_total'])

    def test_invalid_options_reject_before_constructing_model(self):
        for options in ({'model_call_limit': 0}, {'model_call_limit': True}, {'mode': 'offline', 'provider': 'deepseek'},
                        {'mode': 'live', 'provider': 'deepseek', 'model': 'deepseek-flash'}, {'assumed_input_tokens': -1}):
            with self.subTest(options=options), patch.object(evaluation, 'build_real_model') as model:
                self.assertEqual(evaluation.evaluate(**options)['status'], 'invalid_request')
                model.assert_not_called()

    def test_reports_keep_manual_review_rows_and_preview_writes_nothing(self):
        with TemporaryDirectory() as root:
            output = Path(evaluation.write_report(self.report, root))
            self.assertIn('草稿题库、需人工复核', (output / 'report.md').read_text(encoding='utf-8'))
            reviews = [json.loads(line) for line in (output / 'manual-review.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(reviews), 10)
            self.assertTrue(all(row['reviewed_by'] is None for row in reviews))
            preview = Path(root) / 'preview'
            with redirect_stdout(io.StringIO()):
                code = cli.main(['--plan-only', '--report-dir', str(preview)])
            self.assertEqual(code, 0)
            self.assertFalse(preview.exists())
