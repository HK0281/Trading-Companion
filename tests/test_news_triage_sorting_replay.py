"""Frozen paired runs use the actual checked executor and synthetic model/transport."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.model import TokenUsage
from trading_companion.news_triage_sorting import NewsTriageSorter
from trading_companion.news_triage_sorting_replay import (
    ReplayCase, VERSION, freeze_case, load_manifest, run_pairs, paired_summary, PairBudget, decode,
    VerifiedCostGuard, VerifiedPrice,
)
from trading_companion.product_research import collect_product_research
from trading_companion.research_intent_v2 import check_response, IntentContext
from trading_companion.research_session import ResearchSession
from tests.test_research_intent_v2 import draft,response
from tests.test_news_triage_sorting import expanded_data, settings, Transport
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import plan, directory, NOW


def example_case():
    data=expanded_data(4)
    collect_product_research(plan(kinds=("news","announcements")),data_service=data,now=NOW)
    batches=tuple(data.collect(req,kinds) for req,kinds in list(data.calls))
    checked=check_response("分析平安银行的新闻公告",response(draft(research_kinds=[
        dict(kind="news",phrase="新闻"),dict(kind="announcements",phrase="公告")])),
        context=IntentContext(ResearchSession(),directory(),NOW,NOW.date()))
    return ReplayCase("case0","分析平安银行的新闻公告",checked,NOW,directory(),batches,
                      important_items=(("重要人工事件",("E4",)),))


class Answer(ProductModelFixture):
    def complete(self,messages,*,tools):
        response=super().complete(messages,tools=tools)
        return replace(response,provider="deepseek",requested_model="deepseek-flash",model="deepseek-flash",
                       is_mock=False,usage=TokenUsage(10,5,15))


class SortingReplayTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch("socket.getaddrinfo",side_effect=AssertionError("No DNS")),
                      patch("socket.socket.connect",side_effect=AssertionError("No HTTP")),
                      patch("trading_companion.local_env._read_local_values",side_effect=AssertionError("No .env"))):
            guard.start();self.addCleanup(guard.stop)

    def cases(self):
        case=example_case()
        return [(replace(case,id=f"case{i}"),"on" if i<15 else "off") for i in range(30)]

    def test_manifest_codec_hashes_balance_and_preview_have_no_side_effects(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);rows=[]
            for case,first in self.cases():
                path=root/(case.id+".json")
                rows.append(dict(file=path.name,sha256=freeze_case(case,path),first=first))
            manifest=root/"manifest.json"
            manifest.write_text(json.dumps(dict(version=VERSION,cases=rows)),encoding="utf-8")
            cases,plan=load_manifest(manifest)
            self.assertEqual(cases,self.cases())
            self.assertEqual(plan["max_calls"],dict(jev=300,deepseek=270,total=570))
            self.assertEqual(plan["mode"],"plan_only")
            self.assertEqual(len(list(root.iterdir())),31)
            (root/"case0.json").write_text("tampered",encoding="utf-8")
            with self.assertRaises(ValueError):load_manifest(manifest)
        with self.assertRaises(ValueError):decode(dict(tag="os.system",value={}))

    def run_case(self,after_turn=lambda arm,budget:None,model_factory=Answer):
        def factory(sink,guard):
            return NewsTriageSorter(configure=settings,post=Transport(),sink=sink,attempt_guard=guard)
        guard=VerifiedCostGuard(dict(
            jev=VerifiedPrice("jev-latest","jev-test-version",Decimal("0.042"),Decimal(0)),
            deepseek=VerifiedPrice("deepseek-flash","deepseek-flash",Decimal(2),Decimal(8))))
        return run_pairs(self.cases(),model_factory=model_factory,sorter_factory=factory,cost_guard=guard,after_turn=after_turn)

    def test_sixty_turns_through_product_point_no_intent_or_source_network(self):
        report=self.run_case()
        self.assertEqual(report["status"],"awaiting_blind_review",report["stop_reason"])
        self.assertEqual(len(report["rows"]),30)
        self.assertEqual(report["attempts"]["jev"],240)
        self.assertLessEqual(report["attempts"]["deepseek"],150)
        for row in report["rows"]:
            self.assertEqual(set(row["arms"]),{"on","off"})
            self.assertEqual(row["arms"]["on"]["bundle"],row["arms"]["off"]["bundle"])
        self.assertFalse(paired_summary(report,{})["passed"])
        reviews={r["id"]:dict(baseline_omissions=[],new_omissions=0,cross_company_errors=0,citation_errors=0,important_promoted=1)
                 for r in report["rows"]}
        summary=paired_summary(report,reviews)
        self.assertTrue(summary["passed"],summary)
        self.assertEqual(summary["changed_pairs"],30)
        self.assertTrue(all(r["jev"]==24800 for r in summary["token_deltas"]))
        reviews["case0"]["citation_errors"]=1
        self.assertFalse(paired_summary(report,reviews)["passed"])

    def test_cost_guard_and_actual_attempt_cap_stop_without_repeating_turns(self):
        def guard(arm,budget):budget.stop_reason="price_or_usage_unknown"
        report=self.run_case(guard)
        self.assertEqual(report["status"],"partial")
        self.assertEqual(len(report["rows"]),1)
        self.assertEqual(len(report["rows"][0]["arms"]),1)
        budget=PairBudget()
        for _ in range(270):budget.attempt("deepseek")
        with self.assertRaises(Exception):budget.attempt("deepseek")
        self.assertEqual(budget.counts["deepseek"],270)

    def test_unknown_usage_identity_and_money_preflight_stop(self):
        prices=dict(jev=VerifiedPrice("alias","resolved-version",Decimal("0.042"),Decimal(0)),
                    deepseek=VerifiedPrice("ds","ds",Decimal(2),Decimal(8)))
        guard=VerifiedCostGuard(prices);budget=PairBudget()
        call=dict(provider="jev",requested_model="alias",model="resolved-version",
                  usage=dict(prompt_tokens=3100,completion_tokens=0,total_tokens=3100))
        self.assertTrue(guard.reconcile(dict(answer_calls=[],triage=dict(calls=[call])),budget)["known"])
        for change in (dict(usage=None),dict(model="different-version")):
            budget=PairBudget()
            result=guard.reconcile(dict(answer_calls=[],triage=dict(calls=[{**call,**change}])),budget)
            self.assertFalse(result["known"])
            self.assertEqual(budget.stop_reason,"price_or_usage_unknown")
        budget=PairBudget();guard.spent["deepseek"]=Decimal("14.9")
        guard.preflight(False,budget)
        self.assertEqual(budget.stop_reason,"pair_money_budget")

    def test_existing_answer_transport_retry_is_counted_before_stopping_next_arm(self):
        from trading_companion.model import ModelCallError
        class RetryOnce(Answer):
            failed=False
            def complete(self,messages,*,tools):
                if not self.failed:
                    self.failed=True
                    raise ModelCallError("timeout","synthetic timeout")
                return super().complete(messages,tools=tools)
        report=self.run_case(model_factory=RetryOnce)
        self.assertEqual(report["stop_reason"],"answer_usage_unknown")
        self.assertEqual(len(report["rows"]),1)
        arms=report["rows"][0]["arms"]
        self.assertEqual(set(arms),{"on"})
        calls=arms["on"]["answer_calls"]
        self.assertEqual(len(calls),2)
        self.assertIsNone(calls[0]["usage"])
        self.assertEqual(calls[1]["usage"]["total_tokens"],15)
        self.assertEqual(report["attempts"]["deepseek"],3)


if __name__=="__main__":unittest.main()
