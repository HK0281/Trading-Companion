from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Literal

from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import DailyBarSource
from trading_companion.errors import (
    DataSourceError,
    DataValidationError,
    InvalidRequestError,
)
from trading_companion.history import bars_as_of_close
from trading_companion.indicators import sma_close_points


AnalysisStatus = Literal[
    "ok", "no_data", "insufficient_history", "source_error", "invalid_data"
]


@dataclass(frozen=True)
class AnalysisRow:
    bar: DailyBar
    sma_close: float | None


@dataclass(frozen=True)
class AnalysisResult:
    status: AnalysisStatus
    reason: str
    message: str
    instrument_id: str | None
    requested_as_of_date: date
    requested_start_date: date | None
    window: int
    source: str
    price_adjustment: str
    retrieved_at: datetime | None
    rows: tuple[AnalysisRow, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def latest_trade_date(self) -> date | None:
        return self.rows[-1].bar.trade_date if self.rows else None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-compatible values without rounding calculation results."""
        rows = []
        for row in self.rows:
            fields = asdict(row.bar)
            fields["trade_date"] = row.bar.trade_date.isoformat()
            fields["sma_close"] = row.sma_close
            rows.append(fields)
        return {
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "instrument_id": self.instrument_id,
            "requested_as_of_date": self.requested_as_of_date.isoformat(),
            "requested_start_date": (
                self.requested_start_date.isoformat() if self.requested_start_date else None
            ),
            "latest_trade_date": (
                self.latest_trade_date.isoformat() if self.latest_trade_date else None
            ),
            "window": self.window,
            "bars_used": len(self.rows),
            "missing_bars": (
                None if self.status in ("source_error", "invalid_data")
                else max(0, self.window - len(self.rows))
            ),
            "source": self.source,
            "price_adjustment": self.price_adjustment,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "warnings": list(self.warnings),
            "rows": rows,
        }


def analyze_daily_bars(
    source: DailyBarSource,
    *,
    as_of_date: date,
    window: int = 3,
    instrument_id: str | None = None,
    start_date: date | None = None,
) -> AnalysisResult:
    """Read, select and analyze one instrument; never print or place trades."""
    if type(as_of_date) is not date:
        raise InvalidRequestError("as_of_date must be a date")
    if start_date is not None and (
        type(start_date) is not date or start_date > as_of_date
    ):
        raise InvalidRequestError("start_date must be a date on or before as_of_date")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise InvalidRequestError("window must be a positive integer")
    if instrument_id is not None and (
        not isinstance(instrument_id, str) or not instrument_id.strip()
    ):
        raise InvalidRequestError("instrument_id must be a non-empty string")

    configured_instrument = getattr(source, "instrument_id", None)
    if instrument_id and configured_instrument and instrument_id != configured_instrument:
        raise InvalidRequestError("Requested instrument differs from the configured source")
    selected_instrument = instrument_id or configured_instrument
    retrieved_at = None
    warnings = [
        "End-of-day trade-date filtering only; historical publication and revision "
        "availability are not verified."
    ]
    if source.adjustment == "unknown":
        warnings.append("CSV price adjustment and data authenticity are unspecified.")

    def result(
        status: AnalysisStatus, reason: str, message: str,
        rows: tuple[AnalysisRow, ...] = (),
    ) -> AnalysisResult:
        return AnalysisResult(
            status=status, reason=reason, message=message,
            instrument_id=selected_instrument,
            requested_as_of_date=as_of_date, requested_start_date=start_date,
            window=window, source=source.source_name, price_adjustment=source.adjustment,
            retrieved_at=retrieved_at, rows=rows, warnings=tuple(warnings),
        )

    try:
        bars = source.read_daily_bars()
    except DataSourceError as error:
        return result("source_error", "source_unavailable", str(error))
    except DataValidationError as error:
        return result("invalid_data", "invalid_source_data", str(error))

    retrieved_at = datetime.now(timezone.utc)
    if not isinstance(bars, list) or any(not isinstance(bar, DailyBar) for bar in bars):
        return result("invalid_data", "invalid_source_data", "Source must return DailyBar records")
    if not bars:
        return result("no_data", "empty_source", "Source returned no records; the cause is unknown")

    snapshot = [
        bar for bar in bars_as_of_close(bars, as_of_date)
        if (start_date is None or bar.trade_date >= start_date)
        and (selected_instrument is None or bar.instrument_id == selected_instrument)
    ]
    if not snapshot:
        return result("no_data", "no_matching_bars", "No records match the instrument and date range")
    try:
        points = sma_close_points(snapshot, window)
    except ValueError as error:
        return result("invalid_data", "invalid_series", str(error))

    selected_instrument = snapshot[0].instrument_id
    rows = tuple(AnalysisRow(bar, point.value) for bar, point in zip(snapshot, points))
    if snapshot[-1].trade_date < as_of_date:
        warnings.append("Latest bar precedes the requested date; holiday or suspension is not inferred.")
    if len(rows) < window:
        return result(
            "insufficient_history", "sma_not_ready",
            f"SMA{window} requires {window} bars; only {len(rows)} are available", rows,
        )
    return result("ok", "analysis_complete", "Daily-bar analysis completed", rows)
