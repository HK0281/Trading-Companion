import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_companion.csv_loader import load_daily_bars
from trading_companion.analysis import analyze_daily_bars
from trading_companion.data_source import CsvDailyBarSource


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


if __name__ == "__main__":
    unittest.main()
