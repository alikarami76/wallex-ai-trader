"""Price and quantity rounding that matches Wallex tickSize / stepSize.

`tickSize` and `stepSize` on GET /v1/markets are decimal-place counts, not
increments. TMN prices are usually 0 decimals. Quantity is always rounded
down so an order cannot ask for more than the available balance. Passive
buys round the price down and passive sells round it up, which keeps a
resting limit from crossing the spread. A marketable buy rounds up so the
limit actually meets the ask.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP


class OrderRejected(ValueError):
    """The order violates precision or the minimum size."""


def _quantum(decimals: int) -> Decimal:
    if decimals <= 0:
        return Decimal(1)
    return Decimal(1).scaleb(-int(decimals))


def to_decimal(value: float | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize(value: float | str | Decimal, decimals: int, rounding: str) -> Decimal:
    mode = ROUND_DOWN if rounding == "down" else ROUND_UP
    return to_decimal(value).quantize(_quantum(decimals), rounding=mode)


def format_number(value: Decimal, decimals: int) -> str:
    if decimals <= 0:
        return f"{value.quantize(Decimal(1)):f}"
    return f"{value:.{int(decimals)}f}"


def round_qty(qty: float, qty_decimals: int) -> Decimal:
    rounded = quantize(qty, qty_decimals, "down")
    if rounded < 0:
        return Decimal(0)
    return rounded


def round_price(price: float, price_decimals: int, direction: str) -> Decimal:
    """direction is 'down' or 'up'."""
    if direction not in {"down", "up"}:
        raise OrderRejected(f"unknown price rounding direction: {direction}")
    return quantize(price, price_decimals, direction)


def build_order_body(
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float | None,
    client_id: str,
    price_decimals: int,
    qty_decimals: int,
    min_qty: float,
    min_notional: float,
    price_direction: str = "down",
) -> dict:
    """Return the JSON body for POST /v1/account/orders.

    MARKET orders omit `price` (error 1003 if it is sent). All numbers are
    strings. `client_id` must match `[A-Za-z0-9_]+`.
    """
    side = side.upper()
    order_type = order_type.upper()
    if side not in {"BUY", "SELL"}:
        raise OrderRejected(f"bad side {side}")
    if order_type not in {"LIMIT", "MARKET"}:
        raise OrderRejected(f"unsupported order type {order_type}")
    if not client_id or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_" for ch in client_id):
        raise OrderRejected("client_id must be letters, digits, or underscore")

    qty = round_qty(quantity, qty_decimals)
    if qty <= 0 or float(qty) < float(min_qty):
        raise OrderRejected(
            f"quantity {qty} below minQty {min_qty} after rounding to {qty_decimals} decimals"
        )

    body: dict[str, str] = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": format_number(qty, qty_decimals),
        "client_id": client_id,
    }
    if order_type == "MARKET":
        return body
    if price is None or price <= 0:
        raise OrderRejected("LIMIT orders require a positive price")
    rounded_price = round_price(price, price_decimals, price_direction)
    if rounded_price <= 0:
        raise OrderRejected("price rounded to zero")
    notional = rounded_price * qty
    if float(notional) < float(min_notional):
        raise OrderRejected(
            f"notional {notional} TMN is below the minimum {min_notional} TMN"
        )
    body["price"] = format_number(rounded_price, price_decimals)
    return body
