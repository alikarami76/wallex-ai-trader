"""Offline scorecard for vetoed, shrunk, and accepted signals.

No network and no API keys. The path is the stored candles: exit at the
stop, the trail, or 24 hours, whichever comes first, after the configured fees.
"""

from __future__ import annotations

import json
from pathlib import Path

from trader.engine.ops import read_jsonl, text_hash
from trader.types import Bar


def seal_signals(features, decision, planned, candidates, equity: float, *, prompt: str, response: str, mode: str) -> list[dict]:
    """Freeze the fields the scorecard needs. Hashes cover the text actually sent."""
    prompt_hash = text_hash(prompt or "")
    response_hash = text_hash(response or "")
    by_candidate = {item.symbol: item for item in candidates}
    sealed = []
    for row in features:
        candidate = by_candidate.get(row.symbol)
        buy = next((action for action in planned.actions if action.symbol == row.symbol and action.side == "BUY"), None)
        model = next((item for item in decision.decisions if item.symbol == row.symbol), None)
        size_before = None if candidate is None else float(candidate.size_fraction)
        size_after = float(buy.size_fraction) if buy else 0.0
        if size_before and size_before > 0 and buy is not None:
            size_mult = size_after / size_before
        elif buy is not None:
            size_mult = size_after
        else:
            size_mult = 0.0
        if candidate is None:
            disposition = "none"
        elif buy is not None and size_before is not None and size_after < size_before - 1e-12:
            disposition = "shrunk"
        elif buy is not None:
            disposition = "accepted"
        else:
            disposition = "vetoed"
        action = "BUY" if buy is not None else (model.action if model is not None else "HOLD")
        snapshot = row.compact()
        sealed.append({
            "symbol": row.symbol,
            "bar_time": snapshot.get("bar_time"),
            "close": snapshot.get("close"),
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "action": action,
            "size_mult": None if size_mult is None else round(float(size_mult), 6),
            "size_before": size_before,
            "size_after": size_after if candidate is not None or buy is not None else None,
            "disposition": disposition,
            "equity_tmn": equity,
            "mode": mode,
            "signal": snapshot,
        })
    return sealed


def load_candles(directory: Path) -> dict[str, list[Bar]]:
    candles: dict[str, list[Bar]] = {}
    if not directory.exists():
        return candles
    for path in sorted(directory.glob("*.csv")):
        bars: list[Bar] = []
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            if not line.strip():
                continue
            time_s, open_s, high_s, low_s, close_s, volume_s = line.split(",")[:6]
            bars.append(Bar(
                int(float(time_s)), float(open_s), float(high_s), float(low_s), float(close_s), float(volume_s),
            ))
        candles[path.stem] = bars
    return candles


def path_return(bars: list[Bar], entry_time: int, entry_price: float, cfg: dict) -> dict:
    """Net return of one long, after fees. Exit is stop, trail, or the 24h bar."""
    risk = cfg["risk"]
    fees = cfg["fees"]
    stop_px = entry_price * (1 - float(risk["stop_loss_pct"]))
    arm_px = entry_price * (1 + float(risk["trail_arm_pct"]))
    trail_gap = float(risk["trail_distance_pct"])
    deadline = int(entry_time) + int(float(risk["time_stop_hours"]) * 3600)
    maker = float(fees["maker_rate"])
    taker = float(fees["taker_rate"])
    peak = entry_price
    exit_px = entry_price
    reason = "incomplete"
    fee_out = maker
    for bar in bars:
        if bar.time <= entry_time:
            continue
        peak = max(peak, bar.high)
        if bar.low <= stop_px:
            exit_px, reason, fee_out = stop_px, "stop", taker
            break
        if peak >= arm_px:
            trail = peak * (1 - trail_gap)
            if bar.low <= trail:
                exit_px, reason, fee_out = trail, "trail", taker
                break
        if bar.time >= deadline:
            exit_px, reason, fee_out = bar.close, "time", maker
            break
    else:
        if any(bar.time > entry_time for bar in bars):
            last = [bar for bar in bars if bar.time > entry_time][-1]
            exit_px, reason, fee_out = last.close, "incomplete", maker
    gross = exit_px / entry_price - 1 if entry_price else 0.0
    fee = maker + fee_out * (exit_px / entry_price if entry_price else 1.0)
    return {
        "exit_reason": reason,
        "exit_price": exit_px,
        "net_return": gross - fee,
    }


def _signals_from_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Sealed symbol rows win. Older cycle blobs are used when they carry a signal."""
    shadow = 0
    found: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        if row.get("mode") == "shadow":
            shadow += 1
        sealed = row.get("sealed")
        if isinstance(sealed, list) and sealed:
            for item in sealed:
                if item.get("disposition") not in {"vetoed", "shrunk", "accepted"}:
                    continue
                key = (item.get("symbol"), item.get("bar_time"), item.get("disposition"))
                if key in seen:
                    continue
                seen.add(key)
                found.append(item)
            continue
        if row.get("record") == "symbol" and row.get("disposition") in {"vetoed", "shrunk", "accepted"}:
            key = (row.get("symbol"), row.get("bar_time"), row.get("disposition"))
            if key in seen:
                continue
            seen.add(key)
            found.append(row)
            continue
        if not row.get("features"):
            continue
        actions = ((row.get("risk") or {}).get("actions") or [])
        for feature in row["features"]:
            if not feature.get("signal"):
                continue
            symbol = feature.get("symbol")
            buy = next((item for item in actions if item.get("symbol") == symbol and item.get("side") == "BUY"), None)
            size_before = float((row.get("strategy_size") or feature.get("size_fraction") or 0.12))
            size_after = float(buy.get("size_fraction") or 0) if buy else 0.0
            if buy and size_after < size_before - 1e-12:
                disposition = "shrunk"
            elif buy:
                disposition = "accepted"
            else:
                disposition = "vetoed"
            key = (symbol, feature.get("bar_time"), disposition)
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "symbol": symbol,
                "bar_time": feature.get("bar_time"),
                "close": feature.get("close"),
                "prompt_hash": row.get("prompt_hash"),
                "response_hash": row.get("response_hash"),
                "action": "BUY" if buy else "HOLD",
                "size_mult": 0.0 if not size_before else size_after / size_before,
                "size_before": size_before,
                "size_after": size_after,
                "disposition": disposition,
                "equity_tmn": row.get("equity_tmn") or 0,
                "mode": row.get("mode"),
                "signal": feature,
            })
    return found, shadow


def _notional(item: dict, size: float | None) -> float:
    equity = float(item.get("equity_tmn") or 0)
    if equity <= 0 or size is None:
        return 0.0
    return equity * float(size)


def build_scorecard(rows: list[dict], candles: dict[str, list[Bar]], cfg: dict) -> dict:
    signals, shadow_rows = _signals_from_rows(rows)
    buckets = {
        name: {"n": 0, "rules_pnl_tmn": 0.0, "taken_pnl_tmn": 0.0, "exits": {}}
        for name in ("vetoed", "shrunk", "accepted")
    }
    details = []
    for item in signals:
        bars = candles.get(item.get("symbol") or "")
        entry = float(item.get("close") or 0)
        when = item.get("bar_time")
        if not bars or not entry or when is None:
            continue
        path = path_return(bars, int(when), entry, cfg)
        full = _notional(item, item.get("size_before"))
        taken_size = 0.0 if item["disposition"] == "vetoed" else float(item.get("size_after") or 0)
        taken = _notional(item, taken_size)
        rules_pnl = full * path["net_return"]
        taken_pnl = taken * path["net_return"]
        bucket = buckets[item["disposition"]]
        bucket["n"] += 1
        bucket["rules_pnl_tmn"] += rules_pnl
        bucket["taken_pnl_tmn"] += taken_pnl
        exits = bucket["exits"]
        exits[path["exit_reason"]] = exits.get(path["exit_reason"], 0) + 1
        details.append({
            "symbol": item.get("symbol"),
            "bar_time": when,
            "disposition": item["disposition"],
            "action": item.get("action"),
            "size_mult": item.get("size_mult"),
            "prompt_hash": item.get("prompt_hash"),
            "response_hash": item.get("response_hash"),
            "exit_reason": path["exit_reason"],
            "net_return": path["net_return"],
            "rules_pnl_tmn": rules_pnl,
            "taken_pnl_tmn": taken_pnl,
        })
    rules_total = sum(bucket["rules_pnl_tmn"] for bucket in buckets.values())
    taken_total = sum(bucket["taken_pnl_tmn"] for bucket in buckets.values())
    for bucket in buckets.values():
        bucket["rules_pnl_tmn"] = round(bucket["rules_pnl_tmn"], 2)
        bucket["taken_pnl_tmn"] = round(bucket["taken_pnl_tmn"], 2)
    return {
        "rows": len(rows),
        "shadow_rows": shadow_rows,
        "signals": len(details),
        "rules_pnl_tmn": round(rules_total, 2),
        "taken_pnl_tmn": round(taken_total, 2),
        "gap_tmn": round(rules_total - taken_total, 2),
        "buckets": buckets,
        "trades": details,
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Veto scorecard",
        "",
        "Offline replay of sealed signals on stored candles. Exit is the stop, the trail, or 24 hours, after fees.",
        "The rules column is the full quant size. Taken is zero on a veto and the post-shrink size otherwise.",
        "",
        f"Rows read: {report['rows']}. Shadow-mode rows: {report['shadow_rows']}. Signals scored: {report['signals']}.",
        "",
        f"Rules PnL {report['rules_pnl_tmn']:.2f} TMN. Taken PnL {report['taken_pnl_tmn']:.2f} TMN. Gap {report['gap_tmn']:.2f} TMN.",
        "",
        "| bucket | n | rules TMN | taken TMN | exits |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name in ("vetoed", "shrunk", "accepted"):
        bucket = report["buckets"][name]
        exits = ", ".join(f"{key} {value}" for key, value in sorted(bucket["exits"].items())) or "-"
        lines.append(
            f"| {name} | {bucket['n']} | {bucket['rules_pnl_tmn']:.2f} | {bucket['taken_pnl_tmn']:.2f} | {exits} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(log_dir: Path, candle_dir: Path, cfg: dict, dest: Path) -> dict:
    rows = read_jsonl(Path(log_dir) / "decisions.jsonl")
    report = build_scorecard(rows, load_candles(Path(candle_dir)), cfg)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scorecard.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (dest / "scorecard.md").write_text(render_markdown(report), encoding="utf-8")
    return report
