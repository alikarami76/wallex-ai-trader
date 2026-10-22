"""Optional Telegram alerts. Off unless both env vars are set.

The trading loop never waits on the network. A missing token or chat id is a
no-op. The bot token is not written to logs.
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
_last_sent: dict[str, float] = {}


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


def _deliver(token: str, chat: str, text: str, post) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat,
        "text": str(text)[:400],
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
