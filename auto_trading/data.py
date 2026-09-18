"""Public-only acquisition and deterministic, fail-closed market data visibility.

Endpoint names follow futures_data_downloader.ENDPOINTS. Its write-capable data
layer and unrestricted transport are deliberately not imported by this boundary.
Taker timestamps denote period starts; OI statistics timestamps denote ends.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import digest, record

BASE_URL = "https://fapi.binance.com"
FLOW_MS = 300_000
INTERVALS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
FLOW_PATHS = {
    "oi": "/futures/data/openInterestHist",
    "taker": "/futures/data/takerlongshortRatio",
    "account_ratio": "/futures/data/globalLongShortAccountRatio",
    "top_position_ratio": "/futures/data/topLongShortPositionRatio",
}
PUBLIC_PATHS = frozenset(FLOW_PATHS.values()) | {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr", "/fapi/v2/ticker/price",
    "/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker", "/fapi/v1/depth",
    "/fapi/v1/klines", "/fapi/v1/openInterest", "/fapi/v1/fundingRate",
}
STABLE_ASSETS = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP", "USDE", "USD1",
                 "USDD", "USD0", "USDN", "FRAX", "LUSD", "PYUSD", "RLUSD", "EURC", "EURI"}


class DataError(RuntimeError):
    """An audited, bounded acquisition failure (never a no-opportunity signal)."""


def _number(value: Any, *, positive: bool = False) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else True))


def _timestamp(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _visible(item: Any, as_of: int) -> bool:
    if not isinstance(item, dict):
        return True  # preserve malformed evidence for explicit rejection
    available = item.get("available_at")
    end = item.get("period_end")
    return not ((_timestamp(available) and available > as_of)
                or (_timestamp(end) and end > as_of))


def visible_snapshot(snapshot: dict, as_of: int, profile: dict | None = None) -> dict:
    """As-of projection, without sorting, fixing, or mutating original evidence."""
    result = copy.deepcopy(snapshot)
    if not isinstance(result, dict):
        return result
    bars = result.get("bars", {})
    records = result.get("records", {})
    for key, rows in (bars.items() if isinstance(bars, dict) else []):
        if isinstance(rows, list):
            result["bars"][key] = [row for row in rows if _visible(row, as_of)]
    for key, value in (records.items() if isinstance(records, dict) else []):
        if isinstance(value, list):
            result["records"][key] = [row for row in value if _visible(row, as_of)]
        elif not _visible(value, as_of):
            result["records"][key] = None
    return result


def funding_percent(rate: float) -> str:
    if not _number(rate):
        raise ValueError("SCHEMA_CONFLICT:funding")
    rendered = format(Decimal(str(rate)) * 100, "f")
    return (rendered.rstrip("0").rstrip(".") if "." in rendered else rendered) + "%"


def eligible_metadata(metadata: dict) -> bool:
    return (isinstance(metadata, dict) and metadata.get("status") == "TRADING"
            and isinstance(metadata.get("symbol"), str)
            and isinstance(metadata.get("baseAsset"), str)
            and metadata.get("contractType") == "PERPETUAL"
            and metadata.get("quoteAsset") == "USDT"
            and metadata.get("asset_class") == "crypto"
            and metadata.get("baseAsset") not in STABLE_ASSETS
            and bool(metadata.get("baseAsset")))


def _record_error(item: Any, symbol: str, as_of: int) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
        return True
    if any(not _timestamp(item.get(k)) for k in ("event_time", "received_at", "available_at")):
        return True
    if any(k in item and not _timestamp(item[k]) for k in ("period_start", "period_end")):
        return True
    if "availability_estimated" in item and not isinstance(item["availability_estimated"], bool):
        return True
    if not all(item.get(k) for k in ("source_endpoint", "symbol", "units", "raw_ref")):
        return True
    if item["symbol"] != symbol or item["event_time"] > as_of:
        return True
    if item["available_at"] < item["event_time"] or item["received_at"] < item["event_time"]:
        return True
    if not item.get("availability_estimated") and item["available_at"] < item["received_at"]:
        return True
    return False


def _data_error(name: str, data: dict) -> bool:
    positive = lambda key: _number(data.get(key), positive=True)
    if name in {"price", "mark", "index"}:
        return not positive("price")
    if name == "quote":
        return not (positive("bid") and positive("ask") and data["bid"] <= data["ask"])
    if name in {"oi", "current_oi"}:
        return not positive("quantity") or ("notional" in data and not positive("notional"))
    if name in {"taker", "account_ratio", "top_position_ratio", "top_account_ratio"}:
        return not positive("ratio")
    if name == "ticker24h":
        return not (all(positive(k) for k in ("quote_volume", "high", "last"))
                    and _number(data.get("change_pct")) and data["last"] <= data["high"])
    if name == "funding":
        return not (_number(data.get("rate")) and _timestamp(data.get("next_funding_time")))
    if name == "depth":
        for side in ("bids", "asks"):
            levels = data.get(side)
            if not isinstance(levels, list) or not levels:
                return True
            for level in levels:
                if (not isinstance(level, (list, tuple)) or len(level) != 2
                        or not all(_number(v, positive=True) for v in level)):
                    return True
        return max(level[0] for level in data["bids"]) > min(level[0] for level in data["asks"])
    return False


def validate_snapshot(snapshot: dict, as_of: int, profile: dict, for_entry: bool = False) -> list[str]:
    """Reject missing/stale/schema-conflicting core evidence; no wall-clock reads."""
    errors: list[str] = []
    if not _timestamp(as_of) or not isinstance(snapshot, dict):
        return ["SCHEMA_CONFLICT:snapshot"]
    malformed = [f"SCHEMA_CONFLICT:{key}" for key in ("bars", "records", "metadata")
                 if not isinstance(snapshot.get(key), dict)]
    if malformed:
        return malformed
    view = visible_snapshot(snapshot, as_of)
    symbol = view.get("symbol", "")
    metadata = view.get("metadata", {})
    if not eligible_metadata(metadata) or metadata.get("symbol") != symbol:
        errors.append("SCHEMA_CONFLICT:metadata")
    if any(not _number(metadata.get(key), positive=True)
           for key in ("tick_size", "step_size", "min_qty", "min_notional")):
        errors.append("SCHEMA_CONFLICT:filters")
    for interval, step in profile.get("intervals_ms", INTERVALS).items():
        rows = view.get("bars", {}).get(interval)
        if not isinstance(rows, list) or not rows:
            errors.append(f"DATA_INSUFFICIENT:{interval}")
            continue
        previous = None
        for row in rows:
            bad = _record_error(row, symbol, as_of)
            if not bad:
                start, end, data = row.get("period_start"), row.get("period_end"), row["data"]
                bad = (not _timestamp(start) or not _timestamp(end) or end - start != step
                       or start % step != 0 or row["event_time"] != end - 1
                       or row["available_at"] < end)
                if not bad:
                    bad = (not all(_number(data.get(k), positive=True) for k in ("open", "high", "low", "close"))
                           or data["low"] > min(data["open"], data["close"])
                           or data["high"] < max(data["open"], data["close"])
                           or data["low"] > data["high"]
                           or not _number(data.get("volume")) or data["volume"] < 0
                           or not _number(data.get("taker_buy_volume"))
                           or not 0 <= data["taker_buy_volume"] <= data["volume"])
                if previous is not None and start != previous:
                    bad = True
                previous = end
            if bad:
                errors.append(f"SCHEMA_CONFLICT:{interval}")
        expected_end = as_of // step * step
        if (isinstance(rows[-1], dict) and _timestamp(rows[-1].get("period_end"))
                and rows[-1]["period_end"] < expected_end):
            errors.append(f"DATA_STALE:{interval}")
    records = view.get("records", {})
    for name in profile["required_records"]:
        value = records.get(name)
        if value is None or value == []:
            errors.append(f"DATA_INSUFFICIENT:{name}")
            continue
        rows = value if isinstance(value, list) else [value]
        previous = None
        for item in rows:
            bad = _record_error(item, symbol, as_of)
            if not bad:
                bad = _data_error(name, item["data"])
                if name in {"oi", "current_oi"}:
                    bad |= not any(unit in str(item["units"]).lower()
                                   for unit in ("contract", "base_asset", "quantity_base"))
                if name in {"oi", "taker"}:
                    start, end = item.get("period_start"), item.get("period_end")
                    bad |= (not _timestamp(start) or not _timestamp(end)
                            or end - start != FLOW_MS or end % FLOW_MS != 0
                            or item["available_at"] < end)
                    if not bad:
                        if name == "taker":
                            bad |= item["event_time"] != start
                        else:
                            bad |= item["event_time"] not in {end, end - 1}
                        if previous is not None and end - previous != FLOW_MS:
                            bad = True
                        previous = end
                elif isinstance(value, list):
                    if previous is not None and item["event_time"] - previous != FLOW_MS:
                        bad = True
                    previous = item["event_time"]
                if name == "funding" and item["data"].get("next_funding_time", 0) <= item["event_time"]:
                    bad = True
            if bad:
                errors.append(f"SCHEMA_CONFLICT:{name}")
        latest = rows[-1]
        if isinstance(latest, dict) and _timestamp(latest.get("event_time")):
            timestamp = latest.get("period_end", latest["event_time"])
            if not _timestamp(timestamp):
                continue
            age = as_of - timestamp
            bound = (profile["flow_max_age_ms"] if name in {"oi", "current_oi", "taker", "account_ratio", "top_position_ratio"}
                     else profile["market_max_age_ms"])
            if for_entry and name in {"quote", "depth"}:
                bound = profile["quote_max_age_ms"]
            if age > bound:
                errors.append(f"DATA_STALE:{name}")
    return list(dict.fromkeys(errors))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("PUBLIC_ENDPOINT_REDIRECT_REJECTED")


def _public_get(path: str, params: dict, timeout: float) -> tuple[int, dict, Any]:
    if path not in PUBLIC_PATHS:
        raise DataError("PUBLIC_ENDPOINT_REJECTED")
    request = Request(BASE_URL + path + "?" + urlencode(params), method="GET")
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise DataError("RESPONSE_TOO_LARGE")
            return response.status, dict(response.headers), json.loads(body)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), None


class PublicAdapter:
    """Two-phase, per-lane cached public GET reader; constructor performs no IO."""
    def __init__(self, profile: dict, *, clock: Callable[[], int] | None = None,
                 transport: Callable | None = None, sleeper: Callable | None = None):
        if profile.get("mode") == "live" or profile.get("production_allowed"):
            raise DataError("LIVE_NOT_IMPLEMENTED")
        self.profile = copy.deepcopy(profile)
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._raw_clock = self.clock
        self._clock_injected = clock is not None
        self.clock_calibration: dict | None = None
        self.transport = transport or _public_get
        self.sleeper = sleeper or time.sleep
        self.cache: dict = {}
        self.metadata: dict = {}
        self.audit: list[dict] = []
        self.circuit_until = 0
        self.deadline_ms: int | None = None
        self.cancelled: Callable[[], bool] = lambda: False
        self.failures = 0
        self.health: dict = {}
        self.reset_budget()

    def reset_budget(self) -> None:
        self.request_count = 0
        self.weight_used = 0
        self.health = {"request_count": 0, "weight_used": 0, "cache_hits": 0,
                       "scanned": 0, "eligible": 0, "candidates": 0, "failures": 0}

    def synchronize_clock(self) -> None:
        """Public server-time interval: upper receipt, lower candle completion.

        No system clock is changed. Original local receipt time is retained.
        Upper/lower bounds account for the entire request round-trip instead of
        pretending an estimated midpoint is an exact exchange clock.
        """
        sent = self._raw_clock()
        payload, _ = self._get("/fapi/v1/time")
        received = self._raw_clock()
        server = payload.get("serverTime") if isinstance(payload, dict) else None
        if not _timestamp(server) or received < sent:
            raise DataError("SCHEMA_CONFLICT:serverTime")
        self.clock_calibration = {"local_sent_at": sent, "local_received_at": received,
                                  "server_time": server, "offset_lower_ms": server - received,
                                  "offset_upper_ms": server - sent, "uncertainty_ms": received - sent}
        upper = self.clock_calibration["offset_upper_ms"]
        self.clock = lambda: self._raw_clock() + upper

    def completed_before(self, normalized_received: int) -> int:
        uncertainty = self.clock_calibration["uncertainty_ms"] if self.clock_calibration else 0
        return normalized_received - uncertainty

    def _get(self, path: str, params: dict | None = None, *, ttl_ms: int = 0,
             weight: int = 1) -> tuple[Any, int]:
        if path not in PUBLIC_PATHS:
            raise DataError("PUBLIC_ENDPOINT_REJECTED")
        params = params or {}
        allowed_params = {"symbol", "interval", "period", "limit"}
        if set(params) - allowed_params or ("symbol" in params and not re.fullmatch(r"[A-Z0-9]{3,30}", str(params["symbol"]))):
            raise DataError("PUBLIC_PARAMETERS_REJECTED")
        key, now = digest([path, params]), self.clock()
        cached = self.cache.get(key)
        if cached and now < cached[2]:
            self.health["cache_hits"] += 1
            return copy.deepcopy(cached[0]), cached[1]
        if now < self.circuit_until:
            raise DataError("CIRCUIT_OPEN")
        attempts = min(2, max(0, int(self.profile.get("retries", 1)))) + 1
        for attempt in range(attempts):
            remaining = None if self.deadline_ms is None else self.deadline_ms - self.clock()
            if self.cancelled() or (remaining is not None and remaining <= 0):
                raise DataError("REQUEST_BUDGET:ROUND_DEADLINE_OR_STOP")
            if (self.request_count >= self.profile.get("max_requests", 256)
                    or self.weight_used + weight > self.profile.get("max_weight", 1000)):
                raise DataError("REQUEST_BUDGET")
            self.request_count += 1
            self.weight_used += weight
            self.health.update(request_count=self.request_count, weight_used=self.weight_used)
            received = self.clock()
            retry_ms = 0
            try:
                timeout = min(10, self.profile.get("timeout_seconds", 5))
                if remaining is not None:
                    timeout = min(timeout, max(.001, remaining / 1000))
                result = self.transport(path, copy.deepcopy(params), timeout)
                status, headers, payload = result if isinstance(result, tuple) and len(result) == 3 else (200, {}, result)
                received = self.clock()
                audit = {"source_endpoint": path, "parameters": params, "received_at": received,
                         "status": status, "weight": weight, "attempt": attempt + 1}
                self.audit.append(audit)
                if status == 200:
                    audit["raw_ref"] = digest(payload)
                    self.failures = 0
                    expiry = (received // ttl_ms + 1) * ttl_ms if ttl_ms else received
                    self.cache[key] = (copy.deepcopy(payload), received, expiry)
                    return payload, received
                if status in {418, 429}:
                    retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "60")
                    try:
                        retry_ms = max(1000, int(float(retry) * 1000))
                    except (ValueError, TypeError):
                        retry_ms = max(1000, int(parsedate_to_datetime(retry).timestamp() * 1000) - received)
                    self.circuit_until = received + retry_ms
                    raise DataError(f"HTTP_{status}:CIRCUIT_OPEN")
                if status < 500:
                    raise DataError(f"HTTP_{status}")
                raise OSError(f"HTTP_{status}")
            except DataError:
                self.health["failures"] += 1
                raise
            except (OSError, ValueError, TypeError) as exc:
                self.failures += 1
                self.health["failures"] += 1
                self.audit.append({"source_endpoint": path, "received_at": received,
                                   "error": type(exc).__name__, "attempt": attempt + 1})
                if self.failures >= 3:
                    self.circuit_until = received + 60_000
                if attempt + 1 == attempts or self.circuit_until > received:
                    raise DataError(f"PUBLIC_READ_FAILED:{type(exc).__name__}") from exc
                self.sleeper(min(2, .25 * 2 ** attempt))
        raise DataError("PUBLIC_READ_FAILED")

    def _metadata(self) -> None:
        payload, _ = self._get("/fapi/v1/exchangeInfo", ttl_ms=3_600_000)
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise DataError("SCHEMA_CONFLICT:exchangeInfo")
        self.metadata = {}
        for item in payload["symbols"]:
            filters = {f["filterType"]: f for f in item.get("filters", [])}
            asset_type = item.get("underlyingType", "")
            explicit = str(item.get("asset_class", "")).lower()
            coin = asset_type == "COIN" or explicit == "crypto"
            # A documented non-crypto type always wins over any local fallback.
            if not asset_type and not explicit and item.get("symbol") in self.profile.get("known_crypto_symbols", []):
                coin = True
            if asset_type not in {"", "COIN"} or explicit not in {"", "crypto"}:
                coin = False
            subtype = item.get("underlyingSubType", [])
            subtype_text = " ".join(str(tag).upper() for tag in subtype) if isinstance(subtype, list) else str(subtype).upper()
            if any(tag in subtype_text for tag in ("TRADFI", "STOCK", "EQUITY", "COMMODIT", "STABLECOIN", "STABLE_COIN", "FOREX")):
                coin = False
            try:
                metadata = {k: item.get(k) for k in ("symbol", "baseAsset", "quoteAsset", "contractType", "status")}
                metadata.update(asset_class="crypto" if coin else "unknown",
                                classification_source="exchangeInfo" if asset_type or explicit else "profile_known_crypto_symbols",
                                underlyingType=asset_type, underlyingSubType=copy.deepcopy(subtype),
                                tick_size=float(filters["PRICE_FILTER"]["tickSize"]),
                                step_size=float(filters["LOT_SIZE"]["stepSize"]),
                                min_qty=float(filters["LOT_SIZE"]["minQty"]),
                                min_notional=float(filters["MIN_NOTIONAL"].get("notional", filters["MIN_NOTIONAL"].get("minNotional"))))
            except (KeyError, TypeError, ValueError):
                continue
            if eligible_metadata(metadata):
                self.metadata[metadata["symbol"]] = metadata
        self.health.update(scanned=len(payload["symbols"]), eligible=len(self.metadata))

    def discover(self) -> list[str]:
        self._metadata()
        payload, _ = self._get("/fapi/v1/ticker/24hr", ttl_ms=60_000, weight=40)
        if not isinstance(payload, list):
            raise DataError("SCHEMA_CONFLICT:ticker24h")
        symbols = set(self.profile.get("fixed_watchlist", []) + self.profile.get("benchmarks", [])) & self.metadata.keys()
        for item in payload:
            symbol = item.get("symbol")
            if symbol not in self.metadata:
                continue
            try:
                change, volume, high, last = (float(item[k]) for k in ("priceChangePercent", "quoteVolume", "highPrice", "lastPrice"))
                if not all(math.isfinite(v) for v in (change, volume, high, last)) or min(high, last) <= 0:
                    continue
                short = (change >= self.profile.get("short_discovery_change_pct", 8)
                         and volume >= self.profile.get("short_discovery_quote_volume", 5_000_000)
                         and 0 <= (high - last) / high * 100 <= self.profile.get("short_discovery_drawdown_pct", 15))
                long = (change >= self.profile.get("long_discovery_change_pct", 3)
                        and volume >= self.profile.get("long_discovery_quote_volume", 10_000_000))
                if short or long:
                    symbols.add(symbol)
            except (KeyError, TypeError, ValueError):
                self.health["failures"] += 1
        self.health["candidates"] = len(symbols)
        return sorted(symbols)

    def snapshot(self, symbol: str) -> dict:
        if not self._clock_injected and (self.clock_calibration is None or
                self._raw_clock() - self.clock_calibration["local_received_at"] > 3600000):
            self.synchronize_clock()
        self._metadata()
        if symbol not in self.metadata:
            raise DataError("MARKET_INELIGIBLE:" + symbol)
        result = {"symbol": symbol, "metadata": copy.deepcopy(self.metadata[symbol]), "bars": {}, "records": {}}
        records = result["records"]
        def rec(data, event, received, path, units="mixed", **kwargs):
            value = record(data, int(event), received, path, symbol, units, **kwargs)
            value["availability_estimated"] = False
            if self.clock_calibration:
                value["received_at_wallclock"] = received - self.clock_calibration["offset_upper_ms"]
                value["clock_uncertainty_ms"] = self.clock_calibration["uncertainty_ms"]
            return value
        for interval, step in INTERVALS.items():
            path = "/fapi/v1/klines"
            rows, received = self._get(path, {"symbol": symbol, "interval": interval, "limit": min(99, self.profile.get("bar_limit", 80))}, ttl_ms=step)
            bars = []
            for row in rows:
                # Never cache an incomplete last candle as completed later.
                if int(row[6]) >= self.completed_before(received):
                    continue
                bars.append(rec({"open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                                 "close": float(row[4]), "volume": float(row[5]), "taker_buy_volume": float(row[9])},
                                int(row[6]), received, path, "base_asset", period_start=int(row[0]), period_end=int(row[6]) + 1))
            result["bars"][interval] = bars
        for name, path in FLOW_PATHS.items():
            rows, received = self._get(path, {"symbol": symbol, "period": "5m", "limit": min(30, self.profile.get("flow_limit", 12))}, ttl_ms=FLOW_MS)
            flow = []
            for row in rows:
                timestamp = int(row["timestamp"])
                end = timestamp + FLOW_MS if name == "taker" else timestamp
                if end > self.completed_before(received):
                    continue
                if name == "oi":
                    data = {"quantity": float(row["sumOpenInterest"]), "notional": float(row["sumOpenInterestValue"])}
                    units = "contracts;notional_USDT"
                else:
                    data = {"ratio": float(row["buySellRatio"] if name == "taker" else row["longShortRatio"])}
                    units = "ratio"
                kwargs = {"period_start": end - FLOW_MS, "period_end": end} if name in {"oi", "taker"} else {}
                flow.append(rec(data, timestamp, received, path, units, **kwargs))
            records[name] = flow
        path = "/fapi/v1/ticker/24hr"
        row, received = self._get(path, {"symbol": symbol}, ttl_ms=60_000)
        records["ticker24h"] = rec({"change_pct": float(row["priceChangePercent"]), "quote_volume": float(row["quoteVolume"]),
                                    "high": float(row["highPrice"]), "last": float(row["lastPrice"])}, row["closeTime"], received, path)
        path = "/fapi/v1/openInterest"
        row, received = self._get(path, {"symbol": symbol}, ttl_ms=60_000)
        records["current_oi"] = rec({"quantity": float(row["openInterest"])}, row["time"], received, path, "contracts")
        path = "/fapi/v2/ticker/price"
        row, received = self._get(path, {"symbol": symbol})
        records["price"] = rec({"price": float(row["price"])}, row["time"], received, path, "USDT")
        path = "/fapi/v1/premiumIndex"
        row, received = self._get(path, {"symbol": symbol})
        for name, key in (("mark", "markPrice"), ("index", "indexPrice")):
            records[name] = rec({"price": float(row[key])}, row["time"], received, path, "USDT")
        records["funding"] = rec({"rate": float(row["lastFundingRate"]), "next_funding_time": int(row["nextFundingTime"])}, row["time"], received, path, "decimal_rate")
        path = "/fapi/v1/fundingRate"
        settlements, received = self._get(path, {"symbol": symbol, "limit": 4}, ttl_ms=60_000)
        records["funding_settlements"] = [
            rec({"rate": float(item["fundingRate"]), "mark_price": float(item["markPrice"]), "settlement": True},
                item["fundingTime"], received, path, "decimal_rate;mark_USDT")
            for item in settlements if int(item["fundingTime"]) <= received]
        if records["funding_settlements"]:
            records["funding"]["data"]["settlement_interval_ms"] = (
                records["funding"]["data"]["next_funding_time"]
                - records["funding_settlements"][-1]["event_time"])
        # Acquisition can cross a candle boundary. Refresh only the affected
        # interval before the final depth/quote reads; never age an incomplete
        # cached candle into a completed one.
        for interval, step in INTERVALS.items():
            expected = self.completed_before(self.clock()) // step * step
            if result["bars"][interval] and result["bars"][interval][-1]["period_end"] >= expected:
                continue
            path = "/fapi/v1/klines"
            rows, received = self._get(path, {"symbol": symbol, "interval": interval,
                                              "limit": min(99, self.profile.get("bar_limit", 80))}, ttl_ms=step)
            result["bars"][interval] = [rec({"open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                                             "close": float(row[4]), "volume": float(row[5]), "taker_buy_volume": float(row[9])},
                                           int(row[6]), received, path, "base_asset",
                                           period_start=int(row[0]), period_end=int(row[6]) + 1)
                                          for row in rows if int(row[6]) < self.completed_before(received)]
        path = "/fapi/v1/depth"
        row, received = self._get(path, {"symbol": symbol, "limit": 20}, weight=2)
        records["depth"] = rec({side: [[float(p), float(q)] for p, q in row[side]] for side in ("bids", "asks")}, row["T"], received, path, "price_USDT;quantity_base")
        path = "/fapi/v1/ticker/bookTicker"
        row, received = self._get(path, {"symbol": symbol}, weight=2)
        records["quote"] = rec({"bid": float(row["bidPrice"]), "ask": float(row["askPrice"]),
                                 "bid_qty": float(row["bidQty"]), "ask_qty": float(row["askQty"])}, row["time"], received, path, "price_USDT;quantity_base")
        result["acquired_at"] = self.clock()
        result["clock_calibration"] = copy.deepcopy(self.clock_calibration)
        result["raw_refs"] = [entry.get("raw_ref") for entry in self.audit if entry.get("raw_ref")]
        return result
