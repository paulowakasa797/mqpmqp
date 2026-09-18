from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from auto_trading.contracts import digest, strategy_hash


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


def fast_symbols(state: dict, profile: dict, now: int) -> list[str]:
    armed = {symbol for symbol, setups in state["setups"].items()
             if any(setup.get("expires_at", 0) > now and
                    setup.get("signal_state") not in {"EXPIRED", "INVALIDATED"} for setup in setups)}
    retained = {symbol for symbol in state["candidates"]
                if now - state["candidate_seen_at"].get(symbol, 0) <= profile["armed_ttl_ms"]}
    attempts = state.get("last_snapshot_attempt", {})
    candidates = sorted((retained | armed) - set(profile["benchmarks"]),
                        key=lambda symbol: (symbol not in armed, attempts.get(symbol, 0), symbol))
    return [*profile["benchmarks"], *candidates]


def require_mode(mode: str, profile: dict, report_path: Path | None = None,
                 data_manifest_path: Path | None = None) -> dict:
    # No credentials/config/production modules are consulted by this entrypoint.
    if mode not in {"observe", "replay", "paper"}:
        raise ValueError("LIVE_FORBIDDEN: unsupported mode")
    for switch in ("ALLOW_LIVE", "ENABLE_LIVE_TRADING", "LIVE_TRADING"):
        if os.environ.get(switch, "").lower() in {"true", "1", "yes", "on"}:
            raise ValueError("LIVE_FORBIDDEN: live switch set")
    if profile.get("production_allowed") is not False:
        raise ValueError("LIVE_FORBIDDEN: invalid profile")
    if mode == "paper":
        from auto_trading.research_gate import verify_gate
        gate = verify_gate(report_path, profile, data_manifest_path)
        if not gate.get("allowed"):
            raise ValueError("RESEARCH_GATE_REFUSED: " + ";".join(gate.get("reasons", ["MISSING_REPORT"])))
        return {"mode": mode, "gate": gate, "production_allowed": False}
    return {"mode": mode, "production_allowed": False}


class StateStore:
    """One atomic JSON transaction contains decisions, orders and their audit.

    Unlike the production StateStore, corruption never resets account risk.
    Kernel file locks are released on crashes; no stale PID killing is required.
    """

    def __init__(self, directory: Path, mode: str, profile: dict, data_hash: str = "public_forward"):
        self.directory = Path(directory).resolve()
        self.mode = mode
        self.profile = profile
        self.binding = {"mode": mode, "strategy_hash": strategy_hash(),
                        "strategy_version": profile["strategy_version"],
                        "profile_hash": digest(profile), "data_hash": data_hash}
        self.state: dict = {}
        self.mutex = threading.RLock()
        self._lock_file = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.directory / "instance.lock").open("a+b")
        try:
            if os.fstat(self._lock_file.fileno()).st_size == 0:
                self._lock_file.write(b"0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("INSTANCE_ALREADY_RUNNING") from exc
        try:
            path = self.directory / "state.json"
            if path.exists():
                envelope = json.loads(path.read_text(encoding="utf-8"))
                self.state = envelope["state"]
                if digest(self.state) != envelope["checksum"]:
                    raise ValueError("checksum")
                if self.state["binding"] != self.binding:
                    raise ValueError("binding mismatch; preserve state, use another run directory")
            else:
                self.state = {"schema_version": 1, "binding": self.binding,
                              "run_id": digest(self.binding)[:24], "setups": {},
                              "candidates": [], "candidate_seen_at": {}, "cursors": {},
                              "signal_ids": [], "simulator": None, "audit": [], "health": {},
                              "trials": {}, "quote_history": {}, "production_allowed": False}
            self.state["running"] = True
            self.save()
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            raise RuntimeError("STATE_STORE_REFUSED: " + str(exc)) from exc

    def __exit__(self, *_):
        if self._lock_file is not None:
            try:
                self._lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def save(self) -> None:
        with self.mutex:
            temporary = self.directory / "state.pending.json"
            payload = json.dumps({"checksum": digest(self.state), "state": self.state},
                                 sort_keys=True, ensure_ascii=True, allow_nan=False)
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.directory / "state.json")

    def audit(self, status: str, as_of: int, **fields) -> dict:
        with self.mutex:
            row = {"sequence": len(self.state["audit"]), "run_id": self.state["run_id"],
                   **self.binding, "status": status, "decision_time": as_of,
                   "production_allowed": False, **fields}
            self.state["audit"].append(row)
            return row

    def archive_snapshot(self, snapshot: dict) -> str:
        raw_hash = digest(snapshot)
        folder = self.directory / "snapshots"
        folder.mkdir(exist_ok=True)
        path = folder / (raw_hash + ".json")
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                json.dump(snapshot, handle, sort_keys=True, allow_nan=False)
        return "snapshots/" + path.name


class DualScheduler:
    """Two real independent worker lanes; no overlapping invocation per lane."""

    def __init__(self, discovery: Callable, fast: Callable, clock: Callable = wall_ms,
                 executor=None, on_health: Callable | None = None):
        self.clock = clock
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="research")
        self.owns_executor = executor is None
        self.callbacks = {"discovery": discovery, "fast": fast}
        self.intervals = {"discovery": 900000, "fast": 60000}
        self.pending: dict = {}
        self.on_health = on_health
        now = clock()
        self.health = {name: {"runs": 0, "failures": 0, "overlap_prevented": 0,
                              "last_run_at": None, "next_due_at": now, "delay_ms": 0}
                       for name in self.callbacks}

    def tick(self) -> None:
        now = self.clock()
        for name, callback in self.callbacks.items():
            health = self.health[name]
            previous = self.pending.get(name)
            if previous is not None and previous.done():
                try:
                    previous.result()
                    health["last_error"] = None
                except Exception as exc:
                    health["failures"] += 1
                    health["last_error"] = type(exc).__name__ + ": " + str(exc)
                health["completed_at"] = now
                self.pending.pop(name)
            if now < health["next_due_at"]:
                continue
            if name in self.pending:
                # Keep next_due_at pending; first free slot services it, no silent skip.
                health["overlap_prevented"] += 1
                health["delay_ms"] = now - health["next_due_at"]
                continue
            health["delay_ms"] = now - health["next_due_at"]
            health["last_run_at"] = now
            health["next_due_at"] = now + self.intervals[name]
            health["runs"] += 1
            self.pending[name] = self.executor.submit(callback)
        if self.on_health:
            self.on_health(copy.deepcopy(self.health))

    def close(self) -> None:
        if self.owns_executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        for name, future in self.pending.items():
            if future.cancelled():
                continue
            try:
                future.result()
            except Exception as exc:
                self.health[name]["failures"] += 1
                self.health[name]["last_error"] = type(exc).__name__ + ": " + str(exc)
        self.pending.clear()
        if self.on_health:
            self.on_health(copy.deepcopy(self.health))


class Engine:
    def __init__(self, store: StateStore, profile: dict):
        from auto_trading.execution import Simulator
        self.store = store
        self.profile = profile
        self.simulator = Simulator(profile, state=store.state["simulator"])

    def pause_orders(self, symbol: str, as_of: int, reasons: list[str]) -> None:
        for order in list(self.simulator.state["orders"].values()):
            if order["symbol"] == symbol and order["status"] in {"WORKING", "PARTIALLY_FILLED"}:
                outcome = self.simulator.cancel(order["order_id"], as_of)
                self.store.audit("RISK_INCREASE_PAUSED", as_of, symbol=symbol, reasons=reasons, execution=outcome)

    def forward_protection(self, snapshots: dict, as_of: int) -> None:
        """Use only quotes actually collected before an execution open.

        Missing historical quotes never manufacture entries; recovery only uses
        a fresh current quote to close an existing breached hard stop.
        """
        state = self.store.state
        cursors = state.setdefault("execution_cursors", {})
        histories = state.setdefault("execution_snapshot_history", {})
        for symbol, snapshot in snapshots.items():
            records = snapshot.get("records", {})
            quote = records.get("quote")
            history = state["quote_history"].setdefault(symbol, [])
            inputs = histories.setdefault(symbol, [])
            inputs.append({"observed_at": as_of, "snapshot": copy.deepcopy(snapshot)})
            inputs[:] = inputs[-4:]
            if quote and quote.get("available_at", as_of + 1) <= as_of:
                if not history or quote != history[-1]:
                    history.append(quote)
                    history[:] = history[-32:]
            bars = [b for b in snapshot.get("bars", {}).get("1m", [])
                    if b.get("available_at", as_of + 1) <= as_of
                    and b.get("period_end", as_of + 1) <= as_of]
            last_cursor = cursors.get(symbol)
            # New forward runner cannot reconstruct unobserved execution history.
            pending = [b for b in bars if last_cursor is not None and b["period_end"] > last_cursor]
            for bar in pending:
                candidates = [q for q in history if q.get("available_at", as_of + 1) <= bar["period_start"]
                              and 0 <= bar["period_start"] - q.get("event_time", -1) <= self.profile["quote_max_age_ms"]]
                opening_quote = max(candidates, key=lambda q: q["event_time"]) if candidates else None
                from auto_trading.data import validate_snapshot
                eligible_inputs = [item["snapshot"] for item in inputs if item["observed_at"] <= bar["period_start"]
                                   and not validate_snapshot(item["snapshot"], bar["period_start"], self.profile, for_entry=True)]
                if not eligible_inputs:
                    self.pause_orders(symbol, as_of, ["DATA_INSUFFICIENT:EXECUTION_SNAPSHOT_AT_OPEN"])
                for event in self.simulator.process_bar(symbol, bar, opening_quote, as_of):
                    self.store.audit(event.get("status", "EXECUTION"), as_of, execution=event)
            if bars:
                cursors[symbol] = max(b["period_end"] for b in bars)
            if quote:
                for event in self.simulator.recover_quote(symbol, quote, as_of):
                    self.store.audit(event.get("status", "PROTECTION_RECOVERY"), as_of, execution=event)
            for settlement in records.get("funding_settlements", []):
                result = self.simulator.apply_funding(settlement, as_of)
                self.store.audit("FUNDING", as_of, execution=result)

    def process(self, snapshots: dict, as_of: int, execution_events: list | None = None,
                funding_events: list | None = None, failed_symbols: list[str] | None = None,
                decision_symbols: list[str] | None = None) -> dict:
        from auto_trading.strategy import evaluate, market_regime
        from auto_trading.data import validate_snapshot
        state = self.store.state
        with self.store.mutex:
            if self.store.mode != "observe":
                for symbol in failed_symbols or []:
                    self.pause_orders(symbol, as_of, ["DATA_INSUFFICIENT:PUBLIC_READ_FAILED"])
                for symbol, snapshot in snapshots.items():
                    reasons = validate_snapshot(snapshot, as_of, self.profile)
                    if reasons:
                        self.pause_orders(symbol, as_of, reasons)
                if self.store.mode == "paper":
                    self.forward_protection(snapshots, as_of)
                for item in execution_events or []:
                    # A replay must supply the complete inputs that were known
                    # at execution open. Quotes alone cannot bypass data gates.
                    at_open = item.get("snapshot")
                    reasons = (["DATA_INSUFFICIENT:EXECUTION_SNAPSHOT"] if at_open is None else
                               validate_snapshot(at_open, item["bar"]["period_start"], self.profile, for_entry=True))
                    if reasons:
                        self.pause_orders(item["symbol"], as_of, reasons)
                    for event in self.simulator.process_bar(item["symbol"], item["bar"], item.get("quote"), as_of):
                        self.store.audit(event.get("status", "EXECUTION"), as_of, execution=event)
                for event in funding_events or []:
                    result = self.simulator.apply_funding(event, as_of)
                    self.store.audit("FUNDING", as_of, execution=result)
            benchmark_data = {s: snapshots[s] for s in self.profile["benchmarks"] if s in snapshots}
            regime = market_regime(benchmark_data, as_of, self.profile)
            state["market_regime"] = regime
            signals = []
            pending_signals = state.setdefault("pending_entry_signals", {})
            for symbol, snapshot in sorted(snapshots.items()):
                if decision_symbols is not None and symbol not in decision_symbols:
                    continue
                try:
                    quote = snapshot.get("records", {}).get("quote")
                    if quote and self.store.mode != "observe":
                        q = quote["data"]
                        if quote["available_at"] <= as_of and as_of - quote["event_time"] <= self.profile["quote_max_age_ms"]:
                            self.simulator.mark(symbol, q["bid"], q["ask"], as_of)
                    result = evaluate(snapshot, as_of, self.profile, regime, state["setups"].get(symbol, []))
                    try:
                        raw_ref = self.store.archive_snapshot(snapshot)
                    except (ValueError, TypeError):
                        raw_ref = None
                        self.store.audit("SCHEMA_CONFLICT", as_of, symbol=symbol, reason="NONCANONICAL_SNAPSHOT")
                    previous_equities = {s["setup_id"]: s.get("setup_equity")
                                         for s in state["setups"].get(symbol, [])}
                    for setup in result["setups"]:
                        setup["setup_equity"] = previous_equities.get(setup["setup_id"]) or self.simulator.account_equity()
                    state["setups"][symbol] = result["setups"]
                    bars = snapshot.get("bars", {}).get("5m", [])
                    visible = [b for b in bars if b.get("available_at", as_of + 1) <= as_of
                               and b.get("period_end", as_of + 1) <= as_of]
                    if visible:
                        state["cursors"][symbol] = visible[-1]["period_end"]
                    self.store.audit(result["status"], as_of, symbol=symbol, snapshot_ref=raw_ref,
                                     rejections=result["rejections"], regime=regime)
                    for signal in result["signals"]:
                        matching = next((s for s in result["setups"] if s["setup_id"] == signal["setup_id"]), None)
                        if matching:
                            signal["setup_equity"] = matching["setup_equity"]
                        signals.append((signal, snapshot))
                except Exception as exc:
                    self.store.audit("RUNTIME_ERROR", as_of, symbol=symbol,
                                     reason=type(exc).__name__ + ": " + str(exc))
            emitted = {signal["signal_id"] for signal, _ in signals}
            for sid, signal in list(pending_signals.items()):
                if as_of - signal["event_time"] > self.profile["entry_ttl_ms"]:
                    pending_signals.pop(sid)
                    self.store.audit("EXPIRED", as_of, signal_id=sid, reason="PENDING_ENTRY_TTL")
                    continue
                if sid in emitted or signal["symbol"] not in snapshots:
                    continue
                setup = next((s for s in state["setups"].get(signal["symbol"], [])
                              if s["setup_id"] == signal["setup_id"]), None)
                if not setup or setup.get("signal_state") in {"INVALIDATED", "EXPIRED"}:
                    pending_signals.pop(sid)
                    continue
                if regime["state"] != signal["regime"]["state"]:
                    continue
                retry = copy.deepcopy(signal)
                retry["decision_time"] = as_of
                quote = snapshots[signal["symbol"]].get("records", {}).get("quote", {})
                retry["received_at"] = max(retry["received_at"], quote.get("received_at", as_of))
                retry["available_at"] = max(retry["available_at"], quote.get("available_at", as_of))
                retry["retry_of_decision_time"] = signal["decision_time"]
                signals.append((retry, snapshots[signal["symbol"]]))
            # Deterministic across engine arrival order; never a probability score.
            def rank(pair):
                signal, snapshot = pair
                quote = snapshot["records"]["quote"]
                q = quote["data"]
                return (-signal["net_rr"], (q["ask"] - q["bid"]) / q["bid"],
                        as_of - quote["event_time"], signal["symbol"], signal["signal_id"])
            selected = False
            for signal, snapshot in sorted(signals, key=rank):
                sid = signal["signal_id"]
                if sid in state["signal_ids"]:
                    self.store.audit("DUPLICATE_SIGNAL", as_of, signal=signal)
                    continue
                if selected:
                    self.store.audit("RISK_LIMIT", as_of, signal=signal, reason="SINGLE_CANDIDATE_RANKING")
                    continue
                if self.store.mode == "observe":
                    selected = True
                    state["signal_ids"].append(sid)
                    self.store.audit("SIGNAL_READY", as_of, signal=signal, position_state="NOT_OPENED_OBSERVE")
                else:
                    outcome = self.simulator.submit(signal, snapshot, as_of)
                    self.store.audit(outcome.get("status", "EXECUTION"), as_of, signal=signal, execution=outcome)
                    selected = outcome.get("status") in {"VIRTUAL_ORDER_WORKING", "VIRTUAL_FILLED"}
                    if selected:
                        state["signal_ids"].append(sid)
                        pending_signals.pop(sid, None)
                    elif outcome.get("status") in {"DATA_STALE", "DATA_INSUFFICIENT"}:
                        pending_signals[sid] = copy.deepcopy(signal)
            state["simulator"] = self.simulator.state
            state["last_decision_at"] = as_of
            self.store.save()
            return {"signals": len(signals), "regime": regime["state"]}


def status(directory: Path) -> dict:
    path = Path(directory) / "state.json"
    if not path.exists():
        return {"status": "NOT_STARTED", "production_allowed": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = payload["state"]
    if digest(state) != payload["checksum"]:
        raise RuntimeError("STATE_CORRUPT")
    simulator = state.get("simulator") or {}
    return {"binding": state["binding"], "run_id": state["run_id"],
            "running_marker": state.get("running", False), "health": state["health"],
            "last_decision_at": state.get("last_decision_at"),
            "candidate_count": len(state["candidates"]), "audit_count": len(state["audit"]),
            "virtual_positions": len(simulator.get("positions", {})),
            "orders": len(simulator.get("orders", {})), "equity": simulator.get("equity"),
            "production_allowed": False}


def run_replay(fixture_path: Path, directory: Path, profile: dict) -> dict:
    require_mode("replay", profile)
    raw = Path(fixture_path).read_bytes()
    fixture = json.loads(raw)
    events = fixture["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("REPLAY_REQUIRES_EVENTS")
    times = [event["as_of"] for event in events]
    if times != sorted(set(times)) or any(type(t) is not int or t < 0 for t in times):
        raise ValueError("REPLAY_TIME_ORDER_CONFLICT")
    data_hash = hashlib.sha256(raw).hexdigest()
    with StateStore(directory, "replay", profile, data_hash) as store:
        engine = Engine(store, profile)
        trial_id = digest(store.binding)
        # Duplicate trial is resumed, never tuned/relabelled as unseen OOS.
        trial = store.state["trials"].setdefault(trial_id, {"binding": store.binding,
                     "evidence_kind": fixture.get("evidence_kind", "UNVERIFIED_FIXTURE"),
                     "status": "RESEARCH_EXPERIMENT", "last_event": -1})
        for index, event in enumerate(events):
            if index <= trial["last_event"]:
                continue
            # Checkpoint shares the atomic decision/order/audit transaction.
            trial["last_event"] = index
            engine.process(event.get("snapshots", {}), event["as_of"],
                           event.get("execution_events"), event.get("funding_events"))
            store.save()
        trial["status"] = "ENGINEERING_REPLAY_COMPLETED"
        store.state["running"] = False
        store.save()
    return {**status(directory), "replay_events": len(events), "research_gate": "NOT_EVALUATED"}


def run_observe(directory: Path, profile: dict, seconds: float = 0, mode: str = "observe",
                gate_report: Path | None = None, data_manifest: Path | None = None,
                max_requests: int | None = None) -> dict:
    eligibility = require_mode(mode, profile, gate_report, data_manifest)
    from auto_trading.data import PublicAdapter
    network_profile = copy.deepcopy(profile)
    if max_requests is not None:
        network_profile["max_requests"] = min(max_requests, profile["max_requests"])
    binding = digest(eligibility["gate"]["bindings"]) if mode == "paper" else "public_forward"
    with StateStore(directory, mode, profile, binding) as store:
        if mode == "paper":
            store.state["research_gate"] = eligibility["gate"]
        engine = Engine(store, profile)
        discovery_adapter = PublicAdapter(network_profile)
        fast_adapter = PublicAdapter(network_profile)
        stop_path = store.directory / "stop.request"
        shutdown = threading.Event()
        for adapter in (discovery_adapter, fast_adapter):
            adapter.cancelled = lambda: shutdown.is_set() or stop_path.exists()
        if stop_path.exists():
            raise RuntimeError("STOP_REQUEST_PRESENT: choose a new run directory")

        def discovery():
            adapter = discovery_adapter
            adapter.reset_budget()
            adapter.deadline_ms = wall_ms() + 50000
            now = wall_ms()
            try:
                symbols = adapter.discover()
                with store.mutex:
                    for symbol in symbols:
                        store.state["candidate_seen_at"][symbol] = now
                    live = {s for s, seen in store.state["candidate_seen_at"].items()
                            if now - seen <= profile["armed_ttl_ms"]}
                    store.state["candidates"] = sorted(live)
                    store.audit("DISCOVERY_COMPLETED", now, candidates=len(symbols),
                                adapter_health=getattr(adapter, "health", {}))
                    store.save()
            except Exception as exc:
                with store.mutex:
                    store.audit("RUNTIME_ERROR", now, lane="discovery", reason=str(exc))
                    store.save()
                raise

        def fast():
            adapter = fast_adapter
            adapter.reset_budget()
            adapter.deadline_ms = wall_ms() + 50000
            with store.mutex:
                current = wall_ms()
                ordered = fast_symbols(store.state, profile, current)
            snapshots = {}
            benchmark_cache = store.state.setdefault("benchmark_snapshots", {})
            successes = 0
            failures = 0
            deferred = 0
            failed_symbols = []
            # Benchmark evidence is independent of individual candidate directions.
            attempts = store.state.setdefault("last_snapshot_attempt", {})
            for symbol in ordered:
                try:
                    attempts[symbol] = wall_ms()
                    snapshots[symbol] = adapter.snapshot(symbol)
                    successes += 1
                    if symbol in profile["benchmarks"]:
                        benchmark_cache[symbol] = snapshots[symbol]
                    if mode == "observe":
                        engine.process({**benchmark_cache, symbol: snapshots[symbol]}, adapter.clock(),
                                       decision_symbols=[symbol])
                except Exception as exc:
                    if "REQUEST_BUDGET" in str(exc) or "CIRCUIT_OPEN" in str(exc):
                        with store.mutex:
                            pending = ordered[ordered.index(symbol):]
                            deferred += len(pending)
                            failed_symbols.extend(pending)
                            store.audit("DATA_INSUFFICIENT", wall_ms(), unprocessed=pending,
                                        reason="PUBLIC_REQUEST_BUDGET_OR_CIRCUIT")
                        break
                    failures += 1
                    failed_symbols.append(symbol)
                    with store.mutex:
                        store.audit("DATA_INSUFFICIENT", wall_ms(), symbol=symbol, reason=str(exc))
                if stop_path.exists():
                    break
            if mode != "observe":
                engine.process(snapshots, adapter.clock(), failed_symbols=failed_symbols)
            with store.mutex:
                store.state["health"]["fast_watch"] = {"symbols": len(ordered), "successes": successes,
                                  "failures": failures, "deferred": deferred, "last_run_at": wall_ms(),
                                  "last_processed_5m": copy.deepcopy(store.state["cursors"])}
                store.state["health"]["fast_watch"]["adapter_health"] = getattr(adapter, "health", {})
                store.state["health"]["fast_watch"]["clock_calibration"] = getattr(adapter, "clock_calibration", None)
                store.save()

        def persist_health(health):
            with store.mutex:
                store.state["health"]["scheduler"] = health
                store.state["health"]["state_store"] = "OK"
                store.save()

        scheduler = DualScheduler(discovery, fast, on_health=persist_health)
        deadline = time.monotonic() + seconds
        try:
            if seconds == 0:
                # One bounded round discovers before processing the full candidate set.
                for name, callback in (("discovery", discovery), ("fast", fast)):
                    now = wall_ms()
                    health = scheduler.health[name]
                    health.update(runs=1, last_run_at=now, next_due_at=now + scheduler.intervals[name])
                    try:
                        callback()
                    except Exception as exc:
                        health["failures"] += 1
                        health["last_error"] = type(exc).__name__ + ": " + str(exc)
                persist_health(scheduler.health)
            else:
                while time.monotonic() < deadline and not stop_path.exists():
                    scheduler.tick()
                    time.sleep(0.05)
        except KeyboardInterrupt:
            store.audit("STOP_REQUESTED", wall_ms(), reason="KeyboardInterrupt")
        finally:
            shutdown.set()
            scheduler.close()
            store.state["running"] = False
            store.audit("STOPPED", wall_ms())
            store.save()
    return status(directory)
