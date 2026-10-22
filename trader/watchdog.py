"""Sidecar that tails journals and sends Telegram alerts.

Off unless WATCHDOG_ENABLED is set. It does not place orders, call the model,
or write trader files. Alert text is the event and the percent PnL.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path


log = logging.getLogger("trader.watchdog")

INTERVAL_SECONDS = 900.0
GAP_SECONDS = 300.0
POLL_SECONDS = 5.0


def enabled() -> bool:
    return os.getenv("WATCHDOG_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Tail journals and alert. Off unless WATCHDOG_ENABLED is set.")
    parser.add_argument("--log-dir", default="results/live")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not enabled():
        print("watchdog off")
        return 0
    run(Path(args.log_dir), once=args.once)
    return 0


def run(
    log_dir: Path,
    *,
    once: bool = False,
    post=None,
    now: float | None = None,
    state: dict | None = None,
) -> list[str]:
    """Tail cycles and decisions. Returns the event keys sent on this call."""
    log_dir = Path(log_dir)
    if state is None:
        state = {"sent": {}, "last_cycle": None, "offsets": {}}
    state.setdefault("sent", {})
    state.setdefault("offsets", {})
    sent: list[str] = []
    while True:
        moment = time.monotonic() if now is None else float(now)
        sent.extend(_poll(log_dir, state, moment, post))
        if once or now is not None:
            return sent
        time.sleep(POLL_SECONDS)


def _poll(log_dir: Path, state: dict, now: float, post) -> list[str]:
    cycles = _new_rows(log_dir / "cycles.jsonl", state)
    decisions = _new_rows(log_dir / "decisions.jsonl", state)
    if not cycles:
        cycles = [row for row in decisions if "equity_tmn" in row and row.get("record") != "symbol"]
        decisions = [row for row in decisions if row.get("record") == "symbol"]
    by_cycle: dict[str, list[dict]] = {}
    for row in decisions:
        if row.get("cycle_id"):
            by_cycle.setdefault(str(row["cycle_id"]), []).append(row)
    sent = []
    token, chat = _credentials()
    for row in cycles:
        enriched = _enrich(row, by_cycle.get(str(row.get("cycle_id")), []))
        moment = _instant(enriched)
        previous = state.get("last_cycle")
        if previous is not None and moment is not None and (moment - previous).total_seconds() > GAP_SECONDS:
            if _notify(state, "cycle_gap", f"cycle gap {_pnl(enriched)}", now, token, chat, post):
                sent.append("cycle_gap")
        if moment is not None:
            state["last_cycle"] = moment
        ops_state = str(enriched.get("state") or "")
        if ops_state in {"HALT", "KILLED"}:
            if _notify(state, ops_state, f"{ops_state} {_pnl(enriched)}", now, token, chat, post):
                sent.append(ops_state)
        if int(enriched.get("n_orders_rejected") or 0) > 0:
            if _notify(state, "order_reject", f"order reject {_pnl(enriched)}", now, token, chat, post):
                sent.append("order_reject")
        if int(enriched.get("n_entry_taker_fills") or 0) > 0:
            if _notify(state, "taker_entry", f"taker entry {_pnl(enriched)}", now, token, chat, post):
                sent.append("taker_entry")
    return sent


def _enrich(row: dict, symbols: list[dict]) -> dict:
    item = dict(row)
    if item.get("n_orders_rejected") is None:
        item["n_orders_rejected"] = sum(
            1 for symbol in symbols
            if str((symbol.get("order") or {}).get("status") or "").lower() == "rejected"
        )
    if item.get("n_entry_taker_fills") is None:
        item["n_entry_taker_fills"] = sum(1 for symbol in symbols if _taker_entry(symbol))
    return item


def _taker_entry(row: dict) -> bool:
    order = row.get("order") if isinstance(row.get("order"), dict) else {}
    side = str(order.get("side") or "").upper()
    flag = str(order.get("liquidity_flag") or order.get("type") or "").lower()
    return side == "BUY" and flag in {"taker", "market"}


def _notify(state, key, text, now, token, chat, post) -> bool:
    if not token or not chat:
        return False
    previous = state["sent"].get(key)
    if previous is not None and now - previous < INTERVAL_SECONDS:
        return False
    if not _deliver(token, chat, text, post):
        return False
    state["sent"][key] = now
    return True


def _pnl(row: dict) -> str:
    value = row.get("pnl_from_golive_pct")
    try:
        if value is None or value == "":
            raise TypeError
        return f"pnl {float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "pnl n/a"


def _new_rows(path: Path, state: dict) -> list[dict]:
    offsets = state["offsets"]
    offset, pending = offsets.get(str(path), (0, ""))
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read()
        offset = handle.tell()
    text = pending + blob.decode("utf-8", errors="replace")
    if text and not text.endswith("\n"):
        lines = text.split("\n")
        pending = lines[-1]
        complete = lines[:-1]
    else:
        pending = ""
        complete = text.split("\n") if text else []
    offsets[str(path)] = (offset, pending)
    rows = []
    for line in complete:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _credentials() -> tuple[str, str]:
    return (
        os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "").strip(),
        os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip(),
    )


def _deliver(token: str, chat: str, text: str, post) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat, "text": str(text)[:400], "disable_web_page_preview": True}).encode()
    sender = post or _post
    try:
        sender(url, body)
    except Exception as exc:
        log.warning("watchdog alert failed: %s", type(exc).__name__)
        return False
    return True


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def _instant(row: dict):
    from datetime import datetime, timezone
    raw = row.get("ts_utc") or row.get("ts_tehran") or row.get("timestamp")
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
