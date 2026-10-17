import unittest
from datetime import date
from pathlib import Path

from trading_companion.csv_loader import load_daily_bars
from trading_companion.history import bars_as_of_close
from trading_companion.indicators import ema_close_points, macd_close_points, sma_close_points


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


if __name__ == "__main__":
    unittest.main()
