"""Temporary standalone databases; no real personal or conversation records are opened."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trading_companion.personal_memory import MAX_WATCHLIST, PersonalMemoryError
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore


NOW = datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc)


class PersonalMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "personal.sqlite3"
        self.store = self.build()

    def build(self, **kwargs):
        store = SqlitePersonalMemoryStore(self.path, clock=kwargs.get("clock", lambda: NOW))
        self.addCleanup(store.close)
        return store

    def test_restart_and_independent_instances_share_the_same_watchlist(self):
        self.assertEqual(self.store.list(), [])
        result = self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.assertTrue(result["added"])
        self.store.close()
        reopened = self.build()
        self.assertEqual(reopened.list(), [result["item"]])
        peer = self.build()
        peer.add(instrument_id="300750.SZ", name="宁德时代")
        self.assertEqual([item["instrument_id"] for item in reopened.list()], ["000001.SZ", "300750.SZ"])

    def test_duplicate_preserves_original_name_and_times_even_at_capacity(self):
        for number in range(MAX_WATCHLIST):
            self.store.add(instrument_id=f"{number:06d}.SZ", name=f"测试股票{number}")
        original = self.store.list()[0]
        peer = self.build(clock=lambda: NOW + timedelta(days=1))
        self.assertEqual(peer.add(instrument_id=original["instrument_id"], name="改名"),
                         {"added": False, "item": original})
        with self.assertRaisesRegex(PersonalMemoryError, "watchlist_full"):
            peer.add(instrument_id="000099.SZ", name="超额")
        self.assertEqual(len(self.store.list()), MAX_WATCHLIST)

    def test_capacity_is_atomic_between_two_connections(self):
        peer = self.build()
        stores = [self.store, peer]

        def add(number):
            try:
                return stores[number % 2].add(instrument_id=f"{number:06d}.SZ", name=f"股票{number}")["added"]
            except PersonalMemoryError as error:
                self.assertEqual(error.code, "watchlist_full")
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(add, range(60)))
        self.assertEqual(sum(outcomes), MAX_WATCHLIST)
        self.assertEqual(len(self.store.list()), MAX_WATCHLIST)

    def test_remove_clear_and_user_ownership_are_idempotent(self):
        self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.store.add("other", instrument_id="000001.SZ", name="另一位用户")
        self.assertTrue(self.store.remove(instrument_id="000001.SZ"))
        self.assertFalse(self.store.remove(instrument_id="000001.SZ"))
        self.assertEqual(self.store.clear(), 0)
        self.assertEqual(len(self.store.list("other")), 1)
        self.assertEqual(self.store.clear("other"), 1)
        self.assertEqual(self.store.clear("other"), 0)

    def test_returned_records_cannot_modify_persistent_data(self):
        response = self.store.add(instrument_id="000001.SZ", name="平安银行")
        response["item"]["name"] = "已修改副本"
        self.store.list()[0]["name"] = "另一个副本"
        self.assertEqual(self.store.list()[0]["name"], "平安银行")

    def test_external_corrupt_record_or_excess_rows_are_not_shown_as_empty(self):
        self.store.add(instrument_id="000001.SZ", name="平安银行")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE watchlist SET name = ?", ("秘密\n内容",))
        for operation in (self.store.list, lambda: self.store.add(instrument_id="000002.SZ", name="新股"),
                          lambda: self.store.remove(instrument_id="000001.SZ"), self.store.clear, self.build):
            with self.subTest(operation=operation), self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
                operation()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM watchlist")
            connection.executemany("INSERT INTO watchlist VALUES (?, ?, ?, ?, ?)", [
                ("local", f"{number:06d}.SZ", "股票", NOW.isoformat(), NOW.isoformat()) for number in range(51)])
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            self.store.list()

    def test_unknown_version_schema_and_added_trigger_fail_without_rebuilding(self):
        self.store.add(instrument_id="000001.SZ", name="平安银行")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_schema_mismatch"):
            self.build()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT name FROM watchlist").fetchall(), [("平安银行",)])
            connection.execute("PRAGMA user_version = 1")
            connection.execute("CREATE TRIGGER surprise AFTER INSERT ON watchlist BEGIN DELETE FROM watchlist; END")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_schema_mismatch"):
            self.store.add(instrument_id="000002.SZ", name="新股")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM watchlist").fetchone()[0], 1)

    def test_wrong_database_and_wrong_table_definition_are_rejected(self):
        self.store.close()
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE conversations (secret TEXT)")
            connection.execute("INSERT INTO conversations VALUES ('do not expose')")
            connection.execute("PRAGMA user_version = 1")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_schema_mismatch"):
            self.build()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT secret FROM conversations").fetchone()[0], "do not expose")
            connection.execute("DROP TABLE conversations")
            connection.execute("CREATE TABLE watchlist (user_id TEXT, instrument_id TEXT, name TEXT, created_at TEXT, updated_at TEXT)")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_schema_mismatch"):
            self.build()

    def test_unreadable_write_closed_and_invalid_input_use_fixed_codes(self):
        self.store._connection.execute("PRAGMA query_only = ON")
        with self.assertRaisesRegex(PersonalMemoryError, "^personal_memory_store_unavailable$"):
            self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.assertEqual(self.store.list(), [])
        self.store.close()
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_store_closed"):
            self.store.list()
        for path in (None, "", "\x00private-path"):
            with self.subTest(path=path), self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_path"):
                SqlitePersonalMemoryStore(path)
        corrupt = Path(self.temporary.name) / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite; secret content")
        with self.assertRaisesRegex(PersonalMemoryError, "^personal_memory_store_unavailable$"):
            SqlitePersonalMemoryStore(corrupt)

    def test_invalid_time_rolls_back_insertion(self):
        peer = self.build(clock=lambda: datetime(2026, 11, 12))
        with self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_time"):
            peer.add(instrument_id="000001.SZ", name="平安银行")
        self.assertEqual(self.store.list(), [])
        with self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_time"):
            self.build(clock=0)

        def unavailable_clock():
            raise RuntimeError("sensitive diagnostic")

        failed_clock = self.build(clock=unavailable_clock)
        with self.assertRaisesRegex(PersonalMemoryError, "^invalid_personal_memory_time$"):
            failed_clock.add(instrument_id="000001.SZ", name="平安银行")
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
