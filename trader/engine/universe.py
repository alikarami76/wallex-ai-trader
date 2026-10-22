"""Tradable universe: config candidates, minus the credit-plan ban list, minus wide books."""

from __future__ import annotations

from trader.types import MarketRule


def select_universe(
    cfg: dict,
    rules: dict[str, MarketRule],
    unavailable: set[str],
    live_spreads: dict[str, float] | None = None,
) -> list[str]:
    live_spreads = live_spreads or {}
    snapshot = cfg["universe"].get("spread_bps_snapshot") or {}
    chosen: list[str] = []
    rejected: list[tuple[str, str]] = []
    for symbol in cfg["universe"]["candidates"]:
        symbol = symbol.upper()
        if symbol in unavailable:
            rejected.append((symbol, "banned by credit plan"))
            continue
        rule = rules.get(symbol)
        if rule is None or rule.quote_asset != "TMN":
            rejected.append((symbol, "not a listed TMN market"))
            continue
        if rule.quote_volume_24h < float(cfg["universe"]["min_quote_volume_tmn"]):
            rejected.append((symbol, "24h quote volume below the floor"))
            continue
        spread = live_spreads.get(symbol)
        if spread is None:
            spread = snapshot.get(symbol, rule.spread_bps)
        if spread is None:
            rejected.append((symbol, "spread unknown"))
            continue
        if float(spread) > float(cfg["universe"]["max_spread_bps"]):
            rejected.append((symbol, f"spread {float(spread):.1f} bps"))
            continue
        chosen.append(symbol)
        if len(chosen) >= int(cfg["universe"]["max_symbols"]):
            break
    return chosen


def explain_universe(
    cfg: dict,
    rules: dict[str, MarketRule],
    unavailable: set[str],
    live_spreads: dict[str, float] | None = None,
) -> list[dict]:
    """One row per candidate, including the ones that were refused."""
    live_spreads = live_spreads or {}
    snapshot = cfg["universe"].get("spread_bps_snapshot") or {}
    rows = []
    for symbol in cfg["universe"]["candidates"]:
        symbol = symbol.upper()
        rule = rules.get(symbol)
        spread = live_spreads.get(symbol)
        if spread is None and rule is not None:
            spread = snapshot.get(symbol, rule.spread_bps)
        reason = "selected"
        if symbol in unavailable:
            reason = "banned"
        elif rule is None or rule.quote_asset != "TMN":
            reason = "not_listed"
        elif rule.quote_volume_24h < float(cfg["universe"]["min_quote_volume_tmn"]):
            reason = "thin_volume"
        elif spread is None:
            reason = "spread_unknown"
        elif float(spread) > float(cfg["universe"]["max_spread_bps"]):
            reason = "spread_wide"
        rows.append({
            "symbol": symbol,
            "reason": reason,
            "spread_bps": spread,
            "quote_volume_24h": None if rule is None else rule.quote_volume_24h,
        })
    return rows
