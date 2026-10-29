from copy import deepcopy
import unittest

from trading_companion.news_triage_metrics import calibration, cascade, costs, percentiles, scores, selective


def observation(relevance="primary", *, confidence=.8, good=True):
    gold={"relevance":relevance,"event_type":"capital","tone":"neutral"}
    answers={k:{"choice":v,"confidence":confidence,"probabilities":None} for k,v in gold.items()} if good else None
    return {"gold":gold,"answers":answers,"attempted":True,"model":"jev-latest",
            "usage":{"prompt_tokens":100,"completion_tokens":10,"total_tokens":110},"elapsed_ms":10}


def prices():
    return {"entries":[{"provider":p,"model":m,"currency":c,"verified_at":"2026-10-01","source_url":"https://example.invalid/pricing",
             "input_per_million":"1","output_per_million":"0","input_pricing":"uniform"}
             for p,m,c in (("typesafe","jev-latest","USD"),("deepseek","ds","CNY"))]}


class MetricTests(unittest.TestCase):
    def test_hand_calculated_f1_missing_and_combined_denominators(self):
        a,b,c,d=observation(),observation(),observation("mentioned"),observation("mentioned",good=False)
        b['answers']['relevance']['choice']='mentioned'
        c['answers']['relevance']['choice']='primary'
        report=scores([a,b,c,d])
        relevance=report['questions']['relevance']
        self.assertEqual(relevance['accuracy']['value'],.25)
        self.assertEqual(relevance['classes']['primary']['f1'],.5)
        self.assertEqual(relevance['classes']['mentioned']['f1'],0)
        self.assertAlmostEqual(relevance['macro_f1'],1/6)
        self.assertEqual(relevance['confusion']['mentioned']['missing'],1)
        self.assertEqual(report['combined_accuracy']['numerator'],7)
        self.assertEqual(report['combined_accuracy']['denominator'],12)
        self.assertEqual(report['joint_accuracy']['numerator'],1)
        self.assertEqual(len(report['combined_confusion']),14)
        self.assertIsNone(scores([])['combined_accuracy']['value'])

    def test_hand_calculated_ece_boundaries_and_missing_coverage(self):
        a,b,c=observation(confidence=.8),observation(confidence=.8),observation(good=False)
        b['answers']['relevance']['choice']='mentioned'
        report=calibration([a,b,c],['relevance'])
        self.assertAlmostEqual(report['ece'],.3)
        self.assertEqual(report['coverage']['numerator'],2)
        self.assertEqual(report['coverage']['denominator'],3)
        self.assertEqual(report['bins'][8]['n'],2)
        for confidence,index in ((0,0),(.1,1),(.5,5),(.95,9),(1,9)):
            r=calibration([observation(confidence=confidence)],['relevance'])
            self.assertEqual(r['bins'][index]['n'],1)
        self.assertIsNone(calibration([c],['relevance'])['ece'])

    def test_request_level_cascade_costs_and_selective_threshold(self):
        a,b=observation(confidence=.9),observation(confidence=.6)
        b['answers']['tone']['confidence']=.4
        da,db=observation(),observation()
        for r in (da,db):
            r['model']='ds'
            r['elapsed_ms']=30
        rows=[{'gold':r['gold'],'results':{'jev':r,'deepseek':d}} for r,d in ((a,da),(b,db))]
        curve=cascade(rows,{'jev':'jev-latest','deepseek':'ds'},prices())
        point=curve[0]
        self.assertEqual(point['deepseek_demand']['value'],.5)
        self.assertEqual(point['cost_by_provider']['jev']['estimated_total'],'0.000200')
        self.assertEqual(point['cost_by_provider']['deepseek']['estimated_total'],'0.000100')
        self.assertEqual(point['cost_by_provider']['jev']['currency'],'USD')
        self.assertEqual(point['cost_by_provider']['deepseek']['currency'],'CNY')
        self.assertEqual(point['estimated_serial_latency']['p95_ms'],40)
        self.assertEqual(point['scores']['joint_accuracy']['value'],1)
        self.assertEqual(selective([a,b])[0]['title_coverage']['value'],.5)
        # At 0.95 neither headline is selected; there is no extra model invocation.
        self.assertEqual(curve[-1]['deepseek_demand']['value'],1)
        db['answers']=None
        missing=cascade(rows,{'jev':'jev-latest','deepseek':'ds'},prices())[0]
        self.assertEqual(missing['missing_required_results'],1)
        self.assertEqual(missing['scores']['joint_accuracy']['value'],.5)

    def test_unknown_alias_usage_and_zero_sample_quantiles(self):
        row=observation()
        row['model']='served-version'
        self.assertIsNone(costs([row],'jev','jev-latest',prices())['estimated_total'])
        row['model']='jev-latest'
        row['usage']['total_tokens']=999
        self.assertEqual(costs([row],'jev','jev-latest',prices())['unknown_requests'],1)
        self.assertEqual(percentiles([1,2,3,4,100]),dict(n=5,p50_ms=3,p95_ms=100))
        self.assertIsNone(percentiles([])['p50_ms'])
