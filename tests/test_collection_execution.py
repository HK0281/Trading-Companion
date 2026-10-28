"""Deterministic dispatch, revocation and atomic allowance contracts; no network."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier, Event, Lock, Thread
from time import monotonic
import unittest

from trading_companion.collection_execution import CollectionExecutor, CollectionScope, CollectionUnavailable, QuoteCollectionScope
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn


class MutableClock:
    def __init__(self, value=0.0):
        self.value, self.lock = value, Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = value


class BackgroundCall:
    """A test driver that preserves exceptions and never leaves an unbounded join."""

    def __init__(self, function):
        self.value = self.error = None
        def run():
            try:
                self.value = function()
            except BaseException as error:
                self.error = error
        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(5)
        if self.thread.is_alive():
            raise AssertionError("Coordinator did not exit while its workers were blocked")
        if self.error is not None:
            raise self.error
        return self.value


def scope(*, clock=monotonic, timeout=20, requests=5, response_bytes=4_048_000):
    return CollectionScope(deadline=clock() + timeout, requests=requests, response_bytes=response_bytes, clock=clock)


class CollectionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.pool = CollectionExecutor()
        self.addCleanup(self.pool.close, wait=True)

    def test_quote_twenty_descriptions_share_three_permits_without_expanding_company_limits(self):
        current = QuoteCollectionScope(deadline=monotonic() + 20, requests=20)
        entered, release = [Event() for _ in range(20)], Event()
        def task(index):
            current.reserve_request()
            entered[index].set()
            if not release.wait(5):
                raise AssertionError("Unreleased quote task")
            return index
        call = BackgroundCall(lambda: self.pool.run(tuple(lambda i=i: task(i) for i in range(20)),
            scope=current, max_workers=3))
        try:
            for event in entered[:3]:
                self.assertTrue(event.wait(5))
            self.assertFalse(any(event.is_set() for event in entered[3:]))
            release.set()
            self.assertEqual(call.result(), tuple(range(20)))
            self.assertEqual(current.usage()["requests"], 20)
        finally:
            release.set()
        for budget, size in ((scope(), 13), (QuoteCollectionScope(deadline=monotonic() + 20, requests=20), 21)):
            with self.assertRaises(InvalidRequestError):
                self.pool.run((lambda: None,) * size, scope=budget, max_workers=3)
        self.assertEqual(self.pool.run((), scope=QuoteCollectionScope(deadline=monotonic() + 20, requests=0),
                                       max_workers=3), ())

    def test_quote_atomic_actual_bytes_and_requests_never_refund_or_accept_after_close(self):
        for request, limit, size, expected in ((True, 2, 1, "research_request_budget_exhausted"),
            (False, 2_000_000, 1_000_000, "research_byte_budget_exhausted")):
            current = QuoteCollectionScope(deadline=monotonic() + 20, requests=2, response_bytes=2_000_000)
            barrier = Barrier(3)
            def charge():
                barrier.wait(5)
                try:
                    current.reserve_request() if request else current.consume(size)
                    return "ok"
                except CollectionUnavailable as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=3) as workers:
                self.assertEqual(sorted(workers.map(lambda _: charge(), range(3))), sorted(["ok", "ok", expected]))
            self.assertEqual(current.usage()["requests" if request else "response_bytes"], limit)
            current.close()
            for action in (current.reserve_request, lambda: current.consume(0), lambda: current.accept(0, "late")):
                with self.assertRaises(CollectionUnavailable):
                    action()
        for options in ({"requests": 21}, {"requests": True}, {"requests": -1},
                        {"requests": 1, "response_bytes": 2_000_001}, {"requests": 1, "response_bytes": True}):
            with self.assertRaises(InvalidRequestError):
                QuoteCollectionScope(deadline=100, **options)

    def test_twelve_descriptions_use_at_most_the_requested_width(self):
        for width in (1, 2, 3):
            with self.subTest(width=width):
                entered, release = [Event() for _ in range(12)], Event()
                def task(index):
                    entered[index].set()
                    if not release.wait(5):
                        raise AssertionError("Unreleased collection task")
                    return index
                call = BackgroundCall(lambda: self.pool.run(tuple(lambda i=i: task(i) for i in range(12)),
                                                            scope=scope(), max_workers=width))
                try:
                    for event in entered[:width]:
                        self.assertTrue(event.wait(5))
                    self.assertFalse(entered[width].is_set())
                    release.set()
                    self.assertEqual(call.result(), tuple(range(12)))
                finally:
                    release.set()
        with self.assertRaises(InvalidRequestError):
            self.pool.run((lambda: None,) * 13, scope=scope(), max_workers=3)

    def test_company_and_batch_ledgers_charge_atomically_without_borrowing(self):
        for method, key, expected in (("reserve_request", "requests", "research_request_budget_exhausted"),
                                      ("consume", "response_bytes", "research_byte_budget_exhausted")):
            current = CollectionScope(deadline=monotonic() + 20, company_allowances=((2, 2), (1, 1), (0, 0)))
            first, second, third = (current.for_company(index) for index in range(3))
            barrier = Barrier(3)
            def spend():
                barrier.wait(5)
                try:
                    getattr(first, method)(*(() if method == "reserve_request" else (1,)))
                    return "ok"
                except CollectionUnavailable as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=3) as pool:
                self.assertEqual(sorted(pool.map(lambda _: spend(), range(3))), sorted(["ok", "ok", expected]))
            self.assertEqual((current.usage()[key], first.usage()[key], second.usage()[key]), (2, 2, 0))
            with self.assertRaises(CollectionUnavailable):
                getattr(third, method)(*(() if method == "reserve_request" else (1,)))
            getattr(second, method)(*(() if method == "reserve_request" else (1,)))
            self.assertEqual((current.usage()[key], second.usage()[key]), (3, 1))
            with self.assertRaises(CollectionUnavailable):
                getattr(first, method)(*(() if method == "reserve_request" else (1,)))
        current.close()
        with self.assertRaises(CollectionUnavailable):
            second.consume(0)

    def test_batch_allowances_must_be_one_to_three_bounded_company_quotas(self):
        for allowances in ((), ((6, 1),), ((1, 4_048_001),), ((True, 1),), ((1, -1),),
                           ((1, 1),) * 4, [(1, 1)], ((1,),)):
            with self.subTest(allowances=allowances), self.assertRaises(InvalidRequestError):
                CollectionScope(deadline=100, company_allowances=allowances)
        with self.assertRaises(InvalidRequestError):
            CollectionScope(deadline=100, requests=15, response_bytes=12_144_000)
        current = CollectionScope(deadline=monotonic() + 20, company_allowances=((5, 4_048_000),) * 3)
        with self.assertRaises(InvalidRequestError):
            current.reserve_request()
        for index in (True, -1, 3):
            with self.assertRaises(InvalidRequestError):
                current.for_company(index)

    def test_concurrent_company_views_update_the_same_batch_totals(self):
        current = CollectionScope(deadline=monotonic() + 20, company_allowances=((1, 1),) * 3)
        views, barrier = tuple(current.for_company(index) for index in range(3)), Barrier(3)
        def spend(view):
            barrier.wait(5)
            view.reserve_request()
            view.consume(1)
            for action in (view.reserve_request, lambda: view.consume(1)):
                with self.assertRaises(CollectionUnavailable):
                    action()
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(spend, views))
        self.assertEqual(current.usage(), {"requests": 3, "response_bytes": 3})
        self.assertTrue(all(view.usage() == {"requests": 1, "response_bytes": 1} for view in views))

    def test_three_inflight_and_original_order_with_reverse_completion(self):
        entered = [Event() for _ in range(4)]
        release = [Event() for _ in range(4)]
        self.addCleanup(lambda: [item.set() for item in release])
        def task(index):
            entered[index].set()
            if not release[index].wait(5):
                raise AssertionError("Test barrier not released")
            return index
        call = BackgroundCall(lambda: self.pool.run(tuple(lambda i=i: task(i) for i in range(4)),
                                                    scope=scope(), max_workers=3))
        try:
            for item in entered[:3]:
                self.assertTrue(item.wait(5))
            self.assertFalse(entered[3].is_set())
            release[2].set()
            self.assertTrue(entered[3].wait(5))
            release[3].set()
            release[1].set()
            release[0].set()
            self.assertEqual(call.result(), (0, 1, 2, 3))
        finally:
            for item in release:
                item.set()

    def test_abandoned_futures_keep_all_permits_across_repeated_collections(self):
        clock, release, barrier = MutableClock(), Event(), Barrier(4)
        self.addCleanup(release.set)
        def blocked():
            barrier.wait(5)
            if not release.wait(5):
                raise AssertionError("Test worker not released")
            return "late"
        first = BackgroundCall(lambda: self.pool.run((blocked,) * 3,
            scope=scope(clock=clock, timeout=1), max_workers=3))
        try:
            barrier.wait(5)
            clock.set(2)
            self.assertEqual(first.result(), (None, None, None))
            called = Event()
            for _ in range(3):
                current = scope(clock=clock, timeout=1)
                started = Event()
                def invoke():
                    started.set()
                    return self.pool.run((lambda: called.set(),), scope=current, max_workers=1)
                later = BackgroundCall(invoke)
                self.assertTrue(started.wait(5))
                clock.set(clock() + 2)
                self.assertEqual(later.result(), (None,))
            self.assertFalse(called.is_set())
            self.assertEqual(len(self.pool._pool._threads), 3)
        finally:
            release.set()
        self.assertEqual(self.pool.run((lambda: "recovered",), scope=scope(), max_workers=1), ("recovered",))

    def test_each_worker_has_independent_context_and_same_turn_budget(self):
        marker = ContextVar("collection-test-marker", default=None)
        barrier = Barrier(3)
        control = TurnControl(TurnLimits(analyses=1))
        control.start()
        def task():
            before = marker.get()
            marker.set("worker-local")
            barrier.wait(5)
            from trading_companion.turn_control import reserve_analysis
            try:
                reserve_analysis()
                return before, "reserved"
            except TurnStopped as error:
                return before, error.code
        # The coordinator is the current thread, so it copies this exact binding for every task.
        with bind_turn(control):
            token = marker.set("original")
            try:
                result = self.pool.run((task,) * 3, scope=scope(), max_workers=3)
            except TurnStopped as error:
                self.assertEqual(error.code, "analysis_budget_exhausted")
            else:
                self.fail("The shared turn budget must stop the coordinator as well")
            self.assertEqual(marker.get(), "original")
            marker.reset(token)
        self.assertEqual(control.to_dict()["analyses_used"], 1)

    def test_context_changes_do_not_escape_workers_or_cross_calls(self):
        marker, barrier = ContextVar("isolated-marker", default="unset"), Barrier(3)
        def task():
            original = marker.get()
            marker.set("local")
            barrier.wait(5)
            return original
        token = marker.set("caller")
        try:
            self.assertEqual(self.pool.run((task,) * 3, scope=scope(), max_workers=3), ("caller",) * 3)
            self.assertEqual(marker.get(), "caller")
        finally:
            marker.reset(token)
        self.assertEqual(self.pool.run((marker.get,), scope=scope(), max_workers=1), ("unset",))

    def test_source_deadline_preserves_accepted_results_and_rejects_late_ones(self):
        clock, entered, release = MutableClock(), Event(), Event()
        self.addCleanup(release.set)
        def late():
            entered.set()
            release.wait(5)
            return "late"
        # Width 1 makes entry into the second task prove acceptance of the first one.
        current = scope(clock=clock, timeout=1)
        call = BackgroundCall(lambda: self.pool.run((lambda: "on-time", late), scope=current, max_workers=1))
        try:
            self.assertTrue(entered.wait(5))
            clock.set(2)
            self.assertEqual(call.result(), ("on-time", None))
            with self.assertRaises(CollectionUnavailable):
                current.accept(1, "cannot append")
        finally:
            release.set()

    def test_cancellation_returns_before_worker_release_and_revokes_context(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        control = TurnControl()
        control.start()
        current = scope()
        def late():
            entered.set()
            release.wait(5)
            current.check()
        def invoke():
            with bind_turn(control):
                return self.pool.run((late,), scope=current, max_workers=1)
        call = BackgroundCall(invoke)
        try:
            self.assertTrue(entered.wait(5))
            control.cancel()
            with self.assertRaises(TurnStopped) as caught:
                call.result()
            self.assertEqual(caught.exception.code, "turn_cancelled")
            control.finish()
            with self.assertRaises(TurnStopped):
                current.check()  # No access to the finished control is needed.
        finally:
            release.set()

    def test_request_and_byte_reservations_are_atomic_non_refundable(self):
        for method, limit, expected in (("reserve_request", 2, "research_request_budget_exhausted"),
                                        ("consume", 2, "research_byte_budget_exhausted")):
            current, barrier = scope(requests=limit, response_bytes=limit), Barrier(3)
            def consume():
                barrier.wait(5)
                try:
                    getattr(current, method)(*(() if method == "reserve_request" else (1,)))
                    return "ok"
                except CollectionUnavailable as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=3) as workers:
                results = list(workers.map(lambda _: consume(), range(3)))
            self.assertEqual(sorted(results), sorted(["ok", "ok", expected]))
            self.assertEqual(current.usage()["requests" if method == "reserve_request" else "response_bytes"], limit)
            with self.assertRaises(CollectionUnavailable):
                getattr(current, method)(*(() if method == "reserve_request" else (1,)))

    def test_closed_scope_does_not_accept_results_or_consume_more_budget(self):
        current = scope()
        current.close()
        for action in (current.check, current.reserve_request, lambda: current.consume(1),
                       lambda: current.accept(0, "late")):
            with self.assertRaises(CollectionUnavailable):
                action()
        self.assertEqual(current.results(1), (None,))
        self.assertEqual(current.usage(), {"requests": 0, "response_bytes": 0})

    def test_turn_deadline_stops_coordinator_even_when_source_deadline_has_time(self):
        clock, entered, release = MutableClock(), Event(), Event()
        self.addCleanup(release.set)
        control = TurnControl(TurnLimits(timeout_seconds=1), clock=clock)
        control.start()
        def blocked():
            entered.set()
            release.wait(5)
            return "late"
        def invoke():
            with bind_turn(control):
                return self.pool.run((blocked,), scope=scope(timeout=20), max_workers=1)
        call = BackgroundCall(invoke)
        try:
            self.assertTrue(entered.wait(5))
            clock.set(2)
            with self.assertRaises(TurnStopped) as caught:
                call.result()
            self.assertEqual(caught.exception.code, "turn_deadline_exceeded")
        finally:
            release.set()
