"""Provider-independent research evidence; publication and retrieval are never interchangeable."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite
import re

from trading_companion.errors import InvalidRequestError


SHANGHAI = timezone(timedelta(hours=8))
RESEARCH_KINDS = ("profile", "financial", "news", "announcements")


def aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class ResearchDataRequest:
    instrument_id: str
    security_name: str
    as_of: datetime
    start_date: date | None = None
    limit: int = 5
    allow_current_background: bool = False

    def __post_init__(self):
        match = re.fullmatch(r"([0-9]{6})\.(SH|SZ|BJ)", self.instrument_id) if type(self.instrument_id) is str else None
        prefixes = {"SH": ("6",), "SZ": ("0", "3"), "BJ": ("4", "8", "9")}
        if match is None or not match[1].startswith(prefixes[match[2]]):
            raise InvalidRequestError("Use one qualified mainland A-share identifier")
        if (type(self.security_name) is not str or not self.security_name.strip()
                or len(self.security_name) > 80 or not self.security_name.isprintable() or not aware(self.as_of)
                or type(self.limit) is not int or not 1 <= self.limit <= 5
                or type(self.allow_current_background) is not bool):
            raise InvalidRequestError("Use bounded, trusted company-research conditions")
        if self.start_date is not None and (type(self.start_date) is not date
                or not 0 <= (self.as_of.astimezone(SHANGHAI).date() - self.start_date).days <= 366):
            raise InvalidRequestError("Publication window must be historical and at most 366 days")

    @property
    def publication_start(self):
        return self.start_date or self.as_of.astimezone(SHANGHAI).date() - timedelta(days=30)

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "security_name": self.security_name,
                "as_of": self.as_of.isoformat(), "start_date": self.publication_start.isoformat(),
                "limit": self.limit, "allow_current_background": self.allow_current_background}


@dataclass(frozen=True)
class ResearchEvidence:
    id: str
    instrument_id: str
    section: str
    source: str
    metric: str
    value: float | str
    unit: str
    url: str
    published_at: datetime | None
    published_date: date | None
    publication_precision: str
    retrieved_at: datetime
    report_period: date | None = None
    status: str = "available"
    content_kind: str = "fact"
    temporal_scope: str = "published_before_cutoff"
    updated_date: date | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if (not aware(self.retrieved_at) or self.published_at is not None and not aware(self.published_at)
                or self.section not in RESEARCH_KINDS or self.publication_precision not in ("datetime", "date", "unknown")
                or self.temporal_scope not in ("published_before_cutoff", "current_context")
                or self.content_kind not in ("fact", "snippet", "title", "background")
                or type(self.value) not in (str, float) or type(self.value) is float and not isfinite(self.value)):
            raise InvalidRequestError("Invalid bounded company evidence")
        if self.publication_precision == "datetime" and self.published_at is None:
            raise InvalidRequestError("Exact publication precision requires a source timestamp")
        if self.publication_precision == "date" and (type(self.published_date) is not date or self.published_at is not None):
            raise InvalidRequestError("Date-only publication must not invent an intraday timestamp")
        if self.publication_precision == "unknown" and (self.published_at is not None or self.published_date is not None
                or self.temporal_scope != "current_context"):
            raise InvalidRequestError("Unknown publication is current context, never historical evidence")

    def to_dict(self):
        return {"id": self.id, "instrument_id": self.instrument_id, "section": self.section,
                "source": self.source, "metric": self.metric, "value": self.value, "unit": self.unit,
                "url": self.url, "published_at": self.published_at.isoformat() if self.published_at else None,
                "published_date": self.published_date.isoformat() if self.published_date else None,
                "publication_precision": self.publication_precision, "retrieved_at": self.retrieved_at.isoformat(),
                "report_period": self.report_period.isoformat() if self.report_period else None,
                "status": self.status, "content_kind": self.content_kind, "temporal_scope": self.temporal_scope,
                "updated_date": self.updated_date.isoformat() if self.updated_date else None,
                "warnings": list(self.warnings)}


@dataclass(frozen=True)
class ResearchDataSection:
    kind: str
    source: str
    status: str
    items: tuple[ResearchEvidence, ...]
    code: str
    message: str
    retrieved_at: datetime
    skipped_count: int = 0
    bounded_snapshot: bool = True

    def to_dict(self):
        return {"kind": self.kind, "source": self.source, "status": self.status,
                "items": [item.to_dict() for item in self.items], "code": self.code, "message": self.message,
                "retrieved_at": self.retrieved_at.isoformat(), "skipped_count": self.skipped_count,
                "bounded_snapshot": self.bounded_snapshot}


@dataclass(frozen=True)
class ResearchDataBatch:
    request: ResearchDataRequest
    source_results: tuple[ResearchDataSection, ...]

    @property
    def items(self):
        return tuple(item for section in self.source_results for item in section.items)

    @property
    def status(self):
        if not self.items:
            return "unavailable"
        return "partial" if any(section.status != "available" or section.skipped_count
                                for section in self.source_results) else "ok"

    def to_dict(self):
        return {"version": "company-research-v1", "status": self.status, "request": self.request.to_dict(),
                "items": [item.to_dict() for item in self.items],
                "source_results": [section.to_dict() for section in self.source_results],
                "temporal_scope": "bounded_current_vendor_snapshots_not_versioned_point_in_time"}
