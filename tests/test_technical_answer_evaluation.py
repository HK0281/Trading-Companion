"""Draft corpus, source-entry auditing, budgets, boundaries and complete report contracts, plus the individual answer checks."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from evaluation_tools import technical_answer_evaluation as evaluation
from evaluation_tools.technical_answer_evaluation_checks import (
    grade_turn,
    quality_metrics,
    unexpected_indicators,
)
from evaluation_tools.technical_answer_evaluation_fixtures import (
    NOW,
    TechnicalContractModel,
    TechnicalFixedSources,
)
from scripts import evaluate_technical_answers as cli
from trading_companion.company_research_contracts import ResearchDataRequest


# ---- test_technical_answer_evaluation: Draft corpus, source-entry auditing, budgets, boundaries and complete report contracts.

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


# ---- test_technical_answer_evaluation_checks: Adversarial grading examples: a happy scripted model cannot certify its own checker.

class TechnicalCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = evaluation.evaluate()
        cls.cases = evaluation.load_corpus()[0]

    def inputs(self, index=0, turn=0):
        case = self.cases[index]
        actual = deepcopy(self.report['cases'][index]['turns'][turn])
        result = dict(status=actual['turn_status'], answer=actual['answer'], parameters=actual['parameters'],
                      company_research=actual['company_research'])
        return case, case['turns'][turn], result, actual['observation'], actual['source_calls']

    def test_unreferenced_ma20_evidence_is_a_scope_violation(self):
        case, expected, result, observation, calls = self.inputs()
        evidence = deepcopy(result['company_research']['evidence'][0])
        evidence.update(id='injected-ma20', label='MA20', display='20日均线为10元')
        result['company_research']['evidence'].append(evidence)
        grade = grade_turn(case, expected, result, observation, calls)
        self.assertFalse(grade['passed'])
        self.assertIn('scope_evidence_indicator', [v['code'] for v in grade['scope']['violations']])

    def test_extra_indicators_in_prose_or_actual_computation_fail(self):
        for text in ('RSI6偏强。', 'ATR14为2元。', 'MA20提供支撑。', '5日均线拐头。', '布林带收窄。'):
            with self.subTest(text=text):
                case, expected, result, observation, calls = self.inputs()
                result['company_research']['narratives'][0]['current']['text'] = text
                self.assertFalse(grade_turn(case, expected, result, observation, calls)['scope']['passed'])
        case, expected, result, observation, calls = self.inputs()
        observation['technical_requests'][0].append('atr(window=14)')
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['passed'])

    def test_raw_volume_is_not_the_volume_indicator_and_aliases_keep_parameters(self):
        allowed = ['sma(window=3)']
        self.assertEqual(unexpected_indicators('daily_bar|volume_shares SMA(3) sma(window=3)', allowed), [])
        self.assertTrue(unexpected_indicators('volume(window=20)', allowed))
        self.assertEqual(unexpected_indicators('未计算ATR14，不使用RSI6。', allowed, prose=True), [])
        self.assertTrue(unexpected_indicators('未计算ATR14，但RSI6偏强。', allowed, prose=True))

    def test_naked_stop_or_target_and_structured_levels_without_plan_fail(self):
        for text in ('止损10元。', '目标价设为20元。', '止损价位为10.5。', '止损十元。'):
            with self.subTest(text=text):
                case, expected, result, observation, calls = self.inputs()
                result['answer'] = text
                grade = grade_turn(case, expected, result, observation, calls)
                self.assertIn('scope_plan_price_without_atr', [v['code'] for v in grade['scope']['violations']])
        case, expected, result, observation, calls = self.inputs()
        result['company_research']['narratives'][0]['levels'][0]['lower_label'] = '目标价'
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['passed'])

    def test_failed_news_fetch_is_a_violation_even_without_news_evidence(self):
        case, expected, result, observation, calls = self.inputs(9)
        calls.append(dict(instrument_id=case['instrument_id'], kind='news', status='failed'))
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['scope']['passed'])
        self.assertTrue(all(e['kind'] == 'technical' for e in result['company_research']['evidence']))

    def test_negative_target_statement_is_not_a_structured_plan_claim(self):
        for text in ('SMA5本身不提供目标价', '没有止损价，支撑参考25元', '目标价无法给出'):
            with self.subTest(text=text):
                case, expected, result, observation, calls = self.inputs(4)
                result['company_research']['narratives'][0]['levels'][0]['text'] = text
                result['answer'] = text
                grade = grade_turn(case, expected, result, observation, calls)
                self.assertEqual(grade['scope']['violations'], [])
        case, expected, result, observation, calls = self.inputs(4)
        result['company_research']['narratives'][0]['levels'][0]['text'] = '止损参考位'
        self.assertIn('scope_structured_plan_without_atr', [v['code'] for v in
            grade_turn(case, expected, result, observation, calls)['scope']['violations']])
        result['answer'] = '不提供目标价，但止损20元'
        self.assertIn('scope_plan_price_without_atr', [v['code'] for v in
            grade_turn(case, expected, result, observation, calls)['scope']['violations']])

    def test_absent_audit_or_wrong_identity_never_passes(self):
        case, expected, result, observation, calls = self.inputs()
        grade = grade_turn(case, expected, result, None, None)
        self.assertFalse(grade['scope']['passed'])
        self.assertEqual(len(grade['scope']['unknown']), 2)
        result['parameters']['instrument_id'] = '000001.SZ'
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['formed'])

    def test_decision_direction_and_unavailable_plan_explanation_are_required(self):
        case, expected, result, observation, calls = self.inputs(6, 1)
        grade = grade_turn(case, expected, result, observation, calls)
        self.assertTrue(grade['checks']['reference_plan'])
        self.assertIn('未计算', grade['plan_unavailable_explanation'])
        result['company_research']['decisions'][0]['direction'] = '偏空'
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['checks']['rating_direction'])
        for plan in result['company_research']['trade_plans']:
            plan['code'] = ''
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['checks']['reference_plan'])

    def test_available_plan_requires_atr_evidence_not_a_claimed_plan_status(self):
        case, expected, result, observation, calls = self.inputs(5, 1)
        self.assertTrue(grade_turn(case, expected, result, observation, calls)['checks']['reference_plan'])
        result['company_research']['evidence'] = [e for e in result['company_research']['evidence']
                                                if e.get('content_kind') != 'trade_plan']
        self.assertFalse(grade_turn(case, expected, result, observation, calls)['checks']['reference_plan'])

    def test_facts_only_or_empty_v3_never_counts_as_model_analysis(self):
        for field, value in (('origin', 'application'), ('narratives', [])):
            case, expected, result, observation, calls = self.inputs()
            result['company_research'][field] = value
            self.assertFalse(grade_turn(case, expected, result, observation, calls)['formed'])

    def test_unknown_quality_counts_are_not_zero_and_drafts_are_not_double_counted(self):
        unknown = quality_metrics({})
        self.assertIsNone(unknown['generated_sentences'])
        self.assertIsNone(unknown['output_truncations'])
        self.assertIsNone(unknown['draft_failure_counts']['all_drafts']['answer_stance_mismatch'])
        quality = dict(issue_count_version='answer-issues-v1', draft_issue_counts=[{'answer_numeric_unmatched': 3}, {}],
                       reasons=[], generated_sentences=10, deleted_sentences=3)
        counts = quality_metrics(quality)
        self.assertEqual(counts['draft_failure_counts']['all_drafts']['answer_numeric_unmatched'], 3)
        self.assertEqual(counts['draft_failure_counts']['repair_draft']['answer_numeric_unmatched'], 0)
        self.assertEqual(counts['final_reason_counts']['answer_numeric_unmatched'], 0)


if __name__ == '__main__':
    unittest.main()
