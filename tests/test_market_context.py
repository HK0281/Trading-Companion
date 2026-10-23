from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.market_context import (
    MAX_PUBLIC_BYTES, CalendarDay, ExchangeCalendar, MarketContextSnapshot,
    SecurityState, build_market_context, load_market_context, parse_market_context_bytes,
)
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "sample_daily_bars_synthetic.csv"
REFERENCE = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
PRIVATE = "private-path-or-provider-message"


def payload():
    return {
        "version": "market-context-snapshot-v1", "snapshot_id": "synthetic-context-v1",
        "retrieved_at": "2026-09-26T00:00:00+08:00", "is_fixture": True,
        "security_states": [{
            "instrument_id": "000001.SZ", "effective_from": "2026-09-01", "effective_to": "2026-09-30",
            "board": "szse_main", "listing_date": "1991-04-03", "st_status": "not_st",
            "suspension_status": "not_full_day_suspended", "published_at": "2026-09-01T00:00:00+08:00",
            "source": "synthetic_fixture", "reference": "artificial-security-state-v1",
        }],
        "calendars": [{
            "exchange": "SZ", "coverage_start": "2026-09-21", "coverage_end": "2026-09-25",
            "published_at": "2026-01-01T00:00:00+08:00", "source": "synthetic_fixture",
            "reference": "artificial-calendar-v1",
            "days": [{"date": f"2026-09-{day}", "status": "open"} for day in range(21, 26)],
        }],
    }


class MarketContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "context.json"
        self.request = MultiIndicatorRequest(
            "000001.SZ", date(2026, 9, 21), date(2026, 9, 25),
            (IndicatorSpec("sma", window=3),), "csv", "market-context-test",
        )
        self.source = CsvDailyBarSource(FIXTURE, is_synthetic=True)
        self.result = analyze_indicators(self.source, self.request, allow_development_csv=True)
        self.snapshot = self.load()

    def load(self, value=None):
        self.path.write_text(json.dumps(payload() if value is None else value), encoding="utf-8")
        return load_market_context(self.path)

    def context(self, result=None, request=None, snapshot=None, reference_at=REFERENCE):
        return build_market_context(
            self.result if result is None else result,
            expected_request=self.request if request is None else request,
            snapshot=self.snapshot if snapshot is None else snapshot, reference_at=reference_at,
        )

    def assert_invalid(self, value):
        with self.assertRaises(DataValidationError) as caught:
            self.load(value)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))

    def assert_unavailable(self, value, code="context_integrity_mismatch"):
        self.assertEqual(value, {"version": "research-market-context-v1", "status": "unavailable", "code": code})
        self.assertNotIn(PRIVATE, json.dumps(value))

    def test_loader_produces_frozen_typed_snapshot(self):
        self.assertIs(type(self.snapshot), MarketContextSnapshot)
        self.assertIs(type(self.snapshot.security_states[0]), SecurityState)
        self.assertIs(type(self.snapshot.calendars[0]), ExchangeCalendar)
        self.assertIs(type(self.snapshot.calendars[0].days[0]), CalendarDay)
        self.assertEqual(self.snapshot.retrieved_at.utcoffset(), timedelta(hours=8))
        self.assertEqual(self.snapshot.security_states[0].listing_date, date(1991, 4, 3))
        self.assertIs(type(self.snapshot.security_states), tuple)

    def test_context_schema_is_bound_and_explicitly_not_point_in_time(self):
        context = self.context()
        self.assertEqual(set(context), {
            "version", "status", "code", "request_id", "instrument_id", "start_date", "as_of_date",
            "snapshot_id", "retrieved_at", "data_authenticity", "validation_scope", "historical_availability",
            "security", "calendar", "conflicts", "warnings",
        })
        self.assertEqual((context["status"], context["code"]), ("available", "reported_market_context"))
        self.assertEqual((context["request_id"], context["instrument_id"]), ("market-context-test", "000001.SZ"))
        self.assertEqual((context["start_date"], context["as_of_date"]), ("2026-09-21", "2026-09-25"))
        self.assertEqual(context["data_authenticity"], "synthetic")
        self.assertEqual(context["validation_scope"], "reported_business_date_context_not_point_in_time")
        self.assertEqual(context["historical_availability"], "not_established")
        self.assertIn("not_point_in_time", context["warnings"])
        self.assertIn("retrieved_after_analysis_cutoff", context["warnings"])
        self.assertEqual(set(context["security"]), {
            "status", "reason", "effective_from", "effective_to", "published_at", "source", "reference",
            "board", "listing_date", "st_status", "suspension_status", "listing_age_calendar_days",
        })
        self.assertEqual(set(context["calendar"]), {
            "exchange", "coverage_start", "coverage_end", "published_at", "source", "reference",
            "open_dates", "closed_dates", "unknown_dates", "missing_bar_dates", "unexpected_bar_dates",
        })
        self.assertEqual(context["security"]["listing_age_calendar_days"], (self.request.as_of_date - date(1991, 4, 3)).days)

    def test_checked_in_artificial_context_fixture_is_loadable_and_does_not_claim_market_facts(self):
        snapshot = load_market_context(ROOT / "data" / "market_context_fixture.json")
        context = self.context(snapshot=snapshot)
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["data_authenticity"], "synthetic")
        self.assertIn("synthetic_context_not_market_facts", context["warnings"])

    def test_query_366_day_boundary_is_inclusive_but_367_is_rejected(self):
        request = replace(self.request, start_date=self.request.as_of_date - timedelta(days=365))
        result = replace(self.result, request=request)
        context = self.context(result=result, request=request)
        self.assertEqual(context["status"], "limited")
        calendar = context["calendar"]
        self.assertEqual(sum(len(calendar[name]) for name in ("open_dates", "closed_dates", "unknown_dates")), 366)
        object.__setattr__(request, "start_date", request.start_date - timedelta(days=1))
        self.assert_unavailable(self.context(result=result, request=request))

    def test_no_config_has_fixed_small_unavailable_contract(self):
        self.assert_unavailable(build_market_context(
            self.result, expected_request=self.request, snapshot=None, reference_at=REFERENCE,
        ), "context_not_configured")

    def test_unknown_security_or_gap_does_not_become_a_normal_security(self):
        for value in ([], [{**payload()["security_states"][0], "effective_to": "2026-09-24"}],
                      [{**payload()["security_states"][0], "effective_from": "2026-09-26"}]):
            with self.subTest(states=value):
                data = payload()
                data["security_states"] = value
                context = self.context(snapshot=self.load(data))
                self.assertEqual(context["status"], "limited")
                security = context["security"]
                self.assertEqual((security["status"], security["reason"]), ("unknown", "state_not_covered"))
                self.assertEqual((security["board"], security["st_status"], security["suspension_status"]),
                                 ("unknown", "unknown", "unknown"))
                self.assertIsNone(security["listing_date"])
                self.assertIsNone(security["effective_from"])

    def test_effective_interval_is_inclusive_on_both_ends(self):
        data = payload()
        data["security_states"][0].update(effective_from="2026-09-25", effective_to="2026-09-25")
        self.assertEqual(self.context(snapshot=self.load(data))["security"]["status"], "available")

    def test_partial_state_is_not_silently_filled_from_identity_or_code_prefix(self):
        for field, value in (("board", "unknown"), ("listing_date", None),
                             ("st_status", "unknown"), ("suspension_status", "unknown")):
            with self.subTest(field=field):
                data = payload()
                data["security_states"][0][field] = value
                context = self.context(snapshot=self.load(data))
                self.assertEqual(context["status"], "limited")
                self.assertEqual(context["security"]["status"], "limited")
                self.assertEqual(context["security"]["reason"], "partial_state_unknown")
                self.assertEqual(context["security"][field], value)

    def test_missing_or_future_publication_hides_security_values(self):
        for published, reason in ((None, "publication_time_unknown"),
                                  ("2026-09-26T00:00:00+08:00", "published_after_analysis_cutoff")):
            with self.subTest(published=published):
                data = payload()
                data["security_states"][0]["published_at"] = published
                context = self.context(snapshot=self.load(data))
                self.assertEqual(context["security"]["status"], "unknown")
                self.assertEqual(context["security"]["reason"], reason)
                self.assertEqual(context["security"]["board"], "unknown")
                self.assertIsNone(context["security"]["listing_date"])
                self.assertEqual(context["security"]["source"], "synthetic_fixture")

    def test_shanghai_end_of_day_publication_cutoff_is_not_utc_midnight(self):
        data = payload()
        data["security_states"][0]["published_at"] = "2026-09-25T15:59:59.999999+00:00"
        self.assertEqual(self.context(snapshot=self.load(data))["security"]["status"], "available")
        data["security_states"][0]["published_at"] = "2026-09-25T16:00:00+00:00"
        self.assertEqual(self.context(snapshot=self.load(data))["security"]["reason"], "published_after_analysis_cutoff")

    def test_future_retrieval_relative_to_reference_is_unavailable(self):
        reference = datetime(2026, 9, 25, 15, 59, tzinfo=timezone.utc)
        self.assert_unavailable(self.context(reference_at=reference), "context_future_snapshot")
        same = self.context(reference_at=self.snapshot.retrieved_at)
        self.assertEqual(same["status"], "available")

    def test_st_star_st_and_new_listing_are_preserved_not_excluded(self):
        for st in ("st", "star_st"):
            data = payload()
            data["security_states"][0].update(st_status=st, listing_date="2026-09-21")
            context = self.context(snapshot=self.load(data))
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["security"]["st_status"], st)
            self.assertEqual(context["security"]["listing_age_calendar_days"], 4)
            self.assertEqual(context["conflicts"], [])

    def test_star_board_is_reported_from_metadata_not_excluded(self):
        request = replace(self.request, instrument_id="688981.SH")
        bars = [replace(bar, instrument_id=request.instrument_id) for bar in self.result.bars]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=bars):
            result = analyze_indicators(self.source, request, allow_development_csv=True)
        data = payload()
        data["security_states"][0].update(instrument_id=request.instrument_id, board="star")
        data["calendars"][0]["exchange"] = "SH"
        context = self.context(result=result, request=request, snapshot=self.load(data))
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["security"]["board"], "star")

    def test_listing_day_age_zero_and_future_listing_not_a_negative_age(self):
        data = payload()
        data["security_states"][0]["listing_date"] = "2026-09-25"
        context = self.context(snapshot=self.load(data))
        self.assertEqual(context["security"]["listing_age_calendar_days"], 0)
        self.assertIn("bar_before_reported_listing_date", context["conflicts"])
        data["security_states"][0]["listing_date"] = "2026-09-26"
        context = self.context(snapshot=self.load(data))
        self.assertIsNone(context["security"]["listing_age_calendar_days"])
        self.assertIn("analysis_precedes_reported_listing_date", context["warnings"])

    def test_suspension_conflict_uses_actual_historical_interval_and_allowed_publication(self):
        data = payload()
        old = deepcopy(data["security_states"][0])
        old.update(effective_to="2026-09-23", suspension_status="full_day_suspended")
        data["security_states"][0]["effective_from"] = "2026-09-24"
        data["security_states"].append(old)
        context = self.context(snapshot=self.load(data))
        self.assertEqual(context["security"]["suspension_status"], "not_full_day_suspended")
        self.assertIn("bar_during_reported_full_day_suspension", context["conflicts"])
        self.assertEqual(context["status"], "limited")
        old["published_at"] = "2026-09-26T00:00:00+08:00"
        context = self.context(snapshot=self.load(data))
        self.assertNotIn("bar_during_reported_full_day_suspension", context["conflicts"])

    def test_calendar_explicit_holiday_and_unknown_are_not_treated_as_open(self):
        data = payload()
        data["calendars"][0]["days"][0]["status"] = "closed"
        data["calendars"][0]["days"][1]["status"] = "unknown"
        context = self.context(snapshot=self.load(data))
        self.assertEqual(context["calendar"]["closed_dates"], ["2026-09-21"])
        self.assertEqual(context["calendar"]["unknown_dates"], ["2026-09-22"])
        self.assertEqual(context["calendar"]["unexpected_bar_dates"], ["2026-09-21"])
        self.assertIn("bar_on_reported_closed_date", context["conflicts"])
        self.assertEqual(len(self.result.bars), 5)

    def test_weekend_can_be_explicitly_open_without_weekday_inference(self):
        request = replace(self.request, as_of_date=date(2026, 9, 27))
        result = replace(self.result, request=request)
        data = payload()
        calendar = data["calendars"][0]
        calendar["coverage_end"] = "2026-09-27"
        calendar["days"].extend([{"date": "2026-09-26", "status": "open"},
                                  {"date": "2026-09-27", "status": "closed"}])
        context = self.context(result=result, request=request, snapshot=self.load(data))
        self.assertIn("2026-09-26", context["calendar"]["open_dates"])
        self.assertEqual(context["calendar"]["missing_bar_dates"], ["2026-09-26"])
        self.assertEqual(context["calendar"]["closed_dates"], ["2026-09-27"])
        self.assertEqual(context["status"], "available")
        self.assertIn("open_dates_without_bars_do_not_imply_suspension", context["warnings"])

    def test_calendar_unknown_publication_or_absent_exchange_makes_every_query_day_unknown(self):
        for calendars in ([], [{**payload()["calendars"][0], "exchange": "SH"}],
                          [{**payload()["calendars"][0], "published_at": None}],
                          [{**payload()["calendars"][0], "published_at": "2026-09-26T00:00:00+08:00"}]):
            data = payload()
            data["calendars"] = calendars
            context = self.context(snapshot=self.load(data))
            calendar = context["calendar"]
            self.assertEqual(len(calendar["unknown_dates"]), 5)
            self.assertEqual(calendar["open_dates"], [])
            self.assertEqual(calendar["closed_dates"], [])
            self.assertEqual(calendar["unexpected_bar_dates"], [])
            self.assertEqual(context["status"], "limited")

    def test_calendar_partial_coverage_marks_outside_dates_unknown_without_padding(self):
        data = payload()
        calendar = data["calendars"][0]
        calendar["coverage_start"] = "2026-09-22"
        calendar["coverage_end"] = "2026-09-24"
        calendar["days"] = calendar["days"][1:-1]
        context = self.context(snapshot=self.load(data))
        self.assertEqual(context["calendar"]["unknown_dates"], ["2026-09-21", "2026-09-25"])
        self.assertEqual(len(context["calendar"]["open_dates"]), 3)

    def test_fixture_authenticity_cannot_mix_with_real_or_unverified_price_data(self):
        for authenticity in ("unverified_csv", "provider_reported"):
            if authenticity == "provider_reported":
                request = replace(self.request, source="tencent")
                result = replace(self.result, request=request, source="tencent", price_adjustment="unadjusted",
                                 data_authenticity=authenticity)
            else:
                request = self.request
                result = replace(self.result, data_authenticity=authenticity)
            self.assert_unavailable(self.context(result=result, request=request), "context_authenticity_mismatch")
        data = payload()
        data["is_fixture"] = False
        data["security_states"][0]["source"] = "curated_local"
        data["calendars"][0]["source"] = "curated_local"
        real_snapshot = self.load(data)
        self.assert_unavailable(self.context(snapshot=real_snapshot), "context_authenticity_mismatch")
        context = self.context(result=replace(self.result, data_authenticity="unverified_csv"), snapshot=real_snapshot)
        self.assertEqual(context["data_authenticity"], "unverified_local_snapshot")

    def test_wrong_request_or_corrupt_latest_evidence_blocks_context(self):
        for request in (replace(self.request, request_id="other"),
                        replace(self.request, instrument_id="600000.SH"),
                        replace(self.request, as_of_date=date(2026, 9, 26)),
                        replace(self.request, source="tencent")):
            self.assert_unavailable(self.context(request=request))
        self.assert_unavailable(self.context(result=replace(self.result, facts=())))

    def test_invalid_reference_or_tampered_snapshot_does_not_leak(self):
        self.assert_unavailable(self.context(reference_at=datetime(2026, 9, 26)))
        self.assert_unavailable(self.context(reference_at=PRIVATE))
        snapshot = replace(self.snapshot)
        object.__setattr__(snapshot, "snapshot_id", "../" + PRIVATE)
        self.assert_unavailable(self.context(snapshot=snapshot))
        state = replace(self.snapshot.security_states[0])
        object.__setattr__(state, "board", PRIVATE)
        snapshot = replace(self.snapshot)
        object.__setattr__(snapshot, "security_states", (state,))
        self.assert_unavailable(self.context(snapshot=snapshot))

    def test_no_data_source_error_and_invalid_data_have_no_numeric_context(self):
        for status in ("no_data", "source_error", "invalid_data"):
            result = replace(self.result, data_status=status, bars=(), message=PRIVATE)
            self.assert_unavailable(self.context(result=result), "context_result_unavailable")

    def test_full_json_byte_limit_is_checked_not_truncated(self):
        context = self.context()
        size = len(json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        self.assertLess(size, MAX_PUBLIC_BYTES)
        with patch("trading_companion.market_context.MAX_PUBLIC_BYTES", size):
            self.assertEqual(self.context()["status"], "available")
        with patch("trading_companion.market_context.MAX_PUBLIC_BYTES", size - 1):
            self.assert_unavailable(self.context(), "context_payload_limit")

    def test_projection_is_deeply_detached_and_does_not_write_fetch_or_recalculate(self):
        with patch.object(Path, "open", side_effect=AssertionError("No file reads or writes")) as files, \
                patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No market reads")) as read, \
                patch("trading_companion.multi_analysis.analyze_indicators", side_effect=AssertionError("No analysis")) as analyze, \
                patch("socket.create_connection", side_effect=AssertionError("No network")) as network:
            context = self.context()
            fresh = self.context()
        self.assertEqual(context, fresh)
        context["security"]["board"] = "unknown"
        context["calendar"]["open_dates"].clear()
        context["warnings"].clear()
        self.assertEqual(self.context(), fresh)
        for guard in (files, read, analyze, network):
            guard.assert_not_called()

    def test_loader_rejects_unknown_missing_fields_and_wrong_primitive_types(self):
        variants = []
        for target, key in (("root", "unexpected"), ("state", "unexpected"),
                            ("calendar", "unexpected"), ("day", "unexpected")):
            data = payload()
            selected = {"root": data, "state": data["security_states"][0],
                        "calendar": data["calendars"][0], "day": data["calendars"][0]["days"][0]}[target]
            selected[key] = PRIVATE
            variants.append(data)
        for key in payload():
            data = payload()
            del data[key]
            variants.append(data)
        for key, value in (("version", True), ("is_fixture", 1), ("security_states", {}),
                           ("calendars", "[]"), ("snapshot_id", "../" + PRIVATE)):
            data = payload()
            data[key] = value
            variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                self.assert_invalid(data)

    def test_loader_rejects_noncanonical_dates_timestamps_and_unbounded_provenance(self):
        cases = (("effective_from", "2026-9-01"), ("effective_to", "2026-02-30"),
                 ("listing_date", "20260901"), ("published_at", "2026-09-01"),
                 ("published_at", "2026-09-01T00:00:00"), ("published_at", "2026-09-01 00:00:00+08:00"),
                 ("published_at", "2026-09-01T00:00:00+00:60"),
                 ("published_at", "2026-09-01T00:00:00+24:00"),
                 ("published_at", "2026-09-01T24:00:00+08:00"),
                 ("published_at", "2026-09-01T00:60:00+08:00"),
                 ("published_at", "2026-09-01T00:00:60+08:00"),
                 ("published_at", "2026-09-27T00:00:00+08:00"), ("reference", "C:/" + PRIVATE),
                 ("reference", "x" * 101), ("source", PRIVATE), ("instrument_id", "000001"),
                 ("board", "sse_main"), ("st_status", False), ("suspension_status", "normal"))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                data = payload()
                data["security_states"][0][key] = value
                self.assert_invalid(data)

    def test_loader_rejects_overlapping_states_but_accepts_explicit_gap(self):
        data = payload()
        data["security_states"].append(deepcopy(data["security_states"][0]))
        self.assert_invalid(data)
        data["security_states"][0]["effective_to"] = "2026-09-20"
        data["security_states"][1]["effective_from"] = "2026-09-22"
        self.assertIs(type(self.load(data)), MarketContextSnapshot)

    def test_calendar_must_cover_every_declared_natural_day_in_exact_order(self):
        original = payload()["calendars"][0]["days"]
        for days in (original[:-1], original[1:], list(reversed(original)),
                     [original[0], original[0], *original[2:]], [*original, original[-1]]):
            data = payload()
            data["calendars"][0]["days"] = days
            self.assert_invalid(data)
        data = payload()
        data["calendars"].append(deepcopy(data["calendars"][0]))
        self.assert_invalid(data)

    def test_size_and_count_limits_fail_closed(self):
        self.path.write_bytes(b" " * 33)
        with patch("trading_companion.market_context.MAX_SNAPSHOT_BYTES", 32):
            with self.assertRaises(DataValidationError):
                load_market_context(self.path)
        with patch("trading_companion.market_context.MAX_STATES", 0):
            self.assert_invalid(payload())
        with patch("trading_companion.market_context.MAX_CALENDAR_DAYS", 4):
            self.assert_invalid(payload())

    def test_calendar_3660_day_boundary_is_inclusive(self):
        data = payload()
        calendar = data["calendars"][0]
        start = date(2020, 1, 1)
        calendar["coverage_start"] = start.isoformat()
        calendar["coverage_end"] = (start + timedelta(days=3659)).isoformat()
        calendar["days"] = [{"date": (start + timedelta(days=index)).isoformat(), "status": "unknown"}
                            for index in range(3660)]
        self.assertEqual(len(self.load(data).calendars[0].days), 3660)
        calendar["coverage_end"] = (start + timedelta(days=3660)).isoformat()
        calendar["days"].append({"date": calendar["coverage_end"], "status": "unknown"})
        self.assert_invalid(data)

    def test_duplicate_json_keys_nonfinite_constants_and_invalid_encoding_are_rejected(self):
        for content in (b'{"snapshot_id":"a","snapshot_id":"b"}', b'{"value":NaN}',
                        b'{"value":Infinity}', b'\xff', b'{broken'):
            self.path.write_bytes(content)
            with self.assertRaises(DataValidationError) as caught:
                load_market_context(self.path)
            self.assertNotIn(str(self.path), str(caught.exception))

    def test_missing_file_uses_sanitized_source_error(self):
        with self.assertRaises(DataSourceError) as caught:
            load_market_context(self.path.parent / PRIVATE / "missing.json")
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_bytes_parser_uses_identical_validation_without_reopening_a_file(self):
        raw = json.dumps(payload()).encode("utf-8")
        with patch.object(Path, "open", side_effect=AssertionError("No reread")) as read:
            snapshot = parse_market_context_bytes(raw)
        self.assertEqual(snapshot, self.snapshot)
        read.assert_not_called()
        for invalid in (raw.decode("utf-8"), bytearray(raw), None, b'{"a":1,"a":2}', b'NaN', b'\xff'):
            with self.subTest(raw=invalid), self.assertRaises(DataValidationError):
                parse_market_context_bytes(invalid)
        with patch("trading_companion.market_context.MAX_SNAPSHOT_BYTES", len(raw)):
            self.assertEqual(parse_market_context_bytes(raw), self.snapshot)
        with patch("trading_companion.market_context.MAX_SNAPSHOT_BYTES", len(raw) - 1):
            with self.assertRaises(DataValidationError):
                parse_market_context_bytes(raw)

    def test_file_loader_performs_one_bounded_read_then_delegates_same_bytes(self):
        raw = self.path.read_bytes()
        with patch("trading_companion.market_context.parse_market_context_bytes", return_value=self.snapshot) as parse:
            self.assertIs(load_market_context(self.path), self.snapshot)
        parse.assert_called_once_with(raw)

    def test_non_minute_offsets_cannot_escape_typed_construction_as_noncanonical_iso(self):
        with self.assertRaises(DataValidationError):
            replace(self.snapshot, retrieved_at=datetime(2026, 9, 26, tzinfo=timezone(timedelta(seconds=1))))

    def test_real_snapshot_cannot_claim_fixture_source_or_wrong_exchange_source(self):
        data = payload()
        data["is_fixture"] = False
        self.assert_invalid(data)
        data["security_states"][0]["source"] = "sse"
        data["calendars"][0]["source"] = "szse"
        self.assert_invalid(data)


if __name__ == "__main__":
    unittest.main()
