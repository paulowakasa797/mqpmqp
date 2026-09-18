from __future__ import annotations

import unittest

from auto_trading.contracts import load_profile
from auto_trading.execution import Simulator
from tests.market_fixtures import kline, snapshot


def _bar(start: int, o: float, h: float, l: float, c: float, symbol="BTCUSDT", step=60_000, received=None):
    return kline(start, o, h, l, c, symbol=symbol, step=step, received=received)


def _quote(symbol: str, bid: float, ask: float, at: int) -> dict:
    return {
        "data": {"bid": bid, "ask": ask, "bid_qty": 100.0, "ask_qty": 100.0},
        "event_time": at - 200,
        "received_at": at,
        "available_at": at,
        "source_endpoint": "/fapi/v1/ticker/bookTicker",
        "symbol": symbol,
        "units": "price_USDT;quantity_base",
        "raw_ref": "quote-fixture",
    }


def _signal(as_of: int, signal_id: str, setup_id: str) -> dict:
    return {
        "signal_id": signal_id,
        "setup_id": setup_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stage": "C0",
        "decision_time": as_of,
        "event_time": as_of,
        "received_at": as_of,
        "available_at": as_of,
        "entry": 109.3,
        "stop": 90.0,
        "targets": [160.0, 180.0, 200.0],
        "atr": 8.0,
        "breakout_level": 108.0,
        "stop_source": {"source": "test"},
        "target_sources": [],
        "invalidation_level": 108.0,
    }


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()
        self.sim = Simulator(self.profile)
        self.snap = snapshot()
        self.as_of = self.snap["acquired_at"]

    def _submit(self, sim: Simulator, suffix: str) -> dict:
        event = sim.submit(_signal(self.as_of, f"sig-{suffix}", f"setup-{suffix}"), self.snap, self.as_of)
        self.assertEqual(event["status"], "VIRTUAL_ORDER_WORKING", event)
        return event

    def _next_minute(self, after: int) -> int:
        return (after // 60_000 + 1) * 60_000

    def test_same_bar_stop_is_taken_before_take_profit(self):
        self._submit(self.sim, "long-1")
        open_start = self._next_minute(self.as_of)
        quote = _quote("BTCUSDT", 109.0, 109.2, open_start)
        fill_bar = _bar(open_start, 109.2, 110.0, 108.8, 109.5, received=open_start + 61_000)
        fills = self.sim.process_bar("BTCUSDT", fill_bar, quote, open_start + 65_000)
        self.assertTrue(any(e["status"] == "VIRTUAL_FILLED" for e in fills), fills)
        self.assertIn("BTCUSDT", self.sim.state["positions"])
        next_start = open_start + 60_000
        crash = _quote("BTCUSDT", 89.0, 89.2, next_start)
        bar = _bar(next_start, 109.0, 170.0, 80.0, 165.0, received=next_start + 61_000)
        events = self.sim.process_bar("BTCUSDT", bar, crash, next_start + 65_000)
        self.assertTrue(any(e.get("reason") == "HARD_STOP" for e in events), events)
        self.assertNotIn("BTCUSDT", self.sim.state["positions"])
        self.assertFalse(any(str(e.get("reason", "")).startswith("TP") for e in events))

    def test_bar_gap_cancels_working_entry_and_does_not_fill(self):
        sim = Simulator(self.profile)
        self._submit(sim, "gap-1")
        first_start = self._next_minute(self.as_of)
        quote = _quote("BTCUSDT", 109.0, 109.2, first_start)
        # Record the first minute without filling (start is not yet > eligible_after if equal).
        # eligible_after is as_of; choose a tracked bar at or before eligible_after, then jump.
        marker = first_start - 60_000
        if marker <= 0:
            marker = first_start
        early = _bar(marker, 109.0, 109.1, 108.9, 109.05, received=marker + 61_000)
        sim.process_bar("BTCUSDT", early, _quote("BTCUSDT", 109.0, 109.2, marker), marker + 65_000)
        jumped = _bar(marker + 120_000, 109.2, 110.0, 108.8, 109.5, received=marker + 181_000)
        events = sim.process_bar("BTCUSDT", jumped, quote, marker + 185_000)
        reasons = [e.get("reason") for e in events]
        self.assertIn("EXECUTION_BAR_GAP", reasons, events)
        self.assertFalse(any(e["status"] == "VIRTUAL_FILLED" for e in events), events)
        self.assertEqual(sim.state["positions"], {})

    def test_production_flag_stays_false_on_every_fill_event(self):
        self._submit(self.sim, "flag-1")
        self.assertIs(self.sim.state["production_allowed"], False)
        for event in self.sim.state["audit"]:
            self.assertIs(event["production_allowed"], False)


if __name__ == "__main__":
    unittest.main()
