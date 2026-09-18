"""Pure causal candidate rules, unvalidated for trading profitability."""
from __future__ import annotations

import copy
import math
from statistics import median

from feature_engineering import atr as wilder_atr
from auto_trading.contracts import digest, finite_positive
from auto_trading.data import validate_snapshot, visible_snapshot


def _received(row: dict) -> int:
    # Estimated replay keeps download time separately; this is its declared assumption.
    return row["available_at"] if row.get("availability_estimated") is True else row["received_at"]


def _completed(rows: list[dict], as_of: int) -> list[dict]:
    return [r for r in rows if r.get("period_end", as_of+1) <= as_of
            and r.get("available_at", as_of+1) <= as_of and _received(r) <= as_of]


def confirmed_pivots(rows: list[dict], as_of: int, profile: dict) -> list[dict]:
    """A strict pivot is known only after its complete right wing has arrived."""
    rows = _completed(rows, as_of)
    left, right = profile["pivot_left"], profile["pivot_right"]
    result = []
    for i in range(left, len(rows)-right):
        row = rows[i]
        neighbors = rows[i-left:i] + rows[i+1:i+right+1]
        confirmation = max(max(r["period_end"], r["available_at"], _received(r))
                           for r in rows[i-left:i+right+1])
        for kind, field in (("HIGH", "high"), ("LOW", "low")):
            value = row["data"][field]
            valid = all(value > r["data"][field] for r in neighbors) if kind == "HIGH" else all(value < r["data"][field] for r in neighbors)
            if valid:
                result.append({"kind": kind, "price": value, "event_time": row["event_time"],
                               "period_end": row["period_end"], "confirmed_at": confirmation,
                               "raw_ref": row["raw_ref"], "index": i})
    return result


def _trend(rows: list[dict], as_of: int, profile: dict) -> dict:
    pivots = confirmed_pivots(rows, as_of, profile)
    highs = [p for p in pivots if p["kind"] == "HIGH"][-2:]
    lows = [p for p in pivots if p["kind"] == "LOW"][-2:]
    trend = "unknown"
    if len(highs) == len(lows) == 2:
        trend = "neutral"
        if highs[-1]["price"] > highs[0]["price"] and lows[-1]["price"] > lows[0]["price"]:
            trend = "bullish"
        elif highs[-1]["price"] < highs[0]["price"] and lows[-1]["price"] < lows[0]["price"]:
            trend = "bearish"
    return {"trend": trend, "highs": highs, "lows": lows}


def _volume_ratio(rows: list[dict], profile: dict) -> float | None:
    count = profile["volume_window"]
    previous = rows[-count-1:-1]
    if len(previous) != count:
        return None
    denominator = median(r["data"]["volume"] for r in previous)
    return rows[-1]["data"]["volume"] / denominator if denominator > 0 else None


def _return(rows: list[dict], end: int) -> float | None:
    found = next((r for r in rows if r["period_end"] == end), None)
    if found is None:
        return None
    data = found["data"]
    return 100 * (data["close"] / data["open"] - 1)


def _strong_break(rows: list[dict], side: str, profile: dict) -> dict:
    if len(rows) < profile["volume_window"]+1:
        return {"confirmed": False}
    trigger = rows[-1]
    pivots = confirmed_pivots(rows[:-1], trigger["period_start"], profile)
    candidates = [p for p in pivots if p["kind"] == ("HIGH" if side == "LONG" else "LOW")]
    if not candidates:
        return {"confirmed": False}
    pivot = candidates[-1]
    data = trigger["data"]
    volume = _volume_ratio(rows, profile)
    buy, total = data.get("taker_buy_volume"), data["volume"]
    ratio = buy/(total-buy) if buy is not None and total > buy >= 0 else None
    crossed = data["open"] <= pivot["price"] < data["close"] if side == "LONG" else data["open"] >= pivot["price"] > data["close"]
    intermediate = [r for r in rows[:-1] if r["period_start"] >= pivot["confirmed_at"]]
    consumed = any(r["data"]["close"] > pivot["price"] if side == "LONG" else r["data"]["close"] < pivot["price"] for r in intermediate)
    flow = ratio is not None and (ratio >= profile["long_taker_min"] if side == "LONG" else ratio <= profile["short_taker_max"])
    return {"confirmed": bool(crossed and not consumed and flow and volume is not None and volume >= profile["regime_breakout_volume_ratio"]),
            "pivot": pivot, "event_time": trigger["event_time"], "volume_ratio": volume,
            "taker_ratio": ratio, "flow_source": "closed_15m_kline_taker_buy_volume"}


def market_regime(benchmarks: dict[str, dict], as_of: int, profile: dict) -> dict:
    evidence = {}
    for symbol in profile["benchmarks"]:
        snapshot = benchmarks.get(symbol)
        if snapshot is None:
            evidence[symbol] = {"trend": "unknown", "reason": "DATA_INSUFFICIENT"}
            continue
        visible = visible_snapshot(snapshot, as_of)
        # Regime uses 15m/1h structure and their own kline order flow. Unused
        # quote/1m freshness must not turn valid benchmark trends into UNKNOWN.
        regime_profile = {**profile, "required_records": [],
                          "intervals_ms": {key: profile["intervals_ms"][key] for key in ("15m", "1h")}}
        failures = validate_snapshot(visible, as_of, regime_profile)
        if failures:
            evidence[symbol] = {"trend": "unknown", "reasons": failures}
            continue
        bars = visible["bars"]
        trends = {tf: _trend(_completed(bars.get(tf, []), as_of), as_of, profile) for tf in ("15m", "1h")}
        votes = [item["trend"] for item in trends.values()]
        trend = votes[0] if votes.count(votes[0]) == 2 else ("unknown" if "unknown" in votes else "neutral")
        returns = {tf: {"period_end": as_of // profile["intervals_ms"][tf] * profile["intervals_ms"][tf],
                        "return_pct": _return(bars.get(tf, []), as_of // profile["intervals_ms"][tf] * profile["intervals_ms"][tf])}
                   for tf in ("5m", "15m", "1h")}
        evidence[symbol] = {"trend": trend, "timeframes": trends, "returns": returns,
                            "strong_long": _strong_break(bars.get("15m", []), "LONG", profile),
                            "strong_short": _strong_break(bars.get("15m", []), "SHORT", profile)}
    votes = [e["trend"] for e in evidence.values()]
    state = "RISK_ON" if votes.count("bullish") >= 2 else "RISK_OFF" if votes.count("bearish") >= 2 else "UNKNOWN" if votes.count("unknown") >= 2 else "NEUTRAL"
    if state in {"RISK_ON", "RISK_OFF"}:
        field = "strong_long" if state == "RISK_ON" else "strong_short"
        eligible = [s for s,e in evidence.items() if e.get(field, {}).get("confirmed")]
        if "BTCUSDT" in eligible and len(eligible) >= 2:
            state = "STRONG_" + state
    return {"state": state, "as_of": as_of, "evidence": evidence, "strategy_version": profile["strategy_version"]}


def assess_entry(side: str, entry: float, stop: float, targets: list[float], profile: dict) -> dict:
    """Entry E already contains entry slippage; add exit slippage/fees/funding."""
    reasons = []
    out = {"ok": False, "reasons": reasons, "net_rr": None, "risk_per_unit": None,
           "net_reward": None, "targets": list(targets), "costs": {}}
    if side not in {"LONG", "SHORT"}:
        reasons.append("SCHEMA_CONFLICT:side")
    if not targets:
        reasons.append("TARGET_UNVERIFIABLE")
    if not all(finite_positive(v) for v in [entry, stop, *targets]):
        reasons.append("SCHEMA_CONFLICT:price")
    costs = [profile["fee_rate"], profile["slippage_bps"], profile["funding_reserve_rate"]]
    if not all(isinstance(v, (int,float)) and math.isfinite(v) and v >= 0 for v in costs):
        reasons.append("SCHEMA_CONFLICT:cost")
    if reasons:
        return out
    direction = 1 if side == "LONG" else -1
    if direction*(entry-stop) <= 0:
        reasons.append("SCHEMA_CONFLICT:stop_direction")
    distances = [direction*(t-entry) for t in targets]
    if any(v <= 0 for v in distances):
        reasons.append("SCHEMA_CONFLICT:target_direction")
    if any(b <= a for a,b in zip(distances, distances[1:])):
        reasons.append("SCHEMA_CONFLICT:target_order")
    if reasons:
        return out
    fee, slip, funding = profile["fee_rate"], profile["slippage_bps"]/10000, profile["funding_reserve_rate"]
    stop_cost = (entry+stop)*fee + stop*slip + entry*funding
    target_costs = [(entry+t)*fee + t*slip + entry*funding for t in targets]
    risk = abs(entry-stop) + stop_cost
    rewards = [distance-cost for distance,cost in zip(distances,target_costs)]
    rr = rewards[0]/risk
    if rr < profile["min_net_rr"]:
        reasons.append("RR_INSUFFICIENT")
    out.update(ok=not reasons, net_rr=rr, risk_per_unit=risk, net_reward=rewards[0],
               net_rewards=rewards, net_rrs=[reward/risk for reward in rewards],
               costs={"stop": stop_cost, "targets": target_costs, "target": target_costs[0], "entry_slippage_already_in_entry": True})
    return out


def _atr(rows: list[dict], profile: dict) -> float | None:
    result = wilder_atr([r["data"] for r in rows], profile["atr_period"])
    return result[-1] if result else None


def _targets(snapshot: dict, side: str, level: float, frozen_at: int, profile: dict) -> tuple[list, list]:
    kind, direction = ("LOW", -1) if side == "SHORT" else ("HIGH", 1)
    candidates = []
    for timeframe in ("5m", "15m", "1h"):
        rows = _completed(snapshot["bars"].get(timeframe, []), frozen_at)
        for pivot in confirmed_pivots(rows, frozen_at, profile):
            if pivot["kind"] != kind or direction*(pivot["price"]-level) <= 0:
                continue
            violated = any(r["data"]["low"] < pivot["price"] if side == "SHORT" else r["data"]["high"] > pivot["price"] for r in rows[pivot["index"]+1:])
            if not violated:
                candidates.append({**pivot, "timeframe": timeframe, "source": "confirmed_pivot"})
    candidates.sort(key=lambda p: direction*(p["price"]-level))
    unique = []
    for item in candidates:
        if item["price"] not in [p["price"] for p in unique]:
            unique.append(item)
    return [p["price"] for p in unique[:3]], unique[:3]


def _freeze(snapshot: dict, rows: list[dict], side: str, profile: dict, previous: list[dict], include_current: bool = False) -> dict | None:
    before = rows if include_current else rows[:-1]
    count = profile["base_min_bars"]
    base = before[-count:]
    atr = _atr(before, profile)
    if len(base) < count or not atr or atr <= 0:
        return None
    highs, lows = [r["data"]["high"] for r in base], [r["data"]["low"] for r in base]
    bodies = [r["data"]["close"]-r["data"]["open"] for r in base]
    if max(lows) > min(highs) or max(highs)-min(lows) > profile["base_max_atr"]*atr:
        return None
    if any(not max(lows) <= r["data"]["close"] <= min(highs) for r in base):
        return None
    if all(b < 0 for b in bodies) or all(b > 0 for b in bodies):
        return None
    same_side = [s for s in previous if s.get("symbol") == snapshot["symbol"] and s.get("side", s.get("direction")) == side]
    if same_side and base[0]["period_start"] < max(s.get("trigger_end", s["base_end"]) for s in same_side):
        return None
    frozen_at = max(max(r["available_at"],_received(r),r["period_end"]) for r in base)
    if not include_current and frozen_at > rows[-1]["period_start"]:
        return None
    low, high = min(lows), max(highs)
    level = low if side == "SHORT" else high
    targets, sources = _targets(snapshot, side, level, frozen_at, profile)
    identifier = digest({"symbol": snapshot["symbol"], "side": side, "base_start": base[0]["period_start"],
                         "base_end": base[-1]["period_end"], "profile": digest(profile)})[:24]
    return {"setup_id": identifier, "symbol": snapshot["symbol"], "side": side, "direction": side,
            "frozen_at": frozen_at, "armed_at": frozen_at, "expires_at": frozen_at+profile["renewed_ttl_ms" if same_side else "armed_ttl_ms"],
            "base_start": base[0]["period_start"], "base_end": base[-1]["period_end"],
            "base_high": high, "base_low": low, "breakout_level": level,
            "stop": high+atr*profile["stop_buffer_atr"] if side == "SHORT" else low-atr*profile["stop_buffer_atr"],
            "stop_source": {"source": "frozen_base_extreme", "base_start": base[0]["period_start"], "base_end": base[-1]["period_end"], "buffer_atr": profile["stop_buffer_atr"]},
            "targets": targets, "target_sources": sources, "atr": atr, "signal_state": "ARMED_"+side,
            "position_state": "NO_POSITION_INFORMATION", "stage": None, "emitted_stages": [],
            "structure_kind": "SECOND_LEG" if same_side else "BASE", "raw_refs": [r["raw_ref"] for r in base]}


def _flow(snapshot: dict, end: int, side: str, profile: dict) -> tuple[list,dict]:
    reasons = []
    records = snapshot["records"]
    takers = {r.get("period_end"): r["data"].get("ratio") for r in records.get("taker", [])
              if r.get("period_start") == r.get("period_end", 0)-300_000}
    current, previous = takers.get(end), takers.get(end-300_000)
    if side == "SHORT":
        okay = current is not None and (current <= profile["short_taker_max"] or (previous is not None and current < profile["short_two_taker_max"] and previous < profile["short_two_taker_max"]))
        horizons, minimum = (300_000,600_000), profile["short_oi_min_pct"]
    else:
        okay = current is not None and (current >= profile["long_taker_min"] or (previous is not None and current > profile["long_two_taker_min"] and previous > profile["long_two_taker_min"]))
        horizons, minimum = (300_000,900_000), profile["long_oi_min_pct"]
    if not okay:
        reasons.append("DATA_INSUFFICIENT:taker_confirmation")
    quantities = {r.get("period_end"): r["data"].get("quantity") for r in records.get("oi", [])}
    changes = {}
    for horizon in horizons:
        old, new = quantities.get(end-horizon), quantities.get(end)
        if not finite_positive(old) or not finite_positive(new):
            reasons.append("DATA_INSUFFICIENT:oi_alignment_"+str(horizon))
        else:
            changes[str(horizon)] = 100*(new/old-1)
            if changes[str(horizon)] < minimum:
                reasons.append("OI_DELEVERAGING_LIMIT")
    return reasons, {"taker_ratio": current, "previous_taker_ratio": previous, "oi_quantity_changes_pct": changes,
                     "oi_interpretation": "contract_quantity_change_only", "period_end": end}


def _regime_eligibility(snapshot: dict, trigger: dict, side: str, regime: dict, flow: dict, volume: float | None, profile: dict) -> tuple[list,bool,dict]:
    state = regime.get("state", "UNKNOWN")
    relative = {}
    for tf in ("5m", "15m", "1h"):
        benchmark = regime.get("evidence", {}).get("BTCUSDT", {}).get("returns", {}).get(tf, {})
        end, btc = benchmark.get("period_end"), benchmark.get("return_pct")
        candidate = _return(snapshot["bars"].get(tf, []), end) if end is not None else None
        relative[tf] = {"period_end": end, "difference_pp": candidate-btc if candidate is not None and btc is not None else None}
    if state == "UNKNOWN":
        return ["REGIME_CONFLICT:UNKNOWN"], False, relative
    if side == "LONG" and state == "STRONG_RISK_OFF":
        return ["REGIME_CONFLICT"], False, relative
    if side == "SHORT" and state == "STRONG_RISK_ON":
        btc = regime.get("evidence", {}).get("BTCUSDT", {}).get("returns", {}).get("5m", {})
        diff = relative["5m"]["difference_pp"]
        complete = (btc.get("period_end") == trigger["period_end"] and btc.get("return_pct") is not None
                    and btc["return_pct"] >= 0 and diff is not None and diff <= profile["counter_relative_weakness_pp"]
                    and volume is not None and volume >= 2 and flow["taker_ratio"] is not None
                    and flow["taker_ratio"] <= profile["counter_short_taker_max"])
        return ([] if complete else ["REGIME_CONFLICT"]), bool(complete), relative
    return [], False, relative


def _attempt(snapshot: dict, rows: list[dict], trigger: dict, setup: dict, stage: str, as_of: int, profile: dict, regime: dict) -> tuple[dict | None,list]:
    side = setup["side"]
    reasons, flow = _flow(snapshot, trigger["period_end"], side, profile)
    volume = _volume_ratio(rows, profile)
    threshold = (profile["short_volume_min"] if side == "SHORT" else profile["long_volume_min"]) if stage in {"B0","C0"} else profile["confirmation_volume_ratio"]
    if volume is None or volume < threshold:
        reasons.append("VOLUME_UNCONFIRMED")
    conflicts, counter, relative = _regime_eligibility(snapshot, trigger, side, regime, flow, volume, profile)
    reasons.extend(conflicts)
    if regime.get("as_of") != as_of or regime.get("strategy_version", profile["strategy_version"]) != profile["strategy_version"]:
        reasons.append("REGIME_CONFLICT:time_or_version")
    if as_of-trigger["period_end"] > profile["entry_ttl_ms"]:
        reasons.append("LATE_DETECTION")
    quote = snapshot["records"]["quote"]
    slip = profile["slippage_bps"]/10000
    entry = quote["data"]["ask"]*(1+slip) if side == "LONG" else quote["data"]["bid"]*(1-slip)
    drift = abs(entry-setup["breakout_level"])/setup["atr"]
    drift_limit = profile["short_drift_atr"] if side == "SHORT" else profile["long_drift_atr"]
    if drift > drift_limit:
        reasons.append("LATE_ENTRY")
    if (entry >= setup["breakout_level"] if side == "SHORT" else entry <= setup["breakout_level"]):
        reasons.append("INVALIDATED:executable_price_reclaimed")
    assessment = assess_entry(side, entry, setup["stop"], setup["targets"], profile)
    reasons.extend(assessment["reasons"])
    setup["last_assessment"] = {"decision_time": as_of, "stage": stage, "reasons": sorted(set(reasons)), "volume_ratio": volume, "flow": flow, "relative_strength": relative, "entry_drift_atr": drift, "assessment": assessment}
    if reasons:
        setup["signal_state"] = "VALID_EVENT_NO_ENTRY"
        return None, reasons
    raw = [r for values in snapshot["bars"].values() for r in values]
    for value in snapshot["records"].values():
        raw.extend(value if isinstance(value,list) else [value])
    signal = {"signal_id": digest({"setup_id": setup["setup_id"], "stage": stage, "event": trigger["period_end"]})[:24],
              "setup_id": setup["setup_id"], "symbol": snapshot["symbol"], "side": side, "stage": stage,
              "counter_regime": counter or setup.get("counter_regime",False), "decision_time": as_of,
              "event_time": trigger["event_time"], "received_at": max(_received(r) for r in raw),
              "download_received_at": max(r["received_at"] for r in raw),
              "availability_estimated": any(r.get("availability_estimated") is True for r in raw),
              "available_at": max(r["available_at"] for r in raw), "entry": entry, "stop": setup["stop"],
              "targets": list(setup["targets"]), "stop_source": copy.deepcopy(setup["stop_source"]),
              "target_sources": copy.deepcopy(setup["target_sources"]), "atr": setup["atr"],
              "breakout_level": setup["breakout_level"], "net_rr": assessment["net_rr"], "risk_per_unit": assessment["risk_per_unit"],
              "invalidation_level": setup["breakout_level"],
              "regime": copy.deepcopy(regime), "raw_refs": sorted({r["raw_ref"] for r in raw}),
              "data_hash": digest(snapshot), "strategy_version": profile["strategy_version"], "profile_hash": digest(profile),
              "hard_stop_trigger": "CONTRACT_PRICE", "frozen_structure": {k: copy.deepcopy(setup[k]) for k in ("frozen_at","base_start","base_end","base_high","base_low","structure_kind")},
              "flow": flow, "relative_strength": relative, "volume_ratio": volume}
    setup["counter_regime"] = signal["counter_regime"]
    setup["stage"] = stage
    setup["emitted_stages"].append(stage)
    setup["signal_state"] = "SIGNAL_READY"
    return signal, []


def _process(snapshot: dict, rows: list[dict], setup: dict, as_of: int, profile: dict, regime: dict) -> tuple[list,list]:
    signals, rejections = [], []
    if setup.get("signal_state") in {"EXPIRED","INVALIDATED"}:
        return signals,rejections
    if as_of > setup["expires_at"]:
        setup["signal_state"] = "EXPIRED"
        return signals,["EXPIRED"]
    side, level = setup["side"], setup["breakout_level"]
    for i, row in enumerate(rows):
        end, data = row["period_end"], row["data"]
        if end <= setup.get("last_processed_event",setup["base_end"]):
            continue
        setup["last_processed_event"] = end
        broken = data["close"] < level if side == "SHORT" else data["close"] > level
        if not setup.get("trigger_end"):
            crossed = data["open"] >= level > data["close"] if side == "SHORT" else data["open"] <= level < data["close"]
            if crossed:
                setup.update(trigger_end=end, trigger_close=data["close"], trigger_low=data["low"], trigger_high=data["high"])
                stage = "B0" if side == "SHORT" else "C0"
                signal,reasons = _attempt(snapshot, rows[:i+1], row, setup, stage, as_of, profile, regime)
                rejections.extend(reasons)
                if signal:
                    signals.append(signal)
            elif broken:
                setup["signal_state"] = "EXPIRED"
                rejections.append("LATE_DETECTION:unobserved_first_break")
                break
            elif side == "LONG":
                setup["signal_state"] = "PRE_C0"
            continue
        if not broken:
            setup["signal_state"] = "INVALIDATED"
            setup["invalidated_at"] = end
            setup["invalidation_reason"] = "5m_close_reclaimed_structure_early_exit_only"
            rejections.append("INVALIDATED")
            break
        # Retest and its confirmation must occupy distinct completed bars.
        touches = data["high"] >= level if side == "SHORT" else data["low"] <= level
        if not setup.get("retest_end") and touches and end > setup["trigger_end"]:
            setup.update(retest_end=end,retest_low=data["low"],retest_high=data["high"])
            continue
        if setup.get("retest_end") and end > setup["retest_end"] and "A" not in setup["emitted_stages"]:
            confirms = data["close"] < setup["retest_low"] if side == "SHORT" else data["close"] > setup["retest_high"]
            if confirms:
                signal,reasons = _attempt(snapshot, rows[:i+1],row,setup,"A",as_of,profile,regime)
                rejections.extend(reasons)
                if signal:
                    signals.append(signal)
                continue
        first, second = ("B0","B1") if side == "SHORT" else ("C0","C1")
        if first not in setup["emitted_stages"] or second in setup["emitted_stages"] or "A" in setup["emitted_stages"]:
            continue
        previous = rows[i-1]["data"] if i else {}
        paused = previous and (previous["close"] >= previous["open"] if side == "SHORT" else previous["close"] <= previous["open"])
        confirms = data["close"] < setup["trigger_low"] and data["close"] < data["open"] if side == "SHORT" else data["close"] > setup["trigger_high"] and data["close"] > data["open"]
        if end > setup["trigger_end"]+300_000 and paused and confirms:
            signal,reasons = _attempt(snapshot, rows[:i+1],row,setup,second,as_of,profile,regime)
            rejections.extend(reasons)
            if signal:
                signals.append(signal)
    return signals,rejections


def evaluate(snapshot: dict, as_of: int, profile: dict, regime: dict, setups: list[dict]) -> dict:
    """Failed inputs cannot advance state. Signal state makes no claim of a fill."""
    retained = copy.deepcopy(setups)
    visible = visible_snapshot(snapshot,as_of)
    reasons = validate_snapshot(visible,as_of,profile,for_entry=True)
    if reasons:
        return {"setups":retained,"signals":[],"rejections":reasons,"status":reasons[0].split(":")[0]}
    rows = _completed(visible["bars"].get("5m",[]),as_of)
    if len(rows) < profile["volume_window"]+1:
        return {"setups":retained,"signals":[],"rejections":["DATA_INSUFFICIENT:structure"],"status":"DATA_INSUFFICIENT"}
    signals = []
    for setup in retained:
        if setup.get("symbol") == visible["symbol"]:
            new,rejected = _process(visible,rows,setup,as_of,profile,regime)
            signals.extend(new)
            reasons.extend(rejected)
    for side in ("SHORT","LONG"):
        active = [s for s in retained if s.get("symbol") == visible["symbol"] and s.get("side") == side and s.get("signal_state") not in {"EXPIRED","INVALIDATED"}]
        if active and not any(s.get("trigger_end") for s in active):
            continue
        new_setup = _freeze(visible,rows,side,profile,retained)
        if new_setup is None:
            new_setup = _freeze(visible,rows,side,profile,retained,include_current=True)
        if new_setup is not None:
            for old in active:
                old["signal_state"] = "EXPIRED"
                old["expiry_reason"] = "NEW_INDEPENDENT_BASE"
                old["superseded_by"] = new_setup["setup_id"]
            retained.append(new_setup)
            new,rejected = _process(visible,rows,new_setup,as_of,profile,regime)
            signals.extend(new)
            reasons.extend(rejected)
    status = "SIGNAL_READY" if signals else reasons[0].split(":")[0] if reasons else "WATCH" if retained else "NO_SETUP"
    return {"setups":retained,"signals":signals,"rejections":sorted(set(reasons)),"status":status}
