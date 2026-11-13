"""Public local-watchlist constraints, independent of research and model state."""

from datetime import datetime, timedelta, timezone
import unittest

from trading_companion.personal_memory import (
    PersonalMemoryError, checked_instrument_id, checked_name, checked_record, checked_user, timestamp,
    checked_preferences_changes, checked_preference_record, checked_revision, preferences_collection_revision,
)


class PersonalMemoryTests(unittest.TestCase):
    def test_complete_a_share_codes_only(self):
        for code in ("000001.SZ", "300750.SZ", "600000.SH", "430001.BJ", "920001.BJ"):
            self.assertEqual(checked_instrument_id(code), code)
        for code in ("000001", "000001.SH", "600000.SZ", "000001.sz", "00000１.SZ", None, 1, "平安银行"):
            with self.subTest(code=code), self.assertRaisesRegex(PersonalMemoryError, "invalid_watchlist_instrument"):
                checked_instrument_id(code)

    def test_names_and_users_are_bounded_printable_text(self):
        self.assertEqual(checked_name(" 平安银行 "), "平安银行")
        self.assertEqual(checked_user("local"), "local")
        for value in ("", " ", None, "x" * 81, "平安\n银行"):
            with self.subTest(value=value), self.assertRaisesRegex(PersonalMemoryError, "invalid_watchlist_name"):
                checked_name(value)
        for value in ("", " local", None, "x" * 65, "local\x00"):
            with self.subTest(value=value), self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_user"):
                checked_user(value)

    def test_record_timestamp_is_aware_utc_and_update_cannot_precede_creation(self):
        time = datetime(2026, 11, 12, 12, 30, 10, 999, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(timestamp(time), "2026-11-12T04:30:10+00:00")
        item = {"instrument_id": "000001.SZ", "name": "平安银行",
                "created_at": timestamp(time), "updated_at": timestamp(time)}
        self.assertEqual(checked_record(item), item)
        self.assertIsNot(checked_record(item), item)
        for update in (
                {"created_at": "2026-11-12T04:30:10"}, {"updated_at": "2026-11-11T04:30:10+00:00"},
                {"created_at": "2026-11-12T12:30:10+08:00"}, {"updated_at": "2026-11-12T04:30:10Z"},
                {"updated_at": 1}, {"extra": "ignored?"}, {"name": " 平安银行"},
                {"instrument_id": "000001.SH"}, {"created_at": "not a date"}):
            with self.subTest(update=update), self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
                checked_record({**item, **update})
        with self.assertRaisesRegex(PersonalMemoryError, "invalid_personal_memory_time"):
            timestamp(datetime(2026, 11, 12))

    def test_error_does_not_echo_unknown_values(self):
        self.assertEqual(str(PersonalMemoryError("secret local database path")), "personal_memory_store_unavailable")

    def test_preference_categories_only_accept_explicit_enumerations_or_null(self):
        self.assertEqual(checked_preferences_changes({"holding_status": "unknown", "risk": None}),
                         {"holding_status": "unknown", "risk": None})
        for changes in ({}, None, [], {"horizon": "三年"}, {"risk": 1}, {"holding_status": False},
                        {"cost": "10.4"}, {"position": "20%"}, {"risk": "high", "note": "free text"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(PersonalMemoryError, "invalid_preferences"):
                checked_preferences_changes(changes)

    def test_preference_revisions_and_records_are_strict_and_nonempty(self):
        stamp = "2026-11-12T04:30:10+00:00"
        profile = {"instrument_id": "000001.SZ", "name": "平安银行", "holding_status": "unknown", "horizon": None,
                   "risk": None, "created_at": stamp, "updated_at": stamp, "revision": "a" * 32}
        self.assertEqual(checked_preference_record(profile), profile)
        for update in ({"revision": "A" * 32}, {"revision": "a" * 64}, {"revision": None},
                       {"holding_status": None}, {"holding_status": "未持有"}, {"cost": "secret"}):
            with self.subTest(update=update), self.assertRaisesRegex(PersonalMemoryError, "personal_memory_corrupt"):
                checked_preference_record({**profile, **update})
        for value in (None, "", "G" * 32, "a" * 64, ["a" * 32]):
            with self.subTest(value=value), self.assertRaisesRegex(PersonalMemoryError, "invalid_preferences_revision"):
                checked_revision(value)
        self.assertEqual(checked_revision("a" * 64, collection=True), "a" * 64)
        second = {**profile, "instrument_id": "000002.SZ", "revision": "b" * 32}
        original = preferences_collection_revision([profile, second])
        self.assertRegex(original, r"^[0-9a-f]{64}$")
        self.assertEqual(original, preferences_collection_revision([second, profile]))
        self.assertNotEqual(original, preferences_collection_revision([profile, {**second, "risk": "low"}]))
        self.assertNotEqual(preferences_collection_revision([]), preferences_collection_revision([], "other"))


if __name__ == "__main__":
    unittest.main()
