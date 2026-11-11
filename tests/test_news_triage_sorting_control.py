"""Budget, cancellation and settlement races without provider requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from trading_companion.model import TokenUsage
from trading_companion.news_triage_sorting_control import SortingRound, invoke, submit_classifier
from trading_companion.turn_control import company_turn_control, TurnStopped
from trading_companion.turn_trace import JsonlTraceSink, TraceLimits, trace_message, news_triage_sink
from trading_companion.typesafe_jev import ClassifierError
from tests.test_news_triage_sorting import Transport, settings
from trading_companion.news_triage_product_models import ProductJev
from tests.test_news_triage_product import sample
from trading_companion.news_triage_product import input_state
from tests.test_turn_trace import read_records


class SortingControlTests(unittest.TestCase):
    def control(self):
        c=company_turn_control();c.start();return c

    def test_optional_reservations_atomic_and_never_take_last_two_calls(self):
        c=self.control()
        with ThreadPoolExecutor(max_workers=8) as pool:
            allowed=list(pool.map(lambda _:c.optional_model_available(100,500,reserve=True), range(20)))
        self.assertEqual(sum(allowed),4)
        self.assertIsNone(c.to_dict()["stop_code"])
        c.reserve_model();c.reserve_model()
        self.assertEqual(c.to_dict()["model_calls_attempted"],6)

    def test_optional_time_or_token_refusal_does_not_stop_the_answer(self):
        c=self.control()
        self.assertFalse(c.optional_model_available(8000,17000,reserve=True))
        self.assertEqual(c.to_dict()["model_calls_attempted"],0)
        self.assertIsNone(c.to_dict()["stop_code"])
        c.observe_response(TokenUsage(23000,0,23000))
        with self.assertRaises(TurnStopped) as caught:
            c.observe_optional_response(TokenUsage(1000,0,1000))
        self.assertEqual(caught.exception.code,"token_budget_exhausted")

    def test_bounded_pool_refuses_instead_of_queuing_across_turns(self):
        release=Event();started=[Event() for _ in range(3)]
        futures=[]
        try:
            for event in started:
                futures.append(submit_classifier(lambda e=event:(e.set(),release.wait(3))))
            self.assertTrue(all(e.wait(1) for e in started))
            self.assertIsNone(submit_classifier(lambda:None))
        finally:
            release.set()
            for future in futures:
                if future is not None:future.result(timeout=3)

    def test_late_settlement_retains_usage_without_changing_published_or_next_control(self):
        c=self.control();next_control=self.control()
        with TemporaryDirectory() as temp:
            sink=JsonlTraceSink(Path(temp))
            with trace_message(sink,mode="normal",provider="deepseek",history=False):
                round=SortingRound(c,sink=news_triage_sink())
                call=round.begin("jev","jev-latest")
                round.finish("triage_timeout")
                c.finish()
            before=c.to_dict()
            with self.assertRaises(ClassifierError):
                round.settle(call,dict(model="jev-test",usage=dict(prompt_tokens=3100,completion_tokens=0,total_tokens=3100)),"ok",round.started)
            self.assertEqual(c.to_dict(),before)
            self.assertEqual(next_control.to_dict()["observed_tokens"],0)
            records=read_records(Path(temp))
            self.assertEqual(records[-1]["record_type"],"news_triage_settlement")
            self.assertEqual(records[-1]["news_triage"]["calls"][0]["usage"]["total_tokens"],3100)
            self.assertTrue(records[-1]["news_triage"]["calls"][0]["late"])

    def test_late_deepseek_cannot_mutate_finished_control(self):
        c=self.control();round=SortingRound(c)
        call=round.begin("deepseek","deepseek-flash")
        round.finish("triage_timeout");c.finish();before=c.to_dict()
        with self.assertRaises(ClassifierError):
            round.settle(call,dict(model="deepseek-flash",usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15)),"ok",round.started)
        self.assertEqual(c.to_dict(),before)

    def test_small_trace_record_retains_totals_and_counts_omitted_call_details(self):
        c=self.control()
        with TemporaryDirectory() as temp:
            sink=JsonlTraceSink(Path(temp),limits=TraceLimits(record_bytes=2048))
            with trace_message(sink,mode="normal",provider="deepseek",history=False):
                round=SortingRound(c,sink=news_triage_sink())
                for _ in range(10):
                    call=round.begin("jev","jev-latest")
                    round.settle(call,dict(model="jev-test",usage=dict(prompt_tokens=3100,completion_tokens=0,total_tokens=3100)),
                                 "ok",round.started)
                round.finish("classified")
            records=read_records(Path(temp))
            self.assertEqual(len(records),2)
            self.assertEqual(records[0]["schema_version"],records[1]["schema_version"])
            triage=records[1]["news_triage"]
            self.assertEqual(triage["totals"]["jev"],dict(attempts=10,known_tokens=31000,unknown_calls=0))
            self.assertEqual(len(triage["calls"])+triage["omitted_calls"],10)
            self.assertGreater(triage["omitted_calls"],0)

    def test_cancel_during_streaming_closes_response_and_preserves_turn_stop(self):
        c=self.control();round=SortingRound(c)
        transport=Transport();responses=[]
        def post(url,**kwargs):
            response=transport(url,**kwargs);responses.append(response)
            original=response.iter_content
            def chunks(**options):
                c.cancel()
                yield from original(**options)
            response.iter_content=chunks
            return response
        with self.assertRaises(TurnStopped) as caught:
            invoke(round,"jev",lambda p:ProductJev(settings()[0],post=p).classify(input_state(sample())),
                   deadline=round.deadline,post=post)
        self.assertEqual(caught.exception.code,"turn_cancelled")
        self.assertTrue(responses[0].closed)


if __name__=="__main__":unittest.main()
