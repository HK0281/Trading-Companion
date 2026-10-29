from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from trading_companion.news_triage import collision_check, definition_digest, input_state, load_corpus, rule_classify
from trading_companion.typesafe_jev import ClassifierError


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
