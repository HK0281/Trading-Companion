"""Screening correctness, truthful coverage and fixture/live boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.screening import (
    ScreeningCondition, ScreeningRequest, ScreeningService, ScreeningSnapshot,
    demo_screening_snapshot, render_screening_result,
)
from trading_companion.screening_cli import main


NOW = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)


def fixture():
    return demo_screening_snapshot(clock=lambda: NOW)


def request(*conditions, **kwargs):
    return ScreeningRequest(tuple(ScreeningCondition(*item) for item in (conditions or (("amount_cny", "gt", 0),))), **kwargs)


def screen(snapshot=None, selected=None, *, now=NOW):
    return ScreeningService(clock=lambda: now).screen(snapshot or fixture(), selected or request())


def test_demo_is_offline_explicit_and_does_not_exclude_star_st_new_listing_or_bse():
    result = screen().to_dict()
    assert result["status"] == "completed"
    assert len(result["rows"]) == 5
    assert {row["stratum"] for row in result["rows"]} == {"sse_main", "szse_main", "star", "chinext", "bse"}
    assert any("ST" in row["name"] for row in result["rows"])
    assert result["source"]["data_authenticity"] == "synthetic"
    assert result["inclusion_policy"]["new_listings"] == "not_excluded"
    assert result["inclusion_policy"]["suspensions"] == "not_excluded"
    assert all(row["listing_date"] is None and row["risk_status_verified"] is False for row in result["rows"])
    assert result["trading_enabled"] is False
    assert result["forecast"] is None and result["overall_score"] is None
    json.dumps(result, allow_nan=False)


def test_numeric_conditions_are_and_and_missing_pe_is_unknown_not_zero_or_failed():
    result = screen(selected=request(("pe_dynamic", "gt", 0), ("pe_dynamic", "lte", 10))).to_dict()
    assert {row["instrument_id"] for row in result["rows"]} == {"000001.SZ", "600010.SH"}
    assert result["conditions"][0]["unknown"] == 1
    assert result["conditions"][0]["failed"] == 0
    assert result["conditions"][1]["unknown"] == 1
    assert result["conditions"][1]["failed"] == 2
    for row in result["rows"]:
        assert len(row["condition_checks"]) == 2
        assert all(check["status"] == "passed" and check["quoted_at"] == row["quoted_at"] for check in row["condition_checks"])


@pytest.mark.parametrize("operator, threshold, matches", [
    ("lt", 12, 2), ("lte", 12, 3), ("eq", 12, 1), ("gte", 12, 3), ("gt", 12, 2),
])
def test_all_comparators_have_correct_boundary_semantics(operator, threshold, matches):
    assert len(screen(selected=request(("price", operator, threshold))).rows) == matches


def test_negative_valuation_is_preserved_and_not_automatically_a_value_recommendation():
    snapshot = fixture()
    snapshot = replace(snapshot, quotes=(replace(snapshot.quotes[0], pe_dynamic=-8.0), *snapshot.quotes[1:]))
    result = screen(snapshot, request(("pe_dynamic", "lt", 0)))
    assert len(result.rows) == 1 and result.rows[0]["values"]["pe_dynamic"] == -8
    assert result.to_dict()["forecast"] is None


def test_sort_uses_raw_values_ties_by_instrument_and_limit_reports_truncation():
    snapshot = fixture()
    quotes = tuple(replace(item, amount_cny=100.0) for item in snapshot.quotes)
    quotes = (replace(quotes[0], amount_cny=100.00000000000001), *quotes[1:])
    snapshot = replace(snapshot, quotes=quotes)
    result = screen(snapshot, request(limit=3))
    assert [row["instrument_id"] for row in result.rows] == ["000001.SZ", "300001.SZ", "600010.SH"]
    assert result.coverage["sortable_matched_records"] == 5
    assert result.coverage["result_truncated"] is True
    assert [row["sample_position"] for row in result.rows] == [1, 2, 3]
    ascending = screen(snapshot, request(sort_direction="asc"))
    assert ascending.rows[-1]["instrument_id"] == "000001.SZ"


def test_sort_missing_is_separate_from_condition_failure():
    snapshot = fixture()
    snapshot = replace(snapshot, quotes=(replace(snapshot.quotes[0], amount_cny=None), *snapshot.quotes[1:]))
    result = screen(snapshot, request(("price", "gt", 0)))
    assert len(result.rows) == 4
    assert result.coverage["condition_matched_records"] == 5
    assert result.coverage["matched_missing_sort_records"] == 1
    assert result.condition_summary[0]["passed"] == 5
    assert result.coverage["field_coverage_within_verified_sample"]["amount_cny"] == {"available": 4, "missing": 1}


def test_stale_rows_are_not_evaluated_and_not_counted_as_failed_conditions():
    snapshot = fixture()
    snapshot = replace(snapshot, quotes=(replace(snapshot.quotes[0], quoted_at=NOW - timedelta(days=8)), *snapshot.quotes[1:]))
    result = screen(snapshot)
    assert result.coverage["stale_records_not_evaluated"] == 1
    assert result.coverage["age_eligible_records"] == 4
    assert result.condition_summary[0]["passed"] == 4
    assert result.condition_summary[0]["failed"] == 0


def test_quote_age_is_shanghai_calendar_days_not_naive_datetime():
    snapshot = fixture()
    quote_time = NOW - timedelta(minutes=1)
    snapshot = replace(snapshot, quotes=tuple(replace(item, quoted_at=quote_time) for item in snapshot.quotes))
    result = screen(snapshot, request(max_quote_age_days=0))
    assert result.status == "unavailable"  # Shanghai midnight is crossed at this UTC time.
    assert result.coverage["stale_records_not_evaluated"] == 5


def test_empty_is_actual_sample_no_matches_and_never_full_market():
    result = screen(selected=request(("price", "gt", 10000)))
    assert result.status == "empty"
    assert result.coverage["identity_verified_records"] == 5
    assert result.coverage["independent_full_market_completeness_proven"] is False
    assert result.coverage["not_sampled_records_are_not_condition_failures"] is True
    text = render_screening_result(result)
    assert "不表示全市场" in text and "人工样例" in text


def test_no_verified_rows_is_unavailable_with_unverified_coverage_not_failed_conditions():
    snapshot = fixture()
    snapshot = replace(snapshot, quotes=(), strata=tuple(replace(item, verified_records=0) for item in snapshot.strata))
    result = screen(snapshot)
    assert result.status == "unavailable"
    assert result.coverage["unverified_records"] == 5
    assert all(item["passed"] == item["failed"] == item["unknown"] == 0 for item in result.condition_summary)


@pytest.mark.parametrize("age", [timedelta(hours=24, seconds=1), timedelta(seconds=-1)])
def test_snapshot_cannot_be_future_or_reused_after_24_hours(age):
    with pytest.raises(DataValidationError):
        screen(now=NOW + age)


def test_snapshot_requires_security_scope_duplicate_guard_and_coverage_binding():
    snapshot = fixture()
    with pytest.raises(DataValidationError):
        replace(snapshot, quotes=(snapshot.quotes[0],) * 5)
    with pytest.raises(DataValidationError):
        replace(snapshot, quotes=(replace(snapshot.quotes[0], name="错名"), *snapshot.quotes[1:]))
    with pytest.raises(DataValidationError):
        replace(snapshot, strata=(replace(snapshot.strata[0], verified_records=0), *snapshot.strata[1:]))
    with pytest.raises(DataValidationError):
        replace(snapshot, is_fixture=False)
    with pytest.raises(DataValidationError):
        replace(snapshot, quotes=(replace(snapshot.quotes[0], quoted_at=NOW + timedelta(seconds=1)), *snapshot.quotes[1:]))


@pytest.mark.parametrize("value", [True, "12", float("nan"), float("inf"), 10 ** 1000])
def test_invalid_numeric_thresholds_and_quote_values_rejected(value):
    with pytest.raises(InvalidRequestError):
        ScreeningCondition("price", "gt", value)
    with pytest.raises(DataValidationError):
        replace(fixture().quotes[0], price=value)


@pytest.mark.parametrize("payload", [
    {}, {"conditions": []}, {"conditions": (), "sort_by": "price"},
    {"conditions": [{"field": "revenue", "operator": "gt", "value": 2}]},
    {"conditions": [{"field": "price", "operator": "approx", "value": 2}]},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2, "source": "arbitrary"}]},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2}], "source": "arbitrary"},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2}], "limit": True},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2}], "sort_by": "composite_score"},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2}], "limit": 51},
    {"conditions": [{"field": "price", "operator": "gt", "value": 2}], "max_quote_age_days": 15},
])
def test_strict_request_json_cannot_introduce_unsupported_fields_or_resource_options(payload):
    with pytest.raises(InvalidRequestError):
        ScreeningRequest.from_dict(payload)


def test_request_round_trip_and_explicit_percent_unit():
    selected = request(("change_percent", "gte", 1), ("turnover_percent", "gte", 3), sort_by="pb", sort_direction="asc")
    assert ScreeningRequest.from_dict(selected.to_dict()) == selected
    result = screen(selected=selected).to_dict()
    assert len(result["rows"]) == 2
    assert result["fields"]["change_percent"]["unit"] == "percent"
    assert all(row["values"]["change_percent"] >= 1 for row in result["rows"])


def test_naive_clocks_and_duplicate_conditions_are_rejected():
    with pytest.raises(InvalidRequestError):
        ScreeningService(clock=lambda: NOW.replace(tzinfo=None)).screen(fixture(), request())
    with pytest.raises(InvalidRequestError):
        request(("price", "gt", 0), ("price", "gt", 0))
    with pytest.raises(InvalidRequestError):
        ScreeningService().screen({}, request())


def test_cli_demo_uses_no_network_and_has_human_readable_output(monkeypatch, capsys):
    def blocked(*args, **kwargs):
        raise AssertionError("Network must not be requested")
    monkeypatch.setattr("trading_companion.screening_source.EastmoneyQuoteSnapshotSource.read_snapshot", blocked)
    assert main(["--demo", "--condition", "pe_dynamic", "gt", "0", "--condition", "pe_dynamic", "lt", "10"]) == 0
    output = capsys.readouterr().out
    assert "人工样例" in output and "符合条件2条" in output and "不是全市场" in output


def test_cli_demo_json_and_help(capsys):
    assert main(["--demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"]["data_authenticity"] == "synthetic"
    assert main([]) == 0
    assert "--live" in capsys.readouterr().out


def test_cli_live_requires_explicit_directory_and_conditions():
    with pytest.raises(SystemExit):
        main(["--live"])
    with pytest.raises(SystemExit):
        main(["--demo", "--directory", "data/security_directory_live.json"])


def test_cli_invalid_threshold_returns_fixed_error_without_provider_details(capsys):
    assert main(["--demo", "--condition", "price", "gt", "NaN"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "invalid_screening_request"
    assert payload["model_calls"] == 0
