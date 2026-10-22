"""Drive the real runtime against the mock, on a virtual clock."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from trader.config import load_config
from trader.e2e.clock import install, restore
from trader.engine.runtime import run_runtime


@contextmanager
def scoped_env(updates: dict[str, str | None]):
    saved = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def cycles_of(directory: Path) -> list[dict]:
    return [row for row in read_jsonl(directory / "cycles.jsonl") if row.get("record", "cycle") == "cycle"]


def symbols_of(directory: Path) -> list[dict]:
    return [row for row in read_jsonl(directory / "decisions.jsonl") if row.get("record") == "symbol"]


def install_clock(clock, world) -> list:
    saved = install(clock)

    def sleeping(seconds: float) -> None:
        world.note_sleep(seconds)
        clock.advance(seconds)

    import trader.wallex.client as client

    saved.append((client.time, "sleep", client.time.sleep))
    client.time.sleep = sleeping
    # runtime.time and client.time are the same module. One assignment covers both.
    saved.append((client.RateLimiter, "acquire", client.RateLimiter.acquire))

    def _acquire(self) -> None:
        return None

    client.RateLimiter.acquire = _acquire
    return saved


def drive(
    world,
    directory: Path,
    *,
    mode: str,
    cycles: int,
    send: bool = False,
    authorize: bool = False,
    override: bool = False,
    fresh: bool = True,
    interval: float = 60.0,
    timeout: float = 2.0,
    llm_timeout: float | None = None,
) -> None:
    """Run the real loop. `authorize` and `override` exist only inside this call."""
    directory.mkdir(parents=True, exist_ok=True)
    updates: dict[str, str | None] = {
        "WALLEX_API_KEY": "e2e-mock-key",
        "WALLEX_SUBACCOUNT_ID": "e2e-sub",
        "LLM_BASE_URL": f"{world.base}/llm/v1",
        "LLM_API_KEY": "e2e-llm-key",
        "TRADER_LLM_USAGE_PATH": str(directory / "llm_usage.json"),
        "TRADER_SENTIMENT_CACHE": str(directory / "sentiment.json"),
        "TRADER_HEALTH_PORT": None,
        "TRADER_DRY_RUN": None,
        "TRADER_LIVE_AUTHORIZED": "1" if authorize else None,
        "TRADER_EV_OVERRIDE": "1" if override else None,
        "TRADER_DATA_DIR": None,
    }
    with scoped_env(updates):
        cfg = load_config("config/default.yaml")
        cfg["exchange"]["base_url"] = world.base
        cfg["exchange"]["timeout_seconds"] = timeout
        cfg["mcp"]["url"] = f"{world.base}/mcp"
        cfg["sentiment"]["enabled"] = False
        if llm_timeout is not None:
            cfg["llm"]["timeout_seconds"] = llm_timeout
            cfg["llm"]["routine_timeout_seconds"] = llm_timeout
        duration = 1.0 if cycles <= 1 else (cycles - 1) * interval + 1
        run_runtime(
            cfg,
            mode=mode,
            out_dir=directory,
            state_path=directory / "state.json",
            duration=None if cycles <= 1 else duration,
            once=cycles <= 1,
            dry_run=not send,
            interval=interval,
            fresh=fresh,
        )
