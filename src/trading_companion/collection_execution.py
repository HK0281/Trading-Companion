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


class _CollectionScope:
    """Shared deadline, revocation and result lifecycle; budgets belong to each source."""

    def __init__(self, *, deadline, clock):
        if type(deadline) not in (int, float) or not isfinite(deadline) or not callable(clock):
            raise InvalidRequestError("Use a finite collection deadline and callable clock")
        self.deadline, self._clock = deadline, clock
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


class CollectionScope(_CollectionScope):
    """One to three company ledgers with atomic, non-refundable allowances."""

    max_tasks = 12

    def __init__(self, *, deadline, requests=None, response_bytes=None, company_allowances=None, clock=monotonic):
        if company_allowances is None:
            company_allowances = ((requests, response_bytes),)
        elif requests is not None or response_bytes is not None:
            raise InvalidRequestError("Use either one company or explicit company allowances")
        if (type(company_allowances) is not tuple or not 1 <= len(company_allowances) <= 3
                or any(type(item) is not tuple or len(item) != 2
                    or type(item[0]) is not int or not 0 <= item[0] <= 5
                    or type(item[1]) is not int or not 0 <= item[1] <= 4_048_000 for item in company_allowances)):
            raise InvalidRequestError("Use bounded collection allowances")
        super().__init__(deadline=deadline, clock=clock)
        self._allowances = company_allowances
        self._request_limit = sum(item[0] for item in company_allowances)
        self._byte_limit = sum(item[1] for item in company_allowances)
        self._company_usage = [[0, 0] for _ in company_allowances]

    def _charge(self, company_index, resource, size):
        self.check()
        with self._lock:
            self._check()
            used = self._company_usage[company_index]
            total = self._requests if resource == 0 else self._bytes
            limit = self._request_limit if resource == 0 else self._byte_limit
            if used[resource] + size > self._allowances[company_index][resource] or total + size > limit:
                raise CollectionUnavailable("research_request_budget_exhausted" if resource == 0
                                            else "research_byte_budget_exhausted")
            used[resource] += size
            if resource == 0:
                self._requests += size
            else:
                self._bytes += size

    def for_company(self, index):
        if type(index) is not int or not 0 <= index < len(self._allowances):
            raise InvalidRequestError("Choose one validated company budget")
        return _CompanyScope(self, index)

    def _single_company(self):
        if len(self._allowances) != 1:
            raise InvalidRequestError("Charge a batch through its company budget view")
        return self.for_company(0)

    def reserve_request(self):
        self._single_company().reserve_request()

    def consume(self, size):
        if type(size) is not int or size < 0:
            raise InvalidRequestError("Count actual nonnegative response bytes")
        self._single_company().consume(size)


class QuoteCollectionScope(_CollectionScope):
    """Fixed quote-batch plan: up to twenty requests sharing one 2 MB body ledger."""

    max_tasks = 20

    def __init__(self, *, deadline, requests, response_bytes=2_000_000, clock=monotonic):
        if (type(requests) is not int or not 0 <= requests <= 20
                or type(response_bytes) is not int or not 0 <= response_bytes <= 2_000_000):
            raise InvalidRequestError("Use bounded quote collection allowances")
        super().__init__(deadline=deadline, clock=clock)
        self._request_limit, self._byte_limit = requests, response_bytes

    def _charge(self, size, *, request=False):
        self.check()
        with self._lock:
            self._check()
            used, limit = ((self._requests, self._request_limit) if request else (self._bytes, self._byte_limit))
            if used + size > limit:
                raise CollectionUnavailable("research_request_budget_exhausted" if request
                                            else "research_byte_budget_exhausted")
            if request:
                self._requests += size
            else:
                self._bytes += size

    def reserve_request(self):
        self._charge(1, request=True)

    def consume(self, size):
        if type(size) is not int or size < 0:
            raise InvalidRequestError("Count actual nonnegative response bytes")
        self._charge(size)


class _CompanyScope:
    """A task can spend only its company's allowance; revocation/deadline are shared."""

    def __init__(self, scope, index):
        self._scope, self._index = scope, index
        self.deadline = scope.deadline

    def check(self):
        self._scope.check()

    def remaining(self):
        return self._scope.remaining()

    def timeout(self, per_request):
        return self._scope.timeout(per_request)

    def reserve_request(self):
        self._scope._charge(self._index, 0, 1)

    def consume(self, size):
        if type(size) is not int or size < 0:
            raise InvalidRequestError("Count actual nonnegative response bytes")
        self._scope._charge(self._index, 1, size)

    def usage(self):
        with self._scope._lock:
            requests, response_bytes = self._scope._company_usage[self._index]
            return {"requests": requests, "response_bytes": response_bytes}


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
        if (not isinstance(scope, _CollectionScope) or type(tasks) is not tuple
                or len(tasks) > min(20, scope.max_tasks) or any(not callable(task) for task in tasks)):
            raise InvalidRequestError("Use bounded collection tasks within the source-specific limit")
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
