"""Flattened company/section dispatch through real collectors and artificial owned clients."""

from copy import deepcopy
from dataclasses import replace
from threading import Event
import json
import unittest
from unittest.mock import patch

import requests

from scripts.verify import offline_network
from trading_companion.collection_execution import CollectionExecutor
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import RESEARCH_KINDS, ResearchDataRequest
from trading_companion.comparison_research import build_candidate_comparison
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.security_directory import Security
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn
from tests.test_collection_execution import BackgroundCall, MutableClock
from tests.test_company_collection_concurrency import FakeTransport
from tests.test_company_research import NOW, CUTOFF
from tests.test_product_research import MemorySourceFactory, plan


COMPANIES = (Security("300308.SZ", "中际旭创"), Security("000001.SZ", "平安银行"), Security("600000.SH", "浦发银行"))
REQUESTS = tuple(ResearchDataRequest(company.instrument_id, company.name, CUTOFF, allow_current_background=True)
                 for company in COMPANIES)


class MultiCompanyCollectionTests(unittest.TestCase):
    def setUp(self):
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        guard = patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env"))
        guard.start()
        self.addCleanup(guard.stop)
        self.pool = CollectionExecutor()
        self.addCleanup(self.pool.close, wait=True)

    def build(self, http=None, **kwargs):
        http = FakeTransport() if http is None else http
        return ResearchDataService(session_factory=http.factory, executor=self.pool,
                                   clock=lambda: NOW, news_provider="eastmoney", **kwargs), http

    def test_width_is_applied_once_to_all_twelve_tasks_and_rotates_companies(self):
        for width in (1, 2, 3):
            with self.subTest(width=width):
                wave = ([(REQUESTS[0].instrument_id, "profile")] if width == 1 else
                        [(r.instrument_id, "profile") for r in REQUESTS[:width]])
                entered, release = {key: Event() for key in wave}, Event()
                http = FakeTransport()
                def handler(symbol, kind, kwargs):
                    if (symbol, kind) in entered:
                        entered[symbol, kind].set()
                        if not release.wait(5):
                            raise AssertionError("Unreleased multi-company source")
                http.company_handler = handler
                service, _ = self.build(http, max_workers=width)
                call = BackgroundCall(lambda: service.collect_many(REQUESTS))
                try:
                    for event in entered.values():
                        self.assertTrue(event.wait(5))
                    self.assertEqual(len(http.calls), width)
                    self.assertEqual(sorted(http.company_calls), sorted(wave))
                    release.set()
                    batches = call.result()
                finally:
                    release.set()
                self.assertEqual(http.maximum, width)
                self.assertEqual(len(http.calls), 12)
                self.assertTrue(all(batch.status == "ok" for batch in batches))
                self.assertEqual([batch.request for batch in batches], list(REQUESTS))
                if width == 1:
                    self.assertEqual(http.company_calls, [(r.instrument_id, kind) for r in REQUESTS for kind in RESEARCH_KINDS])
                for client in http.clients:
                    client.close.assert_called_once()

    def test_cross_company_single_financial_requests_overlap(self):
        entered, release = [Event() for _ in range(3)], Event()
        def handler(symbol, kind, kwargs):
            entered[[r.instrument_id for r in REQUESTS].index(symbol)].set()
            if not release.wait(5):
                raise AssertionError("Unreleased financial source")
        service, http = self.build(FakeTransport(company_handler=handler))
        call = BackgroundCall(lambda: service.collect_many(REQUESTS, ("financial",)))
        try:
            for event in entered:
                self.assertTrue(event.wait(5))
            self.assertEqual(http.maximum, 3)
            release.set()
            self.assertTrue(all(batch.items for batch in call.result()))
        finally:
            release.set()

    def test_reverse_completion_keeps_evidence_aliases_comparison_and_dependencies_stable(self):
        selected = plan(securities=COMPANIES, kinds=RESEARCH_KINDS)
        serial, _ = self.build(max_workers=1)
        expected = collect_product_research(selected, data_service=serial, now=NOW)
        keys = [(r.instrument_id, kind) for kind in RESEARCH_KINDS for r in REQUESTS]
        entered, release = {key: Event() for key in keys}, {key: Event() for key in keys}
        def handler(symbol, kind, kwargs):
            entered[symbol, kind].set()
            if not release[symbol, kind].wait(5):
                raise AssertionError("Unreleased reversed source")
        service, http = self.build(FakeTransport(company_handler=handler))
        call = BackgroundCall(lambda: collect_product_research(selected, data_service=service, now=NOW))
        try:
            for key in keys[:3]:
                self.assertTrue(entered[key].wait(5))
            # Keep the first two companies' profiles pending while the last slot cycles all other work.
            for key in keys[2:] + [keys[1], keys[0]]:
                self.assertTrue(entered[key].wait(5))
                release[key].set()
            actual = call.result()
        finally:
            for event in release.values():
                event.set()
        self.assertEqual(http.maximum, 3)
        self.assertEqual(actual.to_dict(), expected.to_dict())
        self.assertEqual(model_research_context(actual), model_research_context(expected))
        self.assertEqual(build_candidate_comparison(actual).to_dict(), build_candidate_comparison(expected).to_dict())
        comparison_records = [item for item in actual.evidence if item.source == "application_comparison"]
        self.assertTrue(comparison_records)
        self.assertTrue(all(len(item.dependencies) == 3 for item in comparison_records))

    def test_one_company_all_sources_fail_and_another_news_fails_without_discarding_third(self):
        def handler(symbol, kind, kwargs):
            if symbol == REQUESTS[0].instrument_id or symbol == REQUESTS[1].instrument_id and kind == "news":
                raise requests.Timeout("private-multi-company-source-sentinel")
        service, http = self.build(FakeTransport(company_handler=handler))
        bundle = collect_product_research(plan(securities=COMPANIES, kinds=RESEARCH_KINDS), data_service=service, now=NOW)
        first, second, third = bundle.companies
        self.assertEqual(first.evidence, ())
        self.assertTrue(all(item["status"] == "unavailable" for item in first.coverage))
        self.assertEqual([item["status"] for item in second.coverage], ["available", "available", "unavailable", "available"])
        self.assertTrue(all(item["status"] == "available" for item in third.coverage))
        self.assertTrue(third.evidence)
        self.assertEqual(len(http.calls), 12)
        self.assertTrue(bundle.partial)
        self.assertNotIn("private-multi-company-source-sentinel", json.dumps(bundle.to_dict()))
        self.assertTrue(all(metric.status != "comparable" for metric in build_candidate_comparison(bundle).metrics))

    def test_company_request_and_byte_budgets_cannot_be_borrowed_by_other_companies(self):
        for quota, expected_code in (((1, 2_048_000), "research_request_budget_exhausted"),
                                     ((4, 1), "research_byte_budget_exhausted")):
            with self.subTest(quota=quota):
                service, http = self.build(max_workers=1)
                original = service._allowances
                def allowances(request, kinds):
                    return quota if request.instrument_id == REQUESTS[0].instrument_id else original(request, kinds)
                with patch.object(service, "_allowances", side_effect=allowances):
                    batches = service.collect_many(REQUESTS)
                self.assertTrue(all(batch.status == "ok" for batch in batches[1:]))
                self.assertTrue(all(section.code == expected_code for section in batches[0].source_results[1:]))
                if quota[1] == 1:
                    self.assertEqual(batches[0].source_results[0].code, expected_code)
                expected_calls = 9 if quota[0] == 1 else 12
                self.assertEqual(len(http.calls), expected_calls)

    def test_third_company_late_section_keeps_all_previous_accepted_evidence(self):
        clock, entered, release = MutableClock(), Event(), Event()
        def handler(symbol, kind, kwargs):
            if symbol == REQUESTS[2].instrument_id and kind == "announcements":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Unreleased late third company")
        service, http = self.build(FakeTransport(company_handler=handler), max_workers=1,
                                   monotonic_clock=clock, deadline_seconds=1)
        call = BackgroundCall(lambda: collect_product_research(plan(securities=COMPANIES, kinds=RESEARCH_KINDS),
                                                              data_service=service, now=NOW))
        try:
            self.assertTrue(entered.wait(5))  # Width 1 proves the other eleven results were accepted.
            clock.set(2)
            bundle = call.result()
            frozen = deepcopy(bundle.to_dict())
            self.assertTrue(all(item["status"] == "available" for company in bundle.companies[:2] for item in company.coverage))
            self.assertEqual([item["status"] for item in bundle.companies[2].coverage], ["available"] * 3 + ["unavailable"])
            self.assertEqual(bundle.companies[2].coverage[-1]["code"], "research_data_deadline_exceeded")
            self.assertTrue(bundle.partial)
            self.assertTrue(all(company.evidence for company in bundle.companies))
            release.set()
            self.pool.close(wait=True)
            self.assertEqual(bundle.to_dict(), frozen)
        finally:
            release.set()

    def test_deadline_is_not_restarted_when_the_next_company_begins(self):
        clock = MutableClock()
        timeouts = []
        def handler(symbol, kind, kwargs):
            timeouts.append(kwargs["timeout"][1])
            clock.set(clock() + 1)
        service, http = self.build(FakeTransport(company_handler=handler), max_workers=1,
                                   monotonic_clock=clock, deadline_seconds=5)
        batches = service.collect_many(REQUESTS)
        self.assertEqual(timeouts, [5, 4, 3, 2, 1])
        self.assertEqual(len(http.calls), 5)
        self.assertTrue(all(section.status == "available" for section in batches[0].source_results))
        self.assertTrue(all(section.code == "research_data_deadline_exceeded" for batch in batches[1:] for section in batch.source_results))

    def test_cancel_in_data_phase_returns_without_starting_any_daily_analysis(self):
        control, entered, release = TurnControl(), Event(), Event()
        control.start()
        def handler(symbol, kind, kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Unreleased cancelled data phase")
        service, http = self.build(FakeTransport(company_handler=handler))
        factory = MemorySourceFactory()
        def invoke():
            with bind_turn(control):
                return collect_product_research(plan(securities=COMPANIES, kinds=("financial", "technical")),
                                                data_service=service, now=NOW, source_factory=factory)
        call = BackgroundCall(invoke)
        try:
            self.assertTrue(entered.wait(5))
            control.cancel()
            with self.assertRaises(TurnStopped) as caught:
                call.result()
            self.assertEqual(caught.exception.code, "turn_cancelled")
            self.assertEqual(factory.requests, [])
            self.assertEqual(control.to_dict()["analyses_used"], 0)
        finally:
            release.set()

    def test_daily_analysis_stays_in_caller_order_with_one_shared_allowance(self):
        for allowance in (2, 3):
            control, factory = TurnControl(TurnLimits(analyses=allowance, profile="company_research")), MemorySourceFactory()
            service, http = self.build()
            control.start()
            with self.subTest(allowance=allowance), bind_turn(control):
                def collect():
                    return collect_product_research(plan(securities=COMPANIES, kinds=("financial", "technical")),
                        data_service=service, now=NOW, source_factory=factory)
                if allowance == 2:
                    with self.assertRaises(TurnStopped) as caught:
                        collect()
                    self.assertEqual(caught.exception.code, "analysis_budget_exhausted")
                else:
                    self.assertEqual(len(collect().companies), 3)
            self.assertEqual([request.instrument_id for request in factory.reads],
                             [company.instrument_id for company in COMPANIES[:allowance]])
            self.assertEqual(control.to_dict()["analyses_used"], allowance)
            self.assertEqual(len(http.calls), 3)

    def test_all_bad_prefetches_are_rejected_before_daily_analysis(self):
        fixture = ResearchDataService()  # Its batch method is replaced; no client is created.
        selected = plan(securities=COMPANIES, kinds=("financial", "technical"))
        from tests.test_product_research import CompanyDataFixture
        data = CompanyDataFixture()
        def tamper(requests_, kinds):
            batches = tuple(data.collect(request, kinds) for request in requests_)
            return transform(batches)
        def wrong_evidence(batches):
            section = batches[-1].source_results[0]
            item = replace(section.items[0], instrument_id=REQUESTS[0].instrument_id)
            return (*batches[:-1], replace(batches[-1], source_results=(replace(section, items=(item,)),)))
        def future(batches):
            section = batches[-1].source_results[0]
            item = replace(section.items[0], published_at=NOW.replace(year=2027), published_date=NOW.date().replace(year=2027))
            return (*batches[:-1], replace(batches[-1], source_results=(replace(section, items=(item,)),)))
        for transform in (lambda b: list(b), lambda b: b[:2], lambda b: b[::-1], wrong_evidence, future):
            factory = MemorySourceFactory()
            with self.subTest(transform=transform), patch.object(fixture, "collect_many", side_effect=tamper), self.assertRaises(ValueError):
                collect_product_research(selected, data_service=fixture, now=NOW, source_factory=factory)
            self.assertEqual(factory.requests, [])
