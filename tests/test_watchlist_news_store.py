"""Bounded temporary inbox databases; no real user files, sources or model calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from time import monotonic
import unittest

from trading_companion.personal_memory import PersonalMemoryError
from trading_companion.personal_memory_store import (
    SqlitePersonalMemoryStore, APPLICATION_ID, _CREATE_TABLE, _CREATE_PREFERENCES,
)
from trading_companion.watchlist_news import SOURCES, MAX_MESSAGES, MAX_SEEN, WatchlistNewsError, decode_cursor


NOW = datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc)


def title(number=1, *, source=SOURCES[0], at=NOW, heading="平安银行公共标题"):
    published = at - timedelta(minutes=5)
    return {"title": heading, "url": (f"https://finance.sina.com.cn/stock/doc-test{number}.shtml" if source == SOURCES[0] else
            f"https://data.eastmoney.com/notices/detail/000001/AN{number:012d}.html"),
            "published_date": published.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            "published_at": published.isoformat(), "publication_precision": "datetime", "retrieved_at": at.isoformat()}


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "inbox.sqlite3"
        self.store = self.build()
        self.store.add(instrument_id="000001.SZ", name="平安银行")

    def build(self):
        store = SqlitePersonalMemoryStore(self.path, clock=lambda: NOW)
        self.addCleanup(store.close)
        return store

    def stock(self, store=None):
        return (store or self.store).inbox_snapshot()["items"][0]

    def ingest(self, items=(), *, store=None, source=SOURCES[0], snapshot=None, **kwargs):
        store = store or self.store
        stock = snapshot or self.stock(store)
        state = next(state for state in stock["sources"] if state["source"] == source)
        return store.ingest_inbox(instrument_id="000001.SZ", source=source, generation=stock["generation"],
            expected_revision=state["revision"], items=list(items), checked_at=kwargs.pop("checked_at", NOW), **kwargs)

    def test_snapshot_shared_generation_and_unchanged_public_watchlist_survive_restart(self):
        initial = self.store.list()
        stock = self.stock()
        self.assertEqual({row["source"] for row in stock["sources"]}, set(SOURCES))
        self.assertEqual(len({row["generation"] for row in stock["sources"]}), 1)
        self.assertEqual({row["status"] for row in stock["sources"]}, {"not_checked"})
        reopened = self.build()
        self.assertEqual(reopened.inbox_snapshot(), self.store.inbox_snapshot())
        self.assertEqual(reopened.list(), initial)
        self.assertEqual(set(initial[0]), {"instrument_id", "name", "created_at", "updated_at"})

    def test_each_source_builds_its_own_baseline_and_failure_never_advances_success(self):
        first = self.ingest([title()])
        self.assertTrue(first["baseline"])
        self.assertTrue(first["source_state"]["initialized"])
        failed = self.ingest(source=SOURCES[1], status="unavailable")
        self.assertFalse(failed["source_state"]["initialized"])
        self.assertIsNone(failed["source_state"]["last_success_at"])
        empty = self.ingest(source=SOURCES[1], status="empty")
        self.assertTrue(empty["source_state"]["initialized"])
        self.assertEqual(empty["source_state"]["last_success_at"], NOW.isoformat())
        second = self.ingest([title(2)])
        self.assertFalse(second["baseline"])
        self.assertEqual([item["baseline"] for item in self.store.list_inbox()["items"]].count(False), 1)

    def test_partial_saves_verified_titles_without_claiming_complete_success(self):
        result = self.ingest([title()], skipped_count=2)
        self.assertEqual((result["status"], result["saved"]), ("partial", 1))
        self.assertFalse(result["source_state"]["initialized"])
        self.assertIsNone(result["source_state"]["last_success_at"])
        self.assertEqual(self.store.list_inbox()["unread_count"], 1)

    def test_title_correction_preserves_first_seen_and_read_state_while_other_documents_remain_separate(self):
        self.ingest([title()])
        original = self.store.list_inbox()["items"][0]
        self.assertEqual(self.store.mark_inbox_read(message_id=original["message_id"]), {"changed": True})
        self.assertEqual(self.store.mark_inbox_read(message_id=original["message_id"]), {"changed": False})
        result = self.ingest([title(heading="平安银行标题更正"), title(2)])
        self.assertEqual((result["saved"], result["duplicates"]), (1, 1))
        all_items = self.store.list_inbox(unread_only=False)["items"]
        old = next(item for item in all_items if item["message_id"] == original["message_id"])
        self.assertTrue(old["read"])
        self.assertEqual(old["title"], original["title"])
        self.assertEqual(old["first_seen_at"], original["first_seen_at"])
        self.assertEqual(self.store.list_inbox()["unread_count"], 1)

    def test_sidecar_never_advances_manual_baseline_or_watermark_and_stale_source_revision_loses(self):
        before = self.stock()
        result = self.ingest([title()], origin="research", snapshot=before)
        self.assertIsNone(result["source_state"]["last_attempt_at"])
        self.assertIsNone(result["source_state"]["last_success_at"])
        self.assertFalse(result["source_state"]["initialized"])
        self.assertEqual(self.store.list_inbox()["items"][0]["discovery"], "research")
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_conflict"):
            self.ingest([title(2)], snapshot=before)

    def test_remove_readd_at_same_clock_and_clear_inbox_invalidate_old_work(self):
        before = self.stock()
        self.store.remove(instrument_id="000001.SZ")
        self.store.add(instrument_id="000001.SZ", name="平安银行")
        self.assertNotEqual(self.stock()["generation"], before["generation"])
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_conflict"):
            self.ingest([title()], snapshot=before)
        self.ingest([title()])
        old_work = self.stock()
        revision = self.store.inbox_snapshot()["collection_revision"]
        self.assertEqual(self.store.clear_inbox(expected_revision=revision), 1)
        self.assertEqual(self.store.list_inbox()["items"], [])
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_conflict"):
            self.ingest([title(2)], snapshot=old_work)
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_conflict"):
            self.store.clear_inbox(expected_revision=revision)
        self.assertTrue(self.ingest([title()])["baseline"])

    def test_read_changes_confirmation_revision_and_old_cursor_or_clear_confirmation_is_rejected(self):
        for offset in range(0, 25, 5):
            self.ingest([title(i) for i in range(offset, offset + 5)])
        first = self.store.list_inbox()
        self.assertEqual((len(first["items"]), first["unread_count"], first["has_more"]), (20, 25, True))
        second = self.store.list_inbox(cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse({item["message_id"] for item in first["items"]} & {item["message_id"] for item in second["items"]})
        self.store.mark_inbox_read(message_id=first["items"][0]["message_id"])
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_invalid_cursor"):
            self.store.list_inbox(cursor=first["next_cursor"])
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_conflict"):
            self.store.clear_inbox(expected_revision=first["collection_revision"])

    def test_same_discovery_batch_orders_publication_dates_instants_and_unknown_times_across_pages(self):
        records, groups = [], []
        for offset in (1, 2, 3):
            day = (NOW.date() - timedelta(days=offset)).isoformat()
            stamps = {
                "late_us2": day + "T18:00:00.000002+08:00",
                "late_us1": day + "T18:00:00.000001+08:00",
                "late": day + "T18:00:00+08:00",
                "middle": day + "T15:00:00+08:00",
                "ten": day + "T10:00:00+08:00",
                "nine_local": day + "T09:00:00+08:00",
                "nine_utc": day + "T01:00:00+00:00",
                "early": day + "T08:00:00+08:00",
                **{"date" + str(index): None for index in range(6)},
            }
            group = {}
            for index, (label, published_at) in enumerate(stamps.items()):
                value = title(offset * 100 + index)
                value.update(published_date=day, published_at=published_at,
                             publication_precision="date" if published_at is None else "datetime")
                group[label] = value["url"]
                records.append(value)
            groups.append(group)
        # Deliberately import the oldest records first; all imports have one discovery stamp.
        records.reverse()
        for offset in range(0, len(records), 5):
            self.ingest(records[offset:offset + 5])
        newer_discovery = title(9999, at=NOW - timedelta(days=5))
        self.ingest([newer_discovery], checked_at=NOW + timedelta(seconds=1))
        ids = dict(self.store._connection.execute("SELECT url, message_id FROM inbox_messages"))
        expected = [ids[newer_discovery["url"]]]
        for group in groups:
            expected.extend(ids[group[label]] for label in ("late_us2", "late_us1", "late", "middle", "ten"))
            expected.extend(sorted((ids[group["nine_local"]], ids[group["nine_utc"]]), reverse=True))
            expected.append(ids[group["early"]])
            expected.extend(sorted((ids[group["date" + str(index)]] for index in range(6)), reverse=True))
        pages, observed, cursor = [], [], None
        while True:
            page = self.store.list_inbox(cursor=cursor, now=NOW + timedelta(seconds=1))
            pages.append(page)
            observed.extend(page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertLess(len(pages), 4)
        self.assertEqual([len(page["items"]) for page in pages], [20, 20, 3])
        self.assertEqual([item["message_id"] for item in observed], expected)
        self.assertEqual(len({item["message_id"] for item in observed}), len(expected))
        self.assertTrue(all(item["published_at"] is None for item in observed if item["publication_precision"] == "date"))
        self.assertIsNone(decode_cursor(pages[1]["next_cursor"], "list")["published_at"])
        self.assertEqual(self.store.list_inbox(cursor=pages[0]["next_cursor"], now=NOW + timedelta(seconds=1)), pages[1])

    def test_collection_and_message_ownership_are_isolated(self):
        self.ingest([title()])
        item = self.store.list_inbox()["items"][0]
        self.assertEqual(self.store.list_inbox("other")["items"], [])
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_not_found"):
            self.store.mark_inbox_read("other", message_id=item["message_id"])
        self.store.clear("other")
        self.assertEqual(self.store.list_inbox()["unread_count"], 1)
        self.store.clear()
        self.assertEqual(self.store.list_inbox()["source_states"], [])
        for table in ("inbox_source_state", "inbox_messages", "inbox_seen"):
            self.assertEqual(self.store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_capacity_evicts_read_first_and_keeps_seen_identity_after_message_eviction(self):
        for offset in range(0, MAX_MESSAGES, 5):
            self.ingest([title(i) for i in range(offset, offset + 5)])
        read = self.store.list_inbox()["items"][0]
        self.store.mark_inbox_read(message_id=read["message_id"])
        self.ingest([title(MAX_MESSAGES)])
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM inbox_messages").fetchone()[0], MAX_MESSAGES)
        self.assertIsNone(self.store._connection.execute("SELECT 1 FROM inbox_messages WHERE message_id = ?", (read["message_id"],)).fetchone())
        duplicate = next(i for i in range(MAX_MESSAGES) if title(i)["url"] == read["url"])
        self.assertEqual(self.ingest([title(duplicate)])["saved"], 0)
        self.assertEqual(self.store.list_inbox()["source_states"][0]["evicted_unread_count"], 0)
        self.ingest([title(MAX_MESSAGES + 1)])
        self.assertEqual(self.store.list_inbox()["source_states"][0]["evicted_unread_count"], 1)

    def test_expired_unread_removal_is_reported_and_bounded_seen_capacity_cannot_silently_forget(self):
        old = NOW - timedelta(days=91)
        self.ingest([title(at=old)], checked_at=old)
        current = self.store.list_inbox(now=NOW)
        self.assertEqual(current["unread_count"], 0)
        self.assertEqual(current["source_states"][0]["evicted_unread_count"], 1)
        with self.store._connection:
            self.store._connection.executemany("INSERT INTO inbox_seen VALUES (?, ?, ?, ?, ?)",
                [("local", "000001.SZ", SOURCES[0], f"{i:064x}", NOW.date().isoformat()) for i in range(MAX_SEEN)])
        result = self.ingest([title(9)])
        self.assertEqual((result["saved"], result["code"]), (0, "inbox_seen_capacity"))
        self.assertEqual(result["source_state"]["last_success_at"], old.isoformat())
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM inbox_seen").fetchone()[0], MAX_SEEN)

    def test_two_connections_cannot_publish_the_same_revision_twice(self):
        before = self.stock()
        stores = (self.store, self.build())
        def run(index):
            try:
                return self.ingest([title(index)], snapshot=before, store=stores[index])["saved"]
            except WatchlistNewsError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, (0, 1)))
        self.assertEqual(outcomes.count(1), 1)
        self.assertEqual(outcomes.count("inbox_conflict"), 1)

    def test_before_commit_callback_is_validated_and_exception_rolls_back_the_whole_ingest(self):
        before = self.store.inbox_snapshot()
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_invalid_request"):
            self.ingest([title()], before_commit=False)
        def stop():
            raise WatchlistNewsError("inbox_cancelled")
        with self.assertRaisesRegex(WatchlistNewsError, "inbox_cancelled"):
            self.ingest([title()], before_commit=stop)
        self.assertEqual(self.store.inbox_snapshot(), before)
        self.assertEqual(self.store.list_inbox()["items"], [])
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM inbox_seen").fetchone()[0], 0)

    def test_sidecar_lock_and_sqlite_busy_wait_share_deadline_and_restore_normal_timeout(self):
        entered, release = Event(), Event()
        def hold():
            with self.store._lock:
                entered.set()
                release.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hold)
            self.assertTrue(entered.wait(1))
            started = monotonic()
            try:
                with self.assertRaisesRegex(WatchlistNewsError, "inbox_store_timeout"):
                    self.store.inbox_snapshot(deadline=started + .04)
                self.assertLess(monotonic() - started, .2)
            finally:
                release.set()
            future.result()
        stock = self.stock()
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = monotonic()
            with self.assertRaisesRegex(WatchlistNewsError, "inbox_store_timeout"):
                self.ingest([title()], snapshot=stock, deadline=started + .04)
            self.assertLess(monotonic() - started, .2)
            blocker.rollback()
        self.assertEqual(self.store._connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertFalse(self.stock()["sources"][0]["initialized"])
        self.assertEqual(self.ingest([title()])["saved"], 1)


class InboxMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "v2.sqlite3"

    def make_v2(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(_CREATE_TABLE)
            connection.execute(_CREATE_PREFERENCES)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute("PRAGMA user_version = 2")
            for owner in ("local", "other"):
                connection.execute("INSERT INTO watchlist VALUES (?, ?, ?, ?, ?)", (owner, "000001.SZ", "原名称", NOW.isoformat(), NOW.isoformat()))
                connection.execute("INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner, "000002.SZ", "偏好原名称", "held", "short", None, NOW.isoformat(), NOW.isoformat(), "b" * 32))

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return (connection.execute("PRAGMA user_version").fetchone(),
                    connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall(),
                    {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() for table in tables})

    def test_v2_upgrade_preserves_all_old_rows_schema_and_creates_distinct_memberships(self):
        self.make_v2()
        before = self.snapshot()
        store = SqlitePersonalMemoryStore(self.path)
        self.addCleanup(store.close)
        after = self.snapshot()
        self.assertEqual(after[0], (3,))
        for table in ("watchlist", "preferences"):
            self.assertEqual(after[2][table], before[2][table])
            self.assertEqual([row for row in after[1] if row[2] == table], [row for row in before[1] if row[2] == table])
        self.assertNotEqual(store.inbox_snapshot()["items"][0]["generation"], store.inbox_snapshot("other")["items"][0]["generation"])

    def test_v3_final_validation_failure_rolls_back_tables_version_and_original_rows(self):
        self.make_v2()
        before = self.snapshot()
        class Failing(SqlitePersonalMemoryStore):
            def _validate_all(self, *, version=3):
                super()._validate_all(version=version)
                if version == 3:
                    raise PersonalMemoryError("personal_memory_store_unavailable")
        with self.assertRaisesRegex(PersonalMemoryError, "personal_memory_store_unavailable"):
            Failing(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_bad_nonlocal_data_or_extra_object_is_rejected_before_any_v3_mutation(self):
        for mutation in ("UPDATE preferences SET revision = 'bad' WHERE user_id = 'other'",
                         "CREATE INDEX stranger ON watchlist(name)"):
            with self.subTest(mutation=mutation):
                if self.path.exists():
                    self.path.unlink()
                self.make_v2()
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(mutation)
                before = self.snapshot()
                with self.assertRaises(PersonalMemoryError):
                    SqlitePersonalMemoryStore(self.path)
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
