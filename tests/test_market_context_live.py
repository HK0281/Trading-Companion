"""Offline exchange metadata fixtures; no credentials or live network are used."""

from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.exchange_directory import DIRECTORY_SOURCE, SSE_QUERY, SSE_URL, SZSE_URL
from trading_companion.market_context import (
    LIVE_SNAPSHOT_VERSION, build_market_context, current_security_background, load_market_context, parse_market_context_bytes,
)
from trading_companion.market_context_live import (
    ExchangeMarketContextSource, RISK_QUERIES, RISK_URL, SZSE_JSON_URL, _encode, main, parse_sse_metadata,
    parse_sse_risk_status, parse_szse_metadata, parse_szse_security_metadata, refresh_market_context, save_market_context,
)
from trading_companion.market_rules import explain_market_rules, rules_from_market_context
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.security_directory import Security, SecurityDirectory
from tests.test_product_research import MemorySourceFactory
from tests.test_security_directory_refresh import sse_body, sse_payload, szse_body


NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


def risk_body(query, rows):
    return json.dumps({"sqlId": query, "actionErrors": [], "fieldErrors": {}, "result": rows,
        "pageHelp": {"total": len(rows), "pageNo": 1, "pageCount": 1 if rows else 0}}).encode()


def szse_json_payload(code="000001", *, reported="2026-10-26"):
    return [{"metadata": {"catalogid": "1110", "tabkey": "tab1", "pageno": 1, "pagecount": 1,
                           "recordcount": 1, "subname": reported + " "},
        "data": [{"bk": "主板", "agdm": code, "agjc": "<a href='https://www.szse.cn/'><u>平安银行</u></a>",
                  "agssrq": "1991-04-03"}]}]


class Transport:
    def __init__(self, *, risk_failure=False, listing_failure=False):
        self.calls = []
        self.risk_failure, self.listing_failure = risk_failure, listing_failure

    def __call__(self, url, params, *, referer, timeout):
        self.calls.append((url, params, referer, timeout))
        if self.listing_failure:
            raise DataSourceError("Listing endpoint failed")
        if url == SSE_URL:
            stock_type = params["STOCK_TYPE"]
            payload = sse_payload(stock_type)
            payload["result"][0]["LIST_DATE"] = "19991110" if stock_type == "1" else "20261026"
            return sse_body(payload)
        if url == SZSE_URL:
            return szse_body([("000001", "平安银行", "主板", "1991-04-03"),
                              ("300001", "*ST人工", "创业板", "2009-10-30")],
                             header=("A股代码", "A股简称", "板块", "A股上市日期"))
        if url == SZSE_JSON_URL:
            return json.dumps(szse_json_payload(params["txtDMorJC"])).encode()
        assert url == RISK_URL
        if self.risk_failure:
            raise DataSourceError("Risk endpoint failed")
        query = params["sqlId"]
        return risk_body(query, [{"INSTRUMENT_ID": "600000", "INSTRUMENT_SHORT": "*ST人工"}]
                         if query == RISK_QUERIES[0][0] else
                         [{"secCode": "688981", "secNameCn": "ST人工"}])


@pytest.fixture(autouse=True)
def offline_guards():
    with patch("requests.get", side_effect=AssertionError("No live HTTP in metadata tests")), \
         patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credential reads")):
        yield


def snapshot(**kwargs):
    return ExchangeMarketContextSource(transport=Transport(**kwargs), clock=lambda: NOW).read_snapshot()


def test_three_listing_fragments_are_validated_and_risk_membership_is_positive_only():
    transport = Transport()
    source = ExchangeMarketContextSource(transport=transport, clock=lambda: NOW)
    value = source.read_snapshot()
    assert len(transport.calls) == 5
    by_id = {state.instrument_id: state for state in value.security_states}
    assert by_id["600000.SH"].st_status == "star_st"
    assert by_id["688981.SH"].st_status == "st"
    assert by_id["688981.SH"].board == "star"
    assert by_id["688981.SH"].listing_date == date(2026, 10, 26)
    assert by_id["300001.SZ"].board == "chinext"
    assert by_id["300001.SZ"].st_status == "unknown"
    assert by_id["000001.SZ"].st_status == "unknown"
    assert all(state.effective_from == state.effective_to == date(2026, 10, 26) for state in value.security_states)
    assert all(state.published_at is None and state.observed_at == NOW for state in value.security_states)
    assert value.calendars == ()
    assert "exchange_calendar_not_fetched" in source.last_unavailable
    assert all(item.publication_precision == "unknown" for item in value.source_records)


def test_optional_risk_failure_is_explicit_without_losing_listing_facts():
    source = ExchangeMarketContextSource(transport=Transport(risk_failure=True), clock=lambda: NOW)
    value = source.read_snapshot()
    assert len(value.security_states) == 4
    assert all(state.st_status == "unknown" for state in value.security_states)
    assert len(value.source_records) == 3
    assert "sse_main_risk_unavailable" in source.last_unavailable


def test_required_listing_failure_cannot_replace_an_existing_snapshot(tmp_path):
    path = tmp_path / "context.json"
    original = snapshot()
    save_market_context(original, path)
    original_bytes = path.read_bytes()
    with pytest.raises(DataSourceError):
        refresh_market_context(path, source=ExchangeMarketContextSource(transport=Transport(listing_failure=True)))
    assert path.read_bytes() == original_bytes


def test_v2_snapshot_roundtrips_and_cannot_overwrite_an_unrelated_file_or_fixture(tmp_path):
    value, path = snapshot(), tmp_path / "context.json"
    save_market_context(value, path)
    assert load_market_context(path) == value
    payload = json.loads(path.read_bytes())
    assert payload["version"] == LIVE_SNAPSHOT_VERSION
    assert payload["source_records"][0]["published_at"] is None
    assert not list(tmp_path.glob(".market-context-*"))
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"private":"unchanged"}')
    with pytest.raises(DataValidationError):
        save_market_context(value, unrelated)
    assert json.loads(unrelated.read_bytes()) == {"private": "unchanged"}
    fixture = Path(__file__).resolve().parents[1] / "data" / "market_context_fixture.json"
    with pytest.raises(InvalidRequestError):
        save_market_context(value, fixture)


def test_observation_is_never_extended_to_past_or_future_dates():
    value = snapshot()
    current = current_security_background(value, instrument_id="600000.SH", analysis_date=date(2026, 10, 26), reference_at=NOW)
    assert current["analysis_date_st_status"] == "star_st"
    previous = current_security_background(value, instrument_id="600000.SH", analysis_date=date(2026, 10, 23), reference_at=NOW)
    assert previous["observed_st_status"] == "star_st"
    assert previous["analysis_date_st_status"] == "unknown"
    assert previous["publication_precision"] == "unknown"
    assert previous["historical_availability"] == "not_established"
    assert previous["suspension_status"] == "unknown"
    assert current_security_background(value, instrument_id="600000.SH", analysis_date=date(2026, 10, 26), reference_at=NOW - timedelta(seconds=1))["status"] == "unknown"
    assert current_security_background(value, instrument_id="600000.SH", analysis_date=date(2026, 10, 26), reference_at=NOW + timedelta(days=8))["code"] == "current_background_stale"


@pytest.mark.parametrize("mutation", ["extend_state", "invent_publication", "wrong_host", "missing_reference", "future_observation", "empty_source_records"])
def test_live_provenance_rejects_unbound_or_invented_claims(mutation):
    value = snapshot()
    payload = json.loads(json.dumps({"version": LIVE_SNAPSHOT_VERSION, **asdict(value)}, default=_encode))
    if mutation == "extend_state":
        payload["security_states"][0]["effective_from"] = "2026-01-01"
    elif mutation == "invent_publication":
        payload["source_records"][0]["published_at"] = NOW.isoformat()
    elif mutation == "wrong_host":
        payload["source_records"][0]["source_url"] = "https://sse.com.cn.attacker.example/list"
    elif mutation == "missing_reference":
        payload["security_states"][0]["reference"] = "missing"
    elif mutation == "future_observation":
        payload["security_states"][0]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        payload["source_records"] = []
    with pytest.raises(DataValidationError):
        parse_market_context_bytes(json.dumps(payload).encode())


def test_sse_metadata_keeps_missing_listing_unknown_and_rejects_bad_dates():
    payload = sse_payload()
    assert parse_sse_metadata(sse_body(payload), stock_type="1")[0][2] is None
    for value in (True, "2026-02-30", "2026-1-1", "bad"):
        payload["result"][0]["LIST_DATE"] = value
        with pytest.raises(DataValidationError):
            parse_sse_metadata(sse_body(payload), stock_type="1")


def test_szse_does_not_guess_a_board_from_its_code_or_ignore_a_bad_listing_date():
    data = szse_body([("300001", "人工", "未来新板块", "2026-10-26")], header=("A股代码", "A股简称", "板块", "A股上市日期"))
    assert parse_szse_metadata(data)[0][1] == "unknown"
    with pytest.raises(DataValidationError):
        parse_szse_metadata(szse_body())


def test_risk_parser_rejects_partial_pages_duplicate_codes_and_non_stock_codes():
    query = RISK_QUERIES[0][0]
    row = {"INSTRUMENT_ID": "600000", "INSTRUMENT_SHORT": "ST人工"}
    for payload in (json.loads(risk_body(query, [row])), json.loads(risk_body(query, [row, row])),
                    json.loads(risk_body(query, [{**row, "INSTRUMENT_ID": "000001"}]))):
        if len(payload["result"]) == 1 and payload["result"][0] == row:
            payload["pageHelp"]["total"] = 2
        with pytest.raises(DataValidationError):
            parse_sse_risk_status(json.dumps(payload).encode(), query=query)
    assert parse_sse_risk_status(risk_body(query, []), query=query) == {}


def test_cli_requires_explicit_live_and_does_not_touch_network_or_files():
    output = StringIO()
    with redirect_stdout(output), patch.object(ExchangeMarketContextSource, "read_snapshot", side_effect=AssertionError):
        assert main([]) == 0
    assert json.loads(output.getvalue())["network_requested"] is False


def test_metadata_cross_checks_an_explicit_exchange_directory():
    trusted = SecurityDirectory((Security("000001.SZ", "wrong"),), DIRECTORY_SOURCE, NOW, False)
    with pytest.raises(DataValidationError, match="identities differ"):
        ExchangeMarketContextSource(transport=Transport(), clock=lambda: NOW).read_snapshot(directory=trusted)


def test_selected_szse_path_uses_one_filtered_official_request_and_no_full_market_export():
    transport = Transport()
    trusted = SecurityDirectory((Security("000001.SZ", "平安银行"),), DIRECTORY_SOURCE, NOW, False)
    source = ExchangeMarketContextSource(transport=transport, clock=lambda: NOW)
    value = source.read_snapshot(instrument_ids=("000001.SZ",), directory=trusted)
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == SZSE_JSON_URL
    assert transport.calls[0][1]["txtDMorJC"] == "000001"
    assert len(value.security_states) == 1
    assert value.security_states[0].listing_date == date(1991, 4, 3)


def test_report_business_date_is_distinct_from_next_morning_observation():
    source = ExchangeMarketContextSource(transport=Transport(), clock=lambda: NOW + timedelta(days=1))
    value = source.read_snapshot(instrument_ids=("000001.SZ",))
    assert value.source_records[0].as_of_date == date(2026, 10, 26)
    assert value.security_states[0].observed_at == NOW + timedelta(days=1)
    background = current_security_background(value, instrument_id="000001.SZ", analysis_date=date(2026, 10, 26),
                                             reference_at=NOW + timedelta(days=1))
    assert background["source_as_of_date"] == "2026-10-26"
    assert background["observation_date"] == "2026-10-27"
    assert background["analysis_date_st_status"] == "unknown"


def test_bound_background_projection_exposes_current_provenance_and_hides_late_historical_values():
    request = MultiIndicatorRequest("000001.SZ", date(2026, 7, 19), date(2026, 10, 26),
                                    (IndicatorSpec("sma", window=3),), "tencent", "context-live-test")
    result = analyze_indicators(MemorySourceFactory()(request), request)
    current = ExchangeMarketContextSource(transport=Transport(), clock=lambda: NOW).read_snapshot(instrument_ids=("000001.SZ",))
    projected = build_market_context(result, expected_request=request, snapshot=current, reference_at=NOW)
    assert projected["security"]["board"] == "szse_main"
    assert projected["security"]["published_at"] is None
    assert projected["security"]["publication_precision"] == "unknown"
    assert projected["source_records"][0]["as_of_date"] == "2026-10-26"
    assert projected["calendar"]["missing_bar_dates"] == []
    later = ExchangeMarketContextSource(transport=Transport(), clock=lambda: NOW + timedelta(days=1)).read_snapshot(instrument_ids=("000001.SZ",))
    historical = build_market_context(result, expected_request=request, snapshot=later, reference_at=NOW + timedelta(days=1))
    assert historical["security"]["board"] == "unknown"
    assert historical["security"]["listing_date"] is None
    assert historical["security"]["st_status"] == "unknown"
    assert historical["security"]["reason"] == "observed_after_analysis_cutoff"


@pytest.mark.parametrize("mutation", ["wrong_identity", "partial", "stale", "future", "boolean_count", "unexpected_markup"])
def test_selected_szse_parser_rejects_scope_and_report_date_mismatches(mutation):
    value = szse_json_payload()
    if mutation == "wrong_identity":
        value[0]["data"][0]["agdm"] = "000002"
    elif mutation == "partial":
        value[0]["metadata"]["recordcount"] = 2
    elif mutation == "stale":
        value[0]["metadata"]["subname"] = "2026-10-18"
    elif mutation == "future":
        value[0]["metadata"]["subname"] = "2026-10-27"
    elif mutation == "boolean_count":
        value[0]["metadata"]["recordcount"] = True
    else:
        value[0]["data"][0]["agjc"] = "<script>bad</script>"
    with pytest.raises(DataValidationError):
        parse_szse_security_metadata(json.dumps(value).encode(), instrument_id="000001.SZ", observation_date=date(2026, 10, 26))


@pytest.mark.parametrize("identifiers", [(), ("000001.SZ",) * 2, ("000001.SZ",) * 4, (["000001.SZ"],)])
def test_selected_requests_are_bounded_and_validated_before_network(identifiers):
    transport = Transport()
    with pytest.raises(InvalidRequestError):
        ExchangeMarketContextSource(transport=transport, clock=lambda: NOW).read_snapshot(instrument_ids=identifiers)
    assert transport.calls == []


@pytest.mark.parametrize("board", ["sse_main", "szse_main"])
def test_main_board_st_change_is_effective_on_july_six_not_publication_date(board):
    previous = explain_market_rules(board=board, st_status="star_st", as_of_date=date(2026, 7, 5))
    current = explain_market_rules(board=board, st_status="star_st", as_of_date=date(2026, 7, 6))
    assert previous["ordinary_daily_limit_percent"] == 5
    assert current["ordinary_daily_limit_percent"] == 10
    assert current["policy_published_date"] == "2026-04-24"
    assert current["policy_effective_from"] == "2026-07-06"
    assert all(item["url"].startswith("https://www.") for item in current["sources"])


@pytest.mark.parametrize("board", ["star", "chinext"])
@pytest.mark.parametrize("status", ["not_st", "st", "star_st", "unknown"])
def test_growth_board_risk_categories_retain_twenty_percent_and_research_access(board, status):
    value = explain_market_rules(board=board, st_status=status, as_of_date=date(2026, 10, 26))
    assert value["ordinary_daily_limit_percent"] == 20
    assert value["same_day_resale"] == "not_permitted_for_ordinary_a_shares"
    assert value["execution_allowed"] is False
    assert value["tradability"] == "not_assessed"


def test_ipo_requires_a_known_session_ordinal_not_a_calendar_day_or_bar_count():
    arguments = {"board": "sse_main", "st_status": "not_st", "as_of_date": date(2026, 10, 26),
                 "listing_date": date(2026, 10, 19), "special_event": "ordinary"}
    unknown = explain_market_rules(**arguments)
    assert unknown["ipo_no_limit_applies"] is None
    assert unknown["applied_daily_limit_percent"] is None
    assert "ipo_session_ordinal_not_established" in unknown["unknowns"]
    fifth = explain_market_rules(**arguments, listing_trading_day=5)
    sixth = explain_market_rules(**arguments, listing_trading_day=6)
    assert fifth["price_limit_status"] == "no_daily_price_limit_ipo"
    assert fifth["applied_daily_limit_percent"] is None
    assert sixth["applied_daily_limit_percent"] == 10
    assert sixth["status"] == "available"


def test_unknown_historical_st_and_uncovered_rule_versions_remain_unknown():
    unknown_st = explain_market_rules(board="sse_main", st_status="unknown", as_of_date=date(2026, 1, 1))
    assert unknown_st["ordinary_daily_limit_percent"] is None
    assert "historical_st_status_unknown" in unknown_st["unknowns"]
    for board, day in (("unknown", date(2026, 10, 26)), ("bse", date(2026, 10, 26)),
                       ("sse_main", date(2023, 4, 9)), ("sse_main", date(2026, 10, 27))):
        value = explain_market_rules(board=board, st_status="not_st", as_of_date=day)
        assert value["price_limit_status"] == "unknown"
        assert value["ordinary_daily_limit_percent"] is None


def test_special_event_explanation_never_claims_an_ordinary_daily_limit():
    value = explain_market_rules(board="szse_main", st_status="not_st", as_of_date=date(2026, 10, 26),
                                 special_event="relisting_first_day")
    assert value["price_limit_status"] == "no_daily_price_limit_special_event"
    assert value["applied_daily_limit_percent"] is None


@pytest.mark.parametrize("changes", [{"listing_trading_day": True}, {"listing_trading_day": 0},
    {"listing_trading_day": 3, "listing_date": date(2026, 10, 26)}, {"board": []}, {"special_event": []}])
def test_rule_inputs_reject_ambiguous_or_inconsistent_facts(changes):
    arguments = {"board": "star", "st_status": "unknown", "as_of_date": date(2026, 10, 26), **changes}
    with pytest.raises(InvalidRequestError):
        explain_market_rules(**arguments)


def test_context_wrapper_never_infers_suspension_or_current_st_from_missing_bars():
    value = rules_from_market_context({"status": "limited", "as_of_date": "2026-01-01",
        "security": {"board": "sse_main", "st_status": "unknown", "listing_date": "1999-11-10"},
        "calendar": {"missing_bar_dates": ["2026-01-01"]}})
    assert value["ordinary_daily_limit_percent"] is None
    assert value["tradability"] == "not_assessed"
