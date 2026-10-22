"""Pick a BoxAPI model and reasoning effort for one job.

BoxAPI is OpenRouter-like. Prefixed ids (`openai/gpt-6-luna`) are the ones
we send. The bare ids also answer, and `canonical_model` rewrites the ones
verified on 2026-10-22.

tier0 / routine: headline sentiment (openai/gpt-6-luna, low).
tier1 / signal: sell, reduce, or cancel, with no new buy (openai/gpt-6-luna, medium).
tier2 / trade: a buy on a flat book. This is the default decision (openai/gpt-6-luna, medium).
tier3 / stakes: a buy while the book is already exposed, or the Tehran day is
already down at least 0.75%. Rare. openai/gpt-6-sol, high.

Near the USD cap the choice steps down one tier, and inside the last $0.20 it
uses the cheapest tier. The caller still refuses the call once a hard cap is hit.
"""

from __future__ import annotations

import os

ORDER = ("routine", "signal", "trade", "stakes")
ALIASES = {
    "tier0": "routine",
    "tier1": "signal",
    "tier2": "trade",
    "tier3": "stakes",
    "0": "routine",
    "1": "signal",
    "2": "trade",
    "3": "stakes",
    "high": "stakes",
}

# List price, USD per 1M tokens, (input, output). Verified 2026-10-22.
# Real bills land about 15% above this list. Reasoning tokens bill as output.
LIST_PRICES = {
    "openai/gpt-6-luna": (0.10, 0.50),
    "gpt-6-luna": (0.10, 0.50),
    "openai/gpt-6-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "openai/gpt-6.1-sol": (2.0, 10.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "openai/gpt-6-astra": (10.0, 50.0),
    "gpt-6-astra": (10.0, 50.0),
    "openai/gpt-5-nano": (0.05, 0.40),
    "gpt-5-nano": (0.05, 0.40),
}

PREFIXED = {
    "gpt-6-luna": "openai/gpt-6-luna",
    "gpt-6-sol": "openai/gpt-6-sol",
    "gpt-6.1-sol": "openai/gpt-6.1-sol",
    "gpt-6-astra": "openai/gpt-6-astra",
    "gpt-5-nano": "openai/gpt-5-nano",
}

DEFAULT_TIERS = {
    "routine": {"model": "openai/gpt-6-luna", "reasoning_effort": "low"},
    "signal": {"model": "openai/gpt-6-luna", "reasoning_effort": "medium"},
    "trade": {"model": "openai/gpt-6-luna", "reasoning_effort": "medium"},
    "stakes": {"model": "openai/gpt-6-sol", "reasoning_effort": "high"},
}

TYPICAL_PROMPT_TOKENS = 3000
TYPICAL_COMPLETION_TOKENS = 500


def canonical_model(name: str) -> str:
    """Prefer the prefixed id for models verified on BoxAPI. Other names pass through."""
    text = (name or "").strip()
    if not text:
        return "openai/gpt-6-luna"
    return PREFIXED.get(text, text)


def list_prices(model: str, settings: dict | None = None) -> tuple[float, float]:
    """USD per 1M tokens. Known models keep their list price.

    gpt-6-sol and gpt-6.1-sol are $2 / $10. gpt-6-luna is $0.10 / $0.50.
    The luna assumed rate is only for a model that is not in the table.
    """
    settings = settings or {}
    key = (model or "").strip()
    bare = key.split("/")[-1].lower()
    table = {name.lower(): pair for name, pair in LIST_PRICES.items()}
    if key.lower() in table:
        return table[key.lower()]
    if bare in table:
        return table[bare]
    if "gpt-6.1-sol" in bare or "gpt-6-sol" in bare:
        return (2.0, 10.0)
    if "gpt-6-luna" in bare:
        return (0.10, 0.50)
    return (
        float(settings.get("assumed_input_usd_per_million", 0.10)),
        float(settings.get("assumed_output_usd_per_million", 0.50)),
    )


def quoted_call_usd(model: str, settings: dict | None = None) -> float:
    """List price for ~3k input and ~500 output, times the measured 15% markup."""
    settings = settings or {}
    incoming, outgoing = list_prices(model, settings)
    markup = float(settings.get("price_markup", 1.15))
    prompt = float(settings.get("typical_prompt_tokens", TYPICAL_PROMPT_TOKENS))
    completion = float(settings.get("typical_completion_tokens", TYPICAL_COMPLETION_TOKENS))
    return (prompt / 1_000_000 * incoming + completion / 1_000_000 * outgoing) * markup


def high_stakes(risk_state: dict | None, settings: dict | None = None) -> bool:
    """A new buy is high-stakes only when the book is already carrying risk.

    That is a positive gross exposure (a second position) or a Tehran-day
    drawdown already at half the daily kill. A first entry on a flat book stays
    on the cheap decision model.
    """
    settings = settings or {}
    state = risk_state or {}
    try:
        exposure = float(state.get("gross_exposure") or 0)
    except (TypeError, ValueError):
        exposure = 0.0
    try:
        daily = float(state.get("daily_drawdown") or 0)
    except (TypeError, ValueError):
        daily = 0.0
    floor = float(settings.get("high_stakes_exposure", 0.0))
    drawdown = float(settings.get("high_stakes_daily_drawdown", 0.0075))
    return exposure > floor or daily >= drawdown


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


def _tier_env_model(tier: str) -> str:
    """Per-tier override. `LLM_MODEL` is not read, so it cannot pin every tier."""
    if tier in {"routine", "signal", "trade"}:
        return (os.getenv("LLM_MODEL_CHEAP") or "").strip()
    if tier == "stakes":
        return (os.getenv("LLM_MODEL_STRONG") or "").strip()
    return ""


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
    # LLM_MODEL used to pin every tier, which forced the sol stakes tier onto luna.
    # Cheap tiers read LLM_MODEL_CHEAP. The stakes tier reads LLM_MODEL_STRONG.
    # Unset, the config router stays as it is.
    override = _tier_env_model(name)
    spec["model"] = canonical_model(override or spec.get("model") or "")
    spec["tier"] = name
    spec["requested_tier"] = requested
    spec["downgraded"] = name != requested
    return spec
