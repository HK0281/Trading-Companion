import json
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from trading_companion.analysis import analyze_daily_bars
from trading_companion.csv_loader import load_daily_bars
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError


SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "sample_daily_bars_synthetic.csv"


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = load_daily_bars(SAMPLE_PATH)

    def source(self, bars=None, error=None):
        return SimpleNamespace(
            source_name="test_source", adjustment="unadjusted",
            read_daily_bars=Mock(return_value=self.bars if bars is None else bars, side_effect=error),
        )

    def test_weekend_request_preserves_actual_trade_date_and_provenance(self) -> None:
        result = analyze_daily_bars(CsvDailyBarSource(SAMPLE_PATH), as_of_date=date(2026, 9, 27))
        payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["requested_as_of_date"], "2026-09-27")
        self.assertEqual(payload["latest_trade_date"], "2026-09-25")
        self.assertEqual(payload["source"], "csv")
        self.assertEqual(payload["price_adjustment"], "unknown")
        self.assertTrue(payload["retrieved_at"].endswith("+00:00"))
        self.assertEqual(payload["bars_used"], 5)
        self.assertIsNone(payload["rows"][0]["sma_close"])

    def test_cutoff_excludes_later_prices(self) -> None:
        result = analyze_daily_bars(self.source(), as_of_date=date(2026, 9, 23))
        self.assertEqual(len(result.rows), 3)
        self.assertAlmostEqual(result.rows[-1].sma_close, (10.20 + 10.35 + 10.10) / 3)
        self.assertEqual(result.latest_trade_date, date(2026, 9, 23))

    def test_insufficient_history_retains_bars_without_inventing_sma(self) -> None:
        result = analyze_daily_bars(self.source(), as_of_date=date(2026, 9, 25), window=20)
        self.assertEqual(result.status, "insufficient_history")
        self.assertEqual(result.to_dict()["missing_bars"], 15)
        self.assertEqual(len(result.rows), 5)
        self.assertTrue(all(row.sma_close is None for row in result.rows))

    def test_empty_source_and_no_matching_bars_are_distinct(self) -> None:
        empty = analyze_daily_bars(self.source([]), as_of_date=date(2026, 9, 25))
        filtered = analyze_daily_bars(self.source(), as_of_date=date(2026, 9, 20))
        self.assertEqual((empty.status, filtered.status), ("no_data", "no_data"))
        self.assertEqual(empty.reason, "empty_source")
        self.assertEqual(filtered.reason, "no_matching_bars")
        self.assertIsNone(empty.latest_trade_date)

    def test_source_and_validation_failures_are_not_no_data(self) -> None:
        for error, status in (
            (DataSourceError("Timed out"), "source_error"),
            (DataValidationError("Invalid OHLC"), "invalid_data"),
        ):
            with self.subTest(status=status):
                result = analyze_daily_bars(self.source(error=error), as_of_date=date(2026, 9, 25))
                self.assertEqual(result.status, status)
                self.assertIsNone(result.retrieved_at)
                self.assertIsNone(result.to_dict()["missing_bars"])
                self.assertEqual(result.rows, ())

    def test_invalid_requests_do_not_read_source(self) -> None:
        for overrides in (
            {"window": 0}, {"window": True}, {"window": 2.5},
            {"as_of_date": "2026-09-25"}, {"start_date": date(2026, 9, 26)},
            {"instrument_id": " "},
        ):
            with self.subTest(overrides=overrides):
                source = self.source()
                kwargs = {"as_of_date": date(2026, 9, 25), **overrides}
                with self.assertRaises(InvalidRequestError):
                    analyze_daily_bars(source, **kwargs)
                source.read_daily_bars.assert_not_called()

    def test_mixed_duplicate_and_out_of_order_series_are_invalid(self) -> None:
        invalid_series = (
            [self.bars[0], replace(self.bars[1], instrument_id="600000.SH")],
            [self.bars[0], self.bars[0]],
            list(reversed(self.bars)),
        )
        for bars in invalid_series:
            with self.subTest(bars=bars):
                result = analyze_daily_bars(self.source(bars), as_of_date=date(2026, 9, 25))
                self.assertEqual(result.status, "invalid_data")
                self.assertEqual(result.rows, ())

    def test_explicit_symbol_and_start_filter_a_multi_stock_file(self) -> None:
        bars = [replace(self.bars[0], instrument_id="600000.SH"), *self.bars]
        result = analyze_daily_bars(
            self.source(bars), as_of_date=date(2026, 9, 25), start_date=date(2026, 9, 23),
            instrument_id="000001.SZ", window=3,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[0].bar.trade_date, date(2026, 9, 23))
        self.assertTrue(all(row.bar.instrument_id == "000001.SZ" for row in result.rows))

    def test_configured_source_must_match_requested_symbol(self) -> None:
        source = self.source()
        source.instrument_id = "000001.SZ"
        with self.assertRaises(InvalidRequestError):
            analyze_daily_bars(source, as_of_date=date(2026, 9, 25), instrument_id="600000.SH")
        source.read_daily_bars.assert_not_called()

    def test_unexpected_programming_errors_are_not_hidden(self) -> None:
        with self.assertRaises(RuntimeError):
            analyze_daily_bars(self.source(error=RuntimeError("Bug")), as_of_date=date(2026, 9, 25))


if __name__ == "__main__":
    unittest.main()
