from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle
from trading_companion.risk_research import build_risk_scenario
from trading_companion.security_directory import Security
from trading_companion.snapshot_store import (
    MAX_SNAPSHOT_BYTES, ResearchSnapshotStore, SNAPSHOT_POLICY, SnapshotStoreError, VERSION,
)
from trading_companion.tool_model import ToolModelResponse


UTC = timezone.utc
OBSERVED = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
PRIVATE = "private-question-model-answer-never-save"
START, END = date(2026, 9, 21), date(2026, 9, 25)


class _MemorySource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def read_daily_bars(self):
        return [DailyBar("000001.SZ", START + timedelta(days=index), close, close, close, close, 100, 0)
                for index, close in enumerate((10, 11, 13, 12, 14))]


def make_bundle(*, technical=False, retrieved_at=RETRIEVED.isoformat(), marker="100亿元"):
    plan = ProductPlan(status="ready", securities=(Security("000001.SZ", "平安银行"),),
        kinds=("financial", "technical") if technical else ("financial",), as_of=END,
        start_date=START, end_date=END, source="csv" if technical else "tencent",
        user_question=PRIVATE, model_response=ToolModelResponse("complete", PRIVATE))
    items = (ProductEvidence("source-document-1", "000001.SZ", "financial", "营业收入", marker,
        "fixture-company-source", date="2026-08-25", published_at="2026-08-25T08:00:00+08:00",
        publication_precision="datetime", report_period="2026-06-30", retrieved_at=retrieved_at),)
    coverage = ({"kind": "financial", "status": "available", "code": "ok", "message": "资料可用",
                 "source": "fixture-company-source", "retrieved_at": retrieved_at},)
    result, risk = None, None
    if technical:
        request = MultiIndicatorRequest("000001.SZ", START, END,
            indicators=(IndicatorSpec("atr", window=2), IndicatorSpec("sma", window=3)),
            source="csv", request_id="snapshot-store-fixture")
        result = replace(analyze_indicators(_MemorySource(), request, allow_development_csv=True),
                         retrieved_at=RETRIEVED)
        risk = build_risk_scenario(result, expected_request=request)
        plan = replace(plan, indicators=request.indicators, indicators_explicit=True)
    return ProductResearchBundle(plan, (ProductCompany("000001.SZ", "平安银行", items, coverage, result, risk),))


class SnapshotStoreTests(unittest.TestCase):
    def test_indicator_scope_mismatch_cannot_be_archived(self):
        bundle = make_bundle(technical=True)
        altered = replace(bundle, plan=replace(bundle.plan, indicators=(IndicatorSpec("rsi", window=7),)))
        with self.assertRaises(SnapshotStoreError):
            self.store.save(altered)

    def setUp(self):
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No network in snapshot tests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"
        self.bundle = make_bundle()
        self.store = ResearchSnapshotStore(self.path)
        self.addCleanup(self.store.close)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(SnapshotStoreError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def update(self, snapshot_id, column, value):
        # Columns are fixed test literals, never external input.
        self.assertIn(column, ("payload", "digest", "schema_version", "observed_at", "source_latest_at", "unknown_count"))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(f"UPDATE research_snapshots SET {column} = ? WHERE snapshot_id = ?", (value, snapshot_id))

    def test_import_does_not_open_a_database(self):
        import trading_companion.snapshot_store as module
        spec = importlib.util.spec_from_file_location("_snapshot_import_without_io", module.__file__)
        with patch("sqlite3.connect", side_effect=AssertionError("Import must not open a database")):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_round_trip_keeps_technical_facts_dependencies_risk_and_synthetic_labels(self):
        bundle = make_bundle(technical=True)
        identifier = self.store.save(bundle)
        payload = self.store.load(identifier)
        self.assertEqual(len(identifier), 64)
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(payload["snapshot_policy"], SNAPSHOT_POLICY)
        self.assertFalse(payload["trading_enabled"])
        company = payload["companies"][0]
        self.assertEqual(company["instrument_id"], "000001.SZ")
        self.assertEqual(company["technical"], bundle.companies[0].technical.to_dict())
        self.assertEqual(len(company["technical"]["bars"]), 5)
        self.assertEqual(company["technical"]["data_authenticity"], "synthetic")
        self.assertTrue(company["technical"]["facts"])
        self.assertEqual(company["risk"], bundle.companies[0].risk.to_dict())
        self.assertEqual(len(company["risk"]["evidence_ids"]), 2)

    def test_multiple_companies_keep_original_scopes_and_evidence_order(self):
        original = self.bundle.companies[0]
        other = ProductCompany("600000.SH", "浦发银行",
            (replace(original.evidence[0], id="source-document-2", instrument_id="600000.SH", display="80亿元"),),
            original.coverage)
        plan = replace(self.bundle.plan, securities=(Security(original.instrument_id, original.name),
                                                    Security(other.instrument_id, other.name)))
        bundle = replace(self.bundle, plan=plan, companies=(original, other))
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual([item["instrument_id"] for item in payload["companies"]], ["000001.SZ", "600000.SH"])
        self.assertEqual([item["evidence"][0]["display"] for item in payload["companies"]], ["100亿元", "80亿元"])
        self.assertEqual(self.store.list()[0]["evidence_count"], 2)
        duplicate_id = replace(bundle, companies=(original, replace(other,
            evidence=(replace(other.evidence[0], id=original.evidence[0].id),))))
        self.assert_error("invalid_snapshot_bundle", self.store.save, duplicate_id)

    def test_question_and_model_response_are_not_written_to_payload_or_sqlite(self):
        payload = self.store.load(self.store.save(self.bundle))
        self.assertNotIn(PRIVATE, json.dumps(payload))
        self.assertNotIn(PRIVATE.encode(), self.path.read_bytes())
        self.assertNotIn("user_question", payload["plan"])
        self.assertNotIn("model_response", payload["plan"])
        self.assertNotIn("context", payload["plan"])

    def test_duplicate_save_is_idempotent_and_keeps_first_observation(self):
        first = self.store.save(self.bundle)
        with patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED + timedelta(days=1)):
            second = self.store.save(replace(self.bundle,
                plan=replace(self.bundle.plan, user_question="a different private question")))
        self.assertEqual(first, second)
        self.assertEqual(self.store.load(first)["observed_at"], OBSERVED.isoformat(timespec="microseconds"))
        self.assertEqual(len(self.store.list()), 1)

    def test_distinct_data_creates_distinct_identifier_and_survives_reopen(self):
        first, second = self.store.save(self.bundle), self.store.save(make_bundle(marker="101亿元"))
        self.assertNotEqual(first, second)
        self.store.close()
        with ResearchSnapshotStore(self.path) as reopened:
            self.assertEqual(reopened.load(first)["companies"][0]["evidence"][0]["display"], "100亿元")
            self.assertEqual(reopened.load(second)["companies"][0]["evidence"][0]["display"], "101亿元")

    def test_loaded_and_listed_objects_are_detached(self):
        identifier = self.store.save(self.bundle)
        payload = self.store.load(identifier)
        payload["companies"][0]["evidence"][0]["display"] = "tampered"
        metadata = self.store.list()[0]
        metadata["securities"].clear()
        self.assertEqual(self.store.load(identifier)["companies"][0]["evidence"][0]["display"], "100亿元")
        self.assertEqual(len(self.store.list()[0]["securities"]), 1)

    def test_known_at_uses_first_local_observation_instead_of_analysis_date(self):
        identifier = self.store.save(self.bundle)
        self.assert_error("snapshot_not_found", self.store.load, identifier,
                          known_at=OBSERVED - timedelta(microseconds=1))
        self.assertEqual(self.store.list(known_at=OBSERVED - timedelta(microseconds=1)), ())
        self.assertEqual(self.store.load(identifier, known_at=OBSERVED)["snapshot_id"], identifier)
        self.assertEqual(len(self.store.list(known_at=OBSERVED)), 1)

    def test_source_retrieval_timestamps_are_normalized_and_publication_is_separate(self):
        local = RETRIEVED.astimezone(timezone(timedelta(hours=8)))
        payload = self.store.load(self.store.save(make_bundle(retrieved_at=local.isoformat())))
        boundary = payload["source_retrieval"]
        self.assertEqual(boundary["latest_at"], RETRIEVED.isoformat(timespec="microseconds"))
        self.assertEqual(boundary["earliest_at"], boundary["latest_at"])
        self.assertEqual(boundary["unknown_count"], 0)
        self.assertEqual(boundary["sources"][0]["records"], 2)
        self.assertEqual(payload["companies"][0]["evidence"][0]["published_at"], "2026-08-25T08:00:00+08:00")
        self.assertEqual(len(self.store.list(known_at=OBSERVED.astimezone(timezone(timedelta(hours=8))))), 1)

    def test_missing_source_time_is_not_inferred_from_publication_and_excluded_from_known_at(self):
        identifier = self.store.save(make_bundle(retrieved_at=None))
        payload = self.store.load(identifier)
        self.assertEqual(payload["source_retrieval"]["unknown_count"], 2)
        self.assertIsNone(payload["source_retrieval"]["latest_at"])
        self.assertEqual(self.store.list(known_at=OBSERVED), ())
        self.assert_error("snapshot_not_found", self.store.load, identifier, known_at=OBSERVED)

    def test_naive_invalid_future_source_times_are_rejected_without_partial_writes(self):
        for value, code in (("not-a-time", "invalid_snapshot_timestamp"),
                            ("2026-09-26T03:00:00", "invalid_snapshot_timestamp"),
                            ((OBSERVED + timedelta(seconds=1)).isoformat(), "snapshot_source_time_in_future")):
            with self.subTest(value=value):
                self.assert_error(code, self.store.save, make_bundle(retrieved_at=value))
                self.assertEqual(self.store.list(), ())
        self.store.save(self.bundle)
        self.assertEqual(len(self.store.list()), 1)

    def test_known_at_only_accepts_aware_datetimes(self):
        identifier = self.store.save(self.bundle)
        for value in ("2026-10-25T12:00:00+00:00", date(2026, 10, 25), OBSERVED.replace(tzinfo=None), 0, True):
            with self.subTest(value=value):
                self.assert_error("invalid_known_at", self.store.load, identifier, known_at=value)
                self.assert_error("invalid_known_at", self.store.list, known_at=value)

    def test_list_is_bounded_ordered_metadata_without_document_text(self):
        first = self.store.save(self.bundle)
        with patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED + timedelta(seconds=1)):
            second = self.store.save(make_bundle(marker="101亿元"))
        metadata = self.store.list(limit=1)
        self.assertEqual([item["snapshot_id"] for item in metadata], [second])
        self.assertEqual([item["snapshot_id"] for item in self.store.list()], [second, first])
        self.assertEqual(metadata[0]["evidence_count"], 1)
        self.assertEqual(metadata[0]["technical_snapshots"], 0)
        self.assertNotIn("companies", metadata[0])
        self.assertNotIn("101亿元", json.dumps(metadata))

    def test_invalid_identifiers_and_limits_do_not_become_sql(self):
        self.store.save(self.bundle)
        for value in ("' OR 1=1 --", "../snapshot", "a" * 63, "A" * 64, None, 5, b"a" * 64):
            with self.subTest(value=value):
                self.assert_error("invalid_snapshot_id", self.store.load, value)
        self.assert_error("snapshot_not_found", self.store.load, "a" * 64)
        for value in (0, -1, 101, True, 1.0, "1; DROP TABLE research_snapshots", None):
            with self.subTest(limit=value):
                self.assert_error("invalid_snapshot_limit", self.store.list, value)
        self.assertEqual(len(self.store.list()), 1)

    def test_payload_and_metadata_corruption_are_detected_on_load(self):
        changes = (("payload", b"{}"), ("digest", "0" * 64), ("schema_version", 2),
                   ("observed_at", "2025-01-01T00:00:00+00:00"),
                   ("source_latest_at", None), ("unknown_count", 99))
        for column, value in changes:
            with self.subTest(column=column):
                path = self.path.parent / f"corrupt-{column}.sqlite3"
                with ResearchSnapshotStore(path) as store:
                    identifier = store.save(self.bundle)
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute(f"UPDATE research_snapshots SET {column} = ? WHERE snapshot_id = ?",
                                           (value, identifier))
                    self.assert_error("snapshot_corrupt", store.load, identifier)

    def test_corrupted_existing_content_is_not_silently_replaced(self):
        identifier = self.store.save(self.bundle)
        self.update(identifier, "digest", "0" * 64)
        self.assert_error("snapshot_corrupt", self.store.save, self.bundle)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT digest FROM research_snapshots").fetchone()[0], "0" * 64)

    def test_recomputed_digest_cannot_hide_content_identity_or_envelope_version_changes(self):
        for field, value in (("version", "research-snapshot-v99"), ("trading_enabled", True),
                             ("snapshot_policy", "strict-pit"), ("snapshot_id", "b" * 64)):
            with self.subTest(field=field):
                identifier = self.store.save(make_bundle(marker=field))
                payload = self.store.load(identifier)
                payload[field] = value
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.update(identifier, "payload", raw)
                self.update(identifier, "digest", hashlib.sha256(raw).hexdigest())
                self.assert_error("snapshot_corrupt", self.store.load, identifier)

    def test_duplicate_json_keys_and_nan_are_corrupt_even_with_matching_digest(self):
        for raw in (b'{"version":"a","version":"b"}', b'{"value":NaN}'):
            with self.subTest(raw=raw):
                identifier = self.store.save(make_bundle(marker=str(raw)))
                self.update(identifier, "payload", raw)
                self.update(identifier, "digest", hashlib.sha256(raw).hexdigest())
                self.assert_error("snapshot_corrupt", self.store.load, identifier)

    def test_payload_limit_and_nonfinite_values_do_not_leave_rows(self):
        self.assert_error("snapshot_too_large", self.store.save, make_bundle(marker="x" * MAX_SNAPSHOT_BYTES))
        for value in (float("nan"), float("inf")):
            item = replace(self.bundle.companies[0].evidence[0], display=value)
            invalid = replace(self.bundle, companies=(replace(self.bundle.companies[0], evidence=(item,)),))
            self.assert_error("invalid_snapshot_payload", self.store.save, invalid)
        self.assertEqual(self.store.list(), ())

    def test_payload_larger_than_two_megabytes_is_supported_within_new_limit(self):
        marker = "资料" * 400_000
        identifier = self.store.save(make_bundle(marker=marker))
        self.assertEqual(self.store.load(identifier)["companies"][0]["evidence"][0]["display"], marker)

    def test_wrong_company_scope_duplicate_ids_and_untrusted_fields_are_rejected(self):
        company = self.bundle.companies[0]
        variants = (
            replace(self.bundle, plan=replace(self.bundle.plan, status="needs_clarification")),
            replace(self.bundle, plan=replace(self.bundle.plan, securities=(Security("600000.SH", "浦发银行"),))),
            replace(self.bundle, companies=()),
            replace(self.bundle, companies=(replace(company, instrument_id="600000.SH"),)),
            replace(self.bundle, companies=(replace(company, evidence=company.evidence * 2),)),
            replace(self.bundle, companies=(replace(company, evidence=(replace(company.evidence[0], instrument_id="600000.SH"),)),)),
            replace(self.bundle, companies=(replace(company, coverage=({"source": "fixture", "raw_question": PRIVATE},)),)),
            replace(self.bundle, companies=(replace(company, risk=object()),)),
        )
        for bundle in variants:
            with self.subTest(bundle=bundle):
                self.assert_error("invalid_snapshot_bundle", self.store.save, bundle)
        self.assertEqual(self.store.list(), ())

    def test_technical_snapshot_must_match_checked_plan_scope(self):
        bundle = make_bundle(technical=True)
        for changes in ({"source": "tencent"}, {"start_date": START - timedelta(days=1)},
                        {"end_date": END + timedelta(days=1)}):
            with self.subTest(changes=changes):
                invalid = replace(bundle, plan=replace(bundle.plan, **changes))
                self.assert_error("invalid_snapshot_bundle", self.store.save, invalid)
        company = bundle.companies[0]
        facts = tuple(replace(fact, value=999) if fact.field == "close_price" and fact.trade_date == END else fact
                      for fact in company.technical.facts)
        invalid = replace(bundle, companies=(replace(company, technical=replace(company.technical, facts=facts)),))
        self.assert_error("invalid_snapshot_bundle", self.store.save, invalid)

    def test_one_instance_serializes_multithreaded_saves_reads_and_lists(self):
        def work(index):
            identifier = self.store.save(make_bundle(marker=f"{index % 3}亿元"))
            self.assertEqual(self.store.load(identifier)["snapshot_id"], identifier)
            self.assertGreater(len(self.store.list()), 0)
            return identifier
        with ThreadPoolExecutor(max_workers=6) as pool:
            identifiers = tuple(pool.map(work, range(30)))
        self.assertEqual(len(set(identifiers)), 3)
        self.assertEqual(len(self.store.list()), 3)

    def test_two_connections_save_same_content_atomically(self):
        with ResearchSnapshotStore(self.path) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(store.save, self.bundle) for store in (self.store, second)]
                identifiers = [future.result() for future in futures]
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_concurrent_first_open_creates_one_schema_and_idempotent_record(self):
        path = self.path.parent / "concurrent-open.sqlite3"
        def work(_):
            with ResearchSnapshotStore(path, clock=lambda: OBSERVED) as store:
                return store.save(self.bundle)
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = tuple(pool.map(work, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        with ResearchSnapshotStore(path) as reopened:
            self.assertEqual(len(reopened.list()), 1)

    def test_injected_clock_sets_observation_and_invalid_clock_is_rejected(self):
        observed = OBSERVED + timedelta(days=1)
        with ResearchSnapshotStore(":memory:", clock=lambda: observed) as store:
            payload = store.load(store.save(self.bundle))
        self.assertEqual(payload["observed_at"], observed.isoformat(timespec="microseconds"))
        self.assert_error("invalid_snapshot_clock", ResearchSnapshotStore, ":memory:", clock=5)
        with ResearchSnapshotStore(":memory:", clock=lambda: observed.replace(tzinfo=None)) as store:
            self.assert_error("invalid_snapshot_timestamp", store.save, self.bundle)
            self.assertEqual(store.list(), ())

    def test_context_manager_and_closed_operations(self):
        with ResearchSnapshotStore(":memory:") as store:
            identifier = store.save(self.bundle)
        store.close()
        for function, args in ((store.save, (self.bundle,)), (store.load, (identifier,)), (store.list, ())):
            self.assert_error("snapshot_store_closed", function, *args)

    def test_unknown_database_version_tables_and_invalid_files_are_preserved(self):
        for kind in ("version", "table", "not-sqlite"):
            with self.subTest(kind=kind):
                path = self.path.parent / f"unknown-{kind}"
                if kind == "not-sqlite":
                    path.write_bytes(b"private-not-a-database")
                else:
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("PRAGMA user_version = 99" if kind == "version"
                                           else "CREATE TABLE unrelated (value TEXT)")
                before = path.read_bytes()
                expected = "snapshot_store_unavailable" if kind == "not-sqlite" else "snapshot_schema_mismatch"
                self.assert_error(expected, ResearchSnapshotStore, path)
                self.assertEqual(path.read_bytes(), before)

    def test_same_named_table_with_wrong_column_types_is_not_treated_as_our_schema(self):
        path = self.path.parent / "wrong-schema.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE research_snapshots (snapshot_id INTEGER PRIMARY KEY, "
                "schema_version INTEGER NOT NULL, observed_at TEXT NOT NULL, source_latest_at TEXT, "
                "unknown_count INTEGER NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL)")
            connection.execute("PRAGMA user_version = 1")
        before = path.read_bytes()
        self.assert_error("snapshot_schema_mismatch", ResearchSnapshotStore, path)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_parent_and_invalid_path_return_fixed_errors(self):
        self.assert_error("snapshot_store_unavailable", ResearchSnapshotStore,
                          self.path.parent / "missing" / "store.sqlite3")
        for path in (None, "", " ", 1):
            self.assert_error("invalid_snapshot_path", ResearchSnapshotStore, path)


if __name__ == "__main__":
    unittest.main()
