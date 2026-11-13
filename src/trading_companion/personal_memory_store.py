"""Strict standalone SQLite storage for bounded local watchlists and self-reported preferences."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from trading_companion.personal_memory import (
    LOCAL_USER, MAX_WATCHLIST, MAX_PREFERENCES, PREFERENCE_FIELDS, PersonalMemoryError, checked_instrument_id,
    checked_name, checked_record, checked_preference_record, checked_preferences_changes, checked_revision,
    checked_user, preferences_collection_revision, timestamp,
)


SCHEMA_VERSION = 2
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
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version == 1:
                    self._assert_schema(version=1)
                    self._check_integrity()
                    self._validate_all(version=1)
                    self._connection.execute(_CREATE_PREFERENCES)
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
        if version == 2:
            expected = sorted([*expected,
                ("index", "sqlite_autoindex_preferences_1", "preferences", None),
                ("table", "preferences", "preferences", _CREATE_PREFERENCES)], key=lambda item: (item[0], item[1]))
        if (objects != expected
                or self._connection.execute("PRAGMA user_version").fetchone()[0] != version
                or self._connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
            raise PersonalMemoryError("personal_memory_schema_mismatch")

    def _check_integrity(self):
        if self._connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            raise PersonalMemoryError("personal_memory_corrupt")

    def _validate_all(self, *, version=SCHEMA_VERSION):
        # Stream users, then bounded rows per user, including nonlocal legacy owners.
        tables = ("watchlist", "preferences") if version == 2 else ("watchlist",)
        for table in tables:
            for (user_id,) in self._connection.execute(f"SELECT DISTINCT user_id FROM {table}"):
                try:
                    checked_user(user_id)
                except PersonalMemoryError:
                    raise PersonalMemoryError("personal_memory_corrupt") from None
                self._read_records(user_id) if table == "watchlist" else self._read_preferences(user_id)

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

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._connection.close()
                except sqlite3.Error:
                    raise PersonalMemoryError("personal_memory_store_unavailable") from None
