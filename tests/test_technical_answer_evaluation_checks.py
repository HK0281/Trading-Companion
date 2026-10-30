"""Adversarial grading examples: a happy scripted model cannot certify its own checker."""
from copy import deepcopy
import unittest

from trading_companion import technical_answer_evaluation as evaluation
from trading_companion.technical_answer_evaluation_checks import grade_turn, quality_metrics, unexpected_indicators


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
