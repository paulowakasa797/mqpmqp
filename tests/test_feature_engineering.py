from __future__ import annotations

import unittest

from feature_engineering import atr, ema, profit_factor, rolling_mean


class FeatureEngineeringTests(unittest.TestCase):
    def test_wilder_atr_uses_prior_close_and_needs_period_plus_one_bars(self):
        bars = []
        price = 100.0
        for i in range(16):
            bars.append({"open": price, "high": price + 2, "low": price - 1, "close": price + 0.5, "volume": 10})
            price += 0.5
        values = atr(bars, 14)
        self.assertIsNone(values[12])
        self.assertIsNotNone(values[13])
        self.assertIsNotNone(values[-1])
        self.assertGreater(values[-1], 0)

    def test_invalid_periods_do_not_raise_or_invent_values(self):
        values = [1.0, 2.0, 3.0]
        self.assertEqual(ema(values, 0), [None, None, None])
        self.assertEqual(atr([{"high": 2, "low": 1, "close": 1.5}] * 20, 0), [None] * 20)
        self.assertEqual(rolling_mean(values, 0), [None, None, None])

    def test_profit_factor_is_not_inf_for_empty_or_all_zero(self):
        self.assertEqual(profit_factor([]), 0.0)
        self.assertEqual(profit_factor([0.0, 0.0]), 0.0)
        self.assertGreater(profit_factor([1.0, -0.5]), 1.0)


if __name__ == "__main__":
    unittest.main()
