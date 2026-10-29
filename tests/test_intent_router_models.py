from copy import deepcopy
import json
import unittest

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.intent_router_models import JevIntent,DeepSeekIntent,fixture_post
from trading_companion.intent_router_task import load_corpus,input_state,questions,LABELS
from trading_companion.news_triage_models import _FakeResponse
from trading_companion.typesafe_jev import JevConfig,ClassifierError


class IntentModelTests(unittest.TestCase):
    def setUp(self):
        self.state=input_state(load_corpus()[0][0])

    def test_both_protocols_use_identical_label_definitions_and_minimal_input(self):
        calls=[]
        def transport(provider):
            def post(url,**kw):
                calls.append(kw['json'])
                return fixture_post(provider)(url,**kw)
            return post
        a=JevIntent(JevConfig('fake'),post=transport('jev')).classify(self.state)
        b=DeepSeekIntent(DeepSeekConfig('ds','fake'),post=transport('deepseek')).classify(self.state)
        self.assertIn(a['prediction'],LABELS)
        self.assertIsNone(b['confidence'])
        self.assertEqual(calls[0]['questions'],questions())
        self.assertIn(json.dumps(questions(),ensure_ascii=False),calls[1]['messages'][0]['content'])
        self.assertEqual(calls[0]['state'],self.state)
        self.assertEqual(json.loads(calls[1]['messages'][-1]['content']),self.state)
        self.assertEqual(calls[1]['thinking'],{'type':'disabled'})

    def test_deepseek_invalid_labels_extra_fields_and_missing_metadata_rejected(self):
        for content in ('{"intent":"command"}','{"intent":"clarify","execute":true}','{"intent":"clarify","intent":"screening"}'):
            def post(url,**kwargs):
                return _FakeResponse({'model':'ds','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}]})
            with self.subTest(content=content),self.assertRaises(ClassifierError):
                DeepSeekIntent(DeepSeekConfig('ds','fake'),post=post).classify(self.state)

    def test_unknown_usage_is_not_fabricated(self):
        def post(url,**kwargs):
            return _FakeResponse({'model':'served','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"intent":"clarify"}'}}]})
        result=DeepSeekIntent(DeepSeekConfig('ds','fake'),post=post).classify(self.state)
        self.assertIsNone(result['usage']['total_tokens'])
        self.assertEqual(result['model'],'served')

    def test_state_rejects_history_gold_or_extra_context(self):
        for mutate in (lambda s:s.update(gold='clarify'),lambda s:s['context'].update(history='private')):
            state=deepcopy(self.state); mutate(state)
            with self.assertRaises(ClassifierError):
                JevIntent(JevConfig('fake'),post=fixture_post('jev')).classify(state)
