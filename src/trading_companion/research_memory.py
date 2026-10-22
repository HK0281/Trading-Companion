"""Detached successful result snapshots, with explicit current-versus-history semantics."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report
from trading_companion.security_directory import Security


RESULT_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True)
class SavedResearch:
    request: MultiIndicatorRequest
    security: Security
    saved_at: datetime
    _result: MultiIndicatorResult = field(repr=False)
    development_source: CsvDailyBarSource | None = field(default=None, repr=False)

    @classmethod
    def checked(cls, result: MultiIndicatorResult, *, request: MultiIndicatorRequest, security: Security,
                now: datetime, development_source: CsvDailyBarSource | None):
        detached = deepcopy(result)
        report = build_research_report(detached, expected_request=request)
        if (report.status not in ("complete", "limited") or not detached.synthesis_allowed
                or security.instrument_id != request.instrument_id):
            return None
        return cls(request, security, now, detached, development_source)

    @property
    def result(self) -> MultiIndicatorResult:
        return deepcopy(self._result)

    @property
    def expires_at(self) -> datetime:
        return self.saved_at + RESULT_LIFETIME

    def metadata(self, *, current: bool) -> dict[str, object]:
        return {"request_id": self.request.request_id, "security": self.security.to_dict(),
                "start_date": self.request.start_date.isoformat(), "as_of_date": self.request.as_of_date.isoformat(),
                "latest_trade_date": self._result.latest_trade_date.isoformat(), "source": self.request.source,
                "data_authenticity": self._result.data_authenticity, "is_current": current,
                "saved_at": self.saved_at.isoformat(), "expires_at": self.expires_at.isoformat()}


@dataclass(frozen=True)
class ResultLookup:
    status: str
    code: str
    message: str
    snapshot: SavedResearch | None = field(default=None, repr=False)
    is_current: bool = False
