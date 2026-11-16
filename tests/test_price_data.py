"""Daily bars, trusted CSV loading and historical indicator series."""

from datetime import date
from pathlib import Path
import tempfile
import unittest

from trading_companion.analysis import analyze_daily_bars
from trading_companion.csv_loader import load_daily_bars
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.history import bars_as_of_close
from trading_companion.indicators import ema_close_points, macd_close_points, sma_close_points


# ---- test_daily_bar

class DailyBarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_bar = {
            "instrument_id": "600519.SH",
            "trade_date": date(2026, 9, 25),
            "open_price": 100.0,
            "high_price": 105.0,
            "low_price": 98.0,
            "close_price": 102.0,
            "volume_shares": 1000,
            "amount_cny": 101000.0,
        }

    def test_accepts_valid_daily_bar(self) -> None:
        bar = DailyBar(**self.valid_bar)
        self.assertEqual(bar.close_price, 102.0)

    def test_rejects_price_outside_daily_range(self) -> None:
        values = {**self.valid_bar, "close_price": 106.0}
        with self.assertRaisesRegex(ValueError, "between low_price and high_price"):
            DailyBar(**values)

    def test_rejects_nonfinite_price(self) -> None:
        values = {**self.valid_bar, "open_price": float("nan")}
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            DailyBar(**values)

    def test_rejects_invalid_volume_and_amount(self) -> None:
        for field, value in (("volume_shares", -1), ("amount_cny", -1.0)):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    DailyBar(**{**self.valid_bar, field: value})


# ---- test_csv_loader

class CsvLoaderTests(unittest.TestCase):
    header = (
        "instrument_id,trade_date,open_price,high_price,low_price,"
        "close_price,volume_shares,amount_cny\n"
    )
    valid_row = "000001.SZ,2026-09-21,10.00,10.30,9.90,10.20,100000,1010000.00\n"

    def test_malformed_files_are_invalid_data_through_analysis(self) -> None:
        malformed = (
            "", "instrument_id,trade_date\n",
            self.header.strip() + ",close_price\n" + self.valid_row.strip() + ",10.20\n",
            self.header + self.valid_row.strip() + ",unexpected\n",
            self.header + "000001.SZ,2026-09-21,10\n",
            self.header + '"unterminated',
            self.header + self.valid_row.replace("10.20", "NaN"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "invalid.csv"
            for content in malformed:
                with self.subTest(content=content):
                    csv_path.write_text(content, encoding="utf-8")
                    result = analyze_daily_bars(
                        CsvDailyBarSource(csv_path), as_of_date=date(2026, 9, 25),
                    )
                    self.assertEqual(result.status, "invalid_data")
                    self.assertEqual(result.rows, ())

    def test_header_only_is_no_data_not_invalid_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "empty.csv"
            csv_path.write_text(self.header, encoding="utf-8")
            result = analyze_daily_bars(CsvDailyBarSource(csv_path), as_of_date=date(2026, 9, 25))
            self.assertEqual(result.status, "no_data")
            self.assertEqual(result.reason, "empty_source")

    def test_invalid_encoding_is_invalid_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "invalid_encoding.csv"
            csv_path.write_bytes(b"\xff\xfe")
            result = analyze_daily_bars(CsvDailyBarSource(csv_path), as_of_date=date(2026, 9, 25))
            self.assertEqual(result.status, "invalid_data")

    def test_loads_synthetic_sample(self) -> None:
        sample_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "sample_daily_bars_synthetic.csv"
        )
        bars = load_daily_bars(sample_path)

        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0].instrument_id, "000001.SZ")
        self.assertEqual(bars[-1].trade_date, date(2026, 9, 25))
        self.assertEqual(bars[-1].close_price, 10.40)

    def test_reports_invalid_row_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "invalid.csv"
            csv_path.write_text(
                "instrument_id,trade_date,open_price,high_price,low_price,"
                "close_price,volume_shares,amount_cny\n"
                "000001.SZ,2026-09-21,10.00,10.30,9.90,10.40,100000,1010000.00\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid CSV row 2"):
                load_daily_bars(csv_path)


# ---- test_history

class BarsAsOfCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        sample_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "sample_daily_bars_synthetic.csv"
        )
        self.bars = load_daily_bars(sample_path)

    def test_sma_at_past_date_cannot_use_later_bars(self) -> None:
        snapshot = bars_as_of_close(self.bars, date(2026, 9, 23))
        points = sma_close_points(snapshot, window=3)

        self.assertEqual(len(snapshot), 3)
        self.assertEqual(points[-1].trade_date, date(2026, 9, 23))
        self.assertAlmostEqual(points[-1].value, (10.20 + 10.35 + 10.10) / 3)

    def test_date_before_first_bar_has_no_data(self) -> None:
        self.assertEqual(bars_as_of_close(self.bars, date(2026, 9, 20)), [])

    def test_ema_at_past_date_cannot_use_later_bars(self) -> None:
        snapshot = bars_as_of_close(self.bars, date(2026, 9, 23))
        points = ema_close_points(snapshot, window=3)

        self.assertEqual(len(points), 3)
        self.assertEqual(points[-1].trade_date, date(2026, 9, 23))
        self.assertEqual([point.value for point in points[:2]], [None, None])
        self.assertAlmostEqual(points[-1].value, (10.20 + 10.35 + 10.10) / 3)

    def test_macd_at_past_date_uses_only_the_selected_history(self) -> None:
        snapshot = bars_as_of_close(self.bars, date(2026, 9, 24))
        points = macd_close_points(snapshot, fast=2, slow=3, signal=2)

        self.assertEqual(len(points), 4)
        self.assertEqual(points[-1].trade_date, date(2026, 9, 24))
        self.assertAlmostEqual(points[-1].dif, -1 / 18)
        self.assertAlmostEqual(points[-1].dea, -41 / 720)
        self.assertAlmostEqual(points[-1].histogram, 1 / 360)
        self.assertEqual(points, macd_close_points(self.bars, 2, 3, 2)[:4])


if __name__ == '__main__':
    unittest.main()
