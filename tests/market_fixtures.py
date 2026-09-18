"""Deterministic public-market fixtures. No network and no fill claims."""
from __future__ import annotations

from auto_trading.contracts import digest, load_profile


STEP = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}


def profile() -> dict:
    return load_profile()


def _record(data: dict, start: int, end: int, symbol: str, endpoint: str, units: str,
            received: int | None = None, event_time: int | None = None) -> dict:
    recv = end if received is None else received
    event = end - 1 if event_time is None else event_time
    item = {
        "data": data,
        "event_time": event,
        "received_at": recv,
        "available_at": recv,
        "source_endpoint": endpoint,
        "symbol": symbol,
        "units": units,
        "raw_ref": digest({"data": data, "event_time": event, "symbol": symbol, "endpoint": endpoint}),
        "period_start": start,
        "period_end": end,
        "availability_estimated": False,
    }
    return item


def kline(start: int, o: float, h: float, l: float, c: float, *, symbol="BTCUSDT",
          step=300_000, volume=10.0, taker=5.0, received=None) -> dict:
    end = start + step
    return _record(
        {"open": o, "high": h, "low": l, "close": c, "volume": volume, "taker_buy_volume": taker},
        start, end, symbol, "/fapi/v1/klines", "base_asset", received=received,
    )


def metadata(symbol: str = "BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "baseAsset": symbol[:-4],
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "asset_class": "crypto",
        "tick_size": 0.1,
        "step_size": 0.001,
        "min_qty": 0.001,
        "min_notional": 5.0,
    }


def series(count: int, start: int, step: int, *, symbol="BTCUSDT", base=100.0) -> list[dict]:
    rows = []
    price = base
    for i in range(count):
        o = price
        c = price + 0.2
        rows.append(kline(start + i * step, o, c + 0.1, o - 0.1, c, symbol=symbol, step=step, volume=20 + i))
        price = c
    return rows


def snapshot(symbol: str = "BTCUSDT", *, as_of: int | None = None, bars_5m: list[dict] | None = None) -> dict:
    start = 1_775_998_800_000  # aligned to 1h/15m/5m/1m
    rows_5m = bars_5m if bars_5m is not None else series(40, start, STEP["5m"], symbol=symbol)
    last_end = rows_5m[-1]["period_end"]
    now = last_end + 1_000 if as_of is None else as_of
    bars_1m = series(40, last_end - 40 * STEP["1m"], STEP["1m"], symbol=symbol, base=108.0)
    bars_15m = series(24, start, STEP["15m"], symbol=symbol, base=100.0)
    bars_1h = series(20, start, STEP["1h"], symbol=symbol, base=100.0)

    def flow(name: str, endpoint: str, data_fn):
        rows = []
        for i in range(12):
            end = last_end - (11 - i) * 300_000
            start_i = end - 300_000
            event = start_i if name == "taker" else end - 1
            rows.append(_record(data_fn(i), start_i, end, symbol, endpoint,
                                "contracts;notional_USDT" if name == "oi" else "ratio",
                                received=now - 500, event_time=event))
        return rows

    quote = _record(
        {"bid": 109.0, "ask": 109.2, "bid_qty": 50.0, "ask_qty": 50.0},
        last_end, last_end, symbol, "/fapi/v1/ticker/bookTicker", "price_USDT;quantity_base",
        received=now - 200, event_time=now - 300,
    )
    quote.pop("period_start", None)
    quote.pop("period_end", None)
    depth = _record(
        {"bids": [[109.0, 10.0], [108.9, 8.0]], "asks": [[109.2, 10.0], [109.3, 8.0]]},
        last_end, last_end, symbol, "/fapi/v1/depth", "price_USDT;quantity_base",
        received=now - 200, event_time=now - 400,
    )
    depth.pop("period_start", None)
    depth.pop("period_end", None)

    def point(name, data, endpoint, units, event):
        item = _record(data, last_end, last_end, symbol, endpoint, units, received=now - 200, event_time=event)
        item.pop("period_start", None)
        item.pop("period_end", None)
        return item

    return {
        "symbol": symbol,
        "metadata": metadata(symbol),
        "bars": {"1m": bars_1m, "5m": rows_5m, "15m": bars_15m, "1h": bars_1h},
        "records": {
            "price": point("price", {"price": 109.1}, "/fapi/v2/ticker/price", "USDT", now - 300),
            "mark": point("mark", {"price": 109.1}, "/fapi/v1/premiumIndex", "USDT", now - 300),
            "index": point("index", {"price": 109.05}, "/fapi/v1/premiumIndex", "USDT", now - 300),
            "quote": quote,
            "ticker24h": point("ticker24h", {"change_pct": 4.0, "quote_volume": 20_000_000.0,
                                            "high": 120.0, "last": 109.1},
                               "/fapi/v1/ticker/24hr", "mixed", now - 1_000),
            "funding": point("funding", {"rate": 0.0001, "next_funding_time": now + 3_600_000},
                             "/fapi/v1/premiumIndex", "decimal_rate", now - 300),
            "current_oi": point("current_oi", {"quantity": 1000.0, "notional": 109000.0},
                                "/fapi/v1/openInterest", "contracts", now - 300),
            "oi": flow("oi", "/futures/data/openInterestHist",
                       lambda i: {"quantity": 1000.0 + i, "notional": 109000.0 + i}),
            "taker": flow("taker", "/futures/data/takerlongshortRatio", lambda i: {"ratio": 1.3}),
            "account_ratio": flow("account_ratio", "/futures/data/globalLongShortAccountRatio",
                                  lambda i: {"ratio": 1.1}),
            "top_position_ratio": flow("top_position_ratio", "/futures/data/topLongShortPositionRatio",
                                       lambda i: {"ratio": 1.2}),
            "depth": depth,
        },
        "acquired_at": now,
    }
