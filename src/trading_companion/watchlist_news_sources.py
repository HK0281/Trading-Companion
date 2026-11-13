"""Serial public title checks with fixed endpoints and actual transport accounting."""

from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import Event, Lock
from time import monotonic

import requests as http

from trading_companion.collection_execution import (
    CollectionExecutor, CollectionUnavailable, POLL_SECONDS, shared_collection_executor,
)
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import ResearchDataRequest, SHANGHAI, aware
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import TurnStopped, checkpoint, request_timeout


SOURCES = ("sina_company_news", "eastmoney_announcements")
_ENDPOINTS = {
    SOURCES[0]: "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php",
    SOURCES[1]: "https://np-anotice-stock.eastmoney.com/api/security/ann",
}
_RESPONSE_BYTES = 512_000
_BATCH_BYTES = 5_120_000


class _Stopped(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SourceFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _ReadScope:
    """Adapter checks never count speculative sends or double-count body bytes."""

    def __init__(self, *, deadline, cancel, clock):
        self.deadline, self._cancel, self._clock = deadline, cancel, clock
        self.requests = self.response_bytes = 0
        self.request_deadline = None

    def check(self):
        if self._cancel():
            raise _Stopped("watchlist_news_cancelled")
        if self._clock() >= self.deadline:
            raise _Stopped("research_data_deadline_exceeded")
        if self.request_deadline is not None and self._clock() >= self.request_deadline:
            raise _SourceFailure("source_request_deadline_exceeded")
        checkpoint()

    def remaining(self):
        self.check()
        stop = self.deadline if self.request_deadline is None else min(self.deadline, self.request_deadline)
        return max(0.0, stop - self._clock())

    def timeout(self, per_request):
        return request_timeout(min(6.0, per_request, self.remaining()))

    def reserve_request(self):
        self.check()

    def consume(self, size):
        self.check()

    def start_request(self):
        self.check()
        if self.requests >= 10:
            raise _SourceFailure("research_request_budget_exhausted")
        self.requests += 1
        self.request_deadline = min(self.deadline, self._clock() + 6.0)

    def count_bytes(self, size):
        if self.response_bytes + size > _BATCH_BYTES:
            raise _SourceFailure("research_byte_budget_exhausted")
        self.response_bytes += size


class _Response:
    def __init__(self, response, scope):
        self._response, self._scope = response, scope
        self.status_code, self.headers = response.status_code, response.headers
        declared = self.headers.get("Content-Length")
        if declared is not None and (type(declared) is not str or not declared.isascii()
                or not declared.isdigit() or int(declared) > _RESPONSE_BYTES):
            raise _SourceFailure("source_response_too_large")

    def iter_content(self, chunk_size):
        count = 0
        chunks = iter(self._response.iter_content(min(8192, chunk_size)))
        while True:
            self._scope.check()
            try:
                chunk = next(chunks)
            except StopIteration:
                return
            if type(chunk) is not bytes:
                raise _SourceFailure("invalid_source_body")
            self._scope.count_bytes(len(chunk))
            self._scope.check()
            count += len(chunk)
            if count > _RESPONSE_BYTES:
                raise _SourceFailure("source_response_too_large")
            yield chunk

    def close(self):
        self._response.close()


class _Transport:
    """Only a planned pair can make one actual GET, with no retry or redirect."""

    def __init__(self, reader, client, request, source, scope, before_request=None):
        self._reader, self._client = reader, client
        self._request, self._source, self._scope = request, source, scope
        self._before_request = before_request
        self.attempted = False

    def request(self, method, url, **kwargs):
        request, source, scope = self._request, self._source, self._scope
        if source == SOURCES[0]:
            expected = {"symbol": request.instrument_id[-2:].lower() + request.instrument_id[:6], "Page": 1}
        else:
            expected = {"sr": "-1", "page_size": str(request.limit), "page_index": "1", "ann_type": "A",
                "client_source": "web", "stock_list": request.instrument_id[:6],
                "begin_time": request.publication_start.isoformat(),
                "end_time": request.as_of.astimezone(SHANGHAI).date().isoformat()}
        if (self.attempted or method != "GET" or url != _ENDPOINTS[source]
                or not request.instrument_id.endswith((".SH", ".SZ")) or kwargs.get("params") != expected
                or kwargs.get("data") is not None or kwargs.get("allow_redirects") is not False
                or kwargs.get("stream") is not True):
            raise _SourceFailure("watchlist_source_request_rejected")
        self._reader._pace(scope)
        scope.check()
        if self._before_request is not None:
            self._before_request(request.instrument_id, source)
        scope.check()
        timeout = scope.timeout(6.0)
        kwargs["timeout"] = (min(3.0, timeout), timeout)
        scope.start_request()
        self.attempted = True
        self._reader._last_started = self._reader._timer()
        response = self._client.request(method, url, **kwargs)
        try:
            scope.check()
            return _Response(response, scope)
        except BaseException:
            response.close()
            raise


class WatchlistNewsSourceReader:
    """One serial batch, preserving the last actual start time between batches."""

    def __init__(self, *, session_factory=None, executor=None, clock=None, monotonic_clock=None, wait=None):
        if (session_factory is not None and not callable(session_factory)
                or executor is not None and type(executor) is not CollectionExecutor
                or any(value is not None and not callable(value) for value in (clock, monotonic_clock, wait))):
            raise InvalidRequestError("Use bounded watchlist source dependencies")
        self._factory, self._executor = session_factory, executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timer, self._wait = monotonic_clock or monotonic, wait or Event().wait
        self._last_started, self._lock = None, Lock()

    def _now(self):
        value = self._clock()
        if not aware(value):
            raise InvalidRequestError("Watchlist source clock must be timezone-aware")
        return value

    def _pace(self, scope):
        while self._last_started is not None:
            scope.check()
            delay = 1.0 - (self._timer() - self._last_started)
            if delay <= 0:
                break
            self._wait(min(POLL_SECONDS, delay, scope.remaining()))
        scope.check()

    def read_watchlist_sources(self, requests, *, cancel, deadline, on_source, start_source=0, before_request=None):
        if (type(requests) is not tuple or not 1 <= len(requests) <= 5
                or len({request.instrument_id for request in requests if type(request) is ResearchDataRequest}) != len(requests)
                or any(type(request) is not ResearchDataRequest for request in requests)
                or type(deadline) not in (int, float) or not isfinite(deadline)
                or not callable(on_source) or type(start_source) is not int or start_source not in (0, 1)
                or before_request is not None and not callable(before_request)):
            raise InvalidRequestError("Use up to five distinct bounded watchlist requests")
        cancelled = cancel.is_set if callable(getattr(cancel, "is_set", None)) else cancel
        if not callable(cancelled):
            raise InvalidRequestError("Use a cooperative watchlist cancellation signal")
        now = self._now()
        for request in requests:
            if (request.as_of > now or request.limit != 5
                    or request.publication_start != request.as_of.astimezone(SHANGHAI).date() - timedelta(days=30)):
                raise InvalidRequestError("Use a thirty-day watchlist title window")
        scope = _ReadScope(deadline=min(deadline, self._timer() + 20.0), cancel=cancelled, clock=self._timer)
        completed, stopped, code = 0, False, None
        acquired = False
        try:
            while not acquired:
                scope.check()
                acquired = self._lock.acquire(timeout=min(POLL_SECONDS, scope.remaining()))
            for index, request in enumerate(requests):
                for source in SOURCES[start_source if index == 0 else 0:]:
                    scope.request_deadline = None
                    scope.check()
                    transport = None
                    filter_counts = {"filtered_count": 0}
                    try:
                        def read():
                            nonlocal transport
                            client = (self._factory or http.Session)()
                            try:
                                transport = _Transport(self, client, request, source, scope, before_request)
                                service = ResearchDataService(clock=self._clock, monotonic_clock=self._timer)
                                return service.read_watchlist_section(request, source, deadline=scope.deadline,
                                    scope=scope, session=transport, filter_counts=filter_counts)
                            finally:
                                client.close()
                        section = (self._executor or shared_collection_executor()).run_inline(read, scope=scope)
                        scope.check()
                        result = {"instrument_id": request.instrument_id, "source": source,
                            "status": section.status, "code": section.code, "skipped_count": section.skipped_count,
                            "filtered_count": filter_counts["filtered_count"],
                            "items": section.items, "checked_at": section.retrieved_at,
                            "attempted": transport is not None and transport.attempted}
                    except (_Stopped, _SourceFailure, CollectionUnavailable, TurnStopped) as error:
                        attempted = transport is not None and transport.attempted
                        is_stop = isinstance(error, (_Stopped, TurnStopped))
                        if is_stop and not attempted:
                            raise
                        result = {"instrument_id": request.instrument_id, "source": source,
                            "status": "unavailable", "code": error.code, "skipped_count": 0,
                            "filtered_count": 0,
                            "items": (), "checked_at": self._now(), "attempted": attempted}
                        if is_stop:
                            stopped, code = True, error.code
                    except (OSError, http.RequestException, TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
                        result = {"instrument_id": request.instrument_id, "source": source,
                            "status": "unavailable", "code": ("source_connection_failed"
                                if isinstance(error, (OSError, http.RequestException)) else "invalid_source_shape"),
                            "skipped_count": 0, "filtered_count": 0, "items": (), "checked_at": self._now(),
                            "attempted": transport is not None and transport.attempted}
                    completed += 1
                    on_source(result)
                    if stopped:
                        return {"attempts": scope.requests, "response_bytes": scope.response_bytes,
                            "completed_pairs": completed, "stopped": True, "code": code}
        except (_Stopped, TurnStopped) as error:
            stopped, code = True, error.code
        finally:
            if acquired:
                self._lock.release()
        return {"attempts": scope.requests, "response_bytes": scope.response_bytes,
            "completed_pairs": completed, "stopped": stopped, "code": code}
