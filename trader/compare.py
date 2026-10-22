"""Replay stored candles and diff the trade list against a recorded commit.

The run is offline. Market precision is the 2026-10-22 public snapshot that
the recorded backtest used, so a later tick-size change cannot move the diff.
Results are written beside that backtest, not over it.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import subprocess
from pathlib import Path

from trader.engine.backtest import run_backtest
from trader.engine.ops import config_hash, git_sha
from trader.scorecard import load_candles
from trader.types import MarketRule


# tickSize, stepSize, minQty from GET /v1/markets on 2026-10-22. Quote is TMN.
RECORDED_PRECISION = {
    "USDTTMN": (0, 2, 0.01),
    "BTCTMN": (0, 8, 1e-8),
    "ETHTMN": (0, 5, 1e-5),
    "XAUTTMN": (0, 5, 1e-5),
    "XRPTMN": (0, 1, 0.1),
    "DOGETMN": (0, 1, 0.1),
}


def recorded_rules(symbols: list[str]) -> dict[str, MarketRule]:
    rules = {}
    for symbol in symbols:
        price_decimals, qty_decimals, min_qty = RECORDED_PRECISION.get(symbol, (0, 8, 1e-8))
        rules[symbol] = MarketRule(
            symbol, symbol[:-3], "TMN", price_decimals, qty_decimals, min_qty, 50_000,
        )
    return rules


def scale_fees(cfg: dict, fee_multiple: float, extra_slippage_bps: float) -> dict:
    """Copy the config and scale fees. The caller's config is left unchanged."""
    scaled = copy.deepcopy(cfg)
    fees = scaled.setdefault("fees", {})
    fees["maker_rate"] = float(fees.get("maker_rate", 0.0)) * fee_multiple
    fees["taker_rate"] = float(fees.get("taker_rate", 0.0)) * fee_multiple
    fees["slippage_bps"] = float(fees.get("slippage_bps", 0.0)) + extra_slippage_bps
    return scaled


def _fear_greed(path: Path) -> list[tuple[int, float, str]]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [(int(row["t"]), float(row["v"]), str(row.get("label") or "")) for row in rows]


def replay(cfg: dict, backtest_dir: Path, out_dir: Path | None = None) -> dict:
    candles = load_candles(backtest_dir / "candles")
    summary = run_backtest(
        cfg,
        candles,
        recorded_rules(list(candles)),
        set(),
        out_dir,
        fear_greed=_fear_greed(backtest_dir / "fear_greed.json"),
    )
    return summary


def _git_text(sha: str, path: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"{sha}:{path}"],
        text=True,
        stderr=subprocess.DEVNULL,
    )


def _rev_parse(sha: str) -> str:
    return subprocess.check_output(["git", "rev-parse", sha], text=True, stderr=subprocess.DEVNULL).strip()


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def diff_trades(base_text: str, head_text: str) -> dict:
    base = _rows(base_text)
    head = _rows(head_text)
    limit = max(len(base), len(head))
    deltas = []
    for index in range(limit):
        left = base[index] if index < len(base) else None
        right = head[index] if index < len(head) else None
        if left != right:
            deltas.append({"row": index + 1, "base": left, "head": right})
            if len(deltas) >= 8:
                break
    return {
        "identical": base == head,
        "base_trades": len(base),
        "head_trades": len(head),
        "deltas": deltas,
    }


def _pnl(summary: dict) -> dict:
    return {
        "return_pct": summary.get("return_pct"),
        "closed_trades": summary.get("closed_trades"),
        "ending_equity": summary.get("ending_equity"),
        "realized_pnl": summary.get("realized_pnl"),
        "fees_paid": summary.get("fees_paid"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
    }


def compare_backtest(cfg: dict, sha_a: str, sha_b: str, backtest_dir: Path, dest: Path) -> dict:
    """Diff trade lists at two commits. HEAD replays the working tree on stored candles."""
    backtest_dir = Path(backtest_dir)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    resolved_a = _rev_parse(sha_a)
    resolved_b = _rev_parse(sha_b)
    replay_b = sha_b == "HEAD" or resolved_b == _rev_parse("HEAD")
    base_trades = _git_text(resolved_a, "results/backtest/trades.csv")
    base_summary = json.loads(_git_text(resolved_a, "results/backtest/summary.json"))
    summary = None
    if replay_b:
        head_dir = dest / "head"
        if head_dir.exists():
            for child in head_dir.iterdir():
                if child.is_file():
                    child.unlink()
        summary = replay(cfg, backtest_dir, head_dir)
        head_trades = (head_dir / "trades.csv").read_text(encoding="utf-8")
        head_summary = summary
    else:
        head_trades = _git_text(resolved_b, "results/backtest/trades.csv")
        head_summary = json.loads(_git_text(resolved_b, "results/backtest/summary.json"))
    trades = diff_trades(base_trades, head_trades)
    if summary is None:
        summary = replay(cfg, backtest_dir, None)
    sensitivity = [{**_pnl(summary), "fee_multiple": 1.0, "extra_slippage_bps": 0.0}]
    for multiple, extra in ((1.5, 0.0), (1.0, 5.0), (1.5, 5.0)):
        scaled = scale_fees(cfg, multiple, extra)
        row = _pnl(replay(scaled, backtest_dir, None))
        row["fee_multiple"] = multiple
        row["extra_slippage_bps"] = extra
        sensitivity.append(row)
    report = {
        "sha_a": resolved_a,
        "sha_b": resolved_b,
        "replayed_b": replay_b,
        "base_sha": resolved_a,
        "head_sha": resolved_b,
        "config_hash": config_hash(cfg),
        "identical": trades["identical"],
        "base": _pnl(base_summary),
        "head": _pnl(head_summary),
        "trades": trades,
        "sensitivity": sensitivity,
    }
    (dest / "compare.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (dest / "compare.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    base = report["base"]
    head = report["head"]
    verdict = "IDENTICAL" if report["identical"] else "DIFFERENT"
    lines = [
        "# Backtest compare",
        "",
        f"`{report['sha_a']}` versus `{report['sha_b']}`.",
        f"Config hash `{report['config_hash']}`.",
        (
            "The second ref is HEAD, so its trade list is a replay of the stored candles."
            if report.get("replayed_b")
            else "Both trade lists are the files committed at those SHAs."
        ),
        "",
        f"Trades and PnL: **{verdict}**.",
        "",
        "| | return | closed trades | ending equity | realized PnL | fees |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        f"| sha_a | {base.get('return_pct')}% | {base.get('closed_trades')} | {base.get('ending_equity')} | {base.get('realized_pnl')} | {base.get('fees_paid')} |",
        f"| sha_b | {head.get('return_pct')}% | {head.get('closed_trades')} | {head.get('ending_equity')} | {head.get('realized_pnl')} | {head.get('fees_paid')} |",
        "",
        "## Sensitivity",
        "",
        "Fee multiples and the extra slippage are applied only in this table. The strategy file is unchanged.",
        "",
        "| fees | extra slippage | return | closed trades | realized PnL |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["sensitivity"]:
        lines.append(
            f"| {row['fee_multiple']:.1f}× | +{row['extra_slippage_bps']:.0f} bps | "
            f"{row['return_pct']}% | {row['closed_trades']} | {row['realized_pnl']} |"
        )
    if report["trades"]["deltas"]:
        lines.extend(["", "## First trade differences", ""])
        for item in report["trades"]["deltas"]:
            lines.append(f"- row {item['row']}: base `{item['base']}` / HEAD `{item['head']}`")
    lines.append("")
    return "\n".join(lines)
