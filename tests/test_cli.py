import unittest
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading_companion.__main__ import main
from trading_companion.csv_loader import load_daily_bars
from trading_companion.errors import DataSourceError


class CliTests(unittest.TestCase):
    sample_path = Path(__file__).resolve().parents[1] / "data" / "sample_daily_bars_synthetic.csv"

    def test_prints_only_bars_visible_as_of_selected_date(self) -> None:
        sample_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "sample_daily_bars_synthetic.csv"
        )
        output = StringIO()

        with redirect_stdout(output):
            result = main([str(sample_path), "--as-of", "2026-09-23", "--window", "3"])

        self.assertEqual(result, 0)
        self.assertIn("2026-09-23    10.10    10.22", output.getvalue())
        self.assertNotIn("2026-09-24", output.getvalue())

    def test_json_exposes_insufficient_history_and_null_sma(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main([str(self.sample_path), "--as-of", "2026-09-25", "--window", "20", "--format", "json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "insufficient_history")
        self.assertEqual(payload["missing_bars"], 15)
        self.assertIsNone(payload["rows"][-1]["sma_close"])

    def test_akshare_mode_selects_provider_and_returns_provenance(self) -> None:
        source = SimpleNamespace(
            source_name="akshare_eastmoney", adjustment="unadjusted", instrument_id="000001.SZ",
            read_daily_bars=Mock(return_value=load_daily_bars(self.sample_path)),
        )
        output = StringIO()
        with patch("trading_companion.__main__.AkshareDailyBarSource", return_value=source) as factory:
            with redirect_stdout(output):
                code = main([
                    "--source", "akshare", "--symbol", "000001.SZ", "--start", "2026-09-21",
                    "--as-of", "2026-09-25", "--timeout", "2", "--format", "json",
                ])
        factory.assert_called_once_with("000001.SZ", date(2026, 9, 21), date(2026, 9, 25), timeout_seconds=2.0)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["price_adjustment"], "unadjusted")

    def test_source_failure_is_json_with_nonzero_exit(self) -> None:
        output = StringIO()
        with patch("trading_companion.data_source.CsvDailyBarSource.read_daily_bars", side_effect=DataSourceError("Unavailable")):
            with redirect_stdout(output):
                code = main([str(self.sample_path), "--as-of", "2026-09-25", "--format", "json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "source_error")

    def test_tencent_mode_selects_provider_and_preserves_json_output(self) -> None:
        source = SimpleNamespace(
            source_name="tencent", adjustment="unadjusted", instrument_id="000001.SZ",
            read_daily_bars=Mock(return_value=load_daily_bars(self.sample_path)),
        )
        output = StringIO()
        with patch("trading_companion.__main__.TencentDailyBarSource", return_value=source) as factory:
            with patch("trading_companion.__main__.AkshareDailyBarSource") as eastmoney:
                with redirect_stdout(output):
                    code = main([
                        "--source", "tencent", "--symbol", "000001.SZ", "--start", "2026-09-21",
                        "--as-of", "2026-09-25", "--timeout", "2", "--format", "json",
                    ])
        factory.assert_called_once_with("000001.SZ", date(2026, 9, 21), date(2026, 9, 25), timeout_seconds=2.0)
        eastmoney.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["source"], "tencent")
        self.assertEqual(payload["price_adjustment"], "unadjusted")
        self.assertEqual(payload["bars_used"], 5)

    def test_tencent_failure_does_not_fall_back_to_eastmoney(self) -> None:
        output = StringIO()
        with patch("trading_companion.tencent_adapter.TencentDailyBarSource.read_daily_bars", side_effect=DataSourceError("Unavailable")):
            with patch("trading_companion.__main__.AkshareDailyBarSource") as eastmoney:
                with redirect_stdout(output):
                    code = main([
                        "--source", "tencent", "--symbol", "000001.SZ", "--start", "2025-09-01",
                        "--as-of", "2025-09-05", "--format", "json",
                    ])
        eastmoney.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "source_error")

    def test_tencent_invalid_options_fail_before_read(self) -> None:
        cases = (
            [],
            ["--symbol", "000001.SZ"],
            [str(self.sample_path), "--symbol", "000001.SZ", "--start", "2025-09-01"],
            ["--symbol", "000001.SZ", "--start", "2025-09-01", "--timeout", "0"],
            ["--symbol", "830001.BJ", "--start", "2025-09-01"],
        )
        for extra in cases:
            with self.subTest(extra=extra):
                with patch("trading_companion.tencent_adapter.TencentDailyBarSource.read_daily_bars") as read:
                    with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                        main(["--source", "tencent", "--as-of", "2025-09-05", *extra])
                self.assertEqual(caught.exception.code, 2)
                read.assert_not_called()

    def test_missing_akshare_options_do_not_construct_source(self) -> None:
        with patch("trading_companion.__main__.AkshareDailyBarSource") as factory:
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--source", "akshare", "--as-of", "2026-09-25"])
        self.assertEqual(caught.exception.code, 2)
        factory.assert_not_called()

    def test_missing_csv_is_a_source_failure_not_empty_data(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main([str(self.sample_path.with_name("does_not_exist.csv")), "--as-of", "2026-09-25", "--format", "json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "source_error")


if __name__ == "__main__":
    unittest.main()
