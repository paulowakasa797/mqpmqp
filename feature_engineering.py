from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Sequence


Bar = Dict[str, float | int]


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    previous = seed
    out[period - 1] = previous
    for idx in range(period, len(values)):
        previous = values[idx] * alpha + previous * (1.0 - alpha)
        out[idx] = previous
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    tr_values: List[float] = []
    for idx, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        if idx == 0:
            tr_values.append(high - low)
            continue
        prev_close = float(bars[idx - 1]["close"])
        tr_values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    previous = sum(tr_values[:period]) / period
    out[period - 1] = previous
    for idx in range(period, len(tr_values)):
        previous = (previous * (period - 1) + tr_values[idx]) / period
        out[idx] = previous
    return out


def rolling_mean(values: Sequence[float], lookback: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    total = 0.0
    for idx, value in enumerate(values):
        total += value
        if idx >= lookback:
            total -= values[idx - lookback]
        if idx + 1 >= lookback:
            out[idx] = total / lookback
    return out


def rolling_extreme(values: Sequence[float], lookback: int, is_max: bool, include_current: bool = False) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    q: deque[tuple[int, float]] = deque()
    for idx in range(len(values)):
        add_idx = idx if include_current else idx - 1
        if add_idx >= 0:
            value = values[add_idx]
            while q and ((q[-1][1] <= value) if is_max else (q[-1][1] >= value)):
                q.pop()
            q.append((add_idx, value))
        min_idx = idx - lookback + (1 if include_current else 0)
        while q and q[0][0] < min_idx:
            q.popleft()
        if idx >= lookback - (1 if include_current else 0) and q:
            out[idx] = q[0][1]
    return out


def vwap_like(bars: Sequence[Bar], lookback: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(bars)
    pv = 0.0
    volume = 0.0
    typicals = [((float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3.0, float(bar["volume"])) for bar in bars]
    for idx, (typical, vol) in enumerate(typicals):
        pv += typical * vol
        volume += vol
        if idx >= lookback:
            old_typical, old_vol = typicals[idx - lookback]
            pv -= old_typical * old_vol
            volume -= old_vol
        if idx + 1 >= lookback and volume > 0:
            out[idx] = pv / volume
    return out


def pct_change(values: Sequence[float], lookback: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for idx in range(lookback, len(values)):
        base = values[idx - lookback]
        if base:
            out[idx] = values[idx] / base - 1.0
    return out


def max_drawdown(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def profit_factor(values: Sequence[float]) -> float:
    wins = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value <= 0))
    if losses == 0:
        return 999.0 if wins > 0 else 0.0
    return wins / losses


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
