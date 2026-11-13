"""Strict public-title DTOs and opaque bounded cursors; no storage or network I/O."""

from base64 import b64decode, urlsafe_b64encode
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import re

from trading_companion.personal_memory import checked_instrument_id, checked_name, timestamp


VERSION = "personal-memory-notifications-v1"
SOURCES = ("sina_company_news", "eastmoney_announcements")
SOURCE_KINDS = dict(zip(SOURCES, ("news", "announcements")))
PAGE_SIZE = 20
MAX_TITLES = 5
MAX_BATCH_STOCKS = 5
MAX_MESSAGES = 500
MAX_SEEN = 2000
LOOKBACK_DAYS = 30
RETENTION_DAYS = 90
STATUSES = ("not_checked", "available", "empty", "partial", "unavailable")
CODES = frozenset((
    "inbox_not_checked", "inbox_source_available", "inbox_source_empty", "inbox_source_partial",
    "inbox_source_unavailable", "inbox_source_timeout", "inbox_source_cancelled", "inbox_seen_capacity",
    "inbox_store_timeout", "inbox_store_unavailable", "inbox_conflict", "inbox_invalid_request",
    "inbox_invalid_cursor", "inbox_invalid_title", "inbox_not_found", "inbox_corrupt",
    "inbox_cancelled", "inbox_deadline", "inbox_closed", "inbox_request_conflict", "inbox_request_retired",
    "inbox_snapshot_changed", "inbox_revision_conflict", "inbox_request_limit", "inbox_busy",
    "inbox_source_invalid", "inbox_response_limit",
))
TITLE_FIELDS = frozenset(("title", "url", "published_at", "published_date", "publication_precision", "retrieved_at"))
SOURCE_COLUMNS = ("source", "generation", "revision", "initialized", "last_success_at", "last_attempt_at",
                  "status", "code", "skipped_count", "evicted_unread_count", "evicted_at")
MESSAGE_COLUMNS = ("message_id", "source", "document_key", "title", "url", "published_date", "published_at",
                   "publication_precision", "first_seen_at", "retrieved_at", "discovery", "baseline", "read_at")


class WatchlistNewsError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in CODES else "inbox_store_unavailable"
        super().__init__(self.code)


def token(value, *, collection=False):
    size = 64 if collection else 32
    if type(value) is not str or re.fullmatch("[0-9a-f]{" + str(size) + "}", value) is None:
        raise WatchlistNewsError("inbox_invalid_request")
    return value


def utc_stamp(value):
    try:
        if type(value) is str:
            value = datetime.fromisoformat(value)
        return timestamp(value)
    except (ValueError, TypeError, OverflowError):
        raise WatchlistNewsError("inbox_invalid_request") from None


def canonical_stamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str or not 20 <= len(value) <= 32 or utc_stamp(value) != value:
        raise WatchlistNewsError("inbox_corrupt")
    return value


def publication_sort_key(value):
    """Order known source instants precisely; an empty key means the time is unknown."""
    if value is None:
        return ""
    try:
        if type(value) is not str or not 20 <= len(value) <= 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        raise WatchlistNewsError("inbox_invalid_cursor") from None


def checked_source(value):
    if type(value) is not str or value not in SOURCES:
        raise WatchlistNewsError("inbox_invalid_request")
    return value


def checked_day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise WatchlistNewsError("inbox_invalid_title") from None


def document_key(source, url, instrument_id):
    checked_source(source)
    checked_instrument_id(instrument_id)
    if type(url) is not str or len(url) > 600:
        raise WatchlistNewsError("inbox_invalid_title")
    if source == "sina_company_news":
        match = re.fullmatch(
            r"https://finance\.sina\.com\.cn/(?:[A-Za-z0-9_-]+/)*doc-([a-z0-9]+)\.shtml", url)
        if match:
            return "sina:" + match[1]
        match = re.fullmatch(r"https://cj\.sina\.cn/articles/view/([0-9]+)/([A-Za-z0-9]+)", url)
        if match:
            return "sina-cj:" + match[1] + ":" + match[2]
    else:
        match = re.fullmatch(
            r"https://data\.eastmoney\.com/notices/detail/([0-9]{6})/(AN[0-9]{12,40})\.html", url)
        if match and match[1] == instrument_id[:6]:
            return match[2]
    raise WatchlistNewsError("inbox_invalid_title")


def checked_title(value, *, instrument_id, source, checked_at=None):
    checked_instrument_id(instrument_id)
    checked_source(source)
    if instrument_id.endswith(".BJ") or type(value) is not dict or value.keys() != TITLE_FIELDS:
        raise WatchlistNewsError("inbox_invalid_title")
    title = value["title"]
    if type(title) is not str or not 1 <= len(title) <= 600 or not title.isprintable() or title != " ".join(title.split()):
        raise WatchlistNewsError("inbox_invalid_title")
    day = checked_day(value["published_date"])
    precision, published = value["publication_precision"], value["published_at"]
    if precision == "date":
        if published is not None:
            raise WatchlistNewsError("inbox_invalid_title")
    elif precision == "datetime":
        try:
            parsed = datetime.fromisoformat(published) if type(published) is str else None
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            if parsed.astimezone(timezone(timedelta(hours=8))).date().isoformat() != day:
                raise ValueError()
            published = parsed.isoformat()
        except (ValueError, TypeError, OverflowError):
            raise WatchlistNewsError("inbox_invalid_title") from None
    else:
        raise WatchlistNewsError("inbox_invalid_title")
    retrieved = utc_stamp(value["retrieved_at"])
    if checked_at is not None:
        cutoff = datetime.fromisoformat(utc_stamp(checked_at))
        if datetime.fromisoformat(retrieved) > cutoff:
            raise WatchlistNewsError("inbox_invalid_title")
        availability = (datetime.fromisoformat(published) + (timedelta(seconds=59)
                        if source == "sina_company_news" else timedelta()) if published else
                        datetime.combine(date.fromisoformat(day), datetime.max.time().replace(microsecond=0),
                                         timezone(timedelta(hours=8))))
        if availability > cutoff:
            raise WatchlistNewsError("inbox_invalid_title")
        if not cutoff.astimezone(timezone(timedelta(hours=8))).date() - timedelta(days=LOOKBACK_DAYS) <= date.fromisoformat(day):
            raise WatchlistNewsError("inbox_invalid_title")
    key = document_key(source, value["url"], instrument_id)
    identity = json.dumps([instrument_id, source, key, day], ensure_ascii=True, separators=(",", ":"))
    return {**value, "published_at": published, "retrieved_at": retrieved, "document_key": key,
            "message_id": sha256(identity.encode("utf-8")).hexdigest()}


def title_from_evidence(item):
    from trading_companion.company_research_contracts import ResearchEvidence
    if (type(item) is not ResearchEvidence or item.source not in SOURCES
            or item.section != SOURCE_KINDS[item.source] or item.content_kind != "title"
            or item.temporal_scope != "published_before_cutoff" or type(item.value) is not str):
        raise WatchlistNewsError("inbox_invalid_title")
    value = {"title": item.value, "url": item.url, "published_at": item.published_at.isoformat() if item.published_at else None,
             "published_date": item.published_date.isoformat() if item.published_date else None,
             "publication_precision": item.publication_precision, "retrieved_at": item.retrieved_at.isoformat()}
    checked_title(value, instrument_id=item.instrument_id, source=item.source)
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def encode_cursor(kind, payload):
    value = {"version": 1, "kind": kind, "payload": payload}
    encoded = urlsafe_b64encode(json.dumps(value, ensure_ascii=True, allow_nan=False,
                                         separators=(",", ":")).encode("ascii")).decode("ascii").rstrip("=")
    decode_cursor(encoded, kind)
    return encoded


def decode_cursor(value, kind):
    try:
        if kind not in ("list", "check") or type(value) is not str or not 1 <= len(value) <= 2048:
            raise ValueError()
        if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError()
        raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(decoded) is not dict or decoded.keys() != {"version", "kind", "payload"}
                or type(decoded["version"]) is not int or decoded["version"] != 1 or decoded["kind"] != kind):
            raise ValueError()
        payload = decoded["payload"]
        if type(payload) is not dict:
            raise ValueError()
        if kind == "check":
            if payload.keys() != {"watchlist_revision", "position"}:
                raise ValueError()
            token(payload["watchlist_revision"], collection=True)
            if type(payload["position"]) is not int or not 0 <= payload["position"] <= 100:
                raise ValueError()
        else:
            if payload.keys() != {"collection_revision", "unread_only", "instrument_id", "first_seen_at", "message_id",
                                  "published_date", "published_at"}:
                raise ValueError()
            token(payload["collection_revision"], collection=True)
            token(payload["message_id"], collection=True)
            canonical_stamp(payload["first_seen_at"])
            checked_day(payload["published_date"])
            publication_sort_key(payload["published_at"])
            if payload["published_at"] is not None and datetime.fromisoformat(payload["published_at"]).astimezone(
                    timezone(timedelta(hours=8))).date().isoformat() != payload["published_date"]:
                raise ValueError()
            if type(payload["unread_only"]) is not bool:
                raise ValueError()
            if payload["instrument_id"] is not None:
                checked_instrument_id(payload["instrument_id"])
        return payload
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WatchlistNewsError("inbox_invalid_cursor") from None


def collection_hash(value):
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")).hexdigest()
