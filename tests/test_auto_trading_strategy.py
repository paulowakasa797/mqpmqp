from __future__ import annotations

import unittest

from auto_trading.contracts import load_profile
from auto_trading.data import validate_snapshot, visible_snapshot
from auto_trading.strategy import (
    _regime_eligibility,
    assess_entry,
    confirmed_pivots,
    market_regime,
)
from tests.market_fixtures import kline, snapshot


class StrategyCausalityTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()

    def test_pivot_is_unknown_until_right_wing_closes(self):
        start = 1_776_000_000_000
        step = 300_000
        # Bars 0-1 low, bar 2 unique high, bars 3-4 right wing.
        prices = [
            (100, 101, 99, 100.5),
            (100.5, 101, 99.5, 100.2),
            (100.2, 110, 100, 109),
            (109, 109.5, 108, 108.5),
            (108.5, 109, 107, 108),
            (108, 108.2, 107.5, 107.8),
        ]
        rows = [kline(start + i * step, *px) for i, px in enumerate(prices)]
        before = confirmed_pivots(rows, rows[3]["period_end"], self.profile)
        after = confirmed_pivots(rows, rows[4]["period_end"], self.profile)
        self.assertFalse(any(p["kind"] == "HIGH" and p["price"] == 110 for p in before))
        self.assertTrue(any(p["kind"] == "HIGH" and p["price"] == 110 for p in after))

    def test_future_received_bar_is_hidden_from_decision(self):
        snap = snapshot()
        as_of = snap["bars"]["5m"][-2]["period_end"]
        future = dict(snap["bars"]["5m"][-1])
        future["received_at"] = as_of + 60_000
        future["available_at"] = as_of + 60_000
        snap["bars"]["5m"][-1] = future
        visible = visible_snapshot(snap, as_of)
        self.assertNotEqual(visible["bars"]["5m"][-1]["period_end"], future["period_end"])
        self.assertTrue(all(r["received_at"] <= as_of for r in visible["bars"]["5m"]))

    def test_unknown_regime_never_opens_an_entry_path(self):
        snap = snapshot()
        trigger = snap["bars"]["5m"][-1]
        reasons, ok, _ = _regime_eligibility(snap, trigger, "LONG", {"state": "UNKNOWN"}, {}, 2.0, self.profile)
        self.assertFalse(ok)
        self.assertIn("REGIME_CONFLICT:UNKNOWN", reasons)

    def test_tight_stop_is_rejected_when_fees_dominate_structural_risk(self):
        result = assess_entry("LONG", 100.0, 99.95, [101.0, 102.0, 103.0], self.profile)
        self.assertFalse(result["ok"])
        self.assertIn("COST_DOMINATES_STRUCTURAL_RISK", result["reasons"])

    def test_assess_entry_keeps_fee_slippage_and_funding_on_both_sides(self):
        result = assess_entry("LONG", 100.0, 97.0, [110.0, 114.0, 118.0], self.profile)
        fee, slip, funding = self.profile["fee_rate"], self.profile["slippage_bps"] / 10000, self.profile["funding_reserve_rate"]
        expected_stop = (100 + 97) * fee + 97 * slip + 100 * funding
        self.assertAlmostEqual(result["costs"]["stop"], expected_stop)
        self.assertGreater(result["risk_per_unit"], 3.0)
        self.assertGreater(result["net_rr"], self.profile["min_net_rr"])
        self.assertTrue(result["ok"], result["reasons"])

    def test_current_breakout_bar_is_excluded_from_pivot_confirmation(self):
        start = 1_776_000_000_000
        step = 300_000
        rows = []
        price = 100.0
        for i in range(12):
            rows.append(kline(start + i * step, price, price + 1, price - 0.5, price + 0.2))
            price += 0.2
        # Last bar closes through a high that only exists on that same bar.
        rows.append(kline(start + 12 * step, 110, 120, 110, 119))
        as_of = rows[-1]["period_end"]
        pivots = confirmed_pivots(rows[:-1], rows[-1]["period_start"], self.profile)
        self.assertFalse(any(p["price"] == 120 for p in pivots))
        later = confirmed_pivots(rows, as_of, self.profile)
        # Even after close, a last-bar extreme has no right wing yet.
        self.assertFalse(any(p["price"] == 120 for p in later))

    def test_validate_snapshot_accepts_the_fixture_and_rejects_a_gap(self):
        snap = snapshot()
        as_of = snap["acquired_at"]
        self.assertEqual(validate_snapshot(snap, as_of, self.profile, for_entry=True), [])
        gapped = snapshot()
        gapped["bars"]["5m"] = gapped["bars"]["5m"][:10] + gapped["bars"]["5m"][12:]
        errors = validate_snapshot(gapped, as_of, self.profile)
        self.assertTrue(any(e.startswith("SCHEMA_CONFLICT:5m") for e in errors))

    def test_market_regime_is_unknown_without_benchmarks(self):
        regime = market_regime({}, 1_776_012_000_000, self.profile)
        self.assertEqual(regime["state"], "UNKNOWN")
        for symbol in self.profile["benchmarks"]:
            self.assertEqual(regime["evidence"][symbol]["trend"], "unknown")


if __name__ == "__main__":
    unittest.main()
