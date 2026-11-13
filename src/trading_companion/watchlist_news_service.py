"""Independent, explicitly requested inbox batches; no research or model budget."""

from collections import OrderedDict
from copy import deepcopy
from datetime import timedelta
import json
from threading import Event, Lock
from time import monotonic

from trading_companion.company_research_contracts import ResearchDataRequest, SHANGHAI
from trading_companion.personal_memory import LOCAL_USER, PersonalMemoryError
from trading_companion.watchlist_news import (
    VERSION, SOURCES, WatchlistNewsError, encode_cursor, decode_cursor, title_from_evidence,
)
from trading_companion.watchlist_news_sources import WatchlistNewsSourceReader


class InboxCancellation:
    """Order cancellation against a bounded local commit, independently of research."""

    def __init__(self, external=None):
        self._event, self._lock, self._external = Event(), Lock(), external

    def is_set(self):
        return self._event.is_set() or self._external is not None and self._external.is_set()

    def set(self):
        with self._lock:
            self._event.set()

    def commit(self, callback):
        with self._lock:
            if self.is_set():
                raise WatchlistNewsError("inbox_cancelled")
            return callback()


def disabled_inbox():
    return dict(version=VERSION, enabled=False, limit=20, items=[], unread_count=0,
                source_states=[], collection_revision=None, has_more=False, next_cursor=None)


class WatchlistInboxService:
    """One physical batch slot, bounded request replay and generation-checked writes."""

    def __init__(self, store, *, clock, reader=None):
        self.store, self.clock = store, clock
        self.reader = reader or WatchlistNewsSourceReader(clock=clock)
        self._lock, self._active, self._closed = Lock(), None, False
        # A retired ID is never treated as permission to repeat network I/O.
        self._accepted, self._cache = {}, OrderedDict()
        self.sidecar_status = None

    def note_sidecar(self, code):
        if code in ("inbox_sidecar_saved", "inbox_sidecar_unavailable"):
            self.sidecar_status = code

    def local_page(self):
        return dict(self.store.list_inbox(LOCAL_USER, now=self.clock()), sidecar_status=self.sidecar_status)

    def close(self):
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active[1].set()

    def invalidate(self):
        with self._lock:
            if self._active is not None:
                self._active[1].set()

    def check(self, request_id, cursor=None, *, cancelled=None):
        import re
        if (type(request_id) is not str or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
                or cursor is not None and (type(cursor) is not str or len(cursor) > 2048)):
            raise WatchlistNewsError("inbox_invalid_request")
        gate = cancelled if isinstance(cancelled, InboxCancellation) else InboxCancellation(cancelled)
        with self._lock:
            if self._closed:
                raise WatchlistNewsError("inbox_closed")
            if request_id in self._accepted:
                if self._accepted[request_id] != cursor:
                    raise WatchlistNewsError("inbox_request_conflict")
                if self._active is not None and self._active[0] == request_id:
                    raise WatchlistNewsError("inbox_busy")
                cached = self._cache.get(request_id)
                if cached is None:
                    raise WatchlistNewsError("inbox_request_retired")
                cached = deepcopy(cached)
            else:
                if self._active is not None:
                    raise WatchlistNewsError("inbox_busy")
                if len(self._accepted) >= 4096:
                    raise WatchlistNewsError("inbox_request_limit")
                # Validation below precedes acceptance: rejected cursors spend no network ID.
                cached = None
                self._active = (request_id, gate)
        if cached is not None:
            snapshot = self.store.inbox_snapshot(LOCAL_USER)
            if snapshot["watchlist_revision"] != cached.pop("_watchlist_revision"):
                raise WatchlistNewsError("inbox_snapshot_changed")
            cached["inbox"] = self.local_page()
            cached["replayed"] = True
            return cached
        accepted = False
        try:
            snapshot = self.store.inbox_snapshot(LOCAL_USER)
            revision, stocks = snapshot["watchlist_revision"], snapshot["items"]
            position = 0
            if cursor is not None:
                payload = decode_cursor(cursor, "check")
                if (set(payload) != {"watchlist_revision", "position"}
                        or payload["watchlist_revision"] != revision
                        or type(payload["position"]) is not int
                        or not 0 <= payload["position"] <= len(stocks) * 2):
                    raise WatchlistNewsError("inbox_snapshot_changed")
                position = payload["position"]
            with self._lock:
                self._accepted[request_id] = cursor
            accepted = True
            deadline, now = monotonic() + 20.0, self.clock()
            if gate.is_set():
                raise WatchlistNewsError("inbox_cancelled")
            selected = stocks[position // 2:position // 2 + 5]
            targets = tuple(ResearchDataRequest(item["instrument_id"], item["name"], now,
                now.astimezone(SHANGHAI).date() - timedelta(days=30), 5) for item in selected)
            originals = {item["instrument_id"]: item for item in selected}
            results = []

            def before_request(_instrument, _source):
                # Once per actual send, after pacing: never read SQLite for every chunk.
                try:
                    live = self.store.inbox_snapshot(LOCAL_USER, deadline=min(deadline, monotonic() + .05))
                    if live["watchlist_revision"] != revision:
                        gate.set()
                except (WatchlistNewsError, PersonalMemoryError):
                    gate.set()

            def receive(section):
                source, instrument = section["source"], section["instrument_id"]
                filtered_count = section["filtered_count"]
                if (instrument not in originals or source not in SOURCES or len(results) >= 10
                        or type(filtered_count) is not int or not 0 <= filtered_count <= 50):
                    raise WatchlistNewsError("inbox_source_invalid")
                original = originals[instrument]
                state = next(s for s in original["sources"] if s["source"] == source)
                status = section["status"] if section["status"] in ("available", "empty", "partial", "unavailable") else "unavailable"
                code = "inbox_source_" + status
                outcome = dict(instrument_id=instrument, source=source, status=section["status"],
                    code=code, saved=0, duplicates=0, baseline=not state["initialized"],
                    filtered_count=filtered_count)
                try:
                    if monotonic() >= deadline:
                        raise WatchlistNewsError("inbox_deadline")
                    items = [title_from_evidence(item) for item in section["items"]]
                    stored = gate.commit(lambda: self.store.ingest_inbox(LOCAL_USER,
                        instrument_id=instrument, source=source, generation=original["generation"],
                        expected_revision=state["revision"], items=items,
                        checked_at=section["checked_at"], origin="manual", status=status,
                        skipped_count=section["skipped_count"],
                        deadline=min(deadline, monotonic() + 0.2)))
                    outcome.update({key: stored[key] for key in ("saved", "duplicates", "baseline", "status", "code")})
                except WatchlistNewsError as error:
                    outcome.update(status="unavailable", code=error.code)
                except PersonalMemoryError:
                    outcome.update(status="unavailable", code="inbox_store_unavailable")
                results.append(outcome)

            stats = (self.reader.read_watchlist_sources(targets, cancel=gate.is_set,
                deadline=deadline, on_source=receive, start_source=position % 2,
                before_request=before_request) if targets else
                dict(attempts=0, completed_pairs=0, stopped=False, code="inbox_checked"))
            completed = stats["completed_pairs"]
            if type(completed) is not int or not 0 <= completed <= len(selected) * 2 - position % 2:
                raise WatchlistNewsError("inbox_source_invalid")
            next_position = position + completed
            remaining = len(stocks) * 2 - next_position
            next_cursor = encode_cursor("check", dict(watchlist_revision=revision, position=next_position)) if remaining else None
            fresh = self.store.inbox_snapshot(LOCAL_USER)
            if fresh["watchlist_revision"] != revision:
                raise WatchlistNewsError("inbox_snapshot_changed")
            response = dict(version=VERSION, enabled=True, request_id=request_id, replayed=False,
                status="cancelled" if gate.is_set() else "partial" if stats["stopped"]
                    or any(r["status"] in ("partial", "unavailable") for r in results) else "completed",
                attempted_requests=stats["attempts"], results=results, next_cursor=next_cursor,
                remaining_sources=remaining, checked_stocks=next_position // 2, total_stocks=len(stocks),
                inbox=self.local_page())
            if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
                raise WatchlistNewsError("inbox_response_limit")
            with self._lock:
                self._cache[request_id] = dict(deepcopy(response), _watchlist_revision=revision)
                while len(self._cache) > 32:
                    self._cache.popitem(last=False)
            return response
        finally:
            # Only this synchronous worker, after the reader's finally, frees the slot.
            with self._lock:
                if self._active is not None and self._active[0] == request_id:
                    self._active = None
                if not accepted:
                    self._accepted.pop(request_id, None)
