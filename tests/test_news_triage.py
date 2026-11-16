"""News triage corpus, model protocol and metric checks; offline only."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock
import unittest

from tests.test_typesafe_jev import http_response
from trading_companion.deepseek_model import DeepSeekConfig, DEEPSEEK_CHAT_URL
from trading_companion.news_triage import (
    collision_check,
    definition_digest,
    input_state,
    load_corpus,
    rule_classify,
    questions,
)
from trading_companion.news_triage_metrics import calibration, cascade, costs, percentiles, scores, selective
from trading_companion.news_triage_models import DeepSeekTriage, JevTriage, fixture_post
from trading_companion.typesafe_jev import ClassifierError, JevConfig


# ---- test_news_triage

class CorpusTests(unittest.TestCase):
    def test_independent_annotations_and_all_quotas(self):
        items, manifest = load_corpus()
        self.assertIn(manifest["annotation_status"],("draft","reviewed"))
        self.assertEqual(manifest["reviewer"],None if manifest["annotation_status"]=="draft" else "claude")
        self.assertEqual(manifest["definition_sha256"],definition_digest())
        self.assertEqual(len(items),72)
        self.assertEqual(manifest["label_counts"]["relevance"],dict(primary=44,mentioned=16,unrelated=12))
        self.assertEqual(manifest["label_counts"]["tone"],dict(positive=23,neutral=29,negative=20))
        self.assertEqual(manifest["label_counts"]["event_type"],dict(earnings=10,capital=12,governance=8,regulatory=10,operations=12,market=8,industry=6,other=6))
        for tag, minimum in dict(mismatch=12,negation=8,multi_event=10,industry_mention=8,clickbait=6,routine=10,mixed=8).items():
            self.assertGreaterEqual(manifest["difficulty_counts"][tag],minimum)
        self.assertEqual(len({r["company_name"] for r in items}),12)
        self.assertEqual(manifest["collision_checks"]["fixture"]["status"],"checked")
        self.assertTrue(all(r["gold"]["tone"]=="neutral" for r in items if r["gold"]["relevance"]=="unrelated"))
        self.assertEqual(set(input_state(items[0])), {"title","company_name","company_code"})

    def test_collision_against_names_aliases_and_codes(self):
        items, _ = load_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'directory.json'
            for row in ({"name":items[0]["company_name"],"aliases":[],"instrument_id":"000001.SZ"},
                        {"name":"other","aliases":[items[0]["company_name"]],"instrument_id":"000001.SZ"},
                        {"name":"other","aliases":[],"instrument_id":items[0]["company_code"]+".SH"}):
                path.write_text(json.dumps({"securities":[row]}),encoding='utf-8')
                with self.assertRaises(ClassifierError):
                    collision_check(items,path)

    def test_rule_baseline_is_gold_free_and_keeps_negation_neutral(self):
        state={"title":"曜棱科技否认重组传闻","company_name":"曜棱科技","company_code":"990901"}
        result=rule_classify(state)
        self.assertEqual(result['event_type']['choice'],'capital')
        self.assertEqual(result['tone']['choice'],'neutral')
        self.assertIsNone(result['tone']['confidence'])


# ---- test_news_triage_models

class TriageModelTests(unittest.TestCase):
    def test_both_adapters_consume_exact_same_state_and_criteria(self):
        items, _ = load_corpus()
        state = input_state(items[0])
        for provider, cls, config in (("jev", JevTriage, JevConfig("test")),
                                     ("deepseek", DeepSeekTriage, DeepSeekConfig("test-model", "test"))):
            post = Mock(side_effect=fixture_post(provider))
            result = cls(config, post=post).classify(state)
            body = post.call_args.kwargs["json"]
            self.assertEqual(set(result["answers"]), set(questions()))
            if provider == "deepseek":
                self.assertEqual(json.loads(body["messages"][-1]["content"]), state)
                self.assertEqual(body["temperature"], 0)
                self.assertEqual(body["response_format"], {"type":"json_object"})
                self.assertIn(json.dumps(questions(), ensure_ascii=False), body["messages"][0]["content"])
                self.assertTrue(all(r["confidence"] is None for r in result["answers"].values()))
                self.assertEqual(post.call_args.args[0], DEEPSEEK_CHAT_URL)
            else:
                self.assertEqual(body["state"], state)
                self.assertEqual(body["questions"], questions())

    def test_deepseek_rejects_malformed_labels_and_does_not_invent_confidence(self):
        good = {"relevance":"primary","event_type":"capital","tone":"neutral"}
        for content in ("```json\n{}\n```", json.dumps({**good,"confidence":.9}), json.dumps({**good,"tone":"buy"}),
                        '{"relevance":"primary","relevance":"mentioned","event_type":"capital","tone":"neutral"}'):
            payload = {"model":"served-model", "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":content}}],
                       "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}
            post = Mock(return_value=http_response(payload))
            with self.subTest(content=content), self.assertRaises(ClassifierError) as context:
                DeepSeekTriage(DeepSeekConfig("requested-model","test"),post=post).classify({})
            self.assertEqual(context.exception.metadata["usage"]["total_tokens"],15)
            self.assertEqual(context.exception.metadata["model"],"served-model")
            post.assert_called_once()

    def test_fixture_does_not_depend_on_gold(self):
        items, _ = load_corpus()
        for provider, model in (("deepseek", DeepSeekTriage(DeepSeekConfig("test-model","test"),post=fixture_post("deepseek"))),
                                ("jev", JevTriage(JevConfig("test"),post=fixture_post("jev")))):
            with self.subTest(provider=provider):
                item = deepcopy(items[0])
                before = model.classify(input_state(item))
                item["gold"] = {"relevance":"unrelated","event_type":"other","tone":"negative"}
                item["reason"] = "changed annotation must not enter fixture input"
                after = model.classify(input_state(item))
                self.assertEqual(before,after)


# ---- test_news_triage_metrics

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
    def test_shared_helpers_preserve_original_news_outputs(self):
        from trading_companion import classification_metrics as shared
        from trading_companion import news_triage_metrics as news
        self.assertIs(news.ratio,shared.ratio)
        self.assertIs(news.percentiles,shared.percentiles)
        self.assertEqual(news.ratio(3,4),{'numerator':3,'denominator':4,'value':.75})
        self.assertEqual(news.percentiles([40,10,30,20]),{'n':4,'p50_ms':20,'p95_ms':40})
        self.assertEqual(news.percentiles([]),{'n':0,'p50_ms':None,'p95_ms':None})

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


if __name__ == '__main__':
    unittest.main()
