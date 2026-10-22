"""TMN cash plus long spot inventory. Fees follow Wallex: buy fee in base, sell fee in TMN."""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_time: int
    cost_tmn: float
    stop_price: float
    peak_price: float
    setup: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "Position":
        return cls(**payload)


@dataclass
class Fill:
    symbol: str
    side: str
    price: float
    qty: float
    fee_tmn: float
    fee_rate: float
    notional_tmn: float
    pnl_tmn: float | None
    reason: str
    order_type: str
    client_id: str


class Portfolio:
    def __init__(self, cash: float):
        self.cash = float(cash)
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.closed_trades = 0

    def equity(self, marks: dict[str, float]) -> float:
        total = self.cash
        for symbol, position in self.positions.items():
            total += position.qty * marks.get(symbol, position.entry_price)
        return total

    def exposure(self, marks: dict[str, float]) -> float:
        equity = self.equity(marks)
        if equity <= 0:
            return 1.0
        gross = sum(position.qty * marks.get(symbol, position.entry_price) for symbol, position in self.positions.items())
        return gross / equity

    def buy(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        fee_rate: float,
        time: int,
        stop_price: float,
        setup: str,
        reason: str,
        client_id: str,
        order_type: str,
    ) -> Fill | None:
        if symbol in self.positions or qty <= 0 or price <= 0:
            return None
        notional = price * qty
        if notional > self.cash:
            qty = self.cash / price
            notional = price * qty
        if qty <= 0 or notional <= 0:
            return None
        base_qty = qty * (1 - fee_rate)
        fee_tmn = notional * fee_rate
        self.cash -= notional
        self.fees_paid += fee_tmn
        self.positions[symbol] = Position(
            symbol=symbol,
            qty=base_qty,
            entry_price=price,
            entry_time=time,
            cost_tmn=notional,
            stop_price=stop_price,
            peak_price=price,
            setup=setup,
            reason=reason,
        )
        return Fill(
            symbol=symbol,
            side="BUY",
            price=price,
            qty=base_qty,
            fee_tmn=fee_tmn,
            fee_rate=fee_rate,
            notional_tmn=notional,
            pnl_tmn=None,
            reason=reason,
            order_type=order_type,
            client_id=client_id,
        )

    def top_up(
        self,
        *,
        symbol: str,
        price: float,
        qty: float,
        fee_rate: float,
        reason: str,
        client_id: str,
        order_type: str,
    ) -> Fill | None:
        """Add base to a held coin. Used only to lift dust over the minimum before a sell."""
        if qty <= 0 or price <= 0:
            return None
        notional = price * qty
        if notional > self.cash + 1e-6:
            return None
        base_qty = qty * (1 - fee_rate)
        fee_tmn = notional * fee_rate
        self.cash -= notional
        self.fees_paid += fee_tmn
        position = self.positions.get(symbol)
        if position is None:
            self.positions[symbol] = Position(
                symbol=symbol, qty=base_qty, entry_price=price, entry_time=0,
                cost_tmn=notional, stop_price=price, peak_price=price,
                setup="dust_topup", reason=reason,
            )
        else:
            position.qty += base_qty
            position.cost_tmn += notional
        return Fill(
            symbol=symbol, side="BUY", price=price, qty=base_qty, fee_tmn=fee_tmn,
            fee_rate=fee_rate, notional_tmn=notional, pnl_tmn=None, reason=reason,
            order_type=order_type, client_id=client_id,
        )

    def sell(
        self,
        *,
        symbol: str,
        price: float,
        fee_rate: float,
        reason: str,
        client_id: str,
        order_type: str,
        qty: float | None = None,
    ) -> Fill | None:
        position = self.positions.get(symbol)
        if position is None or price <= 0 or position.qty <= 0:
            return None
        sell_qty = position.qty if qty is None else min(qty, position.qty)
        if sell_qty <= 0:
            return None
        gross = price * sell_qty
        fee_tmn = gross * fee_rate
        proceeds = gross - fee_tmn
        cost = position.cost_tmn * (sell_qty / position.qty)
        pnl = proceeds - cost
        self.cash += proceeds
        self.fees_paid += fee_tmn
        self.realized_pnl += pnl
        position.qty -= sell_qty
        position.cost_tmn -= cost
        if position.qty <= 1e-10:
            del self.positions[symbol]
            self.closed_trades += 1
        return Fill(
            symbol=symbol,
            side="SELL",
            price=price,
            qty=sell_qty,
            fee_tmn=fee_tmn,
            fee_rate=fee_rate,
            notional_tmn=gross,
            pnl_tmn=pnl,
            reason=reason,
            order_type=order_type,
            client_id=client_id,
        )

    def mark_peaks(self, highs: dict[str, float]) -> None:
        for symbol, position in self.positions.items():
            price = highs.get(symbol)
            if price is not None and price > position.peak_price:
                position.peak_price = price

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "positions": {symbol: position.to_dict() for symbol, position in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Portfolio":
        portfolio = cls(float(payload.get("cash") or 0))
        portfolio.realized_pnl = float(payload.get("realized_pnl") or 0)
        portfolio.fees_paid = float(payload.get("fees_paid") or 0)
        for symbol, row in (payload.get("positions") or {}).items():
            portfolio.positions[symbol] = Position.from_dict(row)
        return portfolio
