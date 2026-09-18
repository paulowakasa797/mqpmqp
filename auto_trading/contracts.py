from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    """Stable binding; non-finite values never receive a valid data hash."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def load_profile() -> dict:
    return json.loads(Path(__file__).with_name("profile.json").read_text(encoding="utf-8"))


def record(data: dict, event_time: int, received_at: int, source_endpoint: str,
           symbol: str, units: str, period_start: int | None = None,
           period_end: int | None = None, available_at: int | None = None) -> dict:
    item = {"data": data, "event_time": event_time, "received_at": received_at,
            "available_at": received_at if available_at is None else available_at,
            "source_endpoint": source_endpoint, "symbol": symbol, "units": units,
            "raw_ref": digest(data)}
    if period_start is not None:
        item["period_start"] = period_start
    if period_end is not None:
        item["period_end"] = period_end
    return item


def finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def strategy_hash() -> str:
    """Bind the complete executable candidate, including execution and gate code."""
    root = Path(__file__).parent
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(root.glob("*.py"))}
    sources["../feature_engineering.py"] = hashlib.sha256((root.parent / "feature_engineering.py").read_bytes()).hexdigest()
    return digest(sources)
