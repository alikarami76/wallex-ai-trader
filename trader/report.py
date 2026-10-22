"""Build a markdown performance report and charts from a journal directory."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def build_report(log_dir: Path, out_dir: Path | None = None, title: str = "Wallex trader report") -> Path:
    log_dir = Path(log_dir)
    out_dir = Path(out_dir or log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    equity = _read_csv(log_dir / "equity.csv")
    trades = _read_csv(log_dir / "trades.csv")
    decisions = _read_jsonl(log_dir / "decisions.jsonl")
    stats = _stats(equity, trades, decisions)
    _charts(equity, out_dir)
    path = out_dir / "report.md"
    path.write_text(_markdown(title, stats, out_dir), encoding="utf-8")
    return path


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _stats(equity: list[dict], trades: list[dict], decisions: list[dict]) -> dict:
    series = []
    for row in equity:
        try:
            stamp = datetime.fromisoformat(row["timestamp"])
            series.append((stamp, float(row["equity_tmn"])))
        except (KeyError, TypeError, ValueError):
            continue
    start = series[0][1] if series else 0.0
    end = series[-1][1] if series else 0.0
    ret = (end / start - 1) if start else 0.0
    peak = start
    max_dd = 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)
    rets = []
    dts = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        if prev:
            rets.append(series[i][1] / prev - 1)
            dts.append((series[i][0] - series[i - 1][0]).total_seconds())
    sharpe = _sharpe(rets, dts)
    sells = [row for row in trades if row.get("side") == "SELL" and row.get("pnl_tmn") not in ("", None)]
    pnls = [float(row["pnl_tmn"]) for row in sells]
    wins = [pnl for pnl in pnls if pnl > 0]
    fees = 0.0
    for row in trades:
        try:
            fees += float(row.get("fee_tmn") or 0)
        except ValueError:
            pass
    sources = {}
    errors = 0
    latencies = []
    for row in decisions:
        source = ((row.get("agent") or {}).get("source")) if isinstance(row.get("agent"), dict) else None
        if source:
            sources[source] = sources.get(source, 0) + 1
        if row.get("event") == "error" or row.get("error"):
            errors += 1
        if row.get("latency_ms") is not None:
            try:
                latencies.append(float(row["latency_ms"]))
            except (TypeError, ValueError):
                pass
    latencies.sort()
    realized = sum(pnls)
    return {
        "points": len(series),
        "start": start,
        "end": end,
        "return_pct": ret * 100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "round_trips": len(pnls),
        "win_rate_pct": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "avg_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
        "fees": fees,
        "fills": len(trades),
        "cycles": len(decisions),
        "agent_sources": sources,
        "errors": errors,
        "realized_pnl": realized,
        "latency_median_ms": latencies[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
        "latency_samples": len(latencies),
        "residuals": _latest_residuals(decisions),
        "decision_cards": decision_cards(decisions),
    }


def _latest_residuals(decisions: list[dict]) -> list[dict]:
    latest: list[dict] = []
    for row in decisions:
        if "residuals" in row:
            latest = list(row.get("residuals") or [])
    return latest


def decision_cards(decisions: list[dict]) -> str:
    """Latest-cycle decision cards. Feature numbers, the cost gate, and the size change."""
    symbols = [row for row in decisions if row.get("record") == "symbol"]
    if not symbols:
        return ""
    cycle = symbols[-1].get("cycle_id")
    rows = [row for row in symbols if row.get("cycle_id") == cycle] if cycle else symbols[-6:]
    lines = ["## Decision card", ""]
    for row in rows:
        llm = row.get("llm") if isinstance(row.get("llm"), dict) else {}
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        gate = row.get("cost_gate") if isinstance(row.get("cost_gate"), dict) else {}
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        features = ", ".join(
            f"{key}={signal.get(key)}"
            for key in ("close", "roc_24h", "roc_4h", "atr_pct", "spread_bps", "signal")
            if key in signal
        ) or "n/a"
        reasons = ", ".join(str(item) for item in (risk.get("reason_codes") or [])) or "none"
        lines.append(f"### {row.get('symbol')}")
        lines.append("")
        lines.append(f"First block: {row.get('first_block') or 'none'}.")
        lines.append(f"Features: {features}.")
        lines.append(
            "Cost gate: "
            f"prior {gate.get('prior')}, "
            f"fee in {gate.get('fee_in_bps')} bps, "
            f"fee out {gate.get('fee_out_bps')} bps, "
            f"half-spread {gate.get('half_spread_bps')} bps, "
            f"buffer {gate.get('buffer_bps')} bps, "
            f"hurdle {gate.get('hurdle')}."
        )
        lines.append(
            f"LLM source {llm.get('source') or 'n/a'}, "
            f"confidence {llm.get('confidence')}, "
            f"rationale: {llm.get('rationale') or 'n/a'}."
        )
        lines.append(
            f"Size {risk.get('size_before')} → {risk.get('size_after')}. Reasons: {reasons}."
        )
        lines.append("")
    return "\n".join(lines)


def _residual_lines(rows: list[dict]) -> str:
    if not rows:
        return "Residual coin: none."
    lines = ["Residual coin still held:", ""]
    for row in rows:
        lines.append(
            f"- {row.get('symbol')}: qty {row.get('qty')} "
            f"mark {row.get('mark_tmn')} TMN. {row.get('reason')}"
        )
    return "\n".join(lines)


def _sharpe(rets: list[float], dts: list[float]) -> float | None:
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    median_dt = sorted(dts)[len(dts) // 2] if dts else 3600
    if median_dt <= 0:
        median_dt = 3600
    periods = (365 * 24 * 3600) / median_dt
    return (mean / std) * math.sqrt(periods)


def _charts(equity: list[dict], out_dir: Path) -> None:
    stamps = []
    values = []
    for row in equity:
        try:
            stamps.append(datetime.fromisoformat(row["timestamp"]))
            values.append(float(row["equity_tmn"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not values:
        return
    peak = values[0]
    drawdown = []
    for value in values:
        peak = max(peak, value)
        drawdown.append((value / peak - 1) * 100 if peak else 0)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(stamps, values, color="#0f6f6a", linewidth=1.6)
    ax.set_title("Equity (TMN)")
    ax.set_ylabel("TMN")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "equity.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 3.4))
    ax.fill_between(stamps, drawdown, 0, color="#9a3b2f", alpha=0.8)
    ax.set_title("Drawdown (%)")
    ax.set_ylabel("%")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "drawdown.png", dpi=120)
    plt.close(fig)


def _latency(stats: dict) -> str:
    if stats.get("latency_median_ms") is None:
        return "n/a"
    return (
        f"median {stats['latency_median_ms']:.0f} ms, "
        f"p95 {stats['latency_p95_ms']:.0f} ms "
        f"({stats['latency_samples']} cycles)"
    )


def _walk_forward(out_dir: Path) -> str:
    path = out_dir / "walk_forward.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    lines = ["", "## Walk-forward and deflated Sharpe", ""]
    full = payload.get("full_sample") or {}
    if full.get("ok"):
        lines.append(
            f"Full sample: per-bar Sharpe {full.get('sharpe_per_bar')}, "
            f"selection hurdle {full.get('hurdle_sharpe')} at N={full.get('n_trials')}, "
            f"deflated Sharpe probability {full.get('deflated_sharpe_probability')}."
        )
        lines.append("")
    lines.append("| Fold | Return | Realized PnL | Fees | Fold DSR |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in payload.get("folds_result") or []:
        lines.append(
            f"| {row.get('fold')} | {row.get('return_pct')}% | {row.get('realized_pnl')} | "
            f"{row.get('fees_paid')} | {row.get('deflated_sharpe_probability')} |"
        )
    lines.append("")
    lines.append(
        "Folds use the same frozen parameters and a fresh 5,000,000 TMN. "
        "The deflated Sharpe probability is Bailey and López de Prado (2014): "
        "the chance the observed per-bar Sharpe beats the expected maximum of N null trials, "
        "after skew and kurtosis. It is not an annualized Sharpe."
    )
    return "\n".join(lines)


def _markdown(title: str, stats: dict, out_dir: Path) -> str:
    sharpe = "n/a" if stats["sharpe"] is None else f"{stats['sharpe']:.2f}"
    sources = ", ".join(f"{key}={value}" for key, value in sorted(stats["agent_sources"].items())) or "none"
    mtm = stats["end"] - stats["start"]
    walk = _walk_forward(out_dir)
    return f"""# {title}

## Desk metrics

| Metric | Value |
| --- | --- |
| PnL after fees (mark-to-market) | {mtm:,.0f} TMN ({stats['return_pct']:.3f}%) |
| PnL after fees (realized) | {stats['realized_pnl']:,.0f} TMN |
| Sharpe (annualized from the equity spacing) | {sharpe} |
| Max drawdown | {stats['max_drawdown_pct']:.3f}% |
| Hit rate | {stats['win_rate_pct']:.1f}% ({stats['round_trips']} closed trades) |
| Decision latency | {_latency(stats)} |

| Other | Value |
| --- | --- |
| Starting equity | {stats['start']:,.0f} TMN |
| Ending equity | {stats['end']:,.0f} TMN |
| Average closed-trade PnL | {stats['avg_pnl']:,.0f} TMN |
| Fees paid | {stats['fees']:,.0f} TMN |
| Fills | {stats['fills']} |
| Decision cycles | {stats['cycles']} |
| Agent sources | {sources} |
| Logged errors | {stats['errors']} |

Equity samples: {stats['points']}.

{_residual_lines(stats.get('residuals') or [])}

{stats.get('decision_cards') or ''}

![Equity](equity.png)

![Drawdown](drawdown.png)

The return is mark-to-market in toman, including fees. Hit rate counts a closed trade as a win when its realized toman PnL is positive. Sharpe uses the median spacing of the equity samples, so an hourly backtest and a short shadow run are not on the same scale. Latency is the decision cycle, from the book fetch through the risk plan.
{walk}
"""
