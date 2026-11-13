"""Strict standalone SQLite storage for bounded local watchlists and self-reported preferences."""

from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from math import isfinite
from pathlib import Path
import sqlite3
from threading import RLock
from time import monotonic
from uuid import uuid4

from trading_companion.personal_memory import (
    LOCAL_USER, MAX_WATCHLIST, MAX_PREFERENCES, PREFERENCE_FIELDS, PersonalMemoryError, checked_instrument_id,
    checked_name, checked_record, checked_preference_record, checked_preferences_changes, checked_revision,
    checked_user, preferences_collection_revision, timestamp,
)
from trading_companion.watchlist_news import (
    VERSION as INBOX_VERSION, SOURCES, SOURCE_COLUMNS, MESSAGE_COLUMNS, PAGE_SIZE, MAX_TITLES,
    MAX_MESSAGES, MAX_SEEN, LOOKBACK_DAYS, RETENTION_DAYS, STATUSES, CODES, WatchlistNewsError,
    token, utc_stamp, canonical_stamp, checked_day, checked_source, checked_title,
    collection_hash, encode_cursor, decode_cursor, publication_sort_key,
)


SCHEMA_VERSION = 3
APPLICATION_ID = 0x5443504D
PersonalMemoryStoreError = PersonalMemoryError

_CREATE_TABLE = """CREATE TABLE watchlist (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, instrument_id))"""
_CREATE_PREFERENCES = """CREATE TABLE preferences (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    name TEXT NOT NULL,
    holding_status TEXT,
    horizon TEXT,
    risk TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY (user_id, instrument_id))"""
_PREFERENCE_COLUMNS = ("instrument_id", "name", *PREFERENCE_FIELDS, "created_at", "updated_at", "revision")
_CREATE_INBOX_SOURCE = """CREATE TABLE inbox_source_state (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    source TEXT NOT NULL,
    generation TEXT NOT NULL,
    revision TEXT NOT NULL,
    initialized INTEGER NOT NULL,
    last_success_at TEXT,
    last_attempt_at TEXT,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    skipped_count INTEGER NOT NULL,
    evicted_unread_count INTEGER NOT NULL,
    evicted_at TEXT,
    PRIMARY KEY (user_id, instrument_id, source))"""
_CREATE_INBOX_MESSAGES = """CREATE TABLE inbox_messages (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source TEXT NOT NULL,
    document_key TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_date TEXT NOT NULL,
    published_at TEXT,
    publication_precision TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    discovery TEXT NOT NULL,
    baseline INTEGER NOT NULL,
    read_at TEXT,
    PRIMARY KEY (user_id, message_id))"""
_CREATE_INBOX_SEEN = """CREATE TABLE inbox_seen (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    published_date TEXT NOT NULL,
    PRIMARY KEY (user_id, instrument_id, source, fingerprint))"""
_INBOX_TABLES = (("inbox_source_state", _CREATE_INBOX_SOURCE), ("inbox_messages", _CREATE_INBOX_MESSAGES),
                 ("inbox_seen", _CREATE_INBOX_SEEN))


class PersonalMemoryStore:
    """Trusted application-selected storage; no conversation or research state is involved."""

    def list(self, user_id=LOCAL_USER): raise NotImplementedError
    def add(self, user_id=LOCAL_USER, *, instrument_id, name): raise NotImplementedError
    def remove(self, user_id=LOCAL_USER, *, instrument_id): raise NotImplementedError
    def clear(self, user_id=LOCAL_USER): raise NotImplementedError
    def list_preferences(self, user_id=LOCAL_USER): raise NotImplementedError
    def patch_preferences(self, user_id=LOCAL_USER, *, instrument_id, changes, expected_revision, name=None): raise NotImplementedError
    def remove_preferences(self, user_id=LOCAL_USER, *, instrument_id, expected_revision): raise NotImplementedError
    def clear_preferences(self, user_id=LOCAL_USER, *, expected_revision): raise NotImplementedError
    def inbox_snapshot(self, user_id=LOCAL_USER, *, deadline=None): raise NotImplementedError
    def list_inbox(self, user_id=LOCAL_USER, **kwargs): raise NotImplementedError
    def ingest_inbox(self, user_id=LOCAL_USER, **kwargs): raise NotImplementedError
    def mark_inbox_read(self, user_id=LOCAL_USER, *, message_id): raise NotImplementedError
    def clear_inbox(self, user_id=LOCAL_USER, *, expected_revision): raise NotImplementedError
    def close(self): raise NotImplementedError


class SqlitePersonalMemoryStore(PersonalMemoryStore):
    def __init__(self, path, *, clock=None):
        self._lock = RLock()
        self._closed = True
        self._clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
        if not isinstance(path, (str, Path)) or not str(path).strip() or "\x00" in str(path):
            raise PersonalMemoryError("invalid_personal_memory_path")
        if not callable(self._clock):
            raise PersonalMemoryError("invalid_personal_memory_time")
        try:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            self._connection.execute("PRAGMA trusted_schema = OFF")
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE")
                objects = self._connection.execute("SELECT name FROM sqlite_master").fetchall()
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                application = self._connection.execute("PRAGMA application_id").fetchone()[0]
                if not objects and version == 0 and application == 0:
                    self._connection.execute(_CREATE_TABLE)
                    self._connection.execute(_CREATE_PREFERENCES)
                    self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    self._connection.execute("PRAGMA user_version = 2")
                    version = 2
                if version == 1:
                    self._assert_schema(version=1)
                    self._check_integrity()
                    self._validate_all(version=1)
                    self._connection.execute(_CREATE_PREFERENCES)
                    self._connection.execute("PRAGMA user_version = 2")
                    version = 2
                if version == 2:
                    self._assert_schema(version=2)
                    self._check_integrity()
                    self._validate_all(version=2)
                    for _, definition in _INBOX_TABLES:
                        self._connection.execute(definition)
                    for user_id, instrument_id in self._connection.execute("SELECT user_id, instrument_id FROM watchlist"):
                        self._create_inbox_sources(user_id, instrument_id)
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._assert_schema()
                self._check_integrity()
                self._validate_all()
            self._closed = False
        except PersonalMemoryError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise
        except (sqlite3.Error, OSError, ValueError):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def _open(self):
        if self._closed:
            raise PersonalMemoryError("personal_memory_store_closed")

    def _timestamp(self):
        try:
            return timestamp(self._clock())
        except Exception:
            raise PersonalMemoryError("invalid_personal_memory_time") from None

    def _assert_schema(self, *, version=SCHEMA_VERSION):
        objects = self._connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        expected = [("index", "sqlite_autoindex_watchlist_1", "watchlist", None),
                    ("table", "watchlist", "watchlist", _CREATE_TABLE)]
        if version >= 2:
            expected = sorted([*expected,
                ("index", "sqlite_autoindex_preferences_1", "preferences", None),
                ("table", "preferences", "preferences", _CREATE_PREFERENCES)], key=lambda item: (item[0], item[1]))
        if version == 3:
            expected = sorted([*expected, *(entry for name, definition in _INBOX_TABLES for entry in (
                ("index", "sqlite_autoindex_" + name + "_1", name, None),
                ("table", name, name, definition)))], key=lambda item: (item[0], item[1]))
        if (objects != expected
                or self._connection.execute("PRAGMA user_version").fetchone()[0] != version
                or self._connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
            raise PersonalMemoryError("personal_memory_schema_mismatch")

    def _check_integrity(self):
        if self._connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            raise PersonalMemoryError("personal_memory_corrupt")

    def _validate_all(self, *, version=SCHEMA_VERSION):
        # Stream users, then bounded rows per user, including nonlocal legacy owners.
        tables = ("watchlist", "preferences") if version >= 2 else ("watchlist",)
        for table in tables:
            for (user_id,) in self._connection.execute(f"SELECT DISTINCT user_id FROM {table}"):
                try:
                    checked_user(user_id)
                except PersonalMemoryError:
                    raise PersonalMemoryError("personal_memory_corrupt") from None
                self._read_records(user_id) if table == "watchlist" else self._read_preferences(user_id)
        if version == 3:
            self._validate_inbox_all()

    def _read_records(self, user_id):
        rows = self._connection.execute(
            "SELECT instrument_id, name, created_at, updated_at FROM watchlist WHERE user_id = ? "
            "ORDER BY created_at, instrument_id LIMIT ?", (user_id, MAX_WATCHLIST + 1)).fetchall()
        if len(rows) > MAX_WATCHLIST:
            raise PersonalMemoryError("personal_memory_corrupt")
        return [checked_record(dict(zip(("instrument_id", "name", "created_at", "updated_at"), row)))
                for row in rows]

    def _read_preferences(self, user_id):
        rows = self._connection.execute(
            "SELECT instrument_id, name, holding_status, horizon, risk, created_at, updated_at, revision "
            "FROM preferences WHERE user_id = ? ORDER BY created_at, instrument_id LIMIT ?",
            (user_id, MAX_PREFERENCES + 1)).fetchall()
        if len(rows) > MAX_PREFERENCES:
            raise PersonalMemoryError("personal_memory_corrupt")
        return [checked_preference_record(dict(zip(_PREFERENCE_COLUMNS, row))) for row in rows]

    def list(self, user_id=LOCAL_USER):
        user_id = checked_user(user_id)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN")
                    self._assert_schema()
                    return self._read_records(user_id)
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def add(self, user_id=LOCAL_USER, *, instrument_id, name):
        user_id, instrument_id, name = checked_user(user_id), checked_instrument_id(instrument_id), checked_name(name)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    records = self._read_records(user_id)
                    existing = next((record for record in records if record["instrument_id"] == instrument_id), None)
                    if existing is not None:
                        return {"added": False, "item": existing}
                    if len(records) >= MAX_WATCHLIST:
                        raise PersonalMemoryError("watchlist_full")
                    stamp = self._timestamp()
                    item = {"instrument_id": instrument_id, "name": name, "created_at": stamp, "updated_at": stamp}
                    self._connection.execute("INSERT INTO watchlist VALUES (?, ?, ?, ?, ?)",
                        (user_id, instrument_id, name, stamp, stamp))
                    self._create_inbox_sources(user_id, instrument_id)
                    return {"added": True, "item": item}
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def remove(self, user_id=LOCAL_USER, *, instrument_id):
        user_id, instrument_id = checked_user(user_id), checked_instrument_id(instrument_id)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    records = self._read_records(user_id)
                    found = any(record["instrument_id"] == instrument_id for record in records)
                    self._connection.execute("DELETE FROM watchlist WHERE user_id = ? AND instrument_id = ?",
                                             (user_id, instrument_id))
                    for table, _ in _INBOX_TABLES:
                        self._connection.execute(f"DELETE FROM {table} WHERE user_id = ? AND instrument_id = ?",
                                                 (user_id, instrument_id))
                    return found
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def clear(self, user_id=LOCAL_USER):
        user_id = checked_user(user_id)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    count = len(self._read_records(user_id))
                    self._connection.execute("DELETE FROM watchlist WHERE user_id = ?", (user_id,))
                    for table, _ in _INBOX_TABLES:
                        self._connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                    return count
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def list_preferences(self, user_id=LOCAL_USER):
        user_id = checked_user(user_id)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN")
                    self._assert_schema()
                    items = self._read_preferences(user_id)
                    return {"items": items, "collection_revision": preferences_collection_revision(items, user_id)}
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def patch_preferences(self, user_id=LOCAL_USER, *, instrument_id, changes, expected_revision, name=None):
        user_id, instrument_id = checked_user(user_id), checked_instrument_id(instrument_id)
        changes = checked_preferences_changes(changes)
        if expected_revision is not None:
            checked_revision(expected_revision)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    records = self._read_preferences(user_id)
                    existing = next((item for item in records if item["instrument_id"] == instrument_id), None)
                    if (existing is None and expected_revision is not None
                            or existing is not None and existing["revision"] != expected_revision):
                        raise PersonalMemoryError("preferences_conflict")
                    values = {key: existing[key] if existing is not None else None for key in PREFERENCE_FIELDS}
                    values.update(changes)
                    if not any(value is not None for value in values.values()):
                        raise PersonalMemoryError("empty_preferences")
                    if existing is not None and all(existing[key] == values[key] for key in PREFERENCE_FIELDS):
                        return {"created": False, "changed": False, "item": existing}
                    if existing is None and len(records) >= MAX_PREFERENCES:
                        raise PersonalMemoryError("preferences_full")
                    stamp = self._timestamp()
                    if existing is not None and stamp < existing["updated_at"]:
                        raise PersonalMemoryError("invalid_personal_memory_time")
                    item = {"instrument_id": instrument_id,
                            "name": existing["name"] if existing is not None else checked_name(name),
                            **values, "created_at": existing["created_at"] if existing is not None else stamp,
                            "updated_at": stamp, "revision": uuid4().hex}
                    if existing is None:
                        self._connection.execute("INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (user_id, *(item[key] for key in _PREFERENCE_COLUMNS)))
                    else:
                        self._connection.execute(
                            "UPDATE preferences SET holding_status = ?, horizon = ?, risk = ?, updated_at = ?, revision = ? "
                            "WHERE user_id = ? AND instrument_id = ?",
                            (*(item[key] for key in PREFERENCE_FIELDS), stamp, item["revision"], user_id, instrument_id))
                    return {"created": existing is None, "changed": True, "item": item}
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def remove_preferences(self, user_id=LOCAL_USER, *, instrument_id, expected_revision):
        user_id, instrument_id = checked_user(user_id), checked_instrument_id(instrument_id)
        checked_revision(expected_revision)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    records = self._read_preferences(user_id)
                    existing = next((item for item in records if item["instrument_id"] == instrument_id), None)
                    if existing is None:
                        return False
                    if existing["revision"] != expected_revision:
                        raise PersonalMemoryError("preferences_conflict")
                    self._connection.execute("DELETE FROM preferences WHERE user_id = ? AND instrument_id = ?",
                                             (user_id, instrument_id))
                    return True
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def clear_preferences(self, user_id=LOCAL_USER, *, expected_revision):
        user_id = checked_user(user_id)
        checked_revision(expected_revision, collection=True)
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._assert_schema()
                    records = self._read_preferences(user_id)
                    if preferences_collection_revision(records, user_id) != expected_revision:
                        raise PersonalMemoryError("preferences_conflict")
                    self._connection.execute("DELETE FROM preferences WHERE user_id = ?", (user_id,))
                    return len(records)
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def _create_inbox_sources(self, user_id, instrument_id):
        generation = uuid4().hex
        self._connection.executemany("INSERT INTO inbox_source_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(user_id, instrument_id, source, generation, uuid4().hex, 0, None, None,
              "not_checked", "inbox_not_checked", 0, 0, None) for source in SOURCES])

    @contextmanager
    def _inbox_transaction(self, *, write=False, deadline=None, before_commit=None):
        """Budget the Python lock and SQLite work without changing ordinary-method timeouts."""
        if deadline is not None and (type(deadline) not in (int, float) or not isfinite(deadline)):
            raise WatchlistNewsError("inbox_invalid_request")
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise WatchlistNewsError("inbox_store_timeout")
        acquired = self._lock.acquire() if remaining is None else self._lock.acquire(timeout=remaining)
        if not acquired:
            raise WatchlistNewsError("inbox_store_timeout")
        configured = False
        try:
            self._open()
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise WatchlistNewsError("inbox_store_timeout")
                # A native busy wait cannot consult the SQL progress handler. Fail immediately
                # on DB contention so work already spent in this budget cannot be spent twice.
                self._connection.execute("PRAGMA busy_timeout = 0")
                self._connection.set_progress_handler(lambda: int(monotonic() >= deadline), 100)
                configured = True
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._assert_schema()
                yield
                if before_commit is not None:
                    before_commit()
                if deadline is not None and monotonic() >= deadline:
                    raise WatchlistNewsError("inbox_store_timeout")
        except sqlite3.Error as error:
            if deadline is not None and (monotonic() >= deadline or getattr(error, "sqlite_errorcode", None)
                                         in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT)):
                raise WatchlistNewsError("inbox_store_timeout") from None
            raise WatchlistNewsError("inbox_store_unavailable") from None
        finally:
            if configured:
                self._connection.set_progress_handler(None, 0)
                self._connection.execute("PRAGMA busy_timeout = 5000")
            self._lock.release()

    def _source_rows(self, user_id, instrument_id):
        rows = self._connection.execute(
            "SELECT " + ", ".join(SOURCE_COLUMNS) + " FROM inbox_source_state "
            "WHERE user_id = ? AND instrument_id = ? ORDER BY source LIMIT 3", (user_id, instrument_id)).fetchall()
        if len(rows) != 2:
            raise WatchlistNewsError("inbox_corrupt")
        records = []
        try:
            for row in rows:
                item = dict(zip(SOURCE_COLUMNS, row))
                checked_source(item["source"])
                token(item["generation"])
                token(item["revision"])
                if type(item["initialized"]) is not int or item["initialized"] not in (0, 1):
                    raise WatchlistNewsError("inbox_corrupt")
                for key in ("last_success_at", "last_attempt_at", "evicted_at"):
                    canonical_stamp(item[key], nullable=True)
                if (item["status"] not in STATUSES or item["code"] not in CODES
                        or any(type(item[key]) is not int or not 0 <= item[key] <= 2_147_483_647
                               for key in ("skipped_count", "evicted_unread_count"))
                        or item["last_success_at"] is not None and (not item["initialized"]
                            or item["last_attempt_at"] is None or item["last_success_at"] > item["last_attempt_at"])
                        or bool(item["evicted_unread_count"]) != (item["evicted_at"] is not None)):
                    raise WatchlistNewsError("inbox_corrupt")
                item["initialized"] = bool(item["initialized"])
                records.append(item)
            if {item["source"] for item in records} != set(SOURCES) or len({item["generation"] for item in records}) != 1:
                raise WatchlistNewsError("inbox_corrupt")
        except (ValueError, TypeError):
            raise WatchlistNewsError("inbox_corrupt") from None
        return sorted(records, key=lambda item: SOURCES.index(item["source"]))

    def _inbox_snapshot(self, user_id):
        records = self._read_records(user_id)
        count = self._connection.execute("SELECT count(*) FROM inbox_source_state WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count != len(records) * 2:
            raise WatchlistNewsError("inbox_corrupt")
        identifiers = {record["instrument_id"] for record in records}
        for table, maximum in (("inbox_messages", MAX_MESSAGES), ("inbox_seen", MAX_SEEN)):
            counts = self._connection.execute(f"SELECT instrument_id, count(*) FROM {table} "
                "WHERE user_id = ? GROUP BY instrument_id LIMIT 51", (user_id,)).fetchall()
            if any(code not in identifiers or size > maximum for code, size in counts):
                raise WatchlistNewsError("inbox_corrupt")
        items = []
        for record in records:
            sources = self._source_rows(user_id, record["instrument_id"])
            items.append({"instrument_id": record["instrument_id"], "name": record["name"],
                          "generation": sources[0]["generation"], "sources": sources})
        return {"version": INBOX_VERSION,
            "watchlist_revision": collection_hash([user_id, [[item["instrument_id"], item["generation"]] for item in items]]),
            "collection_revision": collection_hash([user_id, [[item["instrument_id"], item["generation"],
                [[source["source"], source["revision"]] for source in item["sources"]]] for item in items]]),
            "items": items}

    def inbox_snapshot(self, user_id=LOCAL_USER, *, deadline=None):
        user_id = checked_user(user_id)
        with self._inbox_transaction(deadline=deadline):
            return self._inbox_snapshot(user_id)

    def _validate_message(self, row, *, instrument_id):
        item = dict(zip(MESSAGE_COLUMNS, row))
        try:
            title = checked_title({key: item[key] for key in ("title", "url", "published_date", "published_at",
                                                             "publication_precision", "retrieved_at")},
                                  instrument_id=instrument_id, source=item["source"])
            if title["message_id"] != item["message_id"] or title["document_key"] != item["document_key"]:
                raise WatchlistNewsError("inbox_corrupt")
            canonical_stamp(item["first_seen_at"])
            canonical_stamp(item["retrieved_at"])
            canonical_stamp(item["read_at"], nullable=True)
            if (type(item["baseline"]) is not int or item["baseline"] not in (0, 1)
                    or item["discovery"] not in ("manual", "research")
                    or item["read_at"] is not None and item["read_at"] < item["first_seen_at"]):
                raise WatchlistNewsError("inbox_corrupt")
        except (ValueError, TypeError):
            raise WatchlistNewsError("inbox_corrupt") from None
        return item

    def _validate_inbox_all(self):
        owners = self._connection.execute("SELECT user_id FROM watchlist UNION SELECT user_id FROM inbox_source_state "
            "UNION SELECT user_id FROM inbox_messages UNION SELECT user_id FROM inbox_seen")
        try:
            for (user_id,) in owners:
                checked_user(user_id)
                snapshot = self._inbox_snapshot(user_id)
                for table in ("inbox_messages", "inbox_seen"):
                    if self._connection.execute(f"SELECT 1 FROM {table} t WHERE user_id = ? AND NOT EXISTS "
                        "(SELECT 1 FROM watchlist w WHERE w.user_id = t.user_id AND w.instrument_id = t.instrument_id) LIMIT 1",
                        (user_id,)).fetchone() is not None:
                        raise WatchlistNewsError("inbox_corrupt")
                for stock in snapshot["items"]:
                    instrument_id = stock["instrument_id"]
                    rows = self._connection.execute("SELECT " + ", ".join(MESSAGE_COLUMNS) +
                        " FROM inbox_messages WHERE user_id = ? AND instrument_id = ? LIMIT ?",
                        (user_id, instrument_id, MAX_MESSAGES + 1)).fetchall()
                    if len(rows) > MAX_MESSAGES:
                        raise WatchlistNewsError("inbox_corrupt")
                    for row in rows:
                        self._validate_message(row, instrument_id=instrument_id)
                    seen = self._connection.execute("SELECT source, fingerprint, published_date FROM inbox_seen "
                        "WHERE user_id = ? AND instrument_id = ? LIMIT ?", (user_id, instrument_id, MAX_SEEN + 1)).fetchall()
                    if len(seen) > MAX_SEEN:
                        raise WatchlistNewsError("inbox_corrupt")
                    for source, fingerprint, day in seen:
                        checked_source(source)
                        token(fingerprint, collection=True)
                        checked_day(day)
        except (WatchlistNewsError, PersonalMemoryError):
            raise PersonalMemoryError("personal_memory_corrupt") from None

    def _retain_inbox(self, user_id, instrument_id, now):
        """Remove expired records, then read old records first; seen identities survive message eviction."""
        stamp = utc_stamp(now)
        expiry = utc_stamp(datetime.fromisoformat(stamp) - timedelta(days=RETENTION_DAYS))
        rows = self._connection.execute("SELECT message_id, source, first_seen_at, read_at FROM inbox_messages "
            "WHERE user_id = ? AND instrument_id = ? ORDER BY first_seen_at, message_id LIMIT ?",
            (user_id, instrument_id, MAX_MESSAGES + MAX_TITLES + 1)).fetchall()
        if len(rows) > MAX_MESSAGES + MAX_TITLES:
            raise WatchlistNewsError("inbox_corrupt")
        expired = [row for row in rows if row[2] < expiry]
        kept = [row for row in rows if row[2] >= expiry]
        overflow = max(0, len(kept) - MAX_MESSAGES)
        victims = expired + sorted(kept, key=lambda row: (row[3] is None, row[2], row[0]))[:overflow]
        if victims:
            self._connection.executemany("DELETE FROM inbox_messages WHERE user_id = ? AND message_id = ?",
                                         [(user_id, row[0]) for row in victims])
            for source in {row[1] for row in victims}:
                unread = sum(row[1] == source and row[3] is None for row in victims)
                self._connection.execute("UPDATE inbox_source_state SET revision = ?, "
                    "evicted_unread_count = evicted_unread_count + ?, evicted_at = CASE WHEN ? > 0 THEN ? ELSE evicted_at END "
                    "WHERE user_id = ? AND instrument_id = ? AND source = ?",
                    (uuid4().hex, unread, unread, stamp, user_id, instrument_id, source))
        # A fingerprint can expire only after its original publication date leaves the source query window.
        day = (datetime.fromisoformat(stamp).astimezone(timezone(timedelta(hours=8))).date()
               - timedelta(days=LOOKBACK_DAYS)).isoformat()
        self._connection.execute("DELETE FROM inbox_seen WHERE user_id = ? AND instrument_id = ? AND published_date < ?",
                                 (user_id, instrument_id, day))

    @staticmethod
    def _public_source(stock, state):
        return {"instrument_id": stock["instrument_id"], "name": stock["name"],
                **{key: value for key, value in state.items() if key not in ("generation", "revision")}}

    def list_inbox(self, user_id=LOCAL_USER, *, unread_only=True, instrument_id=None, cursor=None, now=None, deadline=None):
        user_id = checked_user(user_id)
        if type(unread_only) is not bool:
            raise WatchlistNewsError("inbox_invalid_request")
        if instrument_id is not None:
            checked_instrument_id(instrument_id)
        stamp = self._timestamp() if now is None else utc_stamp(now)
        after = None if cursor is None else decode_cursor(cursor, "list")
        with self._inbox_transaction(write=True, deadline=deadline):
            snapshot = self._inbox_snapshot(user_id)
            for stock in snapshot["items"]:
                self._retain_inbox(user_id, stock["instrument_id"], stamp)
            snapshot = self._inbox_snapshot(user_id)
            if after is not None and (after["collection_revision"] != snapshot["collection_revision"]
                    or after["unread_only"] != unread_only or after["instrument_id"] != instrument_id):
                raise WatchlistNewsError("inbox_invalid_cursor")
            where, params = ["m.user_id = ?"], [user_id]
            if unread_only:
                where.append("m.read_at IS NULL")
            if instrument_id is not None:
                where.append("m.instrument_id = ?")
                params.append(instrument_id)
            if after is not None:
                where.append("(m.first_seen_at, m.published_date, inbox_publication_order(m.published_at), m.message_id) < (?, ?, ?, ?)")
                params.extend((after["first_seen_at"], after["published_date"],
                               publication_sort_key(after["published_at"]), after["message_id"]))
            # Publication dates stay dates. Within one date, known instants precede
            # unknown times; the empty sorting key never becomes a persisted timestamp.
            self._connection.create_function("inbox_publication_order", 1, publication_sort_key, deterministic=True)
            try:
                rows = self._connection.execute("SELECT m.instrument_id, w.name, " +
                    ", ".join("m." + key for key in MESSAGE_COLUMNS) + " FROM inbox_messages m JOIN watchlist w "
                    "ON w.user_id = m.user_id AND w.instrument_id = m.instrument_id WHERE " + " AND ".join(where) +
                    " ORDER BY m.first_seen_at DESC, m.published_date DESC, inbox_publication_order(m.published_at) DESC, "
                    "m.message_id DESC LIMIT ?", (*params, PAGE_SIZE + 1)).fetchall()
            finally:
                self._connection.create_function("inbox_publication_order", 1, None)
            items = []
            for code, name, *row in rows[:PAGE_SIZE]:
                checked_name(name)
                value = self._validate_message(row, instrument_id=code)
                items.append({"instrument_id": code, "name": name,
                    **{key: field for key, field in value.items() if key != "document_key"},
                    "baseline": bool(value["baseline"]), "read": value["read_at"] is not None})
            has_more = len(rows) > PAGE_SIZE
            next_cursor = (encode_cursor("list", {"collection_revision": snapshot["collection_revision"],
                "unread_only": unread_only, "instrument_id": instrument_id,
                "first_seen_at": items[-1]["first_seen_at"], "message_id": items[-1]["message_id"],
                "published_date": items[-1]["published_date"], "published_at": items[-1]["published_at"]}) if has_more else None)
            unread_count = self._connection.execute("SELECT count(*) FROM inbox_messages WHERE user_id = ? AND read_at IS NULL",
                                                    (user_id,)).fetchone()[0]
            return {"version": INBOX_VERSION, "enabled": True, "limit": PAGE_SIZE, "items": items,
                "unread_count": unread_count, "source_states": [self._public_source(stock, state)
                    for stock in snapshot["items"] for state in stock["sources"]],
                "collection_revision": snapshot["collection_revision"], "has_more": has_more, "next_cursor": next_cursor}

    def ingest_inbox(self, user_id=LOCAL_USER, *, instrument_id, source, generation, expected_revision,
                     items, checked_at, origin="manual", status="available", code=None, skipped_count=0,
                     deadline=None, before_commit=None):
        user_id, instrument_id = checked_user(user_id), checked_instrument_id(instrument_id)
        checked_source(source)
        token(generation)
        token(expected_revision)
        stamp = utc_stamp(checked_at)
        if (origin not in ("manual", "research") or status not in ("available", "empty", "partial", "unavailable")
                or type(items) not in (tuple, list) or len(items) > MAX_TITLES
                or type(skipped_count) is not int or not 0 <= skipped_count <= 50
                or status in ("empty", "unavailable") and items
                or code is not None and (type(code) is not str or code not in CODES)
                or before_commit is not None and not callable(before_commit)):
            raise WatchlistNewsError("inbox_invalid_request")
        titles = [checked_title(item, instrument_id=instrument_id, source=source, checked_at=stamp) for item in items]
        if len({item["message_id"] for item in titles}) != len(titles):
            titles = list({item["message_id"]: item for item in titles}.values())
        if skipped_count:
            status = "partial"
        with self._inbox_transaction(write=True, deadline=deadline, before_commit=before_commit):
            stocks = self._read_records(user_id)
            if not any(stock["instrument_id"] == instrument_id for stock in stocks):
                raise WatchlistNewsError("inbox_conflict")
            states = self._source_rows(user_id, instrument_id)
            state = next(item for item in states if item["source"] == source)
            if (state["generation"] != generation or state["revision"] != expected_revision
                    or state["last_attempt_at"] is not None and stamp < state["last_attempt_at"]):
                raise WatchlistNewsError("inbox_conflict")
            self._retain_inbox(user_id, instrument_id, stamp)
            baseline = not state["initialized"]
            known = {row[0] for row in self._connection.execute("SELECT fingerprint FROM inbox_seen "
                "WHERE user_id = ? AND instrument_id = ? LIMIT ?", (user_id, instrument_id, MAX_SEEN + 1))}
            if len(known) > MAX_SEEN:
                raise WatchlistNewsError("inbox_corrupt")
            new = [item for item in titles if item["message_id"] not in known]
            result_code = code or "inbox_source_" + status
            if len(known) + len(new) > MAX_SEEN:
                new, status, result_code = [], "unavailable", "inbox_seen_capacity"
            for title in new:
                self._connection.execute("INSERT INTO inbox_seen VALUES (?, ?, ?, ?, ?)",
                    (user_id, instrument_id, source, title["message_id"], title["published_date"]))
                values = {key: title[key] for key in ("message_id", "document_key", "title", "url", "published_date",
                                                     "published_at", "publication_precision", "retrieved_at")}
                values.update(source=source, first_seen_at=stamp, discovery=origin, baseline=int(baseline), read_at=None)
                self._connection.execute("INSERT OR IGNORE INTO inbox_messages VALUES (?, ?, " +
                    ", ".join("?" for _ in MESSAGE_COLUMNS) + ")", (user_id, instrument_id, *(values[key] for key in MESSAGE_COLUMNS)))
            if origin == "manual":
                complete = status in ("available", "empty") and not skipped_count
                self._connection.execute("UPDATE inbox_source_state SET revision = ?, initialized = ?, "
                    "last_success_at = ?, last_attempt_at = ?, status = ?, code = ?, skipped_count = ? "
                    "WHERE user_id = ? AND instrument_id = ? AND source = ?",
                    (uuid4().hex, int(state["initialized"] or complete), stamp if complete else state["last_success_at"],
                     stamp, status, result_code, skipped_count, user_id, instrument_id, source))
            elif new:
                self._connection.execute("UPDATE inbox_source_state SET revision = ? WHERE user_id = ? AND instrument_id = ? AND source = ?",
                                         (uuid4().hex, user_id, instrument_id, source))
            self._retain_inbox(user_id, instrument_id, stamp)
            current = next(item for item in self._source_rows(user_id, instrument_id) if item["source"] == source)
            return {"saved": len(new), "duplicates": len(titles) - len([item for item in titles if item["message_id"] not in known]),
                    "baseline": baseline, "status": status, "code": result_code, "source_state": current}

    def mark_inbox_read(self, user_id=LOCAL_USER, *, message_id):
        user_id = checked_user(user_id)
        token(message_id, collection=True)
        with self._inbox_transaction(write=True):
            row = self._connection.execute("SELECT instrument_id, source, first_seen_at, read_at FROM inbox_messages "
                                           "WHERE user_id = ? AND message_id = ?", (user_id, message_id)).fetchone()
            if row is None:
                raise WatchlistNewsError("inbox_not_found")
            if row[3] is not None:
                return {"changed": False}
            stamp = self._timestamp()
            if stamp < row[2]:
                raise WatchlistNewsError("inbox_invalid_request")
            self._connection.execute("UPDATE inbox_messages SET read_at = ? WHERE user_id = ? AND message_id = ?",
                                     (stamp, user_id, message_id))
            self._connection.execute("UPDATE inbox_source_state SET revision = ? WHERE user_id = ? AND instrument_id = ? AND source = ?",
                                     (uuid4().hex, user_id, row[0], row[1]))
            return {"changed": True}

    def clear_inbox(self, user_id=LOCAL_USER, *, expected_revision):
        user_id = checked_user(user_id)
        token(expected_revision, collection=True)
        with self._inbox_transaction(write=True):
            snapshot = self._inbox_snapshot(user_id)
            if snapshot["collection_revision"] != expected_revision:
                raise WatchlistNewsError("inbox_conflict")
            count = self._connection.execute("SELECT count(*) FROM inbox_messages WHERE user_id = ?", (user_id,)).fetchone()[0]
            for table, _ in _INBOX_TABLES:
                self._connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            for stock in snapshot["items"]:
                self._create_inbox_sources(user_id, stock["instrument_id"])
            return count

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._connection.close()
                except sqlite3.Error:
                    raise PersonalMemoryError("personal_memory_store_unavailable") from None
