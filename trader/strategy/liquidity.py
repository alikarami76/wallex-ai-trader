"""Liquidity from the book and from traded volume.

Depth is quote notional within a band of mid, taken from `/v2/depth/all`.
Spread is the touch in basis points. Volume is hourly quote volume
(base size times close). Z-scores use the log of volume, and the raw level
for depth and spread, against the recent window excluding the current point.
A missing book does not block. The backtest has no historical depth, so only
the volume score is active there.

Thresholds are fixed. They were not searched on the equity curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.strategy.indicators import clip
from trader.types import Bar, OrderBook


def liquidity_settings(cfg: dict) -> dict:
    raw = dict(cfg.get("liquidity") or {})
    raw.setdefault("depth_band", 0.01)
    raw.setdefault("depth_band_tight", 0.005)
    raw.setdefault("history", 24)
    raw.setdefault("min_history", 18)
    raw.setdefault("z_block", 2.0)
    raw.setdefault("max_order_fraction_of_depth", 0.10)
    raw.setdefault("min_depth_tmn", 1_000_000)
    raw.setdefault("min_scale", 0.50)
    raw.setdefault("exit_defer_hours", 6)
    return raw


@dataclass
class Liquidity:
    depth_50_tmn: float | None = None
    depth_100_tmn: float | None = None
    bid_depth_100_tmn: float | None = None
    ask_depth_100_tmn: float | None = None
    spread_bps: float | None = None
    quote_volume: float | None = None
    depth_z: float | None = None
    depth_change_z: float | None = None
    spread_z: float | None = None
    spread_change_z: float | None = None
    volume_z: float | None = None
    volume_change_z: float | None = None
    scale: float = 1.0
    exit_ready: bool = True
    block_reason: str = ""

    def as_log(self) -> dict:
        def n(value: float | None, digits: int = 3) -> float | None:
            if value is None:
                return None
            return round(float(value), digits)

        return {
            "depth_50_tmn": n(self.depth_50_tmn, 0),
            "depth_100_tmn": n(self.depth_100_tmn, 0),
            "bid_depth_100_tmn": n(self.bid_depth_100_tmn, 0),
            "ask_depth_100_tmn": n(self.ask_depth_100_tmn, 0),
            "spread_bps": n(self.spread_bps, 2),
            "quote_volume": n(self.quote_volume, 0),
            "depth_z": n(self.depth_z),
            "depth_change_z": n(self.depth_change_z),
            "spread_z": n(self.spread_z),
            "spread_change_z": n(self.spread_change_z),
            "volume_z": n(self.volume_z),
            "volume_change_z": n(self.volume_change_z),
            "liquidity_scale": round(self.scale, 3),
            "exit_ready": self.exit_ready,
            "liquidity_block": self.block_reason,
        }


def trailing_z(series: list[float], min_history: int, sd_floor: float = 0.0) -> float | None:
    """Z-score of the last point against the preceding points.

    `sd_floor` stops a smooth series from turning a tiny wiggle into a shock.
    Depth and volume are scored in logs, so the floor is in log points.
    Spread is in basis points.
    """
    if len(series) < min_history + 1:
        return None
    current = series[-1]
    prior = series[:-1]
    mean = sum(prior) / len(prior)
    var = sum((item - mean) ** 2 for item in prior) / len(prior)
    sd = max(math.sqrt(var), sd_floor)
    if sd <= 1e-12:
        return 0.0
    return (current - mean) / sd


def _change_z(series: list[float], min_history: int, sd_floor: float = 0.0) -> float | None:
    if len(series) < min_history + 2:
        return None
    changes = [series[i] - series[i - 1] for i in range(1, len(series))]
    return trailing_z(changes, min_history, sd_floor)


def _remember(history: dict | None, key: str, value: float | None, limit: int) -> list[float]:
    if history is None:
        return []
    series = history.setdefault(key, [])
    if value is not None and math.isfinite(value) and value >= 0:
        series.append(float(value))
        del series[:-limit]
    return [float(item) for item in series]


def _quote_volume(bars: list[Bar], limit: int) -> list[float]:
    out: list[float] = []
    for bar in bars[-limit:]:
        if bar.close > 0 and bar.volume >= 0 and math.isfinite(bar.volume):
            out.append(bar.volume * bar.close)
    return out


def _health(z: float | None, z_block: float, *, invert: bool = False) -> float | None:
    if z is None:
        return None
    value = -z if invert else z
    return clip((value + z_block) / z_block, 0, 1)


def measure_liquidity(
    bars: list[Bar],
    cfg: dict,
    *,
    book: OrderBook | None = None,
    spread_bps: float | None = None,
    history: dict | None = None,
    max_spread_bps: float | None = None,
) -> Liquidity:
    """Score one symbol. Appends the live depth and spread into `history`."""
    settings = liquidity_settings(cfg)
    band = float(settings["depth_band"])
    tight = float(settings["depth_band_tight"])
    keep = int(settings["history"]) + 2
    minimum = int(settings["min_history"])
    z_block = float(settings["z_block"])
    liq = Liquidity()

    if book is not None:
        wide = book.depth_notional(band)
        narrow = book.depth_notional(tight)
        if wide is not None:
            liq.bid_depth_100_tmn, liq.ask_depth_100_tmn = wide
            liq.depth_100_tmn = wide[0] + wide[1]
        if narrow is not None:
            liq.depth_50_tmn = narrow[0] + narrow[1]
        if book.spread_bps is not None:
            spread_bps = book.spread_bps
    liq.spread_bps = spread_bps

    depth_series = _remember(history, "depth", liq.depth_100_tmn, keep)
    spread_series = _remember(history, "spread", spread_bps, keep)
    volumes = _quote_volume(bars, keep)
    if volumes:
        liq.quote_volume = volumes[-1]
    logs = [math.log(max(item, 1.0)) for item in volumes]

    depth_logs = [math.log(max(item, 1.0)) for item in depth_series]
    liq.depth_z = trailing_z(depth_logs, minimum, sd_floor=0.20) if depth_logs else None
    liq.depth_change_z = _change_z(depth_logs, minimum, sd_floor=0.15) if depth_logs else None
    liq.spread_z = trailing_z(spread_series, minimum, sd_floor=1.0) if spread_series else None
    liq.spread_change_z = _change_z(spread_series, minimum, sd_floor=1.0) if spread_series else None
    liq.volume_z = trailing_z(logs, minimum, sd_floor=0.20) if logs else None
    liq.volume_change_z = _change_z(logs, minimum, sd_floor=0.15) if logs else None

    parts = [
        _health(liq.depth_z, z_block),
        _health(liq.depth_change_z, z_block),
        _health(liq.volume_z, z_block),
        _health(liq.volume_change_z, z_block),
        _health(liq.spread_z, z_block, invert=True),
        _health(liq.spread_change_z, z_block, invert=True),
    ]
    known = [item for item in parts if item is not None]
    if known:
        worst = min(known)
        floor = float(settings["min_scale"])
        liq.scale = floor + (1.0 - floor) * worst

    reasons: list[str] = []
    if liq.depth_100_tmn is not None and liq.depth_100_tmn < float(settings["min_depth_tmn"]):
        reasons.append(f"depth {liq.depth_100_tmn:.0f} TMN inside 1% is below the floor")
    if _shocked(liq.depth_z, liq.depth_change_z, z_block, low=True):
        reasons.append("depth thinned versus recent history")
    if _shocked(liq.spread_z, liq.spread_change_z, z_block, low=False):
        reasons.append("spread widened versus recent history")
    if _shocked(liq.volume_z, liq.volume_change_z, z_block, low=True):
        reasons.append("traded volume dried up versus recent history")
    liq.block_reason = "; ".join(reasons)

    shocked_book = False
    if liq.depth_100_tmn is not None and liq.depth_100_tmn < float(settings["min_depth_tmn"]):
        shocked_book = True
    if max_spread_bps is not None and spread_bps is not None and spread_bps > float(max_spread_bps):
        shocked_book = True
    if liq.depth_change_z is not None and liq.depth_change_z <= -z_block:
        shocked_book = True
    if liq.spread_change_z is not None and liq.spread_change_z >= z_block:
        shocked_book = True
    liq.exit_ready = not shocked_book
    return liq


def _shocked(level_z: float | None, change_z: float | None, z_block: float, *, low: bool) -> bool:
    limit = -z_block if low else z_block
    for value in (level_z, change_z):
        if value is None:
            continue
        if low and value <= limit:
            return True
        if not low and value >= limit:
            return True
    return False
