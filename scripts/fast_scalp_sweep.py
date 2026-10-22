#!/usr/bin/env python3
"""One fast_scalp backtest plus a small parameter sweep on live Wallex hours."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from trader.config import load_config
from trader.engine.backtest import run_backtest
from trader.strategy.profiles import apply_strategy_profile
from trader.wallex.client import WallexClient


def _stats(summary: dict, days: float) -> dict:
    closed = int(summary.get("closed_trades") or 0)
    start = float(summary.get("start_equity") or 5_000_000)
    end = float(summary["ending_equity"])
    net = end - start
    return {
        "closed_trades": closed,
        "trades_per_day": round(closed / days, 2) if days > 0 else 0.0,
        "win_rate": summary.get("win_rate"),
        "net_tmn": round(net, 2),
        "net_pct": round((end / start - 1) * 100, 3) if start else 0.0,
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "fees_paid": round(float(summary.get("fees_paid") or 0), 2),
        "realized_pnl": round(float(summary.get("realized_pnl") or 0), 2),
    }


def _with_wins(cfg, candles, rules, unavailable, spreads) -> dict:
    from trader.engine.portfolio import Portfolio

    # Re-run through a light wrapper: capture sell pnls from a patched journal.
    wins = {"n": 0, "w": 0}
    class CountJournal:
        def decision(self, *_a, **_k): return None
        def equity(self, *_a, **_k): return None
        def close(self): return None
        def trade(self, row):
            pnl = row.get("pnl_tmn")
            if pnl is None:
                return
            wins["n"] += 1
            if float(pnl) > 0:
                wins["w"] += 1

    # Prefer the stock runner; win rate from sell fills in a second pass.
    summary = run_backtest(cfg, candles, rules, unavailable, None, spreads=spreads, realize_open=True)
    # Approximate win rate from realized / closed when we lack per-trade: recompute via fills path.
    # Stock summary lacks win_rate; derive from portfolio by replaying sell pnls from equity path is hard.
    # Instead patch: run again counting via monkey on Portfolio.sell.
    original = Portfolio.sell
    def counted(self, *args, **kwargs):
        fill = original(self, *args, **kwargs)
        if fill is not None and fill.pnl_tmn is not None:
            wins["n"] += 1
            if fill.pnl_tmn > 0:
                wins["w"] += 1
        return fill
    Portfolio.sell = counted
    try:
        wins["n"] = wins["w"] = 0
        summary = run_backtest(cfg, candles, rules, unavailable, None, spreads=spreads, realize_open=True)
    finally:
        Portfolio.sell = original
    summary["win_rate"] = None if not wins["n"] else round(wins["w"] / wins["n"], 3)
    summary["start_equity"] = float(cfg["account"]["initial_equity_tmn"])
    return summary


def main() -> int:
    root = Path("results/fast_scalp")
    root.mkdir(parents=True, exist_ok=True)
    base = load_config("config/default.yaml")
    # Drop liquidation deadline so the sweep is not pinned to the hackathon flat_by.
    base["liquidation"]["deadline"] = "2099-01-01T00:00:00+03:30"
    base["liquidation"]["flat_by"] = "2099-01-01T00:00:00+03:30"
    base["ops"]["entry_cutoff"] = "2099-01-01T00:00:00+03:30"

    client = WallexClient(base_url=base["exchange"]["base_url"], api_key="", timeout=20)
    markets = client.get_markets()
    end = int(time.time())
    start = end - 14 * 86400
    symbols = [s for s in base["universe"]["candidates"] if s != "USDTTMN" and s in markets]
    candles = {}
    spreads = dict(base["universe"].get("spread_bps_snapshot") or {})
    for symbol in symbols:
        candles[symbol] = client.get_ohlcv(symbol, "1h", start, end)
        print(symbol, len(candles[symbol]))
    rules = {symbol: markets[symbol] for symbol in symbols}
    days = max(1.0, (end - start) / 86400)

    cfg = apply_strategy_profile(copy.deepcopy(base), "fast_scalp")
    primary = _with_wins(cfg, candles, rules, set(), spreads)
    primary_stats = _stats(primary, days)
    print("PRIMARY", json.dumps(primary_stats))

    grid = []
    for tp, sl, hold in (
        (0.012, 0.008, 2),
        (0.010, 0.008, 2),
        (0.012, 0.006, 1),
        (0.015, 0.010, 3),
        (0.012, 0.008, 3),
    ):
        row = apply_strategy_profile(copy.deepcopy(base), "fast_scalp")
        row["risk"]["take_profit_pct"] = tp
        row["risk"]["stop_loss_pct"] = sl
        row["risk"]["time_stop_hours"] = hold
        summary = _with_wins(row, candles, rules, set(), spreads)
        stats = _stats(summary, days)
        stats.update({"tp": tp, "sl": sl, "hold_h": hold})
        grid.append(stats)
        print("SWEEP", stats)

    out = {"primary": primary_stats, "sweep": grid, "days": days, "symbols": symbols}
    (root / "sweep.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
