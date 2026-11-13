"""Strict standalone SQLite storage for a bounded, explicitly enabled local watchlist."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import RLock

from trading_companion.personal_memory import (
    LOCAL_USER, MAX_WATCHLIST, PersonalMemoryError, checked_instrument_id, checked_name,
    checked_record, checked_user, timestamp,
)


SCHEMA_VERSION = 1
APPLICATION_ID = 0x5443504D
PersonalMemoryStoreError = PersonalMemoryError

_CREATE_TABLE = """CREATE TABLE watchlist (
    user_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, instrument_id))"""


class PersonalMemoryStore:
    """Trusted application-selected storage; no conversation or research state is involved."""

    def list(self, user_id=LOCAL_USER): raise NotImplementedError
    def add(self, user_id=LOCAL_USER, *, instrument_id, name): raise NotImplementedError
    def remove(self, user_id=LOCAL_USER, *, instrument_id): raise NotImplementedError
    def clear(self, user_id=LOCAL_USER): raise NotImplementedError
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
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                self._assert_schema()
                if self._connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
                    raise PersonalMemoryError("personal_memory_corrupt")
                self._read_records(LOCAL_USER)
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

    def _assert_schema(self):
        objects = self._connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        expected = [("index", "sqlite_autoindex_watchlist_1", "watchlist", None),
                    ("table", "watchlist", "watchlist", _CREATE_TABLE)]
        if (objects != expected
                or self._connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                or self._connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
            raise PersonalMemoryError("personal_memory_schema_mismatch")

    def _read_records(self, user_id):
        rows = self._connection.execute(
            "SELECT instrument_id, name, created_at, updated_at FROM watchlist WHERE user_id = ? "
            "ORDER BY created_at, instrument_id LIMIT ?", (user_id, MAX_WATCHLIST + 1)).fetchall()
        if len(rows) > MAX_WATCHLIST:
            raise PersonalMemoryError("personal_memory_corrupt")
        return [checked_record(dict(zip(("instrument_id", "name", "created_at", "updated_at"), row)))
                for row in rows]

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
                    return count
            except sqlite3.Error:
                raise PersonalMemoryError("personal_memory_store_unavailable") from None

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._connection.close()
                except sqlite3.Error:
                    raise PersonalMemoryError("personal_memory_store_unavailable") from None
