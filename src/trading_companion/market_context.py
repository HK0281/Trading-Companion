"""Trusted local business-date context, explicitly not historical point-in-time proof."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report


SNAPSHOT_VERSION = "market-context-snapshot-v1"
LIVE_SNAPSHOT_VERSION = "market-context-snapshot-v2"
VERSION = "research-market-context-v1"
VALIDATION_SCOPE = "reported_business_date_context_not_point_in_time"
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
MAX_PUBLIC_BYTES = 64 * 1024
MAX_STATES = 10000
MAX_CALENDAR_DAYS = 3660
MAX_QUERY_DAYS = 366
SHANGHAI = timezone(timedelta(hours=8))
SOURCES = {"synthetic_fixture", "sse", "szse", "bse", "curated_local"}
BOARDS = {"sse_main", "star", "szse_main", "chinext", "bse", "unknown"}
ST_STATUSES = {"not_st", "st", "star_st", "unknown"}
SUSPENSION_STATUSES = {"full_day_suspended", "not_full_day_suspended", "unknown"}
DAY_STATUSES = {"open", "closed", "unknown"}
EXCHANGES = {"SH", "SZ", "BJ"}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
_INSTRUMENT = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _require(condition: bool) -> None:
    if not condition:
        raise DataValidationError("Invalid market context snapshot data")


def _identifier(value: object) -> bool:
    return type(value) is str and _SAFE_ID.fullmatch(value) is not None


def _aware(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    offset = value.utcoffset()
    # Keep public ISO timestamps within the same minute-offset contract as the loader.
    return offset is not None and offset.total_seconds() % 60 == 0


def _date(value: object) -> date:
    _require(type(value) is str and _DATE.fullmatch(value) is not None)
    parsed = date.fromisoformat(value)
    _require(parsed.isoformat() == value)
    return parsed


def _datetime(value: object) -> datetime:
    _require(type(value) is str and _TIMESTAMP.fullmatch(value) is not None)
    parsed = datetime.fromisoformat(value)
    _require(_aware(parsed))
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


@dataclass(frozen=True)
class SecurityState:
    instrument_id: str
    effective_from: date
    effective_to: date
    board: str
    listing_date: date | None
    st_status: str
    suspension_status: str
    published_at: datetime | None
    source: str
    reference: str
    observed_at: datetime | None = None
    st_reference: str | None = None


@dataclass(frozen=True)
class MarketSourceRecord:
    """Publication time and observation time are distinct provenance claims."""

    reference: str
    source: str
    source_url: str
    as_of_date: date
    retrieved_at: datetime
    published_at: datetime | None
    publication_precision: str
    field_scope: tuple[str, ...]
    coverage: str


@dataclass(frozen=True)
class CalendarDay:
    date: date
    status: str


@dataclass(frozen=True)
class ExchangeCalendar:
    exchange: str
    coverage_start: date
    coverage_end: date
    published_at: datetime | None
    source: str
    reference: str
    days: tuple[CalendarDay, ...]


@dataclass(frozen=True)
class MarketContextSnapshot:
    snapshot_id: str
    retrieved_at: datetime
    is_fixture: bool
    security_states: tuple[SecurityState, ...]
    calendars: tuple[ExchangeCalendar, ...]
    source_records: tuple[MarketSourceRecord, ...] = ()

    def __post_init__(self) -> None:
        _validate_snapshot(self)


def _provenance(item, snapshot: MarketContextSnapshot, exchange: str) -> None:
    _require(type(item.source) is str and item.source in SOURCES and _identifier(item.reference))
    _require((item.source == "synthetic_fixture") == snapshot.is_fixture)
    expected_source = {"SH": "sse", "SZ": "szse", "BJ": "bse"}[exchange]
    _require(item.source in ("synthetic_fixture", "curated_local", expected_source))
    _require(item.published_at is None or _aware(item.published_at)
             and item.published_at <= snapshot.retrieved_at)


def _validate_snapshot(snapshot: MarketContextSnapshot) -> None:
    _require(type(snapshot) is MarketContextSnapshot and _identifier(snapshot.snapshot_id)
             and _aware(snapshot.retrieved_at) and type(snapshot.is_fixture) is bool
             and type(snapshot.security_states) is tuple and len(snapshot.security_states) <= MAX_STATES
             and type(snapshot.calendars) is tuple and len(snapshot.calendars) <= 3
             and type(snapshot.source_records) is tuple and len(snapshot.source_records) <= 10)
    references = set()
    _require(not snapshot.source_records or not snapshot.is_fixture)
    for source in snapshot.source_records:
        _require(type(source) is MarketSourceRecord and _identifier(source.reference)
                 and source.reference not in references and type(source.source) is str and source.source in {"sse", "szse", "bse"}
                 and type(source.source_url) is str and len(source.source_url) <= 500
                 and all(character.isprintable() and not character.isspace() for character in source.source_url)
                 and type(source.as_of_date) is date and _aware(source.retrieved_at)
                 and source.retrieved_at <= snapshot.retrieved_at
                 and 0 <= (source.retrieved_at.astimezone(SHANGHAI).date() - source.as_of_date).days <= 7
                 and source.publication_precision in {"unknown", "second", "day"}
                 and type(source.field_scope) is tuple and 1 <= len(source.field_scope) <= 8
                 and all(type(field) is str and field in {"board", "listing_date", "st_status", "calendar", "suspension_status"}
                         for field in source.field_scope)
                 and len(set(source.field_scope)) == len(source.field_scope)
                 and _identifier(source.coverage))
        parts = urlsplit(source.source_url)
        _require(parts.scheme == "https" and parts.hostname in {
            "sse": {"www.sse.com.cn", "query.sse.com.cn"},
            "szse": {"www.szse.cn", "docs.static.szse.cn"}, "bse": {"www.bse.cn"},
        }[source.source] and parts.username is None and parts.password is None
                 and parts.port is None and not parts.fragment)
        _require((source.published_at is None) == (source.publication_precision == "unknown")
                 and (source.published_at is None or _aware(source.published_at)
                      and source.published_at <= source.retrieved_at))
        references.add(source.reference)
    by_instrument = {}
    for item in snapshot.security_states:
        _require(type(item) is SecurityState and type(item.instrument_id) is str
                 and _INSTRUMENT.fullmatch(item.instrument_id) is not None
                 and type(item.effective_from) is date and type(item.effective_to) is date
                 and item.effective_from <= item.effective_to
                 and type(item.board) is str and item.board in BOARDS
                 and (item.listing_date is None or type(item.listing_date) is date)
                 and type(item.st_status) is str and item.st_status in ST_STATUSES
                 and type(item.suspension_status) is str and item.suspension_status in SUSPENSION_STATUSES)
        exchange = item.instrument_id[-2:]
        _require(item.board in {
            "SH": {"sse_main", "star", "unknown"},
            "SZ": {"szse_main", "chinext", "unknown"}, "BJ": {"bse", "unknown"},
        }[exchange])
        _provenance(item, snapshot, exchange)
        _require(item.observed_at is None or _aware(item.observed_at)
                 and item.observed_at <= snapshot.retrieved_at
                 and item.reference in references
                 and item.effective_from == item.effective_to
                 and item.published_at is None)
        if item.observed_at is not None:
            record = next(record for record in snapshot.source_records if record.reference == item.reference)
            _require(record.source == item.source and record.retrieved_at == item.observed_at
                     and item.effective_from == record.as_of_date)
        _require(item.st_reference is None or item.observed_at is not None
                 and item.st_reference in references)
        if item.st_reference is not None:
            record = next(record for record in snapshot.source_records if record.reference == item.st_reference)
            _require(record.source == item.source and "st_status" in record.field_scope
                     and record.retrieved_at == item.observed_at)
        by_instrument.setdefault(item.instrument_id, []).append(item)
    for records in by_instrument.values():
        ordered = sorted(records, key=lambda item: item.effective_from)
        _require(all(left.effective_to < right.effective_from for left, right in zip(ordered, ordered[1:])))
    exchanges = set()
    for item in snapshot.calendars:
        _require(type(item) is ExchangeCalendar and type(item.exchange) is str
                 and item.exchange in EXCHANGES and item.exchange not in exchanges
                 and type(item.coverage_start) is date and type(item.coverage_end) is date
                 and 1 <= (item.coverage_end - item.coverage_start).days + 1 <= MAX_CALENDAR_DAYS
                 and type(item.days) is tuple
                 and len(item.days) == (item.coverage_end - item.coverage_start).days + 1)
        exchanges.add(item.exchange)
        _provenance(item, snapshot, item.exchange)
        for index, day in enumerate(item.days):
            _require(type(day) is CalendarDay and type(day.date) is date
                     and day.date == item.coverage_start + timedelta(days=index)
                     and type(day.status) is str and day.status in DAY_STATUSES)


def _object(pairs):
    output = {}
    for key, value in pairs:
        _require(key not in output)
        output[key] = value
    return output


def _constant(value):
    raise DataValidationError("Invalid market context JSON constant")


def _fields(value: object, expected: set[str]) -> None:
    _require(type(value) is dict and value.keys() == expected)


def load_market_context(path: str | Path) -> MarketContextSnapshot:
    """Read a bounded application-selected file; parsing does not authenticate its claims."""
    try:
        with Path(path).open("rb") as handle:
            encoded = handle.read(MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        raise DataSourceError("Cannot read the configured market context snapshot") from None
    except (TypeError, ValueError, OverflowError):
        raise DataValidationError("Invalid market context snapshot") from None
    return parse_market_context_bytes(encoded)


def parse_market_context_bytes(raw: bytes) -> MarketContextSnapshot:
    """Validate the same bounded bytes that an application may separately hash or pin."""
    try:
        _require(type(raw) is bytes and len(raw) <= MAX_SNAPSHOT_BYTES)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        _require(type(payload) is dict and payload.get("version") in {SNAPSHOT_VERSION, LIVE_SNAPSHOT_VERSION})
        live = payload["version"] == LIVE_SNAPSHOT_VERSION
        _fields(payload, {"version", "snapshot_id", "retrieved_at", "is_fixture", "security_states", "calendars"}
                | ({"source_records"} if live else set()))
        _require(type(payload["security_states"]) is list and len(payload["security_states"]) <= MAX_STATES
                 and type(payload["calendars"]) is list and len(payload["calendars"]) <= 3)
        states = []
        for item in payload["security_states"]:
            _fields(item, {"instrument_id", "effective_from", "effective_to", "board", "listing_date",
                           "st_status", "suspension_status", "published_at", "source", "reference"}
                    | ({"observed_at", "st_reference"} if live else set()))
            states.append(SecurityState(
                item["instrument_id"], _date(item["effective_from"]), _date(item["effective_to"]),
                item["board"], None if item["listing_date"] is None else _date(item["listing_date"]),
                item["st_status"], item["suspension_status"], _optional_datetime(item["published_at"]),
                item["source"], item["reference"], _optional_datetime(item["observed_at"]) if live else None,
                item["st_reference"] if live else None,
            ))
        calendars = []
        for item in payload["calendars"]:
            _fields(item, {"exchange", "coverage_start", "coverage_end", "published_at", "source", "reference", "days"})
            _require(type(item["days"]) is list and len(item["days"]) <= MAX_CALENDAR_DAYS)
            days = []
            for day in item["days"]:
                _fields(day, {"date", "status"})
                days.append(CalendarDay(_date(day["date"]), day["status"]))
            calendars.append(ExchangeCalendar(
                item["exchange"], _date(item["coverage_start"]), _date(item["coverage_end"]),
                _optional_datetime(item["published_at"]), item["source"], item["reference"], tuple(days),
            ))
        sources = []
        if live:
            _require(type(payload["source_records"]) is list and len(payload["source_records"]) <= 10)
            for item in payload["source_records"]:
                _fields(item, {"reference", "source", "source_url", "as_of_date", "retrieved_at",
                               "published_at", "publication_precision", "field_scope", "coverage"})
                _require(type(item["field_scope"]) is list)
                sources.append(MarketSourceRecord(item["reference"], item["source"], item["source_url"],
                    _date(item["as_of_date"]), _datetime(item["retrieved_at"]),
                    _optional_datetime(item["published_at"]), item["publication_precision"],
                    tuple(item["field_scope"]), item["coverage"]))
        return MarketContextSnapshot(payload["snapshot_id"], _datetime(payload["retrieved_at"]),
                                     payload["is_fixture"], tuple(states), tuple(calendars), tuple(sources))
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise DataValidationError("Invalid market context snapshot") from None


def _unavailable(code: str) -> dict[str, object]:
    return {"version": VERSION, "status": "unavailable", "code": code}


def _public(item, cutoff: datetime) -> bool:
    return (item.published_at is not None and item.published_at <= cutoff
            or isinstance(item, SecurityState) and item.observed_at is not None and item.observed_at <= cutoff)


def _security(snapshot: MarketContextSnapshot, request: MultiIndicatorRequest, cutoff: datetime):
    item = next((item for item in snapshot.security_states
                 if item.instrument_id == request.instrument_id
                 and item.effective_from <= request.as_of_date <= item.effective_to), None)
    output = {
        "status": "unknown", "reason": "state_not_covered", "effective_from": None, "effective_to": None,
        "published_at": None, "source": None, "reference": None, "board": "unknown",
        "listing_date": None, "st_status": "unknown", "suspension_status": "unknown",
        "listing_age_calendar_days": None,
    }
    if item is None:
        return output
    output.update(effective_from=item.effective_from.isoformat(), effective_to=item.effective_to.isoformat(),
                  published_at=item.published_at.isoformat() if item.published_at else None,
                  source=item.source, reference=item.reference)
    if item.observed_at is not None:
        output.update(observed_at=item.observed_at.isoformat(),
                      status_scope="current_observation_only_not_historical_status",
                      publication_precision="unknown", st_reference=item.st_reference)
    if not _public(item, cutoff):
        output["reason"] = ("observed_after_analysis_cutoff" if item.observed_at is not None and item.observed_at > cutoff
                            else "publication_time_unknown" if item.published_at is None
                            else "published_after_analysis_cutoff")
        return output
    partial = (item.board == "unknown" or item.listing_date is None
               or item.st_status == "unknown" or item.suspension_status == "unknown")
    output.update(status="limited" if partial else "available",
                  reason="partial_state_unknown" if partial else "reported_state_at_analysis_date",
                  board=item.board, listing_date=item.listing_date.isoformat() if item.listing_date else None,
                  st_status=item.st_status, suspension_status=item.suspension_status,
                  listing_age_calendar_days=(request.as_of_date - item.listing_date).days
                  if item.listing_date is not None and item.listing_date <= request.as_of_date else None)
    return output


def _calendar(snapshot: MarketContextSnapshot, request: MultiIndicatorRequest, cutoff: datetime,
              observed: set[date]):
    exchange = request.instrument_id[-2:]
    item = next((item for item in snapshot.calendars if item.exchange == exchange), None)
    query_dates = [request.start_date + timedelta(days=index)
                   for index in range((request.as_of_date - request.start_date).days + 1)]
    statuses = {day.date: day.status for day in item.days} if item is not None and _public(item, cutoff) else {}
    output = {
        "exchange": exchange, "coverage_start": item.coverage_start.isoformat() if item else None,
        "coverage_end": item.coverage_end.isoformat() if item else None,
        "published_at": item.published_at.isoformat() if item and item.published_at else None,
        "source": item.source if item else None, "reference": item.reference if item else None,
        "open_dates": [], "closed_dates": [], "unknown_dates": [],
        "missing_bar_dates": [], "unexpected_bar_dates": [],
    }
    for day in query_dates:
        status = statuses.get(day, "unknown")
        output[status + "_dates"].append(day.isoformat())
        if status == "open" and day not in observed:
            output["missing_bar_dates"].append(day.isoformat())
        if status == "closed" and day in observed:
            output["unexpected_bar_dates"].append(day.isoformat())
    return output, item


def build_market_context(result: MultiIndicatorResult, *, expected_request: MultiIndicatorRequest | None,
                         snapshot: MarketContextSnapshot | None, reference_at: datetime) -> dict[str, object]:
    """Project a read-only context beside, never into, model inputs or screening decisions."""
    if snapshot is None:
        return _unavailable("context_not_configured")
    try:
        _require(_aware(reference_at) and type(expected_request) is MultiIndicatorRequest)
        request = MultiIndicatorRequest(
            expected_request.instrument_id, expected_request.start_date, expected_request.as_of_date,
            expected_request.indicators, expected_request.source, expected_request.request_id,
        )
        _require(request == expected_request and type(result) is MultiIndicatorResult
                 and type(result.bars) is tuple and len(result.bars) <= MAX_QUERY_DAYS)
        _validate_snapshot(snapshot)
        if snapshot.retrieved_at > reference_at:
            return _unavailable("context_future_snapshot")
        report = build_research_report(result, expected_request=request)
        if report.status == "unavailable":
            return _unavailable("context_result_unavailable")
        _require(report.status in ("complete", "limited"))
        if snapshot.is_fixture != (result.data_authenticity == "synthetic"):
            return _unavailable("context_authenticity_mismatch")
        _require(bool(result.bars))
        for bar in result.bars:
            _require(type(bar) is DailyBar)
            DailyBar.__post_init__(bar)
        cutoff = datetime.combine(request.as_of_date, time.max, tzinfo=SHANGHAI)
        security = _security(snapshot, request, cutoff)
        calendar, calendar_item = _calendar(snapshot, request, cutoff, {bar.trade_date for bar in result.bars})
        warnings = ["not_point_in_time", "calendar_not_instrument_tradability", "no_trading_rules_evaluated"]
        if snapshot.is_fixture:
            warnings.append("synthetic_context_not_market_facts")
        if snapshot.retrieved_at > cutoff:
            warnings.append("retrieved_after_analysis_cutoff")
        if security["status"] != "available":
            warnings.append("security_context_unknown_or_partial")
        if calendar["unknown_dates"]:
            warnings.append("calendar_context_unknown_or_partial")
        if calendar_item is None:
            warnings.append("calendar_not_configured_for_exchange")
        elif not _public(calendar_item, cutoff):
            warnings.append("calendar_publication_time_unknown" if calendar_item.published_at is None
                            else "calendar_published_after_analysis_cutoff")
        if calendar["missing_bar_dates"]:
            warnings.append("open_dates_without_bars_do_not_imply_suspension")
        conflicts = []
        if calendar["unexpected_bar_dates"]:
            conflicts.append("bar_on_reported_closed_date")
        known_states = [item for item in snapshot.security_states
                        if item.instrument_id == request.instrument_id and _public(item, cutoff)]
        # The selected state's reported listing event can be compared with all query bars.
        listing_date = date.fromisoformat(security["listing_date"]) if security["listing_date"] else None
        if listing_date is not None and any(bar.trade_date < listing_date for bar in result.bars):
            conflicts.append("bar_before_reported_listing_date")
        if any(item.suspension_status == "full_day_suspended"
               and item.effective_from <= bar.trade_date <= item.effective_to
               for item in known_states for bar in result.bars):
            conflicts.append("bar_during_reported_full_day_suspension")
        if security["listing_date"] is not None and request.as_of_date < date.fromisoformat(security["listing_date"]):
            warnings.append("analysis_precedes_reported_listing_date")
        limited = security["status"] != "available" or bool(calendar["unknown_dates"] or conflicts)
        payload = {
            "version": VERSION, "status": "limited" if limited else "available",
            "code": "market_context_limited" if limited else "reported_market_context",
            "request_id": request.request_id, "instrument_id": request.instrument_id,
            "start_date": request.start_date.isoformat(), "as_of_date": request.as_of_date.isoformat(),
            "snapshot_id": snapshot.snapshot_id, "retrieved_at": snapshot.retrieved_at.isoformat(),
            "data_authenticity": "synthetic" if snapshot.is_fixture else "unverified_local_snapshot",
            "validation_scope": VALIDATION_SCOPE, "historical_availability": "not_established",
            "security": security, "calendar": calendar, "conflicts": conflicts, "warnings": warnings,
        }
        if snapshot.source_records:
            payload["source_records"] = market_source_records(snapshot)
            warnings.append("current_observation_not_historical_state")
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_PUBLIC_BYTES:
            return _unavailable("context_payload_limit")
        return payload
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError, RecursionError):
        return _unavailable("context_integrity_mismatch")


def market_source_records(snapshot: MarketContextSnapshot) -> list[dict[str, object]]:
    """Return bounded public provenance without inventing a publication timestamp."""
    _validate_snapshot(snapshot)
    return [{"reference": item.reference, "source": item.source, "source_url": item.source_url,
             "as_of_date": item.as_of_date.isoformat(), "retrieved_at": item.retrieved_at.isoformat(),
             "published_at": item.published_at.isoformat() if item.published_at else None,
             "publication_precision": item.publication_precision, "field_scope": list(item.field_scope),
             "coverage": item.coverage} for item in snapshot.source_records]


def current_security_background(snapshot: MarketContextSnapshot, *, instrument_id: str,
                                analysis_date: date, reference_at: datetime) -> dict[str, object]:
    """Expose a dated current observation separately from unavailable historical status."""
    _validate_snapshot(snapshot)
    _require(type(instrument_id) is str and _INSTRUMENT.fullmatch(instrument_id) is not None
             and type(analysis_date) is date and _aware(reference_at))
    if snapshot.is_fixture or snapshot.retrieved_at > reference_at:
        return {"status": "unknown", "code": "current_background_unavailable"}
    states = [item for item in snapshot.security_states if item.instrument_id == instrument_id
              and item.observed_at is not None and item.observed_at <= reference_at]
    if not states:
        return {"status": "unknown", "code": "current_background_not_covered"}
    state = max(states, key=lambda item: item.observed_at)
    age = (reference_at.astimezone(SHANGHAI).date() - state.effective_from).days
    if not 0 <= age <= 7:
        return {"status": "unknown", "code": "current_background_stale"}
    same_date = (state.effective_from <= analysis_date <= state.effective_to
                 and state.observed_at.astimezone(SHANGHAI).date() == analysis_date)
    return {"status": "limited", "instrument_id": instrument_id,
        "analysis_date": analysis_date.isoformat(), "source_as_of_date": state.effective_from.isoformat(),
        "observation_date": state.observed_at.astimezone(SHANGHAI).date().isoformat(),
        "observed_at": state.observed_at.isoformat(), "published_at": None, "publication_precision": "unknown",
        "board": state.board, "listing_date": state.listing_date.isoformat() if state.listing_date else None,
        "observed_st_status": state.st_status, "analysis_date_st_status": state.st_status if same_date else "unknown",
        "status_scope": "current_observation_only_not_historical_status",
        "suspension_status": "unknown", "calendar_status": "not_fetched",
        "source": state.source, "reference": state.reference, "st_reference": state.st_reference,
        "source_records": [item for item in market_source_records(snapshot)
                           if item["reference"] in {state.reference, state.st_reference}],
        "historical_availability": "not_established", "tradability": "not_assessed"}
