"""Opt-in SQLite evidence snapshots, observed locally rather than reconstructed as historical PIT."""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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
from trading_companion.research_documents import DocumentError, coverage_code, document_paragraph_limit
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


def _document_text(value, limit):
    return type(value) is str and 0 < len(value) <= limit and all(
        char.isprintable() or char in "\n\t" for char in value)


def _document_hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _document_code(value):
    return type(value) is str and re.fullmatch(r"documents?_[a-z_]{1,80}", value) is not None


def _validate_document_data(data):
    """Archive only bounded public excerpts, tied to their original same-stock titles."""
    supplemental = data.get("supplemental_evidence", [])
    documents = data.get("documents")
    if type(supplemental) is not list or len(supplemental) > 6:
        raise SnapshotStoreError("invalid_snapshot_bundle")
    originals = [item for company in data["companies"] for item in company["evidence"]]
    if any(item.get("content_kind") == "body_excerpt" or item.get("document") is not None for item in originals):
        raise SnapshotStoreError("invalid_snapshot_bundle")
    if documents is None:
        if supplemental or "documents" in data:
            raise SnapshotStoreError("invalid_snapshot_bundle")
        return
    if (type(documents) is not dict or documents.keys() != {"version", "status", "code", "items"}
            or documents["version"] != "research-documents-v1"
            or documents["status"] not in ("available", "partial", "unavailable", "skipped")
            or not _document_code(documents["code"])
            or type(documents["items"]) is not list or len(documents["items"]) > 3):
        raise SnapshotStoreError("invalid_snapshot_bundle")
    by_id = {item["id"]: item for item in originals}
    instruments = {company["instrument_id"] for company in data["companies"]}
    base_keys = {"instrument_id", "kind", "parent_title_id", "title", "url", "source", "status", "code"}
    available_keys = {"document_id", "sha256", "extractor_version", "extracted_characters", "paragraph_count",
                      "excerpts", "retrieved_at", "published_date", "publication_precision"}
    coverage_keys = {"complete", "omitted_tables", "page_count", "private_use_characters_replaced",
                     "omitted_trailing_characters"}
    summaries, expected_excerpts, parents = {}, {}, set()
    for item in documents["items"]:
        if type(item) is not dict or item.get("status") not in ("available", "unavailable"):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        available = item["status"] == "available"
        allowed_keys = (base_keys | available_keys, base_keys | available_keys | coverage_keys) if available else (base_keys,)
        if (item.keys() not in allowed_keys
                or item["instrument_id"] not in instruments or item["kind"] not in ("news", "announcements")
                or not _document_text(item["parent_title_id"], 512) or item["parent_title_id"] in parents
                or not _document_text(item["title"], 1000) or not _document_text(item["url"], 2048)
                or not _document_text(item["source"], 80) or not _document_code(item["code"])):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        parent = by_id.get(item["parent_title_id"])
        if (parent is None or parent.get("content_kind") != "title"
                or any(item[key] != parent.get(key) for key in ("instrument_id", "kind", "source", "url"))
                or item["title"] != (parent.get("raw_value") if type(parent.get("raw_value")) is str else parent["display"])):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        parents.add(item["parent_title_id"])
        if not available:
            continue
        if "complete" in item:
            try:
                expected_code = coverage_code(item["kind"], **{key: item[key] for key in coverage_keys})
            except DocumentError:
                raise SnapshotStoreError("invalid_snapshot_bundle") from None
            if item["code"] != expected_code:
                raise SnapshotStoreError("invalid_snapshot_bundle")
        elif item["code"] != "document_read":
            # Legacy documents cannot claim a newly introduced limitation without its fields.
            raise SnapshotStoreError("invalid_snapshot_bundle")
        expected_extractor = ("sina-artibody-v3" if "complete" in item else "sina-artibody-v2") if item["kind"] == "news" else (
            "eastmoney-announcement-body-v2" if "complete" in item else "eastmoney-announcement-body-v1")
        maximum_characters = 16000 if item["kind"] == "news" else 32000
        if (type(item["document_id"]) is not str
                or re.fullmatch(r"document:[0-9a-f]{64}", item["document_id"]) is None
                or item["document_id"] in summaries
                or not _document_hash(item["sha256"]) or item["extractor_version"] != expected_extractor
                or type(item["extracted_characters"]) is not int or not 1 <= item["extracted_characters"] <= maximum_characters
                or item["extracted_characters"] + item.get("omitted_trailing_characters", 0) > maximum_characters
                or type(item["paragraph_count"]) is not int
                or not 1 <= item["paragraph_count"] <= document_paragraph_limit(item["kind"])
                or type(item["excerpts"]) is not list or len(item["excerpts"]) > 2
                or item["publication_precision"] not in ("date", "datetime")
                or item["publication_precision"] != parent["publication_precision"]
                or item["published_date"] != parent["date"]):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        publication = date.fromisoformat(item["published_date"])
        if publication.isoformat() != item["published_date"]:
            raise SnapshotStoreError("invalid_snapshot_bundle")
        # The extractor's ID also binds a precise article time that a date-only
        # title/public projection may omit. Treat it as an opaque, unique ID;
        # validate archived associations and each excerpt's own hash below.
        retrieved = _timestamp(item["retrieved_at"])
        if publication > datetime.fromisoformat(retrieved).astimezone(timezone(timedelta(hours=8))).date():
            raise SnapshotStoreError("invalid_snapshot_bundle")
        if parent.get("retrieved_at") is not None and retrieved < _timestamp(parent["retrieved_at"]):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        summaries[item["document_id"]] = item
        for excerpt in item["excerpts"]:
            if (type(excerpt) is not dict or excerpt.keys() != {"paragraph_index", "text", "sha256"}
                    or type(excerpt["paragraph_index"]) is not int
                    or not 1 <= excerpt["paragraph_index"] <= item["paragraph_count"]
                    or not _document_text(excerpt["text"], 480)
                    or len(excerpt["text"]) > item["extracted_characters"]
                    or not _document_hash(excerpt["sha256"])
                    or _hash(excerpt["text"].encode("utf-8")) != excerpt["sha256"]):
                raise SnapshotStoreError("invalid_snapshot_bundle")
            key = (item["document_id"], excerpt["paragraph_index"], excerpt["sha256"])
            if key in expected_excerpts:
                raise SnapshotStoreError("invalid_snapshot_bundle")
            expected_excerpts[key] = excerpt
    if any(item.get("complete") is False for item in summaries.values()) and documents["status"] != "partial":
        raise SnapshotStoreError("invalid_snapshot_bundle")
    seen_ids, seen_excerpts = set(by_id), set()
    document_keys = {"document_id", "parent_title_id", "sha256", "paragraph_index", "excerpt_sha256", "extractor_version"}
    evidence_keys = {"id", "instrument_id", "kind", "label", "display", "source", "date", "url", "content_kind",
                     "published_at", "publication_precision", "report_period", "data_authenticity", "temporal_scope",
                     "dependencies", "retrieved_at", "raw_value", "unit", "document"}
    for item in supplemental:
        metadata = item.get("document") if type(item) is dict else None
        if (type(item) is not dict or item.keys() != evidence_keys or type(metadata) is not dict
                or metadata.keys() not in (document_keys, document_keys | {"document_complete"})
                or not _document_text(item["id"], 512)
                or item["id"] in seen_ids or item["content_kind"] != "body_excerpt"
                or item["raw_value"] is not None or item["unit"] != "text"
                or not _document_text(item["label"], 200) or not _document_text(item["display"], 480)
                or type(metadata["paragraph_index"]) is not int):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        document = summaries.get(metadata["document_id"])
        key = (metadata["document_id"], metadata["paragraph_index"], metadata["excerpt_sha256"])
        excerpt = expected_excerpts.get(key)
        if (document is None or key in seen_excerpts
                or ("complete" in document) != ("document_complete" in metadata)
                or "document_complete" in metadata and (type(metadata["document_complete"]) is not bool
                    or metadata["document_complete"] != document["complete"])
                or not 1 <= metadata["paragraph_index"] <= document["paragraph_count"]
                or any(metadata[key] != document[key] for key in ("parent_title_id", "sha256", "extractor_version"))
                or not _document_hash(metadata["excerpt_sha256"])
                or _hash(item["display"].encode("utf-8")) != metadata["excerpt_sha256"]
                or excerpt is not None and item["display"] != excerpt["text"]
                or any(item[key] != document[key] for key in ("instrument_id", "kind", "source", "url", "retrieved_at", "publication_precision"))
                or item["date"] != document["published_date"] or item["dependencies"] != [metadata["parent_title_id"]]):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        parent = by_id[metadata["parent_title_id"]]
        if any(item[key] != parent.get(key) for key in ("published_at", "data_authenticity", "temporal_scope")):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        seen_ids.add(item["id"])
        seen_excerpts.add(key)
    if not expected_excerpts.keys() <= seen_excerpts:
        raise SnapshotStoreError("invalid_snapshot_bundle")


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
        if company.risk is not None and company.risk.method == 'atr_multiple_horizon_long_v2':
            from trading_companion.horizon_trade_plan import build_horizon_risk_scenario, weekly_atr_evidence
            if (technical is None or company.risk.horizon != bundle.plan.objective.horizon
                    or company.risk != build_horizon_risk_scenario(technical, expected_request=technical.request,
                        horizon=bundle.plan.objective.horizon)):
                raise SnapshotStoreError('invalid_snapshot_bundle')
            if company.risk.status == 'available' and company.risk.atr_timeframe == 'weekly':
                weekly, _ = weekly_atr_evidence(technical, expected_request=technical.request)
                if weekly is None or [e for e in company.evidence if e.id == weekly.id] != [weekly]:
                    raise SnapshotStoreError('invalid_snapshot_bundle')
        if company.trade_plans:
            from trading_companion.trade_plan import build_trade_plans, method_for_plan, unavailable_code_for_plan
            from trading_companion.research_decision import excludes_plan
            expected = build_trade_plans(technical if technical is not None else company.instrument_id,
                expected_request=technical.request if technical is not None else None,
                unavailable_code=unavailable_code_for_plan(bundle.plan),
                horizon=bundle.plan.objective.horizon, method=method_for_plan(bundle.plan))
            if company.trade_plans != expected:
                raise SnapshotStoreError("invalid_snapshot_bundle")
            if any(p.status == 'available' and p.atr_timeframe == 'weekly' for p in expected):
                from trading_companion.horizon_trade_plan import weekly_atr_evidence
                weekly, _ = weekly_atr_evidence(technical, expected_request=technical.request)
                if weekly is None or [e for e in company.evidence if e.id == weekly.id] != [weekly]:
                    raise SnapshotStoreError('invalid_snapshot_bundle')
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
        if company.historical_replay is not None:
            from trading_companion.historical_replay import HistoricalReplay, build_historical_replay
            replay = company.historical_replay
            if (type(replay) is not HistoricalReplay or technical is None
                    or replay != build_historical_replay(technical, expected_request=technical.request, config=replay.config)):
                raise SnapshotStoreError("invalid_snapshot_bundle")
            payload["historical_replay"] = replay.to_dict()
        companies.append(payload)
    # ProductPlan.to_dict intentionally omits user_question, model_response and internal pending context.
    data = {"version": VERSION, "plan": bundle.plan.to_dict(), "companies": companies,
            "trading_enabled": False, "snapshot_policy": SNAPSHOT_POLICY}
    if bundle.supplemental_evidence:
        if (type(bundle.supplemental_evidence) is not tuple
                or any(type(item) is not ProductEvidence for item in bundle.supplemental_evidence)):
            raise SnapshotStoreError("invalid_snapshot_bundle")
        data["supplemental_evidence"] = [item.to_dict() for item in bundle.supplemental_evidence]
    if bundle.documents is not None:
        data["documents"] = bundle.documents
    _validate_document_data(data)
    return data


def _source_boundary(data, observed_at):
    if data['version'] in ('index-evidence-snapshot-v1', 'index-evidence-snapshot-v2'):
        from trading_companion.index_snapshot import validate_snapshot
        result = validate_snapshot({k: v for k, v in data.items() if k not in ('trading_enabled', 'snapshot_policy')})
        retrieved = _timestamp(result.retrieved_at)
        if retrieved > observed_at:
            raise SnapshotStoreError('snapshot_source_time_in_future')
        return dict(policy='source_retrieval_times_not_publication_dates', latest_at=retrieved, unknown_count=0,
            sources=[dict(instrument_id=result.request.instrument_id, source=result.request.source,
                records=len(result.bars), earliest_at=retrieved, latest_at=retrieved, unknown_count=0)])
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
    for item in data.get("supplemental_evidence", ()):
        groups[item["instrument_id"], item["source"]].append(item.get("retrieved_at"))
    for item in (data.get("documents") or {}).get("items", ()):
        if item["status"] == "available":
            groups[item["instrument_id"], item["source"]].append(item["retrieved_at"])
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
    if payload['version'] in ('index-evidence-snapshot-v1', 'index-evidence-snapshot-v2'):
        raw = payload['index_snapshot']
        return dict(version=payload['version'], snapshot_id=payload['snapshot_id'], observed_at=payload['observed_at'],
            source_retrieval=payload['source_retrieval'], instrument_kind='index',
            instrument_id=raw['parameters']['instrument_id'], as_of=raw['parameters']['as_of_date'],
            snapshot_policy=SNAPSHOT_POLICY, evidence_count=len(payload['evidence']), technical_snapshots=1)
    plan = payload["plan"]
    return {"version": VERSION, "snapshot_id": payload["snapshot_id"], "observed_at": payload["observed_at"],
            "source_retrieval": payload["source_retrieval"], "securities": plan["securities"],
            "kinds": plan["kinds"], "as_of": plan["as_of"], "snapshot_policy": SNAPSHOT_POLICY,
            "evidence_count": (sum(len(company["evidence"]) for company in payload["companies"])
                               + len(payload.get("supplemental_evidence", ()))),
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
            index_payload = type(payload) is dict and payload.get('version') in ('index-evidence-snapshot-v1', 'index-evidence-snapshot-v2')
            expected_keys = ({'version', 'index_snapshot', 'index_snapshot_sha256', 'evidence', 'limitations', 'trading_enabled', 'snapshot_policy',
                              'snapshot_id', 'observed_at', 'source_retrieval'} if index_payload else {
                    "version", "plan", "companies", "trading_enabled", "snapshot_policy",
                    "snapshot_id", "observed_at", "source_retrieval"})
            if index_payload and payload['version'] == 'index-evidence-snapshot-v2':
                expected_keys |= {'selected_direction', 'scenario_plan'}
            if not index_payload and type(payload) is dict:
                expected_keys |= payload.keys() & {"supplemental_evidence", "documents"}
            if (type(payload) is not dict or payload.keys() != expected_keys
                    or not index_payload and payload["version"] != VERSION or payload["snapshot_id"] != snapshot_id
                    or payload["snapshot_policy"] != SNAPSHOT_POLICY or payload["trading_enabled"] is not False
                    or _timestamp(payload["observed_at"]) != row[2]):
                raise SnapshotStoreError("snapshot_corrupt")
            data = {key: value for key, value in payload.items()
                    if key not in ("snapshot_id", "observed_at", "source_retrieval")}
            if not index_payload:
                _validate_document_data(data)
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
        return self._save_data(data)

    def save_index(self, result, *, direction=None) -> str:
        from trading_companion.index_snapshot import snapshot_data
        try:
            data = dict(**snapshot_data(result, direction=direction), trading_enabled=False, snapshot_policy=SNAPSHOT_POLICY)
            return self._save_data(data)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
            raise SnapshotStoreError('invalid_snapshot_bundle') from None

    def _save_data(self, data):
        snapshot_id = _hash(_encode(data))
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
