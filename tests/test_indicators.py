import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from math import isfinite
from pathlib import Path

from trading_companion.csv_loader import load_daily_bars
from trading_companion.daily_bar import DailyBar
from trading_companion.indicators import (
    EmaPoint,
    _ema_values,
    ema_close,
    ema_close_points,
    sma_close,
    sma_close_points,
)


class SmaCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        sample_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "sample_daily_bars_synthetic.csv"
        )
        self.bars = load_daily_bars(sample_path)

    def test_three_day_average_uses_current_and_previous_closes(self) -> None:
        averages = sma_close(self.bars, window=3)

        self.assertEqual(averages[:2], [None, None])
        self.assertAlmostEqual(averages[2], (10.20 + 10.35 + 10.10) / 3)
        self.assertAlmostEqual(averages[3], (10.35 + 10.10 + 10.00) / 3)
        self.assertAlmostEqual(averages[4], (10.10 + 10.00 + 10.40) / 3)

    def test_future_close_does_not_change_earlier_averages(self) -> None:
        changed_bars = [*self.bars[:-1], replace(self.bars[-1], close_price=10.50)]

        self.assertEqual(sma_close(self.bars, 3)[:4], sma_close(changed_bars, 3)[:4])

    def test_points_keep_instrument_date_and_window(self) -> None:
        points = sma_close_points(self.bars, window=3)

        self.assertEqual(len(points), len(self.bars))
        self.assertEqual(points[0].instrument_id, "000001.SZ")
        self.assertEqual(points[0].trade_date, self.bars[0].trade_date)
        self.assertEqual(points[0].window, 3)
        self.assertIsNone(points[0].value)
        self.assertEqual(points[-1].trade_date, self.bars[-1].trade_date)
        self.assertAlmostEqual(points[-1].value, (10.10 + 10.00 + 10.40) / 3)

    def test_rejects_invalid_window(self) -> None:
        for window in (0, -1, True):
            with self.subTest(window=window):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    sma_close(self.bars, window)

    def test_rejects_mixed_or_out_of_order_bars(self) -> None:
        other_instrument = replace(self.bars[1], instrument_id="600000.SH")
        with self.assertRaisesRegex(ValueError, "one instrument"):
            sma_close([self.bars[0], other_instrument], 2)

        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            sma_close([self.bars[1], self.bars[0]], 2)


class EmaCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closes = [10.0, 12.0, 14.0, 20.0, 18.0, 16.0]
        self.bars = self.make_bars(self.closes)

    @staticmethod
    def make_bars(closes: list[float]) -> list[DailyBar]:
        # These flat OHLC bars are synthetic calculation fixtures, not quotes.
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

    def test_empty_input_has_no_values_or_points(self) -> None:
        self.assertEqual(ema_close([], 3), [])
        self.assertEqual(ema_close_points([], 3), [])

    def test_insufficient_history_is_aligned_and_unavailable(self) -> None:
        for window in (3, 20, 10**400):
            with self.subTest(window=window):
                self.assertEqual(ema_close(self.bars[:2], window), [None, None])
                self.assertEqual(
                    [point.value for point in ema_close_points(self.bars[:2], window)],
                    [None, None],
                )

    def test_exact_window_uses_the_first_closes_mean_as_seed(self) -> None:
        self.assertEqual(ema_close(self.bars[:3], 3), [None, None, 12.0])

    def test_recursive_updates_match_the_hand_calculation(self) -> None:
        self.assertEqual(ema_close(self.bars, 3), [None, None, 12.0, 16.0, 17.0, 16.5])

    def test_twenty_record_seed_and_following_update(self) -> None:
        bars = self.make_bars([float(close) for close in range(1, 22)])
        averages = ema_close(bars, 20)
        self.assertEqual(averages[:19], [None] * 19)
        self.assertAlmostEqual(averages[19], 10.5)
        self.assertAlmostEqual(averages[20], 11.5)

    def test_window_one_is_exactly_the_current_close(self) -> None:
        closes = [1e308, 1e-308, 12.0, 1.0]
        self.assertEqual(ema_close(self.make_bars(closes), 1), closes)

    def test_constant_prices_stay_constant_after_initialization(self) -> None:
        bars = self.make_bars([12.0] * 8)
        self.assertEqual(ema_close(bars, 3), [None, None, *([12.0] * 6)])

    def test_large_finite_prices_do_not_overflow_the_seed_sum(self) -> None:
        values = ema_close(self.make_bars([1e308] * 5), 3)
        self.assertEqual(values[:2], [None, None])
        self.assertTrue(all(isfinite(value) for value in values[2:]))
        self.assertEqual(values[2:], [1e308] * 3)

    def test_values_match_an_independent_decimal_reference(self) -> None:
        bars = self.make_bars([10.2, 10.35, 10.1, 10.0, 10.4, 9.8])
        with localcontext() as context:
            context.prec = 40
            closes = [Decimal(str(bar.close_price)) for bar in bars]
            for window in (1, 2, 3, 6, 8):
                expected = [None] * min(window - 1, len(bars))
                if len(bars) >= window:
                    alpha = Decimal(2) / Decimal(window + 1)
                    value = sum(closes[:window]) / Decimal(window)
                    expected.append(value)
                    for close in closes[window:]:
                        value = alpha * close + (1 - alpha) * value
                        expected.append(value)
                with self.subTest(window=window):
                    actual = ema_close(bars, window)
                    self.assertEqual(len(actual), len(expected))
                    for actual_value, expected_value in zip(actual, expected):
                        if expected_value is None:
                            self.assertIsNone(actual_value)
                        else:
                            self.assertAlmostEqual(actual_value, float(expected_value), delta=1e-10)

    def test_changing_a_future_close_does_not_change_earlier_values(self) -> None:
        changed_bars = self.make_bars([*self.closes[:-1], 160.0])
        original = ema_close(self.bars, 3)
        changed = ema_close(changed_bars, 3)
        self.assertEqual(original[:-1], changed[:-1])
        self.assertNotEqual(original[-1], changed[-1])

    def test_every_prefix_matches_the_same_prefix_of_the_full_result(self) -> None:
        for window in (1, 2, 3, 20):
            full_result = ema_close(self.bars, window)
            for length in range(len(self.bars) + 1):
                with self.subTest(window=window, length=length):
                    self.assertEqual(ema_close(self.bars[:length], window), full_result[:length])

    def test_rejects_invalid_window_even_for_empty_input(self) -> None:
        for window in (0, -1, True, False, 1.5, "3", None, float("nan"), float("inf")):
            for function in (ema_close, ema_close_points):
                with self.subTest(function=function.__name__, window=window):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        function([], window)

    def test_rejects_mixed_instruments_even_with_insufficient_history(self) -> None:
        other_instrument = replace(self.bars[1], instrument_id="600000.SH")
        for function in (ema_close, ema_close_points):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(ValueError, "one instrument"):
                    function([self.bars[0], other_instrument], 20)

    def test_rejects_reversed_or_duplicate_dates(self) -> None:
        duplicate_date = replace(self.bars[1], trade_date=self.bars[0].trade_date)
        for bars in ([self.bars[1], self.bars[0]], [self.bars[0], duplicate_date]):
            with self.subTest(dates=[bar.trade_date for bar in bars]):
                with self.assertRaisesRegex(ValueError, "strictly increasing"):
                    ema_close(bars, 20)

    def test_rejects_non_daily_bar_records(self) -> None:
        for invalid_bar in (None, {}, "not a bar", 12.0):
            with self.subTest(invalid_bar=invalid_bar):
                with self.assertRaisesRegex(ValueError, "DailyBar records"):
                    ema_close([self.bars[0], invalid_bar], 3)

    def test_rejects_non_bar_sequences_and_one_shot_iterators(self) -> None:
        for bars in (None, "", b"", bytearray(), {}, iter(self.bars)):
            with self.subTest(input_type=type(bars).__name__):
                with self.assertRaisesRegex(ValueError, "DailyBar records"):
                    ema_close(bars, 3)

    def test_tuple_input_and_repeated_calls_do_not_modify_bars(self) -> None:
        original_bars = self.bars.copy()
        first = ema_close(self.bars, 3)
        self.assertEqual(ema_close(tuple(self.bars), 3), first)
        self.assertEqual(ema_close(self.bars, 3), first)
        self.assertEqual(self.bars, original_bars)

    def test_points_keep_instrument_date_window_and_values(self) -> None:
        points = ema_close_points(self.bars, 3)
        self.assertEqual(len(points), len(self.bars))
        self.assertEqual([point.value for point in points], ema_close(self.bars, 3))
        for bar, point in zip(self.bars, points):
            self.assertIsInstance(point, EmaPoint)
            self.assertEqual(point.instrument_id, bar.instrument_id)
            self.assertEqual(point.trade_date, bar.trade_date)
            self.assertEqual(point.window, 3)

    def test_date_gaps_count_records_without_inventing_bars(self) -> None:
        bars = [
            replace(bar, trade_date=date(2026, 9, 1) + timedelta(days=offset))
            for bar, offset in zip(self.bars[:4], (0, 3, 9, 19))
        ]
        self.assertEqual(ema_close(bars, 3), [None, None, 12.0, 16.0])
        self.assertEqual(
            [point.trade_date for point in ema_close_points(bars, 3)],
            [bar.trade_date for bar in bars],
        )

    def test_different_calculation_starts_can_change_the_latest_ema(self) -> None:
        full_result = ema_close(self.bars, 3)
        restarted_result = ema_close(self.bars[1:], 3)
        self.assertAlmostEqual(full_result[-1], 16.5)
        self.assertAlmostEqual(restarted_result[-1], 49 / 3)
        self.assertNotAlmostEqual(full_result[-1], restarted_result[-1])


class EmaValueTests(unittest.TestCase):
    def test_signed_numbers_and_zero_are_valid_ema_inputs(self) -> None:
        self.assertEqual(_ema_values([-3.0, 0.0, 3.0, -6.0], 3), [None, None, 0.0, -3.0])

    def test_zero_seed_does_not_restart_initialization(self) -> None:
        self.assertEqual(_ema_values([0.0] * 5, 3), [None, None, 0.0, 0.0, 0.0])

    def test_period_one_preserves_extreme_signed_values_exactly(self) -> None:
        values = [1e308, -1e308, 0.0, 1e-308]
        self.assertEqual(_ema_values(values, 1), values)

    def test_opposite_signed_extremes_have_a_finite_seed(self) -> None:
        values = _ema_values([-1.7e308, 1.7e308, 1.7e308], 2)
        self.assertIsNone(values[0])
        self.assertEqual(values[1], 0.0)
        self.assertTrue(isfinite(values[2]))
        self.assertAlmostEqual(values[2] / 1.7e308, 2 / 3)

    def test_opposite_signed_extremes_have_a_finite_recursive_update(self) -> None:
        values = _ema_values([1.7e308, 1.7e308, -1.7e308], 2)
        self.assertTrue(isfinite(values[-1]))
        self.assertAlmostEqual(values[-1] / 1.7e308, -1 / 3)

    def test_rejects_invalid_numeric_values_even_during_warmup(self) -> None:
        for value in (None, True, False, "1.0", {}, float("nan"), float("inf"), -float("inf"), 10**400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite numbers"):
                _ema_values([0.0, value], 20)

    def test_input_numbers_are_not_modified(self) -> None:
        values = [-3.0, 0.0, 3.0, -6.0]
        original = values.copy()
        result = _ema_values(values, 3)
        self.assertEqual(values, original)
        self.assertEqual(_ema_values(tuple(values), 3), result)


if __name__ == "__main__":
    unittest.main()
