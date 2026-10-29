"""Exercise the demo HTTP entry, not an independently authored frontend answer."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.generate_decision_demo_fixture import capture_demo_turn
from scripts.verify import clean_environment, offline_network


class DecisionDemoTests(unittest.TestCase):
    def test_complete_v3_demo_uses_checked_prices_with_no_deleted_sentences(self):
        with clean_environment(), offline_network(), patch("trading_companion.local_env._read_local_values",
                side_effect=AssertionError("Demo must not read credentials")):
            turn = capture_demo_turn()
        company = turn["company_research"]
        self.assertEqual(company["version"], "product-research-answer-v3")
        self.assertEqual(company["quality"]["state"], "complete")
        self.assertEqual(company["quality"]["deleted_sentences"], 0)
        self.assertEqual(company["quality"]["repair_attempts"], 0)
        self.assertEqual(company["quality"]["reasons"], [])
        narrative = company["narratives"][0]
        by_id = {e["id"]: e for e in company["evidence"]}
        self.assertIn("**", narrative["summary"]["text"])
        self.assertIn(company["decisions"][0]["horizon"], narrative["summary"]["text"])
        self.assertIn("人工示意", narrative["summary"]["text"])
        current = [by_id[i] for i in narrative["current"]["evidence_ids"]]
        self.assertEqual({e["label"] for e in current}, {"最新收盘价", "近20日涨跌幅"})
        for e in current:
            self.assertIn(e["display"], narrative["current"]["text"])
            self.assertIn(e["date"], narrative["current"]["text"])
        self.assertTrue(2 <= len(narrative["drivers"]) <= 4)
        self.assertTrue(all(d["title"] and d["text"] for d in narrative["drivers"]))
        self.assertGreaterEqual(len(narrative["levels"]), 3)
        self.assertEqual({r["lower_label"] for r in narrative["levels"]}, {"近20日最低价", "MA20", "近20日最高价"})
        for row in narrative["levels"]:
            self.assertTrue(row["evidence_ids"])
            self.assertIn(row["lower"], [f'{float(by_id[i]["raw_value"]):.2f}' for i in row["evidence_ids"]])
        self.assertEqual(set(narrative["scenarios"]), {"base", "better", "worse"})
        self.assertTrue(all(c["text"] for c in narrative["scenarios"].values()))
        self.assertIn("倾向买入", narrative["stance"]["text"])
        self.assertEqual(company["decisions"][0]["rating"], "买入")
        self.assertTrue(all(p["data_authenticity"] == "synthetic" for p in company["trade_plans"]))
        # A broad buy/hold question genuinely requested these domains. The demo has no substitutes.
        self.assertEqual((turn["status"], turn["code"]), ("limited", "company_research_partial"))
        sections = company["coverage"][0]["sections"]
        self.assertEqual({s["kind"] for s in sections}, {"profile", "financial", "news", "announcements", "technical"})
        self.assertEqual({s["kind"] for s in sections if s["status"] != "available"}, {"profile", "financial", "news", "announcements"})
        fixture = json.loads((Path(__file__).resolve().parents[1]/"frontend/tests/decisionFixture.json").read_text(encoding="utf-8"))
        # Random request IDs and retrieval times differ; compare every rendered narrative text/price and quality.
        def rendered(value):
            if isinstance(value, dict):
                return {k: rendered(v) for k, v in value.items() if k != "evidence_ids"}
            return [rendered(v) for v in value] if isinstance(value, list) else value
        self.assertEqual(rendered(company["narratives"]), rendered(fixture["narratives"]))
        self.assertEqual(company["quality"], fixture["quality"])
