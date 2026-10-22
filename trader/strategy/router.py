"""Pick a BoxAPI model and reasoning effort for one job.

tier0 / routine: headline sentiment and other flat-cycle checks (gpt-6-luna, low).
tier1 / signal: review an open position when no new candidate exists (gpt-6-luna, medium).
tier2 / trade: open, close, or size a name, and only after the quant cost gate
has already produced a candidate (gpt-6-sol, high).

Near the USD cap the choice steps down one tier, and inside the last $0.20 it
uses the cheapest tier. The caller still refuses the call once a hard cap is hit.
"""

from __future__ import annotations

import os

ORDER = ("routine", "signal", "trade")
ALIASES = {
    "tier0": "routine",
    "tier1": "signal",
    "tier2": "trade",
    "0": "routine",
    "1": "signal",
    "2": "trade",
}

DEFAULT_TIERS = {
    "routine": {"model": "gpt-6-luna", "reasoning_effort": "low"},
    "signal": {"model": "gpt-6-luna", "reasoning_effort": "medium"},
    "trade": {"model": "gpt-6-sol", "reasoning_effort": "high"},
}


def tier_table(settings: dict) -> dict[str, dict]:
    table = {name: dict(spec) for name, spec in DEFAULT_TIERS.items()}
    raw = settings.get("tiers") or {}
    if not isinstance(raw, dict):
        return table
    for key, spec in raw.items():
        name = ALIASES.get(str(key), str(key))
        if name not in table or not isinstance(spec, dict):
            continue
        if spec.get("model"):
            table[name]["model"] = str(spec["model"])
        if spec.get("reasoning_effort"):
            table[name]["reasoning_effort"] = str(spec["reasoning_effort"])
    return table


def choose_tier(task: str, settings: dict, spent_total: float, spent_today: float) -> dict:
    """Return the tier to call. `downgraded` is true when the budget stepped it down."""
    requested = task if task in ORDER else "routine"
    index = ORDER.index(requested)
    total_cap = float(settings.get("max_spend_usd", 2.5))
    daily_cap = float(settings.get("max_spend_usd_per_day", 1.25))
    near = (
        spent_total >= float(settings.get("downgrade_spend_usd", 2.0))
        or spent_today >= float(settings.get("downgrade_daily_usd", 1.0))
    )
    hard = spent_total >= total_cap - 0.20 or spent_today >= daily_cap - 0.20
    if hard:
        index = 0
    elif near:
        index = max(0, index - 1)
    name = ORDER[index]
    spec = dict(tier_table(settings)[name])
    pinned = (os.getenv("LLM_MODEL") or "").strip()
    if pinned:
        spec["model"] = pinned
    spec["tier"] = name
    spec["requested_tier"] = requested
    spec["downgraded"] = name != requested
    return spec
