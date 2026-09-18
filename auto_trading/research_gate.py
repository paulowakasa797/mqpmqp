from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from auto_trading.contracts import digest, strategy_hash


COST_PROFILE_KEYS = ("fee_rate", "slippage_bps", "funding_reserve_rate", "tp_fractions", "time_stop_bars")
REQUIRED_TRIAL_AUDIT_FLAGS = ("all_trials_recorded", "trial_parameters_frozen", "no_oos_selection")
STRICTER_GATE_STATUS = {
    "multiplicity_status": {"PASS", "SELECTION_BIAS_CONTROLLED", "NOT_REQUIRED_SINGLE_CANDIDATE"},
    "source_pit_status": {"PASS", "PIT_VERIFIED"},
    "universe_status": {"PASS", "ACTIVE_UNIVERSE_ONLY"},
}


def verify_gate(report_path: str | Path | None, profile: Mapping[str, Any], data_manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Return fail-closed paper eligibility from immutable local evidence.

    The report is only evidence input. This validator recomputes current
    strategy/profile/data/cost bindings and setup-aggregated OOS metrics instead
    of trusting a user-set PASS boolean. It performs no network, credential, or
    production-entry imports.
    """
    reasons: list[str] = []
    if report_path is None:
        return _result(False, ["RESEARCH_GATE_REPORT_MISSING"], {
            "strategy_hash": strategy_hash(),
            "profile_digest": digest(profile),
            "evidence_kind": "",
        }, _empty_metrics())
    if data_manifest_path is None:
        return _result(False, ["DATA_MANIFEST_MISSING"], {
            "strategy_hash": strategy_hash(),
            "profile_digest": digest(profile),
            "evidence_kind": "",
        }, _empty_metrics())
    report_file = Path(report_path)
    manifest_file = Path(data_manifest_path)
    bindings: dict[str, Any] = {
        "strategy_hash": strategy_hash(),
        "profile_digest": digest(profile),
        "evidence_kind": "",
    }
    metrics = _empty_metrics()

    if not report_file.exists():
        return _result(False, ["RESEARCH_GATE_REPORT_MISSING"], bindings, metrics)

    manifest_payload: dict[str, Any] = {}
    if not manifest_file.exists():
        reasons.append("DATA_MANIFEST_MISSING")
    else:
        manifest_payload, manifest_error = _load_json(manifest_file)
        if manifest_error:
            reasons.append(manifest_error)

    report, report_error = _load_json(report_file)
    if report_error:
        return _result(False, [*reasons, report_error], bindings, metrics)

    bindings["evidence_kind"] = str(report.get("evidence_kind") or "")
    bindings["report_file_hash"] = _sha256_file(report_file)
    bindings["data_manifest_digest"] = digest(manifest_payload)
    bindings["data_file_hashes"] = _manifest_file_hashes(manifest_file, manifest_payload, reasons)
    cost_profile = _cost_profile(profile)
    bindings["cost_profile_digest"] = digest(cost_profile)

    if bindings["evidence_kind"] != "real_market":
        reasons.append("REAL_MARKET_EVIDENCE_REQUIRED")
    _check_equal(report, "strategy_hash", bindings["strategy_hash"], "STRATEGY_HASH_MISMATCH", reasons)
    _check_equal(report, "profile_digest", bindings["profile_digest"], "PROFILE_DIGEST_MISMATCH", reasons)
    _check_equal(report, "data_manifest_digest", bindings["data_manifest_digest"], "DATA_MANIFEST_DIGEST_MISMATCH", reasons)
    _check_equal(report, "cost_profile_digest", bindings["cost_profile_digest"], "COST_PROFILE_DIGEST_MISMATCH", reasons)
    if report.get("cost_profile") != cost_profile:
        reasons.append("COST_PROFILE_MISMATCH")
    if report.get("data_file_hashes") != bindings["data_file_hashes"]:
        reasons.append("DATA_FILE_HASHES_MISMATCH")

    fold_windows, fold_reasons = _evaluate_walk_forward(report.get("walk_forward_folds"))
    reasons.extend(fold_reasons)
    metrics, metric_reasons = _evaluate_oos_metrics(
        report.get("oos_trades"),
        fold_windows,
        set(bindings["data_file_hashes"]),
    )
    reasons.extend(metric_reasons)
    reasons.extend(_validate_source_trade_rows(manifest_file, bindings["data_file_hashes"],
                                               report.get("oos_trades"), bindings, profile))
    reasons.extend(_evaluate_trial_audit(report.get("trial_audit")))
    reasons.extend(_evaluate_schema_evidence(report.get("schema_evidence"), profile))

    return _result(not reasons, sorted(set(reasons)), bindings, metrics)


def _result(allowed: bool, reasons: list[str], bindings: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "allowed": bool(allowed),
        "reasons": list(reasons),
        "bindings": dict(bindings),
        "metrics": dict(metrics),
        "production_allowed": False,
    }


def _validate_source_trade_rows(manifest_file: Path, files: Mapping[str, str], rows: Any,
                                bindings: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    """A digest of an unrelated file cannot validate invented OOS trade rows.

    Supported evidence exports are JSONL trade records or JSON trade_evidence
    lists, keyed by raw_ref. This checks artifact correspondence; it is not a
    certification that an external producer collected authentic market data.
    """
    sources: dict[str, dict[str, dict]] = {}
    reasons = []
    for name in files:
        try:
            path = manifest_file.parent / name
            text = path.read_text(encoding="utf-8")
            source_rows = ([json.loads(line) for line in text.splitlines() if line.strip()]
                           if path.suffix == ".jsonl" else json.loads(text).get("trade_evidence", []))
            indexed = {}
            for source in source_rows:
                key = source.get("raw_ref")
                if key in indexed or not isinstance(key, str) or not key:
                    raise ValueError("duplicate or missing raw_ref")
                indexed[key] = source
            sources[name] = indexed
        except (OSError, ValueError, TypeError, AttributeError):
            reasons.append("SOURCE_TRADE_EVIDENCE_UNSUPPORTED")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("data_refs"), list):
            reasons.append("SOURCE_TRADE_REF_MISSING")
            continue
        matches = [sources.get(name, {}).get(row.get("raw_ref")) for name in row["data_refs"] if isinstance(name, str)]
        matches = [source for source in matches if isinstance(source, dict)]
        if not matches:
            reasons.append("SOURCE_TRADE_REF_MISSING")
            continue
        for source in matches:
            if any(source.get(key) != row.get(key) for key in
                   ("setup_id", "symbol", "fold_id", "entry_time", "exit_time", "gross_r", "cost_r")):
                reasons.append("SOURCE_TRADE_ROW_MISMATCH")
            if (source.get("strategy_hash") != bindings["strategy_hash"]
                    or source.get("profile_digest") != bindings["profile_digest"]):
                reasons.append("SOURCE_TRADE_BINDING_MISMATCH")
            if set(source.get("required_inputs", [])) != set(profile["required_records"]):
                reasons.append("SOURCE_TRADE_INPUT_COVERAGE_MISSING")
    return reasons


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"MISSING_JSON:{path}"
    except json.JSONDecodeError as exc:
        return {}, f"INVALID_JSON:{path}:{exc}"
    if not isinstance(payload, dict):
        return {}, f"INVALID_JSON_OBJECT:{path}"
    return payload, None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_file_hashes(manifest_path: Path, manifest: Mapping[str, Any], reasons: list[str]) -> dict[str, str]:
    base = manifest_path.parent.resolve()
    hashes: dict[str, str] = {}
    entries = manifest.get("entries", [])
    if not isinstance(entries, list):
        reasons.append("DATA_MANIFEST_ENTRIES_INVALID")
        return hashes
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_path = entry.get("path") or entry.get("lake_path") or entry.get("source_path")
        if not raw_path:
            reasons.append("DATA_MANIFEST_ENTRY_PATH_MISSING")
            continue
        candidate = Path(str(raw_path))
        resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            key = resolved.relative_to(base).as_posix()
        except ValueError:
            reasons.append(f"DATA_FILE_OUTSIDE_MANIFEST_DIR:{raw_path}")
            continue
        if not resolved.exists():
            reasons.append(f"DATA_FILE_MISSING:{raw_path}")
            continue
        hashes[key] = _sha256_file(resolved)
    if not hashes:
        reasons.append("DATA_FILE_HASHES_EMPTY")
    return dict(sorted(hashes.items()))


def _cost_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {key: profile.get(key) for key in COST_PROFILE_KEYS}


def _check_equal(report: Mapping[str, Any], key: str, expected: Any, reason: str, reasons: list[str]) -> None:
    if report.get(key) != expected:
        reasons.append(reason)


def _empty_metrics() -> dict[str, Any]:
    return {
        "oos_unique_setups": 0,
        "oos_symbols": 0,
        "oos_months": 0,
        "profit_factor": 0.0,
        "avg_r": 0.0,
        "net_r": 0.0,
        "stress_1_5x_net_r": 0.0,
    }


def _evaluate_oos_metrics(
    rows: Any,
    fold_windows: Mapping[Any, Mapping[str, float]],
    manifest_file_keys: set[str],
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    if not isinstance(rows, list):
        return _empty_metrics(), ["OOS_TRADES_MISSING", "OOS_SETUPS_LT_300"]

    grouped: dict[tuple[str, str, Any], dict[str, Any]] = defaultdict(
        lambda: {"net_r": 0.0, "gross_r": 0.0, "cost_r": 0.0, "symbol": "", "month": ""}
    )
    setup_locations: dict[str, set[tuple[str, Any]]] = defaultdict(set)
    malformed = 0
    missing_refs = 0
    unknown_refs = 0
    outside_fold = 0
    before_freeze = 0
    month_conflicts = 0
    seen_trade_refs: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            malformed += 1
            continue
        setup_id = str(row.get("setup_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        fold_id = _fold_key(row.get("fold_id"))
        gross = _finite_float(row.get("gross_r"))
        cost = _finite_float(row.get("cost_r"))
        entry_time = _finite_float(row.get("entry_time"))
        exit_time = _finite_float(row.get("exit_time"))
        raw_ref = str(row.get("raw_ref") or "").strip()
        data_refs = row.get("data_refs")
        if not isinstance(data_refs, list) or not data_refs:
            missing_refs += 1
            malformed += 1
            continue
        if not setup_id or not symbol or fold_id is None or gross is None or cost is None or cost < 0 or entry_time is None or exit_time is None or exit_time < entry_time or not raw_ref:
            malformed += 1
            continue
        normalized_refs = {str(item) for item in data_refs if str(item)}
        if not normalized_refs:
            missing_refs += 1
            continue
        if not normalized_refs.issubset(manifest_file_keys):
            unknown_refs += 1
            continue
        trade_refs = {(name, raw_ref) for name in normalized_refs}
        if seen_trade_refs.intersection(trade_refs):
            reasons.append("DUPLICATE_OOS_TRADE_REF")
            continue
        seen_trade_refs.update(trade_refs)
        fold = fold_windows.get(fold_id) or fold_windows.get(str(fold_id))
        if not fold:
            outside_fold += 1
            continue
        if entry_time < fold["parameter_frozen_at"]:
            before_freeze += 1
        if entry_time < fold["oos_start"] or exit_time > fold["oos_end"]:
            outside_fold += 1
        derived_month = _month(row)
        if not derived_month:
            malformed += 1
            continue
        declared_month = str(row.get("month") or "")
        if declared_month and declared_month[:7] != derived_month:
            month_conflicts += 1
            continue
        setup_locations[setup_id].add((symbol, fold_id))
        item = grouped[(setup_id, symbol, fold_id)]
        item["gross_r"] += gross
        item["cost_r"] += cost
        item["net_r"] += gross - cost
        item["symbol"] = item["symbol"] or symbol
        item["month"] = item["month"] or derived_month
    if malformed:
        reasons.append("OOS_TRADE_ROWS_MALFORMED")
    if missing_refs:
        reasons.append("OOS_DATA_REFS_MISSING")
    if unknown_refs:
        reasons.append("OOS_DATA_REFS_NOT_IN_MANIFEST_HASHES")
    if before_freeze:
        reasons.append("OOS_TRADE_BEFORE_PARAMETER_FREEZE")
    if outside_fold:
        reasons.append("OOS_TRADE_OUTSIDE_FOLD_WINDOW")
    if month_conflicts:
        reasons.append("OOS_MONTH_INCONSISTENT_WITH_EXIT_TIME")
    if any(len(locations) > 1 for locations in setup_locations.values()):
        reasons.append("SETUP_ID_SPANS_SYMBOLS_OR_FOLDS")

    setups = list(grouped.values())
    unique = len(setups)
    net_values = [float(item["net_r"]) for item in setups]
    stress_values = [float(item["gross_r"]) - float(item["cost_r"]) * 1.5 for item in setups]
    wins = sum(value for value in net_values if value > 0)
    losses = abs(sum(value for value in net_values if value < 0))
    profit_factor = math.inf if wins > 0 and losses == 0 else (wins / losses if losses > 0 else 0.0)
    total_net = sum(net_values)
    avg_r = total_net / unique if unique else 0.0
    metrics = {
        "oos_unique_setups": unique,
        "oos_symbols": len({str(item["symbol"]) for item in setups if item["symbol"]}),
        "oos_months": len({str(item["month"]) for item in setups if item["month"]}),
        "profit_factor": profit_factor,
        "avg_r": avg_r,
        "net_r": total_net,
        "stress_1_5x_net_r": sum(stress_values),
    }
    if unique < 300:
        reasons.append("OOS_SETUPS_LT_300")
    if metrics["oos_symbols"] < 2:
        reasons.append("OOS_MULTI_SYMBOL_REQUIRED")
    if metrics["oos_months"] < 2:
        reasons.append("OOS_MULTI_MONTH_REQUIRED")
    if profit_factor < 1.35:
        reasons.append("PF_LT_1_35")
    if avg_r < 0.08:
        reasons.append("AVG_R_LT_0_08")
    if total_net <= 0:
        reasons.append("NET_R_NOT_POSITIVE")
    if metrics["stress_1_5x_net_r"] <= 0:
        reasons.append("COST_1_5X_NET_R_NOT_POSITIVE")
    return metrics, reasons


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _month(row: Mapping[str, Any]) -> str:
    """Coverage derives from the source-bound exit timestamp, never a label."""
    number = _finite_float(row.get("exit_time"))
    if number is not None and number > 0:
        try:
            return datetime.fromtimestamp(number / 1000.0, timezone.utc).strftime("%Y-%m")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _evaluate_walk_forward(folds: Any) -> tuple[dict[Any, dict[str, float]], list[str]]:
    if not isinstance(folds, list) or not folds:
        return {}, ["WALK_FORWARD_FOLDS_MISSING"]
    reasons: list[str] = []
    windows: dict[Any, dict[str, float]] = {}
    oos_intervals: list[tuple[float, float, float]] = []
    seen_fold_ids: set[str] = set()
    for fold in folds:
        if not isinstance(fold, Mapping):
            reasons.append("WALK_FORWARD_FOLD_INVALID")
            continue
        embargo = fold.get("embargo_ms")
        if not isinstance(embargo, int) or isinstance(embargo, bool) or embargo < 0:
            reasons.append("WALK_FORWARD_EMBARGO_INVALID")
            continue
        fold_id = _fold_key(fold.get("fold_id"))
        if fold_id is None:
            reasons.append("WALK_FORWARD_FOLD_ID_INVALID")
            continue
        if fold_id in seen_fold_ids:
            reasons.append("WALK_FORWARD_FOLD_ID_DUPLICATE")
            continue
        seen_fold_ids.add(fold_id)
        train_start = _finite_float(fold.get("train_start"))
        train_end = _finite_float(fold.get("train_end"))
        validation_start = _finite_float(fold.get("validation_start"))
        validation_end = _finite_float(fold.get("validation_end"))
        oos_start = _finite_float(fold.get("oos_start"))
        oos_end = _finite_float(fold.get("oos_end"))
        parameter_frozen_at = _finite_float(fold.get("parameter_frozen_at"))
        if fold_id is None or None in (train_start, train_end, validation_start, validation_end, oos_start, oos_end, parameter_frozen_at):
            reasons.append("WALK_FORWARD_FOLD_FIELDS_MISSING")
            continue
        if train_start > train_end or validation_start > validation_end or oos_start > oos_end:
            reasons.append("WALK_FORWARD_CHRONOLOGY_OR_EMBARGO_FAILED")
        if train_end + embargo > validation_start or validation_end + embargo > oos_start:
            reasons.append("WALK_FORWARD_CHRONOLOGY_OR_EMBARGO_FAILED")
        if parameter_frozen_at > oos_start:
            reasons.append("PARAMETERS_NOT_FROZEN_BEFORE_OOS")
        if str(fold.get("selection_rule") or "") != "validation_only" or bool(fold.get("oos_selection_used")):
            reasons.append("OOS_SELECTION_FORBIDDEN")
        windows[fold_id] = {
            "oos_start": oos_start,
            "oos_end": oos_end,
            "parameter_frozen_at": parameter_frozen_at,
        }
        windows[str(fold_id)] = windows[fold_id]
        oos_intervals.append((oos_start, oos_end, float(embargo)))
    for index, (start, end, embargo) in enumerate(sorted(oos_intervals)):
        for other_start, other_end, _ in sorted(oos_intervals)[index + 1 :]:
            if end + embargo > other_start and other_end >= start:
                reasons.append("WALK_FORWARD_OOS_FOLDS_OVERLAP")
                break
    return windows, reasons


def _fold_key(value: Any) -> str | None:
    """One stable key: integer 0 and string '0' must not name two folds."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def _evaluate_trial_audit(audit: Any) -> list[str]:
    if not isinstance(audit, Mapping):
        return ["TRIAL_AUDIT_MISSING"]
    reasons: list[str] = []
    for key in REQUIRED_TRIAL_AUDIT_FLAGS:
        if audit.get(key) is not True:
            reasons.append(f"TRIAL_AUDIT_{key.upper()}_MISSING")
    for key, accepted in STRICTER_GATE_STATUS.items():
        if str(audit.get(key) or "").upper() not in accepted:
            reasons.append(f"{key.replace('_status', '').upper()}_GATE_UNSUPPORTED")
    return reasons


def _evaluate_schema_evidence(evidence: Any, profile: Mapping[str, Any]) -> list[str]:
    if not isinstance(evidence, Mapping):
        return ["SCHEMA_EVIDENCE_MISSING"]
    required_inputs = evidence.get("required_inputs")
    if not isinstance(required_inputs, Mapping):
        return ["SCHEMA_REQUIRED_INPUTS_MISSING"]
    reasons: list[str] = []
    for name in profile.get("required_records", []):
        item = required_inputs.get(name)
        if not isinstance(item, Mapping) or str(item.get("status") or "").upper() != "PASS":
            reasons.append(f"SCHEMA_REQUIRED_INPUT_MISSING:{name}")
    if evidence.get("availability_policy") != "available_at_lte_decision_time":
        reasons.append("SCHEMA_AVAILABILITY_POLICY_MISSING")
    return reasons
