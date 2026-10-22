"""Execution cost and maker quotes.

The cost gate is the Almgren and Chriss (2000) temporary-cost split for a
small order: fees, plus half the spread, plus a slippage allowance. The quote
is the inventory term of Avellaneda and Stoikov (2008): the reservation price
moves against a long inventory, and the order stays on the maker side of the
touch. The arrival-intensity half of that paper is not used. In return units
it explodes unless it is fitted, and this book is too short to fit it.
"""

from __future__ import annotations


def half_spread(spread_bps: float | None) -> float:
    if spread_bps is None:
        return 0.0
    return max(0.0, float(spread_bps)) / 2.0 / 10_000.0


def execution_cost(cfg: dict, maker: float, taker: float, spread_bps: float | None) -> float:
    """Fees + half-spread + slippage, in return units."""
    execution = cfg.get("execution") or {}
    entry = taker if execution.get("entry") == "marketable_limit" else maker
    exit_style = execution.get("exit", "limit_maker")
    exit_fee = maker if exit_style == "limit_maker" else taker
    slippage = float((cfg.get("fees") or {}).get("slippage_bps", 0)) / 10_000.0
    return entry + exit_fee + half_spread(spread_bps) + slippage


def fractional_kelly(cfg: dict) -> float | None:
    """Quarter-Kelly (or whatever fraction is configured), capped by the caller.

    f* = p - (1-p)/b from Kelly (1956). A missing kelly_fraction means the
    fixed-fraction cap is used alone. The win probability and payoff are
    priors, not statistics from the backtest being judged.
    """
    risk = cfg.get("risk") or {}
    if risk.get("kelly_fraction") is None:
        return None
    payoff = float(risk.get("kelly_payoff", 1.0))
    if payoff <= 0:
        return 0.0
    win = min(0.99, max(0.01, float(risk.get("kelly_win_prob", 0.55))))
    full = win - (1.0 - win) / payoff
    return max(0.0, full) * float(risk["kelly_fraction"])


def inventory_skew(inventory_fraction: float, sigma: float, gamma: float, tau_hours: float) -> float:
    """Reservation shift q * gamma * sigma^2 * tau, in return units.

    sigma is the hourly ATR as a fraction of price. tau is the remaining
    hold in hours. A flat book (q = 0) does not move the quote.
    """
    if inventory_fraction <= 0 or sigma <= 0 or gamma <= 0 or tau_hours <= 0:
        return 0.0
    return inventory_fraction * gamma * (sigma ** 2) * tau_hours


def maker_buy_limit(
    best_bid: float | None,
    best_ask: float | None,
    mid: float | None,
    skew: float,
) -> float | None:
    """Join the bid, shifted down by the inventory skew. Never cross the ask."""
    anchor = best_bid if best_bid and best_bid > 0 else mid
    if anchor is None or anchor <= 0:
        return None
    price = anchor * (1.0 - max(0.0, skew))
    if best_ask is not None and best_ask > 0 and price >= best_ask:
        if best_bid is not None and best_bid < best_ask:
            return best_bid
        return None
    return price


def maker_sell_limit(
    best_bid: float | None,
    best_ask: float | None,
    mid: float | None,
    skew: float,
) -> float | None:
    """Offer closer to the mid when inventory is long, but stay above the bid."""
    ask = best_ask if best_ask and best_ask > 0 else mid
    if ask is None or ask <= 0:
        return None
    shifted = ask * (1.0 - max(0.0, skew))
    if best_bid is not None and best_bid > 0 and shifted <= best_bid:
        return ask
    return shifted
