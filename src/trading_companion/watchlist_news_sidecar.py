"""Optional bounded local title sink; neither prompts nor research outputs are inputs."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from trading_companion.personal_memory import LOCAL_USER
from trading_companion.turn_control import current_control
from trading_companion.turn_trace import trace_event
from trading_companion.watchlist_news import SOURCES, WatchlistNewsError


_CURRENT = ContextVar("local_watchlist_title_sink", default=None)


@contextmanager
def bind_research_inbox(store, *, report=None):
    token = _CURRENT.set((store, report) if store is not None else None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def prepare_research_inbox(plan, *, development=False):
    context = _CURRENT.get()
    if context is None or development or plan.explicit_historical:
        return None
    store, report = context
    # The two bounded stages together allow at most 200 ms cooperative local I/O.
    with trace_event("check", "inbox_sidecar") as event:
        try:
            snapshot = store.inbox_snapshot(LOCAL_USER, deadline=monotonic() + 0.05)
            wanted = {security.instrument_id for security in plan.securities}
            selected = {item["instrument_id"]: item for item in snapshot["items"] if item["instrument_id"] in wanted}
            event.status, event.code = "ok", "inbox_sidecar_ready"
            return ResearchInboxSink(store, selected, report=report) if selected else None
        except Exception:
            event.status, event.code = "limited", "inbox_sidecar_unavailable"
            if report is not None:
                report("inbox_sidecar_unavailable")
            return None


class ResearchInboxSink:
    def __init__(self, store, snapshot, *, report=None):
        self.store, self.snapshot, self.report = store, snapshot, report

    def capture(self, bundle):
        with trace_event("check", "inbox_sidecar") as event:
            deadline = monotonic() + 0.15
            try:
                for company in bundle.companies:
                    original = self.snapshot.get(company.instrument_id)
                    if original is None:
                        continue
                    for source in SOURCES:
                        values = [item for item in company.evidence if item.source == source
                                  and item.content_kind == "title" and item.data_authenticity == "provider_reported"][:5]
                        if not values:
                            continue
                        checked = []
                        for item in values:
                            checked.append(dict(title=item.raw_value, url=item.url, published_at=item.published_at,
                                published_date=item.date, publication_precision=item.publication_precision,
                                retrieved_at=item.retrieved_at))
                        state = next(s for s in original["sources"] if s["source"] == source)
                        self.store.ingest_inbox(LOCAL_USER, instrument_id=company.instrument_id, source=source,
                            generation=original["generation"], expected_revision=state["revision"], items=checked,
                            checked_at=max(value["retrieved_at"] for value in checked), origin="research",
                            status="available", deadline=deadline,
                            before_commit=current_control().checkpoint if current_control() is not None else None)
                event.status, event.code = "ok", "inbox_sidecar_saved"
            except Exception:
                # The next ordinary checkpoint still handles true research cancellation.
                event.status, event.code = "limited", "inbox_sidecar_unavailable"
            if self.report is not None:
                self.report(event.code)
