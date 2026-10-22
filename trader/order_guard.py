"""Independent pre-send check. It can only refuse an order.

The function is pure: it does not place an order, write a log, or touch the
state machine. The live loop calls it after the price and quantity are known
and before any order request. A refusal leaves the book where it was.
"""

from __future__ import annotations

from dataclasses import dataclass


# Order placement. Reads and cancels are not order requests.
TRADING_ENDPOINTS = frozenset({"/v1/account/orders"})


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    order_type: str
    qty: float
    price: float | None
    mark: float
    endpoint: str
    post_only: bool
    ops_state: str
    held_qty: float
    symbol_notional_tmn: float
    gross_notional_tmn: float
    equity_tmn: float
    quote: str
    base: str
    allowlist: tuple[str, ...]
    deny_bases: tuple[str, ...]
    unavailable: tuple[str, ...]
    size_cap: float
    gross_cap: float
    min_notional_tmn: float = 50_000


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reason: str


def assess(intent: OrderIntent) -> GuardVerdict:
    """Return ok only when every pre-send condition holds."""
    failures: list[str] = []
    symbol = (intent.symbol or "").upper()
    side = (intent.side or "").upper()
    order_type = (intent.order_type or "").upper()
    endpoint = _endpoint(intent.endpoint)
    quote = (intent.quote or "").upper()
    base = (intent.base or "").upper()
    allow = {item.upper() for item in intent.allowlist}
    denied = {item.upper() for item in intent.deny_bases}
    banned = {item.upper() for item in intent.unavailable}

    if quote != "TMN" or not symbol.endswith("TMN"):
        failures.append("not a spot TMN market")
    if endpoint not in TRADING_ENDPOINTS:
        failures.append("endpoint is not on the trading allowlist")
    if not allow or symbol not in allow or base in denied or symbol in banned:
        failures.append("symbol is outside the package allowlist or on a denylist")
    if side == "BUY":
        if intent.ops_state != "LIVE_ARMED":
            failures.append("BUY is only allowed in LIVE_ARMED")
        if order_type != "LIMIT" or not intent.post_only:
            failures.append("entries must be a post-only maker LIMIT")
    elif side == "SELL":
        if float(intent.qty) > float(intent.held_qty) + 1e-9:
            failures.append("SELL qty is above the held qty")
    else:
        failures.append("side is not BUY or SELL")

    notional = _notional(intent)
    if notional + 1e-6 < float(intent.min_notional_tmn):
        failures.append("notional is below 50000 TMN")

    equity = float(intent.equity_tmn)
    if equity <= 0:
        failures.append("equity is not positive")
    else:
        symbol_after, gross_after = _exposure_after(intent, notional)
        if symbol_after > float(intent.size_cap) + 1e-9:
            failures.append("per-symbol exposure would exceed the size cap")
        if gross_after > float(intent.gross_cap) + 1e-9:
            failures.append("gross exposure would exceed the gross cap")

    if failures:
        return GuardVerdict(False, "; ".join(failures))
    return GuardVerdict(True, "")


def _endpoint(path: str) -> str:
    clean = (path or "").split("?", 1)[0].rstrip("/")
    return clean or "/"


def _notional(intent: OrderIntent) -> float:
    qty = float(intent.qty or 0)
    if qty <= 0:
        return 0.0
    if (intent.order_type or "").upper() == "MARKET" or not intent.price:
        return qty * float(intent.mark or 0)
    return qty * float(intent.price)


def _exposure_after(intent: OrderIntent, notional: float) -> tuple[float, float]:
    equity = float(intent.equity_tmn)
    symbol_now = max(0.0, float(intent.symbol_notional_tmn))
    gross_now = max(0.0, float(intent.gross_notional_tmn))
    if (intent.side or "").upper() == "SELL":
        held = float(intent.held_qty)
        if held <= 0 or float(intent.qty) + 1e-9 >= held:
            released = symbol_now
        else:
            released = symbol_now * min(1.0, float(intent.qty) / held)
        symbol_after = max(0.0, symbol_now - released)
        gross_after = max(0.0, gross_now - released)
    else:
        symbol_after = symbol_now + max(0.0, notional)
        gross_after = gross_now + max(0.0, notional)
    return symbol_after / equity, gross_after / equity
