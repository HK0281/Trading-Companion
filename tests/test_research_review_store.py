"""Bounded source reads, damaged rows and ownership without opening user databases."""

from datetime import datetime, timezone
import unittest

from trading_companion.memory_store import MAX_RESULT_BYTES, MemoryStoreError, SqliteConversationStore


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteConversationStore(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.conversation = self.store.create("local", provider="fake", data_mode="tencent", title="test")

    def add(self, conversation=None, user="local"):
        self.store.append(user, conversation or self.conversation, request_id="request", asked_at=NOW, question="问题",
                          result={"ok": True}, digest="never decoded")

    def get(self, **options):
        return self.store.get_result(options.get("user", "local"), options.get("conversation", self.conversation),
            sequence=options.get("sequence", 1), request_id=options.get("request_id", "request"),
            data_mode=options.get("data_mode", "tencent"))

    def test_owned_single_result_includes_conversation_updated_at_without_digest(self):
        self.add()
        value = self.get()
        self.assertEqual(value["result"], {"ok": True})
        self.assertEqual(value["conversation_updated_at"], NOW.isoformat(timespec="seconds"))
        self.assertNotIn("digest", value)
        for changed in (dict(user="other"), dict(data_mode="akshare_eastmoney"), dict(sequence=2), dict(request_id="wrong")):
            with self.subTest(changed=changed), self.assertRaisesRegex(MemoryStoreError, "conversation_not_found"):
                self.get(**changed)

    def test_scanning_is_capped_sorted_and_excludes_other_user_and_mode(self):
        for _ in range(21):
            self.add()
        for user, mode in (("other", "tencent"), ("local", "akshare_eastmoney")):
            cid = self.store.create(user, provider="fake", data_mode=mode, title="isolated")
            self.add(cid, user)
        page = self.store.scan_results("local", data_mode="tencent")
        self.assertEqual(len(page["rows"]), 20)
        self.assertTrue(page["has_more"])
        self.assertEqual([r["sequence"] for r in page["rows"]], list(range(21, 1, -1)))
        for invalid in (21, 0, True):
            with self.assertRaisesRegex(MemoryStoreError, "invalid_memory_limit"):
                self.store.scan_results("local", data_mode="tencent", limit=invalid)

    def test_oversized_and_invalid_json_are_skipped_without_returning_body(self):
        self.add()
        connection = self.store._connection
        for body in (b"{" , b'{"duplicate":1,"duplicate":2}', b'{"not_finite":NaN}', b"[1]",
                     "中" * (MAX_RESULT_BYTES // 2), b"x" * (MAX_RESULT_BYTES + 1)):
            with self.subTest(size=len(body)):
                connection.execute("PRAGMA ignore_check_constraints = ON")
                with connection:
                    connection.execute("UPDATE turns SET result = ?", (body,))
                value = self.store.scan_results("local", data_mode="tencent")["rows"][0]
                self.assertTrue(value["invalid"])
                self.assertIsNone(value["result"])
                self.assertEqual(value["sequence"], 1)

    def test_single_read_is_bounded_even_if_whole_conversation_is_corrupt(self):
        self.add()
        self.add()
        with self.store._connection:
            self.store._connection.execute("UPDATE turns SET result = ? WHERE sequence = 2", (b"broken",))
        self.assertEqual(self.get()["result"], {"ok": True})
        self.assertTrue(self.get(sequence=2)["invalid"])
        self.store.clear("local", data_mode="tencent")
        with self.assertRaisesRegex(MemoryStoreError, "conversation_not_found"):
            self.get()

    def test_question_and_other_metadata_are_bounded_on_corrupt_source(self):
        self.add()
        with self.store._connection:
            self.store._connection.execute("UPDATE turns SET question = ?, request_id = ?", ("问" * 5000, "r" * 5000))
            self.store._connection.execute("UPDATE conversations SET updated_at = ?", ("t" * 5000,))
        value = self.store.scan_results("local", data_mode="tencent")["rows"][0]
        self.assertTrue(value["invalid"])
        self.assertEqual(len(value["question"]), 160)
        self.assertEqual(len(value["request_id"]), 101)
        self.assertEqual(len(value["conversation_updated_at"]), 100)
        with self.store._connection:
            self.store._connection.execute("UPDATE turns SET request_id = ?", ("request",))
        detail = self.get()
        self.assertTrue(detail["invalid"])
        self.assertIsNone(detail["question"])


if __name__ == "__main__":
    unittest.main()
