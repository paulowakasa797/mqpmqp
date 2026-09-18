from __future__ import annotations

import argparse
import json
from pathlib import Path

from auto_trading.contracts import load_profile
from auto_trading.runtime import run_observe, run_replay, status


def run_directory(value: str) -> Path:
    """CLI can only write its own research output subtree, never original assets."""
    root = Path(__file__).resolve().parents[1] / "reports" / "auto_trading_runs"
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("RUN_PATH_OUTSIDE_RESEARCH_OUTPUT")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Candidate research: public observe / offline replay; production_allowed=false")
    parser.add_argument("mode", nargs="?", default="observe", choices=["observe", "replay", "paper", "status", "stop"])
    parser.add_argument("--run", default="observe", help="Directory name under reports/auto_trading_runs")
    parser.add_argument("--input", type=Path, help="Offline replay JSON with ordered as_of events")
    parser.add_argument("--seconds", type=float, default=0, help="0 = one round; otherwise bounded foreground scheduler")
    parser.add_argument("--max-requests", type=int, default=256, help="Upper request count per worker round")
    parser.add_argument("--gate-report", type=Path)
    parser.add_argument("--data-manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        directory = run_directory(args.run)
        if not 0 <= args.seconds <= 2700 or not 1 <= args.max_requests <= 256:
            raise ValueError("BOUND_REQUIRED: seconds 0..2700 and requests 1..256")
        profile = load_profile()
        if args.mode == "status":
            result = status(directory)
        elif args.mode == "stop":
            if not (directory / "state.json").exists():
                raise ValueError("RUN_NOT_FOUND")
            (directory / "stop.request").write_text("stop after current bounded public request\n", encoding="utf-8")
            result = {"status": "STOP_REQUESTED", "production_allowed": False}
        elif args.mode == "replay":
            if args.input is None:
                raise ValueError("REPLAY_INPUT_REQUIRED")
            result = run_replay(args.input, directory, profile)
        else:
            result = run_observe(directory, profile, args.seconds, args.mode,
                                 args.gate_report, args.data_manifest, args.max_requests)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.mode in {"observe", "paper"}:
            health = result.get("health", {})
            if (health.get("fast_watch", {}).get("failures", 0)
                or health.get("fast_watch", {}).get("deferred", 0)) or any(
                lane.get("failures", 0) for lane in health.get("scheduler", {}).values()
            ):
                return 2
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc), "production_allowed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
