"""Tradable universe.

The pool is the config allowlist intersected with config candidates, then
with the live credit-plan list (everything not in unavailableMarkets). A
hard-denied base is refused in every quote, even when the package lists it
as allowed. Wide books and thin volume still drop a name after that.
"""

from __future__ import annotations

from trader.types import MarketRule

# Named in the challenge announcement. Any quote: TONTMN, TONUSDT, and so on.
HARD_DENY_BASES = ("TON", "NEAR", "FET", "INJ", "TAO", "WIF", "BONK", "PENDLE", "RENDER")
_QUOTES = ("TMN", "USDT", "IRT")


def hard_deny_bases(cfg: dict) -> set[str]:
    """Announcement bases, from `denylist` and `hard_deny_bases`.

    Either key adds names. When both are missing, the announcement list applies.
    The denylist sits on top of the allowlist and the live unavailable-markets list.
    """
    universe = cfg.get("universe") or {}
    names: set[str] = set()
    seen = False
    for key in ("denylist", "hard_deny_bases"):
        raw = universe.get(key)
        if raw is None:
            continue
        seen = True
        names.update(str(item).upper() for item in raw)
    if not seen:
        return {item.upper() for item in HARD_DENY_BASES}
    return names


def allowlist_symbols(cfg: dict) -> list[str]:
    raw = (cfg.get("universe") or {}).get("allowlist")
    if not raw:
        return []
    return [str(item).upper() for item in raw]


def base_asset(symbol: str, rule: MarketRule | None = None) -> str:
    if rule is not None and rule.base_asset:
        return str(rule.base_asset).upper()
    text = symbol.upper()
    for quote in _QUOTES:
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)]
    return text


def is_hard_denied(symbol: str, cfg: dict, rule: MarketRule | None = None) -> bool:
    return base_asset(symbol, rule) in hard_deny_bases(cfg)


def symbol_block(symbol: str, cfg: dict, unavailable: set[str], rule: MarketRule | None = None) -> str | None:
    """Why this symbol cannot be bought. None means the allowlist and the ban list both pass."""
    symbol = symbol.upper()
    if is_hard_denied(symbol, cfg, rule):
        return f"{base_asset(symbol, rule)} is hard-denied by the challenge rules"
    allow = allowlist_symbols(cfg)
    if allow and symbol not in allow:
        return "symbol is not on the spot allowlist"
    if symbol in unavailable:
        return "symbol is on the credit-plan banned list"
    return None


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
        rule = rules.get(symbol)
        blocked = symbol_block(symbol, cfg, unavailable, rule)
        if blocked:
            rejected.append((symbol, blocked))
            continue
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
        blocked = symbol_block(symbol, cfg, unavailable, rule)
        if blocked:
            if is_hard_denied(symbol, cfg, rule):
                reason = "hard_denied"
            elif symbol not in (allowlist_symbols(cfg) or [symbol]):
                reason = "not_allowlisted"
            else:
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
