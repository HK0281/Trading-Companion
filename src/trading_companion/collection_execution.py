"""Bounded synchronous I/O dispatch; abandoned work keeps its permit until it exits."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from math import isfinite
from threading import BoundedSemaphore, Event, Lock
from time import monotonic

from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import TurnStopped, checkpoint, request_timeout


POLL_SECONDS = 0.02


class CollectionUnavailable(Exception):
    """Fixed local source failure, never an arbitrary transport error message."""

    def __init__(self, code):
        if code not in ("research_data_deadline_exceeded", "research_request_budget_exhausted",
                        "research_byte_budget_exhausted"):
            raise InvalidRequestError("Use a fixed collection failure code")
        self.code = code
        super().__init__(code)


class CollectionScope:
    """One absolute source deadline and atomic, non-refundable request/body allowances."""

    def __init__(self, *, deadline, requests, response_bytes, clock=monotonic):
        if (type(deadline) not in (int, float) or not isfinite(deadline) or not callable(clock)
                or type(requests) is not int or not 0 <= requests <= 5
                or type(response_bytes) is not int or not 0 <= response_bytes <= 4_048_000):
            raise InvalidRequestError("Use bounded collection allowances")
        self.deadline, self._clock = deadline, clock
        self._request_limit, self._byte_limit = requests, response_bytes
        self._requests = self._bytes = 0
        self._lock = Lock()
        self._closed = False
        self._stop = None
        self._results = {}

    def _check(self):
        if self._closed or self._clock() >= self.deadline:
            if self._stop is not None:
                raise TurnStopped(self._stop)
            raise CollectionUnavailable("research_data_deadline_exceeded")

    def check(self):
        # Check revocation first: a late worker must not use a finished TurnControl.
        with self._lock:
            self._check()
        checkpoint()
        with self._lock:
            self._check()

    def remaining(self):
        self.check()
        with self._lock:
            self._check()
            return max(0.0, self.deadline - self._clock())

    def timeout(self, per_request):
        return request_timeout(min(per_request, self.remaining()))

    def reserve_request(self):
        self.check()
        with self._lock:
            self._check()
            if self._requests >= self._request_limit:
                raise CollectionUnavailable("research_request_budget_exhausted")
            self._requests += 1

    def consume(self, size):
        if type(size) is not int or size < 0:
            raise InvalidRequestError("Count actual nonnegative response bytes")
        self.check()
        with self._lock:
            self._check()
            if self._bytes + size > self._byte_limit:
                raise CollectionUnavailable("research_byte_budget_exhausted")
            self._bytes += size

    def accept(self, index, value):
        self.check()
        with self._lock:
            self._check()
            self._results[index] = value

    def close(self, stop=None):
        with self._lock:
            if not self._closed:
                self._closed, self._stop = True, stop

    def results(self, count):
        with self._lock:
            return tuple(self._results.get(index) for index in range(count))

    def usage(self):
        with self._lock:
            return {"requests": self._requests, "response_bytes": self._bytes}


class CollectionExecutor:
    """At most three unfinished futures, including work abandoned by earlier callers."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="company-collection")
        self._permits = BoundedSemaphore(3)
        self._lock = Lock()
        self._closed = False

    def _submit(self, function, scope):
        scope.check()
        if not self._permits.acquire(blocking=False):
            return None
        try:
            context = copy_context()  # A distinct context for every submitted task.
            with self._lock:
                if self._closed:
                    raise InvalidRequestError("Collection executor is closed")
                future = self._pool.submit(context.run, function)
        except BaseException:
            self._permits.release()
            raise
        future.add_done_callback(lambda completed: self._permits.release())
        return future

    def run(self, tasks, *, scope, max_workers):
        if type(max_workers) is not int or not 1 <= max_workers <= 3:
            raise InvalidRequestError("Choose one to three collection workers")
        if type(tasks) is not tuple or len(tasks) > 4 or any(not callable(task) for task in tasks):
            raise InvalidRequestError("Use at most four bounded collection tasks")
        pending, next_index, stop = {}, 0, None
        try:
            while pending or next_index < len(tasks):
                scope.check()
                while next_index < len(tasks) and len(pending) < max_workers:
                    future = self._submit(tasks[next_index], scope)
                    if future is None:
                        break
                    pending[future] = next_index
                    next_index += 1
                timeout = min(POLL_SECONDS, scope.remaining())
                if not pending:
                    Event().wait(timeout)
                    continue
                done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=pending.get):
                    scope.accept(pending.pop(future), future.result())
        except CollectionUnavailable as error:
            if error.code != "research_data_deadline_exceeded":
                raise
        except TurnStopped as error:
            stop = error.code
            raise
        finally:
            scope.close(stop)
            for future in pending:
                future.cancel()
        return scope.results(len(tasks))

    def close(self, *, wait=False):
        """Process/test lifecycle only; never called by a timed-out collection."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)


_DEFAULT = None
_DEFAULT_LOCK = Lock()


def shared_collection_executor():
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = CollectionExecutor()
        return _DEFAULT
