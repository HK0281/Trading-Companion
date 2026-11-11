"""Fake-only paid-driver checks, durable partial results and blind-map integrity."""

from copy import deepcopy
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.evaluate_news_triage_sorting import main
from trading_companion.news_triage_sorting_annotations import (
    seal_annotations, write_blind, summarize_blind,
)
from trading_companion.news_triage_sorting_artifacts import read_json, write_json
from trading_companion.news_triage_sorting_live import CLAIM, preflight, run_live
from trading_companion.news_triage_sorting_replay import PairBudget, VerifiedCostGuard, VerifiedPrice
from tests.test_news_triage_sorting import settings, Transport
from tests.test_news_triage_sorting_replay import Answer
from tests.test_news_triage_sorting_freeze import manifest_files

PRICES=Path(__file__).resolve().parents[1]/"data/evaluation/model-prices.json"


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manifest,self.digest,self.codex,self.claude=manifest_files(self.root)
        seal_annotations(self.manifest,self.codex,self.claude)
        self.output=self.root/"run"
        for guard in (patch("socket.socket.connect",side_effect=AssertionError("no real connections")),
                      patch("trading_companion.local_env._read_local_values",side_effect=AssertionError("no real keys"))):
            guard.start();self.addCleanup(guard.stop)

    def live(self, **kwargs):
        return run_live(self.manifest,self.digest,PRICES,self.codex,self.claude,
                        kwargs.pop("max_requests",570),self.output,configure=kwargs.pop("configure",settings),
                        answer_factory=kwargs.pop("answer_factory",lambda config:Answer()),sorter_post=Transport(),**kwargs)

    def test_preview_missing_args_invalid_hash_annotations_or_price_never_load_keys_or_claim(self):
        with patch("sys.stdout",new=StringIO()):
            self.assertEqual(main([]),0)
            self.assertEqual(main(["--manifest",str(self.manifest)]),0)
            self.assertEqual(main(["--live","--manifest",str(self.manifest)]),2)
        configure=Mock(side_effect=AssertionError("must not load keys"))
        for cap in (0,571,True):
            with self.assertRaises(ValueError):self.live(max_requests=cap,configure=configure)
        with self.assertRaises(ValueError):
            run_live(self.manifest,"0"*64,PRICES,self.codex,self.claude,570,self.output,configure=configure)
        original=self.codex.read_bytes()
        data,_=read_json(self.codex);data["cases"][0]["completed"]=False
        self.codex.write_text(json.dumps(data),encoding="utf-8")
        with self.assertRaises(ValueError):self.live(configure=configure)
        self.codex.write_bytes(original)
        bad=self.root/"prices.json";data,_=read_json(PRICES);data["entries"][0]["input_per_million"]=None
        write_json(bad,data)
        with self.assertRaises(ValueError):preflight(self.manifest,self.digest,bad,self.codex,self.claude,570,self.output)
        configure.assert_not_called()
        self.assertFalse((self.root/CLAIM).exists())
        self.assertFalse(self.output.exists())

    def test_fake_sixty_rounds_all_prechecks_no_source_calls_and_claim_blocks_new_output(self):
        report=self.live()
        self.assertEqual(report["status"],"awaiting_blind_review",report["stop_reason"])
        self.assertEqual(len(list(self.output.glob("turn-*.json"))),60)
        self.assertEqual(len(report["rows"]),30)
        self.assertEqual(report["run"]["source_requests"],0)
        self.assertEqual(report["run"]["intent_requests"],0)
        self.assertEqual(report["run"]["jev_cost_status"],"estimated_unverified")
        self.assertTrue((self.output/"blind/blind-table.html").exists())
        events=[read_json(p)[0] for p in (self.output/"journal").glob("*.json")]
        self.assertEqual(sum(e["kind"]=="attempt" for e in events),sum(report["attempts"].values()))
        self.output=self.root/"different-output"
        configure=Mock(side_effect=AssertionError("no reload"))
        with self.assertRaises(ValueError):self.live(configure=configure)
        configure.assert_not_called()

    def test_global_cap_and_failure_attempts_retained_before_following_arm(self):
        report=self.live(max_requests=2)
        self.assertEqual(report["status"],"partial")
        self.assertLessEqual(sum(report["attempts"].values()),2)
        self.assertTrue((self.output/"report.json").exists())
        self.assertFalse((self.output/"blind").exists())
        self.assertLessEqual(len(list(self.output.glob("turn-*.json"))),2)
        budget=PairBudget(3)
        for _ in range(3):budget.attempt("jev")
        with self.assertRaises(Exception):budget.attempt("deepseek")
        self.assertEqual(budget.counts,dict(jev=3,deepseek=0))
        broken=PairBudget(attempt_sink=Mock(side_effect=OSError("disk full")))
        with self.assertRaises(OSError):broken.attempt("deepseek")
        with self.assertRaises(Exception):broken.attempt("jev")
        self.assertEqual(broken.counts,dict(jev=0,deepseek=1))

    def test_exception_after_completed_turn_keeps_partial_report_and_never_resumes(self):
        count=0
        def answer(config):
            nonlocal count
            count+=1
            if count==3:raise RuntimeError("synthetic factory failure")
            return Answer()
        report=self.live(answer_factory=answer)
        self.assertEqual(report["status"],"partial")
        self.assertEqual(report["stop_reason"],"run_interrupted")
        saved,_=read_json(self.output/"report.json")
        self.assertEqual(len(saved["rows"]),1)
        self.assertEqual(len(saved["rows"][0]["arms"]),1)
        self.assertTrue((self.root/CLAIM).exists())

    def test_jev_alias_is_estimated_but_deepseek_identity_or_usage_stops(self):
        prices=dict(jev=VerifiedPrice("jev-latest","jev-latest",Decimal("0.042"),Decimal(0)),
                    deepseek=VerifiedPrice("deepseek-flash","deepseek-flash",Decimal(2),Decimal(8)))
        guard=VerifiedCostGuard(prices,jev_alias_estimate=True)
        call=dict(provider="jev",requested_model="jev-latest",model="actual-later-version",
                  usage=dict(prompt_tokens=3100,completion_tokens=0,total_tokens=3100))
        result=guard.reconcile(dict(answer_calls=[],triage=dict(calls=[call])),PairBudget())
        self.assertTrue(result["known"])
        self.assertIsNone(result["observed_USD"])
        self.assertEqual(result["jev_estimated_USD"],"0.0001302")
        self.assertEqual(result["jev_alias_mismatches"],1)
        for change in (dict(usage=None),dict(provider="deepseek",requested_model="deepseek-flash")):
            budget=PairBudget()
            self.assertFalse(guard.reconcile(dict(answer_calls=[],triage=dict(calls=[{**call,**change}])),budget)["known"])
            self.assertEqual(budget.stop_reason,"price_or_usage_unknown")

    def test_real_product_point_preserves_frozen_technical_and_decision_bundles_on_both_sides(self):
        from dataclasses import replace
        from tests.test_news_triage_sorting_freeze import frozen, CASES
        from trading_companion.news_triage_sorting import NewsTriageSorter
        from trading_companion.news_triage_sorting_replay import run_pairs
        specs=[CASES[12],CASES[28]]+[CASES[8]]*28
        cases=[(replace(frozen(spec),id="mixed"+str(i)),"on" if i<15 else "off") for i,spec in enumerate(specs)]
        guard=VerifiedCostGuard(dict(
            jev=VerifiedPrice("jev-latest","jev-latest",Decimal("0.042"),Decimal(0)),
            deepseek=VerifiedPrice("deepseek-flash","deepseek-flash",Decimal(2),Decimal(8))),jev_alias_estimate=True)
        report=run_pairs(cases,model_factory=Answer,cost_guard=guard,
            sorter_factory=lambda sink,attempt:NewsTriageSorter(configure=settings,post=Transport(),sink=sink,attempt_guard=attempt))
        self.assertEqual(report["status"],"awaiting_blind_review",report["stop_reason"])
        for row in report["rows"]:
            self.assertEqual(row["arms"]["on"]["bundle"],row["arms"]["off"]["bundle"])


class BlindTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        # Actual checked product point with fake models and sources, rather than hand-built arms.
        from tests.test_news_triage_sorting_replay import SortingReplayTests
        fixture=SortingReplayTests()
        self.report=fixture.run_case()
        self.path=self.root/"report.json";write_json(self.path,self.report)
        self.blind=self.root/"blind"
        write_blind(self.path,self.blind,chooser=lambda n:1)
        self.reviews,_=read_json(self.blind/"blind-review.template.json")
        for row in self.reviews["rows"]:
            for side in ("A","B"):row[side]["completed"]=True
        self.review_path=self.root/"reviews.json"

    def summarize(self):
        self.review_path.write_text(json.dumps(self.reviews,ensure_ascii=False),encoding="utf-8")
        return summarize_blind(self.path,self.blind/"private-mapping.json",self.blind/"blind-table.json",self.review_path)

    def test_blind_whitelist_independent_A_B_and_preserved_baseline_omissions(self):
        table,_=read_json(self.blind/"blind-table.json")
        for row in table["rows"]:
            self.assertEqual(set(row),{"id","question","important_items","evidence","arms"})
            self.assertEqual(set(row["arms"]),{"A","B"})
            for arm in row["arms"].values():
                self.assertEqual(set(arm),{"answer","selected_E_ids"})
                self.assertLessEqual(set(arm["answer"]),{"text","narratives","decisions"})
            self.assertEqual([e["E"] for e in row["evidence"]],["E"+str(i) for i in range(1,len(row["evidence"])+1)])
        result=self.summarize()
        self.assertTrue(result["summary"]["passed"],result)
        self.reviews["rows"][0]["B"].update(omissions=[0],comments="原回答遗漏该重要事项")
        result=self.summarize()
        self.assertEqual(result["reviews"]["case0"]["baseline_omissions"],["重要人工事件"])
        self.assertEqual(result["reviews"]["case0"]["new_omissions"],0)
        self.reviews["rows"][0]["A"].update(citation_errors=1,comments="人工发现引用支持不足")
        self.assertFalse(self.summarize()["summary"]["passed"])

    def test_missing_review_mapping_swap_and_public_answer_tamper_are_rejected(self):
        self.reviews["rows"][0]["A"]["completed"]=False
        with self.assertRaises(ValueError):self.summarize()
        self.reviews["rows"][0]["A"]["completed"]=True
        mapping_path=self.blind/"private-mapping.json"
        mapping,_=read_json(mapping_path)
        mapping["mappings"][0].update(A="off",B="on")
        mapping_path.write_text(json.dumps(mapping),encoding="utf-8")
        with self.assertRaises(ValueError):self.summarize()
        # Even updating the review's map hash cannot make a swapped public answer match.
        _,self.reviews["mapping_sha256"]=read_json(mapping_path)
        with self.assertRaisesRegex(ValueError,"public answers"):self.summarize()

    def test_partial_report_cannot_be_presented_as_a_complete_blind_batch(self):
        report=deepcopy(self.report);report["status"]="partial"
        path=self.root/"partial.json";write_json(path,report)
        with self.assertRaises(ValueError):write_blind(path,self.root/"partial-blind")


if __name__=="__main__":unittest.main()
