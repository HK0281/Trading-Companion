import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch

from requests.exceptions import HTTPError, JSONDecodeError, Timeout

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.tencent_adapter import (
    TencentDailyBarSource,
    tencent_row_to_daily_bar,
    to_tencent_symbol,
)


class TencentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        clock = patch("trading_companion.tencent_adapter._shanghai_today", return_value=date(2026, 10, 4))
        clock.start()
        self.addCleanup(clock.stop)
        self.row = ["2025-09-05", "10.00", "10.40", "10.50", "9.98", "1300.25", {}, "0.5", "133"]

    def source(self, **kwargs) -> TencentDailyBarSource:
        return TencentDailyBarSource(
            "000001.SZ", date(2025, 9, 1), date(2025, 9, 5), **kwargs
        )

    def response(self, rows=None, *, symbol="sz000001") -> Mock:
        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {symbol: {"day": [self.row] if rows is None else rows}},
        }
        return response

    def test_symbol_mapping_preserves_exchange_and_leading_zeroes(self) -> None:
        for instrument, expected in (
            ("000001.SZ", "sz000001"), ("001001.SZ", "sz001001"),
            ("002001.SZ", "sz002001"), ("003001.SZ", "sz003001"),
            ("300001.SZ", "sz300001"), ("301001.SZ", "sz301001"),
            ("600000.SH", "sh600000"), ("601001.SH", "sh601001"),
            ("603001.SH", "sh603001"), ("605001.SH", "sh605001"),
            ("688001.SH", "sh688001"),
        ):
            with self.subTest(instrument=instrument):
                self.assertEqual(to_tencent_symbol(instrument), expected)

    def test_unsupported_or_malformed_symbols_do_not_guess_volume_units(self) -> None:
        for instrument in ("000001.SH", "688001.SZ", "920001.BJ", "510300.SH", "900901.SH", "200001.SZ", "000001", "sz000001", "", None):
            with self.subTest(instrument=instrument):
                with self.assertRaises(InvalidRequestError):
                    to_tencent_symbol(instrument)

    def test_main_board_and_chinext_volume_is_converted_from_lots(self) -> None:
        for instrument in ("000001.SZ", "600000.SH", "300001.SZ", "301001.SZ"):
            with self.subTest(instrument=instrument):
                bar = tencent_row_to_daily_bar(self.row, instrument)
                self.assertEqual(bar.instrument_id, instrument)
                self.assertEqual(bar.trade_date, date(2025, 9, 5))
                self.assertEqual(bar.open_price, 10.0)
                self.assertEqual(bar.close_price, 10.4)
                self.assertEqual(bar.high_price, 10.5)
                self.assertEqual(bar.low_price, 9.98)
                self.assertEqual(bar.volume_shares, 130025)
                self.assertEqual(bar.amount_cny, 1330000.0)

    def test_star_market_volume_already_represents_shares(self) -> None:
        row = [*self.row]
        row[5] = "130025"
        bar = tencent_row_to_daily_bar(row, "688001.SH")
        self.assertEqual(bar.volume_shares, 130025)
        self.assertEqual(bar.amount_cny, 1330000.0)

    def test_rejects_fractional_negative_nonfinite_or_boolean_volume(self) -> None:
        for instrument, volume in (
            ("000001.SZ", "0.001"), ("688001.SH", "0.25"),
            ("000001.SZ", "-1"), ("000001.SZ", "nan"),
            ("000001.SZ", "inf"), ("000001.SZ", True),
        ):
            with self.subTest(instrument=instrument, volume=volume):
                row = [*self.row]
                row[5] = volume
                with self.assertRaises(DataValidationError):
                    tencent_row_to_daily_bar(row, instrument)

    def test_rejects_malformed_rows_and_invalid_ohlc_or_amount(self) -> None:
        malformed_rows = [None, {}, "not a row", self.row[:8]]
        for index, value in ((0, "not-a-date"), (1, "nan"), (2, "11.0"), (4, "0"), (8, "-1"), (8, "inf"), (8, True)):
            row = [*self.row]
            row[index] = value
            malformed_rows.append(row)
        for row in malformed_rows:
            with self.subTest(row=row):
                with self.assertRaises(DataValidationError):
                    tencent_row_to_daily_bar(row, "000001.SZ")

    def test_source_exposes_metadata_and_requests_unadjusted_data(self) -> None:
        source = self.source(timeout_seconds=2.0)
        response = self.response()
        with patch("requests.get", return_value=response) as fetch:
            bars = source.read_daily_bars()
        self.assertEqual(source.source_name, "tencent")
        self.assertEqual(source.adjustment, "unadjusted")
        self.assertEqual(len(bars), 1)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["params"], {"param": "sz000001,day,2025-09-01,2025-09-05,640,"})
        self.assertEqual(fetch.call_args.kwargs["timeout"], 2.0)
        response.raise_for_status.assert_called_once()
        response.close.assert_called_once()

    def test_configuration_is_validated_before_network_access(self) -> None:
        with patch("requests.get") as fetch:
            for timeout in (0, -1, True, "15", float("nan"), float("inf")):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(InvalidRequestError):
                        self.source(timeout_seconds=timeout)
            for start, end in (
                (date(2025, 9, 6), date(2025, 9, 5)),
                ("2025-09-01", date(2025, 9, 5)),
                (date(2025, 9, 1), datetime(2025, 9, 5)),
            ):
                with self.subTest(start=start, end=end):
                    with self.assertRaises(InvalidRequestError):
                        TencentDailyBarSource("000001.SZ", start, end)
        fetch.assert_not_called()

    def test_current_and_future_shanghai_dates_are_rejected_before_request(self) -> None:
        with patch("requests.get") as fetch:
            for end in (date(2026, 10, 4), date(2026, 10, 5)):
                with self.subTest(end=end):
                    source = TencentDailyBarSource("000001.SZ", date(2026, 10, 1), end)
                    with self.assertRaises(InvalidRequestError):
                        source.read_daily_bars()
        fetch.assert_not_called()

    def test_requests_timeout_and_http_failure_are_source_errors(self) -> None:
        with patch("requests.get", side_effect=Timeout("Read timed out")):
            with self.assertRaises(DataSourceError):
                self.source().read_daily_bars()
        response = self.response()
        response.raise_for_status.side_effect = HTTPError("503 Service Unavailable")
        with patch("requests.get", return_value=response):
            with self.assertRaises(DataSourceError):
                self.source().read_daily_bars()
        response.json.assert_not_called()
        response.close.assert_called_once()

    def test_nonzero_provider_code_is_not_treated_as_empty_data(self) -> None:
        response = self.response()
        response.json.return_value = {"code": 1, "msg": "unavailable", "data": {}}
        with patch("requests.get", return_value=response):
            with self.assertRaises(DataSourceError):
                self.source().read_daily_bars()

    def test_invalid_json_is_a_validation_error(self) -> None:
        response = self.response()
        response.json.side_effect = JSONDecodeError("Invalid JSON", "<html>", 0)
        with patch("requests.get", return_value=response):
            with self.assertRaises(DataValidationError):
                self.source().read_daily_bars()
        response.close.assert_called_once()

    def test_missing_or_adjusted_only_payload_is_invalid_not_empty(self) -> None:
        payloads = [
            None, [], {}, {"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": {}},
            {"code": False, "data": {}}, {"code": "0", "data": {}},
            {"code": 0, "data": {"sz000001": None}},
            {"code": 0, "data": {"sz000001": {}}},
            {"code": 0, "data": {"sz000001": {"qfqday": [self.row]}}},
            {"code": 0, "data": {"sz000001": {"day": None}}},
            {"code": 0, "data": {"sz000001": {"day": {}}}},
            {"code": 0, "data": {"sh600000": {"day": [self.row]}}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.response()
                response.json.return_value = payload
                with patch("requests.get", return_value=response):
                    with self.assertRaises(DataValidationError):
                        self.source().read_daily_bars()

    def test_explicit_empty_daily_series_returns_empty_list(self) -> None:
        with patch("requests.get", return_value=self.response([])):
            self.assertEqual(self.source().read_daily_bars(), [])

    def test_missing_symbol_in_later_chunk_is_not_partial_success(self) -> None:
        source = TencentDailyBarSource("000001.SZ", date(2024, 12, 30), date(2025, 1, 3))
        earlier = self.response([["2024-12-31", *self.row[1:]]])
        malformed = self.response()
        malformed.json.return_value = {"code": 0, "data": {}}
        with patch("requests.get", side_effect=[earlier, malformed]):
            with self.assertRaises(DataValidationError):
                source.read_daily_bars()

    def test_cross_year_requests_filter_each_chunk_and_sort_without_overlap(self) -> None:
        source = TencentDailyBarSource("000001.SZ", date(2024, 12, 30), date(2025, 1, 3))
        def row_on(day):
            return [day, *self.row[1:]]
        responses = [
            self.response([row_on("2024-12-31"), row_on("2024-12-30"), row_on("2024-12-27")]),
            self.response([row_on("2025-01-03"), row_on("2025-01-02"), row_on("2024-12-31")]),
        ]
        with patch("requests.get", side_effect=responses) as fetch:
            bars = source.read_daily_bars()
        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3)])
        self.assertEqual([call.kwargs["params"]["param"] for call in fetch.call_args_list], [
            "sz000001,day,2024-12-30,2024-12-31,640,",
            "sz000001,day,2025-01-01,2025-01-03,640,",
        ])

    def test_full_640_record_response_keeps_only_requested_slice(self) -> None:
        end = date(2025, 9, 5)
        rows = [[(end - timedelta(days=offset)).isoformat(), *self.row[1:]] for offset in range(640)]
        with patch("requests.get", return_value=self.response(rows)):
            bars = self.source().read_daily_bars()
        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0].trade_date, date(2025, 9, 1))
        self.assertEqual(bars[-1].trade_date, end)

    def test_duplicate_in_range_dates_are_rejected(self) -> None:
        with patch("requests.get", return_value=self.response([self.row, self.row])):
            with self.assertRaises(DataValidationError):
                self.source().read_daily_bars()

    def test_bars_after_requested_end_date_are_rejected(self) -> None:
        response = self.response([["2025-09-08", *self.row[1:]]])
        with patch("requests.get", return_value=response):
            with self.assertRaises(DataValidationError):
                self.source().read_daily_bars()

    def test_extra_older_bars_are_filtered_before_numeric_conversion(self) -> None:
        older_row = ["2025-08-29", "", "", "", "", "", {}, "", ""]
        with patch("requests.get", return_value=self.response([older_row, self.row])):
            bars = self.source().read_daily_bars()
        self.assertEqual([bar.trade_date for bar in bars], [date(2025, 9, 5)])

    def test_invalid_dates_or_structure_in_extra_older_rows_are_not_ignored(self) -> None:
        for row in (["2025-08-29"], ["not-a-date", *self.row[1:]]):
            with self.subTest(row=row):
                with patch("requests.get", return_value=self.response([row, self.row])):
                    with self.assertRaises(DataValidationError):
                        self.source().read_daily_bars()

    def test_later_chunk_failure_does_not_return_partial_history(self) -> None:
        source = TencentDailyBarSource("000001.SZ", date(2024, 12, 30), date(2025, 1, 3))
        response = self.response([["2024-12-31", *self.row[1:]]])
        with patch("requests.get", side_effect=[response, Timeout("Read timed out")]) as fetch:
            with self.assertRaises(DataSourceError):
                source.read_daily_bars()
        self.assertEqual(fetch.call_count, 2)

    def test_missing_requests_dependency_has_actionable_error(self) -> None:
        source = self.source()
        with patch.dict("sys.modules", {"requests": None}):
            with self.assertRaisesRegex(DataSourceError, "[Ii]nstall"):
                source.read_daily_bars()


if __name__ == "__main__":
    unittest.main()
