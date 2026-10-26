"""Opt-in SQLite evidence snapshots, observed locally rather than reconstructed as historical PIT."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

from trading_companion.multi_contracts import MultiIndicatorResult
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle
from trading_companion.research_report import build_research_report
from trading_companion.risk_research import RiskScenario
from trading_companion.security_directory import Security


VERSION = "research-snapshot-v1"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
SNAPSHOT_POLICY = "local_observation_and_source_retrieval_times_not_versioned_point_in_time"
_COLUMNS = ("snapshot_id", "schema_version", "observed_at", "source_latest_at", "unknown_count", "digest", "payload")
_COLUMN_TYPES = ("TEXT", "INTEGER", "TEXT", "TEXT", "INTEGER", "TEXT", "BLOB")
_COVERAGE_KEYS = {"kind", "status", "code", "message", "source", "retrieved_at", "skipped_count", "bounded_snapshot"}


class SnapshotStoreError(ValueError):
    """A fixed error code; filesystem paths, SQLite errors and document excerpts are never exposed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise SnapshotStoreError("invalid_snapshot_timestamp") from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotStoreError("invalid_snapshot_timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _known_at(value):
    if value is None:
        return None
    if type(value) is not datetime:
        raise SnapshotStoreError("invalid_known_at")
    try:
        return _timestamp(value)
    except SnapshotStoreError:
        raise SnapshotStoreError("invalid_known_at") from None


def _encode(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
        raise SnapshotStoreError("invalid_snapshot_payload") from None
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise SnapshotStoreError("snapshot_too_large")
    return encoded


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotStoreError("snapshot_corrupt")
        result[key] = value
    return result


def _constant(value):
    raise SnapshotStoreError("snapshot_corrupt")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SnapshotStoreError("invalid_snapshot_id")
    return value


def _public_data(bundle):
    if (type(bundle) is not ProductResearchBundle or type(bundle.plan) is not ProductPlan
            or bundle.plan.status != "ready" or not 1 <= len(bundle.companies) <= 3
            or tuple((item.instrument_id, item.name) for item in bundle.plan.securities)
            != tuple((item.instrument_id, item.name) for item in bundle.companies)):
        raise SnapshotStoreError("invalid_snapshot_bundle")
    companies = []
    identifiers, all_evidence_ids = set(), set()
    for company in bundle.companies:
        if type(company) is not ProductCompany:
            raise SnapshotStoreError("invalid_snapshot_bundle")
        Security(company.instrument_id, company.name)
        if company.instrument_id in identifiers:
            raise SnapshotStoreError("invalid_snapshot_bundle")
        identifiers.add(company.instrument_id)
        for item in company.evidence:
            if (type(item) is not ProductEvidence or item.instrument_id != company.instrument_id
                    or item.id in all_evidence_ids or type(item.id) is not str or not item.id
                    or item.kind not in ("profile", "financial", "news", "announcements", "technical", "risk")):
                raise SnapshotStoreError("invalid_snapshot_bundle")
            all_evidence_ids.add(item.id)
        if any(type(item) is not dict or item.keys() - _COVERAGE_KEYS for item in company.coverage):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        if company.risk is not None and type(company.risk) is not RiskScenario:
            raise SnapshotStoreError("invalid_snapshot_bundle")
        technical = company.technical
        if technical is not None:
            if (type(technical) is not MultiIndicatorResult
                    or technical.request.instrument_id != company.instrument_id
                    or technical.request.start_date != bundle.plan.start_date
                    or technical.request.as_of_date != bundle.plan.end_date
                    or technical.request.indicators != bundle.plan.indicators
                    or technical.request.source != bundle.plan.source
                    or build_research_report(technical, expected_request=technical.request).status == "blocked"):
                raise SnapshotStoreError("invalid_snapshot_bundle")
        payload = company.to_dict()
        payload["technical"] = technical.to_dict() if technical is not None else None
        if company.technical_insights is not None:
            from trading_companion.technical_insights import TechnicalInsights, build_technical_insights
            insights = company.technical_insights
            if (type(insights) is not TechnicalInsights or technical is None
                    or insights.request_id != technical.request.request_id
                    or insights.instrument_id != company.instrument_id):
                raise SnapshotStoreError("invalid_snapshot_bundle")
            if insights != build_technical_insights(technical, expected_request=technical.request):
                raise SnapshotStoreError("invalid_snapshot_bundle")
            payload["technical_insights"] = insights.to_dict()
        companies.append(payload)
    # ProductPlan.to_dict intentionally omits user_question, model_response and internal pending context.
    return {"version": VERSION, "plan": bundle.plan.to_dict(), "companies": companies,
            "trading_enabled": False, "snapshot_policy": SNAPSHOT_POLICY}


def _source_boundary(data, observed_at):
    groups = defaultdict(list)
    for company in data["companies"]:
        instrument = company["instrument_id"]
        for item in (*company["evidence"], *company["coverage"]):
            if type(item.get("source")) is not str or not item["source"]:
                raise SnapshotStoreError("invalid_snapshot_bundle")
            groups[instrument, item["source"]].append(item.get("retrieved_at"))
        technical = company["technical"]
        if technical is not None:
            groups[instrument, technical["source"]].append(technical["retrieved_at"])
    sources, all_times, unknown_count = [], [], 0
    for (instrument, source), values in sorted(groups.items()):
        times = [_timestamp(value) for value in values if value is not None]
        if any(value > observed_at for value in times):
            raise SnapshotStoreError("snapshot_source_time_in_future")
        missing = sum(value is None for value in values)
        unknown_count += missing
        all_times.extend(times)
        sources.append({"instrument_id": instrument, "source": source, "records": len(values),
                        "earliest_at": min(times) if times else None, "latest_at": max(times) if times else None,
                        "unknown_count": missing})
    return {"earliest_at": min(all_times) if all_times else None,
            "latest_at": max(all_times) if all_times else None,
            "unknown_count": unknown_count, "sources": sources}


def _metadata(payload):
    plan = payload["plan"]
    return {"version": VERSION, "snapshot_id": payload["snapshot_id"], "observed_at": payload["observed_at"],
            "source_retrieval": payload["source_retrieval"], "securities": plan["securities"],
            "kinds": plan["kinds"], "as_of": plan["as_of"], "snapshot_policy": SNAPSHOT_POLICY,
            "evidence_count": sum(len(company["evidence"]) for company in payload["companies"]),
            "technical_snapshots": sum(company["technical"] is not None for company in payload["companies"])}


class ResearchSnapshotStore:
    """One fixed-schema ledger; only an explicit constructor opens or creates a database.

    ``known_at`` requires a local observation and every recorded retrieval time to
    precede the query. Missing source retrieval times are not inferred from a
    document's publication date. Hashes detect corruption, not malicious rewriting
    by someone who controls the database or the authenticity of upstream data.
    """

    def __init__(self, path, *, clock=None):
        self._lock = RLock()
        self._closed = True
        self._clock = _utc_now if clock is None else clock
        if not callable(self._clock):
            raise SnapshotStoreError("invalid_snapshot_clock")
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise SnapshotStoreError("invalid_snapshot_path")
        try:
            self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            with self._connection:
                # Serialize first-time schema creation across independently opened stores.
                self._connection.execute("BEGIN IMMEDIATE")
                objects = self._connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects and version == 0:
                    self._connection.execute("""CREATE TABLE research_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        observed_at TEXT NOT NULL,
                        source_latest_at TEXT,
                        unknown_count INTEGER NOT NULL,
                        digest TEXT NOT NULL,
                        payload BLOB NOT NULL CHECK (length(payload) <= 16777216)
                    )""")
                    self._connection.execute("PRAGMA user_version = 1")
                elif version != 1 or objects != [("table", "research_snapshots")]:
                    raise SnapshotStoreError("snapshot_schema_mismatch")
                columns = self._connection.execute("PRAGMA table_info(research_snapshots)").fetchall()
                if (tuple(item[1] for item in columns) != _COLUMNS
                        or tuple(item[2] for item in columns) != _COLUMN_TYPES
                        or tuple(item[3] for item in columns) != (0, 1, 1, 0, 1, 1, 1)
                        or tuple(item[5] for item in columns) != (1, 0, 0, 0, 0, 0, 0)):
                    raise SnapshotStoreError("snapshot_schema_mismatch")
            self._connection.execute("PRAGMA trusted_schema = OFF")
            self._closed = False
        except (sqlite3.Error, OSError):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise SnapshotStoreError("snapshot_store_unavailable") from None
        except SnapshotStoreError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    def _ensure_open(self):
        if self._closed:
            raise SnapshotStoreError("snapshot_store_closed")

    def _read(self, snapshot_id):
        row = self._connection.execute(
            "SELECT snapshot_id, schema_version, observed_at, source_latest_at, unknown_count, digest, length(payload) "
            "FROM research_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise SnapshotStoreError("snapshot_not_found")
        if row[1] != 1 or type(row[6]) is not int or not 0 < row[6] <= MAX_SNAPSHOT_BYTES:
            raise SnapshotStoreError("snapshot_corrupt")
        raw = self._connection.execute("SELECT payload FROM research_snapshots WHERE snapshot_id = ?",
                                       (snapshot_id,)).fetchone()[0]
        if type(raw) is not bytes or _hash(raw) != row[5]:
            raise SnapshotStoreError("snapshot_corrupt")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
            if (type(payload) is not dict or payload.keys() != {
                    "version", "plan", "companies", "trading_enabled", "snapshot_policy",
                    "snapshot_id", "observed_at", "source_retrieval"}
                    or payload["version"] != VERSION or payload["snapshot_id"] != snapshot_id
                    or payload["snapshot_policy"] != SNAPSHOT_POLICY or payload["trading_enabled"] is not False
                    or _timestamp(payload["observed_at"]) != row[2]):
                raise SnapshotStoreError("snapshot_corrupt")
            data = {key: value for key, value in payload.items()
                    if key not in ("snapshot_id", "observed_at", "source_retrieval")}
            if (_hash(_encode(data)) != snapshot_id
                    or _source_boundary(data, row[2]) != payload["source_retrieval"]
                    or row[3] != payload["source_retrieval"]["latest_at"]
                    or row[4] != payload["source_retrieval"]["unknown_count"]):
                raise SnapshotStoreError("snapshot_corrupt")
            return payload
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
            raise SnapshotStoreError("snapshot_corrupt") from None

    def save(self, bundle: ProductResearchBundle) -> str:
        try:
            data = _public_data(bundle)
            body = _encode(data)
        except SnapshotStoreError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError, OverflowError):
            raise SnapshotStoreError("invalid_snapshot_bundle") from None
        snapshot_id = _hash(body)
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    exists = self._connection.execute(
                        "SELECT snapshot_id FROM research_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
                    if exists:
                        self._read(snapshot_id)
                        return snapshot_id
                    observed = _timestamp(self._clock())
                    boundary = _source_boundary(data, observed)
                    payload = {**data, "snapshot_id": snapshot_id, "observed_at": observed, "source_retrieval": boundary}
                    encoded = _encode(payload)
                    self._connection.execute(
                        "INSERT INTO research_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (snapshot_id, 1, observed, boundary["latest_at"], boundary["unknown_count"], _hash(encoded), encoded))
                return snapshot_id
            except sqlite3.Error:
                raise SnapshotStoreError("snapshot_store_unavailable") from None

    def load(self, snapshot_id: str, *, known_at: datetime | None = None) -> dict:
        snapshot_id, cutoff = _identifier(snapshot_id), _known_at(known_at)
        with self._lock:
            self._ensure_open()
            try:
                payload = self._read(snapshot_id)
            except sqlite3.Error:
                raise SnapshotStoreError("snapshot_store_unavailable") from None
            boundary = payload["source_retrieval"]
            if cutoff is not None and (payload["observed_at"] > cutoff or boundary["unknown_count"]
                                       or boundary["latest_at"] is not None and boundary["latest_at"] > cutoff):
                raise SnapshotStoreError("snapshot_not_found")
            return payload

    def list(self, limit: int = 20, *, known_at: datetime | None = None) -> tuple[dict, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SnapshotStoreError("invalid_snapshot_limit")
        cutoff = _known_at(known_at)
        with self._lock:
            self._ensure_open()
            try:
                if cutoff is None:
                    rows = self._connection.execute(
                        "SELECT snapshot_id FROM research_snapshots ORDER BY observed_at DESC, snapshot_id ASC LIMIT ?",
                        (limit,)).fetchall()
                else:
                    rows = self._connection.execute(
                        "SELECT snapshot_id FROM research_snapshots WHERE observed_at <= ? AND unknown_count = 0 "
                        "AND (source_latest_at IS NULL OR source_latest_at <= ?) "
                        "ORDER BY observed_at DESC, snapshot_id ASC LIMIT ?", (cutoff, cutoff, limit)).fetchall()
                return tuple(_metadata(self.load(row[0], known_at=known_at)) for row in rows)
            except sqlite3.Error:
                raise SnapshotStoreError("snapshot_store_unavailable") from None

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self):
        with self._lock:
            self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
