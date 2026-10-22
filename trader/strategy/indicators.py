"""Small numeric indicators. Inputs are closes or bar lists, oldest first."""

from __future__ import annotations

import math


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2 / (period + 1)
    current = seed
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        out[i] = current
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def atr_percent(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr / closes[i] if closes[i] else 0.0)
        if len(trs) >= period:
            out[i] = sum(trs[-period:]) / period
    return out


def roc(values: list[float], bars: int) -> float | None:
    if bars <= 0 or len(values) <= bars:
        return None
    past = values[-1 - bars]
    if past == 0:
        return None
    return values[-1] / past - 1


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def finite(value: float | None) -> bool:
    return value is not None and not math.isnan(value) and math.isfinite(value)
