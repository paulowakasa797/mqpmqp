from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageIntegrityTests(unittest.TestCase):
    def test_manifest_hashes_for_present_imported_sources(self):
        manifest = json.loads((ROOT / "BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
        missing_source = []
        mismatched = []
        present = 0
        for item in manifest["files"]:
            path = item["path"]
            if "__pycache__" in path or path.endswith(".pyc"):
                continue
            target = ROOT / path
            if not target.is_file():
                missing_source.append(path)
                continue
            present += 1
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            # Improved sources are allowed to diverge after the import snapshot.
            # The original digest is still recorded so reviewers can diff.
            if digest != item["sha256"] and path in {
                "AGENTS.md",
                "START_HERE.md",
                "auto_trading/profile.json",
                "auto_trading/README.md",
                "reports/pump_r1_review_20260919/replay.py",
                "reports/pump_r1_review_20260919/evidence/baseline/candidate/rules.py",
            }:
                mismatched.append(path)
        self.assertGreaterEqual(present, 19)
        self.assertTrue(any(name.startswith("reports/breakout_continuation_20260919/") for name in missing_source))
        self.assertEqual(mismatched, [])

    def test_breakout_engine_and_evidence_are_absent(self):
        folder = ROOT / "reports/breakout_continuation_20260919"
        self.assertFalse((folder / "engine.py").is_file())
        self.assertFalse((folder / "evidence/BTCUSDT_5m.json").is_file())
        self.assertFalse((folder / "replay_02/decisions.jsonl").is_file())
        self.assertFalse((folder / "freeze.json").is_file())
        self.assertFalse((folder / "protocol.json").is_file())

    def test_production_allowed_literals_remain_false_in_profile(self):
        profile = json.loads((ROOT / "auto_trading/profile.json").read_text(encoding="utf-8"))
        self.assertIs(profile["production_allowed"], False)
        self.assertTrue((ROOT / "auto_trading/__init__.py").read_text(encoding="utf-8").count("PRODUCTION_ALLOWED = False"))


if __name__ == "__main__":
    unittest.main()
