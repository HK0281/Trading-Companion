"""Actual company collectors and API entry, with isolated artificial HTTP clients."""

from copy import deepcopy
from datetime import date
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from scripts.verify import offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.collection_execution import CollectionExecutor
from trading_companion.company_research import ResearchDataService, _ENDPOINTS
from trading_companion.company_research_contracts import RESEARCH_KINDS, ResearchDataRequest
from trading_companion.errors import InvalidRequestError
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.turn_trace import JsonlTraceSink
from tests.test_collection_execution import BackgroundCall, MutableClock
from tests.test_company_research import NOW, CUTOFF, announcements, financial, news, profile, response
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import plan
from tests.test_turn_trace import read_records


class FakeTransport:
    """URL-dispatched fixtures; each factory call supplies a fresh owned client."""

    def __init__(self, handler=None):
        self.handler = handler
        self.lock = Lock()
        self.clients, self.responses, self.calls = [], [], []
        self.active = self.maximum = 0

    def factory(self):
        client = Mock()
        client.request.side_effect = partial(self.request, client)
        with self.lock:
            self.clients.append(client)
        return client

    def request(self, client, method, url, **kwargs):
        kind = next(key for key, (_, endpoint) in _ENDPOINTS.items() if url == endpoint)
        with self.lock:
            self.calls.append((kind, client, kwargs))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if self.handler:
                value = self.handler(kind)
                if value is not None:
                    with self.lock:
                        self.responses.append(value)
                    return value
            payload = {"profile": profile, "financial": financial, "news": news,
                       "announcements": announcements}[kind]()
            value = response(payload)
            with self.lock:
                self.responses.append(value)
            return value
        finally:
            with self.lock:
                self.active -= 1


class CompanyCollectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        env = patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))
        env.start()
        self.addCleanup(env.stop)
        self.pool = CollectionExecutor()
        self.addCleanup(self.pool.close, wait=True)
        self.request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF, date(2026, 7, 1),
                                           allow_current_background=True)

    def build(self, transport=None, **kwargs):
        transport = FakeTransport() if transport is None else transport
        return ResearchDataService(session_factory=transport.factory, executor=self.pool,
                                   clock=lambda: NOW, news_provider="eastmoney", **kwargs), transport

    def assert_closed(self, transport):
        self.assertEqual(len({id(client) for client in transport.clients}), len(transport.clients))
        for client in transport.clients:
            client.close.assert_called_once()
        for item in transport.responses:
            item.close.assert_called_once()

    def test_reverse_completion_keeps_batch_evidence_and_model_aliases_identical(self):
        serial, baseline_http = self.build(max_workers=1)
        expected = serial.collect(self.request)
        entered = {kind: Event() for kind in RESEARCH_KINDS}
        release = {kind: Event() for kind in RESEARCH_KINDS}
        def handler(kind):
            entered[kind].set()
            if not release[kind].wait(5):
                raise AssertionError("Unreleased source fixture")
        parallel, http = self.build(FakeTransport(handler))
        call = BackgroundCall(lambda: parallel.collect(self.request))
        try:
            for kind in RESEARCH_KINDS[:3]:
                self.assertTrue(entered[kind].wait(5))
            self.assertFalse(entered["announcements"].is_set())
            release["news"].set()
            self.assertTrue(entered["announcements"].wait(5))
            for kind in ("announcements", "financial", "profile"):
                release[kind].set()
            actual = call.result()
        finally:
            for item in release.values():
                item.set()
        self.assertEqual(actual.to_dict(), expected.to_dict())
        self.assertEqual(http.maximum, 3)
        self.assert_closed(http)
        self.assert_closed(baseline_http)
        selected = plan(securities=(Security("300308.SZ", "中际旭创"),), kinds=RESEARCH_KINDS)
        first = collect_product_research(selected, data_service=serial, now=NOW)
        second, _ = self.build()
        other = collect_product_research(selected, data_service=second, now=NOW)
        self.assertEqual(first.to_dict(), other.to_dict())
        self.assertEqual(model_research_context(first), model_research_context(other))

    def test_source_failure_preserves_other_domains_and_never_retries(self):
        def fail_news(kind):
            if kind == "news":
                raise requests.Timeout("private-source-sentinel")
        service, http = self.build(FakeTransport(fail_news))
        batch = service.collect(self.request)
        self.assertEqual([section.status for section in batch.source_results],
                         ["available", "available", "unavailable", "available"])
        self.assertEqual(sorted(kind for kind, _, _ in http.calls), sorted(RESEARCH_KINDS))
        self.assertNotIn("private-source-sentinel", json.dumps(batch.to_dict()))
        self.assert_closed(http)

    def test_aggregate_request_budget_is_not_recreated_per_task(self):
        service, http = self.build(max_workers=1)
        with patch.object(service, "_allowances", return_value=(1, 2_048_000)):
            batch = service.collect(self.request)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual([item.code for item in batch.source_results[1:]],
                         ["research_request_budget_exhausted"] * 3)
        self.assert_closed(http)

    def test_byte_budget_charges_failed_json_and_is_shared_with_later_tasks(self):
        service, http = self.build(FakeTransport(lambda kind: response(text="xxxxx")), max_workers=1)
        with patch.object(service, "_allowances", return_value=(4, 5)):
            batch = service.collect(self.request)
        self.assertEqual(batch.source_results[0].code, "invalid_source_json")
        self.assertEqual([item.code for item in batch.source_results[1:]], ["research_byte_budget_exhausted"] * 3)
        self.assert_closed(http)

    def test_cninfo_two_requests_use_one_ledger_and_same_client(self):
        http = FakeTransport(lambda kind: response({"stockList": [{"code": "300308", "orgId": "123456"}]}))
        service = ResearchDataService(session_factory=http.factory, executor=self.pool,
            clock=lambda: NOW, announcement_provider="cninfo")
        self.assertEqual(service._allowances(self.request, RESEARCH_KINDS), (5, 4_048_000))
        with patch.object(service, "_allowances", return_value=(1, 2_512_000)):
            section = service.fetch_section(self.request, "announcements")
        self.assertEqual(section.code, "research_request_budget_exhausted")
        self.assertEqual([kind for kind, _, _ in http.calls], ["cninfo_directory"])
        self.assert_closed(http)

    def test_invalid_workers_and_shared_session_parallelism_fail_before_http(self):
        client = Mock()
        for workers in (0, 4, True, 1.5, "3"):
            with self.subTest(workers=workers), self.assertRaises(InvalidRequestError):
                ResearchDataService(max_workers=workers)
        for kwargs in ({"max_workers": 2}, {"max_workers": 3}, {"session_factory": lambda: client}):
            with self.assertRaises(InvalidRequestError):
                ResearchDataService(session=client, **kwargs)
        self.assertEqual(ResearchDataService(session=client).max_workers, 1)
        client.request.assert_not_called()

    def test_borrowed_client_is_serialized_across_collections_and_not_closed(self):
        entered, release, lock = Event(), Event(), Lock()
        active = maximum = calls = 0
        def request(*args, **kwargs):
            nonlocal active, maximum, calls
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls += 1
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError("Unreleased borrowed client")
                return response(financial())
            finally:
                with lock:
                    active -= 1
        client = Mock(request=Mock(side_effect=request))
        service = ResearchDataService(session=client, clock=lambda: NOW, executor=self.pool)
        first = BackgroundCall(lambda: service.fetch_section(self.request, "financial"))
        try:
            self.assertTrue(entered.wait(5))
            second = BackgroundCall(lambda: service.collect(self.request, ("financial",)))
            release.set()
            self.assertEqual(first.result().status, "available")
            self.assertEqual(second.result().status, "ok")
        finally:
            release.set()
        self.assertEqual((maximum, calls), (1, 2))
        client.close.assert_not_called()

    def test_source_deadline_returns_partial_and_detaches_late_results(self):
        clock, entered, release, closed = MutableClock(), Event(), Event(), Event()
        def handler(kind):
            if kind == "financial":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Unreleased late source")
        service, http = self.build(FakeTransport(handler), max_workers=1,
                                   monotonic_clock=clock, deadline_seconds=1)
        original_factory = service._session_factory
        def factory():
            client = original_factory()
            client.close.side_effect = closed.set
            return client
        service._session_factory = factory
        call = BackgroundCall(lambda: service.collect(self.request))
        try:
            self.assertTrue(entered.wait(5))
            closed.clear()
            clock.set(2)
            batch = call.result()
            frozen = deepcopy(batch.to_dict())
            self.assertEqual(batch.source_results[0].status, "available")
            self.assertTrue(all(item.code == "research_data_deadline_exceeded" for item in batch.source_results[1:]))
            release.set()
            self.assertTrue(closed.wait(5))
            self.assertEqual(batch.to_dict(), frozen)
        finally:
            release.set()
        self.assert_closed(http)

    def make_api(self, data, **kwargs):
        security = Security("300308.SZ", "中际旭创")
        model = ProductModelFixture((security.name,))
        service = ResearchApiService(model_factory=lambda: model,
            directory_loader=lambda _: SecurityDirectory((security,), "artificial-directory", NOW),
            data_mode="tencent", product_data_service=data, reference_date=NOW.date(),
            clock=lambda: 100.0, wall_clock=lambda: NOW, **kwargs)
        self.addCleanup(service.close)
        credentials = service.create_session()
        return service, credentials, model

    def test_real_api_entry_runs_parallel_collector_with_stable_checked_evidence(self):
        barrier = Barrier(3)
        def handler(kind):
            if kind in RESEARCH_KINDS[:3]:
                barrier.wait(5)
        data, http = self.build(FakeTransport(handler))
        service, credentials, model = self.make_api(data)
        actual = service.message(credentials["session_id"], credentials["access_token"], uuid4().hex,
            "分析中际旭创的基本面、新闻和公告，不要技术指标")["result"]
        self.assertEqual(actual["code"], "company_research_completed")
        self.assertEqual((actual["model_calls_attempted"], actual["analyses_used"]), (2, 0))
        self.assertEqual(http.maximum, 3)
        self.assertEqual(len(model.contexts), 1)
        self.assertEqual([section["kind"] for section in actual["company_research"]["coverage"][0]["sections"]],
                         list(RESEARCH_KINDS))
        self.assertIsNotNone(service._entries[credentials["session_id"]].session.product_saved)
        self.assert_closed(http)

    def test_api_cancel_before_worker_release_never_publishes_or_rewrites_trace(self):
        barrier, release, all_closed = Barrier(4), Event(), Barrier(4)
        def handler(kind):
            barrier.wait(5)
            if not release.wait(5):
                raise AssertionError("Unreleased API source")
        data, http = self.build(FakeTransport(handler))
        original = data._session_factory
        def factory():
            client = original()
            client.close.side_effect = lambda: all_closed.wait(5)
            return client
        data._session_factory = factory
        with TemporaryDirectory() as temporary:
            path = Path(temporary)
            archive = Mock()
            service, credentials, model = self.make_api(data, trace_sink=JsonlTraceSink(path), snapshot_store=archive)
            sid, token, key = credentials["session_id"], credentials["access_token"], uuid4().hex
            call = BackgroundCall(lambda: service.message(sid, token, key,
                "分析中际旭创的基本面、新闻和公告，不要技术指标"))
            try:
                barrier.wait(5)
                service.cancel(sid, token, key)
                result = call.result()["result"]
                self.assertEqual((result["status"], result["code"]), ("cancelled", "turn_cancelled"))
                first = deepcopy(read_records(path)[0])
                self.assertIsNone(service._entries[sid].session.product_saved)
                service.message(sid, token, uuid4().hex, "MACD是什么")
                self.assertEqual(len(model.calls), 1)
                release.set()
                all_closed.wait(5)
                self.pool.close(wait=True)
                self.assertEqual(read_records(path)[0], first)
                self.assertEqual(len(read_records(path)), 2)
                self.assertIsNone(service._entries[sid].session.product_saved)
                self.assertEqual(archive.mock_calls, [])
            finally:
                release.set()
        self.assertEqual(len(http.calls), 3)
        self.assert_closed(http)
