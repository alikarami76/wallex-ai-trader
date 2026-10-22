"""Named strategy overlays. The active name is strategy.name in the config."""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    "uptrend_pullback": {},
    "fast_scalp": {
        "strategy": {
            "name": "fast_scalp",
            "ema_fast": 3,
            "ema_slow": 10,
            "pullback_bars": 2,
            "pullback_roc_max": -0.002,
            "pullback_roc_min": -0.012,
            "trend_bars": 6,
            "trend_roc_min": 0.004,
            "slow_bars": 0,
            "slow_roc_min": 0.0,
            "max_atr_pct": 0.025,
            "expected_gross": 0.012,
            "edge_multiple": 1.05,
            "min_micro_return": -0.006,
            "entry_size_fraction": 0.10,
            "imbalance_block": -0.65,
        },
        "risk": {
            "stop_loss_pct": 0.008,
            "take_profit_pct": 0.012,
            "time_stop_hours": 2,
            "trail_arm_pct": 0.012,
            "trail_distance_pct": 0.005,
            "cooldown_after_stop_seconds": 3600,
            "max_entries_per_hour": 3,
        },
        "execution": {
            "entry": "limit_maker",
            "exit": "limit_maker",
        },
        "ops": {
            "frozen_gross_prior": 0.012,
        },
    },
}


def apply_strategy_profile(cfg: dict, name: str | None = None) -> dict:
    """Merge a named profile into cfg. Unknown names raise."""
    chosen = (name or (cfg.get("strategy") or {}).get("name") or "uptrend_pullback").strip()
    if chosen not in PROFILES:
        raise ValueError(f"unknown strategy.name {chosen!r}; choose one of {sorted(PROFILES)}")
    overlay = PROFILES[chosen]
    for section, values in overlay.items():
        target = cfg.setdefault(section, {})
        if not isinstance(target, dict):
            continue
        target.update(values)
    cfg.setdefault("strategy", {})["name"] = chosen
    return cfg
