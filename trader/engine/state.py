"""Disk state so a restart can resume instead of opening a second set of orders."""

from __future__ import annotations

import json
from pathlib import Path

from trader.engine.portfolio import Portfolio


def load_state(path: Path, initial_cash: float) -> dict:
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["portfolio"] = Portfolio.from_dict(payload.get("portfolio") or {"cash": initial_cash})
        payload.setdefault("peak_equity", initial_cash)
        payload.setdefault("day_start_equity", initial_cash)
        payload.setdefault("day_key", "")
        payload.setdefault("kill_switch", False)
        payload.setdefault("kill_reason", "")
        payload.setdefault("cooldowns", {})
        payload.setdefault("order_times", [])
        payload.setdefault("pending", {})
        payload.setdefault("client_seq", 1)
        payload.setdefault("liquidity", {})
        payload.setdefault("ops", {})
        payload.setdefault("dust_book", {})
        return payload
    return fresh_state(initial_cash)


def fresh_state(initial_cash: float) -> dict:
    return {
        "portfolio": Portfolio(initial_cash),
        "peak_equity": initial_cash,
        "day_start_equity": initial_cash,
        "day_key": "",
        "kill_switch": False,
        "kill_reason": "",
        "cooldowns": {},
        "order_times": [],
        "pending": {},
        "client_seq": 1,
        "liquidity": {},
        "ops": {},
        "dust_book": {},
    }


def _seed_n(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _trim_liquidity(history: dict) -> dict:
    trimmed: dict = {}
    if not isinstance(history, dict):
        return trimmed
    for symbol, row in history.items():
        if not isinstance(row, dict):
            continue
        trimmed[str(symbol)] = {
            "depth": [float(item) for item in (row.get("depth") or [])][-48:],
            "spread": [float(item) for item in (row.get("spread") or [])][-48:],
            "volume": [float(item) for item in (row.get("volume") or [])][-48:],
            "volume_ts": [int(item) for item in (row.get("volume_ts") or [])][-48:],
            "seed_n": _seed_n(row.get("seed_n")),
        }
    return trimmed


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    portfolio: Portfolio = state["portfolio"]
    payload = {
        "portfolio": portfolio.to_dict(),
        "peak_equity": state["peak_equity"],
        "day_start_equity": state["day_start_equity"],
        "day_key": state["day_key"],
        "kill_switch": state["kill_switch"],
        "kill_reason": state["kill_reason"],
        "cooldowns": state["cooldowns"],
        "order_times": state["order_times"][-500:],
        "pending": state["pending"],
        "client_seq": state["client_seq"],
        "liquidity": _trim_liquidity(state.get("liquidity") or {}),
        "dust_book": state.get("dust_book") or {},
        "ops": {
            key: value
            for key, value in (state.get("ops") or {}).items()
            if key not in {"_queued", "_ops_ready"}
        },
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
