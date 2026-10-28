"""List-source guards using injected transports; no live requests in the test suite."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.screening import ScreeningCondition, ScreeningRequest, ScreeningService
from trading_companion.screening_source import (
    ENDPOINT, MARKET_FILTERS, MAX_RESPONSE_BYTES, TENCENT_ENDPOINT,
    EastmoneyQuoteSnapshotSource, TencentQuoteSnapshotSource,
)
from trading_companion.security_directory import Security, SecurityDirectory


NOW = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
IDENTITIES = {
    "sse_main": ("600010.SH", "*ST人工样例"), "szse_main": ("000001.SZ", "平安银行"),
    "star": ("688981.SH", "中芯国际"), "chinext": ("300001.SZ", "N新股人工样例"),
    "bse": ("920000.BJ", "安徽凤凰"),
}


def directory():
    return SecurityDirectory(tuple(Security(*item) for item in IDENTITIES.values()), "trusted_test_directory", NOW, False)


def quote(stratum):
    instrument, name = IDENTITIES[stratum]
    return {"f12": instrument[:6], "f13": 1 if instrument.endswith(".SH") else 0, "f14": name,
            "f124": int(NOW.timestamp()), "f2": "12.30", "f3": -1.2, "f6": "1000000",
            "f8": 2.5, "f9": -8.0, "f23": 0.9, "f20": 500000000, "f21": 400000000}


def response_payload(stratum):
    return {"rc": 0, "data": {"total": 1, "diff": [quote(stratum)]}}


class Response:
    def __init__(self, payload=None, *, body=None, status=200, headers=None):
        self.body = json.dumps(payload, ensure_ascii=False).encode() if body is None else body
        self.status_code, self.headers, self.closed = status, headers or {}, False

    def iter_content(self, _size):
        for start in range(0, len(self.body), 8192):
            yield self.body[start:start + 8192]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, mutate=None):
        self.calls, self.responses, self.mutate = [], [], mutate

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        stratum = next(key for key, value in MARKET_FILTERS.items() if value == kwargs["params"]["fs"])
        value = response_payload(stratum)
        response = self.mutate(value, stratum, kwargs["params"]) if self.mutate else Response(value)
        self.responses.append(response)
        return response


def source(session=None, **kwargs):
    return EastmoneyQuoteSnapshotSource(session=session or Session(), clock=lambda: NOW, **kwargs)


def test_stratified_sample_is_verified_dated_bounded_and_integrates_with_pure_service():
    session = Session()
    snapshot = source(session).read_snapshot(directory())
    assert len(session.calls) == 5 and len(snapshot.quotes) == 5
    assert all(item.closed for item in session.responses)
    assert {item.stratum for item in snapshot.strata} == set(MARKET_FILTERS)
    for method, endpoint, kwargs in session.calls:
        assert method == "GET" and endpoint == ENDPOINT
        assert kwargs["params"]["pn"] == "1" and kwargs["params"]["pz"] == "100"
        assert kwargs["params"]["fid"] == "f12" and kwargs["params"]["po"] == "0"
        assert kwargs["stream"] is True and kwargs["allow_redirects"] is False
        assert 0 < kwargs["timeout"][0] <= 3 and 0 < kwargs["timeout"][1] <= 6
    result = ScreeningService(clock=lambda: NOW).screen(snapshot, ScreeningRequest((ScreeningCondition("price", "gt", 0),)))
    payload = result.to_dict()
    assert payload["source"]["data_authenticity"] == "vendor_snapshot"
    assert payload["coverage"]["scanned_records"] == 5
    assert payload["coverage"]["independent_full_market_completeness_proven"] is False
    assert payload["rows"][0]["values"]["pe_dynamic"] == -8


def test_unverified_identity_is_excluded_with_explicit_coverage_not_failed_condition():
    trusted = directory()
    trusted = replace(trusted, securities=tuple(item for item in trusted.securities if not item.instrument_id.endswith(".BJ")))
    snapshot = source().read_snapshot(trusted)
    assert len(snapshot.quotes) == 4
    bse = next(item.to_dict() for item in snapshot.strata if item.stratum == "bse")
    assert bse["scanned_records"] == 1 and bse["identity_verified_records"] == 0
    assert bse["unverified_records"] == 1
    result = ScreeningService(clock=lambda: NOW).screen(snapshot, ScreeningRequest((ScreeningCondition("price", "gt", 0),)))
    assert result.coverage["unverified_records"] == 1
    assert result.condition_summary[0]["failed"] == 0


def test_missing_numbers_remain_null_zero_price_is_not_a_real_trade_and_zero_amount_is_real():
    def mutate(payload, _stratum, _params):
        payload["data"]["diff"][0].update({"f2": 0, "f3": "-", "f6": 0, "f8": None, "f9": "--", "f23": ""})
        return Response(payload)
    snapshot = source(Session(mutate)).read_snapshot(directory())
    for item in snapshot.quotes:
        assert item.price is None and item.change_percent is None and item.pe_dynamic is None and item.pb is None
        assert item.amount_cny == 0 and item.turnover_percent is None


def test_current_directory_name_is_used_after_unicode_and_space_equivalence_check():
    def mutate(payload, stratum, _params):
        if stratum == "szse_main":
            payload["data"]["diff"][0]["f14"] = "平 安银行"
        return Response(payload)
    snapshot = source(Session(mutate)).read_snapshot(directory())
    assert next(item for item in snapshot.quotes if item.instrument_id == "000001.SZ").name == "平安银行"


def test_page_cap_is_explicit_partial_coverage_not_full_market():
    def mutate(payload, _stratum, _params):
        payload["data"]["total"] = 500
        return Response(payload)
    snapshot = source(Session(mutate), page_size=1).read_snapshot(directory())
    assert sum(item.vendor_total for item in snapshot.strata) == 2500
    assert all(not item.to_dict()["vendor_listing_exhausted"] for item in snapshot.strata)
    assert len(snapshot.quotes) == 5


def test_two_pages_are_combined_once_without_fanout_and_limit_is_reported():
    def mutate(payload, stratum, params):
        if stratum == "sse_main":
            payload["data"]["total"] = 2
            if params["pn"] == "2":
                payload["data"]["diff"][0].update({"f12": "600011", "f14": "华能国际"})
        return Response(payload)
    trusted = directory()
    trusted = replace(trusted, securities=(*trusted.securities, Security("600011.SH", "华能国际")))
    session = Session(mutate)
    snapshot = source(session, page_size=1, pages_per_stratum=2).read_snapshot(trusted)
    assert len(session.calls) == 6
    assert len(snapshot.quotes) == 6
    assert snapshot.strata[0].pages_read == 2


@pytest.mark.parametrize("changes", [
    {"f12": 1}, {"f12": "00001"}, {"f13": True}, {"f13": 7}, {"f13": 0},
    {"f14": ""}, {"f14": "wrong name"}, {"f124": 0}, {"f124": "1793030400"},
    {"f124": int(NOW.timestamp()) + 1}, {"f6": -1}, {"f8": True}, {"f2": "12 yuan"},
    {"f2": float("nan")}, {"f2": "1e500"},
])
def test_bad_identity_dates_names_or_numeric_rows_fail_closed(changes):
    def mutate(payload, _stratum, _params):
        payload["data"]["diff"][0].update(changes)
        return Response(payload)
    session = Session(mutate)
    with pytest.raises(DataValidationError):
        source(session).read_snapshot(directory())
    assert len(session.calls) == 1
    assert session.responses[0].closed


@pytest.mark.parametrize("payload", [
    [], {}, {"rc": False, "data": {}}, {"rc": 1, "data": None},
    {"rc": 0, "data": {"total": True, "diff": []}},
    {"rc": 0, "data": {"total": 10001, "diff": []}},
    {"rc": 0, "data": {"total": 1, "diff": []}},
    {"rc": 0, "data": {"total": 0, "diff": [quote("sse_main")]}},
    {"rc": 0, "data": {"total": 1, "diff": {"0": quote("sse_main")}}},
])
def test_bad_page_and_declared_count_do_not_become_successful_empty_results(payload):
    with pytest.raises(DataValidationError):
        source(Session(lambda *_: Response(payload))).read_snapshot(directory())


@pytest.mark.parametrize("body", [b'{"rc":0,"rc":0}', b'{"rc":NaN}', b"not JSON", b"\xff"])
def test_duplicate_json_keys_nonfinite_json_and_invalid_encoding_are_rejected(body):
    with pytest.raises(DataValidationError):
        source(Session(lambda *_: Response(body=body))).read_snapshot(directory())


@pytest.mark.parametrize("kind", ["duplicate", "total_changed", "out_of_order", "wrong_stratum"])
def test_cross_page_mutations_or_scope_escape_are_rejected(kind):
    def mutate(payload, _stratum, params):
        payload["data"]["total"] = 2
        if kind == "wrong_stratum":
            payload["data"]["diff"][0] = quote("star")
        if params["pn"] == "2":
            if kind == "total_changed":
                payload["data"]["total"] = 3
            elif kind == "out_of_order":
                payload["data"]["diff"][0]["f12"] = "600009"
        return Response(payload)
    with pytest.raises(DataValidationError):
        source(Session(mutate), page_size=1, pages_per_stratum=2).read_snapshot(directory())


@pytest.mark.parametrize("status", [301, 302, 403, 429, 500])
def test_http_failure_is_explicit_never_retried_and_response_closed(status):
    session = Session(lambda *_: Response({}, status=status))
    with pytest.raises(DataSourceError):
        source(session).read_snapshot(directory())
    assert len(session.calls) == 1 and session.responses[0].closed


def test_stream_and_declared_size_limits_close_response():
    for response in (Response(body=b" " * (MAX_RESPONSE_BYTES + 1)),
                     Response({}, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
                     Response({}, headers={"Content-Length": "not-an-integer"})):
        with pytest.raises(DataValidationError):
            source(Session(lambda *_: response)).read_snapshot(directory())
        assert response.closed


def test_deadline_is_checked_during_body_read(monkeypatch):
    ticks = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr("trading_companion.screening_source.monotonic", lambda: next(ticks, 31.0))
    session = Session()
    with pytest.raises(DataSourceError, match="deadline"):
        source(session).read_snapshot(directory())
    assert len(session.calls) == 1 and session.responses[0].closed


def test_connection_exception_is_sanitized_and_not_retried():
    class Broken:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("secret host credentials")
    session = Broken()
    with pytest.raises(DataSourceError) as error:
        source(session).read_snapshot(directory())
    assert "secret" not in str(error.value)
    assert session.calls == 1


def test_directory_fixture_staleness_and_future_are_rejected_before_network():
    for trusted in (replace(directory(), is_fixture=True), replace(directory(), retrieved_at=NOW - timedelta(days=8)),
                    replace(directory(), retrieved_at=NOW + timedelta(days=1))):
        session = Session()
        with pytest.raises(DataValidationError):
            source(session).read_snapshot(trusted)
        assert session.calls == []


@pytest.mark.parametrize("options", [
    {"page_size": 101}, {"pages_per_stratum": 3}, {"page_size": True}, {"timeout_seconds": 11},
    {"deadline_seconds": 31}, {"timeout_seconds": float("nan")}, {"clock": "not callable"},
])
def test_configuration_cannot_expand_budgets(options):
    with pytest.raises(InvalidRequestError):
        EastmoneyQuoteSnapshotSource(**options)


def tencent_line(security):
    fields = [""] * 88
    fields[0], fields[1], fields[2] = "1", security.name, security.instrument_id[:6]
    for position, value in {3: "11.30", 30: "20261027000000", 32: "0.89",
        35: "11.30/1323113/1485999504", 38: "0.68", 39: "5.04", 44: "2192.84",
        45: "2192.87", 46: "0.46", 52: "4.20", 53: "5.14", 57: "148599.9504", 82: "CNY"}.items():
        fields[position] = value
    symbol = security.instrument_id[-2:].lower() + security.instrument_id[:6]
    return symbol, fields


class TencentSession:
    def __init__(self, trusted=None, mutate=None):
        self.calls, self.responses, self.mutate = [], [], mutate
        self.trusted = {item.instrument_id[-2:].lower() + item.instrument_id[:6]: item
                        for item in (trusted or directory()).securities}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        symbols = url.removeprefix(TENCENT_ENDPOINT).split(",")
        lines = []
        for symbol in symbols:
            key, fields = tencent_line(self.trusted[symbol])
            lines.append((key, fields))
        if self.mutate:
            lines = self.mutate(lines)
        body = "\n".join('v_' + key + '="' + "~".join(fields) + '";' for key, fields in lines)
        response = Response(body=body.encode("gb18030"))
        self.responses.append(response)
        return response


def tencent_source(session=None, **kwargs):
    return TencentQuoteSnapshotSource(session=session or TencentSession(), clock=lambda: NOW, **kwargs)


def test_tencent_uses_validated_dynamic_pe_exact_amount_units_and_stratified_directory_coverage():
    session = TencentSession()
    snapshot = tencent_source(session).read_snapshot(directory())
    assert len(snapshot.quotes) == 5 and len(session.calls) == 5
    for quote in snapshot.quotes:
        assert quote.price == 11.3 and quote.change_percent == 0.89
        assert quote.pe_dynamic == 4.2  # Index 39 is 5.04 and must not be substituted.
        assert quote.pb == 0.46 and quote.turnover_percent == 0.68
        assert quote.amount_cny == 1485999504
        assert quote.market_cap_cny == 219287000000 and quote.float_market_cap_cny == 219284000000
        assert quote.quoted_at.isoformat() == "2026-10-27T00:00:00+08:00"
    for method, url, kwargs in session.calls:
        assert method == "GET" and url.startswith(TENCENT_ENDPOINT)
        assert kwargs["stream"] is True and kwargs["allow_redirects"] is False
        assert len(url.removeprefix(TENCENT_ENDPOINT).split(",")) <= 80
    result = ScreeningService(clock=lambda: NOW).screen(snapshot, ScreeningRequest((ScreeningCondition("price", "gt", 0),)))
    assert result.coverage["vendor_declared_records"] is None
    assert result.coverage["directory_universe_records"] == 5
    assert result.source["sampling_policy"] == "configured_directory_fixed_market_strata_code_ascending"
    assert all(item["vendor_listing_exhausted"] is None for item in result.coverage["strata"])
    assert all(response.closed for response in session.responses)


def test_tencent_deterministic_sample_and_batches_are_bounded_even_for_large_directory():
    groups = (("600", "SH"), ("000", "SZ"), ("688", "SH"), ("300", "SZ"), ("920", "BJ"))
    identities = tuple(Security(f"{prefix}{index:03d}.{exchange}", f"人工样例{prefix}{index}")
                       for prefix, exchange in groups for index in range(120))
    trusted = SecurityDirectory(tuple(reversed(identities)), "synthetic_transport_test", NOW, False)
    session = TencentSession(trusted)
    snapshot = tencent_source(session, per_stratum=100).read_snapshot(trusted)
    assert len(snapshot.quotes) == 500 and len(session.calls) == 10
    assert all(len(url.removeprefix(TENCENT_ENDPOINT).split(",")) <= 80 for _, url, _ in session.calls)
    assert all(item.scanned_records == 100 and item.vendor_total == 120 for item in snapshot.strata)
    assert all(not item.to_dict()["directory_stratum_exhausted"] for item in snapshot.strata)
    assert all(quote.instrument_id[:6][-3:] < "100" for quote in snapshot.quotes)


def test_tencent_omitted_quote_is_missing_not_identity_failure_or_condition_failure():
    identities = (*directory().securities, Security("600011.SH", "华能国际"))
    trusted = replace(directory(), securities=identities)
    session = TencentSession(trusted, mutate=lambda lines: lines[:-1] if len(lines) == 2 else lines)
    snapshot = tencent_source(session).read_snapshot(trusted)
    result = ScreeningService(clock=lambda: NOW).screen(snapshot, ScreeningRequest((ScreeningCondition("price", "gt", 0),)))
    assert len(snapshot.quotes) == 5
    assert result.coverage["scanned_records"] == 6
    assert result.coverage["missing_quote_records"] == 1 and result.coverage["unverified_records"] == 0
    assert result.condition_summary[0]["failed"] == 0


def test_tencent_explicit_no_match_marker_is_unavailable_quote_not_empty_condition_result():
    class NoMatch:
        def request(self, *args, **kwargs):
            return Response(body=b'v_pv_none_match="1";\n')
    snapshot = tencent_source(NoMatch()).read_snapshot(directory())
    result = ScreeningService(clock=lambda: NOW).screen(snapshot, ScreeningRequest((ScreeningCondition("price", "gt", 0),)))
    assert result.status == "unavailable" and result.coverage["missing_quote_records"] == 5


def test_tencent_star_u_qualifier_is_allowlisted_without_stripping_other_security_names():
    def mutate(lines):
        for symbol, fields in lines:
            if symbol.startswith("sh688"):
                fields[1] += "-U"
        return lines
    snapshot = tencent_source(TencentSession(mutate=mutate)).read_snapshot(directory())
    assert next(item for item in snapshot.quotes if item.instrument_id == "688981.SH").name == "中芯国际"
    def bad_qualifier(lines):
        lines[0][1][1] += "-U"
        return lines
    with pytest.raises(DataValidationError, match="name"):
        tencent_source(TencentSession(mutate=bad_qualifier)).read_snapshot(directory())


def test_tencent_missing_dynamic_pe_never_falls_back_to_ttm_or_static_values():
    def mutate(lines):
        lines[0][1][52] = ""
        lines[0][1][57] = ""
        return lines
    snapshot = tencent_source(TencentSession(mutate=mutate)).read_snapshot(directory())
    assert all(quote.pe_dynamic is None and quote.amount_cny is None for quote in snapshot.quotes)


@pytest.mark.parametrize("position, value", [
    (2, "999999"), (1, "another company"), (30, "15:00:00"), (30, "20260230000000"),
    (30, "20261028000000"), (82, "USD"), (3, "NaN"), (57, "123"), (35, "11.30/123/1"),
    (38, "-1"), (45, "-1"),
])
def test_tencent_dated_identity_currency_units_and_numeric_fields_are_checked(position, value):
    def mutate(lines):
        lines[0][1][position] = value
        return lines
    session = TencentSession(mutate=mutate)
    with pytest.raises(DataValidationError):
        tencent_source(session).read_snapshot(directory())
    assert len(session.calls) == 1 and session.responses[0].closed


@pytest.mark.parametrize("body", [
    b"", b"<html>not quotes</html>", b'v_sh600010="";alert(1);',
    b'v_sh999999="";', b'v_sh600010="";v_sh600010="";', b'v_pv_none_match="1";v_sh600010="";',
])
def test_tencent_assignment_protocol_cannot_execute_code_hide_invalid_rows_or_escape_scope(body):
    class Bad:
        def request(self, *args, **kwargs):
            return Response(body=body)
    with pytest.raises(DataValidationError):
        tencent_source(Bad()).read_snapshot(directory())


def test_tencent_source_failure_is_not_silently_changed_to_eastmoney():
    class Unavailable:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            return Response(body=b"blocked", status=429)
    session = Unavailable()
    with pytest.raises(DataSourceError):
        tencent_source(session).read_snapshot(directory())
    assert session.calls == 1


@pytest.mark.parametrize("options", [
    {"per_stratum": 101}, {"per_stratum": True}, {"batch_size": 81}, {"batch_size": 1},
    {"deadline_seconds": 31}, {"timeout_seconds": 11},
])
def test_tencent_configuration_preserves_total_sample_batch_and_deadline_budgets(options):
    with pytest.raises(InvalidRequestError):
        TencentQuoteSnapshotSource(**options)


@pytest.mark.parametrize("options", [
    {"max_workers": 0}, {"max_workers": 4}, {"max_workers": True}, {"max_workers": 1.5},
    {"session_factory": 1}, {"monotonic_clock": 1}, {"executor": object()},
    {"session": TencentSession(), "max_workers": 2}, {"session": TencentSession(), "max_workers": 3},
    {"session": TencentSession(), "session_factory": TencentSession},
])
def test_tencent_worker_client_configuration_rejected_before_any_request(options):
    with pytest.raises(InvalidRequestError):
        TencentQuoteSnapshotSource(**options)


def test_tencent_default_owns_independent_requests_sessions_and_keeps_borrowed_default_serial(monkeypatch):
    from tests.test_screening_collection_concurrency import QuoteTransport
    from trading_companion.collection_execution import CollectionExecutor
    http = QuoteTransport(directory())
    pool = CollectionExecutor()
    monkeypatch.setattr("requests.Session", http.factory)
    try:
        normal = TencentQuoteSnapshotSource(executor=pool, clock=lambda: NOW)
        assert normal.max_workers == 3
        assert len(normal.read_snapshot(directory()).quotes) == 5
        assert len(http.clients) == 5 and all(client.closed == 1 for client in http.clients)
        assert TencentQuoteSnapshotSource(session=TencentSession()).max_workers == 1
    finally:
        pool.close(wait=True)


@pytest.mark.parametrize("width", [1, 2, 3])
def test_eastmoney_factory_keeps_cross_page_reads_serial_for_every_worker_setting(monkeypatch, width):
    from trading_companion.screening_source import configured_quote_source
    session = Session()
    monkeypatch.setattr("requests.request", session.request)
    monkeypatch.setattr("trading_companion.screening_source.shared_collection_executor",
                        lambda: pytest.fail("Eastmoney must not dispatch quote workers"))
    selected = configured_quote_source("eastmoney", "expanded", max_workers=width)
    selected._clock = lambda: NOW
    snapshot = selected.read_snapshot(directory())
    assert [call[2]["params"]["fs"] for call in session.calls] == list(MARKET_FILTERS.values())
    assert len(snapshot.quotes) == 5 and all(response.closed for response in session.responses)
