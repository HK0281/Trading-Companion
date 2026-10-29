"""Application completion of literal identities, without treating predicates as stocks."""
from datetime import datetime,timezone
import unittest

from trading_companion.research_instruments import complete_mentions,research_spans
from trading_companion.security_directory import Security,SecurityDirectory
from trading_companion.product_plan import prepare_product_plan
from trading_companion.tool_model import FakeToolModel
from trading_companion.decision_answer_evaluation import load_corpus
from tests.test_product_plan import _arguments,_response,REFERENCE,NOW


class ResearchInstrumentTests(unittest.TestCase):
    def setUp(self):
        cases,_=load_corpus()
        identities={c['instrument_id']:Security(c['instrument_id'],c['security_name']) for c in cases}
        identities['000858.SZ']=Security('000858.SZ','五 粮 液')
        identities['300785.SZ']=Security('300785.SZ','值得买')
        self.directory=SecurityDirectory(tuple(identities.values()),'artificial-test',NOW,True)

    def plan(self,question,mentions=()):
        model=FakeToolModel([_response(_arguments(list(mentions),kinds=['technical']))])
        return prepare_product_plan(question,model=model,directory=self.directory,reference_date=REFERENCE,
            now=NOW,source='csv',development=True)

    def test_all_named_twenty_cases_resolve_even_with_model_omission(self):
        cases,_=load_corpus()
        for case in cases:
            if not case['requires_batch_b']:
                with self.subTest(id=case['id']):
                    plan=self.plan(case['question'])
                    self.assertEqual(plan.status,'ready')
                    self.assertEqual([s.instrument_id for s in plan.securities],[case['instrument_id']])

    def test_terminal_evaluation_is_grammar_but_actual_stock_name_remains(self):
        question='贵州茅台未来一个月值得买吗'
        self.assertEqual(complete_mentions(question,['贵州茅台','值得买'],self.directory),('贵州茅台',))
        for question in ('值得买现在能买吗','比较贵州茅台和值得买','贵州茅台比值得买','值得买这只股票怎么样','分析300785.SZ'):
            with self.subTest(question=question):
                mentions=complete_mentions(question,(),self.directory)
                self.assertTrue(any(self.directory.lookup(m).candidates[0].instrument_id=='300785.SZ' for m in mentions))

    def test_offsets_are_original_and_lookup_agrees_with_space_normalization(self):
        question='研究五粮液接下来一个月走势如何'
        spans=research_spans(question,self.directory)
        self.assertEqual([question[a:b] for a,b in spans],['五粮液'])
        self.assertEqual(self.plan(question,['五 粮 液']).securities[0].instrument_id,'000858.SZ')
        directory=SecurityDirectory((Security('000001.SZ','Café',('Ａ股',)),),'artificial-test',NOW,True)
        question='分析Cafe\u0301和Ａ 股'
        self.assertEqual([question[a:b] for a,b in directory.mentioned_spans(question)],['Cafe\u0301','Ａ 股'])
        self.assertEqual(directory.lookup('Cafe\u0301').status,'resolved')

    def test_invented_code_conflict_and_ambiguous_name_do_not_gain_authority(self):
        self.assertEqual(self.plan('贵州茅台现在能买吗',['000001.SZ']).code,'ungrounded_product_intent')
        self.assertEqual(self.plan('贵州茅台(000001.SZ)现在能买吗').code,'product_identity_conflict')
        self.directory=SecurityDirectory((Security('000001.SZ','甲股',('平安',)),Security('600000.SH','乙股',('平安',))),
            'artificial-test',NOW,True)
        self.assertNotEqual(self.plan('平安现在能买吗',['平安']).status,'ready')
