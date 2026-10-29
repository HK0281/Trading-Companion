from copy import deepcopy
import unittest

from trading_companion.intent_router_metrics import (scores,provider_metrics,calibration,selective,choose_threshold,THRESHOLDS,
    cascade,wilson,cutover)
from tests.test_news_triage_metrics import prices


def row(gold,prediction,confidence=.8,*,status='ok'):
    value=dict(status=status,prediction=prediction,confidence=confidence,attempted=status!='not_run',model='jev-latest',elapsed_ms=10,
               usage=dict(prompt_tokens=100,completion_tokens=10,total_tokens=110),code='ok')
    return dict(gold=gold,tags=['near_miss','paraphrase'],forbidden_intents=['screening'],results={'jev':value,'rules':deepcopy(value)})


class IntentMetricTests(unittest.TestCase):
    def test_hand_counted_scores_and_fair_subset(self):
        rows=[row('company_research','company_research'),row('company_research','screening'),row('clarify','clarify'),row('clarify',None,status='failed')]
        result=scores(rows,'jev')
        self.assertEqual(result['accuracy']['value'],.5)
        self.assertAlmostEqual(result['classes']['company_research']['f1'],2/3)
        self.assertEqual(result['confusion']['clarify']['failed'],1)
        self.assertEqual(scores(rows,'jev',non_clarify=True)['accuracy']['denominator'],2)
        self.assertEqual(len(scores(rows,'jev',non_clarify=True)['classes']),7)
        m=provider_metrics(rows,'rules')
        self.assertEqual(m['misroute_rate']['value'],.25)
        self.assertEqual(m['correct_ask']['value'],.5)
        self.assertEqual(m['false_trigger_rate']['value'],.25)
        self.assertEqual(m['executor_clarification']['status'],'not_determined')

    def test_hand_counted_ece_selective_and_wilson(self):
        rows=[row('company_research','company_research'),row('company_research','screening'),row('clarify',None,status='not_run')]
        self.assertAlmostEqual(calibration(rows,'jev')['ece'],.3)
        self.assertEqual(calibration(rows,'jev')['coverage']['value'],2/3)
        self.assertEqual(selective(rows,.8)['accuracy']['value'],.5)
        self.assertIsNone(wilson(0,0)['lower'])
        self.assertAlmostEqual(wilson(50,100)['lower'],.4038315304)
        self.assertIsNone(choose_threshold(rows))
        good=[row('company_research','company_research',.95) for _ in range(100)]
        self.assertEqual(choose_threshold(good),.95)

    def test_counterfactual_cascade_counts_serial_latency_missing_calls_and_currency(self):
        rows=[row('company_research','company_research',.95),row('company_research','screening',.5)]
        for r in rows:
            r['results']['deepseek']={**r['results']['jev'],'model':'ds','prediction':'company_research','elapsed_ms':30}
        point=cascade(rows,.9,{'jev':'jev-latest','deepseek':'ds'},prices(),fallback='deepseek')
        self.assertEqual(point['scores']['accuracy']['value'],1)
        self.assertEqual(point['fallback_demand']['value'],.5)
        self.assertEqual(point['estimated_serial_latency']['p95_ms'],40)
        self.assertEqual(point['cost_by_provider']['jev']['currency'],'USD')
        self.assertEqual(point['cost_by_provider']['deepseek']['currency'],'CNY')
        rows[1]['results']['deepseek'].update(status='not_run',attempted=False,elapsed_ms=None)
        missing=cascade(rows,.9,{'jev':'jev-latest','deepseek':'ds'},prices(),fallback='deepseek')
        self.assertEqual(missing['missing_required_results'],1)
        self.assertEqual(missing['estimated_serial_latency']['n'],1)

    def test_missing_and_ask_are_separate(self):
        rows=[row('company_research','clarify'),row('clarify','company_research'),row('clarify',None,status='not_run')]
        m=provider_metrics(rows,'rules')
        self.assertEqual(m['excess_ask']['value'],1/3)
        self.assertEqual(m['forced_route']['value'],.5)
        self.assertEqual(m['statuses']['not_run'],1)

    def test_unattempted_jev_cannot_be_counted_as_correct_cascade_abstention(self):
        missing=row('clarify',None,status='not_run')
        result=cascade([missing],.9,{'jev':'jev-latest'},prices(),fallback='ask')
        self.assertEqual(result['scores']['accuracy']['numerator'],0)
        self.assertEqual(result['scores']['confusion']['clarify']['not_run'],1)
        self.assertEqual(result['missing_required_results'],1)
