"""Offline journal audit. No network and no API keys.

Reads the jsonl journals, rebuilds cash and coin from fills, and writes a
report. A secret-like string fails the audit. The matched text is not copied
into the report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.engine.ops import read_jsonl


DRIFT_TMN = 1.0
GAP_SECONDS = 300.0
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

_SECRET_PATTERNS = (
    ("assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|bot[_-]?token|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"
    )),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{16,}")),
    ("telegram_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{8,}")),
)

_JSONL_NAMES = (
    "cycles.jsonl",
    "decisions.jsonl",
    "adjustments.jsonl",
    "reviews.jsonl",
    "replay.jsonl",
)

_SUBMITTED = {"filled", "submitted", "open", "new", "partial", "partially_filled"}


def write_audit(
    log_dir: Path,
    dest: Path,
    *,
    starting_cash: float = 5_000_000,
    config_hash: str | None = None,
    wallex_snapshot: Path | None = None,
) -> dict:
    report = build_audit(
        Path(log_dir),
        starting_cash=starting_cash,
        config_hash=config_hash,
        wallex_snapshot=Path(wallex_snapshot) if wallex_snapshot else None,
    )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (dest / "audit.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def audit_summary(report: dict) -> str:
    """Short block for a checkpoint review. No fill prices and no secrets."""
    lines = [
        "",
        "## Audit",
        f"ok: {str(bool(report.get('ok'))).lower()}",
        f"drift_tmn: {report.get('max_drift_tmn')}",
        f"duplicate_client_ids: {len(report.get('duplicate_client_ids') or [])}",
        f"gaps_over_5min: {len(report.get('gaps') or [])}",
        f"clock_mismatches: {len(report.get('clock_mismatches') or [])}",
        f"order_failures: {len(report.get('order_failures') or [])}",
        f"secret_hits: {len(report.get('secret_hits') or [])}",
        f"fee_bill_tmn: {report.get('fee_bill_tmn')}",
    ]
    return "\n".join(lines) + "\n"


def build_audit(
    log_dir: Path,
    *,
    starting_cash: float = 5_000_000,
    config_hash: str | None = None,
    wallex_snapshot: Path | None = None,
) -> dict:
    log_dir = Path(log_dir)
    files = {name: read_jsonl(log_dir / name) for name in _JSONL_NAMES}
    cycles = _cycle_rows(files)
    fills = _fills(files["decisions.jsonl"])
    cash, coins, priced = _apply(float(starting_cash), fills)
    drift = _equity_drift(cycles, files["decisions.jsonl"], float(starting_cash))
    gaps = _gaps(cycles)
    clocks = _clocks(files)
    duplicates = _duplicate_ids(files["decisions.jsonl"])
    orders = _order_checks(files["decisions.jsonl"], config_hash)
    slippage = _slippage(fills)
    secrets = _secrets(log_dir)
    snapshot = _snapshot(wallex_snapshot, cash, coins, priced) if wallex_snapshot else None
    failures = []
    if any(item["drift_tmn"] > DRIFT_TMN for item in drift):
        failures.append("equity drift over 1 TMN")
    if duplicates:
        failures.append("duplicate client_id")
    if gaps:
        failures.append("cycle gap over 5 min")
    if clocks:
        failures.append("ts_utc and ts_tehran disagree")
    if orders:
        failures.append("submitted order failed the contract check")
    if secrets:
        failures.append("secret-like string")
    if snapshot and snapshot.get("failures"):
        failures.append("wallex snapshot drift")
    return {
        "ok": not failures,
        "failures": failures,
        "starting_cash_tmn": float(starting_cash),
        "cash_tmn": round(cash, 4),
        "coins": {asset: qty for asset, qty in sorted(coins.items()) if abs(qty) > 1e-12},
        "max_drift_tmn": round(max((item["drift_tmn"] for item in drift), default=0.0), 4),
        "drift": drift,
        "duplicate_client_ids": duplicates,
        "gaps": gaps,
        "clock_mismatches": clocks,
        "order_failures": orders,
        "fills": slippage,
        "fee_bill_tmn": round(sum(float(item.get("fee_tmn") or 0) for item in fills), 4),
        "secret_hits": secrets,
        "snapshot": snapshot,
        "rows": {name: len(rows) for name, rows in files.items()},
    }


def render_markdown(report: dict) -> str:
    verdict = "PASS" if report["ok"] else "FAIL"
    lines = [
        "# Journal audit",
        "",
        f"Result: **{verdict}**.",
        "",
        f"Rebuilt cash {report['cash_tmn']} TMN from a start of {report['starting_cash_tmn']} TMN.",
        f"Largest equity drift {report['max_drift_tmn']} TMN. A drift over {DRIFT_TMN:.0f} TMN fails.",
        "",
        "## Fee bill",
        "",
        f"Fee bill: {report['fee_bill_tmn']} TMN.",
        "",
        "## Fills",
        "",
    ]
    if not report["fills"]:
        lines.append("No fills.")
    for item in report["fills"]:
        slip = "n/a" if item["slippage_bps"] is None else f"{item['slippage_bps']:.2f}"
        lines.append(
            f"- {item['symbol']} {item['side']} {item['liquidity']} "
            f"slippage {slip} bps vs mid, fee {item['fee_tmn']} TMN."
        )
    lines.extend(["", "## Drift", ""])
    if not report["drift"]:
        lines.append("No logged equity to compare.")
    for item in report["drift"]:
        lines.append(
            f"- {item['ts']}: rebuilt {item['rebuilt_equity_tmn']} vs logged {item['logged_equity_tmn']} "
            f"(drift {item['drift_tmn']} TMN)."
        )
    lines.extend(["", "## Other checks", ""])
    lines.append(f"Duplicate client ids: {len(report['duplicate_client_ids'])}.")
    for item in report["duplicate_client_ids"]:
        lines.append(f"- {item['client_id']} x{item['count']}")
    lines.append(f"Cycle gaps over 5 min: {len(report['gaps'])}.")
    for item in report["gaps"]:
        lines.append(f"- {item['from_ts']} → {item['to_ts']} ({item['seconds']:.0f}s)")
    lines.append(f"Clock mismatches: {len(report['clock_mismatches'])}.")
    for item in report["clock_mismatches"]:
        lines.append(f"- {item['file']} line {item['line']}: {item['reason']}")
    lines.append(f"Submitted-order failures: {len(report['order_failures'])}.")
    for item in report["order_failures"]:
        lines.append(f"- {item['symbol']} {item['client_id']}: {item['reason']}")
    if report["snapshot"] is not None:
        lines.append(f"Wallex snapshot failures: {len(report['snapshot']['failures'])}.")
        for item in report["snapshot"]["failures"]:
            lines.append(f"- {item['asset']}: {item['reason']}")
    lines.append(f"Secret-like hits: {len(report['secret_hits'])}.")
    for item in report["secret_hits"]:
        lines.append(f"- {item['file']} line {item['line']}: {item['pattern']}")
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        for item in report["failures"]:
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _cycle_rows(files: dict[str, list[dict]]) -> list[dict]:
    rows = list(files["cycles.jsonl"])
    if rows:
        return rows
    return [row for row in files["decisions.jsonl"] if "equity_tmn" in row and row.get("record") != "symbol"]


def _fills(decisions: list[dict]) -> list[dict]:
    found = []
    seen: set[tuple] = set()
    for row in decisions:
        blobs = []
        if isinstance(row.get("fills"), list):
            for item in row["fills"]:
                if isinstance(item, dict):
                    blobs.append(item)
        order = row.get("order")
        if isinstance(order, dict) and str(order.get("status") or "").lower() == "filled":
            blobs.append({
                "symbol": row.get("symbol") or order.get("symbol"),
                "side": order.get("side"),
                "price": order.get("limit_price"),
                "qty": order.get("filled_qty") if order.get("filled_qty") is not None else order.get("qty"),
                "notional_tmn": order.get("notional_tmn"),
                "fee_tmn": order.get("fee_tmn") or 0,
                "order_type": order.get("type"),
                "client_id": order.get("client_id"),
                "liquidity_flag": order.get("liquidity_flag"),
            })
        for blob in blobs:
            side = str(blob.get("side") or "").upper()
            symbol = str(blob.get("symbol") or row.get("symbol") or "")
            if side not in {"BUY", "SELL"} or not symbol:
                continue
            client_id = str(blob.get("client_id") or "")
            price = _float(blob.get("price"))
            qty = _float(blob.get("qty"))
            key = (client_id, symbol, side, price, qty)
            if client_id and key in seen:
                continue
            if client_id:
                seen.add(key)
            notional = _float(blob.get("notional_tmn"))
            if notional <= 0 and price > 0 and qty > 0:
                notional = price * qty
            found.append({
                "symbol": symbol,
                "side": side,
                "price": price,
                "qty": qty,
                "notional_tmn": notional,
                "fee_tmn": _float(blob.get("fee_tmn")),
                "order_type": str(blob.get("order_type") or blob.get("type") or ""),
                "client_id": client_id,
                "liquidity_flag": blob.get("liquidity_flag"),
                "mid": _mid(row, blob),
                "ts": str(row.get("ts_tehran") or row.get("timestamp") or row.get("ts_utc") or ""),
                "marks": _marks(row),
            })
    return found


def _apply(cash: float, fills: list[dict]) -> tuple[float, dict[str, float], dict[str, float]]:
    coins: dict[str, float] = {}
    priced: dict[str, float] = {}
    for fill in fills:
        cash, coins, priced = _apply_one(cash, coins, priced, fill)
    return cash, coins, priced


def _apply_one(cash, coins, priced, fill):
    asset = _base(fill["symbol"])
    price = float(fill["price"] or 0)
    qty = float(fill["qty"] or 0)
    notional = float(fill["notional_tmn"] or 0)
    fee = float(fill["fee_tmn"] or 0)
    coins = dict(coins)
    priced = dict(priced)
    if price > 0:
        priced[asset] = price
    if fill["side"] == "BUY":
        cash -= notional if notional > 0 else price * qty
        coins[asset] = coins.get(asset, 0.0) + qty
    else:
        gross = notional if notional > 0 else price * qty
        cash += gross - fee
        coins[asset] = coins.get(asset, 0.0) - qty
    return cash, coins, priced


def _equity_drift(cycles: list[dict], decisions: list[dict], starting_cash: float) -> list[dict]:
    """Replay fills in log order and compare each logged equity to cash plus coin marks."""
    cycle_ids = {row.get("cycle_id") for row in cycles if row.get("cycle_id")}
    by_cycle: dict[str, list[dict]] = {}
    if cycle_ids:
        for row in decisions:
            if row.get("record") != "symbol" or row.get("cycle_id") not in cycle_ids:
                continue
            by_cycle.setdefault(str(row.get("cycle_id")), []).extend(_fills([row]))
    cash = float(starting_cash)
    coins: dict[str, float] = {}
    priced: dict[str, float] = {}
    rows = []
    for row in cycles:
        if cycle_ids:
            for fill in by_cycle.get(str(row.get("cycle_id")), []):
                cash, coins, priced = _apply_one(cash, coins, priced, fill)
        else:
            for fill in _fills([row]):
                cash, coins, priced = _apply_one(cash, coins, priced, fill)
        logged = row.get("equity_tmn")
        if logged is None:
            continue
        marks = _marks(row) or priced
        equity = _marked(cash, coins, marks, priced)
        logged_value = float(logged)
        rows.append({
            "ts": str(row.get("ts_tehran") or row.get("timestamp") or row.get("ts_utc") or ""),
            "rebuilt_equity_tmn": round(equity, 4),
            "logged_equity_tmn": round(logged_value, 4),
            "drift_tmn": round(abs(equity - logged_value), 4),
        })
    return rows


def _marked(cash: float, coins: dict[str, float], marks: dict[str, float], priced: dict[str, float]) -> float:
    equity = cash
    for asset, qty in coins.items():
        mark = marks.get(asset)
        if mark is None:
            mark = priced.get(asset) or 0.0
        equity += qty * float(mark or 0)
    return equity


def _marks(row: dict) -> dict[str, float]:
    marks = {}
    for feature in row.get("features") or []:
        if isinstance(feature, dict) and feature.get("symbol") and feature.get("close") is not None:
            marks[_base(str(feature["symbol"]))] = float(feature["close"])
    signal = row.get("signal")
    if isinstance(signal, dict) and signal.get("close") is not None and row.get("symbol"):
        marks[_base(str(row["symbol"]))] = float(signal["close"])
    return marks


def _gaps(cycles: list[dict]) -> list[dict]:
    stamped = []
    for row in cycles:
        moment = _instant(row.get("ts_utc") or row.get("ts_tehran") or row.get("timestamp"))
        if moment is None:
            continue
        stamped.append((moment, str(row.get("ts_tehran") or row.get("timestamp") or row.get("ts_utc"))))
    stamped.sort(key=lambda item: item[0])
    gaps = []
    for previous, current in zip(stamped, stamped[1:]):
        seconds = (current[0] - previous[0]).total_seconds()
        if seconds > GAP_SECONDS:
            gaps.append({"from_ts": previous[1], "to_ts": current[1], "seconds": seconds})
    return gaps


def _clocks(files: dict[str, list[dict]]) -> list[dict]:
    mismatches = []
    for name, rows in files.items():
        for index, row in enumerate(rows, start=1):
            reason = _clock_reason(row)
            if reason:
                mismatches.append({"file": name, "line": index, "reason": reason})
    return mismatches


def _clock_reason(row: dict) -> str:
    if row.get("ts_tehran"):
        tehran = _parse(row.get("ts_tehran"))
        if tehran is None or tehran.utcoffset() != TEHRAN_OFFSET:
            return "ts_tehran is not +03:30"
        if row.get("ts_utc"):
            utc = _instant(row.get("ts_utc"))
            if utc is None or abs((tehran.astimezone(timezone.utc) - utc).total_seconds()) > 1:
                return "ts_utc and ts_tehran are different instants"
        return ""
    stamp = _parse(row.get("timestamp")) if row.get("timestamp") else None
    if stamp is not None and stamp.utcoffset() not in {None, TEHRAN_OFFSET}:
        return "timestamp offset is not +03:30"
    return ""


def _duplicate_ids(decisions: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in decisions:
        for client_id in _client_ids(row):
            counts[client_id] = counts.get(client_id, 0) + 1
    return [{"client_id": key, "count": count} for key, count in sorted(counts.items()) if count > 1]


def _client_ids(row: dict) -> list[str]:
    found = []
    order = row.get("order")
    if isinstance(order, dict) and order.get("client_id"):
        found.append(str(order["client_id"]))
    for blob in row.get("fills") or []:
        if isinstance(blob, dict) and blob.get("client_id"):
            found.append(str(blob["client_id"]))
    for blob in row.get("requests") or []:
        if isinstance(blob, dict) and blob.get("client_id"):
            found.append(str(blob["client_id"]))
    return found


def _order_checks(decisions: list[dict], expected_hash: str | None) -> list[dict]:
    failures = []
    for row in decisions:
        order = row.get("order")
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").lower()
        if status not in _SUBMITTED:
            continue
        reasons = []
        trace = row.get("gate_trace") or []
        if not trace or any(item.get("pass") is False for item in trace if isinstance(item, dict)):
            reasons.append("gate_trace is missing or not all-pass")
        git_sha = str(row.get("git_sha") or "")
        go_live = str(row.get("go_live_sha") or "")
        if not git_sha or git_sha != go_live:
            reasons.append("git_sha does not equal go_live_sha")
        seen_hash = str(row.get("config_hash") or "")
        if expected_hash and seen_hash != expected_hash:
            reasons.append("config_hash does not match the config")
        elif not seen_hash:
            reasons.append("config_hash is missing")
        if reasons:
            failures.append({
                "symbol": row.get("symbol"),
                "client_id": order.get("client_id"),
                "reason": "; ".join(reasons),
            })
    return failures


def _slippage(fills: list[dict]) -> list[dict]:
    rows = []
    for fill in fills:
        liquidity = fill.get("liquidity_flag")
        if not liquidity:
            liquidity = "taker" if str(fill.get("order_type")).upper() == "MARKET" else "maker"
        mid = float(fill.get("mid") or 0)
        price = float(fill.get("price") or 0)
        slip = None
        if mid > 0 and price > 0:
            raw = (price - mid) / mid * 10_000
            slip = raw if fill["side"] == "BUY" else -raw
        rows.append({
            "symbol": fill["symbol"],
            "side": fill["side"],
            "liquidity": liquidity,
            "slippage_bps": None if slip is None else round(slip, 4),
            "fee_tmn": round(float(fill.get("fee_tmn") or 0), 4),
        })
    return rows


def _secrets(log_dir: Path) -> list[dict]:
    hits = []
    for name in _JSONL_NAMES:
        path = log_dir / name
        if not path.exists():
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern, compiled in _SECRET_PATTERNS:
                if compiled.search(line):
                    hits.append({"file": name, "line": index, "pattern": pattern})
                    break
    return hits


def _snapshot(path: Path | None, cash: float, coins: dict[str, float], priced: dict[str, float]) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    balances = _snapshot_balances(payload)
    failures = []
    expected = {"TMN": cash, **coins}
    assets = set(balances) | {asset for asset, qty in expected.items() if abs(qty) > 1e-8}
    for asset in sorted(assets):
        got = float(balances.get(asset, 0.0))
        want = float(expected.get(asset, 0.0))
        if asset == "TMN":
            if abs(got - want) > DRIFT_TMN:
                failures.append({"asset": asset, "reason": f"TMN drift {abs(got - want):.4f}"})
            continue
        price = float(priced.get(asset) or 0)
        notional = abs(got - want) * price
        if price > 0 and notional > DRIFT_TMN:
            failures.append({"asset": asset, "reason": f"notional drift {notional:.4f} TMN"})
        elif price <= 0 and abs(got - want) > 1e-8:
            failures.append({"asset": asset, "reason": "quantity differs and no mark is available"})
    return {"failures": failures, "assets": sorted(balances)}


def _snapshot_balances(payload: dict) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("balances") or payload.get("result") or payload
    if isinstance(raw, dict) and "balances" in raw and isinstance(raw["balances"], dict):
        raw = raw["balances"]
    balances = {}
    if not isinstance(raw, dict):
        return balances
    for asset, value in raw.items():
        if asset in {"ok", "result", "balances"}:
            continue
        if isinstance(value, dict):
            amount = value.get("total", value.get("value", value.get("available")))
        else:
            amount = value
        try:
            balances[str(asset)] = float(amount)
        except (TypeError, ValueError):
            continue
    return balances


def _mid(row: dict, blob: dict | None = None) -> float:
    if blob and blob.get("mid") is not None:
        return _float(blob.get("mid"))
    signal = row.get("signal")
    if isinstance(signal, dict) and signal.get("close") is not None:
        return _float(signal.get("close"))
    symbol = str((blob or {}).get("symbol") or row.get("symbol") or "")
    for feature in row.get("features") or []:
        if isinstance(feature, dict) and str(feature.get("symbol") or "") == symbol and feature.get("close") is not None:
            return _float(feature.get("close"))
    return 0.0


def _base(symbol: str) -> str:
    text = symbol.upper()
    return text[:-3] if text.endswith("TMN") else text


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def _instant(value) -> datetime | None:
    parsed = _parse(value)
    if parsed is None:
        return None
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
