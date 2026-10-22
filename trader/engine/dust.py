"""Dust below the 50,000 TMN minimum, at liquidation and other forced exits.

A spot sell under the minimum is rejected by the exchange. The flatten buys
enough extra base that the buy itself and the combined sell both clear the
minimum, then sells the whole amount. Both orders are spot
`POST /v1/account/orders`. OTC and instant convert are not used. When the
top-up cannot be funded and the symbol cannot take a market order, the coin
is left in inventory and reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from trader.engine.portfolio import Fill, Portfolio
from trader.wallex.precision import quantize

FORCED_FLATTEN = frozenset({"liquidation", "kill", "stop", "trail"})


@dataclass(frozen=True)
class DustPlan:
    kind: str
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    mark_tmn: float = 0.0
    reason: str = ""

    def residual(self, symbol: str, qty: float | None = None) -> dict:
        held = self.sell_qty if qty is None else qty
        return {
            "symbol": symbol,
            "qty": held,
            "mark_tmn": round(self.mark_tmn, 2),
            "reason": self.reason,
        }


def plan_dust_flatten(
    *,
    qty: float,
    buy_price: float,
    sell_price: float,
    min_notional: float,
    min_qty: float,
    qty_decimals: int,
    cash: float,
    buy_fee: float,
    market_orders_enabled: bool,
    buy_allowed: bool,
) -> DustPlan:
    """How to get this coin back to TMN.

    `sell` means the held notional already clears the minimum.
    `topup` is a spot buy of at least the minimum, then a sell of the sum.
    `market` is a spot market sell, used when a top-up cannot be funded and
    the symbol allows market orders.
    `residual` is reported and left on the book.
    """
    qty = float(qty)
    buy_price = float(buy_price or 0)
    sell_price = float(sell_price or 0)
    mark = qty * sell_price
    floor = float(min_notional or 0)
    if qty <= 0 or sell_price <= 0:
        return DustPlan("residual", sell_qty=max(qty, 0.0), mark_tmn=mark, reason="residual coin; no price to sell")
    if floor <= 0 or _clears(qty, sell_price, floor, min_qty):
        return DustPlan("sell", sell_qty=qty, mark_tmn=mark, reason="position clears the minimum order")

    fee = min(max(float(buy_fee or 0), 0.0), 0.05)
    keep = 1 - fee
    if buy_allowed and buy_price > 0 and cash + 1e-6 >= floor:
        target = floor * 1.01
        need_base = max(0.0, target / sell_price - qty)
        buy_for_sum = need_base / keep if keep > 0 else need_base
        buy_for_order = floor / buy_price
        raw = max(buy_for_sum, buy_for_order, float(min_qty or 0))
        buy_qty = float(quantize(raw, qty_decimals, "up"))
        if buy_qty < float(min_qty or 0):
            buy_qty = float(min_qty or 0)
        buy_notional = buy_qty * buy_price
        added = buy_qty * keep
        combined = (qty + added) * sell_price
        if (
            buy_qty > 0
            and buy_notional + 1e-6 >= floor
            and combined + 1e-6 >= floor
            and buy_notional <= cash + 1e-6
        ):
            return DustPlan(
                "topup",
                buy_qty=buy_qty,
                sell_qty=qty + added,
                mark_tmn=mark,
                reason="top-up buy then sell the combined amount",
            )

    if market_orders_enabled and qty + 1e-12 >= float(min_qty or 0):
        return DustPlan(
            "market",
            sell_qty=qty,
            mark_tmn=mark,
            reason="spot market sell; the symbol allows market orders and cash cannot fund a top-up",
        )

    if not buy_allowed:
        reason = "residual coin; a top-up buy is blocked and OTC convert is not used"
    elif cash + 1e-6 < floor:
        reason = "residual coin; cash cannot fund a 50,000 TMN top-up and a market sell is not available"
    else:
        reason = "residual coin; the top-up could not clear the minimum after rounding"
    return DustPlan("residual", sell_qty=qty, mark_tmn=mark, reason=reason)


def apply_paper(
    portfolio: Portfolio,
    plan: DustPlan,
    *,
    symbol: str,
    buy_price: float,
    sell_price: float,
    buy_fee: float,
    sell_fee: float,
    reason: str,
    buy_id: str,
    sell_id: str,
) -> list[Fill]:
    """Paper-fill a top-up or a market sell. A normal sell is left to the caller."""
    if plan.kind == "topup":
        bought = portfolio.top_up(
            symbol=symbol, price=buy_price, qty=plan.buy_qty, fee_rate=buy_fee,
            reason=reason, client_id=buy_id, order_type="LIMIT",
        )
        if bought is None:
            return []
        sold = portfolio.sell(
            symbol=symbol, price=sell_price, fee_rate=sell_fee, reason=reason,
            client_id=sell_id, order_type="MARKET",
        )
        return [bought] if sold is None else [bought, sold]
    if plan.kind == "market":
        sold = portfolio.sell(
            symbol=symbol, price=sell_price, fee_rate=sell_fee, reason=reason,
            client_id=sell_id, order_type="MARKET",
        )
        return [] if sold is None else [sold]
    return []


def _clears(qty: float, price: float, min_notional: float, min_qty: float) -> bool:
    return qty + 1e-12 >= float(min_qty or 0) and qty * price + 1e-6 >= float(min_notional)
