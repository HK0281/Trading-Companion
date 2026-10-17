import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext

from trading_companion.daily_bar import DailyBar
from trading_companion.indicators import MacdPoint, MacdValue, macd_close, macd_close_points


def make_bars(closes: list[float]) -> list[DailyBar]:
    """Build synthetic flat-OHLC records for calculation tests only."""
    return [
        DailyBar(
            instrument_id="000001.SZ",
            trade_date=date(2026, 9, 21) + timedelta(days=index),
            open_price=close,
            high_price=close,
            low_price=close,
            close_price=close,
            volume_shares=0,
            amount_cny=0.0,
        )
        for index, close in enumerate(closes)
    ]


def decimal_macd_reference(
    closes: list[float], fast: int, slow: int, signal: int,
) -> list[tuple[float | None, float | None, float | None]]:
    """Use Decimal and direct seed sums, independent of production helpers."""
    with localcontext() as context:
        context.prec = 50

        def reference_ema(values: list[Decimal], window: int) -> list[Decimal | None]:
            result = [None] * min(window - 1, len(values))
            if len(values) < window:
                return result
            alpha = Decimal(2) / Decimal(window + 1)
            current = sum(values[:window], Decimal(0)) / Decimal(window)
            result.append(current)
            for value in values[window:]:
                current = alpha * value + (1 - alpha) * current
                result.append(current)
            return result

        prices = [Decimal(str(close)) for close in closes]
        fast_values = reference_ema(prices, fast)
        slow_values = reference_ema(prices, slow)
        dif_values = [
            None if fast_value is None or slow_value is None else fast_value - slow_value
            for fast_value, slow_value in zip(fast_values, slow_values)
        ]
        valid_dif = [value for value in dif_values if value is not None]
        dea_values = [None] * (len(prices) - len(valid_dif)) + reference_ema(valid_dif, signal)
        return [
            (
                None if dif is None else float(dif),
                None if dea is None else float(dea),
                None if dif is None or dea is None else float(Decimal(2) * (dif - dea)),
            )
            for dif, dea in zip(dif_values, dea_values)
        ]


class MacdCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closes = [10.0, 12.0, 14.0, 20.0, 18.0, 16.0]
        self.bars = make_bars(self.closes)

    def assert_fields_close(self, actual: MacdValue, expected: tuple) -> None:
        for name, value in zip(("dif", "dea", "histogram"), expected):
            if value is None:
                self.assertIsNone(getattr(actual, name))
            else:
                self.assertAlmostEqual(getattr(actual, name), value, delta=1e-10)

    def test_empty_input_returns_no_values_or_points(self) -> None:
        self.assertEqual(macd_close([]), [])
        self.assertEqual(macd_close_points([]), [])

    def test_short_periods_match_hand_calculated_dif_dea_and_histogram(self) -> None:
        values = macd_close(self.bars, fast=2, slow=3, signal=2)
        expected = [
            (None, None, None), (None, None, None), (1.0, None, None),
            (5 / 3, 4 / 3, 2 / 3), (8 / 9, 28 / 27, -8 / 27),
            (7 / 54, 35 / 81, -49 / 81),
        ]
        self.assertEqual(len(values), len(self.bars))
        for actual, expected_fields in zip(values, expected):
            self.assertIsInstance(actual, MacdValue)
            self.assert_fields_close(actual, expected_fields)

    def test_default_readiness_boundaries_are_26_and_34_records(self) -> None:
        for count in (0, 25, 26, 30, 33, 34, 35):
            bars = make_bars([float(value) for value in range(1, count + 1)])
            with self.subTest(count=count):
                values = macd_close(bars)
                self.assertEqual(len(values), count)
                self.assertEqual(sum(value.dif is not None for value in values), max(0, count - 25))
                self.assertEqual(sum(value.dea is not None for value in values), max(0, count - 33))
                self.assertEqual(sum(value.histogram is not None for value in values), max(0, count - 33))
                if count >= 26:
                    self.assertAlmostEqual(values[25].dif, 7.0)
                    self.assertIsNone(values[25].dea)
                if count >= 34:
                    self.assert_fields_close(values[33], (7.0, 7.0, 0.0))

    def test_thirty_records_preserve_dif_without_inventing_dea_or_histogram(self) -> None:
        bars = make_bars([float(value) for value in range(1, 31)])
        values = macd_close(bars)
        self.assertAlmostEqual(values[-1].dif, 7.0)
        self.assertIsNone(values[-1].dea)
        self.assertIsNone(values[-1].histogram)

    def test_values_match_an_independent_decimal_reference(self) -> None:
        closes = [10.2, 10.35, 10.1, 10.0, 10.4, 9.8] * 8
        bars = make_bars(closes)
        for periods in ((1, 2, 1), (2, 3, 2), (3, 7, 4), (12, 26, 9), (2, 5, 10)):
            with self.subTest(periods=periods):
                actual = macd_close(bars, *periods)
                expected = decimal_macd_reference(closes, *periods)
                self.assertEqual(len(actual), len(expected))
                for actual_value, expected_fields in zip(actual, expected):
                    self.assert_fields_close(actual_value, expected_fields)

    def test_falling_prices_allow_negative_dif_and_dea(self) -> None:
        falling_bars = make_bars([30 - close for close in self.closes])
        rising = macd_close(self.bars, 2, 3, 2)
        falling = macd_close(falling_bars, 2, 3, 2)
        self.assertLess(falling[-1].dif, 0)
        self.assertLess(falling[-1].dea, 0)
        for rising_value, falling_value in zip(rising, falling):
            expected = tuple(
                None if value is None else -value
                for value in (rising_value.dif, rising_value.dea, rising_value.histogram)
            )
            self.assert_fields_close(falling_value, expected)

    def test_constant_prices_preserve_zero_as_valid_data(self) -> None:
        values = macd_close(make_bars([12.0] * 40))
        self.assertEqual(values[25], MacdValue(0.0, None, None))
        self.assertEqual(values[33:], [MacdValue(0.0, 0.0, 0.0)] * 7)

    def test_signal_period_one_makes_dea_equal_dif(self) -> None:
        for value in macd_close(self.bars, 2, 3, 1):
            if value.dif is None:
                self.assertEqual(value, MacdValue(None, None, None))
            else:
                self.assertEqual(value.dea, value.dif)
                self.assertEqual(value.histogram, 0.0)

    def test_unfilled_signal_seed_is_not_replaced_by_a_partial_average(self) -> None:
        values = macd_close(self.bars[:3], 2, 3, 2)
        self.assertEqual(values[-1], MacdValue(1.0, None, None))

    def test_rejects_invalid_periods_before_reading_bars(self) -> None:
        for field in ("fast", "slow", "signal"):
            for invalid in (0, -1, True, False, 1.5, "3", None, float("nan"), float("inf")):
                parameters = {"fast": 2, "slow": 3, "signal": 2, field: invalid}
                for function in (macd_close, macd_close_points):
                    with self.subTest(field=field, invalid=invalid, function=function.__name__):
                        with self.assertRaisesRegex(ValueError, "positive integer"):
                            function([], **parameters)

    def test_fast_period_must_be_shorter_than_slow_period(self) -> None:
        for fast, slow in ((3, 3), (4, 3), (26, 12)):
            with self.subTest(fast=fast, slow=slow):
                with self.assertRaisesRegex(ValueError, "fast must be less than slow"):
                    macd_close([], fast, slow, 2)

    def test_very_large_periods_do_not_allocate_by_period_length(self) -> None:
        self.assertEqual(
            macd_close(self.bars, 2, 10**400, 10**400),
            [MacdValue(None, None, None)] * len(self.bars),
        )

    def test_rejects_mixed_instruments_even_before_initialization(self) -> None:
        mixed = [self.bars[0], replace(self.bars[1], instrument_id="600000.SH")]
        with self.assertRaisesRegex(ValueError, "one instrument"):
            macd_close(mixed)

    def test_rejects_duplicate_or_reversed_dates(self) -> None:
        duplicate = replace(self.bars[1], trade_date=self.bars[0].trade_date)
        for bars in ([self.bars[1], self.bars[0]], [self.bars[0], duplicate]):
            with self.subTest(dates=[bar.trade_date for bar in bars]):
                with self.assertRaisesRegex(ValueError, "strictly increasing"):
                    macd_close(bars)

    def test_rejects_invalid_bar_records_and_non_sequences(self) -> None:
        for bars in (None, 12.0, "", b"", bytearray(), {}, iter(self.bars), [None], [12.0], [{"close_price": 12.0}]):
            with self.subTest(input_type=type(bars).__name__):
                with self.assertRaisesRegex(ValueError, "DailyBar records"):
                    macd_close(bars)

    def test_points_keep_original_dates_including_warmup_positions(self) -> None:
        points = macd_close_points(self.bars, 2, 3, 2)
        self.assertEqual(len(points), len(self.bars))
        for bar, point, value in zip(self.bars, points, macd_close(self.bars, 2, 3, 2)):
            self.assertIsInstance(point, MacdPoint)
            self.assertEqual((point.instrument_id, point.trade_date), (bar.instrument_id, bar.trade_date))
            self.assertEqual((point.fast, point.slow, point.signal), (2, 3, 2))
            self.assertEqual((point.dif, point.dea, point.histogram), (value.dif, value.dea, value.histogram))

    def test_default_periods_are_recorded_in_points(self) -> None:
        point = macd_close_points(self.bars)[-1]
        self.assertEqual((point.fast, point.slow, point.signal), (12, 26, 9))

    def test_date_gaps_count_records_without_filling_missing_dates(self) -> None:
        bars = [
            replace(bar, trade_date=date(2026, 9, 1) + timedelta(days=offset))
            for bar, offset in zip(self.bars, (0, 3, 9, 19, 23, 30))
        ]
        self.assertEqual(macd_close(bars, 2, 3, 2), macd_close(self.bars, 2, 3, 2))
        self.assertEqual([point.trade_date for point in macd_close_points(bars, 2, 3, 2)],
                         [bar.trade_date for bar in bars])

    def test_changing_future_prices_does_not_change_earlier_fields(self) -> None:
        original = macd_close(self.bars, 2, 3, 2)
        changed = macd_close(make_bars([*self.closes[:-1], 160.0]), 2, 3, 2)
        self.assertEqual(original[:-1], changed[:-1])
        self.assertNotEqual(original[-1], changed[-1])

    def test_every_prefix_matches_the_full_result_prefix(self) -> None:
        bars = make_bars([10.2, 10.35, 10.1, 10.0, 10.4, 9.8] * 7)
        for periods in ((2, 3, 2), (12, 26, 9)):
            full = macd_close(bars, *periods)
            for length in range(len(bars) + 1):
                with self.subTest(periods=periods, length=length):
                    self.assertEqual(macd_close(bars[:length], *periods), full[:length])

    def test_repeated_calls_and_tuple_inputs_do_not_modify_bars(self) -> None:
        original_bars = self.bars.copy()
        first = macd_close(self.bars, 2, 3, 2)
        self.assertEqual(macd_close(self.bars, 2, 3, 2), first)
        self.assertEqual(macd_close(tuple(self.bars), 2, 3, 2), first)
        self.assertEqual(self.bars, original_bars)

    def test_histogram_overflow_is_rejected_instead_of_returning_infinity(self) -> None:
        bars = make_bars([1.7e308] * 10 + [1.0] * 10 + [1.7e308])
        with self.assertRaisesRegex(ValueError, "histogram calculation must remain finite"):
            macd_close(bars, 1, 10, 10)


if __name__ == "__main__":
    unittest.main()
