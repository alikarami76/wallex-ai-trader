"""Scripted hourly prices. The shape is checked against the real signal rules."""

from __future__ import annotations

from trader.strategy.signals import evaluate_symbol
from trader.types import Bar, BookLevel, OrderBook


def closes(kind: str, count: int = 100) -> list[float]:
    """`flat` never signals. `dip` is an uptrend with a 4-hour pullback of about 1%."""
    if kind == "flat":
        return [1_000_000.0] * count
    price = 900_000.0
    series = []
    for _ in range(count):
        price *= 1.0015
        series.append(price)
    anchor = series[-5]
    price = anchor
    for offset in range(4):
        price *= 0.9975
        series[-4 + offset] = price
    return series


def hourly_bars(kind: str, end: int, count: int = 100) -> list[Bar]:
    """Closed hourly bars ending at the hour before `end`."""
    last_open = (int(end) // 3600) * 3600 - 3600
    values = closes(kind, count)
    bars = []
    for index, close in enumerate(values):
        opened = last_open - (count - 1 - index) * 3600
        high = close * 1.002
        low = close * 0.998
        bars.append(Bar(opened, close, high, low, close, 1_000.0))
    return bars


def minute_bars(kind: str, end: int, hours: int = 36) -> list[Bar]:
    """One-minute bars. The last half hour is flat so the 15-minute return is quiet."""
    hourly = hourly_bars(kind, end, count=max(hours + 2, 8))
    by_hour = {bar.time: bar.close for bar in hourly}
    start = (int(end) // 60) * 60 - hours * 3600
    bars = []
    cursor = start
    flat_from = int(end) - 1800
    while cursor < int(end):
        hour = (cursor // 3600) * 3600
        close = by_hour.get(hour) or by_hour.get(hourly[-1].time) or 1_000_000.0
        if cursor >= flat_from:
            close = by_hour.get((flat_from // 3600) * 3600) or close
        bars.append(Bar(cursor, close, close * 1.0002, close * 0.9998, close, 10.0))
        cursor += 60
    return bars


def book(symbol: str, mid: float, *, spread_bps: float = 10.0, bid_qty: float = 80.0, ask_qty: float = 20.0) -> dict:
    half = mid * (spread_bps / 2.0) / 10_000.0
    bid = mid - half
    ask = mid + half
    return {
        "bid": [{"price": f"{bid:.8f}", "quantity": f"{bid_qty:.8f}"}],
        "ask": [{"price": f"{ask:.8f}", "quantity": f"{ask_qty:.8f}"}],
    }


def as_order_book(symbol: str, payload: dict) -> OrderBook:
    bids = [BookLevel(float(row["price"]), float(row["quantity"])) for row in payload["bid"]]
    asks = [BookLevel(float(row["price"]), float(row["quantity"])) for row in payload["ask"]]
    return OrderBook(symbol, bids, asks)


def signal_blockers(cfg: dict, kind: str = "dip") -> list[str]:
    """Run the real signal function on the scripted book. Empty means the dip is valid."""
    end = 1_800_000_000
    bars = hourly_bars(kind, end)
    mid = bars[-1].close
    payload = book("BTCTMN", mid)
    row = evaluate_symbol(
        "BTCTMN",
        bars,
        cfg,
        spread_bps=as_order_book("BTCTMN", payload).spread_bps,
        imbalance=as_order_book("BTCTMN", payload).imbalance(),
        micro_return=0.0,
        maker_fee=0.0025,
        taker_fee=0.003,
        book=as_order_book("BTCTMN", payload),
    )
    return list(row.blockers)
