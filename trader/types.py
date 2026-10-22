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

    def imbalance(self, levels: int = 5) -> float | None:
        bids = self.bids[:levels]
        asks = self.asks[:levels]
        bid_notional = sum(l.price * l.quantity for l in bids)
        ask_notional = sum(l.price * l.quantity for l in asks)
        total = bid_notional + ask_notional
        if total <= 0:
            return None
        return (bid_notional - ask_notional) / total


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
