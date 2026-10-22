"""YAML config with environment secrets kept out of the file."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def load_config(path: str | Path) -> dict:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"config {path} must be a mapping")
    for key in ("fees", "universe", "strategy", "execution", "risk", "liquidation", "runtime"):
        if key not in payload:
            raise ValueError(f"config is missing '{key}'")
    payload.setdefault("exchange", {})
    payload["exchange"].setdefault("base_url", "https://api.wallex.ir")
    payload["exchange"].setdefault("package_id", 183)
    payload["exchange"].setdefault("timeout_seconds", 20)
    payload["exchange"].setdefault("max_retries", 4)
    llm = payload.setdefault("llm", {})
    llm.setdefault("enabled", True)
    llm.setdefault("timeout_seconds", 20)
    llm.setdefault("send_temperature", False)
    llm.setdefault("temperature", 0.1)
    llm.setdefault("send_response_format", False)
    llm.setdefault("max_llm_calls_per_hour", 2)
    llm.setdefault("max_llm_calls_total", 24)
    llm.setdefault("max_spend_usd", 2.5)
    llm.setdefault("assumed_input_usd_per_million", 0.50)
    llm.setdefault("assumed_output_usd_per_million", 2.00)
    llm.setdefault("assumed_usd_per_call_if_usage_missing", 0.08)
    llm.setdefault("cache_seconds", 3600)
    llm.setdefault("usage_path", "data/llm_usage.json")
    payload.setdefault("mcp", {"enabled": True, "url": "https://api.wallex.ir/mcp", "timeout_seconds": 8, "refresh_seconds": 600})
    payload.setdefault("account", {"initial_equity_tmn": 5_000_000})
    return payload


def wallex_credentials() -> tuple[str, str]:
    return os.getenv("WALLEX_API_KEY", "").strip(), os.getenv("WALLEX_SUBACCOUNT_ID", "").strip()
