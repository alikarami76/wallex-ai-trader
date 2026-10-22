"""Shared data types. Kept free of I/O so strategies and tests can import them."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketRule:
    symbol: str
    base_asset: str
    quote_asset: str
    price_decimals: int
    qty_decimals: int
    min_qty: float
    min_notional: float
    is_zero_fee: bool = False
    market_orders_enabled: bool = True
    quote_volume_24h: float = 0.0
    change_24h: float = 0.0
    last_price: float = 0.0
    bid_price: float | None = None
    ask_price: float | None = None

    @property
    def spread_bps(self) -> float | None:
        if not self.bid_price or not self.ask_price or self.bid_price <= 0:
            return None
        mid = (self.bid_price + self.ask_price) / 2
        if mid <= 0:
            return None
        return (self.ask_price - self.bid_price) / mid * 10_000


@dataclass
class BookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread_bps(self) -> float | None:
        if not self.best_bid or not self.best_ask or self.best_bid <= 0:
            return None
        mid = (self.best_bid + self.best_ask) / 2
        if mid <= 0:
            return None
        return (self.best_ask - self.best_bid) / mid * 10_000

    def imbalance(self, levels: int = 1) -> float | None:
        """Cont, Kukanov and Stoikov (2014) book imbalance at the touch.

        (bid quantity - ask quantity) / (bid quantity + ask quantity) on the
        best `levels` prices. The default is the best price only.
        """
        bids = self.bids[:levels]
        asks = self.asks[:levels]
        bid_qty = sum(level.quantity for level in bids)
        ask_qty = sum(level.quantity for level in asks)
        total = bid_qty + ask_qty
        if total <= 0:
            return None
        return (bid_qty - ask_qty) / total

    def depth_notional(self, band: float) -> tuple[float, float] | None:
        """Quote notional on each side within `band` (a fraction) of mid.

        Bids are below mid and asks above it. Levels outside the band are
        ignored. The book is sorted, so the scan stops at the first price
        past the band.
        """
        mid = self.mid
        if mid is None or mid <= 0 or band <= 0:
            return None
        lo = mid * (1 - band)
        hi = mid * (1 + band)
        bid = 0.0
        ask = 0.0
        for level in self.bids:
            if level.price < lo:
                break
            bid += level.price * level.quantity
        for level in self.asks:
            if level.price > hi:
                break
            ask += level.price * level.quantity
        return bid, ask


@dataclass
class Balance:
    asset: str
    total: float
    locked: float

    @property
    def available(self) -> float:
        return max(0.0, self.total - self.locked)


@dataclass
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: float | None
    orig_qty: float
    executed_qty: float
    executed_sum: float
    executed_price: float | None
    status: str
    active: bool
    fee: float = 0.0
    created_at: float | None = None
    liquidity: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PreparedOrder:
    method: str
    url: str
    headers: dict
    body: dict

    def redacted(self) -> dict:
        headers = dict(self.headers)
        if "X-API-Key" in headers and headers["X-API-Key"]:
            headers["X-API-Key"] = "***"
        return {
            "method": self.method,
            "url": self.url,
            "headers": headers,
            "body": self.body,
        }
