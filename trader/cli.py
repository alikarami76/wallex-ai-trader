"""One entry point for backtest, shadow, live, the order dry-check, and the report."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from trader.config import load_config
from trader.engine.backtest import run_backtest, run_walk_forward, sentiment_ablation
from trader.sentiment import SentimentFeed
from trader.engine.runtime import check_order, run_runtime
from trader.engine.universe import select_universe
from trader.report import build_report
from trader.wallex.client import WallexClient, aggregate_bars, median_spacing


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Wallex fee-aware AI trader")
    parser.add_argument("--mode", required=True, choices=["backtest", "shadow", "live", "report", "check-order"])
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--duration", type=float, default=None, help="seconds, shadow or live")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="build live order requests and do not send them")
    parser.add_argument("--interval", type=float, default=None, help="seconds between decision cycles")
    parser.add_argument("--fresh", action="store_true", help="ignore saved shadow/live state")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--log-dir", default=None, help="journal directory for --mode report")
    parser.add_argument("--symbol", default="BTCTMN")
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--notional", type=float, default=100_000)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(args.config)
    if args.mode == "backtest":
        out = Path(args.out or "results/backtest")
        _run_backtest(cfg, out)
        return
    if args.mode == "report":
        source = Path(args.log_dir or args.out or "results/backtest")
        path = build_report(source, source, title=f"Wallex trader report ({source})")
        print(path)
        return
    if args.mode == "check-order":
        payload = check_order(cfg, args.symbol, args.side, args.notional)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    out = Path(args.out or f"data/{'dry-run' if args.dry_run and args.mode == 'live' else args.mode}")
    run_runtime(
        cfg,
        mode=args.mode,
        out_dir=out,
        duration=args.duration,
        once=args.once,
        dry_run=args.dry_run or args.mode == "shadow",
        interval=args.interval,
        fresh=args.fresh,
    )
    if args.mode == "shadow" or (args.mode == "live" and args.dry_run):
        build_report(out, out, title=f"Wallex {args.mode} report")


def _run_backtest(cfg: dict, out: Path) -> None:
    client = WallexClient(
        base_url=cfg["exchange"]["base_url"],
        timeout=float(cfg["exchange"]["timeout_seconds"]),
        max_retries=int(cfg["exchange"]["max_retries"]),
    )
    rules = client.get_markets()
    unavailable = client.get_unavailable_markets(int(cfg["exchange"]["package_id"]))
    symbols = select_universe(cfg, rules, unavailable)
    if not symbols:
        raise SystemExit("no symbols left after the ban list and spread filter")
    end = int(time.time())
    start = end - int(float(cfg["runtime"]["history_days"]) * 86400)
    candles = {}
    for symbol in symbols:
        logging.info("downloading %s hourly candles", symbol)
        candles[symbol] = client.get_ohlcv(symbol, "1h", start, end)
        logging.info("%s bars=%s", symbol, len(candles[symbol]))
    out.mkdir(parents=True, exist_ok=True)
    for name in ("decisions.jsonl", "trades.csv", "equity.csv", "report.md", "summary.json", "walk_forward.json"):
        stale = out / name
        if stale.exists():
            stale.unlink()
    _write_candles(out / "candles", candles)
    _aggregation_check(client, out)
    feed = SentimentFeed(cfg)
    fear_greed = feed.load_history()
    (out / "fear_greed.json").write_text(
        json.dumps([{"t": t, "v": v, "label": label} for t, v, label in fear_greed]),
        encoding="utf-8",
    )
    ablation = sentiment_ablation(cfg, candles, rules, unavailable, fear_greed)
    (out / "sentiment_ablation.json").write_text(json.dumps(ablation, indent=2), encoding="utf-8")
    if ablation.get("gate_helps"):
        cfg.setdefault("sentiment", {})["block_extreme_greed"] = True
        cfg.setdefault("validation", {})["n_trials"] = int((cfg.get("validation") or {}).get("n_trials", 6)) + 1
        logging.info("fear & greed gate kept: the pre-specified ablation rule passed")
    else:
        cfg.setdefault("sentiment", {})["block_extreme_greed"] = False
        logging.info("fear & greed gate left off: the ablation did not clear the pre-specified bar")
    summary = run_backtest(cfg, candles, rules, unavailable, out, fear_greed=fear_greed)
    walk = run_walk_forward(cfg, candles, rules, unavailable, fear_greed=fear_greed)
    walk["full_sample"] = summary.get("deflated_sharpe")
    (out / "walk_forward.json").write_text(json.dumps(walk, indent=2), encoding="utf-8")
    report = build_report(out, out, title="Wallex backtest")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(report)


def _write_candles(directory: Path, candles: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for symbol, bars in candles.items():
        path = directory / f"{symbol}.csv"
        with path.open("w", encoding="utf-8") as handle:
            handle.write("time,open,high,low,close,volume\n")
            for bar in bars:
                handle.write(f"{bar.time},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}\n")


def _aggregation_check(client: WallexClient, out: Path) -> None:
    """Show that 1-minute history, aggregated locally, lines up with native hourly bars."""
    end = int(time.time())
    start = end - 2 * 86400
    try:
        minutes = client.get_udf("BTCTMN", "15", start, end)
        native = client.get_udf("BTCTMN", "60", start, end)
    except Exception as exc:
        (out / "aggregation_check.json").write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
        return
    aggregated = aggregate_bars(minutes, 3600)
    native_map = {bar.time: bar.close for bar in native}
    overlap = [bar for bar in aggregated if bar.time in native_map and bar.close]
    if not overlap:
        payload = {"ok": False, "minute_bars": len(minutes), "hourly_bars": len(native), "note": "no overlap"}
    else:
        errors = [abs(bar.close - native_map[bar.time]) / bar.close for bar in overlap]
        payload = {
            "ok": True,
            "symbol": "BTCTMN",
            "minute_bars": len(minutes),
            "minute_spacing_seconds": median_spacing(minutes),
            "aggregated_hourly": len(aggregated),
            "native_hourly": len(native),
            "overlap": len(overlap),
            "median_abs_close_error": sorted(errors)[len(errors) // 2],
        }
    (out / "aggregation_check.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
