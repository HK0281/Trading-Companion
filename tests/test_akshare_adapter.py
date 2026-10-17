import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import JSONDecodeError, Timeout

from trading_companion.akshare_adapter import (
    AkshareDailyBarSource,
    akshare_row_to_daily_bar,
    to_akshare_symbol,
)
from trading_companion.errors import DataSourceError, DataValidationError


class AkshareAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        clock = patch("trading_companion.akshare_adapter._shanghai_today", return_value=date(2026, 10, 3))
        clock.start()
        self.addCleanup(clock.stop)
        self.row = {
            "日期": "2026-09-25",
            "开盘": 10.00,
            "最高": 10.50,
            "最低": 9.98,
            "收盘": 10.40,
            "成交量": 1300,
            "成交额": 1330000.00,
        }

    def test_converts_prices_volume_and_amount(self) -> None:
        bar = akshare_row_to_daily_bar(self.row, "000001.SZ")

        self.assertEqual(bar.trade_date, date(2026, 9, 25))
        self.assertEqual(bar.close_price, 10.40)
        self.assertEqual(bar.volume_shares, 130000)
        self.assertEqual(bar.amount_cny, 1330000.00)

    def test_preserves_exchange_and_leading_zeroes(self) -> None:
        self.assertEqual(to_akshare_symbol("000001.SZ"), "000001")
        self.assertEqual(to_akshare_symbol("688001.SH"), "688001")
        with self.assertRaisesRegex(ValueError, "does not match"):
            to_akshare_symbol("000001.SH")

    def test_rejects_fractional_share_count(self) -> None:
        invalid_row = {**self.row, "成交量": "0.001"}
        with self.assertRaisesRegex(ValueError, "whole number of shares"):
            akshare_row_to_daily_bar(invalid_row, "000001.SZ")

    def test_source_requests_unadjusted_daily_bars_and_sorts_dates(self) -> None:
        earlier_row = {**self.row, "日期": "2026-09-24"}
        frame = Mock()
        frame.columns = list(self.row)
        frame.to_dict.return_value = [self.row, earlier_row]
        fetch = Mock(return_value=frame)
        fake_akshare = SimpleNamespace(stock_zh_a_hist=fetch)
        source = AkshareDailyBarSource(
            "000001.SZ", date(2026, 9, 24), date(2026, 9, 25)
        )

        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            bars = source.read_daily_bars()

        fetch.assert_called_once_with(
            symbol="000001",
            period="daily",
            start_date="20260924",
            end_date="20260925",
            adjust="",
            timeout=15.0,
        )
        self.assertEqual([bar.trade_date for bar in bars], [date(2026, 9, 24), date(2026, 9, 25)])

    def test_source_rejects_duplicate_trade_dates(self) -> None:
        frame = Mock()
        frame.columns = list(self.row)
        frame.to_dict.return_value = [self.row, self.row]
        fake_akshare = SimpleNamespace(stock_zh_a_hist=Mock(return_value=frame))
        source = AkshareDailyBarSource(
            "000001.SZ", date(2026, 9, 25), date(2026, 9, 25)
        )

        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            with self.assertRaisesRegex(ValueError, "duplicate bar"):
                source.read_daily_bars()

    def test_request_timeout_is_a_source_error(self) -> None:
        fetch = Mock(side_effect=Timeout("Read timed out"))
        source = AkshareDailyBarSource(
            "000001.SZ", date(2026, 9, 25), date(2026, 9, 25), timeout_seconds=2.0
        )
        with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
            with self.assertRaises(DataSourceError) as caught:
                source.read_daily_bars()
        self.assertIsInstance(caught.exception.__cause__, Timeout)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 2.0)

    def test_missing_dependency_has_installation_hint(self) -> None:
        source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), date(2026, 9, 25))
        with patch.dict("sys.modules", {"akshare": None}):
            with self.assertRaisesRegex(DataSourceError, "Install the data extra"):
                source.read_daily_bars()

    def test_invalid_json_and_schema_are_validation_errors(self) -> None:
        source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), date(2026, 9, 25))
        for error in (JSONDecodeError("Invalid JSON", "<html>", 0), KeyError("data")):
            with self.subTest(error=type(error).__name__):
                fetch = Mock(side_effect=error)
                with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
                    with self.assertRaises(DataValidationError):
                        source.read_daily_bars()

    def test_empty_table_means_no_available_bars(self) -> None:
        source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), date(2026, 9, 25))
        for columns in ([], list(self.row)):
            with self.subTest(columns=columns):
                frame = SimpleNamespace(columns=columns, to_dict=Mock(return_value=[]))
                fetch = Mock(return_value=frame)
                with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
                    self.assertEqual(source.read_daily_bars(), [])

    def test_malformed_tables_do_not_become_empty_results(self) -> None:
        source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), date(2026, 9, 25))
        malformed_frames = [
            None,
            SimpleNamespace(columns=["error"], to_dict=Mock(return_value=[])),
            SimpleNamespace(columns=list(self.row), to_dict=Mock(return_value=None)),
            SimpleNamespace(columns=list(self.row), to_dict=Mock(return_value=["bad row"])),
            SimpleNamespace(columns=[], to_dict=Mock(return_value=[self.row])),
        ]
        for frame in malformed_frames:
            with self.subTest(frame=frame):
                fetch = Mock(return_value=frame)
                with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
                    with self.assertRaises(DataValidationError):
                        source.read_daily_bars()

    def test_invalid_prices_and_out_of_range_dates_are_validation_errors(self) -> None:
        source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), date(2026, 9, 25))
        for row in ({**self.row, "收盘": float("nan")}, {**self.row, "日期": "2026-09-24"}):
            with self.subTest(row=row):
                frame = SimpleNamespace(columns=list(row), to_dict=Mock(return_value=[row]))
                fetch = Mock(return_value=frame)
                with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
                    with self.assertRaises(DataValidationError):
                        source.read_daily_bars()

    def test_timeout_must_be_finite_and_positive(self) -> None:
        for timeout in (0, -1, True, "15", float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                    AkshareDailyBarSource(
                        "000001.SZ", date(2026, 9, 25), date(2026, 9, 25), timeout_seconds=timeout
                    )

    def test_current_and_future_dates_are_rejected_before_request(self) -> None:
        for end_date in (date(2026, 10, 3), date(2026, 10, 4)):
            with self.subTest(end_date=end_date):
                fetch = Mock()
                source = AkshareDailyBarSource("000001.SZ", date(2026, 9, 25), end_date)
                with patch.dict("sys.modules", {"akshare": SimpleNamespace(stock_zh_a_hist=fetch)}):
                    with self.assertRaisesRegex(ValueError, "before today in Shanghai"):
                        source.read_daily_bars()
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
