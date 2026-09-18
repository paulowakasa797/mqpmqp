from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from auto_trading.contracts import load_profile, strategy_hash
from auto_trading.research_gate import verify_gate
from auto_trading.runtime import require_mode


class ResearchGateTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()

    def test_missing_report_is_fail_closed(self):
        result = verify_gate(None, self.profile, None)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["production_allowed"])
        self.assertIn("RESEARCH_GATE_REPORT_MISSING", result["reasons"])

    def test_synthetic_evidence_cannot_open_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report = {
                "evidence_kind": "UNVERIFIED_FIXTURE",
                "strategy_hash": strategy_hash(),
                "profile_digest": "x",
                "oos_trades": [],
            }
            report_path = tmp_path / "gate.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            manifest = tmp_path / "manifest.json"
            manifest.write_text(json.dumps({"entries": []}), encoding="utf-8")
            result = verify_gate(report_path, self.profile, manifest)
            self.assertFalse(result["allowed"])
            self.assertIn("REAL_MARKET_EVIDENCE_REQUIRED", result["reasons"])
            self.assertFalse(result["production_allowed"])

    def test_require_mode_paper_refuses_without_artifacts(self):
        with self.assertRaisesRegex(ValueError, "RESEARCH_GATE_REFUSED"):
            require_mode("paper", self.profile)
        with self.assertRaisesRegex(ValueError, "LIVE_FORBIDDEN"):
            require_mode("live", self.profile)

    def test_live_env_switch_is_refused(self):
        import os
        os.environ["ENABLE_LIVE_TRADING"] = "1"
        try:
            with self.assertRaisesRegex(ValueError, "LIVE_FORBIDDEN"):
                require_mode("observe", self.profile)
        finally:
            del os.environ["ENABLE_LIVE_TRADING"]

    def test_mutated_production_profile_is_refused(self):
        bad = dict(self.profile)
        bad["production_allowed"] = True
        with self.assertRaisesRegex(ValueError, "LIVE_FORBIDDEN"):
            require_mode("observe", bad)


if __name__ == "__main__":
    unittest.main()
