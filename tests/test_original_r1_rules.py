from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "reports/pump_r1_review_20260919/evidence/baseline/candidate/rules.py"
REPLAY_PATH = ROOT / "reports/pump_r1_review_20260919/replay.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r1 = load(RULES_PATH, "audit_r1_rules")
replay = load(REPLAY_PATH, "audit_r1_replay")
STEP = 300_000


class OriginalR1RulesTests(unittest.TestCase):
    def test_rule_match_never_authorizes_execution(self):
        self.assertTrue(r1.RULES.__dataclass_params__.frozen)
        now = 1_775_998_800_000 + 180 * STEP
        raw = []
        price = 100.0
        for i in range(181):
            start = 1_775_998_800_000 + i * STEP
            raw.append([start, price, price + 1, price - 1, price + 0.2, 10, start + STEP - 1, 1000, 4, 5, 500, 0])
            price += 0.2
        alert, reason, evidence = r1.evaluate_setup(raw, now_ms=now, symbol="BTCUSDT")
        self.assertIsInstance(reason, str)
        self.assertNotEqual(reason, "")
        if alert is not None:
            self.assertIs(alert["execution_authorized"], False)
        self.assertIs(evidence["execution_authorized"], False)
        self.assertEqual(evidence["profitability"], "NOT_ESTABLISHED")

    def test_context_at_drops_unclosed_and_future_bars(self):
        start = 1_775_998_800_000
        raw = []
        for i in range(10):
            open_ms = start + i * STEP
            raw.append([open_ms, 1, 2, 0.5, 1.5, 10, open_ms + STEP - 1, 10, 1, 1, 1, 0])
        now = start + 5 * STEP
        visible = replay.context_at(raw, now)
        self.assertTrue(all(row[6] < now for row in visible))
        self.assertEqual(visible[-1][0], start + 4 * STEP)

    def test_future_kline_inside_history_is_rejected(self):
        start = 1_775_998_800_000
        raw = []
        for i in range(180):
            open_ms = start + i * STEP
            raw.append([open_ms, 1, 2, 0.5, 1.5, 10, open_ms + STEP - 1, 10, 1, 1, 1, 0])
        raw[10][6] = start + 400 * STEP
        with self.assertRaises(r1.Reject):
            r1.closed_bars(raw, start + 180 * STEP)


if __name__ == "__main__":
    unittest.main()
