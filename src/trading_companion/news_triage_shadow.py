"""Opt-in bounded public-title snapshots; no model, environment or network access."""

from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import Lock
import unicodedata
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

from trading_companion.company_research_contracts import SHANGHAI, aware
from trading_companion.company_news_sina import _safe_article_url
from trading_companion.turn_control import TurnStopped, checkpoint


ROOT = Path(__file__).resolve().parents[2]
CAPTURE_ROOT = ROOT / ".agentbus/tmp/news-triage-shadow"
COLLECTION_ROOT = ROOT / ".agentbus/tmp/news-title-collection"
VERSION = "news-title-capture-v1"
MAX_RECORD_BYTES = 64 * 1024
MAX_RUN_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 1000
_SOURCES = {"news": ("sina_company_news",),
            "announcements": ("eastmoney_announcements", "cninfo_announcements")}
_CODE = re.compile(r"[a-zA-Z0-9_:-]{1,128}\Z")


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if not aware(parsed):
        raise ValueError("Title timestamps must be timezone-aware")
    return parsed


def _public_url(item):
    if item.source == "sina_company_news":
        return _safe_article_url(item.url)
    if item.source == "eastmoney_announcements":
        return bool(re.fullmatch(r"https://data\.eastmoney\.com/notices/detail/"
                    + re.escape(item.instrument_id[:6]) + r"/AN[0-9]{12,40}\.html", item.url))
    parts = urlsplit(item.url)
    fields = parse_qs(parts.query, strict_parsing=True)
    return (parts.scheme == "https" and parts.netloc == "www.cninfo.com.cn"
            and parts.path == "/new/disclosure/detail" and not parts.fragment
            and set(fields) == {"stockCode", "announcementId", "orgId"}
            and fields["stockCode"] == [item.instrument_id[:6]]
            and all(len(fields[key]) == 1 and re.fullmatch(r"[0-9]{5,30}", fields[key][0])
                    for key in ("announcementId", "orgId")))


def title_snapshot(bundle, *, captured_at, origin="research"):
    """Whitelist public fields from the already validated, unmodified research bundle."""
    if origin not in ("research", "collection") or not aware(captured_at):
        raise ValueError("Use an explicit public-title snapshot origin and aware time")
    if not 1 <= len(bundle.companies) <= 3:
        raise ValueError("Capture at most three checked companies")
    cutoff = (datetime.combine(bundle.plan.end_date, time.max, SHANGHAI)
              if bundle.plan.explicit_historical else captured_at)
    start = max(bundle.plan.start_date, cutoff.astimezone(SHANGHAI).date() - timedelta(days=366))
    companies, title_count = [], 0
    for company in bundle.companies:
        sections, titles, seen, counts = [], [], set(), {}
        for section in company.coverage:
            kind, source = section.get("kind"), section.get("source")
            if source not in _SOURCES.get(kind, ()):
                continue
            if (section.get("status") not in ("available", "empty", "unavailable", "partial")
                    or type(section.get("code")) is not str or not _CODE.fullmatch(section["code"])):
                raise ValueError("Invalid public source status")
            sections.append({key: section[key] for key in ("kind", "source", "status", "code")})
            if section.get("retrieved_at") is not None:
                _timestamp(section["retrieved_at"])
                sections[-1]["retrieved_at"] = section["retrieved_at"]
        for item in company.evidence:
            if item.content_kind != "title" or item.source not in _SOURCES.get(item.kind, ()):
                continue
            if (item.instrument_id != company.instrument_id or item.temporal_scope != "published_before_cutoff"
                    or type(item.raw_value) is not str or not 0 < len(item.raw_value) <= 600
                    or not item.raw_value.isprintable() or item.id in seen
                    or type(item.url) is not str or len(item.url) > 1000
                    or item.publication_precision not in ("date", "datetime") or not _public_url(item)):
                raise ValueError("Invalid checked title identity or content")
            # Keep the original source timestamp; never invent intraday precision.
            published_date = date.fromisoformat(item.date)
            if not start <= published_date <= cutoff.astimezone(SHANGHAI).date():
                raise ValueError("Title is outside its publication window")
            if item.publication_precision == "datetime":
                published = _timestamp(item.published_at)
                if published > cutoff or published.astimezone(SHANGHAI).date() != published_date:
                    raise ValueError("Title is outside its publication cutoff")
            elif item.published_at is not None or datetime.combine(published_date, time.max, SHANGHAI) > cutoff:
                raise ValueError("Date-only title is not yet available")
            _timestamp(item.retrieved_at)
            counts[item.kind] = counts.get(item.kind, 0) + 1
            if counts[item.kind] > 5:
                raise ValueError("Capture cannot expand source title bounds")
            seen.add(item.id)
            normalized = " ".join(unicodedata.normalize("NFKC", item.raw_value).casefold().split())
            titles.append({"evidence_id": item.id, "instrument_id": item.instrument_id,
                "kind": item.kind, "source": item.source, "title": item.raw_value,
                "title_sha256": sha256(normalized.encode("utf-8")).hexdigest(), "url": item.url,
                "published_date": item.date, "published_at": item.published_at,
                "publication_precision": item.publication_precision, "retrieved_at": item.retrieved_at})
        if sections or titles:
            companies.append({"instrument_id": company.instrument_id, "security_name": company.name,
                              "source_results": sections, "titles": titles})
        title_count += len(titles)
    if not companies:
        return None
    return {"version": VERSION, "origin": origin, "captured_at": captured_at.isoformat(),
            "as_of": cutoff.isoformat(), "publication_start": start.isoformat(),
            "title_count": title_count, "companies": companies,
            "snapshot_policy": "bounded_vendor_titles_not_full_articles_or_versioned_pit",
            "model_calls": 0}


class NewsTitleCapture:
    """One lazy, unique local run; capacity or I/O failure disables further writes."""

    def __init__(self, root=CAPTURE_ROOT, *, max_bytes=MAX_RUN_BYTES, warning=None):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RUN_BYTES:
            raise ValueError("Use a bounded title capture capacity")
        if warning is not None and not callable(warning):
            raise ValueError("Use a local warning callback")
        self.root, self.directory = Path(root), None
        self._max_bytes, self._warning = max_bytes, warning
        self._lock = Lock()
        self._records = self._titles = self._bytes = 0
        self._stop_code = None

    def status(self):
        with self._lock:
            return {"records_written": self._records, "titles_written": self._titles,
                    "bytes_written": self._bytes, "stop_code": self._stop_code}

    def capture(self, bundle, *, captured_at, origin="research"):
        checkpoint()
        with self._lock:
            if self._stop_code is not None:
                return False
            try:
                record = title_snapshot(bundle, captured_at=captured_at, origin=origin)
                if record is None:
                    return False
                body = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
                if len(body) > MAX_RECORD_BYTES:
                    self._stop_code = "capture_record_too_large"
                elif self._records >= MAX_RECORDS or self._bytes + len(body) > self._max_bytes:
                    self._stop_code = "capture_capacity_reached"
                else:
                    checkpoint()
                    if self.directory is None:
                        self.root.mkdir(parents=True, exist_ok=True)
                        directory = self.root / uuid4().hex
                        directory.mkdir()
                        self.directory = directory
                    target = self.directory / f"{self._records + 1:06d}.json"
                    temporary = target.with_suffix(".partial")
                    with temporary.open("xb") as handle:
                        handle.write(body)
                    temporary.rename(target)
                    self._records += 1
                    self._titles += record["title_count"]
                    self._bytes += len(body)
                    checkpoint()
                    return True
            except TurnStopped:
                raise
            except Exception:
                # Public-data logging must never change the research answer or expose raw errors.
                self._stop_code = "capture_unavailable"
            if self._warning is not None:
                try:
                    self._warning()
                except Exception:
                    pass
            return False
