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
    llm.setdefault("model", "openai/gpt-6-luna")
    llm.setdefault("enabled", True)
    llm.setdefault("timeout_seconds", 20)
    llm.setdefault("send_temperature", False)
    llm.setdefault("temperature", 0.1)
    llm.setdefault("send_response_format", False)
    llm.setdefault("max_llm_calls_per_hour", 8)
    llm.setdefault("max_llm_calls_total", 160)
    llm.setdefault("max_spend_usd", 2.5)
    llm.setdefault("max_spend_usd_per_day", 1.25)
    llm.setdefault("downgrade_spend_usd", 2.0)
    llm.setdefault("downgrade_daily_usd", 1.0)
    llm.setdefault("min_interval_seconds", 1.0)
    llm.setdefault("max_retries", 3)
    llm.setdefault("retry_backoff_seconds", 0.5)
    llm.setdefault("routine_timeout_seconds", 8)
    llm.setdefault("assumed_input_usd_per_million", 0.10)
    llm.setdefault("assumed_output_usd_per_million", 0.50)
    llm.setdefault("price_markup", 1.15)
    llm.setdefault("assumed_usd_per_call_if_usage_missing", 0.0006325)
    llm.setdefault("competition_budget_usd", 3.0)
    llm.setdefault("high_stakes_exposure", 0.0)
    llm.setdefault("high_stakes_daily_drawdown", 0.0075)
    llm.setdefault("typical_prompt_tokens", 3000)
    llm.setdefault("typical_completion_tokens", 500)
    llm.setdefault("cache_seconds", 3600)
    llm.setdefault("sentiment_cache_seconds", 900)
    llm.setdefault("usage_path", "data/llm_usage.json")
    payload.setdefault("mcp", {"enabled": True, "url": "https://api.wallex.ir/mcp", "timeout_seconds": 8, "refresh_seconds": 600})
    payload.setdefault("account", {"initial_equity_tmn": 5_000_000})
    _apply_data_dir(payload)
    return payload


def _apply_data_dir(payload: dict) -> None:
    """Point journals and caches at TRADER_DATA_DIR when that env var is set.

    A Liara disk is mounted on that path. Restarts keep the decision log,
    positions, the LLM ledger, and the sentiment cache.
    """
    data = os.getenv("TRADER_DATA_DIR", "").strip()
    usage = os.getenv("TRADER_LLM_USAGE_PATH", "").strip()
    cache = os.getenv("TRADER_SENTIMENT_CACHE", "").strip()
    llm = payload.setdefault("llm", {})
    sentiment = payload.setdefault("sentiment", {})
    if usage:
        llm["usage_path"] = usage
    elif data:
        llm["usage_path"] = str(Path(data) / "llm_usage.json")
    if cache:
        sentiment["cache_path"] = cache
    elif data:
        sentiment["cache_path"] = str(Path(data) / "sentiment_cache.json")


def runtime_dirs(explicit_out: str | None, mode: str, dry_run: bool) -> tuple[Path, Path, Path]:
    """Return `(out_dir, state_path, log_dir)`.

    `TRADER_DATA_DIR` is the disk root. `TRADER_STATE_PATH` is the positions
    file. `TRADER_LOG_DIR` is the decision log directory. An explicit `--out`
    still wins over the data-dir default.
    """
    data = os.getenv("TRADER_DATA_DIR", "").strip()
    if explicit_out:
        out = Path(explicit_out)
    elif data:
        out = Path(data)
    else:
        name = "dry-run" if dry_run and mode == "live" else mode
        out = Path("data") / name
    state = Path(os.getenv("TRADER_STATE_PATH", "").strip() or (out / "state.json"))
    logs = Path(os.getenv("TRADER_LOG_DIR", "").strip() or out)
    return out, state, logs


def wallex_credentials() -> tuple[str, str]:
    return os.getenv("WALLEX_API_KEY", "").strip(), os.getenv("WALLEX_SUBACCOUNT_ID", "").strip()
