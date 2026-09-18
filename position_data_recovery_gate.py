from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import math
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd


ASSETS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
EXPECTED_INTERVAL_MS = 5 * 60 * 1_000
HOUR_MS = 60 * 60 * 1_000
HARD_WARMUP_HOURS = 30 * 24
RECOMMENDED_HISTORY_HOURS = 45 * 24

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE_ROOT = (
    ROOT
    / "data_lake_v2"
    / "raw"
    / "position_data_recovery"
    / "metrics"
)
RECOVERY_MANIFEST_PATH = (
    ROOT / "data_lake" / "position_data_recovery_manifest.json"
)
RECOVERY_REPORT_PATH = ROOT / "POSITION_DATA_RECOVERY_GATE_REPORT.md"
DEFAULT_TARGET_START_DAY = "2026-05-18"

ARCHIVE_BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"
ARCHIVE_SOURCE = "binance_public_data_archive"
RECENT_API_SOURCE = "binance_recent_public_api"
ALLOWED_SOURCE_KINDS = frozenset({ARCHIVE_SOURCE, RECENT_API_SOURCE})
RECENT_SOURCE_URLS = {
    "position_bias": (
        "https://fapi.binance.com/futures/data/"
        "topLongShortPositionRatio"
    ),
    "open_interest": (
        "https://fapi.binance.com/futures/data/openInterestHist"
    ),
}
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_CSV_BYTES = 20 * 1024 * 1024

METRICS_HEADER = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]+USDT")
CHECKSUM_PATTERN = re.compile(
    r"([0-9a-f]{64})  ([A-Z0-9]+-metrics-\d{4}-\d{2}-\d{2}\.zip)"
)


class ArchiveValidationError(ValueError):
    """Raised when a Binance Vision archive fails closed validation."""


class SourceCompatibilityError(ValueError):
    """Raised when recovered and existing sources cannot be safely combined."""


class RecoveryPublicationError(RuntimeError):
    """Raised when recovered data cannot be safely published."""


@dataclass(frozen=True)
class ArchiveMetricsRow:
    source_timestamp_ms: int
    grid_timestamp_ms: int
    open_interest: float
    open_interest_value: float
    top_account_ratio: float | None
    top_position_ratio: float | None
    global_account_ratio: float | None
    taker_ratio: float | None


@dataclass(frozen=True)
class ArchiveMetricsDay:
    symbol: str
    day: str
    archive_sha256: str
    rows: tuple[ArchiveMetricsRow, ...]

    @property
    def source_rows_count(self) -> int:
        return len(self.rows)


def _parse_day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ArchiveValidationError("archive target day is invalid") from error
    if parsed.year < 2000:
        raise ArchiveValidationError("archive target day is invalid")
    return parsed


def _day_bounds_ms(value: str) -> tuple[int, int]:
    parsed = _parse_day(value)
    start = int(
        datetime(
            parsed.year,
            parsed.month,
            parsed.day,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1_000
    )
    return start, start + 24 * HOUR_MS


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1_000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def archive_metrics_url(symbol: str, day: str) -> str:
    normalized = symbol.upper()
    _parse_day(day)
    if SYMBOL_PATTERN.fullmatch(normalized) is None:
        raise ArchiveValidationError("archive symbol is invalid")
    filename = f"{normalized}-metrics-{day}.zip"
    return f"{ARCHIVE_BASE_URL}/{normalized}/{filename}"


def _finite_nonnegative(value: str, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ArchiveValidationError(
            f"archive metrics {field} value is not numeric"
        ) from error
    if not math.isfinite(parsed) or parsed < 0:
        raise ArchiveValidationError(
            f"archive metrics {field} value is invalid"
        )
    return parsed


def _optional_ratio(value: str, *, field: str) -> float | None:
    if value == "":
        return None
    return _finite_nonnegative(value, field=field)


def verify_and_parse_metrics_archive(
    archive_payload: bytes,
    checksum_payload: bytes,
    *,
    symbol: str,
    day: str,
) -> ArchiveMetricsDay:
    normalized = symbol.upper()
    filename = archive_metrics_url(normalized, day).rsplit("/", 1)[-1]
    if not archive_payload or len(archive_payload) > MAX_ARCHIVE_BYTES:
        raise ArchiveValidationError("archive metrics ZIP size is invalid")
    try:
        checksum_text = checksum_payload.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ArchiveValidationError(
            "archive metrics checksum encoding is invalid"
        ) from error
    match = CHECKSUM_PATTERN.fullmatch(checksum_text)
    if match is None or match.group(2) != filename:
        raise ArchiveValidationError("archive metrics checksum record is invalid")
    actual_checksum = hashlib.sha256(archive_payload).hexdigest()
    if not hmac.compare_digest(match.group(1), actual_checksum):
        raise ArchiveValidationError("archive metrics checksum mismatch")

    expected_member = filename.removesuffix(".zip") + ".csv"
    try:
        with zipfile.ZipFile(io.BytesIO(archive_payload)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != expected_member:
                raise ArchiveValidationError(
                    "archive metrics ZIP member schema is invalid"
                )
            member = members[0]
            if (
                member.is_dir()
                or member.flag_bits & 1
                or member.compress_type
                not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or not 0 < member.file_size <= MAX_CSV_BYTES
            ):
                raise ArchiveValidationError(
                    "archive metrics ZIP member is unsupported"
                )
            csv_payload = archive.read(member)
    except zipfile.BadZipFile as error:
        raise ArchiveValidationError("archive metrics ZIP is invalid") from error

    try:
        text = csv_payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ArchiveValidationError(
            "archive metrics CSV encoding is invalid"
        ) from error
    parsed_rows = list(csv.reader(io.StringIO(text, newline="")))
    if not parsed_rows or tuple(parsed_rows[0]) != METRICS_HEADER:
        raise ArchiveValidationError("archive metrics CSV schema is invalid")
    data_rows = parsed_rows[1:]
    if not data_rows:
        raise ArchiveValidationError("archive metrics CSV has no rows")

    day_start, next_day_start = _day_bounds_ms(day)
    rows: list[ArchiveMetricsRow] = []
    previous_source: int | None = None
    previous_grid: int | None = None
    for raw in data_rows:
        if len(raw) != len(METRICS_HEADER):
            raise ArchiveValidationError(
                "archive metrics CSV row schema is invalid"
            )
        if raw[1] != normalized:
            raise ArchiveValidationError("archive metrics CSV symbol mismatch")
        try:
            timestamp = int(
                datetime.strptime(raw[0], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc)
                .timestamp()
                * 1_000
            )
        except ValueError as error:
            raise ArchiveValidationError(
                "archive metrics timestamp is invalid"
            ) from error
        grid_timestamp = (
            (timestamp + EXPECTED_INTERVAL_MS // 2)
            // EXPECTED_INTERVAL_MS
            * EXPECTED_INTERVAL_MS
        )
        if not day_start <= grid_timestamp <= next_day_start:
            raise ArchiveValidationError(
                "archive metrics row is outside target day"
            )
        if previous_source is not None:
            if timestamp == previous_source:
                raise ArchiveValidationError(
                    "archive metrics contains duplicate timestamps"
                )
            if timestamp < previous_source:
                raise ArchiveValidationError(
                    "archive metrics timestamps are not monotonic"
                )
        if (
            grid_timestamp < next_day_start
            and previous_grid is not None
            and grid_timestamp <= previous_grid
        ):
            raise ArchiveValidationError(
                "archive metrics normalized timestamps are not unique and monotonic"
            )
        open_interest = _finite_nonnegative(
            raw[2],
            field="sum_open_interest",
        )
        open_interest_value = _finite_nonnegative(
            raw[3],
            field="sum_open_interest_value",
        )
        rows.append(
            ArchiveMetricsRow(
                source_timestamp_ms=timestamp,
                grid_timestamp_ms=grid_timestamp,
                open_interest=open_interest,
                open_interest_value=open_interest_value,
                top_account_ratio=_optional_ratio(
                    raw[4],
                    field="count_toptrader_long_short_ratio",
                ),
                top_position_ratio=_optional_ratio(
                    raw[5],
                    field="sum_toptrader_long_short_ratio",
                ),
                global_account_ratio=_optional_ratio(
                    raw[6],
                    field="count_long_short_ratio",
                ),
                taker_ratio=_optional_ratio(
                    raw[7],
                    field="sum_taker_long_short_vol_ratio",
                ),
            )
        )
        previous_source = timestamp
        if grid_timestamp < next_day_start:
            previous_grid = grid_timestamp
    return ArchiveMetricsDay(
        symbol=normalized,
        day=day,
        archive_sha256=actual_checksum,
        rows=tuple(rows),
    )


def archive_day_frames(day: ArchiveMetricsDay) -> dict[str, pd.DataFrame]:
    day_start, next_day_start = _day_bounds_ms(day.day)
    open_interest_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    for row in day.rows:
        if not day_start <= row.grid_timestamp_ms < next_day_start:
            continue
        provenance = {
            "symbol": day.symbol,
            "timestamp": row.grid_timestamp_ms,
            "datetime": _iso(row.grid_timestamp_ms),
            "interval": "5m",
            "source_kind": ARCHIVE_SOURCE,
            "source_day": day.day,
            "source_checksum": day.archive_sha256,
            "source_timestamp": row.source_timestamp_ms,
        }
        open_interest_rows.append(
            {
                **provenance,
                "dataset": "open_interest",
                "open_interest": row.open_interest,
                "open_interest_value": row.open_interest_value,
            }
        )
        if row.top_position_ratio is not None:
            ratio = row.top_position_ratio
            position_rows.append(
                {
                    **provenance,
                    "dataset": "top_position_long_short",
                    "long_short_ratio": ratio,
                    "long_position": None,
                    "short_position": None,
                }
            )
    return {
        "open_interest": pd.DataFrame(open_interest_rows),
        "position_bias": pd.DataFrame(position_rows),
    }


def _validate_merge_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    value_columns: Sequence[str],
) -> None:
    required = {
        "symbol",
        "timestamp",
        "source_kind",
        "source_checksum",
        *value_columns,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise SourceCompatibilityError(
            f"source schema is missing columns: {missing}"
        )
    if frame.empty:
        return
    if set(frame["symbol"].astype(str)) != {symbol.upper()}:
        raise SourceCompatibilityError("source symbol provenance is invalid")
    invalid_sources = sorted(
        set(frame["source_kind"].astype(str)) - ALLOWED_SOURCE_KINDS
    )
    if invalid_sources:
        raise SourceCompatibilityError(
            f"source provenance is not verified: {invalid_sources}"
        )
    checksum_valid = frame["source_checksum"].astype(str).str.fullmatch(
        r"[0-9a-f]{64}"
    )
    if not bool(checksum_valid.all()):
        raise SourceCompatibilityError("source checksum provenance is invalid")
    timestamps = pd.to_numeric(frame["timestamp"], errors="coerce")
    if timestamps.isna().any():
        raise SourceCompatibilityError("source timestamp is invalid")
    if timestamps.duplicated().any():
        raise SourceCompatibilityError("source timestamps contain duplicates")
    if not timestamps.is_monotonic_increasing:
        raise SourceCompatibilityError("source timestamps are not monotonic")
    for column in value_columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy()).all():
            raise SourceCompatibilityError(
                f"source {column} values are invalid"
            )


def _mismatch_mask(
    left: pd.Series,
    right: pd.Series,
    *,
    relative_tolerance: float,
    absolute_tolerance: float,
) -> np.ndarray:
    left_values = pd.to_numeric(left, errors="coerce").to_numpy(dtype=float)
    right_values = pd.to_numeric(right, errors="coerce").to_numpy(dtype=float)
    return ~np.isclose(
        left_values,
        right_values,
        rtol=relative_tolerance,
        atol=absolute_tolerance,
        equal_nan=False,
    )


def compare_source_overlap(
    recent: pd.DataFrame,
    archive: pd.DataFrame,
    *,
    value_columns: Sequence[str],
    relative_tolerance: float = 1e-9,
    absolute_tolerance: float = 1e-12,
    decimal_places: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    required = {"symbol", "timestamp", *value_columns}
    for name, frame in (("recent", recent), ("archive", archive)):
        missing = sorted(required - set(frame.columns))
        if missing:
            raise SourceCompatibilityError(
                f"{name} overlap schema is missing columns: {missing}"
            )
        timestamps = pd.to_numeric(frame["timestamp"], errors="coerce")
        if timestamps.isna().any() or timestamps.duplicated().any():
            raise SourceCompatibilityError(
                f"{name} overlap timestamps are invalid"
            )
        if frame["symbol"].astype(str).nunique() != 1:
            raise SourceCompatibilityError(
                f"{name} overlap symbol identity is invalid"
            )
    recent_symbol = str(recent["symbol"].iloc[0]) if not recent.empty else ""
    archive_symbol = str(archive["symbol"].iloc[0]) if not archive.empty else ""
    if recent_symbol != archive_symbol:
        raise SourceCompatibilityError("overlap symbol identity does not match")
    overlap = recent[["timestamp", *value_columns]].merge(
        archive[["timestamp", *value_columns]],
        on="timestamp",
        how="inner",
        suffixes=("_recent", "_archive"),
        validate="one_to_one",
    )
    if overlap.empty:
        raise SourceCompatibilityError(
            "source overlap has no common timestamps"
        )
    details: dict[str, dict[str, Any]] = {}
    for column in value_columns:
        recent_column = overlap[f"{column}_recent"]
        archive_column = overlap[f"{column}_archive"]
        places = (
            None
            if decimal_places is None
            else decimal_places.get(column)
        )
        if places is None:
            mismatches = _mismatch_mask(
                recent_column,
                archive_column,
                relative_tolerance=relative_tolerance,
                absolute_tolerance=absolute_tolerance,
            )
        else:
            if not 0 <= int(places) <= 15:
                raise ValueError(
                    f"invalid decimal precision for {column}: {places}"
                )
            recent_values = pd.to_numeric(
                recent_column,
                errors="coerce",
            ).to_numpy(dtype=float)
            archive_values = pd.to_numeric(
                archive_column,
                errors="coerce",
            ).to_numpy(dtype=float)
            representation_tolerance = 0.5 * 10 ** (-int(places))
            floating_slack = np.finfo(float).eps * np.maximum(
                1.0,
                np.maximum(
                    np.abs(recent_values),
                    np.abs(archive_values),
                ),
            )
            mismatches = (
                np.abs(recent_values - archive_values)
                > representation_tolerance + floating_slack
            )
        differences = np.abs(
            pd.to_numeric(recent_column).to_numpy(dtype=float)
            - pd.to_numeric(archive_column).to_numpy(dtype=float)
        )
        details[column] = {
            "matching_rows": int((~mismatches).sum()),
            "mismatching_rows": int(mismatches.sum()),
            "maximum_absolute_difference": float(differences.max()),
            "comparison_decimal_places": places,
        }
        if bool(mismatches.any()):
            raise SourceCompatibilityError(
                f"source overlap mismatch for {column}"
            )
    return {
        "compatible": True,
        "symbol": recent_symbol,
        "overlap_rows": int(len(overlap)),
        "columns": details,
    }


def merge_verified_rows(
    existing: pd.DataFrame,
    recovered: pd.DataFrame,
    *,
    symbol: str,
    value_columns: Sequence[str],
    relative_tolerance: float = 1e-9,
    absolute_tolerance: float = 1e-12,
    decimal_places: Mapping[str, int] | None = None,
) -> pd.DataFrame:
    normalized = symbol.upper()
    _validate_merge_frame(
        existing,
        symbol=normalized,
        value_columns=value_columns,
    )
    _validate_merge_frame(
        recovered,
        symbol=normalized,
        value_columns=value_columns,
    )
    overlapping = set(
        pd.to_numeric(existing["timestamp"], errors="raise").astype("int64")
    ) & set(
        pd.to_numeric(recovered["timestamp"], errors="raise").astype("int64")
    )
    if overlapping:
        compare_source_overlap(
            existing[existing["timestamp"].isin(overlapping)],
            recovered[recovered["timestamp"].isin(overlapping)],
            value_columns=value_columns,
            relative_tolerance=relative_tolerance,
            absolute_tolerance=absolute_tolerance,
            decimal_places=decimal_places,
        )
    new_rows = recovered[~recovered["timestamp"].isin(overlapping)]
    output = pd.concat([existing, new_rows], ignore_index=True, sort=False)
    output["timestamp"] = pd.to_numeric(
        output["timestamp"],
        errors="raise",
    ).astype("int64")
    output = output.sort_values("timestamp").reset_index(drop=True)
    if output["timestamp"].duplicated().any():
        raise SourceCompatibilityError(
            "merged source contains duplicate timestamps"
        )
    return output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_public_bytes(
    url: str,
    max_bytes: int,
    *,
    timeout: float = 30.0,
) -> bytes:
    import requests

    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ArchiveValidationError(
                f"download exceeds maximum size: {declared} > {max_bytes}"
            )
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ArchiveValidationError(
                    f"download exceeds maximum size: {size} > {max_bytes}"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def cache_archive_object(
    cache_root: Path,
    *,
    symbol: str,
    day: str,
    fetcher: Callable[[str, int], bytes] = _download_public_bytes,
) -> dict[str, Any]:
    normalized = symbol.upper()
    url = archive_metrics_url(normalized, day)
    filename = url.rsplit("/", 1)[-1]
    object_dir = cache_root / normalized
    archive_path = object_dir / filename
    checksum_path = object_dir / f"{filename}.CHECKSUM"
    archive_cached = archive_path.is_file()
    checksum_cached = checksum_path.is_file()
    archive_payload = (
        archive_path.read_bytes()
        if archive_cached
        else fetcher(url, MAX_ARCHIVE_BYTES)
    )
    checksum_payload = (
        checksum_path.read_bytes()
        if checksum_cached
        else fetcher(f"{url}.CHECKSUM", 1024)
    )
    parsed = verify_and_parse_metrics_archive(
        archive_payload,
        checksum_payload,
        symbol=normalized,
        day=day,
    )
    if not archive_cached or not checksum_cached:
        object_dir.mkdir(parents=True, exist_ok=True)
        pending: list[tuple[Path, bytes]] = []
        if not archive_cached:
            pending.append((archive_path, archive_payload))
        if not checksum_cached:
            pending.append((checksum_path, checksum_payload))
        temporary_paths: list[tuple[Path, Path]] = []
        try:
            for target, content in pending:
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(content)
                temporary_paths.append((temporary, target))
            for temporary, target in temporary_paths:
                temporary.replace(target)
        finally:
            for temporary, _ in temporary_paths:
                if temporary.exists():
                    temporary.unlink()
    return {
        "cache_status": (
            "cached" if archive_cached and checksum_cached else "downloaded"
        ),
        "symbol": normalized,
        "day": day,
        "url": url,
        "archive_path": str(archive_path),
        "checksum_path": str(checksum_path),
        "archive_sha256": parsed.archive_sha256,
        "source_rows": parsed.source_rows_count,
        "parsed": parsed,
    }


def recovery_archive_days(
    *,
    target_start_day: str,
    existing_start_ms: int,
) -> tuple[str, ...]:
    start = _parse_day(target_start_day)
    existing_day = datetime.fromtimestamp(
        int(existing_start_ms) / 1_000,
        tz=timezone.utc,
    ).date()
    if existing_day < start:
        raise ValueError("existing source starts before recovery target")
    result: list[str] = []
    current = start
    while current <= existing_day:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(result)


def _read_recent_csv(
    path: Path,
    *,
    symbol: str,
    dataset: str,
) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"missing recent source: {path}")
    checksum = _sha256_file(path)
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    normalized = symbol.upper()
    if dataset == "position_bias":
        required = {
            "timestamp",
            "longShortRatio",
            "longAccount",
            "shortAccount",
        }
        missing = sorted(required - set(raw.columns))
        if missing:
            raise SourceCompatibilityError(
                f"recent position schema is missing columns: {missing}"
            )
        frame = pd.DataFrame(
            {
                "symbol": normalized,
                "timestamp": raw["timestamp"],
                "datetime": pd.to_numeric(
                    raw["timestamp"],
                    errors="raise",
                ).map(lambda value: _iso(int(value))),
                "dataset": "top_position_long_short",
                "interval": "5m",
                "long_short_ratio": raw["longShortRatio"],
                "long_position": raw["longAccount"],
                "short_position": raw["shortAccount"],
            }
        )
        value_columns = ("long_short_ratio",)
    elif dataset == "open_interest":
        required = {
            "timestamp",
            "sumOpenInterest",
            "sumOpenInterestValue",
        }
        missing = sorted(required - set(raw.columns))
        if missing:
            raise SourceCompatibilityError(
                f"recent open-interest schema is missing columns: {missing}"
            )
        frame = pd.DataFrame(
            {
                "symbol": normalized,
                "timestamp": raw["timestamp"],
                "datetime": pd.to_numeric(
                    raw["timestamp"],
                    errors="raise",
                ).map(lambda value: _iso(int(value))),
                "dataset": "open_interest",
                "interval": "5m",
                "open_interest": raw["sumOpenInterest"],
                "open_interest_value": raw["sumOpenInterestValue"],
            }
        )
        value_columns = ("open_interest", "open_interest_value")
    else:
        raise ValueError(f"unsupported recent dataset: {dataset}")
    frame["source_kind"] = RECENT_API_SOURCE
    frame["source_checksum"] = checksum
    frame["timestamp"] = pd.to_numeric(
        frame["timestamp"],
        errors="raise",
    ).astype("int64")
    _validate_merge_frame(
        frame,
        symbol=normalized,
        value_columns=value_columns,
    )
    return frame


def _parse_iso_ms(value: str) -> int:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise RecoveryPublicationError(
            "provenance timestamp is not timezone aware"
        )
    return int(parsed.tz_convert("UTC").value // 1_000_000)


def _restore_published_provenance(
    root: Path,
    *,
    symbol: str,
    dataset: str,
    path: Path,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    manifest_path = (
        root / "data_lake" / "position_data_recovery_manifest.json"
    )
    if not manifest_path.is_file():
        return frame
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecoveryPublicationError(
            "published recovery manifest cannot be read"
        ) from error
    publication = manifest.get("publication", {})
    if (
        publication.get("status")
        != "PUBLISHED_TO_DATA_LAKE_AND_FEATURE_STORE"
    ):
        return frame
    relative = str(path.relative_to(root)).replace("\\", "/")
    expected_checksum = publication.get("files", {}).get(relative)
    if (
        not isinstance(expected_checksum, str)
        or not hmac.compare_digest(
            expected_checksum,
            _sha256_file(path),
        )
    ):
        raise RecoveryPublicationError(
            f"published source checksum changed: {relative}"
        )
    inventory = [
        item
        for item in manifest.get("source_inventory", [])
        if isinstance(item, dict)
        and item.get("asset") == symbol
        and item.get("dataset") == dataset
    ]
    recent_entries = [
        item
        for item in inventory
        if item.get("source") == RECENT_API_SOURCE
    ]
    archive_entries = [
        item
        for item in inventory
        if item.get("source") == ARCHIVE_SOURCE
    ]
    if not recent_entries or not archive_entries:
        raise RecoveryPublicationError(
            f"published provenance inventory is incomplete: {symbol} {dataset}"
        )

    ranges: list[tuple[int, int, str, str]] = []
    for item in [*recent_entries, *archive_entries]:
        checksum = str(item.get("checksum", ""))
        if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise RecoveryPublicationError(
                "published provenance checksum is invalid"
            )
        ranges.append(
            (
                _parse_iso_ms(str(item.get("start", ""))),
                _parse_iso_ms(str(item.get("end", ""))),
                str(item.get("source")),
                checksum,
            )
        )
    restored = frame.copy()
    source_kinds: list[str] = []
    source_checksums: list[str] = []
    for raw_timestamp in restored["timestamp"]:
        timestamp = int(raw_timestamp)
        matches = [
            item
            for item in ranges
            if item[0] <= timestamp <= item[1]
        ]
        recent_match = next(
            (
                item
                for item in matches
                if item[2] == RECENT_API_SOURCE
            ),
            None,
        )
        selected = recent_match or next(
            (
                item
                for item in matches
                if item[2] == ARCHIVE_SOURCE
            ),
            None,
        )
        if selected is None:
            raise RecoveryPublicationError(
                f"published row has no provenance: {symbol} {dataset} {timestamp}"
            )
        source_kinds.append(selected[2])
        source_checksums.append(selected[3])
    restored["source_kind"] = source_kinds
    restored["source_checksum"] = source_checksums
    return restored


def read_recent_sources(root: Path, symbol: str) -> dict[str, pd.DataFrame]:
    normalized = symbol.upper()
    feature_root = root / "data" / "futures_features"
    position_path = (
        feature_root / f"{normalized}_top_position_long_short.csv"
    )
    oi_path = feature_root / f"{normalized}_open_interest.csv"
    position = _read_recent_csv(
        position_path,
        symbol=normalized,
        dataset="position_bias",
    )
    open_interest = _read_recent_csv(
        oi_path,
        symbol=normalized,
        dataset="open_interest",
    )
    position = _restore_published_provenance(
        root,
        symbol=normalized,
        dataset="position_bias",
        path=position_path,
        frame=position,
    )
    open_interest = _restore_published_provenance(
        root,
        symbol=normalized,
        dataset="open_interest",
        path=oi_path,
        frame=open_interest,
    )
    _validate_merge_frame(
        position,
        symbol=normalized,
        value_columns=("long_short_ratio",),
    )
    _validate_merge_frame(
        open_interest,
        symbol=normalized,
        value_columns=("open_interest", "open_interest_value"),
    )
    return {
        "position_bias": position,
        "open_interest": open_interest,
    }


def _csv_text(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> str:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise RecoveryPublicationError(
            f"recovered frame is missing publication columns: {missing}"
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for values in frame.loc[:, list(columns)].itertuples(
        index=False,
        name=None,
    ):
        writer.writerow(
            "" if value is None or pd.isna(value) else value
            for value in values
        )
    return output.getvalue()


def _publication_payloads(
    root: Path,
    frames: Mapping[str, Mapping[str, pd.DataFrame]],
) -> dict[Path, bytes]:
    payloads: dict[Path, bytes] = {}
    for asset in ASSETS:
        if asset not in frames:
            raise RecoveryPublicationError(
                f"recovered publication missing asset: {asset}"
            )
        position = frames[asset].get("position_bias")
        open_interest = frames[asset].get("open_interest")
        if position is None or open_interest is None:
            raise RecoveryPublicationError(
                f"recovered publication missing dataset: {asset}"
            )
        position = position.sort_values("timestamp").reset_index(drop=True)
        open_interest = open_interest.sort_values("timestamp").reset_index(
            drop=True
        )
        _validate_merge_frame(
            position,
            symbol=asset,
            value_columns=("long_short_ratio",),
        )
        _validate_merge_frame(
            open_interest,
            symbol=asset,
            value_columns=("open_interest", "open_interest_value"),
        )
        raw_position = position.rename(
            columns={
                "long_short_ratio": "longShortRatio",
                "long_position": "longAccount",
                "short_position": "shortAccount",
            }
        )
        raw_oi = open_interest.rename(
            columns={
                "open_interest": "sumOpenInterest",
                "open_interest_value": "sumOpenInterestValue",
            }
        )
        position_canonical_columns = (
            "symbol",
            "timestamp",
            "datetime",
            "dataset",
            "interval",
            "long_short_ratio",
            "long_position",
            "short_position",
        )
        oi_canonical_columns = (
            "symbol",
            "timestamp",
            "datetime",
            "dataset",
            "interval",
            "open_interest",
            "open_interest_value",
        )
        data_dir = root / "data" / "futures_features"
        lake_dir = root / "data_lake" / "futures_features"
        position_store_dir = root / "feature_store" / "long_short"
        oi_store_dir = root / "feature_store" / "open_interest"
        position_raw_path = (
            data_dir / f"{asset}_top_position_long_short.csv"
        )
        oi_raw_path = data_dir / f"{asset}_open_interest.csv"
        position_lake_path = (
            lake_dir / f"{asset}_top_position_long_short.csv"
        )
        oi_lake_path = lake_dir / f"{asset}_open_interest.csv"
        position_store_path = (
            position_store_dir
            / f"{asset}_top_position_long_short_5m.csv"
        )
        oi_store_path = oi_store_dir / f"{asset}_open_interest_5m.csv"
        payloads[position_raw_path] = _csv_text(
            raw_position,
            (
                "timestamp",
                "longShortRatio",
                "longAccount",
                "shortAccount",
            ),
        ).encode("utf-8")
        payloads[oi_raw_path] = _csv_text(
            raw_oi,
            (
                "timestamp",
                "sumOpenInterest",
                "sumOpenInterestValue",
            ),
        ).encode("utf-8")
        position_canonical = _csv_text(
            position,
            position_canonical_columns,
        ).encode("utf-8")
        oi_canonical = _csv_text(
            open_interest,
            oi_canonical_columns,
        ).encode("utf-8")
        payloads[position_lake_path] = position_canonical
        payloads[position_store_path] = position_canonical
        payloads[oi_lake_path] = oi_canonical
        payloads[oi_store_path] = oi_canonical
    return payloads


def publish_recovered_sources(
    root: Path,
    frames: Mapping[str, Mapping[str, pd.DataFrame]],
    *,
    gate_status: str,
) -> dict[str, str]:
    if gate_status != "READY_FOR_FROZEN_RERUN":
        raise RecoveryPublicationError(
            f"recovery gate is {gate_status}; publication is blocked"
        )
    payloads = _publication_payloads(root, frames)
    temporary_paths: list[tuple[Path, Path]] = []
    try:
        for target, content in payloads.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".recovery.tmp")
            temporary.write_bytes(content)
            temporary_paths.append((temporary, target))
        for temporary, target in temporary_paths:
            temporary.replace(target)
    finally:
        for temporary, _ in temporary_paths:
            if temporary.exists():
                temporary.unlink()
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            content
        ).hexdigest()
        for path, content in sorted(
            payloads.items(),
            key=lambda item: str(item[0]),
        )
    }


def align_to_closed_hours(
    metrics: pd.DataFrame,
    decision_closes: Sequence[int],
    *,
    value_column: str,
) -> pd.DataFrame:
    required = {"timestamp", value_column}
    missing = sorted(required - set(metrics.columns))
    if missing:
        raise ValueError(f"metric frame missing columns: {missing}")
    right = metrics[["timestamp", value_column]].copy()
    right["timestamp"] = pd.to_numeric(right["timestamp"], errors="raise").astype(
        "int64"
    )
    if right["timestamp"].duplicated().any():
        raise ValueError("metric frame contains duplicate timestamps")
    if not right["timestamp"].is_monotonic_increasing:
        raise ValueError("metric frame timestamps are not monotonic")
    right = right.rename(columns={"timestamp": "source_timestamp"})
    left = pd.DataFrame(
        {
            "decision_timestamp": pd.Series(
                list(decision_closes),
                dtype="int64",
            )
        }
    )
    if left["decision_timestamp"].duplicated().any():
        raise ValueError("decision timestamps contain duplicates")
    if not left["decision_timestamp"].is_monotonic_increasing:
        raise ValueError("decision timestamps are not monotonic")
    aligned = pd.merge_asof(
        left,
        right,
        left_on="decision_timestamp",
        right_on="source_timestamp",
        direction="backward",
        tolerance=HOUR_MS,
        allow_exact_matches=True,
    )
    aligned = aligned.dropna(
        subset=["source_timestamp", value_column]
    ).reset_index(drop=True)
    aligned["source_timestamp"] = aligned["source_timestamp"].astype("int64")
    if bool(
        (aligned["source_timestamp"] > aligned["decision_timestamp"]).any()
    ):
        raise ValueError("closed-hour alignment used future data")
    return aligned


def _frame_sequence_checksum(
    frame: pd.DataFrame,
    *,
    value_column: str,
) -> str:
    ordered = frame[["timestamp", value_column]].copy()
    ordered["timestamp"] = pd.to_numeric(
        ordered["timestamp"],
        errors="raise",
    ).astype("int64")
    ordered[value_column] = pd.to_numeric(
        ordered[value_column],
        errors="raise",
    )
    payload = ordered.sort_values("timestamp").to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.17g",
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit_cross_asset_identity(
    frames: Mapping[str, pd.DataFrame],
    *,
    value_column: str,
    assets: Sequence[str] = ASSETS,
) -> dict[str, Any]:
    missing_assets = [asset for asset in assets if asset not in frames]
    pairs: list[dict[str, Any]] = []
    verified = not missing_assets
    for first, second in combinations(assets, 2):
        if first not in frames or second not in frames:
            continue
        left = frames[first]
        right = frames[second]
        for asset, frame in ((first, left), (second, right)):
            required = {"symbol", "timestamp", value_column}
            missing = sorted(required - set(frame.columns))
            if missing:
                raise SourceCompatibilityError(
                    f"{asset} identity schema is missing columns: {missing}"
                )
            if not frame.empty and set(frame["symbol"].astype(str)) != {asset}:
                raise SourceCompatibilityError(
                    f"{asset} identity symbol provenance is invalid"
                )
        left_checksum = _frame_sequence_checksum(
            left,
            value_column=value_column,
        )
        right_checksum = _frame_sequence_checksum(
            right,
            value_column=value_column,
        )
        left_ordered = left[["timestamp", value_column]].sort_values(
            "timestamp"
        )
        right_ordered = right[["timestamp", value_column]].sort_values(
            "timestamp"
        )
        same_timestamps = left_ordered["timestamp"].reset_index(
            drop=True
        ).equals(
            right_ordered["timestamp"].reset_index(drop=True)
        )
        values_identical = bool(
            same_timestamps
            and np.array_equal(
                pd.to_numeric(left_ordered[value_column]).to_numpy(dtype=float),
                pd.to_numeric(right_ordered[value_column]).to_numpy(dtype=float),
                equal_nan=True,
            )
        )
        overlap = left_ordered.merge(
            right_ordered,
            on="timestamp",
            how="inner",
            suffixes=("_left", "_right"),
        )
        if overlap.empty:
            exact_matches = 0
            correlation = None
        else:
            left_values = pd.to_numeric(
                overlap[f"{value_column}_left"],
            ).to_numpy(dtype=float)
            right_values = pd.to_numeric(
                overlap[f"{value_column}_right"],
            ).to_numpy(dtype=float)
            exact_matches = int(
                np.isclose(
                    left_values,
                    right_values,
                    rtol=0,
                    atol=0,
                    equal_nan=True,
                ).sum()
            )
            correlation_value = pd.Series(left_values).corr(
                pd.Series(right_values)
            )
            correlation = (
                None
                if pd.isna(correlation_value)
                else round(float(correlation_value), 12)
            )
        checksums_identical = hmac.compare_digest(
            left_checksum,
            right_checksum,
        )
        if values_identical or checksums_identical:
            verified = False
        pairs.append(
            {
                "assets": f"{first}/{second}",
                "left_sequence_checksum": left_checksum,
                "right_sequence_checksum": right_checksum,
                "checksums_identical": checksums_identical,
                "values_identical": values_identical,
                "overlap_rows": int(len(overlap)),
                "exact_value_matches": exact_matches,
                "correlation": correlation,
            }
        )
    return {
        "verified": verified,
        "missing_assets": missing_assets,
        "pairs": pairs,
    }


def history_gate(hourly_observations: int) -> dict[str, Any]:
    observations = int(hourly_observations)
    hard_pass = observations >= HARD_WARMUP_HOURS
    recommended_pass = observations >= RECOMMENDED_HISTORY_HOURS
    return {
        "hourly_observations": observations,
        "hard_warmup_pass": hard_pass,
        "recommended_history_pass": recommended_pass,
        "eligible_for_frozen_rerun": hard_pass and recommended_pass,
    }


def _timestamp_quality(frame: pd.DataFrame) -> dict[str, Any]:
    timestamps = pd.to_numeric(
        frame.get("timestamp"),
        errors="coerce",
    )
    invalid = int(timestamps.isna().sum())
    valid = timestamps.dropna().astype("int64")
    duplicates = int(valid.duplicated().sum())
    non_monotonic = int(not valid.is_monotonic_increasing)
    off_grid = int((valid % EXPECTED_INTERVAL_MS != 0).sum())
    deltas = valid.diff().dropna()
    gap_deltas = deltas[deltas > EXPECTED_INTERVAL_MS]
    missing_samples = int(
        sum(
            max(0, int(delta) // EXPECTED_INTERVAL_MS - 1)
            for delta in gap_deltas
        )
    )
    unexpected_short_intervals = int(
        ((deltas > 0) & (deltas < EXPECTED_INTERVAL_MS)).sum()
    )
    verified = (
        invalid == 0
        and duplicates == 0
        and non_monotonic == 0
        and off_grid == 0
        and unexpected_short_intervals == 0
    )
    return {
        "verified": verified,
        "invalid_timestamps": invalid,
        "duplicate_timestamps": duplicates,
        "non_monotonic_timestamps": non_monotonic,
        "off_grid_timestamps": off_grid,
        "gap_count": int(len(gap_deltas)),
        "missing_five_minute_samples": missing_samples,
        "unexpected_short_intervals": unexpected_short_intervals,
    }


def _symbol_provenance_verified(
    frame: pd.DataFrame,
    *,
    symbol: str,
) -> bool:
    required = {
        "symbol",
        "source_kind",
        "source_checksum",
    }
    if frame.empty or not required.issubset(frame.columns):
        return False
    symbols = set(frame["symbol"].astype(str))
    source_kinds = set(frame["source_kind"].astype(str))
    checksums = frame["source_checksum"].astype(str)
    return bool(
        symbols == {symbol}
        and source_kinds.issubset(ALLOWED_SOURCE_KINDS)
        and checksums.str.fullmatch(r"[0-9a-f]{64}").all()
    )


def _read_decision_closes(root: Path, asset: str) -> list[int]:
    path = root / "data" / "klines" / f"{asset}_1h.csv"
    if not path.is_file():
        raise FileNotFoundError(f"missing frozen OHLCV source: {path}")
    frame = pd.read_csv(path, usecols=["close_time"])
    timestamps = pd.to_numeric(
        frame["close_time"],
        errors="coerce",
    )
    if (
        timestamps.isna().any()
        or timestamps.duplicated().any()
        or not timestamps.is_monotonic_increasing
    ):
        raise ValueError(f"{asset} OHLCV decision timestamps are invalid")
    return timestamps.astype("int64").tolist()


def evaluate_staged_recovery(
    root: Path,
    frames: Mapping[str, Mapping[str, pd.DataFrame]],
    *,
    overlap_checks: Mapping[str, Mapping[str, Any]],
    source_inventory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    asset_results: dict[str, dict[str, Any]] = {}
    aligned_decisions: dict[str, pd.Index] = {}
    for asset in ASSETS:
        asset_frames = frames.get(asset)
        if asset_frames is None:
            continue
        position = asset_frames.get("position_bias")
        open_interest = asset_frames.get("open_interest")
        if position is None or open_interest is None:
            continue
        decisions = _read_decision_closes(root, asset)
        aligned_position = align_to_closed_hours(
            position,
            decisions,
            value_column="long_short_ratio",
        )
        aligned_oi = align_to_closed_hours(
            open_interest,
            decisions,
            value_column="open_interest",
        )
        common = aligned_position[["decision_timestamp"]].merge(
            aligned_oi[["decision_timestamp"]],
            on="decision_timestamp",
            how="inner",
            validate="one_to_one",
        )
        decision_index = pd.Index(
            common["decision_timestamp"].astype("int64"),
            name="decision_timestamp",
        )
        aligned_decisions[asset] = decision_index
        position_quality = _timestamp_quality(position)
        oi_quality = _timestamp_quality(open_interest)
        timestamp_verified = bool(
            position_quality["verified"] and oi_quality["verified"]
        )
        provenance_verified = bool(
            _symbol_provenance_verified(position, symbol=asset)
            and _symbol_provenance_verified(open_interest, symbol=asset)
        )
        overlap = overlap_checks.get(asset, {})
        overlap_verified = overlap.get("status") == "PASS"
        history = history_gate(len(decision_index))
        position_timestamps = pd.to_numeric(
            position["timestamp"],
            errors="coerce",
        )
        oi_timestamps = pd.to_numeric(
            open_interest["timestamp"],
            errors="coerce",
        )
        asset_results[asset] = {
            **history,
            "rows": int(
                min(
                    len(position),
                    len(open_interest),
                )
            ),
            "position_rows": int(len(position)),
            "open_interest_rows": int(len(open_interest)),
            "start": (
                _iso(
                    int(
                        max(
                            position_timestamps.min(),
                            oi_timestamps.min(),
                        )
                    )
                )
                if len(position) and len(open_interest)
                else ""
            ),
            "end": (
                _iso(
                    int(
                        min(
                            position_timestamps.max(),
                            oi_timestamps.max(),
                        )
                    )
                )
                if len(position) and len(open_interest)
                else ""
            ),
            "first_closed_hour": (
                _iso(int(decision_index[0])) if len(decision_index) else ""
            ),
            "last_closed_hour": (
                _iso(int(decision_index[-1])) if len(decision_index) else ""
            ),
            "symbol_provenance_verified": provenance_verified,
            "timestamps_verified": timestamp_verified,
            "source_compatibility_verified": overlap_verified,
            "non_identity_verified": False,
            "position_timestamp_quality": position_quality,
            "open_interest_timestamp_quality": oi_quality,
        }

    position_identity = audit_cross_asset_identity(
        {
            asset: frames[asset]["position_bias"]
            for asset in ASSETS
            if asset in frames and "position_bias" in frames[asset]
        },
        value_column="long_short_ratio",
    )
    oi_identity = audit_cross_asset_identity(
        {
            asset: frames[asset]["open_interest"]
            for asset in ASSETS
            if asset in frames and "open_interest" in frames[asset]
        },
        value_column="open_interest",
    )
    identity_verified = bool(
        position_identity["verified"] and oi_identity["verified"]
    )
    for result in asset_results.values():
        result["non_identity_verified"] = identity_verified

    reference_asset = next(
        (asset for asset in ASSETS if asset in aligned_decisions),
        "",
    )
    reference = aligned_decisions.get(reference_asset)
    alignment_verified = bool(reference_asset)
    alignment_counts: dict[str, int] = {}
    for asset in ASSETS:
        timestamps = aligned_decisions.get(asset)
        alignment_counts[asset] = 0 if timestamps is None else len(timestamps)
        if (
            timestamps is None
            or reference is None
            or not reference.equals(timestamps)
        ):
            alignment_verified = False
    gate = evaluate_recovery_gate(asset_results)
    blockers = list(gate["blockers"])
    if not alignment_verified:
        blockers.append("cross_asset_closed_hour_alignment")
    status = (
        "READY_FOR_FROZEN_RERUN"
        if not blockers
        else "BLOCKED"
    )
    return {
        "schema_version": 1,
        "phase": "Phase B1R-D",
        "status": status,
        "replication_effect": "NOT_EVALUATED",
        "direction": "NOT_EVALUATED",
        "assets": asset_results,
        "cross_asset_alignment": {
            "verified": alignment_verified,
            "reference_asset": reference_asset,
            "hourly_observations": alignment_counts,
        },
        "cross_asset_identity": {
            "verified": identity_verified,
            "pairs": position_identity.get("pairs", []),
            "datasets": {
                "position_bias": position_identity,
                "open_interest": oi_identity,
            },
        },
        "source_inventory": [
            dict(item)
            for item in sorted(
                source_inventory,
                key=lambda row: (
                    str(row.get("asset", "")),
                    str(row.get("source", "")),
                    str(row.get("dataset", "")),
                    str(row.get("day", "")),
                ),
            )
        ],
        "overlap_checks": {
            asset: dict(overlap_checks.get(asset, {}))
            for asset in ASSETS
        },
        "policy": {
            "metric": "top_position_long_short",
            "period": "5m",
            "warmup_required_hours": HARD_WARMUP_HOURS,
            "recommended_history_hours": RECOMMENDED_HISTORY_HOURS,
            "no_forward_fill": True,
            "no_interpolation": True,
            "no_synthetic_rows": True,
            "no_cross_symbol_copying": True,
            "frozen_study_modified": False,
            "frozen_study_ran": False,
        },
        "blockers": sorted(set(blockers)),
    }


def _source_inventory_record(
    frame: pd.DataFrame,
    *,
    asset: str,
    dataset: str,
    source: str,
    checksum: str,
    status: str,
    day: str = "",
    url: str = "",
) -> dict[str, Any]:
    timestamps = pd.to_numeric(
        frame.get("timestamp"),
        errors="coerce",
    ).dropna()
    return {
        "asset": asset,
        "source": source,
        "dataset": dataset,
        "day": day,
        "rows": int(len(frame)),
        "start": _iso(int(timestamps.min())) if len(timestamps) else "",
        "end": _iso(int(timestamps.max())) if len(timestamps) else "",
        "checksum": checksum,
        "status": status,
        "url": url,
    }


def _load_archive_scope(
    cache_root: Path,
    scope: Sequence[tuple[str, str]],
    *,
    workers: int,
    fetcher: Callable[[str, int], bytes],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str]]:
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    results: dict[tuple[str, str], dict[str, Any]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {
            executor.submit(
                cache_archive_object,
                cache_root,
                symbol=asset,
                day=day,
                fetcher=fetcher,
            ): (asset, day)
            for asset, day in scope
        }
        for future in as_completed(pending):
            asset, day = pending[future]
            try:
                results[(asset, day)] = future.result()
            except Exception as error:
                failures[f"{asset}:{day}"] = (
                    f"{type(error).__name__}: {error}"
                )
    return results, dict(sorted(failures.items()))


def stage_recovery(
    root: Path = ROOT,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    target_start_day: str = DEFAULT_TARGET_START_DAY,
    workers: int = 6,
    fetcher: Callable[[str, int], bytes] = _download_public_bytes,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, pd.DataFrame]],
]:
    recent_by_asset = {
        asset: read_recent_sources(root, asset)
        for asset in ASSETS
    }
    manifest_path = (
        root / "data_lake" / "position_data_recovery_manifest.json"
    )
    if manifest_path.is_file():
        try:
            previous = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RecoveryPublicationError(
                "existing recovery manifest cannot be read"
            ) from error
        if (
            previous.get("publication", {}).get("status")
            == "PUBLISHED_TO_DATA_LAKE_AND_FEATURE_STORE"
        ):
            raise RecoveryPublicationError(
                "recovery is already published; use the read-only status command"
            )
    archive_days_by_asset: dict[str, tuple[str, ...]] = {}
    for asset in ASSETS:
        recent_start = max(
            int(
                pd.to_numeric(
                    recent_by_asset[asset]["position_bias"]["timestamp"],
                    errors="raise",
                ).min()
            ),
            int(
                pd.to_numeric(
                    recent_by_asset[asset]["open_interest"]["timestamp"],
                    errors="raise",
                ).min()
            ),
        )
        archive_days_by_asset[asset] = recovery_archive_days(
            target_start_day=target_start_day,
            existing_start_ms=recent_start,
        )
    scope = [
        (asset, day)
        for asset in ASSETS
        for day in archive_days_by_asset[asset]
    ]
    archive_objects, failures = _load_archive_scope(
        cache_root,
        scope,
        workers=workers,
        fetcher=fetcher,
    )
    source_inventory: list[dict[str, Any]] = []
    for asset in ASSETS:
        recent = recent_by_asset[asset]
        for dataset in ("position_bias", "open_interest"):
            frame = recent[dataset]
            source_inventory.append(
                _source_inventory_record(
                    frame,
                    asset=asset,
                    dataset=dataset,
                    source=RECENT_API_SOURCE,
                    checksum=str(frame.iloc[0]["source_checksum"]),
                    status="VERIFIED",
                    url=RECENT_SOURCE_URLS[dataset],
                )
            )
    recovered_by_asset: dict[str, dict[str, pd.DataFrame]] = {}
    overlap_checks: dict[str, dict[str, Any]] = {}
    for asset in ASSETS:
        daily_frames: dict[str, list[pd.DataFrame]] = {
            "position_bias": [],
            "open_interest": [],
        }
        for day in archive_days_by_asset[asset]:
            item = archive_objects.get((asset, day))
            if item is None:
                continue
            parsed = item["parsed"]
            frames = archive_day_frames(parsed)
            for dataset in daily_frames:
                daily_frames[dataset].append(frames[dataset])
            for dataset, frame in frames.items():
                source_inventory.append(
                    _source_inventory_record(
                        frame,
                        asset=asset,
                        dataset=dataset,
                        source=ARCHIVE_SOURCE,
                        checksum=str(item["archive_sha256"]),
                        status=str(item["cache_status"]).upper(),
                        day=day,
                        url=str(item["url"]),
                    )
                )
        archive_frames = {
            dataset: (
                pd.concat(parts, ignore_index=True, sort=False)
                .sort_values("timestamp")
                .reset_index(drop=True)
                if parts
                else pd.DataFrame()
            )
            for dataset, parts in daily_frames.items()
        }
        recent = recent_by_asset[asset]
        try:
            position_overlap = compare_source_overlap(
                recent["position_bias"],
                archive_frames["position_bias"],
                value_columns=("long_short_ratio",),
                decimal_places={"long_short_ratio": 4},
            )
            oi_overlap = compare_source_overlap(
                recent["open_interest"],
                archive_frames["open_interest"],
                value_columns=(
                    "open_interest",
                    "open_interest_value",
                ),
                relative_tolerance=1e-12,
                absolute_tolerance=1e-8,
            )
            overlap_checks[asset] = {
                "status": "PASS",
                "overlap_rows": min(
                    int(position_overlap["overlap_rows"]),
                    int(oi_overlap["overlap_rows"]),
                ),
                "position_bias": position_overlap,
                "open_interest": oi_overlap,
            }
            recovered_by_asset[asset] = {
                "position_bias": merge_verified_rows(
                    recent["position_bias"],
                    archive_frames["position_bias"],
                    symbol=asset,
                    value_columns=("long_short_ratio",),
                    decimal_places={"long_short_ratio": 4},
                ),
                "open_interest": merge_verified_rows(
                    recent["open_interest"],
                    archive_frames["open_interest"],
                    symbol=asset,
                    value_columns=(
                        "open_interest",
                        "open_interest_value",
                    ),
                    relative_tolerance=1e-12,
                    absolute_tolerance=1e-8,
                ),
            }
        except (SourceCompatibilityError, KeyError) as error:
            overlap_checks[asset] = {
                "status": "FAIL",
                "overlap_rows": 0,
                "error": f"{type(error).__name__}: {error}",
            }
            recovered_by_asset[asset] = recent

    payload = evaluate_staged_recovery(
        root,
        recovered_by_asset,
        overlap_checks=overlap_checks,
        source_inventory=source_inventory,
    )
    payload["archive_scope"] = {
        "target_start_day": target_start_day,
        "end_days": {
            asset: archive_days_by_asset[asset][-1]
            for asset in ASSETS
        },
        "requested_objects": len(scope),
        "verified_objects": len(archive_objects),
        "failed_objects": len(failures),
        "cache_hits": sum(
            1
            for item in archive_objects.values()
            if item["cache_status"] == "cached"
        ),
        "downloads": sum(
            1
            for item in archive_objects.values()
            if item["cache_status"] == "downloaded"
        ),
        "failures": failures,
    }
    if failures:
        payload["blockers"] = sorted(
            {
                *payload.get("blockers", []),
                "archive_object_failures",
            }
        )
        payload["status"] = "BLOCKED"
    return payload, recovered_by_asset


def render_recovery_manifest(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def write_recovery_artifacts(
    root: Path,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    manifest_path = (
        root / "data_lake" / "position_data_recovery_manifest.json"
    )
    report_path = root / "POSITION_DATA_RECOVERY_GATE_REPORT.md"
    artifacts = {
        manifest_path: render_recovery_manifest(payload).encode("utf-8"),
        report_path: render_recovery_report(payload).encode("utf-8"),
    }
    temporary_paths: list[tuple[Path, Path]] = []
    try:
        for target, content in artifacts.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(content)
            temporary_paths.append((temporary, target))
        for temporary, target in temporary_paths:
            temporary.replace(target)
    finally:
        for temporary, _ in temporary_paths:
            if temporary.exists():
                temporary.unlink()
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            content
        ).hexdigest()
        for path, content in sorted(
            artifacts.items(),
            key=lambda item: str(item[0]),
        )
    }


def evaluate_recovery_gate(
    asset_results: Mapping[str, Mapping[str, Any]],
    *,
    assets: Sequence[str] = ASSETS,
) -> dict[str, Any]:
    missing_assets = [asset for asset in assets if asset not in asset_results]
    required_checks = (
        "eligible_for_frozen_rerun",
        "symbol_provenance_verified",
        "timestamps_verified",
        "source_compatibility_verified",
        "non_identity_verified",
    )
    blockers: list[str] = [
        f"missing_asset:{asset}" for asset in missing_assets
    ]
    for asset in assets:
        result = asset_results.get(asset)
        if result is None:
            continue
        for check in required_checks:
            if not bool(result.get(check)):
                blockers.append(f"{asset}:{check}")
    return {
        "status": "READY_FOR_FROZEN_RERUN" if not blockers else "BLOCKED",
        "missing_assets": missing_assets,
        "blockers": blockers,
        "replication_effect": "NOT_EVALUATED",
        "direction": "NOT_EVALUATED",
    }


def _display(value: Any) -> str:
    if value is None or value == "":
        return "NA"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    return str(value)


def render_recovery_report(payload: Mapping[str, Any]) -> str:
    policy = payload.get("policy", {})
    assets = payload.get("assets", {})
    inventory = payload.get("source_inventory", [])
    overlap = payload.get("overlap_checks", {})
    identity = payload.get("cross_asset_identity", {})
    blockers = payload.get("blockers", [])
    lines = [
        "# Position Data Recovery Gate",
        "",
        "Phase B1R-D - data recovery and authenticity validation only.",
        "",
        "## Frozen Contract",
        "",
        "- Metric: `top_position_long_short`.",
        "- Period: `5m`, aligned backward to already-closed `1h` bars.",
        f"- Hard warm-up: `{policy.get('warmup_required_hours', HARD_WARMUP_HOURS)}` hours.",
        f"- Recovery target: `{policy.get('recommended_history_hours', RECOMMENDED_HISTORY_HOURS)}` hours.",
        f"- Forward fill: `{'FORBIDDEN' if policy.get('no_forward_fill', True) else 'ALLOWED'}`.",
        f"- Interpolation: `{'FORBIDDEN' if policy.get('no_interpolation', True) else 'ALLOWED'}`.",
        f"- Synthetic rows: `{'FORBIDDEN' if policy.get('no_synthetic_rows', True) else 'ALLOWED'}`.",
        f"- Frozen study modified: `{'NO' if not policy.get('frozen_study_modified', False) else 'YES'}`.",
        "",
        "## Source Inventory",
        "",
        "|asset|source|dataset|rows|start|end|checksum|status|URL|",
        "|---|---|---|---:|---|---|---|---|---|",
    ]
    for item in sorted(
        inventory,
        key=lambda row: (
            str(row.get("asset", "")),
            str(row.get("source", "")),
            str(row.get("dataset", "")),
        ),
    ):
        lines.append(
            "|{asset}|{source}|{dataset}|{rows}|{start}|{end}|{checksum}|{status}|{url}|".format(
                asset=_display(item.get("asset")),
                source=_display(item.get("source")),
                dataset=_display(item.get("dataset")),
                rows=item.get("rows", 0),
                start=_display(item.get("start")),
                end=_display(item.get("end")),
                checksum=_display(item.get("checksum")),
                status=_display(item.get("status")),
                url=_display(item.get("url")),
            )
        )
    if not inventory:
        lines.append(
            "|NA|NA|NA|0|NA|NA|NA|NOT_AVAILABLE|NA|"
        )

    lines += [
        "",
        "## Asset Coverage Gate",
        "",
        "|asset|5m rows|5m gaps|missing 5m samples|closed 1h observations|hard 720h|target 1080h|symbol|timestamps|source overlap|non-identity|status|",
        "|---|---:|---:|---:|---:|---|---|---|---|---|---|---|",
    ]
    for asset in ASSETS:
        item = assets.get(asset, {})
        ready = bool(item.get("eligible_for_frozen_rerun"))
        checks = (
            bool(item.get("symbol_provenance_verified"))
            and bool(item.get("timestamps_verified"))
            and bool(item.get("source_compatibility_verified"))
            and bool(item.get("non_identity_verified"))
        )
        lines.append(
            "|{asset}|{rows}|{gaps}|{missing}|{hours}|{hard}|{recommended}|{symbol}|{timestamps}|{overlap}|{identity}|{status}|".format(
                asset=asset,
                rows=item.get("rows", 0),
                gaps=max(
                    int(
                        item.get(
                            "position_timestamp_quality",
                            {},
                        ).get("gap_count", 0)
                    ),
                    int(
                        item.get(
                            "open_interest_timestamp_quality",
                            {},
                        ).get("gap_count", 0)
                    ),
                ),
                missing=max(
                    int(
                        item.get(
                            "position_timestamp_quality",
                            {},
                        ).get(
                            "missing_five_minute_samples",
                            0,
                        )
                    ),
                    int(
                        item.get(
                            "open_interest_timestamp_quality",
                            {},
                        ).get(
                            "missing_five_minute_samples",
                            0,
                        )
                    ),
                ),
                hours=item.get("hourly_observations", 0),
                hard=_display(item.get("hard_warmup_pass", False)),
                recommended=_display(
                    item.get("recommended_history_pass", False)
                ),
                symbol=_display(
                    item.get("symbol_provenance_verified", False)
                ),
                timestamps=_display(item.get("timestamps_verified", False)),
                overlap=_display(
                    item.get("source_compatibility_verified", False)
                ),
                identity=_display(item.get("non_identity_verified", False)),
                status="PASS" if ready and checks else "BLOCKED",
            )
        )

    lines += [
        "",
        "## Source Compatibility",
        "",
    ]
    if overlap:
        for asset in ASSETS:
            item = overlap.get(asset, {})
            lines.append(
                f"- {asset}: `{item.get('status', 'NOT_EVALUATED')}`; "
                f"overlap rows `{item.get('overlap_rows', 0)}`."
            )
    else:
        lines.append("- NOT_EVALUATED")

    lines += [
        "",
        "## Cross-Asset Authenticity",
        "",
        f"- Overall: `{'PASS' if identity.get('verified') else 'FAIL'}`.",
    ]
    for pair in identity.get("pairs", []):
        lines.append(
            "- {assets}: checksums identical `{checksums}`; values identical "
            "`{values}`; exact matches `{matches}/{rows}`; correlation `{correlation}`.".format(
                assets=pair.get("assets", "NA"),
                checksums=str(
                    bool(pair.get("checksums_identical"))
                ).lower(),
                values=str(bool(pair.get("values_identical"))).lower(),
                matches=pair.get("exact_value_matches", 0),
                rows=pair.get("overlap_rows", 0),
                correlation=_display(pair.get("correlation")),
            )
        )

    publication = payload.get("publication", {})
    lines += [
        "",
        "## Publication",
        "",
        f"- Status: `{publication.get('status', 'NOT_PUBLISHED')}`.",
    ]
    publication_files = publication.get("files", {})
    if publication_files:
        lines.extend(
            f"- `{path}`: `{checksum}`"
            for path, checksum in sorted(publication_files.items())
        )
    else:
        lines.append("- No files published.")

    lines += [
        "",
        "## Blockers",
        "",
    ]
    if blockers:
        lines.extend(f"- `{blocker}`" for blocker in sorted(map(str, blockers)))
    else:
        lines.append("- None.")

    lines += [
        "",
        "## Current State",
        "",
        f"PHASE_B1R_D={payload.get('status', 'BLOCKED')}",
        f"REPLICATION_EFFECT={payload.get('replication_effect', 'NOT_EVALUATED')}",
        f"DIRECTION={payload.get('direction', 'NOT_EVALUATED')}",
        f"WARMUP_REQUIRED_HOURS={policy.get('warmup_required_hours', HARD_WARMUP_HOURS)}",
        f"RECOMMENDED_HISTORY_HOURS={policy.get('recommended_history_hours', RECOMMENDED_HISTORY_HOURS)}",
        f"FROZEN_STUDY_MODIFIED={'YES' if policy.get('frozen_study_modified', False) else 'NO'}",
        "PUSH=NO",
        "PAPER_LIVE=NO_GO",
        "",
    ]
    return "\n".join(lines)


def current_recovery_status(root: Path = ROOT) -> dict[str, Any]:
    manifest_path = (
        root / "data_lake" / "position_data_recovery_manifest.json"
    )
    previous: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            previous = {}
    frames = {
        asset: read_recent_sources(root, asset)
        for asset in ASSETS
    }
    prior_publication = previous.get("publication", {})
    published = (
        prior_publication.get("status")
        == "PUBLISHED_TO_DATA_LAKE_AND_FEATURE_STORE"
    )
    if published:
        inventory = [
            dict(item)
            for item in previous.get("source_inventory", [])
            if isinstance(item, dict)
        ]
        for item in inventory:
            if item.get("url"):
                continue
            if item.get("source") == ARCHIVE_SOURCE:
                asset = str(item.get("asset", ""))
                day = str(item.get("day", ""))
                if asset and day:
                    item["url"] = archive_metrics_url(asset, day)
            elif item.get("source") == RECENT_API_SOURCE:
                item["url"] = RECENT_SOURCE_URLS.get(
                    str(item.get("dataset", "")),
                    "",
                )
        overlap_checks = {
            asset: dict(
                previous.get("overlap_checks", {}).get(
                    asset,
                    {
                        "status": "NOT_EVALUATED",
                        "overlap_rows": 0,
                    },
                )
            )
            for asset in ASSETS
        }
    else:
        inventory = []
        for asset in ASSETS:
            for dataset in ("position_bias", "open_interest"):
                frame = frames[asset][dataset]
                inventory.append(
                    _source_inventory_record(
                        frame,
                        asset=asset,
                        dataset=dataset,
                        source=RECENT_API_SOURCE,
                        checksum=str(
                            frame.iloc[0]["source_checksum"]
                        ),
                        status="VERIFIED",
                        url=RECENT_SOURCE_URLS[dataset],
                    )
                )
        overlap_checks = {
            asset: {
                "status": "NOT_EVALUATED",
                "overlap_rows": 0,
            }
            for asset in ASSETS
        }
    payload = evaluate_staged_recovery(
        root,
        frames,
        overlap_checks=overlap_checks,
        source_inventory=inventory,
    )
    if published:
        payload["archive_scope"] = dict(
            previous.get("archive_scope", {})
        )
        payload["publication"] = dict(prior_publication)
    else:
        payload["archive_scope"] = {
            "requested_objects": 0,
            "verified_objects": 0,
            "failed_objects": 0,
            "cache_hits": 0,
            "downloads": 0,
            "failures": {},
        }
    return payload


def parse_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase B1R-D verified position-data recovery gate; "
            "does not run the frozen replication study"
        )
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )
    subparsers.add_parser(
        "status",
        help="audit existing local data without network access",
    )
    recover = subparsers.add_parser(
        "recover",
        help="recover only the missing Binance Vision daily metrics objects",
    )
    recover.add_argument(
        "--target-start",
        default=DEFAULT_TARGET_START_DAY,
    )
    recover.add_argument(
        "--cache-root",
        type=Path,
        default=DEFAULT_CACHE_ROOT,
    )
    recover.add_argument(
        "--workers",
        type=int,
        default=6,
    )
    recover.add_argument(
        "--publish",
        action="store_true",
        help=(
            "publish staged rows only when all three assets pass "
            "the 1080-hour authenticity gate"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "status":
        payload = current_recovery_status(ROOT)
        artifacts = write_recovery_artifacts(ROOT, payload)
    else:
        payload, frames = stage_recovery(
            ROOT,
            cache_root=args.cache_root,
            target_start_day=args.target_start,
            workers=args.workers,
        )
        if args.publish:
            if payload["status"] != "READY_FOR_FROZEN_RERUN":
                write_recovery_artifacts(ROOT, payload)
                print(
                    json.dumps(
                        {
                            "status": payload["status"],
                            "blockers": payload["blockers"],
                            "published": False,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 2
            published = publish_recovered_sources(
                ROOT,
                frames,
                gate_status=str(payload["status"]),
            )
            payload["publication"] = {
                "status": "PUBLISHED_TO_DATA_LAKE_AND_FEATURE_STORE",
                "files": published,
            }
        else:
            payload["publication"] = {
                "status": "STAGED_NOT_PUBLISHED",
                "files": {},
            }
        artifacts = write_recovery_artifacts(ROOT, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "replication_effect": payload["replication_effect"],
                "direction": payload["direction"],
                "artifacts": artifacts,
                "archive_scope": payload.get("archive_scope", {}),
                "publication": payload.get(
                    "publication",
                    {"status": "NOT_REQUESTED", "files": {}},
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "READY_FOR_FROZEN_RERUN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
