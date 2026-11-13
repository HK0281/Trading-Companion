"""Temporary standalone databases; no real personal or conversation records are opened."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trading_companion.personal_memory import MAX_WATCHLIST, MAX_PREFERENCES, PersonalMemoryError
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from personal_memory_v1_fixture import legacy_v1_store


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


class PersonalPreferencesStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "preferences.sqlite3"
        self.store = self.build()

    def build(self, *, clock=lambda: NOW):
        store = SqlitePersonalMemoryStore(self.path, clock=clock)
        self.addCleanup(store.close)
        return store

    def create(self, code="000001.SZ", **changes):
        return self.store.patch_preferences(instrument_id=code, name="本机自报名称", changes=changes,
                                            expected_revision=None)["item"]

    def test_null_unknown_held_and_not_held_are_distinct_and_partial_updates_preserve_other_fields(self):
        initial = self.create(holding_status="unknown")
        self.assertIsNone(initial["horizon"])
        self.assertIsNone(initial["risk"])
        self.assertRegex(initial["revision"], r"^[0-9a-f]{32}$")
        peer = self.build(clock=lambda: NOW + timedelta(seconds=1))
        result = peer.patch_preferences(instrument_id=initial["instrument_id"], name="不得替换名称",
            expected_revision=initial["revision"], changes={"horizon": "long", "risk": "low"})
        updated = result["item"]
        self.assertEqual((result["created"], result["changed"]), (False, True))
        self.assertEqual((updated["holding_status"], updated["horizon"], updated["risk"]), ("unknown", "long", "low"))
        self.assertEqual(updated["name"], initial["name"])
        self.assertEqual(updated["created_at"], initial["created_at"])
        self.assertGreater(updated["updated_at"], initial["updated_at"])
        self.assertNotEqual(updated["revision"], initial["revision"])
        for value in ("held", "not_held", None):
            updated = peer.patch_preferences(instrument_id=initial["instrument_id"], changes={"holding_status": value},
                expected_revision=updated["revision"])["item"]
            self.assertEqual(updated["holding_status"], value)
            self.assertEqual((updated["horizon"], updated["risk"]), ("long", "low"))

    def test_same_values_preserve_timestamps_and_tokens_and_survive_restart(self):
        item = self.create(holding_status="held", horizon="short")
        before = self.store.list_preferences()
        peer = self.build(clock=lambda: NOW + timedelta(days=1))
        self.assertEqual(peer.patch_preferences(instrument_id=item["instrument_id"], expected_revision=item["revision"],
            changes={"holding_status": "held"}), {"created": False, "changed": False, "item": item})
        self.assertEqual(peer.list_preferences(), before)
        self.store.close()
        self.assertEqual(self.build().list_preferences(), before)

    def test_invalid_fields_and_clearing_the_last_value_are_rejected_without_losing_record(self):
        item = self.create(risk="medium")
        for changes, code in (({}, "invalid_preferences"), ({"cost": "10.4"}, "invalid_preferences"),
                              ({"risk": "积极"}, "invalid_preferences"), ({"risk": None}, "empty_preferences")):
            with self.subTest(changes=changes), self.assertRaisesRegex(PersonalMemoryError, code):
                self.store.patch_preferences(instrument_id=item["instrument_id"], expected_revision=item["revision"], changes=changes)
        with self.assertRaisesRegex(PersonalMemoryError, "empty_preferences"):
            self.create("000002.SZ", holding_status=None)
        self.assertEqual(self.store.list_preferences()["items"], [item])

    def test_stale_creation_and_update_revisions_never_overwrite_even_the_same_value(self):
        item = self.create(holding_status="held")
        for expected in (None, "a" * 32):
            with self.subTest(expected=expected), self.assertRaisesRegex(PersonalMemoryError, "preferences_conflict"):
                self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"holding_status": "held"},
                                             expected_revision=expected)
        with self.assertRaisesRegex(PersonalMemoryError, "preferences_conflict"):
            self.store.patch_preferences(instrument_id="000002.SZ", name="新记录", changes={"risk": "low"},
                                         expected_revision=item["revision"])
        with self.assertRaisesRegex(PersonalMemoryError, "invalid_preferences_revision"):
            self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"risk": "low"}, expected_revision="*")
        self.assertEqual(self.store.list_preferences()["items"], [item])

    def test_two_connections_racing_from_same_revision_get_one_success_and_one_conflict(self):
        item = self.create(holding_status="held")
        stores = [self.store, self.build()]

        def update(number):
            try:
                return stores[number].patch_preferences(instrument_id=item["instrument_id"],
                    changes={"horizon": "long"} if number == 0 else {"risk": "high"}, expected_revision=item["revision"])
            except PersonalMemoryError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, (0, 1)))
        self.assertEqual(results.count("preferences_conflict"), 1)
        current = self.store.list_preferences()["items"][0]
        self.assertEqual(current["holding_status"], "held")
        self.assertNotEqual(current["revision"], item["revision"])
        self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"horizon": "long", "risk": "high"},
                                     expected_revision=current["revision"])
        final = self.store.list_preferences()["items"][0]
        self.assertEqual((final["horizon"], final["risk"]), ("long", "high"))

    def test_preferences_capacity_is_atomic_and_independent_of_full_watchlist(self):
        for number in range(MAX_WATCHLIST):
            self.store.add(instrument_id=f"{number:06d}.SZ", name="自选测试")
        stores = [self.store, self.build()]

        def create(number):
            try:
                return stores[number % 2].patch_preferences(instrument_id=f"{number + 100:06d}.SZ", name="偏好测试",
                    changes={"risk": "low"}, expected_revision=None)["created"]
            except PersonalMemoryError as error:
                self.assertEqual(error.code, "preferences_full")
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(create, range(60)))
        self.assertEqual(sum(outcomes), MAX_PREFERENCES)
        self.assertEqual(len(self.store.list_preferences()["items"]), MAX_PREFERENCES)
        self.assertEqual(len(self.store.list()), MAX_WATCHLIST)
        item = self.store.list_preferences()["items"][0]
        self.assertFalse(self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"risk": "low"},
            expected_revision=item["revision"])["changed"])

    def test_deleted_then_recreated_record_rejects_old_row_and_collection_tokens(self):
        item = self.create(holding_status="held")
        old_collection = self.store.list_preferences()["collection_revision"]
        self.assertTrue(self.store.remove_preferences(instrument_id=item["instrument_id"], expected_revision=item["revision"]))
        self.assertFalse(self.store.remove_preferences(instrument_id=item["instrument_id"], expected_revision=item["revision"]))
        recreated = self.create(holding_status="held")
        self.assertNotEqual(recreated["revision"], item["revision"])
        for operation in (
                lambda: self.store.remove_preferences(instrument_id=item["instrument_id"], expected_revision=item["revision"]),
                lambda: self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"risk": "low"}, expected_revision=item["revision"]),
                lambda: self.store.clear_preferences(expected_revision=old_collection)):
            with self.subTest(operation=operation), self.assertRaisesRegex(PersonalMemoryError, "preferences_conflict"):
                operation()
        self.assertEqual(self.store.list_preferences()["items"], [recreated])

    def test_clear_token_tracks_the_whole_collection_and_delete_ranges_are_independent(self):
        self.store.add(instrument_id="000001.SZ", name="自选")
        self.create(holding_status="held")
        before = self.store.list_preferences()
        self.store.patch_preferences("other", instrument_id="000001.SZ", name="其他用户", changes={"risk": "low"}, expected_revision=None)
        self.assertEqual(self.store.list_preferences(), before)
        self.store.remove(instrument_id="000001.SZ")
        self.assertEqual(self.store.list_preferences(), before)
        self.store.add(instrument_id="000001.SZ", name="保留自选")
        self.create("000002.SZ", risk="high")
        with self.assertRaisesRegex(PersonalMemoryError, "preferences_conflict"):
            self.store.clear_preferences(expected_revision=before["collection_revision"])
        current = self.store.list_preferences()
        self.assertEqual(self.store.clear_preferences(expected_revision=current["collection_revision"]), 2)
        empty = self.store.list_preferences()
        self.assertEqual(empty["items"], [])
        self.assertEqual(self.store.clear_preferences(expected_revision=empty["collection_revision"]), 0)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(len(self.store.list_preferences("other")["items"]), 1)

    def test_corrupt_preference_revisions_categories_and_excess_rows_fail(self):
        self.create(holding_status="held")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE preferences SET revision = 'private-invalid-value'")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            self.store.list_preferences()
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            self.build()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE preferences SET revision = ?, risk = 'invalid-category'", ("a" * 32,))
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            self.store.list_preferences()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM preferences")
            connection.executemany("INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("local", f"{number:06d}.SZ", "测试", "held", None, None, NOW.isoformat(), NOW.isoformat(), f"{number:032x}")
                for number in range(51)])
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            self.store.list_preferences()

    def test_failed_write_and_invalid_clock_leave_original_profile_unchanged(self):
        item = self.create(holding_status="held")
        backward = self.build(clock=lambda: NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_time"):
            backward.patch_preferences(instrument_id=item["instrument_id"], changes={"risk": "low"}, expected_revision=item["revision"])
        self.store._connection.execute("PRAGMA query_only = ON")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_store_unavailable"):
            self.store.patch_preferences(instrument_id=item["instrument_id"], changes={"risk": "low"}, expected_revision=item["revision"])
        self.assertEqual(self.store.list_preferences()["items"], [item])


class PersonalMemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "actual-v1.sqlite3"

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return (connection.execute("PRAGMA user_version").fetchone(),
                    connection.execute("PRAGMA application_id").fetchone(),
                    connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall(),
                    connection.execute("SELECT * FROM watchlist ORDER BY user_id, instrument_id").fetchall())

    def generate_v1(self):
        with legacy_v1_store(self.path, clock=lambda: NOW) as legacy:
            legacy.add(instrument_id="000001.SZ", name="原始名称")
            legacy.add("other", instrument_id="300750.SZ", name="其他用户原名称")

    def test_actual_committed_v1_upgrades_without_changing_any_watchlist_rows_or_schema(self):
        self.generate_v1()
        before = self.snapshot()
        self.assertEqual(before[0], (1,))
        current = SqlitePersonalMemoryStore(self.path, clock=lambda: NOW + timedelta(days=1))
        self.addCleanup(current.close)
        after = self.snapshot()
        self.assertEqual(after[0], (2,))
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[3], before[3])
        self.assertEqual([row for row in after[2] if row[2] == "watchlist"], before[2])
        self.assertEqual(current.list()[0]["name"], "原始名称")
        self.assertEqual(current.list("other")[0]["name"], "其他用户原名称")
        self.assertEqual(current.list_preferences()["items"], [])
        current.close()
        reopened = SqlitePersonalMemoryStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(self.snapshot(), after)
        reopened.close()
        with self.assertRaisesRegex(ValueError, "personal_memory_schema_mismatch"):
            with legacy_v1_store(self.path, clock=lambda: NOW):
                self.fail("The old application must reject v2")

    def test_failure_after_table_and_version_change_rolls_back_the_complete_migration(self):
        self.generate_v1()
        before = self.snapshot()

        class FailingMigration(SqlitePersonalMemoryStore):
            def _validate_all(self, *, version=2):
                super()._validate_all(version=version)
                if version == 2:
                    raise PersonalMemoryError("personal_memory_store_unavailable")

        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_store_unavailable"):
            FailingMigration(self.path)
        self.assertEqual(self.snapshot(), before)
        with legacy_v1_store(self.path, clock=lambda: NOW) as legacy:
            self.assertEqual(len(legacy.list()), 1)

    def test_unknown_version_extra_objects_and_bad_nonlocal_rows_never_migrate(self):
        for mutation, error_code in (
                ("PRAGMA user_version = 99", "personal_memory_schema_mismatch"),
                ("CREATE INDEX stranger ON watchlist(name)", "personal_memory_schema_mismatch"),
                ("UPDATE watchlist SET name = char(10) WHERE user_id = 'other'", "personal_memory_corrupt"),
                ("UPDATE watchlist SET user_id = 'bad' || char(10) WHERE user_id = 'other'", "personal_memory_corrupt")):
            with self.subTest(mutation=mutation):
                if self.path.exists():
                    self.path.unlink()
                self.generate_v1()
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(mutation)
                before = self.snapshot()
                with self.assertRaisesRegex(PersonalMemoryError, error_code):
                    SqlitePersonalMemoryStore(self.path)
                self.assertEqual(self.snapshot(), before)

    def test_bad_v1_nonlocal_capacity_is_checked_before_schema_mutation(self):
        self.generate_v1()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO watchlist VALUES (?, ?, ?, ?, ?)", [
                ("other", f"{number:06d}.SZ", "测试", NOW.isoformat(), NOW.isoformat()) for number in range(50)])
        before = self.snapshot()
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
            SqlitePersonalMemoryStore(self.path)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
