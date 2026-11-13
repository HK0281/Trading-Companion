"""Offline boundary checks for the explicit review daily-price transport."""

from datetime import date, datetime, timedelta, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from trading_companion.research_review_prices import (
    HOST, MAX_RESPONSE_BYTES, PATH, ReviewPriceError, read_tencent_review_days,
)


def row(day, close="10.5"):
    return [day, "10", close, "11", "9", "12", "0", "0", "3"]


def payload(rows, symbol="sz000001", **extra):
    return json.dumps({"code": 0, "data": {symbol: {"day": rows, **extra}}}).encode()


class FakeClock:
    value = 0.0

    def __call__(self):
        return self.value


class FakeResponse:
    def __init__(self, body=None, *, status=200, headers=None, step=None, error=None, close_error=False):
        self.body = payload([row("2026-01-01"), row("2026-01-02")]) if body is None else body
        self.status, self.headers = status, headers or {}
        self.step, self.error, self.close_error = step, error, close_error
        self.offset, self.reads, self.closed = 0, 0, False

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        self.reads += 1
        if self.step:
            self.step("read")
        if self.error:
            raise self.error
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def close(self):
        self.closed = True
        if self.step:
            self.step("response_close")
        if self.close_error:
            raise OSError("private close detail")


class FakeConnection:
    def __init__(self, response, *, step=None, error_at=None):
        self.response, self.step, self.error_at = response, step, error_at
        self.sock = self
        self.timeouts, self.requests = [], []
        self.closed, self.connected = False, False

    def _stage(self, stage):
        if self.step:
            self.step(stage)
        if stage == self.error_at:
            raise OSError("private transport detail")

    def connect(self):
        self._stage("connect")
        self.connected = True

    def settimeout(self, value):
        self.timeouts.append(value)

    def request(self, method, path, *, headers):
        self._stage("request")
        self.requests.append((method, path, headers))

    def getresponse(self):
        self._stage("getresponse")
        return self.response

    def close(self):
        self.closed = True
        if self.step:
            self.step("connection_close")


class Factory:
    def __init__(self, responses=None, *, step=None, error_at=None):
        self.responses = [FakeResponse()] if responses is None else responses
        self.step, self.error_at = step, error_at
        self.calls, self.connections = [], []

    def __call__(self, host, *, timeout):
        self.calls.append((host, timeout))
        if self.step:
            self.step("factory")
        if self.error_at == "factory":
            raise OSError("private factory detail")
        connection = FakeConnection(self.responses[len(self.connections)],
                                    step=self.step, error_at=self.error_at)
        self.connections.append(connection)
        return connection


class ResearchReviewPriceTests(unittest.TestCase):
    def read(self, factory, **overrides):
        arguments = dict(start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
                         now=datetime(2026, 1, 4, tzinfo=timezone.utc), deadline=8.0,
                         clock=FakeClock(), connection_factory=factory)
        arguments.update(overrides)
        return read_tencent_review_days(arguments.pop("instrument_id", "000001.SZ"), **arguments)

    def assert_error(self, code, factory, **overrides):
        with self.assertRaises(ReviewPriceError) as caught:
            self.read(factory, **overrides)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_fixed_origin_query_headers_and_same_daily_close_mapping(self):
        response = FakeResponse(headers={"Content-Encoding": "identity"})
        factory = Factory([response])
        self.assertEqual(self.read(factory), [
            {"date": "2026-01-01", "close": 10.5}, {"date": "2026-01-02", "close": 10.5},
        ])
        self.assertEqual(factory.calls, [(HOST, 8.0)])
        connection = factory.connections[0]
        method, path, headers = connection.requests[0]
        parsed = urlsplit(path)
        self.assertEqual((method, parsed.path), ("GET", PATH))
        self.assertEqual(parse_qs(parsed.query), {"param": ["sz000001,day,2026-01-01,2026-01-02,640,"]})
        self.assertEqual(headers, {"Accept": "application/json", "Accept-Encoding": "identity"})
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)

    def test_cross_year_is_exactly_two_actual_slices_and_filters_ignored_lower_bound(self):
        responses = [FakeResponse(payload([row("2025-12-29"), row("2025-12-31")])),
                     FakeResponse(payload([row("2025-12-31"), row("2026-01-02")]))]
        factory = Factory(responses)
        self.assertEqual(self.read(factory, start_date=date(2025, 12, 30)), [
            {"date": "2025-12-31", "close": 10.5}, {"date": "2026-01-02", "close": 10.5},
        ])
        parameters = [parse_qs(urlsplit(c.requests[0][1]).query)["param"][0] for c in factory.connections]
        self.assertEqual(parameters, ["sz000001,day,2025-12-30,2025-12-31,640,",
                                      "sz000001,day,2026-01-01,2026-01-02,640,"])
        self.assertTrue(all(r.closed for r in responses))
        self.assertTrue(all(c.closed for c in factory.connections))

    def test_maximum_366_calendar_days_is_accepted_without_filling_gaps(self):
        factory = Factory([FakeResponse(payload([row("2025-01-02")])),
                           FakeResponse(payload([row("2026-01-01")]))])
        self.assertEqual(self.read(factory, start_date=date(2025, 1, 1), end_date=date(2026, 1, 1)), [
            {"date": "2025-01-02", "close": 10.5}, {"date": "2026-01-01", "close": 10.5},
        ])
        self.assertEqual(len(factory.calls), 2)

    def test_empty_daily_rows_are_not_replaced_by_adjusted_rows(self):
        factory = Factory([FakeResponse(payload([], qfqday=[row("2026-01-01")]))])
        self.assertEqual(self.read(factory), [])

    def test_unsupported_instruments_make_zero_attempts(self):
        for instrument in ("000001.SH", "510300.SH", "899050.BJ", "000001", "sz000001", "600000.SH?x=1"):
            with self.subTest(instrument=instrument):
                factory = Factory()
                self.assert_error("review_prices_instrument_unsupported", factory, instrument_id=instrument)
                self.assertEqual(factory.calls, [])

    def test_invalid_dates_times_and_oversized_windows_make_zero_attempts(self):
        invalid = [dict(start_date=date(2026, 1, 3)), dict(start_date="2026-01-01"),
                   dict(end_date=datetime(2026, 1, 2)), dict(start_date=date(2024, 12, 31)),
                   dict(now=datetime(2026, 1, 4)), dict(deadline=True), dict(deadline=float("nan")),
                   dict(deadline=float("inf")), dict(cancelled=None), dict(clock=None)]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                factory = Factory()
                self.assert_error("review_prices_invalid_request", factory, **arguments)
                self.assertEqual(factory.calls, [])

    def test_today_is_rejected_in_shanghai_even_when_utc_is_previous_day(self):
        factory = Factory()
        self.assert_error("review_prices_invalid_request", factory,
                          now=datetime(2026, 1, 1, 16, tzinfo=timezone.utc))
        self.assertEqual(factory.calls, [])

    def test_expired_deadline_or_initial_cancellation_make_zero_attempts(self):
        for arguments, code in [(dict(deadline=0), "review_prices_timeout"),
                                (dict(cancelled=lambda: True), "review_prices_cancelled")]:
            with self.subTest(code=code):
                factory = Factory()
                self.assert_error(code, factory, **arguments)
                self.assertEqual(factory.calls, [])

    def test_deadline_is_capped_at_eight_seconds_even_if_caller_expands_it(self):
        factory = Factory()
        self.read(factory, deadline=100)
        self.assertEqual(factory.calls, [(HOST, 8.0)])

    def test_failure_on_first_slice_is_not_retried_or_followed_by_second_slice(self):
        response = FakeResponse(status=500)
        factory = Factory([response, FakeResponse()])
        self.assert_error("review_prices_transport", factory, start_date=date(2025, 12, 30))
        self.assertEqual(len(factory.calls), 1)
        self.assertTrue(response.closed)
        self.assertTrue(factory.connections[0].closed)

    def test_second_slice_failure_does_not_return_partial_prices_or_retry(self):
        responses = [FakeResponse(payload([row("2025-12-31")])), FakeResponse(status=503)]
        factory = Factory(responses)
        self.assert_error("review_prices_transport", factory, start_date=date(2025, 12, 30))
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(all(r.closed for r in responses))
        self.assertTrue(all(c.closed for c in factory.connections))

    def test_redirects_and_compressed_responses_are_rejected_without_body_read(self):
        responses = [FakeResponse(status=code, headers={"Location": "https://other.invalid/private"})
                     for code in (301, 302, 307, 308)]
        responses += [FakeResponse(headers={"Content-Encoding": "gzip"})]
        for response in responses:
            with self.subTest(status=response.status, headers=response.headers):
                factory = Factory([response])
                self.assert_error("review_prices_transport", factory)
                self.assertEqual(response.reads, 0)
                self.assertEqual(len(factory.calls), 1)
                self.assertTrue(response.closed)
                self.assertTrue(factory.connections[0].closed)

    def test_declared_length_has_ascii_integer_and_size_bounds_before_read(self):
        for length in (str(MAX_RESPONSE_BYTES + 1), "-1", "1.5", " 20", "20,20", "１２", "00000000"):
            with self.subTest(length=length):
                response = FakeResponse(headers={"Content-Length": length})
                factory = Factory([response])
                self.assert_error("review_prices_limit", factory)
                self.assertEqual(response.reads, 0)
                self.assertTrue(response.closed)
                self.assertTrue(factory.connections[0].closed)

    def test_actual_bytes_are_bounded_even_without_header_or_with_false_small_header(self):
        for headers in ({}, {"Content-Length": "5"}):
            with self.subTest(headers=headers):
                response = FakeResponse(b" " * (MAX_RESPONSE_BYTES + 100), headers=headers)
                factory = Factory([response])
                self.assert_error("review_prices_limit", factory)
                self.assertEqual(response.offset, MAX_RESPONSE_BYTES + 1)
                self.assertTrue(response.closed)
                self.assertTrue(factory.connections[0].closed)

    def test_exact_byte_limit_is_accepted_and_declared_truncation_is_rejected(self):
        body = payload([])
        padded = body + b" " * (MAX_RESPONSE_BYTES - len(body))
        factory = Factory([FakeResponse(padded, headers={"Content-Length": str(MAX_RESPONSE_BYTES)})])
        self.assertEqual(self.read(factory), [])
        response = FakeResponse(body, headers={"Content-Length": str(len(body) + 1)})
        self.assert_error("review_prices_invalid_data", Factory([response]))
        self.assertTrue(response.closed)

    def test_payload_identity_code_and_explicit_unadjusted_day_are_required(self):
        bodies = [b"not JSON", b"[]", b'{"code":false,"data":{}}',
                  b'{"code":1,"data":{}}', payload([], symbol="sh600000"),
                  b'{"code":0,"data":{"sz000001":{"qfqday":[]}}}',
                  b'{"code":0,"data":{"sz000001":{"day":null}}}']
        for body in bodies:
            with self.subTest(body=body):
                response = FakeResponse(body)
                factory = Factory([response])
                self.assert_error("review_prices_invalid_data", factory)
                self.assertEqual(len(factory.calls), 1)
                self.assertTrue(response.closed)
                self.assertTrue(factory.connections[0].closed)

    def test_duplicate_reverse_future_and_noncanonical_dates_reject_whole_slice(self):
        rows = [[row("2026-01-01"), row("2026-01-01")],
                [row("2026-01-02"), row("2026-01-01")], [row("2026-01-03")],
                [row("20260101")], [row("2026-02-30")], [row("2026-W01-4")]]
        for records in rows:
            with self.subTest(records=records):
                self.assert_error("review_prices_invalid_data", Factory([FakeResponse(payload(records))]))

    def test_invalid_ohlc_volume_or_amount_reject_even_rows_before_lower_bound(self):
        invalid = []
        for index, value in ((1, True), (2, "NaN"), (2, "0"), (2, "Infinity"),
                             (3, "8"), (4, "12"), (5, "-1"), (5, "0.001"), (8, "-1")):
            record = row("2025-12-31")
            record[index] = value
            invalid.append(record)
        for record in invalid:
            with self.subTest(record=record):
                factory = Factory([FakeResponse(payload([record, row("2026-01-01")]))])
                self.assert_error("review_prices_invalid_data", factory)

    def test_excess_vendor_row_count_rejected_even_when_all_are_outside_window(self):
        factory = Factory([FakeResponse(payload([row("2025-01-01")] * 641))])
        self.assert_error("review_prices_limit", factory)

    def test_supported_shanghai_and_star_identifiers_use_their_exact_symbol(self):
        for instrument, symbol in (("600000.SH", "sh600000"), ("688001.SH", "sh688001")):
            with self.subTest(instrument=instrument):
                factory = Factory([FakeResponse(payload([row("2026-01-01")], symbol=symbol))])
                self.assertEqual(len(self.read(factory, instrument_id=instrument)), 1)
                query = parse_qs(urlsplit(factory.connections[0].requests[0][1]).query)
                self.assertTrue(query["param"][0].startswith(symbol + ",day,"))

    def test_cancel_after_connect_or_headers_or_body_closes_every_acquired_resource(self):
        for stage in ("connect", "getresponse", "read"):
            with self.subTest(stage=stage):
                stopped = [False]
                def step(event):
                    if event == stage:
                        stopped[0] = True
                response = FakeResponse(step=step)
                factory = Factory([response], step=step)
                self.assert_error("review_prices_cancelled", factory, cancelled=lambda: stopped[0])
                self.assertTrue(factory.connections[0].closed)
                self.assertEqual(response.closed, stage != "connect")

    def test_cancel_after_first_slice_cleanup_prevents_second_attempt(self):
        stopped = [False]
        response = FakeResponse(payload([row("2025-12-31")]),
                                step=lambda event: stopped.__setitem__(0, True) if event == "response_close" else None)
        factory = Factory([response, FakeResponse()])
        self.assert_error("review_prices_cancelled", factory, start_date=date(2025, 12, 30),
                          cancelled=lambda: stopped[0])
        self.assertEqual(len(factory.calls), 1)
        self.assertTrue(response.closed)
        self.assertTrue(factory.connections[0].closed)

    def test_remaining_timeout_decreases_before_each_blocking_step(self):
        clock = FakeClock()
        def step(event):
            if event in ("factory", "connect", "request", "getresponse", "read"):
                clock.value += 0.5
        response = FakeResponse(step=step)
        factory = Factory([response], step=step)
        self.read(factory, clock=clock)
        connection = factory.connections[0]
        self.assertEqual(connection.timeout, 7.5)
        self.assertEqual(connection.timeouts, [7.0, 6.5, 6.0, 5.5])

    def test_deadline_after_blocking_connect_or_read_does_not_publish_data(self):
        for stage in ("connect", "read"):
            with self.subTest(stage=stage):
                clock = FakeClock()
                def step(event):
                    if event == stage:
                        clock.value = 8
                response = FakeResponse(step=step)
                factory = Factory([response], step=step)
                self.assert_error("review_prices_timeout", factory, clock=clock)
                self.assertTrue(factory.connections[0].closed)
                self.assertEqual(response.closed, stage != "connect")

    def test_timeout_errors_have_fixed_codes_and_close_response(self):
        response = FakeResponse(error=TimeoutError("private timeout detail"))
        factory = Factory([response])
        self.assert_error("review_prices_timeout", factory)
        self.assertTrue(response.closed)
        self.assertTrue(factory.connections[0].closed)

    def test_connection_failures_are_not_retried_and_close_any_acquired_connection(self):
        for stage in ("factory", "connect", "request", "getresponse"):
            with self.subTest(stage=stage):
                factory = Factory(error_at=stage)
                self.assert_error("review_prices_transport", factory)
                self.assertEqual(len(factory.calls), 1)
                self.assertTrue(all(connection.closed for connection in factory.connections))

    def test_response_close_error_still_closes_connection(self):
        response = FakeResponse(close_error=True)
        factory = Factory([response])
        self.assert_error("review_prices_transport", factory)
        self.assertTrue(response.closed)
        self.assertTrue(factory.connections[0].closed)


if __name__ == "__main__":
    unittest.main()
