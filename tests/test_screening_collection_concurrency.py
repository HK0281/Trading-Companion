"""Real Tencent batch collector with isolated fake HTTP clients and controlled completion."""

from dataclasses import replace
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

import requests

from scripts.verify import offline_network
from trading_companion.collection_execution import CollectionExecutor, QuoteCollectionScope
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import ResearchDataRequest
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.screening import ScreeningCondition, ScreeningRequest, ScreeningService
from trading_companion.screening_source import TencentQuoteSnapshotSource, TENCENT_ENDPOINT
from trading_companion.turn_control import TurnControl, TurnStopped, bind_turn
from tests.test_collection_execution import BackgroundCall, MutableClock
from tests.test_company_collection_concurrency import FakeTransport
from tests.test_company_research import NOW as COMPANY_NOW, CUTOFF
from tests.test_screening_coverage import large_directory
from tests.test_screening_source import NOW, TencentSession, Response, directory


class QuoteTransport:
    """Fresh client per batch; hook runs inside the actual HTTP request boundary."""

    def __init__(self, trusted, hook=None, mutate=None):
        self.trusted, self.hook, self.mutate = trusted, hook, mutate
        self.lock = Lock()
        self.clients, self.calls, self.responses = [], [], []
        self.active = self.maximum = self.response_bytes = 0

    def factory(self):
        owner = self
        class Client(TencentSession):
            def __init__(self):
                super().__init__(owner.trusted, owner.mutate)
                self.closed = 0
                self.finished = Event()

            def request(self, method, url, **kwargs):
                with owner.lock:
                    index = len(owner.calls)
                    owner.calls.append((method, url, kwargs))
                    owner.active += 1
                    owner.maximum = max(owner.maximum, owner.active)
                try:
                    value = owner.hook(index, url, kwargs) if owner.hook else None
                    value = super().request(method, url, **kwargs) if value is None else value
                    with owner.lock:
                        owner.responses.append(value)
                        owner.response_bytes += len(value.body)
                    return value
                finally:
                    with owner.lock:
                        owner.active -= 1

            def close(self):
                self.closed += 1
                self.finished.set()
        client = Client()
        with self.lock:
            self.clients.append(client)
        return client


class ScreeningCollectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.pool = CollectionExecutor()
        self.addCleanup(self.pool.close, wait=True)

    def build(self, trusted=None, *, hook=None, mutate=None, **options):
        trusted = directory() if trusted is None else trusted
        http = QuoteTransport(trusted, hook, mutate)
        source = TencentQuoteSnapshotSource(session_factory=http.factory, executor=self.pool,
            clock=lambda: NOW, **options)
        return source, http

    def assert_closed(self, http):
        self.assertEqual(len(set(map(id, http.clients))), len(http.clients))
        self.assertTrue(all(client.closed == 1 for client in http.clients))
        self.assertTrue(all(response.closed for response in http.responses))

    @staticmethod
    def screen(snapshot):
        return ScreeningService(clock=lambda: NOW).screen(snapshot,
            ScreeningRequest((ScreeningCondition("price", "gt", 0),))).to_dict()

    def test_five_fifteen_twenty_batches_keep_ids_bytes_snapshots_and_sorting_at_all_widths(self):
        trusted = large_directory()
        for profile, batch_size, tasks in (("standard", 80, 5), ("expanded", 80, 15), ("expanded", 50, 20)):
            baseline = expected_calls = expected_bytes = None
            for width in (1, 2, 3):
                with self.subTest(profile=profile, batch_size=batch_size, width=width):
                    source, http = self.build(trusted, coverage=profile, batch_size=batch_size, max_workers=width)
                    actual = source.read_snapshot(trusted)
                    calls = sorted(url for _, url, _ in http.calls)
                    self.assertEqual(len(calls), tasks)
                    self.assertLessEqual(http.maximum, width)
                    self.assertEqual(sum(item.pages_read for item in actual.strata), tasks)
                    self.assertEqual(len(actual.quotes), 250 if profile == "standard" else 1000)
                    self.assert_closed(http)
                    if baseline is None:
                        baseline, expected_calls, expected_bytes = actual, calls, http.response_bytes
                        for stratum in actual.strata:
                            ids = sorted(item.instrument_id for item in trusted.securities
                                         if item.instrument_id in {quote.instrument_id for quote in actual.quotes}
                                         and item.instrument_id[:3] == {"sse_main": "600", "szse_main": "000",
                                             "star": "688", "chinext": "300", "bse": "920"}[stratum.stratum])
                            indices = [int(item[3:6]) for item in ids]
                            self.assertEqual(indices, list(range(50)) if profile == "standard" else list(range(0, 400, 2)))
                    self.assertEqual((actual, calls, http.response_bytes), (baseline, expected_calls, expected_bytes))
                    self.assertEqual(self.screen(actual), self.screen(baseline))

    def test_requested_width_applies_to_whole_plan_and_reverse_completion_cannot_reorder(self):
        expected, _ = self.build(max_workers=1)
        baseline = expected.read_snapshot(directory())
        for width in (1, 2, 3):
            entered, release = [Event() for _ in range(5)], [Event() for _ in range(5)]
            def hook(index, url, kwargs):
                entered[index].set()
                if not release[index].wait(5):
                    raise AssertionError("Unreleased quote HTTP request")
            source, http = self.build(hook=hook, max_workers=width)
            call = BackgroundCall(lambda: source.read_snapshot(directory()))
            try:
                for event in entered[:width]:
                    self.assertTrue(event.wait(5))
                self.assertFalse(entered[width].is_set())
                # Free the last occupied slot first, then keep feeding just that slot.
                for index in range(width - 1, 5):
                    self.assertTrue(entered[index].wait(5))
                    release[index].set()
                for event in reversed(release[:width - 1]):
                    event.set()
                self.assertEqual(call.result(), baseline)
                self.assertEqual(http.maximum, width)
                self.assert_closed(http)
            finally:
                for event in release:
                    event.set()

    def test_empty_and_uneven_strata_do_not_create_extra_requests_or_change_sampling(self):
        trusted = large_directory(201)
        trusted = replace(trusted, securities=tuple(item for item in trusted.securities
                          if not item.instrument_id.endswith(".BJ") and (not item.instrument_id.startswith("000")
                          or int(item.instrument_id[3:6]) < 3)))
        baseline = None
        for width in (1, 3):
            source, http = self.build(trusted, coverage="expanded", max_workers=width)
            actual = source.read_snapshot(trusted)
            self.assertEqual(len(http.calls), 10)
            self.assertEqual([(s.scanned_records, s.pages_read) for s in actual.strata],
                             [(200, 3), (3, 1), (200, 3), (200, 3), (0, 0)])
            baseline = actual if baseline is None else baseline
            self.assertEqual(actual, baseline)

    def test_one_failed_batch_stops_new_submissions_and_discards_other_successes(self):
        entered, release, ready = Event(), Event(), Event()
        def hook(index, url, kwargs):
            if index == 0:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Unreleased late quote")
            elif index == 1:
                if not ready.wait(5):
                    raise AssertionError("No third request")
                return Response(status=429)
            else:
                ready.set()
        source, http = self.build(hook=hook)
        call = BackgroundCall(lambda: source.read_snapshot(directory()))
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(DataSourceError):
                call.result()
            # Fast successes may be submitted before failure is observed, but never after return.
            count = len(http.calls)
            self.assertLessEqual(count, 5)
            release.set()
            self.pool.close(wait=True)
            self.assertEqual(len(http.calls), count)
            self.assert_closed(http)
        finally:
            release.set()

    def test_legal_omissions_and_no_match_are_successful_missing_quote_coverage(self):
        for no_match in (False, True):
            trusted = large_directory(3)
            source, http = self.build(trusted, hook=(lambda *_: Response(body=b'v_pv_none_match="1";'))
                if no_match else None, mutate=lambda rows: rows[:-1])
            result = self.screen(source.read_snapshot(trusted))
            self.assertEqual(result["coverage"]["missing_quote_records"], 15 if no_match else 5)
            self.assertEqual(result["coverage"]["unverified_records"], 0)
            self.assertEqual(result["conditions"][0]["failed"], 0)
            self.assert_closed(http)

    def test_source_deadline_is_shared_and_each_request_uses_remaining_time(self):
        clock = MutableClock()
        def advance(index, url, kwargs):
            if index == 0: clock.set(23)
            elif index == 1: clock.set(24)
        source, http = self.build(hook=advance, max_workers=1, monotonic_clock=clock)
        source.read_snapshot(directory())
        self.assertEqual([kwargs["timeout"][1] for _, _, kwargs in http.calls], [6, 2, 1, 1, 1])
        self.assert_closed(http)

    def test_source_deadline_returns_without_joining_blocked_batches_and_rejects_late_bytes(self):
        entered, release, clock = Barrier(4), Event(), MutableClock()
        def blocked(*_):
            entered.wait(5)
            if not release.wait(5):
                raise AssertionError("Unreleased deadline batch")
        source, http = self.build(hook=blocked, monotonic_clock=clock)
        captured = []
        def budget(**options):
            current = QuoteCollectionScope(**options)
            captured.append(current)
            return current
        with patch("trading_companion.screening_source.QuoteCollectionScope", side_effect=budget):
            call = BackgroundCall(lambda: source.read_snapshot(directory()))
            try:
                entered.wait(5)
                clock.set(25)
                with self.assertRaisesRegex(DataSourceError, "deadline"):
                    call.result()
                usage = captured[0].usage()
                self.assertEqual(usage, {"requests": 3, "response_bytes": 0})
                release.set()
                self.pool.close(wait=True)
                self.assertEqual(captured[0].usage(), usage)
                self.assert_closed(http)
            finally:
                release.set()

    def test_company_and_abandoned_quotes_share_the_same_three_process_slots(self):
        # Use the production default-executor lookup for both real adapters.
        entered, release, clock = Barrier(4), Event(), MutableClock()
        def block(*_):
            entered.wait(5)
            if not release.wait(5):
                raise AssertionError("Unreleased shared-pool batch")
        http = QuoteTransport(directory(), block)
        quotes = TencentQuoteSnapshotSource(session_factory=http.factory, clock=lambda: NOW, monotonic_clock=clock)
        company_http = FakeTransport()
        company_clock = MutableClock()
        company = ResearchDataService(session_factory=company_http.factory, clock=lambda: COMPANY_NOW,
            monotonic_clock=company_clock)
        request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF)
        with patch("trading_companion.screening_source.shared_collection_executor", return_value=self.pool), patch(
            "trading_companion.company_research.shared_collection_executor", return_value=self.pool):
            first = BackgroundCall(lambda: quotes.read_snapshot(directory()))
            try:
                entered.wait(5)
                clock.set(25)
                with self.assertRaises(DataSourceError): first.result()
                # New company coordinator has a separate source window, but no free permit.
                running = Event()
                def timer():
                    value = company_clock()
                    running.set()
                    return value
                company._timer = timer
                second = BackgroundCall(lambda: company.collect(request, ("financial",)))
                self.assertTrue(running.wait(5))
                company_clock.set(20)
                result = second.result()
                self.assertEqual(result.source_results[0].code, "research_data_deadline_exceeded")
                self.assertEqual(company_http.calls, [])
                self.assertEqual(len(self.pool._pool._threads), 3)
            finally:
                release.set()
        self.pool.close(wait=True)
        self.assert_closed(http)

    def test_borrowed_client_cross_call_lock_is_cancellable_and_never_closed_by_collector(self):
        entered, release, contended = Event(), Event(), Event()
        class ObservedLock:
            def __init__(self): self.lock = Lock()
            def acquire(self, *, timeout):
                if self.lock.locked(): contended.set()
                return self.lock.acquire(timeout=timeout)
            def release(self): self.lock.release()
        borrowed = TencentSession()
        original = borrowed.request
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError("Unreleased borrowed client")
            return original(*args, **kwargs)
        borrowed.request = blocked
        source = TencentQuoteSnapshotSource(session=borrowed, executor=self.pool, clock=lambda: NOW)
        source._client_lock = ObservedLock()
        controls = (TurnControl(), TurnControl())
        for control in controls: control.start()
        def invoke(control):
            with bind_turn(control): return source.read_snapshot(directory())
        first = BackgroundCall(lambda: invoke(controls[0]))
        try:
            self.assertTrue(entered.wait(5))
            second = BackgroundCall(lambda: invoke(controls[1]))
            self.assertTrue(contended.wait(5))
            controls[1].cancel()
            with self.assertRaises(TurnStopped): second.result()
            self.assertEqual(borrowed.calls, [])
            controls[0].cancel()
            with self.assertRaises(TurnStopped): first.result()
            release.set()
            self.pool.close(wait=True)
            self.assertEqual(len(borrowed.calls), 1)
            self.assertTrue(all(response.closed for response in borrowed.responses))
            self.assertFalse(hasattr(borrowed, "close"))
        finally:
            release.set()

    def test_live_company_and_quote_calls_cannot_each_take_three_slots(self):
        company_entered, release = Event(), Event()
        quote_entered = [Event() for _ in range(3)]
        def company_hook(kind):
            company_entered.set()
            if not release.wait(5): raise AssertionError("Unreleased company peer")
        def quote_hook(index, url, kwargs):
            if index < 3: quote_entered[index].set()
            if not release.wait(5): raise AssertionError("Unreleased quote peer")
        company_http = FakeTransport(company_hook)
        company = ResearchDataService(session_factory=company_http.factory, executor=self.pool,
            clock=lambda: COMPANY_NOW)
        first = BackgroundCall(lambda: company.collect(ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF),
                                                       ("financial",)))
        quotes, http = self.build(hook=quote_hook)
        second = None
        try:
            self.assertTrue(company_entered.wait(5))
            second = BackgroundCall(lambda: quotes.read_snapshot(directory()))
            for entered in quote_entered[:2]: self.assertTrue(entered.wait(5))
            self.assertFalse(quote_entered[2].is_set())
            self.assertEqual((company_http.active, http.active), (1, 2))
            release.set()
            self.assertEqual(first.result().status, "ok")
            self.assertEqual(len(second.result().quotes), 5)
            self.assert_closed(http)
        finally:
            release.set()

    def test_waiting_for_shared_pool_spends_original_quote_deadline_without_sending(self):
        entered, release = Barrier(4), Event()
        from trading_companion.collection_execution import CollectionScope
        from time import monotonic
        def blocked():
            entered.wait(5)
            if not release.wait(5): raise AssertionError("Unreleased pool occupancy")
        first = BackgroundCall(lambda: self.pool.run((blocked,) * 3,
            scope=CollectionScope(deadline=monotonic() + 20, requests=0, response_bytes=0), max_workers=3))
        clock, planning = MutableClock(), Event()
        def timer():
            value = clock()
            planning.set()
            return value
        quotes, http = self.build(monotonic_clock=timer)
        try:
            entered.wait(5)
            second = BackgroundCall(lambda: quotes.read_snapshot(directory()))
            self.assertTrue(planning.wait(5))
            clock.set(25)
            with self.assertRaisesRegex(DataSourceError, "deadline"): second.result()
            self.assertEqual(http.calls, [])
            self.assertEqual(http.clients, [])
        finally:
            release.set()
            first.result()

    def test_concurrent_small_responses_charge_actual_shared_two_megabyte_budget(self):
        trusted = large_directory()
        def pad(rows):
            for _, fields in rows: fields[0] = "1" * 2000
            return rows
        source, http = self.build(trusted, mutate=pad, coverage="expanded")
        captured = []
        def budget(**options):
            current = QuoteCollectionScope(**options)
            captured.append(current)
            return current
        with patch("trading_companion.screening_source.QuoteCollectionScope", side_effect=budget):
            with self.assertRaisesRegex(DataValidationError, "budget"):
                source.read_snapshot(trusted)
        self.pool.close(wait=True)
        self.assertLessEqual(captured[0].usage()["response_bytes"], 2_000_000)
        self.assertGreater(captured[0].usage()["response_bytes"], 1_800_000)
        self.assertTrue(all(len(response.body) <= 200_000 for response in http.responses))
        self.assertLessEqual(len(http.calls), 15)
        self.assert_closed(http)

    def test_response_protocol_status_encoding_and_chunk_errors_close_owned_clients(self):
        class BadChunk(Response):
            def iter_content(self, _): yield "not bytes"
        for value, error in ((Response(status=429), DataSourceError), (Response(body=b"\x81"), DataValidationError),
            (Response(body=b"not assignment"), DataValidationError), (BadChunk(), DataValidationError),
            (Response(headers={"Content-Length": "200001"}), DataValidationError),
            (Response(headers={"Content-Length": "invalid"}), DataValidationError),
            (Response(body=b" " * 200001), DataValidationError)):
            with self.subTest(error=error, headers=value.headers):
                source, http = self.build(hook=lambda *_: value, max_workers=1)
                with self.assertRaises(error): source.read_snapshot(directory())
                self.assertEqual(len(http.calls), 1)
                self.assert_closed(http)
        def disconnected(*_): raise requests.ConnectionError("private credentials")
        source, http = self.build(hook=disconnected, max_workers=1)
        with self.assertRaises(DataSourceError) as raised: source.read_snapshot(directory())
        self.assertNotIn("private", str(raised.exception))
        self.assert_closed(http)

    def test_twenty_small_bodies_can_exactly_fill_shared_budget_without_per_task_preallocation(self):
        trusted = large_directory()
        def response(index, url, kwargs):
            value = TencentSession(trusted).request("GET", url)
            if whitespace:
                value.body += b" " * (100_000 - len(value.body))
            else:
                lines = value.body.split(b"\n")
                padding, extra = divmod(100_000 - len(value.body), len(lines))
                value.body = b"\n".join(line.replace(b'="1~', b'="' + b"1" * (1 + padding + (i < extra)) + b"~", 1)
                                        for i, line in enumerate(lines))
            self.assertEqual(len(value.body), 100_000)
            value.headers["Content-Length"] = "100000"
            return value
        for whitespace in (False, True):
            with self.subTest(whitespace=whitespace):
                source, http = self.build(trusted, hook=response, coverage="expanded", batch_size=50)
                self.assertEqual(len(source.read_snapshot(trusted).quotes), 1000)
                self.assertEqual((len(http.calls), http.response_bytes), (20, 2_000_000))
                self.assert_closed(http)

    def test_quote_internal_budget_errors_map_to_existing_public_source_errors(self):
        for request_limit, byte_limit, error in ((0, 2_000_000, DataSourceError), (5, 1, DataValidationError)):
            captured = []
            def budget(**options):
                options.update(requests=request_limit, response_bytes=byte_limit)
                current = QuoteCollectionScope(**options)
                captured.append(current)
                return current
            source, http = self.build(max_workers=1)
            with patch("trading_companion.screening_source.QuoteCollectionScope", side_effect=budget):
                with self.assertRaises(error): source.read_snapshot(directory())
            self.assertEqual(len(http.calls), 0 if request_limit == 0 else 1)
            self.assertEqual(captured[0].usage()["response_bytes"], 0)
            self.assert_closed(http)


if __name__ == "__main__":
    unittest.main()
