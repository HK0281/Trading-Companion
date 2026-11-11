"""Real adapters, artificial transport: frozen protocol and safe contextual ordering."""

from copy import deepcopy
from dataclasses import replace
import json
from threading import Lock
import unittest
from unittest.mock import Mock, patch

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.model import TokenUsage
from trading_companion.news_triage_sorting import (
    NewsTriageSorter, SortOrder, apply_context_order, bind_sort_order, compatible_context,
    estimated_tokens, request_body,
)
from trading_companion.news_triage_sorting_control import SortingRound
from trading_companion.news_triage_product import questions
from trading_companion.news_triage_product_models import ProductJev
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_narrative_answer import prepare_answer_context
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.research_session import ResearchSession
from trading_companion.turn_control import bind_turn, company_turn_control, TurnStopped
from trading_companion.typesafe_jev import JevConfig, JEV_URL, ClassifierError
from tests.test_news_triage_shadow import public_data
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import NOW, directory, plan, SECURITIES, MemorySourceFactory


def settings():
    return JevConfig("artificial-key"), DeepSeekConfig("deepseek-flash", "artificial-key")


class Response:
    status_code = 200
    def __init__(self, payload):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.closed = False
    def iter_content(self, **kwargs):
        yield self.raw
    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *, fail=None, ds_items=None, jev_tokens=3100, jev_related="primary", ds_tone="neutral"):
        self.calls, self.lock = [], Lock()
        self.fail, self.ds_items, self.jev_tokens = fail, ds_items, jev_tokens
        self.jev_related, self.ds_tone = jev_related, ds_tone
    def __call__(self, url, **kwargs):
        body = kwargs["json"]
        provider = "jev" if url == JEV_URL else "deepseek"
        with self.lock:
            self.calls.append((provider, body))
        if self.fail == provider:
            raise ClassifierError("connection_error")
        if provider == "jev":
            values = dict(relevance=self.jev_related, tone="neutral",
                          importance="high" if "重要" in body["state"]["title"] else "ordinary")
            payload = dict(model="jev-test-version", usage=dict(input_tokens=self.jev_tokens, output_tokens=0),
                answers={q: dict(type="choice", choice=value, confidence=1.,
                    probabilities={v: float(v==value) for v in body["questions"][q]["criteria"]}) for q,value in values.items()})
        else:
            rows = json.loads(body["messages"][1]["content"])["items"]
            items = [dict(id=r["id"], relevance="primary", tone=self.ds_tone,
                          importance="high" if "重要" in r["title"] else "ordinary") for r in rows]
            if self.ds_items is not None:
                items = self.ds_items(items)
            payload = dict(model="deepseek-flash", usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                choices=[dict(message=dict(role="assistant", content=json.dumps(dict(items=items))), finish_reason="stop")])
        return Response(payload)


def expanded_data(count=5):
    data = public_data()
    prior = data.transform
    def transform(batch):
        batch = prior(batch)
        return replace(batch, source_results=tuple(replace(section, items=tuple(
            replace(item, id=item.id+":"+str(i), value=("重要" if i==3 else "普通")+item.value)
            for item in section.items for i in range(count))) if section.kind in ("news", "announcements") else section
            for section in batch.source_results))
    data.transform = transform
    return data


class SortingTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")),
                      patch("socket.socket.connect", side_effect=AssertionError("No network")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))):
            guard.start()
            self.addCleanup(guard.stop)

    def bundle(self, *, count=5, securities=SECURITIES[:1], kinds=("news", "announcements")):
        return collect_product_research(plan(securities=securities, kinds=kinds), data_service=expanded_data(count),
                                        source_factory=MemorySourceFactory(), now=NOW)

    def sort(self, bundle, transport=None, control=None, configure=settings):
        control = control or company_turn_control()
        if control.to_dict()["state"] == "not_started":
            control.start()
        transport = transport or Transport()
        self.summaries = []
        sorter = NewsTriageSorter(configure=configure, post=transport, sink=self.summaries.append)
        with bind_turn(control):
            order = sorter.sort(bundle, directory=directory(), now=NOW, question="分析公司新闻")
        return order, control, transport

    def test_ten_titles_one_batch_and_jev_tokens_are_independent(self):
        bundle = self.bundle()
        order, control, transport = self.sort(bundle)
        self.assertIsNotNone(order, self.summaries[-1])
        calls = self.summaries[-1]["calls"]
        self.assertEqual(len([c for c in calls if c["provider"]=="jev"]), 10)
        self.assertEqual(sum(c["usage"]["total_tokens"] for c in calls if c["provider"]=="jev"), 31000)
        self.assertEqual(control.to_dict()["observed_tokens"], 15)
        self.assertEqual(control.to_dict()["model_calls_attempted"], 1)
        ds = [b for p,b in transport.calls if p=="deepseek"]
        self.assertEqual(len(ds), 1)
        self.assertEqual(len(json.loads(ds[0]["messages"][1]["content"])["items"]), 10)
        self.assertEqual(len(order.promoted), 2)
        for p,b in transport.calls:
            if p == "jev":
                self.assertEqual(b["questions"], questions())

    def test_multicompany_first_company_only_preserves_all_other_slots_and_ids(self):
        bundle = self.bundle(securities=SECURITIES[:3], kinds=("news", "announcements", "financial"))
        before = deepcopy(bundle.to_dict())
        order, _, transport = self.sort(bundle)
        self.assertIsNotNone(order, self.summaries[-1])
        self.assertEqual(order.company, SECURITIES[0].instrument_id)
        base = prepare_answer_context("分析公司新闻", bundle)
        after = prepare_answer_context("分析公司新闻", bundle, headline_order=order)
        self.assertTrue(compatible_context(base, after, order))
        self.assertEqual(before, bundle.to_dict())
        for a,b in zip(base["evidence"],after["evidence"]):
            if a["instrument_id"] != order.company or a["kind"] not in ("news","announcements"):
                self.assertEqual(a,b)
        for kind in ("news","announcements"):
            items = [r for r in after["evidence"] if r["instrument_id"]==order.company and r["kind"]==kind]
            flags = [r["id"] in order.promoted for r in items]
            self.assertEqual(flags, sorted(flags, reverse=True))
        self.assertEqual({b["state"]["instrument_id"] for p,b in transport.calls if p=="jev"}, {order.company})

    def test_either_provider_or_item_failure_discards_entire_order(self):
        for transport in (Transport(fail="jev"), Transport(fail="deepseek"),
                          Transport(ds_items=lambda r:r[:-1]), Transport(ds_items=lambda r:list(reversed(r))),
                          Transport(ds_items=lambda r:[{**r[0],"tone":"unresolved"},*r[1:]])):
            with self.subTest(transport=transport):
                order, control, _ = self.sort(self.bundle(count=2), transport)
                self.assertIsNone(order)
                self.assertIsNone(control.to_dict()["stop_code"])
        self.assertNotIn("deepseek",[p for p,_ in Transport(fail="jev").calls])

    def test_valid_tone_disagreement_does_not_block_high_or_create_labels(self):
        order, _, _ = self.sort(self.bundle(count=4), Transport(ds_tone="positive"))
        self.assertIsNotNone(order)
        self.assertEqual(len(order.promoted), 2)
        unrelated, _, _ = self.sort(self.bundle(count=4), Transport(jev_related="unrelated"))
        self.assertIsNotNone(unrelated)
        self.assertFalse(unrelated.promoted)

    def test_missing_config_no_calls_and_optional_budget_no_turn_stop(self):
        post=Transport()
        order, _, _ = self.sort(self.bundle(count=1), post, configure=Mock(side_effect=ValueError("secret-marker")))
        self.assertIsNone(order)
        self.assertEqual(post.calls, [])
        for reserve in (4, 5, 6):
            control = company_turn_control();control.start()
            for _ in range(reserve):control.reserve_model()
            order, _, post = self.sort(self.bundle(count=1), control=control)
            self.assertIsNone(order)
            self.assertEqual(post.calls, [])
            self.assertIsNone(control.to_dict()["stop_code"])

    def test_jev_unknown_or_over_budget_restores_original_without_deepseek(self):
        for usage in (None, 21000):
            order, control, transport = self.sort(self.bundle(count=1), Transport(jev_tokens=usage))
            self.assertIsNone(order)
            self.assertFalse(any(p=="deepseek" for p,_ in transport.calls))
            self.assertEqual(control.to_dict()["observed_tokens"], 0)
            self.assertIsNone(control.to_dict()["stop_code"])

    def test_cancel_propagates(self):
        control=company_turn_control();control.start();control.cancel()
        with self.assertRaises(TurnStopped):
            self.sort(self.bundle(count=1),control=control)

    def test_long_candidate_cannot_take_another_domain_slot(self):
        control=company_turn_control();control.start()
        round=SortingRound(control)
        order=SortOrder("000001.SZ",frozenset(("E3",)),round)
        records=[dict(id="E1",instrument_id="000001.SZ",kind="news",display="x"),
                 dict(id="E2",instrument_id="600000.SH",kind="financial",display="protected"),
                 dict(id="E3",instrument_id="000001.SZ",kind="news",display="x"*500)]
        with bind_sort_order(order):
            result=apply_context_order(records[:2],records)
        self.assertEqual(result,records[:2])
        self.assertEqual(round.capacity_fallbacks,1)
        self.assertFalse(compatible_context(dict(evidence=records[:2]),dict(evidence=[records[2]]),order))

    def test_product_answer_and_repair_never_reclassify_and_failures_answer_normally(self):
        for fail in (None,"jev","deepseek"):
            data=expanded_data(4);post=Transport(fail=fail);model=ProductModelFixture()
            sorter=NewsTriageSorter(configure=settings,post=post)
            result=run_product_research_turn("分析平安银行的新闻公告",prepared=plan(kinds=("news","announcements")),
                session=ResearchSession(),model=model,directory=directory(),now=NOW,source="tencent",
                data_service=data,headline_sorter=sorter)
            self.assertIn(result.status,("completed","limited"))
            self.assertTrue(result.product_bundle.evidence)
            self.assertLessEqual(sum(p=="deepseek" for p,_ in post.calls),1)
            if len(model.contexts)>1:
                self.assertEqual(model.contexts[0],model.contexts[-1])

    def test_actual_repair_uses_same_sorted_context_without_another_classification(self):
        from tests.test_product_narrative_answer import NarrativeModel
        def invalid_first(payload, attempt, context):
            if attempt == 1:
                del payload["narratives"][0]["current"]
        model=NarrativeModel(invalid_first);post=Transport()
        result=run_product_research_turn("分析平安银行的新闻公告",prepared=plan(kinds=("news","announcements")),
            session=ResearchSession(),model=model,directory=directory(),now=NOW,source="tencent",
            data_service=expanded_data(4),headline_sorter=NewsTriageSorter(configure=settings,post=post))
        self.assertEqual(len(model.calls),2)
        self.assertEqual(model.calls[0][2],model.calls[1][2])
        self.assertEqual(sum(p=="deepseek" for p,_ in post.calls),1)
        self.assertIsNotNone(result.product_answer)

    def test_final_trim_losing_protected_slot_restores_complete_baseline(self):
        from trading_companion.product_narrative_answer import synthesize_narrative_answer
        bundle=self.bundle(count=4)
        order,_,_=self.sort(bundle)
        baseline=prepare_answer_context("分析公司新闻",bundle)
        candidate=prepare_answer_context("分析公司新闻",bundle,headline_order=order)
        # Simulate the final message-capacity pass removing one whole evidence slot.
        candidate["evidence"].pop()
        model=ProductModelFixture()
        with patch("trading_companion.product_narrative_answer.prepare_answer_context",return_value=candidate):
            synthesize_narrative_answer("分析公司新闻",bundle,model=model,headline_order=order)
        self.assertEqual(model.contexts[0]["evidence"],baseline["evidence"])
        self.assertEqual(order.round.reason,"context_capacity")

    def test_decision_prices_and_plans_remain_identical_with_sorted_titles(self):
        from trading_companion.research_objective import ResearchObjective
        selected=replace(plan(kinds=("technical","financial","news","announcements")),
                         objective=ResearchObjective("decision","未来20个交易日"))
        bundle=collect_product_research(selected,data_service=expanded_data(4),
                                       source_factory=MemorySourceFactory(),now=NOW)
        order,_,_=self.sort(bundle)
        self.assertIsNotNone(order,self.summaries[-1])
        base=prepare_answer_context("分析公司新闻",bundle)
        after=prepare_answer_context("分析公司新闻",bundle,headline_order=order)
        self.assertTrue(compatible_context(base,after,order))
        for key in ("trade_plans","price_levels","_technical_validation"):
            self.assertEqual(base[key],after[key])


class SortingApiTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))):
            guard.start();self.addCleanup(guard.stop)

    def service(self, *, enabled=True, sorting=True, provider="deepseek", sorter=None, **options):
        from trading_companion.api_service import ResearchApiService
        from tests.test_research_intent_adapter import IntentFixture
        from tests.test_research_intent_v2 import draft
        model=ProductModelFixture()
        intent=IntentFixture([draft(research_kinds=[dict(kind="news",phrase="新闻"),
                                                  dict(kind="announcements",phrase="公告")])])
        service=ResearchApiService(model_factory=lambda:model,provider=provider,
            directory_loader=lambda _:directory(), data_mode="tencent",wall_clock=lambda:NOW,
            reference_date=NOW.date(),product_data_service=expanded_data(4),conversation_context=True,
            research_intent_v2_enabled=enabled,intent_model_factory=lambda:intent,
            news_triage_sorting=sorting,news_sorter=sorter,**options)
        self.addCleanup(service.close)
        return service,service.create_session()

    def test_only_new_deepseek_path_calls_sorter_and_request_replay_is_idempotent(self):
        for enabled,sorting,provider in ((True,True,"deepseek"),(False,True,"deepseek"),
                                         (True,False,"deepseek"),(True,True,"openai")):
            with self.subTest(enabled=enabled,sorting=sorting,provider=provider):
                post=Transport();sorter=NewsTriageSorter(configure=settings,post=post)
                service,binding=self.service(enabled=enabled,sorting=sorting,provider=provider,sorter=sorter)
                args=(binding["session_id"],binding["access_token"],"once","分析平安银行的新闻和公告")
                service.message(*args)
                count=len(post.calls)
                service.message(*args)
                self.assertEqual(len(post.calls),count)
                self.assertEqual(bool(count),enabled and sorting and provider=="deepseek")

    def test_default_and_development_modes_cannot_enable_classifier(self):
        from inspect import signature
        from trading_companion.api_service import ResearchApiService
        self.assertIs(signature(ResearchApiService).parameters['news_triage_sorting'].default,False)
        sorter=NewsTriageSorter(configure=Mock(side_effect=AssertionError("Must remain lazy")))
        service,_=self.service(sorter=sorter,trace_mode="development")
        self.assertIsNone(service._news_sorter)


if __name__ == "__main__":
    unittest.main()
