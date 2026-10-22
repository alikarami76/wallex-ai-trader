"""Random-entry baseline for the recorded backtest.

Same universe, denylist, hold, stop, trail, fees, and size. Entry times are
the only random piece. The run is offline and does not change the strategy.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from trader.config import load_config
from trader.scorecard import load_candles


N_RUNS = 1000
QUANTILES = (0.05, 0.25, 0.50, 0.75, 0.95)


def baseline_section(log_dir: Path, *, n_runs: int = N_RUNS, seed: int = 20261022) -> str:
    """Markdown section, also stored as baseline.json beside the journal."""
    log_dir = Path(log_dir)
    cfg = load_config("config/default.yaml")
    summary_path = log_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    observed_return = float(summary.get("return_pct") or 0.0)
    observed_sharpe = _observed_sharpe(log_dir / "equity.csv")
    target = int(summary.get("closed_trades") or 0) or None
    result = random_entry_baseline(
        load_candles(log_dir / "candles"),
        cfg,
        n_runs=n_runs,
        seed=seed,
        target_entries=target,
        observed_return_pct=observed_return,
        observed_sharpe=observed_sharpe,
    )
    (log_dir / "baseline.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return render_baseline(result)


def random_entry_baseline(
    candles: dict,
    cfg: dict,
    *,
    n_runs: int = N_RUNS,
    seed: int = 20261022,
    target_entries: int | None = None,
    observed_return_pct: float = 0.0,
    observed_sharpe: float | None = None,
) -> dict:
    book = _book(candles, cfg)
    returns = []
    sharpes = []
    entered: set[str] = set()
    for index in range(int(n_runs)):
        row = _one_run(book, cfg, random.Random(seed + index), target_entries)
        returns.append(row["return_pct"])
        sharpes.append(row["sharpe"])
        entered.update(row["symbols"])
    return {
        "n_runs": int(n_runs),
        "seed": int(seed),
        "target_entries": int(target_entries or 0),
        "symbols": book["symbols"],
        "symbols_entered": sorted(entered),
        "observed_return_pct": observed_return_pct,
        "observed_sharpe": observed_sharpe,
        "return_percentile": _percentile(observed_return_pct, returns),
        "sharpe_percentile": None if observed_sharpe is None else _percentile(observed_sharpe, sharpes),
        "return_quantiles": _quantiles(returns),
        "sharpe_quantiles": _quantiles(sharpes),
    }


def render_baseline(result: dict) -> str:
    ret_q = result["return_quantiles"]
    shp_q = result["sharpe_quantiles"]
    sharpe = "n/a" if result["observed_sharpe"] is None else f"{result['observed_sharpe']:.2f}"
    sharpe_pct = "n/a" if result["sharpe_percentile"] is None else f"{result['sharpe_percentile']:.1f}"
    lines = [
        "",
        "## Random-entry baseline",
        "",
        f"{result['n_runs']} seeded runs. Entries are random times on the same universe, "
        "with the denylist, 24-hour hold, stop, trail, fees, and size cap left as configured. "
        f"Each run attempts {result['target_entries']} entries.",
        "",
        f"Strategy return {result['observed_return_pct']}% is at the {result['return_percentile']:.1f} percentile "
        f"of the random-entry returns. Strategy Sharpe {sharpe} is at the {sharpe_pct} percentile.",
        "",
        "|  | p05 | p25 | p50 | p75 | p95 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        f"| return % | {ret_q['p05']:.3f} | {ret_q['p25']:.3f} | {ret_q['p50']:.3f} | {ret_q['p75']:.3f} | {ret_q['p95']:.3f} |",
        f"| Sharpe | {shp_q['p05']:.2f} | {shp_q['p25']:.2f} | {shp_q['p50']:.2f} | {shp_q['p75']:.2f} | {shp_q['p95']:.2f} |",
        "",
    ]
    return "\n".join(lines)


def _book(candles: dict, cfg: dict) -> dict:
    universe = cfg.get("universe") or {}
    denied = {str(item).upper() for item in (universe.get("hard_deny_bases") or universe.get("denylist") or [])}
    allow = {str(item).upper() for item in (universe.get("allowlist") or [])}
    symbols = []
    bars = {}
    for symbol, rows in candles.items():
        base = symbol[:-3].upper() if str(symbol).upper().endswith("TMN") else str(symbol).upper()
        if base in denied or (allow and str(symbol).upper() not in allow):
            continue
        ordered = tuple(sorted(rows, key=lambda bar: bar.time))
        if len(ordered) < 3:
            continue
        symbols.append(symbol)
        bars[symbol] = {bar.time: (bar.high, bar.low, bar.close) for bar in ordered}
    times = tuple(sorted({stamp for series in bars.values() for stamp in series}))
    return {"symbols": symbols, "bars": bars, "times": times}


def _one_run(book: dict, cfg: dict, rng: random.Random, target_entries: int | None) -> dict:
    risk = cfg["risk"]
    fees = cfg["fees"]
    maker = float(fees["maker_rate"])
    taker = float(fees["taker_rate"])
    stop_pct = float(risk["stop_loss_pct"])
    arm_pct = float(risk["trail_arm_pct"])
    trail_pct = float(risk["trail_distance_pct"])
    hold = int(float(risk["time_stop_hours"]) * 3600)
    size_cap = float(risk["max_fraction_per_trade"])
    gross_cap = float(risk["max_gross_exposure"])
    slots = int(risk["max_positions"])
    cooldown = int(risk["cooldown_after_stop_seconds"])
    stop_slip = float(cfg["execution"]["stop_slippage_bps"]) / 10_000
    min_notional = float(risk["min_notional_tmn"])
    cash = float(cfg["account"]["initial_equity_tmn"])
    times = book["times"]
    bars = book["bars"]
    symbols = book["symbols"]
    attempts: set[int] = set()
    if len(times) > 2 and symbols:
        pool = list(range(1, len(times) - 1))
        count = len(pool) if target_entries is None else min(len(pool), max(0, int(target_entries)))
        attempts = set(rng.sample(pool, count)) if count else set()
    positions: dict[str, dict] = {}
    cooled: dict[str, int] = {}
    last_close: dict[str, float] = {}
    equity_path = []
    entered: set[str] = set()
    for index, stamp in enumerate(times):
        for symbol in list(positions):
            bar = bars[symbol].get(stamp)
            if bar is None:
                continue
            high, low, close = bar
            last_close[symbol] = close
            position = positions[symbol]
            if stamp <= position["entry_time"]:
                continue
            if low <= position["stop"]:
                _exit(position, position["stop"] * (1 - stop_slip), taker)
                cash += position["proceeds"]
                cooled[symbol] = stamp + cooldown
                del positions[symbol]
                continue
            position["peak"] = max(position["peak"], high)
            trail = position["peak"] * (1 - trail_pct)
            if position["peak"] >= position["entry"] * (1 + arm_pct) and low <= trail:
                _exit(position, trail * (1 - stop_slip), taker)
                cash += position["proceeds"]
                del positions[symbol]
                continue
            if stamp >= position["entry_time"] + hold:
                _exit(position, close, maker)
                cash += position["proceeds"]
                del positions[symbol]
        for symbol, series in bars.items():
            bar = series.get(stamp)
            if bar is not None:
                last_close[symbol] = bar[2]
        if index in attempts and len(positions) < slots:
            choices = [
                symbol for symbol in symbols
                if symbol not in positions and stamp >= cooled.get(symbol, 0) and stamp in bars[symbol]
            ]
            if choices:
                symbol = rng.choice(choices)
                price = bars[symbol][stamp][2]
                equity = _equity(cash, positions, last_close)
                gross = _gross(positions, last_close, equity)
                fraction = min(size_cap, max(0.0, gross_cap - gross))
                notional = equity * fraction
                if price > 0 and notional >= min_notional and notional <= cash:
                    qty = notional / price * (1 - maker)
                    cash -= notional
                    positions[symbol] = {
                        "qty": qty,
                        "entry": price,
                        "entry_time": stamp,
                        "stop": price * (1 - stop_pct),
                        "peak": price,
                        "cost": notional,
                    }
                    entered.add(symbol)
                    last_close[symbol] = price
        equity_path.append(_equity(cash, positions, last_close))
    if positions and times:
        for symbol, position in list(positions.items()):
            price = last_close.get(symbol) or position["entry"]
            _exit(position, price, maker)
            cash += position["proceeds"]
        equity_path.append(cash)
    start = float(cfg["account"]["initial_equity_tmn"])
    ending = equity_path[-1] if equity_path else start
    return {
        "return_pct": (ending / start - 1) * 100 if start else 0.0,
        "sharpe": _sharpe(equity_path, times),
        "symbols": entered,
    }


def _exit(position: dict, price: float, fee_rate: float) -> None:
    position["proceeds"] = position["qty"] * price * (1 - fee_rate)


def _equity(cash: float, positions: dict, marks: dict[str, float]) -> float:
    total = cash
    for symbol, position in positions.items():
        total += position["qty"] * marks.get(symbol, position["entry"])
    return total


def _gross(positions: dict, marks: dict[str, float], equity: float) -> float:
    if equity <= 0:
        return 1.0
    gross = sum(position["qty"] * marks.get(symbol, position["entry"]) for symbol, position in positions.items())
    return gross / equity


def _sharpe(equity: list[float], times: tuple) -> float:
    rets = []
    dts = []
    for index in range(1, len(equity)):
        previous = equity[index - 1]
        if previous:
            rets.append(equity[index] / previous - 1)
            if index < len(times):
                dts.append(times[index] - times[index - 1])
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    step = sorted(dts)[len(dts) // 2] if dts else 3600
    if step <= 0:
        step = 3600
    return (mean / std) * ((365 * 24 * 3600) / step) ** 0.5


def _observed_sharpe(path: Path) -> float | None:
    if not path.exists():
        return None
    import csv
    from datetime import datetime
    equity = []
    times = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                equity.append(float(row["equity_tmn"]))
                times.append(int(datetime.fromisoformat(row["timestamp"]).timestamp()))
            except (KeyError, TypeError, ValueError):
                continue
    if len(equity) < 3:
        return None
    return _sharpe(equity, tuple(times))


def _percentile(observed: float, samples: list[float]) -> float:
    if not samples:
        return 0.0
    worse = sum(1 for item in samples if item < observed)
    equal = sum(1 for item in samples if item == observed)
    return 100.0 * (worse + 0.5 * equal) / len(samples)


def _quantiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    if not ordered:
        return {f"p{int(q * 100):02d}": 0.0 for q in QUANTILES}
    result = {}
    last = len(ordered) - 1
    for q in QUANTILES:
        result[f"p{int(q * 100):02d}"] = ordered[int(round(last * q))]
    return result
