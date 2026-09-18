"""Offline, JSON-persistent research execution. No production or network imports.

Market fills use displayed bid/ask plus configured adverse slippage. Historical
bar volume caps participation as an explicitly estimated fill feasibility model;
it is never strategy evidence. Intrabar exits reconstruct a bid/ask envelope
using the observed spread, require liquidity, and choose SL before TP.
"""
from __future__ import annotations

import copy
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from auto_trading.contracts import digest, finite_positive
from auto_trading.data import validate_snapshot
from auto_trading.strategy import assess_entry

_WORKING = {"WORKING", "PARTIALLY_FILLED"}
_DAY_MS = 86_400_000


def _round(value: float, step: float, up: bool = False) -> float:
    quotient = Decimal(str(value)) / Decimal(str(step))
    return float(quotient.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * Decimal(str(step)))


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Simulator:
    """All clocks, market evidence, and initial virtual equity are explicit."""

    def __init__(self, profile: dict, state: dict | None = None):
        self.profile = copy.deepcopy(profile)
        if not finite_positive(profile.get("initial_equity")):
            raise ValueError("initial_equity must be explicit positive virtual equity")
        self.state = copy.deepcopy(state) if state is not None else {
            "equity": float(profile["initial_equity"]), "positions": {}, "orders": {},
            "seen_signals": [], "marks": {}, "day": {}, "audit": [],
            "setup_risk": {}, "processed_bars": {}, "funding_seen": [], "closed_setups": [], "inventory": [],
        }
        for key, default in (("setup_risk", {}), ("processed_bars", {}), ("funding_seen", []),
                             ("closed_setups", []), ("audit", []), ("marks", {}), ("inventory", [])):
            self.state.setdefault(key, default)
        self.state["production_allowed"] = False
        digest(self.state)  # Refuse corrupt/non-JSON numeric state at restart.

    def _event(self, status: str, as_of: int, **fields) -> dict:
        event = {"status": status, "as_of": as_of, "strategy_version": self.profile["strategy_version"],
                 "profile_hash": digest(self.profile), "research_experiment": True,
                 "production_allowed": False, **fields}
        self.state["audit"].append(event)
        return event

    def account_equity(self) -> float:
        total = self.state["equity"]
        for symbol, position in self.state["positions"].items():
            mark = self.state["marks"].get(symbol)
            if not mark:
                continue
            price = mark["bid"] if position["side"] == "LONG" else mark["ask"]
            direction = 1 if position["side"] == "LONG" else -1
            price *= 1 - direction * self.profile["slippage_bps"] / 10000
            total += direction * (price - position["entry"]) * position["qty"]
            total -= price * position["qty"] * self.profile["fee_rate"]
        return total

    def _day(self, as_of: int) -> bool:
        day = self.state["day"]
        number = as_of // _DAY_MS
        equity = self.account_equity()
        if not day:
            day.update({"date": number, "start_equity": equity, "halted": False})
        elif number > day["date"]:
            # At a missed midnight retain the last known equity; never reset a
            # loss by using a newly received adverse quote as day-start equity.
            day.update({"date": number, "start_equity": day.get("last_equity", equity), "halted": False})
        day["net_pnl"] = equity - day["start_equity"]
        day["loss"] = max(0.0, -day["net_pnl"])
        day["unrealized"] = equity - self.state["equity"]
        day["last_equity"] = equity
        if day["loss"] >= day["start_equity"] * self.profile["daily_loss_fraction"]:
            day["halted"] = True
        return day["halted"]

    def mark(self, symbol: str, bid: float, ask: float, as_of: int) -> None:
        if not finite_positive(bid) or not finite_positive(ask) or bid > ask:
            raise ValueError("invalid executable mark")
        self._day(as_of)
        previous = self.state["marks"].get(symbol)
        if previous and previous["as_of"] > as_of:
            return
        self.state["marks"][symbol] = {"bid": bid, "ask": ask, "as_of": as_of}
        self._day(as_of)

    def _quote_ok(self, quote: dict | None, at: int, symbol: str) -> bool:
        if not isinstance(quote, dict) or quote.get("symbol") != symbol:
            return False
        q = quote.get("data", {})
        if not finite_positive(q.get("bid")) or not finite_positive(q.get("ask")) or q["bid"] > q["ask"]:
            return False
        return all(isinstance(quote.get(key), int) and quote[key] <= at
                   for key in ("event_time", "received_at", "available_at")) and (
                   quote["event_time"] <= quote["received_at"] <= quote["available_at"]
                   and 0 <= at - quote["event_time"] <= self.profile["quote_max_age_ms"])

    def _entry(self, side: str, quote: dict, metadata: dict, opening: float | None = None) -> float:
        q = quote["data"]
        price = q["ask"] if side == "LONG" else q["bid"]
        if opening is not None:
            price = max(price, opening) if side == "LONG" else min(price, opening)
        direction = 1 if side == "LONG" else -1
        return _round(price * (1 + direction * self.profile["slippage_bps"] / 10000),
                      metadata["tick_size"], up=side == "LONG")

    def _entry_check(self, signal: dict, price: float, quote: dict, metadata: dict) -> dict:
        result = assess_entry(signal["side"], price, signal["stop"], signal["targets"], self.profile)
        if result["risk_per_unit"] is not None:
            spread = quote["data"]["ask"] - quote["data"]["bid"]
            envelope = (spread + metadata["tick_size"]) * (1 + self.profile["fee_rate"] + self.profile["slippage_bps"] / 10000)
            result["risk_per_unit"] += envelope
            result["net_reward"] -= envelope
            result["net_rr"] = result["net_reward"] / result["risk_per_unit"]
            result["net_rewards"] = [value - envelope for value in result.get("net_rewards", [result["net_reward"] + envelope])]
            result["net_rrs"] = [value / result["risk_per_unit"] for value in result["net_rewards"]]
            result["costs"]["exit_quote_envelope"] = envelope
            if result["net_rr"] < self.profile["min_net_rr"] and "RR_INSUFFICIENT" not in result["reasons"]:
                result["reasons"].append("RR_INSUFFICIENT")
                result["ok"] = False
        limit = self.profile["long_drift_atr"] if signal["side"] == "LONG" else self.profile["short_drift_atr"]
        if abs(price - signal["breakout_level"]) > signal["atr"] * limit:
            result = {**result, "ok": False, "reasons": [*result["reasons"], "LATE_ENTRY:entry_drift"]}
        return result

    def _active(self) -> list[dict]:
        return [o for o in self.state["orders"].values() if o["status"] in _WORKING]

    def _add_reason(self, position: dict, signal: dict, quote: dict) -> str | None:
        if position["side"] != signal["side"] or position["setup_id"] != signal["setup_id"]:
            return "SINGLE_SYMBOL_POSITION"
        if position["counter_regime"]:
            return "COUNTER_RISK_FROZEN"
        if position["tp_done"] or position.get("exit_pending"):
            return "POSITION_EXITING"
        direction = 1 if signal["side"] == "LONG" else -1
        if direction * (signal["stop"] - position["stop"]) < -1e-10:
            return "STOP_WIDENING"
        price = quote["data"]["bid"] if direction == 1 else quote["data"]["ask"]
        net = direction * (price - position["entry"]) - (price + position["entry"]) * self.profile["fee_rate"]
        if net <= 0:
            return "LOSS_ADD_FORBIDDEN"
        if self.profile["stage_risk"][signal["stage"]] <= position["stage_fraction"]:
            return "STAGE_NOT_UPGRADED"
        return None

    def submit(self, signal: dict, snapshot: dict, as_of: int) -> dict:
        sig = copy.deepcopy(signal)
        signal_id = sig.get("signal_id")
        common = {"signal_id": signal_id, "setup_id": sig.get("setup_id"), "symbol": sig.get("symbol")}
        if signal_id in self.state["seen_signals"]:
            return self._event("DUPLICATE_SIGNAL", as_of, reason="DUPLICATE_SIGNAL", **common)
        required = ("signal_id", "setup_id", "symbol", "side", "stage", "decision_time", "event_time",
                    "received_at", "available_at", "entry", "stop", "targets", "atr", "breakout_level")
        if any(key not in sig for key in required) or sig["side"] not in {"LONG", "SHORT"} or sig["stage"] not in self.profile["stage_risk"]:
            return self._event("SCHEMA_CONFLICT", as_of, reason="SIGNAL_SCHEMA", **common)
        if (not all(isinstance(sig[k], int) and sig[k] <= as_of for k in ("decision_time", "event_time", "received_at", "available_at"))
                or not all(finite_positive(sig[k]) for k in ("entry", "stop", "atr", "breakout_level"))
                or not 1 <= len(sig["targets"]) <= 3 or not all(finite_positive(v) for v in sig["targets"])):
            return self._event("SCHEMA_CONFLICT", as_of, reason="SIGNAL_VALUES", **common)
        if sig.get("trigger_type", "CONTRACT_PRICE") != "CONTRACT_PRICE":
            return self._event("DATA_INSUFFICIENT", as_of, reason="MARK_PRICE_HISTORY_NOT_IMPLEMENTED", **common)
        if as_of - sig["event_time"] > self.profile["entry_ttl_ms"]:
            return self._event("LATE_DETECTION", as_of, reason="EXPIRED_SIGNAL", **common)
        errors = validate_snapshot(snapshot, as_of, self.profile, for_entry=True)
        if errors:
            return self._event(errors[0].split(":")[0], as_of, reason=errors[0], reasons=errors, **common)
        quote = snapshot.get("records", {}).get("quote")
        if not self._quote_ok(quote, as_of, sig["symbol"]):
            return self._event("DATA_STALE", as_of, reason="EXECUTION_QUOTE", **common)
        metadata = copy.deepcopy(snapshot.get("metadata", {}))
        if not all(finite_positive(metadata.get(k)) for k in ("tick_size", "step_size", "min_qty", "min_notional")):
            return self._event("SCHEMA_CONFLICT", as_of, reason="EXCHANGE_FILTERS", **common)
        # Round a protective stop away from entry, and each target toward entry;
        # never improve RR using exchange precision rounding.
        sig["stop"] = _round(sig["stop"], metadata["tick_size"], up=sig["side"] == "SHORT")
        sig["targets"] = [_round(x, metadata["tick_size"], up=sig["side"] == "SHORT") for x in sig["targets"]]
        self.mark(sig["symbol"], quote["data"]["bid"], quote["data"]["ask"], as_of)
        if self._day(as_of):
            return self._event("RISK_LIMIT", as_of, reason="DAILY_LOSS_LIMIT", **common)
        if self._active():
            return self._event("RISK_LIMIT", as_of, reason="SINGLE_ENTRY_INTENT", **common)
        position = self.state["positions"].get(sig["symbol"])
        if self.state["positions"] and not position:
            return self._event("RISK_LIMIT", as_of, reason="SINGLE_SYMBOL_POSITION", **common)
        if sig["setup_id"] in self.state["closed_setups"]:
            return self._event("RISK_LIMIT", as_of, reason="SETUP_ALREADY_EXITED", **common)
        if position:
            reason = self._add_reason(position, sig, quote)
            if reason:
                return self._event("RISK_LIMIT", as_of, reason=reason, **common)
        elif sig["stage"] in {"B1", "C1"}:
            return self._event("RISK_LIMIT", as_of, reason="STARTER_NOT_FILLED", **common)
        price = self._entry(sig["side"], quote, metadata)
        assessment = self._entry_check(sig, price, quote, metadata)
        if not assessment["ok"]:
            return self._event(assessment["reasons"][0].split(":")[0], as_of,
                               reason=assessment["reasons"][0], assessment=assessment, **common)
        equity = sig.get("setup_equity", self.account_equity())
        if not finite_positive(equity):
            return self._event("RISK_LIMIT", as_of, reason="INVALID_SETUP_EQUITY", **common)
        unit = self.state["setup_risk"].setdefault(sig["setup_id"], equity * self.profile["account_risk_fraction"])
        counter = bool(sig.get("counter_regime")) or bool(position and position["counter_regime"])
        fraction = self.profile["counter_regime_risk"] if counter else self.profile["stage_risk"][sig["stage"]]
        total_budget = unit * fraction
        budget = total_budget - (position["risk_used"] if position else 0)
        existing_notional = position["qty"] * price if position else 0
        qty = _round(min(max(0, budget) / assessment["risk_per_unit"],
                         max(0, self.account_equity() * self.profile["max_leverage"] - existing_notional) / price), metadata["step_size"])
        if qty < metadata["min_qty"] or qty * price < metadata["min_notional"]:
            return self._event("RISK_LIMIT", as_of, reason="RISK_OR_EXCHANGE_MINIMUM", **common)
        order_id = digest({"signal_id": signal_id, "setup_id": sig["setup_id"]})[:24]
        self.state["orders"][order_id] = {
            "order_id": order_id, "signal": sig, "symbol": sig["symbol"], "side": sig["side"],
            "qty": qty, "remaining_qty": qty, "filled_qty": 0.0, "status": "WORKING",
            "created_at": as_of, "eligible_after": max(as_of, sig["decision_time"], sig["available_at"], sig["received_at"]),
            "expires_at": sig["event_time"] + self.profile["entry_ttl_ms"], "metadata": metadata,
            "risk_unit": unit, "risk_budget": total_budget, "stage_fraction": fraction,
            "counter_regime": counter, "assessment": assessment,
        }
        self.state["seen_signals"].append(signal_id)
        return self._event("VIRTUAL_ORDER_WORKING", as_of, order_id=order_id, qty=qty,
                           risk_budget=total_budget, entry=price, assessment=assessment, **common)

    def cancel(self, order_id: str, as_of: int) -> dict:
        order = self.state["orders"].get(order_id)
        if not order:
            return self._event("NOT_FOUND", as_of, order_id=order_id)
        if order["status"] in _WORKING:
            order["status"] = "CANCELLED"
            order["cancelled_at"] = as_of
        return self._event(order["status"], as_of, order_id=order_id)

    def _liquidity(self, quote: dict, side: str, bar: dict | None = None) -> float:
        q = quote["data"]
        quantity = q.get("ask_qty" if side == "BUY" else "bid_qty")
        if quantity is None:
            levels = q.get("asks" if side == "BUY" else "bids", [])
            quantity = levels[0][1] if levels else 0
        if not finite_positive(quantity):
            return 0.0
        if bar is not None:
            quantity = min(quantity, bar["data"]["volume"] * self.profile["max_participation"])
        return quantity

    def _fill(self, order: dict, bar: dict, quote: dict, as_of: int) -> dict:
        signal = order["signal"]
        symbol = order["symbol"]
        position = self.state["positions"].get(symbol)
        price = self._entry(order["side"], quote, order["metadata"], bar["data"]["open"])
        assessment = self._entry_check(signal, price, quote, order["metadata"])
        if not assessment["ok"]:
            order["status"] = "CANCELLED"
            return self._event(assessment["reasons"][0].split(":")[0], as_of,
                               reason=assessment["reasons"][0], order_id=order["order_id"], assessment=assessment)
        if position and order["filled_qty"] == 0:
            reason = self._add_reason(position, signal, quote)
            if reason:
                order["status"] = "CANCELLED"
                return self._event("RISK_LIMIT", as_of, reason=reason, order_id=order["order_id"])
        if position and order["filled_qty"] > 0:
            liquidation = quote["data"]["bid"] if position["side"] == "LONG" else quote["data"]["ask"]
            direction = 1 if position["side"] == "LONG" else -1
            if direction * (liquidation - position["entry"]) <= 0:
                return self._event("UNFILLED", as_of, reason="NO_LOSS_COMPLETION", order_id=order["order_id"])
        budget = order["risk_budget"] - (position["risk_used"] if position else 0)
        notional = position["qty"] * price if position else 0
        qty = min(order["remaining_qty"], max(0, budget) / assessment["risk_per_unit"],
                  self._liquidity(quote, "BUY" if order["side"] == "LONG" else "SELL", bar),
                  max(0, self.account_equity() * self.profile["max_leverage"] - notional) / price)
        qty = _round(qty, order["metadata"]["step_size"])
        if qty < order["metadata"]["min_qty"] or qty * price < order["metadata"]["min_notional"]:
            return self._event("UNFILLED", as_of, reason="LIQUIDITY_OR_MINIMUM", order_id=order["order_id"])
        fee = qty * price * self.profile["fee_rate"]
        self.state["equity"] -= fee
        if position is None:
            position = {"position_id": digest({"setup": signal["setup_id"], "first_order": order["order_id"]})[:24],
                        "symbol": symbol, "side": order["side"], "setup_id": signal["setup_id"],
                        "qty": 0.0, "initial_qty": 0.0, "entry": 0.0, "risk_used": 0.0,
                        "risk_unit": order["risk_unit"], "stop": signal["stop"], "targets": signal["targets"],
                        "stop_source": signal.get("stop_source"), "target_sources": signal.get("target_sources"),
                        "opened_at": bar["period_start"], "tp_done": [], "tp_filled": [0.0, 0.0, 0.0], "legs": [], "fees": 0.0,
                        "funding": 0.0, "counter_regime": order["counter_regime"],
                        "stage_fraction": order["stage_fraction"], "trigger_type": "CONTRACT_PRICE",
                        "invalidation_level": signal.get("invalidation_level"), "metadata": order["metadata"],
                        "bars_held": 0, "exit_pending": None}
            self.state["positions"][symbol] = position
        position["entry"] = (position["entry"] * position["qty"] + price * qty) / (position["qty"] + qty)
        position["qty"] += qty
        position["initial_qty"] += qty
        position["risk_used"] += qty * assessment["risk_per_unit"]
        position["fees"] += fee
        position["stage_fraction"] = order["stage_fraction"]
        # A tighter new structural stop may protect the complete position. Its
        # released risk is deliberately not recycled into extra quantity.
        position["stop"] = max(position["stop"], signal["stop"]) if position["side"] == "LONG" else min(position["stop"], signal["stop"])
        position["legs"].append({"order_id": order["order_id"], "signal_id": signal["signal_id"],
                                 "qty": qty, "entry": price, "fill_time": bar["period_start"], "fee": fee,
                                 "risk_per_unit": assessment["risk_per_unit"]})
        order["filled_qty"] += qty
        self.state["inventory"].append({"symbol": symbol, "side": position["side"],
                                        "time": bar["period_start"], "qty_delta": qty,
                                        "trade_id": position["position_id"]})
        order["remaining_qty"] = max(0, order["qty"] - order["filled_qty"])
        order["status"] = "FILLED" if order["remaining_qty"] < order["metadata"]["step_size"] else "PARTIALLY_FILLED"
        return self._event("VIRTUAL_FILLED", as_of, order_id=order["order_id"], signal_id=signal["signal_id"],
                           setup_id=signal["setup_id"], trade_id=position["position_id"], symbol=symbol,
                           qty=qty, price=price, fill_time=bar["period_start"], fee=fee,
                           slippage_bps=self.profile["slippage_bps"], risk_used=position["risk_used"],
                           assessment=assessment, liquidity_estimated=True,
                           data_hash=signal.get("data_hash"), raw_refs=signal.get("raw_refs", []),
                           execution_quote_ref=quote.get("raw_ref"), execution_bar_ref=bar.get("raw_ref"),
                           decision_time=signal["decision_time"], evidence_available_at=signal["available_at"],
                           signal_received_at=signal["received_at"])

    def _exit(self, position: dict, price: float, quantity: float, reason: str, at: int,
              as_of: int, available: float, uncertainty: bool = False) -> dict:
        desired = min(quantity, position["qty"])
        qty = min(desired, available)
        # Exits can always close a residual below initial minNotional; round
        # partial quantities down but retain exact final residual accounting.
        if qty < desired:
            qty = _round(qty, position["metadata"]["step_size"])
        if qty <= 0:
            position["exit_pending"] = reason
            return self._event("PROTECTION_UNCONFIRMED", as_of, symbol=position["symbol"], reason=reason)
        direction = 1 if position["side"] == "LONG" else -1
        price = _round(price * (1 - direction * self.profile["slippage_bps"] / 10000),
                       position["metadata"]["tick_size"], up=direction == -1)
        gross = direction * (price - position["entry"]) * qty
        fee = qty * price * self.profile["fee_rate"]
        self.state["equity"] += gross - fee
        old_qty = position["qty"]
        position["qty"] = max(0.0, old_qty - qty)
        position["risk_used"] *= position["qty"] / old_qty
        position["fees"] += fee
        self.state["inventory"].append({"symbol": position["symbol"], "side": position["side"],
                                        "time": at, "qty_delta": -qty,
                                        "trade_id": position["position_id"]})
        if not reason.startswith("TP") or qty < desired:
            position["exit_pending"] = reason
        for order in self._active():
            if order["symbol"] == position["symbol"]:
                order["status"] = "CANCELLED"
                order["cancelled_at"] = as_of
        event = self._event("VIRTUAL_EXIT", as_of, reason=reason, symbol=position["symbol"],
                            trade_id=position["position_id"], setup_id=position["setup_id"], qty=qty,
                            price=price, fill_time=at, gross_pnl=gross, fee=fee, pnl=gross-fee,
                            trigger_type="CONTRACT_PRICE" if "HARD_STOP" in reason else "STRATEGY_CLOSE" if reason == "INVALIDATED" else "SIMULATED_PRICE",
                            liquidity_estimated=True, recovery_uncertainty=uncertainty)
        if position["qty"] < 1e-10:
            self.state["positions"].pop(position["symbol"], None)
            self.state["closed_setups"].append(position["setup_id"])
        self._day(as_of)
        return event

    def process_bar(self, symbol: str, bar: dict, quote: dict | None, as_of: int) -> list[dict]:
        start, end = bar.get("period_start"), bar.get("period_end")
        data = bar.get("data", {})
        if (bar.get("symbol") != symbol or not isinstance(start, int) or not isinstance(end, int)
                or end-start not in {self.profile["execution_interval_ms"], 300000}
                or start % self.profile["execution_interval_ms"] != 0
                or end > as_of or not all(isinstance(bar.get(k), int) for k in ("event_time", "received_at", "available_at"))
                or not start <= bar["event_time"] < end <= bar["received_at"] <= bar["available_at"] <= as_of
                or not all(finite_positive(data.get(k)) for k in ("open", "high", "low", "close"))
                or not _finite(data.get("volume")) or data["volume"] < 0
                or data["low"] > min(data["open"], data["close"]) or data["high"] < max(data["open"], data["close"])):
            return [self._event("SCHEMA_CONFLICT", as_of, reason="EXECUTION_BAR", symbol=symbol)]
        key = f"{symbol}:{end-start}"
        previous = self.state["processed_bars"].get(key)
        if previous is not None and start <= previous:
            return []
        self.state["processed_bars"][key] = start
        events = []
        self._day(start)
        quote_ok = self._quote_ok(quote, start, symbol)
        if quote_ok:
            self.mark(symbol, quote["data"]["bid"], quote["data"]["ask"], start)
        for order in self._active():
            if order["symbol"] != symbol:
                continue
            if start > order["expires_at"]:
                order["status"] = "EXPIRED"
                events.append(self._event("EXPIRED", as_of, order_id=order["order_id"], reason="ENTRY_TTL"))
            elif self._day(start):
                order["status"] = "CANCELLED"
                events.append(self._event("RISK_LIMIT", as_of, order_id=order["order_id"], reason="DAILY_LOSS_LIMIT"))
            elif end-start == self.profile["execution_interval_ms"] and start > order["eligible_after"]:
                if quote_ok:
                    events.append(self._fill(order, bar, quote, as_of))
                else:
                    events.append(self._event("UNFILLED", as_of, reason="NO_QUOTE_AT_OPEN", order_id=order["order_id"]))
        position = self.state["positions"].get(symbol)
        if position is None or start < position["opened_at"]:
            return events
        if not quote_ok:
            events.append(self._event("PROTECTION_UNCONFIRMED", as_of, symbol=symbol, reason="MISSING_EXECUTION_QUOTE"))
            return events
        direction = 1 if position["side"] == "LONG" else -1
        spread = quote["data"]["ask"] - quote["data"]["bid"]
        liquidity = self._liquidity(quote, "SELL" if direction == 1 else "BUY", bar)
        stop_hit = data["low"] <= position["stop"] if direction == 1 else data["high"] >= position["stop"]
        if stop_hit or position.get("exit_pending") in {"HARD_STOP", "RECOVERY_HARD_STOP"}:
            price = min(data["open"], position["stop"]) - spread if direction == 1 else max(data["open"], position["stop"]) + spread
            gap = direction * (data["open"] - position["stop"]) <= 0
            events.append(self._exit(position, price, position["qty"], "HARD_STOP", start if gap else end, as_of, liquidity))
            return events
        pending = position.get("close_exit")
        if pending and start > pending["decision_time"]:
            price = min(data["open"], quote["data"]["bid"]) if direction == 1 else max(data["open"], quote["data"]["ask"])
            events.append(self._exit(position, price, position["qty"], pending["reason"], start, as_of, liquidity))
            return events
        for index, target in enumerate(position["targets"]):
            if index in position["tp_done"]:
                continue
            hit = data["high"] > target + spread if direction == 1 else data["low"] < target - spread
            if not hit:
                break
            completed = position.setdefault("tp_filled", [0.0, 0.0, 0.0])[index]
            step = position["metadata"]["step_size"]
            allocations = [_round(position["initial_qty"] * fraction, step)
                           for fraction in self.profile["tp_fractions"][:2]]
            allocations.append(position["initial_qty"] - sum(allocations))
            quantity = min(position["qty"], allocations[index] - completed)
            if quantity < step * 1e-8:
                position["tp_done"].append(index)
                continue
            price = target - spread if direction == 1 else target + spread
            event = self._exit(position, price, quantity, f"TP{index+1}", end, as_of, liquidity)
            events.append(event)
            filled = event.get("qty", 0)
            position["tp_filled"][index] += filled
            liquidity -= filled
            if filled + 1e-10 >= quantity:
                position["tp_done"].append(index)
                position["exit_pending"] = None
            else:
                break
            if symbol not in self.state["positions"]:
                return events
        position["bars_held"] = max(position["bars_held"], (end - position["opened_at"]) // 300000)
        level = position.get("invalidation_level")
        invalid = finite_positive(level) and end % 300000 == 0 and direction * (data["close"] - level) < 0
        reason = "INVALIDATED" if invalid else "TIME_STOP" if position["bars_held"] >= self.profile["time_stop_bars"] else None
        if reason:
            # A close-confirmed exit is a working intent; execute on a later
            # bar open rather than claiming the already-observed close fill.
            pending = position.get("close_exit")
            if pending and start > pending["decision_time"]:
                price = min(data["open"], quote["data"]["bid"]) if direction == 1 else max(data["open"], quote["data"]["ask"])
                events.append(self._exit(position, price, position["qty"], pending["reason"], start, as_of, liquidity))
            elif not pending:
                position["close_exit"] = {"reason": reason, "decision_time": max(end, bar["available_at"])}
                events.append(self._event("VIRTUAL_EXIT_WORKING", as_of, reason=reason, symbol=symbol))
        return events

    def recover_quote(self, symbol: str, quote: dict, as_of: int) -> list[dict]:
        position = self.state["positions"].get(symbol)
        if position is None or not self._quote_ok(quote, as_of, symbol):
            return []
        q = quote["data"]
        self.mark(symbol, q["bid"], q["ask"], as_of)
        direction = 1 if position["side"] == "LONG" else -1
        price = q["bid"] if direction == 1 else q["ask"]
        if direction * (price - position["stop"]) > 0 and position.get("exit_pending") not in {"HARD_STOP", "RECOVERY_HARD_STOP"}:
            return []
        return [self._exit(position, price, position["qty"], "RECOVERY_HARD_STOP", as_of, as_of,
                           self._liquidity(quote, "SELL" if direction == 1 else "BUY"), uncertainty=True)]

    def apply_funding(self, funding_record: dict, as_of: int) -> dict:
        data = funding_record.get("data", {})
        symbol = funding_record.get("symbol")
        event_time = funding_record.get("event_time")
        if (data.get("settlement") is not True or not _finite(data.get("rate"))
                or not finite_positive(data.get("mark_price")) or not isinstance(event_time, int)
                or event_time > as_of or funding_record.get("available_at", as_of+1) > as_of):
            return self._event("DATA_INSUFFICIENT", as_of, reason="EXPLICIT_FUNDING_SETTLEMENT_REQUIRED", symbol=symbol)
        key = digest({"symbol": symbol, "event_time": event_time})
        if key in self.state["funding_seen"]:
            return self._event("FUNDING_DUPLICATE", as_of, symbol=symbol)
        self.state["funding_seen"].append(key)
        inventories = {}
        for entry in self.state["inventory"]:
            if entry["symbol"] == symbol and entry["time"] <= event_time:
                signed = 1 if entry["side"] == "LONG" else -1
                inventories[entry["trade_id"]] = inventories.get(entry["trade_id"], 0.0) + signed * entry["qty_delta"]
        signed_qty = sum(inventories.values())
        if abs(signed_qty) < 1e-10:
            return self._event("FUNDING_SKIPPED", as_of, symbol=symbol)
        amount = -data["mark_price"] * signed_qty * data["rate"]
        self.state["equity"] += amount
        position = self.state["positions"].get(symbol)
        if position and position["position_id"] in inventories:
            position["funding"] += -data["mark_price"] * inventories[position["position_id"]] * data["rate"]
        self._day(as_of)
        return self._event("FUNDING_APPLIED", as_of, symbol=symbol, amount=amount,
                           settlement_time=event_time, rate=data["rate"], raw_ref=funding_record.get("raw_ref"))
