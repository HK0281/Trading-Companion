"""Pinned background bytes are bounded, verified and parsed without a second read."""

from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.development_fixtures import ROOT, MARKET_CONTEXT_FIXTURE, load_development_market_context
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.market_context import MAX_SNAPSHOT_BYTES


class MarketContextFixtureTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / "data" / MARKET_CONTEXT_FIXTURE[0]).read_bytes()

    def test_pinned_fixture_is_explicitly_artificial(self):
        snapshot = load_development_market_context()
        self.assertTrue(snapshot.is_fixture)
        self.assertEqual(len(snapshot.calendars), 2)
        self.assertTrue(all(item.source == "synthetic_fixture" for item in snapshot.security_states))

    def test_same_verified_bytes_are_parsed_without_reopening(self):
        changed = self.raw.replace(b"artificial-market-context-20260920-v1", b"changed-after-hash")
        with patch.object(Path, "open", side_effect=[BytesIO(self.raw), BytesIO(changed)]) as opened:
            snapshot = load_development_market_context()
        opened.assert_called_once_with("rb")
        self.assertEqual(snapshot.snapshot_id, "artificial-market-context-20260920-v1")

    def test_line_endings_are_normalized_before_both_hash_and_parse(self):
        raw = self.raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with patch.object(Path, "open", return_value=BytesIO(raw)):
            self.assertTrue(load_development_market_context().is_fixture)

    def test_changed_fixture_is_rejected_without_exposing_content(self):
        changed = self.raw.replace(b"artificial-market-context-20260920-v1", b"private-changed-marker")
        with patch.object(Path, "open", return_value=BytesIO(changed)), self.assertRaises(DataValidationError) as error:
            load_development_market_context()
        self.assertNotIn("private-changed-marker", str(error.exception))

    def test_initial_read_is_bounded_before_hashing(self):
        class Tracked(BytesIO):
            sizes = []
            def read(self, size=-1):
                self.sizes.append(size)
                return super().read(size)
        handle = Tracked(b" " * (MAX_SNAPSHOT_BYTES + 2))
        with patch.object(Path, "open", return_value=handle), self.assertRaises(DataValidationError):
            load_development_market_context()
        self.assertEqual(handle.sizes, [MAX_SNAPSHOT_BYTES + 1])

    def test_missing_fixture_uses_fixed_error(self):
        with patch.object(Path, "open", side_effect=OSError("private-file-path")), self.assertRaises(DataSourceError) as error:
            load_development_market_context()
        self.assertNotIn("private-file-path", str(error.exception))


if __name__ == "__main__":
    unittest.main()
