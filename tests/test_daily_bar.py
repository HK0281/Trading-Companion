import unittest
from datetime import date

from trading_companion.daily_bar import DailyBar


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


if __name__ == "__main__":
    unittest.main()
