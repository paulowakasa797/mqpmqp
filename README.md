# mqpmqp

Offline research package for a Binance USD-M breakout/continuation candidate.
`production_allowed` is always `false`. This repository does not contain an
exchange order client, notification client, or production credentials.

## What was uploaded

Google Drive provided 32 files (20 sources + 12 `__pycache__` `.pyc` files).
The `.pyc` files are not used. SHA-256 values for the imported sources are in
`BUNDLE_MANIFEST.json`. The manifest also lists the breakout-continuation
engine, freeze/protocol files, 5m archives, replay ledgers and 47 engine tests;
those files were **not** in the Drive upload, so the original 2026-08-02..2026-09-01
window cannot be replayed here.

## How to run

```text
python -B -m unittest discover -s tests -v
python -m auto_trading --help
python -m auto_trading paper --run paper-refused
```

The last command must exit `RESEARCH_GATE_REFUSED` without network or account
access. `observe` performs public GETs only; `replay` is offline JSON. Neither
path can enable live trading.

## Audit output

See `reports/breakout_continuation_20260919/REPORT_v3.md` and
`FINAL_DECISION_v3.json`. Deployment remains `NO-GO` / `NOT_AUTHORIZED`.
Original R1 gain is `INCONCLUSIVE` because historical quotes and receipts are
absent.
