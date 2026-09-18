# Candidate v1 research runner

`production_allowed=false`. The default is `observe`: public market reads and local
candidate/audit records only. `replay` executes offline research simulations.
Forward `paper` requires the current source, profile, costs and dataset to pass
the bound Research Gate. No exchange order client or notification client exists.

Run from the verified repository root:

```powershell
Set-Location E:\Migrated_2026\Codex_All\active\.codex\worktrees\8b7c\binance-futures-local-bot
python -m auto_trading --help
python -m auto_trading observe --run observe-once --max-requests 256
python -m auto_trading observe --run observe-bounded --seconds 180 --max-requests 256
python -m auto_trading status --run observe-bounded
python -m auto_trading stop --run observe-bounded
```

The runner stays in the foreground. Ctrl+C requests a bounded shutdown; the
`stop` command only writes a stop request inside the specified research run.
It never kills another process. Completed runs leave no worker threads/processes.
A stop request persists; use a new run name to start a new observation.
`--seconds 0` is one round. Positive durations are capped at 2700 seconds;
individual requests have timeouts and per-worker-round budgets. Network failures
produce explicit health/audit errors and a nonzero CLI result, never NO_SETUP.
Each worker round also has a 50-second acquisition deadline. A stop prevents
further requests and lets the current request finish within its timeout.
Budget-deferred symbols are reported as incomplete coverage and yield a nonzero
CLI result. Benchmarks run first, followed by retained ARMED setups and then
discovery candidates; oldest attempts rotate first within each group. The
900s/60s callback cadence does not guarantee every candidate is covered within
60 seconds when acquisition exceeds the round budget.

Public snapshots calibrate against the allowlisted exchange server-time GET.
They retain local send/receipt times, lower/upper offset bounds and uncertainty.
The lower bound decides candle completion; the upper bound timestamps receipt
and decision availability. The system clock is unchanged. Observe evaluates
each complete snapshot immediately so earlier quotes do not age while the
remaining candidate snapshots are collected. Only candle intervals crossing a
boundary are refreshed before final quote/depth acquisition.

Offline replay input is JSON with `evidence_kind` and an ordered `events` list.
Each event has UTC epoch-millisecond `as_of`, optional `snapshots` keyed by symbol,
`execution_events` (`symbol`, `bar`, `quote`, complete `snapshot` known at that
bar's open), and `funding_events`. Record shapes
are documented in `reports/auto_trading_candidate_v1/INTERFACES.md` and in the
data module tests. No network is called in replay. Input files are read-only.

```powershell
python -m auto_trading replay --run synthetic-replay --input reports/auto_trading_candidate_v1/synthetic_replay.json
python -m auto_trading status --run synthetic-replay
python -m auto_trading paper --run paper-refused
```

The last command must return `RESEARCH_GATE_REFUSED` without any network or
account access. A future qualifying artifact is supplied using `--gate-report`
and `--data-manifest`. See `research_gate.py` for the versioned report schema.
Synthetic engineering fixtures cannot establish a real-market research gate.
The existing repository `RESEARCH_GATE_STATUS.md` also remains unchanged.

The two real scheduler lanes call discovery every 900 seconds and fast watch
every 60 seconds. A slow discovery request cannot occupy the fast worker.
Each lane prevents overlaps, retains overdue work, and records actual runs,
failures, delay and next due time. A virtual-clock test executes the callbacks
over 1800 seconds; it does not pretend this is a live market soak test.

All state lives under `reports/auto_trading_runs/<run>/`. `state.json` contains
the complete simulator account/positions/orders/protection/day loss, frozen
setups, event cursors, deduplication IDs, trial bindings, health, and ordered audit.
Its checksum and source/profile/data bindings are checked on restart. Corruption
or a changed binding refuses restart instead of silently resetting risk. A
kernel lock prevents two runners from writing the same run. Snapshots are
content-addressed, never written over the original data lake or feature store.
Paper additionally persists the verified gate and binds the report-file hash,
manifest digest, source-file hashes and cost profile into its state binding.
Use a fresh run name after any executable source, profile or data change;
never delete prior state to bypass a binding refusal. Status can inspect older
runs and reports their original bindings. Rejected submissions due to transient
missing/stale input retain pending signals until expiry and may retry after
fresh validation; deduplication records an entry only after order acceptance.
On a known data failure, pending entry/add orders are cancelled while existing
positions and protections remain. Forward paper additionally needs the complete
inputs and quote actually observed at an execution open. Missing that capture
means no entry; current quote recovery may close an existing crossed hard stop
with an uncertainty marker. The REST polling implementation cannot guarantee a
quote at every exact minute boundary.

`profile.json` is the explicit unvalidated research configuration. It initializes
a virtual equity of 10,000 USDT; this is not a user account balance. The pure
ATR helper is reused from `feature_engineering.atr` (Wilder ATR14, minimum 15
bars, trigger bar excluded). Per-side fee 5 bps and slippage 5 bps preserve the
existing conservative 20 bps round-trip baseline; funding is additionally
reserved, never credited as a future guaranteed gain. Structural targets are
frozen independently of stop distance. Contract defaults for base/pivots,
regime, flow/OI thresholds, expiry, staged risk, TP allocations and holding time
are research hypotheses, not verified profitability.
Only one to three independently verified targets are accepted. When fewer than
three exist, only their configured 50%/30% allocations execute; the residual
retains its hard stop and time stop. Additional targets are never invented.
The base requires all closes inside the shared high/low overlap with mixed or
flat candle bodies. B1/C1 require an intervening pause and a new extreme. UNKNOWN
regime blocks entry. These fixed source rules are also bound by the source hash.

Research Gate checks the current executable (including the reused ATR helper),
profile, cost and manifest hashes, and matches each report trade to a hashed
JSON/JSONL trade export by `raw_ref`. Its exact schema is in the interface note.
It recomputes setup-level OOS metrics and UTC month coverage from exit timestamps,
and rejects conflicting month labels. Matching exports is an artifact integrity
check; it cannot independently authenticate an external producer's market
provenance. Synthetic fixtures do not qualify this candidate for paper.
Walk-forward embargo must be an explicit nonnegative integer; normalized fold
IDs are unique. Repeated source trade references are rejected before return
aggregation; distinct partial exits still count as one independent setup.

Validation uses the installed Python standard library and existing unittest
framework; this package adds no dependency. The current interpreter lacks pip,
pytest, requests and pandas. Existing unrelated workflows needing those packages
are not claimed to have run. No configured Ruff/mypy/package-build pipeline was
found in this repository.

```powershell
python -m unittest discover -s tests -p 'test_auto_trading*.py' -v
python -m unittest tests.test_strategy_lab_candidates tests.test_cost_model tests.test_no_lookahead tests.test_research_gate tests.test_protected_files -v
python -m compileall -q auto_trading
git diff --check
```

HEMI/EGLD/TUSDT/ARUSDT/MUBARAK conversation examples remain
`UNVERIFIED_FIXTURE`; none is copied into a purported Binance historical receipt.
No OOS profitability, paper eligibility or production acceptance is implied by
engineering tests.

The three authorized public smoke rounds are complete. The last round obtained
one complete BTC snapshot that passed the gate, with NO_SETUP. ETH/SOL reads
failed with URLError, and 100 symbols were deferred by circuit/budget; the CLI
correctly exited 2. This is partial public coverage, not a whole-market soak.
No background runner remains. Full evidence and final source bindings are in
`reports/auto_trading_candidate_v1/REPORT.md`.
The subsequent offline gate review and six synthetic risk scenarios are recorded
in `reports/auto_trading_candidate_v1/continuation_20260905/REPORT.md`; that report
contains the newer executable binding. Historical run bindings remain unchanged.
