import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from math import isfinite

from trading_companion.daily_bar import DailyBar
from trading_companion.history import bars_as_of_close
from trading_companion.indicators import (
    AtrPoint,
    BollPoint,
    BollValue,
    RsiPoint,
    VolumePoint,
    VolumeValue,
    atr,
    atr_points,
    boll_close,
    boll_close_points,
    rsi_close,
    rsi_close_points,
    volume_activity,
    volume_activity_points,
)


def make_bars(closes, volumes=None):
    """Build synthetic flat OHLC records, never real market observations."""
    if volumes is None:
        volumes = [100] * len(closes)
    return [
        DailyBar(
            instrument_id="000001.SZ",
            trade_date=date(2026, 9, 1) + timedelta(days=index),
            open_price=close,
            high_price=close,
            low_price=close,
            close_price=close,
            volume_shares=volume,
            amount_cny=0.0,
        )
        for index, (close, volume) in enumerate(zip(closes, volumes))
    ]


CALCULATIONS = (rsi_close, boll_close, atr, volume_activity)
POINT_CALCULATIONS = (rsi_close_points, boll_close_points, atr_points, volume_activity_points)
ALL_CALCULATIONS = (*CALCULATIONS, *POINT_CALCULATIONS)


class RsiTests(unittest.TestCase):
    def test_three_changes_seed_and_first_wilder_updates_match_hand_calculation(self):
        values = rsi_close(make_bars([10, 12, 11, 14, 13, 15]), 3)
        self.assertEqual(values[:3], [None] * 3)
        for actual, expected in zip(values[3:], (250 / 3, 200 / 3, 475 / 6)):
            self.assertAlmostEqual(actual, expected)

    def test_default_fourteen_changes_need_fifteen_prices(self):
        bars = make_bars(list(range(1, 17)))
        self.assertEqual(rsi_close(bars[:13]), [None] * 13)
        self.assertEqual(rsi_close(bars[:14]), [None] * 14)
        self.assertEqual(rsi_close(bars[:15]), [None] * 14 + [100.0])
        self.assertEqual(rsi_close(bars)[14:], [100.0, 100.0])

    def test_monotone_and_flat_prices_have_defined_boundary_values(self):
        for closes, expected in (([1, 2, 3, 4, 5], 100.0),
                                 ([5, 4, 3, 2, 1], 0.0),
                                 ([2] * 5, 50.0)):
            with self.subTest(closes=closes):
                self.assertEqual(rsi_close(make_bars(closes), 3), [None] * 3 + [expected] * 2)

    def test_zero_gain_and_loss_seed_continues_smoothing_instead_of_reseeding(self):
        values = rsi_close(make_bars([10, 10, 10, 10, 11, 10]), 3)
        self.assertEqual(values[:5], [None, None, None, 50.0, 100.0])
        self.assertAlmostEqual(values[-1], 40.0)

    def test_window_one_uses_current_change_and_preserves_zero(self):
        self.assertEqual(rsi_close(make_bars([2, 3, 3, 1]), 1), [None, 100.0, 50.0, 0.0])

    def test_matches_high_precision_decimal_reference(self):
        closes = [10.2, 10.35, 10.1, 10.0, 10.4, 9.8, 9.8, 10.3, 10.1, 10.8] * 4
        bars = make_bars(closes)
        with localcontext() as context:
            context.prec = 60
            prices = [Decimal(str(close)) for close in closes]
            changes = [right - left for left, right in zip(prices, prices[1:])]
            for window in (1, 3, 14, 30):
                actual = rsi_close(bars, window)
                self.assertEqual(actual[:window], [None] * window)
                gain = sum((max(change, 0) for change in changes[:window]), Decimal(0)) / window
                loss = sum((max(-change, 0) for change in changes[:window]), Decimal(0)) / window
                for index in range(window, len(prices)):
                    if index > window:
                        gain = (gain * (window - 1) + max(changes[index - 1], 0)) / window
                        loss = (loss * (window - 1) + max(-changes[index - 1], 0)) / window
                    expected = Decimal(50) if gain + loss == 0 else 100 * gain / (gain + loss)
                    with self.subTest(window=window, index=index):
                        self.assertAlmostEqual(actual[index], float(expected), delta=1e-11)

    def test_large_finite_changes_stay_finite_and_between_zero_and_one_hundred(self):
        values = rsi_close(make_bars([1e-308, 1e308, 1e-308, 1e308, 1e-308]), 2)
        self.assertAlmostEqual(values[2], 50.0)
        for value in values[2:]:
            self.assertTrue(isfinite(value))
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)

    def test_restarting_history_can_change_latest_rsi(self):
        bars = make_bars([10, 12, 11, 14, 13, 15])
        self.assertNotAlmostEqual(rsi_close(bars, 3)[-1], rsi_close(bars[1:], 3)[-1])


class BollTests(unittest.TestCase):
    def test_population_variance_and_first_rolling_window_match_hand_calculation(self):
        values = boll_close(make_bars([1, 2, 3, 5]), 3)
        self.assertEqual(values[:2], [BollValue(None, None, None)] * 2)
        self.assertEqual(values[2].middle, 2.0)
        self.assertAlmostEqual(values[2].upper, 2 + 2 * (2 / 3) ** 0.5)
        self.assertAlmostEqual(values[2].lower, 2 - 2 * (2 / 3) ** 0.5)
        self.assertAlmostEqual(values[3].middle, 10 / 3)
        self.assertAlmostEqual(values[3].upper, 10 / 3 + 2 * (14 / 9) ** 0.5)

    def test_default_twenty_record_initialization(self):
        bars = make_bars([10.0] * 21)
        self.assertEqual(boll_close(bars[:19]), [BollValue(None, None, None)] * 19)
        self.assertEqual(boll_close(bars[:20])[-1], BollValue(10.0, 10.0, 10.0))
        self.assertEqual(boll_close(bars)[19:], [BollValue(10.0, 10.0, 10.0)] * 2)

    def test_window_one_and_constant_prices_have_equal_bands(self):
        self.assertEqual(boll_close(make_bars([1, 9, 4]), 1),
                         [BollValue(close, close, close) for close in (1, 9, 4)])
        self.assertEqual(boll_close(make_bars([7.0] * 6), 3)[2:], [BollValue(7, 7, 7)] * 4)

    def test_multiplier_scales_width_and_negative_lower_band_is_valid(self):
        bars = make_bars([1, 9])
        self.assertEqual(boll_close(bars, 2)[-1], BollValue(5, 13, -3))
        self.assertEqual(boll_close(bars, 2, 0.5)[-1], BollValue(5, 7, 3))
        self.assertEqual(boll_close(bars, 2, 5)[-1], BollValue(5, 25, -15))

    def test_matches_decimal_population_variance_reference(self):
        closes = [10.2, 10.35, 10.1, 10.0, 10.4, 9.8, 11.3, 10.1] * 4
        bars = make_bars(closes)
        with localcontext() as context:
            context.prec = 60
            prices = [Decimal(str(close)) for close in closes]
            for window in (2, 3, 20):
                values = boll_close(bars, window, 1.5)
                for index in range(window - 1, len(prices)):
                    sample = prices[index + 1 - window:index + 1]
                    middle = sum(sample) / window
                    deviation = (sum((value - middle) ** 2 for value in sample) / window).sqrt()
                    expected = (middle, middle + Decimal("1.5") * deviation,
                                middle - Decimal("1.5") * deviation)
                    with self.subTest(window=window, index=index):
                        for actual, reference in zip((values[index].middle, values[index].upper,
                                                      values[index].lower), expected):
                            self.assertAlmostEqual(actual, float(reference), delta=1e-11)

    def test_small_dispersion_on_large_prices_does_not_collapse_variance(self):
        center = 1e12
        value = boll_close(make_bars([center - 1, center, center + 1]), 3)[-1]
        self.assertEqual(value.middle, center)
        self.assertAlmostEqual(value.upper - center, 2 * (2 / 3) ** 0.5, delta=1e-4)
        self.assertLess(value.lower, center)

    def test_finite_large_constants_do_not_overflow_mean(self):
        self.assertEqual(boll_close(make_bars([1e308] * 4), 3)[2:],
                         [BollValue(1e308, 1e308, 1e308)] * 2)

    def test_rejects_nonfinite_band_results(self):
        for closes, multiplier in (([1.0, 1e308], 4.0), ([1.0, 9.0], 1e308)):
            with self.subTest(closes=closes, multiplier=multiplier):
                with self.assertRaisesRegex(ValueError, "remain finite"):
                    boll_close(make_bars(closes), 2, multiplier)

    def test_rejects_invalid_multiplier_even_for_empty_or_short_history(self):
        for multiplier in (0, -1, True, False, "2", None, float("nan"), float("inf"),
                           -float("inf"), 10**400):
            for function in (boll_close, boll_close_points):
                for bars in ([], make_bars([1])):
                    with self.subTest(function=function.__name__, multiplier=multiplier,
                                      count=len(bars)):
                        with self.assertRaisesRegex(ValueError, "finite and positive"):
                            function(bars, 20, multiplier)

    def test_latest_window_is_independent_of_discarded_older_prices(self):
        bars = make_bars([10, 12, 11, 14, 13, 15])
        self.assertEqual(boll_close(bars, 3)[-1], boll_close(bars[2:], 3)[-1])


class AtrTests(unittest.TestCase):
    def test_true_range_seed_and_first_wilder_updates_match_hand_calculation(self):
        bars = [replace(bar, high_price=close + 1, low_price=close - 1)
                for bar, close in zip(make_bars([10, 12, 11, 15, 14, 16]), [10, 12, 11, 15, 14, 16])]
        values = atr(bars, 3)
        self.assertEqual(values[:3], [None] * 3)
        for actual, expected in zip(values[3:], (10 / 3, 26 / 9, 79 / 27)):
            self.assertAlmostEqual(actual, expected)

    def test_default_fourteen_true_ranges_need_fifteen_bars(self):
        bars = make_bars(list(range(1, 17)))
        self.assertEqual(atr(bars[:13]), [None] * 13)
        self.assertEqual(atr(bars[:14]), [None] * 14)
        self.assertEqual(atr(bars[:15]), [None] * 14 + [1.0])
        self.assertEqual(atr(bars)[14:], [1.0, 1.0])

    def test_previous_close_captures_both_upward_and_downward_gaps(self):
        self.assertEqual(atr(make_bars([10, 20, 8, 9]), 1), [None, 10.0, 12.0, 1.0])

    def test_high_low_range_can_exceed_previous_close_gaps(self):
        bars = make_bars([10, 10])
        bars[1] = replace(bars[1], high_price=15, low_price=5)
        self.assertEqual(atr(bars, 1), [None, 10.0])

    def test_first_bar_range_is_never_fabricated_as_a_true_range(self):
        bars = make_bars([10, 10, 10, 10])
        bars[0] = replace(bars[0], high_price=100, low_price=1)
        self.assertEqual(atr(bars, 3), [None, None, None, 0.0])

    def test_zero_atr_seed_stays_available_and_next_range_updates_it(self):
        values = atr(make_bars([10, 10, 10, 10, 13, 13]), 3)
        self.assertEqual(values[:5], [None, None, None, 0.0, 1.0])
        self.assertAlmostEqual(values[-1], 2 / 3)

    def test_matches_independent_decimal_reference(self):
        closes = [10.2, 10.35, 10.1, 10.0, 10.4, 9.8, 10.8, 9.1] * 4
        bars = [replace(bar, high_price=bar.close_price + 0.3,
                        low_price=bar.close_price - 0.2) for bar in make_bars(closes)]
        with localcontext() as context:
            context.prec = 60
            ranges = []
            for previous, current in zip(bars, bars[1:]):
                high, low, close = (Decimal(str(value)) for value in
                                    (current.high_price, current.low_price, previous.close_price))
                ranges.append(max(high, close) - min(low, close))
            for window in (1, 3, 14, 30):
                actual = atr(bars, window)
                self.assertEqual(actual[:window], [None] * window)
                expected = sum(ranges[:window]) / window
                for index in range(window, len(bars)):
                    if index > window:
                        expected = (expected * (window - 1) + ranges[index - 1]) / window
                    with self.subTest(window=window, index=index):
                        self.assertAlmostEqual(actual[index], float(expected), delta=1e-11)

    def test_large_finite_ranges_do_not_overflow_smoothing(self):
        values = atr(make_bars([1.0, 1e308, 1.0, 1e308, 1.0]), 2)
        self.assertEqual(values[:2], [None, None])
        self.assertEqual(values[2:], [1e308] * 3)

    def test_restarting_history_can_change_latest_atr(self):
        bars = make_bars([10, 20, 11, 14, 13, 15])
        self.assertAlmostEqual(atr(bars, 3)[-1], 112 / 27)
        self.assertAlmostEqual(atr(bars[1:], 3)[-1], 32 / 9)


class VolumeActivityTests(unittest.TestCase):
    def test_inclusive_mean_and_strictly_prior_baseline_match_hand_calculation(self):
        values = volume_activity(make_bars([10] * 5, [10, 20, 30, 120, 0]), 3)
        self.assertEqual(values[:2], [VolumeValue(10, None, None, None, "insufficient_history"),
                                     VolumeValue(20, None, None, None, "insufficient_history")])
        self.assertEqual(values[2], VolumeValue(30, 20.0, None, None, "insufficient_history"))
        self.assertEqual(values[3], VolumeValue(120, 170 / 3, 20.0, 6.0, "available"))
        self.assertEqual(values[4], VolumeValue(0, 50.0, 170 / 3, 0.0, "available"))

    def test_default_mean_and_ratio_have_different_initialization_boundaries(self):
        bars = make_bars([10] * 21, list(range(1, 22)))
        values = volume_activity(bars)
        self.assertIsNone(values[18].volume_ma)
        self.assertEqual(values[19], VolumeValue(20, 10.5, None, None, "insufficient_history"))
        self.assertEqual(values[20], VolumeValue(21, 11.5, 10.5, 2.0, "available"))

    def test_zero_baseline_is_undefined_and_recovers_after_positive_volume(self):
        values = volume_activity(make_bars([10] * 6, [0, 0, 0, 30, 0, 0]), 3)
        self.assertEqual(values[2], VolumeValue(0, 0.0, None, None, "insufficient_history"))
        self.assertEqual(values[3], VolumeValue(30, 10.0, 0.0, None, "undefined"))
        self.assertEqual(values[4], VolumeValue(0, 10.0, 10.0, 0.0, "available"))
        self.assertEqual(values[5].relative_volume_status, "available")
        self.assertEqual(volume_activity(make_bars([10] * 4, [0] * 4), 3)[-1],
                         VolumeValue(0, 0.0, 0.0, None, "undefined"))

    def test_window_one_baseline_is_the_previous_record(self):
        self.assertEqual(volume_activity(make_bars([10] * 4, [0, 10, 20, 0]), 1),
                         [VolumeValue(0, 0.0, None, None, "insufficient_history"),
                          VolumeValue(10, 10.0, 0.0, None, "undefined"),
                          VolumeValue(20, 20.0, 10.0, 2.0, "available"),
                          VolumeValue(0, 0.0, 20.0, 0.0, "available")])

    def test_matches_decimal_window_means_and_prior_only_ratios(self):
        volumes = [10, 20, 0, 45, 17, 100, 0, 0] * 4
        bars = make_bars([10] * len(volumes), volumes)
        with localcontext() as context:
            context.prec = 60
            for window in (1, 3, 20):
                values = volume_activity(bars, window)
                for index, actual in enumerate(values):
                    with self.subTest(window=window, index=index):
                        if index + 1 >= window:
                            expected = Decimal(sum(volumes[index + 1 - window:index + 1])) / window
                            self.assertAlmostEqual(actual.volume_ma, float(expected), delta=1e-11)
                        else:
                            self.assertIsNone(actual.volume_ma)
                        if index >= window:
                            baseline = Decimal(sum(volumes[index - window:index])) / window
                            self.assertAlmostEqual(actual.previous_volume_ma, float(baseline), delta=1e-11)
                            if baseline:
                                self.assertAlmostEqual(actual.relative_volume,
                                                       float(Decimal(volumes[index]) / baseline), delta=1e-11)
                                self.assertEqual(actual.relative_volume_status, "available")
                            else:
                                self.assertIsNone(actual.relative_volume)
                                self.assertEqual(actual.relative_volume_status, "undefined")
                        else:
                            self.assertIsNone(actual.previous_volume_ma)
                            self.assertIsNone(actual.relative_volume)
                            self.assertEqual(actual.relative_volume_status, "insufficient_history")

    def test_large_integer_volumes_do_not_overflow_intermediate_sums(self):
        volume = 10**308
        values = volume_activity(make_bars([10] * 4, [volume] * 4), 3)
        self.assertEqual(values[-1], VolumeValue(volume, 1e308, 1e308, 1.0, "available"))

    def test_rejects_unrepresentable_mean_or_relative_volume(self):
        with self.assertRaisesRegex(ValueError, "remain finite"):
            volume_activity(make_bars([10], [10**400]), 1)
        with self.assertRaisesRegex(ValueError, "remain finite"):
            volume_activity(make_bars([10] * 4, [1, 1, 1, 10**309]), 3)

    def test_older_records_outside_baseline_do_not_change_latest_volume_fields(self):
        bars = make_bars([10] * 6, [10, 50, 20, 100, 60, 0])
        self.assertEqual(volume_activity(bars, 3)[-1], volume_activity(bars[2:], 3)[-1])


class TechnicalIndicatorContractTests(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars([10, 12, 11, 14, 13, 15], [0, 10, 20, 30, 0, 50])

    def test_empty_inputs_have_no_values_or_points(self):
        for function in ALL_CALCULATIONS:
            with self.subTest(function=function.__name__):
                self.assertEqual(function([]), [])

    def test_invalid_windows_are_rejected_even_for_empty_input(self):
        for window in (0, -1, True, False, 1.5, "3", None, float("nan"), float("inf")):
            for function in ALL_CALCULATIONS:
                with self.subTest(function=function.__name__, window=window):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        function([], window)

    def test_very_large_windows_are_valid_but_unavailable(self):
        for function in ALL_CALCULATIONS:
            with self.subTest(function=function.__name__):
                values = function(self.bars, 10**400)
                self.assertEqual(len(values), len(self.bars))
                if function in (rsi_close, atr):
                    self.assertEqual(values, [None] * len(self.bars))
                elif function in (boll_close, boll_close_points):
                    self.assertTrue(all(value.middle is None for value in values))
                elif function in (volume_activity, volume_activity_points):
                    self.assertTrue(all(value.relative_volume_status == "insufficient_history"
                                        and value.volume_ma is None for value in values))
                else:
                    self.assertTrue(all(value.value is None for value in values))

    def test_rejects_mixed_instruments_duplicate_or_reversed_dates_during_warmup(self):
        invalid_sequences = (
            [self.bars[0], replace(self.bars[1], instrument_id="600000.SH")],
            [self.bars[1], self.bars[0]],
            [self.bars[0], replace(self.bars[1], trade_date=self.bars[0].trade_date)],
        )
        for bars in invalid_sequences:
            for function in ALL_CALCULATIONS:
                with self.subTest(function=function.__name__, bars=bars):
                    with self.assertRaises(ValueError):
                        function(bars, 20)

    def test_rejects_missing_or_wrong_record_types_without_silently_filling(self):
        for record in (None, {}, "not a bar", 12.0):
            for function in ALL_CALCULATIONS:
                with self.subTest(function=function.__name__, record=record):
                    with self.assertRaisesRegex(ValueError, "DailyBar records"):
                        function([self.bars[0], record], 20)

    def test_rejects_non_sequences_and_single_use_iterators(self):
        for function in ALL_CALCULATIONS:
            for bars in (None, "", b"", bytearray(), {}, iter(self.bars)):
                with self.subTest(function=function.__name__, bars_type=type(bars).__name__):
                    with self.assertRaisesRegex(ValueError, "DailyBar records"):
                        function(bars, 3)

    def test_tuple_inputs_repeated_calls_and_outputs_do_not_mutate_input(self):
        original = self.bars.copy()
        for function in ALL_CALCULATIONS:
            with self.subTest(function=function.__name__):
                first = function(self.bars, 3)
                self.assertEqual(first, function(tuple(self.bars), 3))
                self.assertEqual(first, function(self.bars, 3))
                first.clear()
                self.assertEqual(self.bars, original)
                self.assertEqual(len(function(self.bars, 3)), len(original))

    def test_points_keep_instrument_date_period_and_all_values(self):
        point_classes = (RsiPoint, BollPoint, AtrPoint, VolumePoint)
        value_fields = (("value",), ("middle", "upper", "lower"), ("value",),
                        ("volume_shares", "volume_ma", "previous_volume_ma",
                         "relative_volume", "relative_volume_status"))
        for values_function, points_function, point_class, fields in zip(
                CALCULATIONS, POINT_CALCULATIONS, point_classes, value_fields):
            values = values_function(self.bars, 3)
            points = points_function(self.bars, 3)
            self.assertEqual(len(points), len(self.bars))
            for bar, value, point in zip(self.bars, values, points):
                with self.subTest(function=points_function.__name__, date=bar.trade_date):
                    self.assertIsInstance(point, point_class)
                    self.assertEqual((point.instrument_id, point.trade_date, point.window),
                                     (bar.instrument_id, bar.trade_date, 3))
                    for field in fields:
                        self.assertEqual(getattr(point, field), value if field == "value"
                                         else getattr(value, field))
                    with self.assertRaises(FrozenInstanceError):
                        point.window = 4
        self.assertTrue(all(point.stddev_multiplier == 1.5
                            for point in boll_close_points(self.bars, 3, 1.5)))

    def test_date_gaps_count_records_without_inventing_or_filling_bars(self):
        gaps = [0, 3, 9, 19, 20, 30]
        spaced = [replace(bar, trade_date=date(2026, 9, 1) + timedelta(days=offset))
                  for bar, offset in zip(self.bars, gaps)]
        for calculation, points_calculation in zip(CALCULATIONS, POINT_CALCULATIONS):
            with self.subTest(function=calculation.__name__):
                self.assertEqual(calculation(spaced, 3), calculation(self.bars, 3))
                self.assertEqual([point.trade_date for point in points_calculation(spaced, 3)],
                                 [bar.trade_date for bar in spaced])

    def test_every_prefix_matches_full_history_before_any_future_record(self):
        for function in ALL_CALCULATIONS:
            for window in (1, 2, 3, 20):
                full = function(self.bars, window)
                for length in range(len(self.bars) + 1):
                    with self.subTest(function=function.__name__, window=window, length=length):
                        self.assertEqual(function(self.bars[:length], window), full[:length])

    def test_changing_or_appending_future_prices_and_volumes_preserves_earlier_results(self):
        future = replace(self.bars[-1], open_price=100, high_price=100, low_price=100,
                         close_price=100, volume_shares=1000000)
        appended = replace(future, trade_date=future.trade_date + timedelta(days=3))
        for function in ALL_CALCULATIONS:
            with self.subTest(function=function.__name__):
                original = function(self.bars, 3)
                self.assertEqual(function([*self.bars[:-1], future], 3)[:-1], original[:-1])
                self.assertEqual(function([*self.bars, appended], 3)[:-1], original)

    def test_cutoff_date_isolates_all_point_calculations_from_later_data(self):
        cutoff = self.bars[3].trade_date
        selected = bars_as_of_close(self.bars, cutoff)
        changed_future = [*self.bars[:4], *[
            replace(bar, open_price=100, high_price=101, low_price=99,
                    close_price=100, volume_shares=1000000) for bar in self.bars[4:]]]
        for function in POINT_CALCULATIONS:
            with self.subTest(function=function.__name__):
                points = function(selected, 3)
                self.assertEqual(points, function(self.bars, 3)[:4])
                self.assertEqual(points, function(bars_as_of_close(changed_future, cutoff), 3))
                self.assertEqual(points[-1].trade_date, cutoff)


if __name__ == "__main__":
    unittest.main()
