"""Local conversation history behind a small store interface; every row belongs to a user id."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

LOCAL_USER = "local"
SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 1_048_576
MAX_TURNS = 500
MAX_TITLE_CHARACTERS = 40
LIST_LIMIT = 100


class MemoryStoreError(ValueError):
    """Fixed codes only; paths and SQLite messages are never exposed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MemoryStoreError("invalid_memory_time")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _identifier(value):
    if type(value) is not str or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise MemoryStoreError("conversation_not_found")
    return value


def _user(value):
    if type(value) is not str or not 1 <= len(value) <= 64 or not value.isprintable():
        raise MemoryStoreError("invalid_memory_user")
    return value


def title_for(question):
    text = " ".join(str(question).split())
    return text if len(text) <= MAX_TITLE_CHARACTERS else text[:MAX_TITLE_CHARACTERS - 1] + "…"


class ConversationStore:
    """The application depends on these methods only; SQLite is the local implementation."""

    def create(self, user_id, *, provider, data_mode, title): raise NotImplementedError
    def append(self, user_id, conversation_id, *, request_id, asked_at, question, result, digest): raise NotImplementedError
    def list(self, user_id, *, data_mode, limit=LIST_LIMIT): raise NotImplementedError
    def load(self, user_id, conversation_id): raise NotImplementedError
    def delete(self, user_id, conversation_id): raise NotImplementedError
    def clear(self, user_id, *, data_mode): raise NotImplementedError
    def close(self): raise NotImplementedError


class SqliteConversationStore(ConversationStore):
    def __init__(self, path, *, clock=None):
        self._lock = RLock()
        self._closed = True
        self._clock = clock or _now
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise MemoryStoreError("invalid_memory_path")
        try:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE")
                objects = {row[0] for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects and version == 0:
                    self._connection.execute("""CREATE TABLE conversations (
                        conversation_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        data_mode TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL)""")
                    self._connection.execute("""CREATE TABLE turns (
                        conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        request_id TEXT NOT NULL,
                        asked_at TEXT NOT NULL,
                        question TEXT NOT NULL,
                        result BLOB NOT NULL CHECK (length(result) <= 1048576),
                        digest TEXT NOT NULL,
                        PRIMARY KEY (conversation_id, sequence))""")
                    self._connection.execute("CREATE INDEX conversations_by_user ON conversations(user_id, data_mode, updated_at)")
                    self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION or objects != {"conversations", "turns"}:
                    raise MemoryStoreError("memory_schema_mismatch")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA trusted_schema = OFF")
            self._closed = False
        except (sqlite3.Error, OSError):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise MemoryStoreError("memory_store_unavailable") from None
        except MemoryStoreError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    def _open(self):
        if self._closed:
            raise MemoryStoreError("memory_store_closed")

    def _owned(self, user_id, conversation_id):
        row = self._connection.execute(
            "SELECT conversation_id, title, provider, data_mode, created_at, updated_at FROM conversations "
            "WHERE conversation_id = ? AND user_id = ?", (_identifier(conversation_id), _user(user_id))).fetchone()
        if row is None:
            raise MemoryStoreError("conversation_not_found")
        return row

    def create(self, user_id, *, provider, data_mode, title):
        conversation_id, stamp = uuid4().hex, _timestamp(self._clock())
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (conversation_id, _user(user_id), title_for(title), str(provider), str(data_mode), stamp, stamp))
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None
        return conversation_id

    def append(self, user_id, conversation_id, *, request_id, asked_at, question, result, digest):
        try:
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            raise MemoryStoreError("invalid_memory_result") from None
        if len(body) > MAX_RESULT_BYTES:
            raise MemoryStoreError("memory_result_too_large")
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._owned(user_id, conversation_id)
                    count = self._connection.execute("SELECT count(*), coalesce(max(sequence), 0) FROM turns "
                                                     "WHERE conversation_id = ?", (conversation_id,)).fetchone()
                    if count[0] >= MAX_TURNS:
                        raise MemoryStoreError("conversation_full")
                    self._connection.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (conversation_id, count[1] + 1, str(request_id), _timestamp(asked_at), str(question), body,
                         str(digest)))
                    self._connection.execute("UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                                             (_timestamp(self._clock()), conversation_id))
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None

    def list(self, user_id, *, data_mode, limit=LIST_LIMIT):
        if type(limit) is not int or not 1 <= limit <= LIST_LIMIT:
            raise MemoryStoreError("invalid_memory_limit")
        with self._lock:
            self._open()
            try:
                rows = self._connection.execute(
                    "SELECT c.conversation_id, c.title, c.provider, c.created_at, c.updated_at, count(t.sequence) "
                    "FROM conversations c LEFT JOIN turns t ON t.conversation_id = c.conversation_id "
                    "WHERE c.user_id = ? AND c.data_mode = ? GROUP BY c.conversation_id "
                    "ORDER BY c.updated_at DESC, c.conversation_id LIMIT ?",
                    (_user(user_id), str(data_mode), limit)).fetchall()
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None
        return [{"conversation_id": r[0], "title": r[1], "provider": r[2], "created_at": r[3],
                 "updated_at": r[4], "turns": r[5]} for r in rows]

    def load(self, user_id, conversation_id):
        with self._lock:
            self._open()
            try:
                row = self._owned(user_id, conversation_id)
                turns = self._connection.execute(
                    "SELECT sequence, request_id, asked_at, question, result, digest FROM turns "
                    "WHERE conversation_id = ? ORDER BY sequence", (conversation_id,)).fetchall()
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None
        output = []
        for sequence, request_id, asked_at, question, result, digest in turns:
            try:
                output.append({"sequence": sequence, "request_id": request_id, "asked_at": asked_at,
                               "question": question, "result": json.loads(result), "digest": json.loads(digest)})
            except (TypeError, ValueError):
                raise MemoryStoreError("memory_corrupt") from None
        return {"conversation_id": row[0], "title": row[1], "provider": row[2], "data_mode": row[3],
                "created_at": row[4], "updated_at": row[5], "turns": output}

    def delete(self, user_id, conversation_id):
        with self._lock:
            self._open()
            try:
                with self._connection:
                    self._owned(user_id, conversation_id)
                    self._connection.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
                    self._connection.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None

    def clear(self, user_id, *, data_mode):
        with self._lock:
            self._open()
            try:
                with self._connection:
                    ids = [row[0] for row in self._connection.execute(
                        "SELECT conversation_id FROM conversations WHERE user_id = ? AND data_mode = ?",
                        (_user(user_id), str(data_mode))).fetchall()]
                    self._connection.executemany("DELETE FROM turns WHERE conversation_id = ?", [(i,) for i in ids])
                    self._connection.executemany("DELETE FROM conversations WHERE conversation_id = ?", [(i,) for i in ids])
            except sqlite3.Error:
                raise MemoryStoreError("memory_store_unavailable") from None
            return len(ids)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()
