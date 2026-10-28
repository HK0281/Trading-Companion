"""Expanded sampling and independent coverage reconciliation, using injected transports."""

from dataclasses import replace
import json

import pytest

from trading_companion.errors import DataValidationError, InvalidRequestError, DataSourceError
from trading_companion.screening import ScreeningCondition, ScreeningRequest, ScreeningService, render_screening_result, market_stratum
from trading_companion.screening_cli import main
from trading_companion.screening_source import TencentQuoteSnapshotSource, EastmoneyQuoteSnapshotSource, configured_quote_source
from trading_companion.security_directory import Security, SecurityDirectory
from tests.test_screening_source import NOW, TencentSession, Session, Response, directory


def large_directory(size=400):
    groups = (("600", "SH"), ("000", "SZ"), ("688", "SH"), ("300", "SZ"), ("920", "BJ"))
    identities = tuple(Security(f"{prefix}{index:03d}.{exchange}", f"人工{prefix}{index}")
        for prefix, exchange in groups for index in range(size))
    return SecurityDirectory(tuple(reversed(identities)), "offline_transport_identity_test", NOW)


def collect(trusted, *, coverage="expanded", session=None, **options):
    return TencentQuoteSnapshotSource(session=session or TencentSession(trusted), coverage=coverage,
        clock=lambda: NOW, **options).read_snapshot(trusted)


def evaluate(snapshot, value=0):
    return ScreeningService(clock=lambda: NOW).screen(snapshot,
        ScreeningRequest((ScreeningCondition("price", "gt", value),)))


def test_expanded_intervals_reach_late_codes_repeat_exactly_and_keep_standard_prefix():
    trusted = large_directory()
    def prices(lines):
        for symbol, fields in lines:
            fields[3] = "99" if symbol == "sh600398" else "1"
        return lines
    first, second = TencentSession(trusted, prices), TencentSession(trusted, prices)
    expanded = collect(trusted, session=first)
    repeated = collect(trusted, session=second)
    standard = collect(trusted, session=TencentSession(trusted, prices), coverage="standard")
    assert expanded == repeated and first.calls == second.calls
    assert len(expanded.quotes) == 1000 and len(first.calls) == 15
    assert len(standard.quotes) == 250
    assert all(int(item.instrument_id[3:6]) < 50 for item in standard.quotes)
    assert evaluate(standard, 50).status == "empty"
    result = evaluate(expanded, 50)
    assert [item["instrument_id"] for item in result.rows] == ["600398.SH"]
    audit = result.coverage["directory_audit"]
    assert audit["selection_method"] == "directory_code_uniform_interval"
    assert audit["directory_records"] == 2000 and audit["directory_unobserved_records"] == 1000
    assert all(item["selection_limit"] == 200 and item["directory_selected_records"] == 200
        and item["directory_not_selected_records"] == 200 for item in audit["strata"])
    assert result.coverage["independent_full_market_completeness_proven"] is False
    assert "均匀间隔" in render_screening_result(result)


@pytest.mark.parametrize("size,expected", [(201, 200), (401, 200), (20, 20)])
def test_interval_fills_available_slots_and_takes_all_of_small_boards(size, expected):
    snapshot = collect(large_directory(size))
    assert all(item.scanned_records == expected and item.selection_limit == 200 for item in snapshot.strata)
    assert all(item["directory_selected_records"] == expected for item in snapshot.directory_audit()["strata"])


def test_real_board_sizes_fill_200_slots_with_increasing_unique_uniform_positions():
    groups = ((600000, "SH", 1701), (0, "SZ", 1495), (688000, "SH", 617), (300000, "SZ", 1411), (920000, "BJ", 150))
    identities = tuple(Security(f"{start + index:06d}.{exchange}", f"人工{start + index}")
        for start, exchange, size in groups for index in range(size))
    trusted = SecurityDirectory(tuple(reversed(identities)), "offline_review_board_sizes", NOW)
    snapshot = collect(trusted)
    assert len(snapshot.quotes) == 950
    for item in snapshot.strata:
        universe = sorted(security.instrument_id for security in identities if market_stratum(security.instrument_id) == item.stratum)
        positions = {instrument: index for index, instrument in enumerate(universe)}
        chosen = [positions[quote.instrument_id] for quote in snapshot.quotes if market_stratum(quote.instrument_id) == item.stratum]
        expected = min(200, len(universe))
        assert item.scanned_records == len(chosen) == expected
        assert len(set(chosen)) == expected and chosen == sorted(chosen)
        assert chosen[0] == 0 and chosen[-1] == len(universe) - (len(universe) + expected - 1) // expected
        assert {right - left for left, right in zip(chosen, chosen[1:])} <= {
            len(universe) // expected, (len(universe) + expected - 1) // expected}
    assert snapshot == collect(trusted)


def test_largest_request_count_and_incomplete_middle_batch_are_explicit():
    trusted = large_directory()
    calls = 0
    def omit(lines):
        nonlocal calls
        calls += 1
        return lines[:-1] if calls == 2 else lines
    session = TencentSession(trusted, omit)
    result = evaluate(collect(trusted, session=session, batch_size=50))
    assert len(session.calls) == 20 and result.coverage["directory_audit"]["request_count"] == 20
    assert result.coverage["missing_quote_records"] == 1
    assert result.coverage["unverified_records"] == 0
    assert result.coverage["scanned_records"] == 1000
    assert result.coverage["directory_audit"]["directory_unobserved_records"] == 1001
    assert result.condition_summary[0]["failed"] == 0
    assert all(response.closed for response in session.responses)


def test_absent_board_is_a_directory_gap_even_if_zero_selected_equals_zero_total():
    trusted = replace(directory(), securities=tuple(item for item in directory().securities if not item.instrument_id.endswith(".BJ")))
    result = evaluate(collect(trusted))
    bse = result.coverage["directory_audit"]["strata"][-1]
    assert bse["directory_records"] == bse["directory_selected_records"] == 0
    assert result.coverage["strata"][-1]["directory_stratum_exhausted"] is True
    assert "目录未覆盖：北交所" in render_screening_result(result)
    assert result.coverage["independent_full_market_completeness_proven"] is False


def test_vendor_second_page_is_evaluated_while_unobserved_directory_ids_are_not_missing_quotes():
    trusted = replace(directory(), securities=(*directory().securities, Security("600011.SH", "后页公司"), Security("600012.SH", "未读公司")))
    def pages(payload, stratum, params):
        if stratum == "sse_main":
            payload["data"]["total"] = 3
            payload["data"]["diff"][0]["f2"] = 1
            if params["pn"] == "2":
                payload["data"]["diff"][0].update(f12="600011", f14="后页公司", f2=99)
        return Response(payload)
    snapshot = EastmoneyQuoteSnapshotSource(session=Session(pages), clock=lambda: NOW,
        page_size=1, pages_per_stratum=2).read_snapshot(trusted)
    result = evaluate(snapshot, 50)
    assert [row["instrument_id"] for row in result.rows] == ["600011.SH"]
    row = result.coverage["directory_audit"]["strata"][0]
    assert row["directory_records"] == 3 and row["directory_unobserved_records"] == 1
    assert row["vendor_not_scanned_records"] == 1 and row["directory_not_selected_records"] is None
    assert result.coverage["missing_quote_records"] == 0
    assert result.coverage["directory_audit"]["selection_method"] == "vendor_page_order"


def test_exhausted_vendor_list_with_unknown_identity_still_has_unobserved_directory_and_no_market_proof():
    def unknown(payload, stratum, params):
        if stratum == "sse_main":
            payload["data"]["diff"][0].update(f12="600011", f14="目录外公司")
        return Response(payload)
    result = evaluate(EastmoneyQuoteSnapshotSource(session=Session(unknown), clock=lambda: NOW).read_snapshot(directory()))
    assert all(row["vendor_listing_exhausted"] for row in result.coverage["strata"])
    assert result.coverage["directory_audit"]["directory_unobserved_records"] == 1
    assert result.coverage["unverified_records"] == 1
    assert result.coverage["missing_quote_records"] == 0
    assert result.coverage["independent_full_market_completeness_proven"] is False


def test_profile_factory_preserves_defaults_deadlines_and_rejects_conflicts():
    standard, expanded = configured_quote_source("tencent"), configured_quote_source("tencent", "expanded")
    assert (standard._per_stratum, standard._coverage) == (50, "standard")
    assert (expanded._per_stratum, expanded._coverage) == (200, "expanded")
    assert standard._deadline == expanded._deadline == 25
    assert configured_quote_source("eastmoney", "expanded")._pages == 2
    for provider, options in (("tencent", {"pages_per_stratum": 1}), ("eastmoney", {"per_stratum": 50}),
        ("tencent", {"coverage": "expanded", "per_stratum": 50}), ("eastmoney", {"coverage": "expanded", "pages_per_stratum": 1}),
        ("other", {}), ("tencent", {"coverage": True})):
        with pytest.raises(InvalidRequestError):
            configured_quote_source(provider, **options)
    for options in ({"coverage": "expanded", "per_stratum": 201}, {"coverage": []}, {"coverage": "expanded", "deadline_seconds": 31}):
        with pytest.raises(InvalidRequestError):
            TencentQuoteSnapshotSource(**options)


def test_snapshot_rejects_invented_directory_counts_limits_and_excessive_requests():
    snapshot = collect(directory())
    with pytest.raises(DataValidationError):
        replace(snapshot, sampling_policy=[])
    for changes in ({"vendor_total": 2}, {"selection_limit": None}, {"selection_limit": True}, {"selection_limit": 201}, {"pages_read": 5}):
        with pytest.raises(DataValidationError):
            replace(snapshot, strata=(replace(snapshot.strata[0], **changes), *snapshot.strata[1:]))


def test_expanded_total_byte_budget_does_not_grow_with_request_count():
    trusted = large_directory()
    def large_rows(lines):
        for _, fields in lines:
            fields[0] = "1" * 2000
        return lines
    session = TencentSession(trusted, large_rows)
    with pytest.raises(DataValidationError, match="budget"):
        collect(trusted, session=session)
    assert len(session.calls) <= 15
    sizes = [len(response.body) for response in session.responses]
    assert max(sizes) <= 200_000 and sum(sizes[:-1]) <= 2_000_000 < sum(sizes)
    assert all(response.closed for response in session.responses)


def test_expanded_collection_retains_25_second_deadline_during_second_batch(monkeypatch):
    elapsed = [0.0]
    calls = 0
    def slow(lines):
        nonlocal calls
        calls += 1
        if calls == 2:
            elapsed[0] = 25.0
        return lines
    trusted = large_directory()
    session = TencentSession(trusted, slow)
    monkeypatch.setattr("trading_companion.screening_source.monotonic", lambda: elapsed[0])
    with pytest.raises(DataSourceError, match="deadline"):
        collect(trusted, session=session)
    assert len(session.calls) == 2 and all(response.closed for response in session.responses)


def test_cli_expanded_profile_passes_real_configuration_before_directory_or_network(monkeypatch, capsys):
    trusted = large_directory()
    sample = collect(trusted)
    captured = []
    def read(self, value):
        captured.append((self._per_stratum, self._coverage, value))
        return sample
    monkeypatch.setattr("trading_companion.screening_cli.load_security_directory", lambda _: trusted)
    monkeypatch.setattr(TencentQuoteSnapshotSource, "read_snapshot", read)
    monkeypatch.setattr("trading_companion.screening_cli.ScreeningService", lambda: ScreeningService(clock=lambda: NOW))
    assert main(["--live", "--directory", "unused", "--condition", "price", "gt", "0",
        "--screening-coverage", "expanded", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["coverage"]["scanned_records"] == 1000
    assert captured == [(200, "expanded", trusted)]
    captured.clear()
    for flags in (["--screening-coverage", "expanded", "--per-stratum", "50"], ["--pages-per-stratum", "1"]):
        assert main(["--live", "--directory", "unused", "--condition", "price", "gt", "0", *flags]) == 1
        assert json.loads(capsys.readouterr().out)["code"] == "invalid_screening_request"
    assert captured == []


def test_cli_worker_width_is_validated_before_directory_and_isolated_from_demo(monkeypatch, capsys):
    monkeypatch.setattr("trading_companion.screening_cli.load_security_directory",
                        lambda _: pytest.fail("No directory before argument validation"))
    monkeypatch.setattr("trading_companion.screening_source.shared_collection_executor",
                        lambda: pytest.fail("No pool in demo or argument validation"))
    for value in ("0", "4", "true", "1.5"):
        with pytest.raises(SystemExit) as error:
            main(["--live", "--collection-workers", value])
        assert error.value.code == 2
    for width in (1, 2, 3):
        with pytest.raises(SystemExit) as error:
            main(["--demo", "--collection-workers", str(width)])
        assert error.value.code == 2
    assert main(["--demo"]) == 0
    assert "人工" in capsys.readouterr().out


def test_cli_live_width_reaches_actual_quote_configuration(monkeypatch, capsys):
    trusted = directory()
    sample = collect(trusted, coverage="standard")
    captured = []
    def read(self, value):
        captured.append(self.max_workers)
        return sample
    monkeypatch.setattr("trading_companion.screening_cli.load_security_directory", lambda _: trusted)
    monkeypatch.setattr(TencentQuoteSnapshotSource, "read_snapshot", read)
    monkeypatch.setattr("trading_companion.screening_cli.ScreeningService", lambda: ScreeningService(clock=lambda: NOW))
    for width in (None, 1, 2, 3):
        flags = [] if width is None else ["--collection-workers", str(width)]
        assert main(["--live", "--directory", "unused", "--condition", "price", "gt", "0", *flags]) == 0
    assert captured == [3, 1, 2, 3]
