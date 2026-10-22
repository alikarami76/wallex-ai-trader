"""Fee-aware uptrend-pullback signal.

The last month of hourly Wallex TMN data does not pay for 0.25%/0.30% fees
on breakouts or on tight scalps. One setup did: an 8h/24h uptrend, a 4-hour
pullback of at least 0.4% that is not a crash, and a 24-hour hold. Its
average forward move was about 0.9% before fees. The bot only arms a buy
when that researched edge clears the round trip it will actually pay.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trader.strategy.indicators import atr_percent, clip, ema, finite, roc, rsi
from trader.types import Bar


@dataclass
class SymbolFeatures:
    symbol: str
    bar_time: int
    close: float
    ema_fast: float | None
    ema_slow: float | None
    roc_pullback: float | None
    roc_trend: float | None
    atr_pct: float | None
    rsi: float | None
    spread_bps: float | None
    imbalance: float | None
    micro_return: float | None
    expected_gross: float
    round_trip_cost: float
    signal: bool
    blockers: list[str] = field(default_factory=list)
    score: float = 0.0

    def compact(self) -> dict:
        def n(value: float | None, digits: int = 5) -> float | None:
            if value is None:
                return None
            return round(value, digits)

        return {
            "symbol": self.symbol,
            "bar_time": self.bar_time,
            "close": self.close,
            "ema_fast": n(self.ema_fast, 2),
            "ema_slow": n(self.ema_slow, 2),
            "roc_4h": n(self.roc_pullback),
            "roc_24h": n(self.roc_trend),
            "atr_pct": n(self.atr_pct),
            "rsi": n(self.rsi, 2),
            "spread_bps": n(self.spread_bps, 2),
            "imbalance": n(self.imbalance, 3),
            "micro_return": n(self.micro_return),
            "expected_gross": n(self.expected_gross),
            "round_trip_cost": n(self.round_trip_cost),
            "signal": self.signal,
            "score": round(self.score, 3),
            "blockers": self.blockers,
        }


@dataclass
class Candidate:
    symbol: str
    setup: str
    size_fraction: float
    score: float
    expected_gross: float
    round_trip_cost: float
    roc_trend: float
    roc_pullback: float
    close: float
    bar_time: int
    facts: dict

    def compact(self) -> dict:
        return {
            "symbol": self.symbol,
            "setup": self.setup,
            "size_fraction": self.size_fraction,
            "score": round(self.score, 3),
            "expected_gross": round(self.expected_gross, 5),
            "round_trip_cost": round(self.round_trip_cost, 5),
            "roc_24h": round(self.roc_trend, 5),
            "roc_4h": round(self.roc_pullback, 5),
            "close": self.close,
        }


def round_trip_cost(cfg: dict, maker: float, taker: float) -> float:
    entry = taker if cfg["execution"]["entry"] == "marketable_limit" else maker
    exit_fee = maker if cfg["execution"]["exit"] == "limit_maker" else taker
    slippage = float(cfg["fees"]["slippage_bps"]) / 10_000
    return entry + exit_fee + slippage


def evaluate_symbol(
    symbol: str,
    bars: list[Bar],
    cfg: dict,
    *,
    spread_bps: float | None,
    imbalance: float | None,
    micro_return: float | None,
    maker_fee: float,
    taker_fee: float,
) -> SymbolFeatures:
    strategy = cfg["strategy"]
    closes = [bar.close for bar in bars]
    last = bars[-1] if bars else None
    features = SymbolFeatures(
        symbol=symbol,
        bar_time=last.time if last else 0,
        close=last.close if last else 0.0,
        ema_fast=None,
        ema_slow=None,
        roc_pullback=None,
        roc_trend=None,
        atr_pct=None,
        rsi=None,
        spread_bps=spread_bps,
        imbalance=imbalance,
        micro_return=micro_return,
        expected_gross=float(strategy["expected_gross"]),
        round_trip_cost=round_trip_cost(cfg, maker_fee, taker_fee),
        signal=False,
    )
    need = max(int(strategy["ema_slow"]), int(strategy["trend_bars"]), int(strategy["atr_period"])) + 2
    if len(bars) < need:
        features.blockers.append(f"warmup {len(bars)}/{need}")
        return features

    fast = ema(closes, int(strategy["ema_fast"]))[-1]
    slow = ema(closes, int(strategy["ema_slow"]))[-1]
    pull = roc(closes, int(strategy["pullback_bars"]))
    trend = roc(closes, int(strategy["trend_bars"]))
    atr = atr_percent(
        [b.high for b in bars],
        [b.low for b in bars],
        closes,
        int(strategy["atr_period"]),
    )[-1]
    rsi_value = rsi(closes, int(strategy["rsi_period"]))[-1]
    features.ema_fast = fast
    features.ema_slow = slow
    features.roc_pullback = pull
    features.roc_trend = trend
    features.atr_pct = atr
    features.rsi = rsi_value

    blockers: list[str] = []
    if not finite(fast) or not finite(slow) or not finite(pull) or not finite(trend):
        blockers.append("indicators not ready")
    else:
        if not (features.close > slow and fast > slow):
            blockers.append("trend structure off")
        if trend < float(strategy["trend_roc_min"]):
            blockers.append(f"24h return {trend:.2%} below {float(strategy['trend_roc_min']):.2%}")
        if pull > float(strategy["pullback_roc_max"]):
            blockers.append(f"4h pullback {pull:.2%} is shallower than {float(strategy['pullback_roc_max']):.2%}")
        if pull < float(strategy["pullback_roc_min"]):
            blockers.append(f"4h drop {pull:.2%} looks like a crash")
    if atr is not None and atr > float(strategy["max_atr_pct"]):
        blockers.append(f"atr {atr:.2%} above cap")
    if spread_bps is None:
        blockers.append("spread unknown")
    elif spread_bps > float(cfg["universe"]["max_spread_bps"]):
        blockers.append(f"spread {spread_bps:.1f} bps too wide")
    if imbalance is not None and imbalance < float(strategy["imbalance_block"]):
        blockers.append(f"book imbalance {imbalance:.2f} is offered")
    if micro_return is not None and micro_return < float(strategy["min_micro_return"]):
        blockers.append(f"last 15m still falling {micro_return:.2%}")
    cost = features.round_trip_cost
    edge = features.expected_gross
    if edge <= cost * float(strategy["edge_multiple"]):
        blockers.append(f"edge {edge:.2%} does not clear cost {cost:.2%}")

    features.blockers = blockers
    features.signal = not blockers
    features.score = _score(features, cfg) if features.signal else 0.0
    return features


def _score(features: SymbolFeatures, cfg: dict) -> float:
    trend = clip((features.roc_trend or 0) / 0.03, 0, 1)
    depth = clip((-(features.roc_pullback or 0) - 0.004) / 0.015, 0, 1)
    spread = features.spread_bps if features.spread_bps is not None else float(cfg["universe"]["max_spread_bps"])
    spread_score = 1 - clip(spread / float(cfg["universe"]["max_spread_bps"]), 0, 1)
    if features.imbalance is None:
        book = 0.5
    else:
        book = clip((features.imbalance + 1) / 2, 0, 1)
    return 0.40 * trend + 0.30 * depth + 0.20 * spread_score + 0.10 * book


def build_candidates(rows: list[SymbolFeatures], cfg: dict) -> list[Candidate]:
    size = float(cfg["strategy"]["entry_size_fraction"])
    found: list[Candidate] = []
    for row in rows:
        if not row.signal or row.roc_trend is None or row.roc_pullback is None:
            continue
        found.append(
            Candidate(
                symbol=row.symbol,
                setup="uptrend_pullback",
                size_fraction=size,
                score=row.score,
                expected_gross=row.expected_gross,
                round_trip_cost=row.round_trip_cost,
                roc_trend=row.roc_trend,
                roc_pullback=row.roc_pullback,
                close=row.close,
                bar_time=row.bar_time,
                facts=row.compact(),
            )
        )
    found.sort(key=lambda item: item.score, reverse=True)
    return found
