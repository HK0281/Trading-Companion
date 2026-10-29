"""Only JSON framing can be repaired; content and ambiguous objects cannot."""
import json
import unittest

from trading_companion.narrative_answer_parser import parse_narrative


class NarrativeParserTests(unittest.TestCase):
    def setUp(self):
        self.payload = dict(version='product-research-answer-v3',narratives=[dict(instrument_id='000001.SZ',summary={'text':'原句10.01元。'})],decisions=[])
        self.raw = json.dumps(self.payload,ensure_ascii=False)

    def test_exact_and_fenced_json_preserve_every_decoded_value(self):
        for prefix,suffix,recovered in (('','',False),('```json\n','\n```',True),('以下为分析：\n```json\n','\n```\n仅供参考。',True)):
            with self.subTest(prefix=prefix):
                result=parse_narrative(prefix+self.raw+suffix)
                self.assertEqual(result.payload,self.payload)
                self.assertEqual(result.recovered,recovered)
                self.assertEqual(result.raw_invalid,recovered)

    def test_complete_prefix_fields_and_array_items_survive_truncated_outer_json(self):
        raw='{"version":"product-research-answer-v3","narratives":'+json.dumps(self.payload['narratives'])+',"decisions":[{"instrument_id":'
        result=parse_narrative(raw)
        self.assertTrue(result.partial)
        self.assertEqual(result.payload['narratives'],self.payload['narratives'])
        self.assertNotIn('decisions',result.payload)
        raw='{"version":"product-research-answer-v3","narratives":['+json.dumps(self.payload['narratives'][0])+',{"summary":'
        self.assertEqual(parse_narrative(raw).payload['narratives'],self.payload['narratives'])

    def test_duplicate_nonfinite_conflicting_and_unsafe_wrappers_are_refused(self):
        for raw in (self.raw+self.raw, '保证收益。'+self.raw,
                    self.raw[:-1]+',"broken":?}'+self.raw,
                    self.raw[:-1]+',"decisions":[{},BROKEN,{"version":"other"}]',
                    '```json\n```json\n'+self.raw+'\n```\n```',
                    self.raw[:-1]+',"version":"product-research-answer-v3"}',
                    self.raw[:-1]+',"bad":NaN}',
                    '{"version":"unknown","narratives":[{}],"truncated":',
                    '{"version":"product-research-answer-v3","narratives":[{"text":',
                    'x'*26001):
            with self.subTest(raw=raw[:50]),self.assertRaises((ValueError,RecursionError)):
                parse_narrative(raw)
