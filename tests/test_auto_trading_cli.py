from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from auto_trading.__main__ import main, run_directory
from auto_trading.contracts import load_profile
from tests.market_fixtures import snapshot


class CliSandboxTests(unittest.TestCase):
    def test_run_directory_cannot_escape_research_output(self):
        with self.assertRaisesRegex(ValueError, "RUN_PATH_OUTSIDE_RESEARCH_OUTPUT"):
            run_directory("../secret")
        with self.assertRaisesRegex(ValueError, "RUN_PATH_OUTSIDE_RESEARCH_OUTPUT"):
            run_directory(".")
        target = run_directory("paper-refused")
        self.assertEqual(target.name, "paper-refused")
        self.assertIn("auto_trading_runs", str(target))

    def test_paper_without_gate_returns_refused_and_touches_no_network(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["paper", "--run", "paper-refused-cli"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "REFUSED")
        self.assertIn("RESEARCH_GATE_REFUSED", payload["reason"])
        self.assertIs(payload["production_allowed"], False)

    def test_replay_is_offline_and_ordered(self):
        snap = snapshot()
        as_of = snap["acquired_at"]
        fixture = {
            "evidence_kind": "UNVERIFIED_FIXTURE",
            "events": [
                {"as_of": as_of, "snapshots": {snap["symbol"]: snap}, "execution_events": [], "funding_events": []},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replay.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["replay", "--run", "synthetic-replay-cli-isolated", "--input", str(path)])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["research_gate"], "NOT_EVALUATED")
            self.assertIs(payload["production_allowed"], False)

    def test_load_profile_stays_research_only(self):
        profile = load_profile()
        self.assertIs(profile["production_allowed"], False)
        self.assertEqual(os.environ.get("ENABLE_LIVE_TRADING", ""), "")


if __name__ == "__main__":
    unittest.main()
