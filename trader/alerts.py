"""Optional Telegram alerts and the trade log.

Watchdog-style alerts use TELEGRAM_ALERT_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID.
Trade decisions use the same bot token and TELEGRAM_TRADES_CHAT_ID. Either
path is a no-op when its chat id is unset. The trading loop never waits on
the network. The bot token is not written to logs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("trader.alerts")

# One message per key per five minutes. A stuck HALT must not page every cycle.
MIN_INTERVAL_SECONDS = 300.0
# Skipped-entry summaries are quieter than live orders.
SKIP_INTERVAL_SECONDS = 1800.0
# Identical holds repeat at most this often. A new reason posts immediately.
HOLD_REPEAT_SECONDS = 900.0
_GATES = ("ema", "return_24h", "pullback_4h", "spread", "zscore", "ev")
_last_sent: dict[str, float] = {}
_skip_sent: float | None = None
_hold_notice = {"key": None, "cycles": 0, "last_sent": None}


def configured() -> bool:
    token, chat = _credentials()
    return bool(token and chat)


def notify(key: str, text: str, *, now: float | None = None, post=None) -> bool:
    """Queue one alert. False when unconfigured, rate-limited, or the send failed."""
    token, chat = _credentials()
    if not token or not chat:
        return False
    moment = time.monotonic() if now is None else float(now)
    previous = _last_sent.get(key)
    if previous is not None and moment - previous < MIN_INTERVAL_SECONDS:
        return False
    _last_sent[key] = moment
    if post is not None:
        return _deliver(token, chat, text, post)
    threading.Thread(
        target=_deliver, args=(token, chat, text, None), name="telegram-alert", daemon=True,
    ).start()
    return True


def report_cycle(
    *,
    gap_seconds: float | None,
    ops_state: str,
    order_rejects: int,
    entry_taker_fills: int,
    post=None,
    now: float | None = None,
) -> list[str]:
    """Alert on a stalled loop, HALT/KILLED, an exchange reject, or a taker entry."""
    sent: list[str] = []
    if gap_seconds is not None and gap_seconds > MIN_INTERVAL_SECONDS:
        if notify("cycle_gap", f"cycle gap {int(gap_seconds)}s", post=post, now=now):
            sent.append("cycle_gap")
    if ops_state in {"HALT", "KILLED"}:
        if notify(f"state:{ops_state}", f"ops state {ops_state}", post=post, now=now):
            sent.append(ops_state)
    if order_rejects:
        if notify("order_reject", f"order rejects {int(order_rejects)}", post=post, now=now):
            sent.append("order_reject")
    if entry_taker_fills:
        if notify("taker_entry", f"taker entry fills {int(entry_taker_fills)}", post=post, now=now):
            sent.append("taker_entry")
    return sent


def _credentials() -> tuple[str, str]:
    return (
        os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "").strip(),
        os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip(),
    )


def _trade_credentials() -> tuple[str, str]:
    """Same bot as alerts. The trade channel is a different chat."""
    return (
        os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "").strip(),
        os.getenv("TELEGRAM_TRADES_CHAT_ID", "").strip(),
    )


def post_text(text: str, *, post=None, limit: int = 700) -> bool:
    """Queue one decision note. No rate limit. False when the trade channel is unset."""
    token, chat = _trade_credentials()
    if not token or not chat or not str(text).strip():
        return False
    cleaned = _scrub(str(text))
    if post is not None:
        return _deliver(token, chat, cleaned, post, limit=limit)
    threading.Thread(
        target=_deliver, args=(token, chat, cleaned, None), kwargs={"limit": limit},
        name="telegram-decision", daemon=True,
    ).start()
    return True


def send_decision(
    *,
    mode: str,
    when,
    symbol: str,
    side: str,
    order_type: str,
    price,
    qty,
    reason: str,
    edge,
    hurdle,
    sl,
    tp,
    client_id: str,
    post=None,
) -> bool:
    """BUY or SELL decision. Errors are logged and swallowed."""
    try:
        text = _decision_text(
            mode=mode, when=when, symbol=symbol, side=side, order_type=order_type,
            price=price, qty=qty, reason=reason, edge=edge, hurdle=hurdle,
            sl=sl, tp=tp, client_id=client_id,
        )
        return post_text(text, post=post)
    except Exception as exc:
        log.warning("telegram decision failed: %s", type(exc).__name__)
        return False


def send_update(
    *,
    mode: str,
    when,
    symbol: str,
    side: str,
    order_type: str,
    status: str,
    price,
    qty,
    qty_total,
    pnl,
    client_id: str,
    fee=None,
    post=None,
) -> bool:
    """Fill, partial fill, or cancel. A sell includes realized PnL after fees."""
    try:
        text = _update_text(
            mode=mode, when=when, symbol=symbol, side=side, order_type=order_type,
            status=status, price=price, qty=qty, qty_total=qty_total, pnl=pnl,
            fee=fee, client_id=client_id,
        )
        return post_text(text, post=post)
    except Exception as exc:
        log.warning("telegram update failed: %s", type(exc).__name__)
        return False


def reset_hold_notice() -> None:
    _hold_notice.update(key=None, cycles=0, last_sent=None)


def classify_hold(
    *,
    features,
    candidates,
    zscore_n: int,
    zscore_min: int,
    entries_allowed: bool,
    entries_blocked: bool,
    block_reason: str,
    cutoff_reason: str,
    decision=None,
    rejected=None,
) -> tuple[str, list[str]]:
    """Why this cycle did not trade, plus the candidate symbols."""
    names = sorted({
        str(getattr(item, "symbol", "") or "")
        for item in candidates or []
        if getattr(item, "symbol", None)
    })
    try:
        have = int(zscore_n)
        need = int(zscore_min)
    except (TypeError, ValueError):
        have, need = 0, 18
    if have < need or str(cutoff_reason or "") == "zscore_warmup":
        return f"warm-up not complete ({have}/{need})", names
    hard = _hard_block(entries_allowed, entries_blocked, block_reason, cutoff_reason)
    if hard:
        return hard, names
    if not names:
        return _filter_reason(features), names
    for row in rejected or []:
        text = str((row or {}).get("reason") or "").strip()
        if text:
            return f"risk/cutoff blocking entry: {text[:300]}", names
    return _llm_hold(decision), names


def send_hold(
    *,
    mode: str,
    ops_state: str,
    when,
    reason: str,
    candidates,
    cash,
    positions,
    post=None,
    now: float | None = None,
) -> bool:
    """One HOLD note. Identical reason and candidates stay quiet for 15 minutes."""
    try:
        token, chat = _trade_credentials()
        if not token or not chat:
            return False
        names = tuple(sorted(str(item) for item in candidates or [] if item))
        key = (str(reason or "").strip(), names)
        moment = time.monotonic() if now is None else float(now)
        if _hold_notice["key"] == key:
            _hold_notice["cycles"] = int(_hold_notice["cycles"]) + 1
            last = _hold_notice["last_sent"]
            if last is not None and moment - float(last) < HOLD_REPEAT_SECONDS:
                return False
            body = format_hold(
                when=when, ops_state=ops_state, reason=reason, cash=cash,
                positions=positions, repeat_cycles=_hold_notice["cycles"],
            )
        else:
            _hold_notice["key"] = key
            _hold_notice["cycles"] = 1
            body = format_hold(
                when=when, ops_state=ops_state, reason=reason, cash=cash, positions=positions,
            )
        _hold_notice["last_sent"] = moment
        prefix = mode
        if str(ops_state or "").upper() == "SHADOW":
            prefix = "shadow"
        return post_text(_with_mode(prefix, body), post=post, limit=900)
    except Exception as exc:
        log.warning("telegram hold failed: %s", type(exc).__name__)
        return False


def format_hold(*, when, ops_state: str, reason: str, cash, positions, repeat_cycles: int | None = None) -> str:
    lines = [_clock(when), str(ops_state or "SHADOW")]
    if repeat_cycles:
        lines.append(f"still holding ({int(repeat_cycles)} cycles), same reason")
    else:
        lines.append("HOLD")
        lines.append(str(reason or "hold").strip() or "hold")
    lines.append(_book_line(cash, positions))
    return "\n".join(lines)


def send_skips(*, mode: str, when, rows: list[tuple[str, str]], post=None, now: float | None = None) -> bool:
    """One skipped-entry summary per half hour. Empty input sends nothing."""
    global _skip_sent
    try:
        if not rows:
            return False
        token, chat = _trade_credentials()
        if not token or not chat:
            return False
        moment = time.monotonic() if now is None else float(now)
        if _skip_sent is not None and moment - _skip_sent < SKIP_INTERVAL_SECONDS:
            return False
        _skip_sent = moment
        lines = [f"{_clock(when)}", "skipped entries"]
        for symbol, reason in rows[:8]:
            lines.append(f"{symbol}: {reason}"[:120])
        return post_text(_with_mode(mode, "\n".join(lines)), post=post)
    except Exception as exc:
        log.warning("telegram skip summary failed: %s", type(exc).__name__)
        return False


def _decision_text(
    *, mode, when, symbol, side, order_type, price, qty, reason, edge, hurdle, sl, tp, client_id,
) -> str:
    notional = _product(price, qty)
    signal = str(reason or "signal").strip() or "signal"
    edge_line = ""
    if _num(edge) is not None or _num(hurdle) is not None:
        edge_line = f"\nedge {_pct(edge)} vs hurdle {_pct(hurdle)}"
    return _with_mode(mode, "\n".join([
        _clock(when),
        f"{str(side).upper()} {symbol} {str(order_type).upper()}",
        f"price {_num_text(price)} qty {_qty_text(qty)} · {_tmn(notional)} TMN",
        f"signal {signal}{edge_line}",
        f"SL {_num_text(sl)} TP {_num_text(tp)}",
        f"id {client_id or '—'}",
    ]))


def _update_text(
    *, mode, when, symbol, side, order_type, status, price, qty, qty_total, pnl, client_id,
    fee=None,
) -> str:
    label = str(status or "FILL").upper()
    if label not in {"FILL", "PARTIAL", "CANCEL"}:
        label = "FILL"
    lines = [
        _clock(when),
        f"{label} {str(side).upper()} {symbol} {str(order_type).upper()}",
        f"qty {_qty_text(qty)} of {_qty_text(qty_total)} @ {_num_text(price)}",
    ]
    if label != "CANCEL" and _num(fee) is not None:
        lines.append(f"fee {_tmn(fee)} TMN")
    if str(side).upper() == "SELL" and label != "CANCEL":
        lines.append(f"pnl {_pnl(pnl)} TMN after fees")
    lines.append(f"id {client_id or '—'}")
    return _with_mode(mode, "\n".join(lines))


def _hard_block(entries_allowed: bool, entries_blocked: bool, block_reason: str, cutoff_reason: str) -> str:
    if entries_allowed and not entries_blocked:
        return ""
    text = str(block_reason or "").strip()
    if text.lower() == "shadow":
        text = ""
    labels = {
        "entry_cutoff": "entry cutoff",
        "flatten": "flatten",
        "halt": "halt",
        "killed": "killed",
        "size_zero": "size cut to zero",
    }
    if text in labels:
        text = labels[text]
    code = str(cutoff_reason or "").strip()
    if code in {"shadow", "zscore_warmup"}:
        code = ""
    if code in labels and not text:
        text = labels[code]
    elif code and not text:
        text = code
    if not text:
        return ""
    return f"risk/cutoff blocking entry: {text[:300]}"


def _filter_reason(features) -> str:
    counts = {name: 0 for name in _GATES}
    seen_symbols = 0
    for row in features or []:
        seen_symbols += 1
        blockers = getattr(row, "blockers", None)
        if blockers is None and isinstance(row, dict):
            blockers = row.get("blockers") or []
        hit = set()
        for blocker in blockers or []:
            gate = _gate_for(str(blocker))
            if gate:
                hit.add(gate)
        for gate in hit:
            counts[gate] += 1
    main = ""
    best = 0
    for name in _GATES:
        if counts[name] > best:
            best = counts[name]
            main = name
    detail = " · ".join(f"{name} {counts[name]}" for name in _GATES)
    if best <= 0:
        return f"no candidates passed the filters ({seen_symbols} symbols)"
    others = [counts[name] for name in _GATES if name != "ev"]
    if main == "ev" and counts["ev"] > max(others or [0]):
        return f"EV below the fee hurdle ({seen_symbols} symbols, ev {counts['ev']})\n{detail}"
    return (
        f"no candidates passed the filters ({seen_symbols} symbols, main filter {main} {counts[main]})\n"
        f"{detail}"
    )


def _gate_for(blocker: str) -> str | None:
    text = blocker.lower()
    if "trend structure" in text:
        return "ema"
    if "24h return" in text:
        return "return_24h"
    if "4h pullback" in text or "4h drop" in text:
        return "pullback_4h"
    if "does not clear cost" in text or "fee hurdle" in text:
        return "ev"
    if any(phrase in text for phrase in ("depth thinned", "spread widened", "volume dried", "z-score", "zscore")):
        return "zscore"
    if "spread" in text:
        return "spread"
    return None


def _llm_hold(decision) -> str:
    lines = []
    for item in getattr(decision, "decisions", []) or []:
        action = str(getattr(item, "action", "") or "HOLD").upper()
        if action != "HOLD":
            continue
        text = str(getattr(item, "rationale", "") or "").strip()
        if text:
            lines.append(text)
    if not lines:
        lines.append(str(getattr(decision, "comment", "") or "").strip())
    blob = " ".join(line for line in lines if line).strip() or "LLM hold"
    return blob[:300].rstrip()


def _book_line(cash, positions) -> str:
    held = []
    for item in positions or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            symbol, qty = item[0], item[1]
        else:
            symbol = getattr(item, "symbol", "")
            qty = getattr(item, "qty", None)
        if not symbol:
            continue
        held.append(f"{symbol} {_qty_text(qty)}")
    book = "flat" if not held else ", ".join(held)
    return f"cash {_tmn(cash)} TMN · {book}"


def _with_mode(mode: str, text: str) -> str:
    if str(mode or "").lower() == "shadow":
        return "[SHADOW] " + text
    return text


def _clock(when) -> str:
    from trader.timeutil import to_tehran

    if when is None:
        return "—"
    local = to_tehran(when)
    return local.strftime("%Y-%m-%d %H:%M +0330")


def _scrub(text: str) -> str:
    for name in (
        "TELEGRAM_ALERT_BOT_TOKEN",
        "TELEGRAM_ALERT_CHAT_ID",
        "TELEGRAM_TRADES_CHAT_ID",
        "WALLEX_API_KEY",
        "WALLEX_SUBACCOUNT_ID",
        "LLM_API_KEY",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_USERS",
    ):
        secret = os.getenv(name, "")
        if len(secret) >= 4 and secret in text:
            text = text.replace(secret, "")
    return text


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _product(price, qty):
    left, right = _num(price), _num(qty)
    if left is None or right is None:
        return None
    return left * right


def _num_text(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    if abs(number) >= 1000:
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    text = f"{number:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _qty_text(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:.8f}".rstrip("0").rstrip(".") or "0"


def _tmn(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:,.2f}"


def _pct(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number * 100:.3f}%"


def _pnl(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:+,.2f}"


def _deliver(token: str, chat: str, text: str, post, limit: int = 400) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat,
        "text": str(text)[:limit],
        "disable_web_page_preview": True,
    }).encode()
    sender = post or _post
    try:
        sender(url, body)
    except Exception as exc:
        # The exception text can contain the request URL, which holds the token.
        log.warning("telegram alert failed: %s", type(exc).__name__)
        return False
    return True


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
