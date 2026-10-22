"""Operational contract for the live book.

Alpha parameters stay in the strategy config. This module is the state
machine, the frozen 0.45% prior, the bot actions, and the checkpoint report.
It does not retune a threshold.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trader.timeutil import parse_ts, to_tehran

STATES = ("SHADOW", "LIVE_ARMED", "LIVE_MANAGE", "FLATTEN", "HALT", "KILLED")
ACTIONS = (
    "A0_NO_CHANGE",
    "A1_HALT_ENTRIES",
    "A2_SIZE_MULT_DOWN",
    "A3_DROP_SYMBOL",
    "A4_REFRESH_FEES",
    "A5_ARM_ENTRIES",
    "A6_RESTART_SAME_SHA",
    "A7_BEGIN_FLATTEN_EARLY",
    "A8_KILL_FRESH",
)
# Higher is tighter. Flatten beats halt, halt beats a size cut, a size cut beats no change.
# Arming entries and clearing a kill are looser than leaving the book alone.
TIGHTNESS = {
    "A8_KILL_FRESH": -2,
    "A5_ARM_ENTRIES": -1,
    "A0_NO_CHANGE": 0,
    "A6_RESTART_SAME_SHA": 0,
    "A4_REFRESH_FEES": 1,
    "A2_SIZE_MULT_DOWN": 2,
    "A3_DROP_SYMBOL": 3,
    "A1_HALT_ENTRIES": 4,
    "A7_BEGIN_FLATTEN_EARLY": 5,
}
SIZE_LADDER = (1.0, 0.5, 0.0)
GATES = (
    "ban",
    "denylist",
    "quote_tmn",
    "volume",
    "spread",
    "universe_cap",
    "momentum_72h",
    "ema",
    "return_24h",
    "pullback_4h",
    "atr",
    "imbalance",
    "ret_15m",
    "cost_gate",
    "depth",
    "zscore",
    "order_vs_depth",
    "llm_veto",
    "llm_confidence",
    "llm_shrink",
    "risk_cap",
    "risk_gross",
    "risk_throttle",
    "risk_kill",
    "risk_cutoff",
    "min_notional",
)
CHECKPOINT_END = {
    "C0": "2026-10-22T21:30:00+03:30",
    "C1": "2026-10-22T22:30:00+03:30",
    "C2": "2026-10-22T23:30:00+03:30",
    "C3": "2026-10-23T01:00:00+03:30",
    "C4": "2026-10-23T08:00:00+03:30",
}
CHECKPOINT_START = {
    "C0": "2026-10-22T19:00:00+03:30",
    "C1": "2026-10-22T21:30:00+03:30",
    "C2": "2026-10-22T22:30:00+03:30",
    "C3": "2026-10-22T23:30:00+03:30",
    "C4": "2026-10-23T01:00:00+03:30",
}
_LOG_CONTEXT = {"config_hash": "", "go_live_sha": None}
_GIT_SHA: str | None = None


def fresh_ops() -> dict:
    return {
        "state": "SHADOW",
        "live_authorized": False,
        "ev_override": False,
        "locked": False,
        "go_live_sha": None,
        "go_live_equity": None,
        "size_multiplier": 1.0,
        "size_cut_done": False,
        "dropped_symbols": [],
        "zscore_block_logged": False,
        "client_boot": None,
        "cycle_seq": 0,
        "adj_seq": 0,
        "fee_fetched_at": None,
        "fee_maker_bps": None,
        "fee_taker_bps": None,
    }


def load_ops(state: dict) -> dict:
    """Return the one ops dict for this process.

    A second call used to copy the dict and store the copy. The cycle keeps the
    first object, so a HALT or a cycle_seq bump after next_client_id() was
    written to the copy that save_state no longer saw. The next cycle then
    reopened LIVE_ARMED.
    """
    raw = state.get("ops")
    if isinstance(raw, dict) and raw.get("_ops_ready"):
        return raw
    merged = fresh_ops()
    if isinstance(raw, dict):
        merged.update({key: value for key, value in raw.items() if key != "_ops_ready"})
    merged["state"] = str(merged.get("state") or "SHADOW")
    if merged["state"] not in STATES:
        merged["state"] = "SHADOW"
    merged["dropped_symbols"] = [str(item) for item in (merged.get("dropped_symbols") or [])]
    merged["size_multiplier"] = float(merged.get("size_multiplier") if merged.get("size_multiplier") is not None else 1.0)
    merged["_ops_ready"] = True
    state["ops"] = merged
    return merged


def frozen_gross_prior(cfg: dict) -> float:
    return float((cfg.get("ops") or {}).get("frozen_gross_prior", 0.0045))


def entry_cutoff(cfg: dict) -> datetime:
    raw = (cfg.get("ops") or {}).get("entry_cutoff") or "2026-10-22T23:30:00+03:30"
    return parse_ts(str(raw))


def git_sha() -> str:
    global _GIT_SHA
    if _GIT_SHA:
        return _GIT_SHA
    env = os.getenv("TRADER_GIT_SHA", "").strip()
    if env:
        _GIT_SHA = env
        return _GIT_SHA
    try:
        _GIT_SHA = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        _GIT_SHA = "unknown"
    return _GIT_SHA


def config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def set_log_context(cfg: dict, go_live_sha: str | None) -> None:
    _LOG_CONTEXT["config_hash"] = config_hash(cfg)
    _LOG_CONTEXT["go_live_sha"] = go_live_sha


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def llm_model_env() -> str:
    return os.getenv("LLM_MODEL", "").strip()


def llm_override_warning() -> str | None:
    """Warn when LLM_MODEL is set.

    The tier table is not replaced. LLM_MODEL_CHEAP and LLM_MODEL_STRONG
    still choose the model. The value is copied onto the cycle row so a
    set variable is visible.
    """
    value = llm_model_env()
    if not value:
        return None
    return (
        f"LLM_MODEL={value} is set and recorded as llm_model_env. "
        "It does not override the tier table. "
        "LLM_MODEL_CHEAP and LLM_MODEL_STRONG still select the model."
    )


def _secret_values() -> list[str]:
    found = []
    for name in ("WALLEX_API_KEY", "WALLEX_SUBACCOUNT_ID", "LLM_API_KEY", "LIARA_API_TOKEN"):
        value = os.getenv(name, "").strip()
        if len(value) >= 6:
            found.append(value)
    return found


def redact(value, secrets: list[str] | None = None):
    secrets = _secret_values() if secrets is None else secrets
    if isinstance(value, dict):
        return {str(key): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret and secret in text:
                text = text.replace(secret, "***")
        return text
    return value


def key_presence() -> dict:
    return {
        "wallex_key_present": bool(os.getenv("WALLEX_API_KEY", "").strip()),
        "subaccount_present": bool(os.getenv("WALLEX_SUBACCOUNT_ID", "").strip()),
        "llm_key_present": bool(os.getenv("LLM_API_KEY", "").strip()),
    }


def stamp(payload: dict, *, go_live_sha: str | None = None) -> dict:
    """Add the contract clocks and identity. Never write a secret value."""
    current = datetime.now(timezone.utc)
    row = dict(payload)
    row.setdefault("ts_utc", current.strftime("%Y-%m-%dT%H:%M:%SZ"))
    row.setdefault("ts_tehran", to_tehran(current).isoformat(timespec="seconds"))
    row.setdefault("git_sha", git_sha())
    row.setdefault("config_hash", _LOG_CONTEXT.get("config_hash") or "")
    if go_live_sha is not None:
        row["go_live_sha"] = go_live_sha
    else:
        row.setdefault("go_live_sha", _LOG_CONTEXT.get("go_live_sha"))
    model = llm_model_env()
    row.setdefault("llm_model_env", model or None)
    row.setdefault("llm_model_override_applied", False)
    return redact(row)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Facts:
    now: datetime
    mode: str
    send_orders: bool
    equity: float
    daily_drawdown: float = 0.0
    global_drawdown: float = 0.0
    kill_switch: bool = False
    zscore_n: int = 0
    order_rejects: int = 0
    entry_taker_fills: int = 0
    fee_hurdle: float | None = None
    fee_maker_bps: float | None = None
    fee_taker_bps: float | None = None
    pnl_from_golive_pct: float | None = None
    phase: str = "normal"
    dropped: tuple = ()
    unprotected: tuple = ()


def _note_row(ops: dict, *, action: str, before: str, after: str, playbook: str, note: str,
              param=None, old=None, new=None, actor: str = "bot", rejected: str | None = None,
              evidence: list | None = None) -> dict:
    ops["adj_seq"] = int(ops.get("adj_seq") or 0) + 1
    current = datetime.now(timezone.utc)
    row = {
        "id": f"adj-{current.strftime('%Y-%m-%dT%H:%M:%SZ')}-{ops['adj_seq']:02d}",
        "checkpoint": None,
        "action": action if action in ACTIONS else "A0_NO_CHANGE",
        "state_before": before,
        "state_after": after,
        "param": param,
        "old": old,
        "new": new,
        "evidence_cycle_ids": list(evidence or []),
        "actor": actor,
        "playbook_rule": playbook,
        "note": note,
        "corrects": None,
        "rejected_action": rejected,
        "applied": rejected is None and action in ACTIONS,
    }
    if action not in ACTIONS:
        row["rejected_action"] = rejected or action
        row["applied"] = False
        row["action"] = "A0_NO_CHANGE"
    return row


def _queue(ops: dict, row: dict) -> None:
    ops.setdefault("_queued", []).append(row)


def entries_allowed(ops: dict, facts: Facts, cfg: dict) -> tuple[bool, str]:
    reasons = {
        "SHADOW": "shadow",
        "LIVE_MANAGE": "entry_cutoff",
        "FLATTEN": "flatten",
        "HALT": "halt",
        "KILLED": "killed",
    }
    if ops["state"] != "LIVE_ARMED":
        return False, reasons.get(ops["state"], ops["state"])
    minimum = int((cfg.get("ops") or {}).get("zscore_min_n", 18))
    if int(facts.zscore_n) < minimum:
        return False, "zscore_warmup"
    if float(ops.get("size_multiplier") or 0) <= 0:
        return False, "size_zero"
    return True, ""


def advance(ops: dict, facts: Facts, cfg: dict, *, stage: str) -> list[dict]:
    """Apply bot actions. `pre` runs before orders. `post` runs after them."""
    ops.setdefault("_queued", [])
    before = ops["state"]
    if not ops.get("locked"):
        ops["live_authorized"] = _env_flag("TRADER_LIVE_AUTHORIZED")
        ops["ev_override"] = _env_flag("TRADER_EV_OVERRIDE")
    elif _env_flag("TRADER_EV_OVERRIDE") and not ops.get("ev_override"):
        _queue(ops, _note_row(
            ops, action="ev_override_after_golive", before=before, after=before,
            playbook="ev_override", note="ev_override was off at go-live and stays off",
            actor="bot", rejected="ev_override_after_golive",
        ))

    if stage == "pre":
        _go_live_if_needed(ops, facts, cfg)
        _drop_symbols(ops, facts)
        _kill_if_needed(ops, facts, cfg)
        _fee_halt_if_needed(ops, facts, cfg)
        _flatten_window(ops, facts)
        _log_zscore_block(ops, facts, cfg)
    else:
        _reject_halt(ops, facts)
        _taker_halt(ops, facts)
        _size_cut(ops, facts, cfg)
        _entry_cutoff(ops, facts, cfg)
    rows = list(ops.get("_queued") or [])
    ops["_queued"] = []
    return rows


def _go_live_if_needed(ops: dict, facts: Facts, cfg: dict) -> None:
    if ops.get("locked"):
        return
    if facts.mode != "live" or not facts.send_orders or not ops.get("live_authorized"):
        ops["state"] = "SHADOW"
        return
    before = ops["state"]
    ops["locked"] = True
    ops["go_live_sha"] = git_sha()
    ops["go_live_equity"] = facts.equity
    set_log_context(cfg, ops["go_live_sha"])
    if _drawdown_killed(facts, cfg):
        ops["state"] = "KILLED"
        playbook = "B5"
    elif to_tehran(facts.now) >= entry_cutoff(cfg):
        ops["state"] = "LIVE_MANAGE"
        playbook = "B7"
    else:
        ops["state"] = "LIVE_ARMED"
        playbook = "C0-R6"
    _queue(ops, _note_row(
        ops, action="A0_NO_CHANGE", before=before, after=ops["state"], playbook=playbook,
        note="live_authorized locked. ev_override is frozen at its pre-live value.",
        param="state", old=before, new=ops["state"], actor="operator:ali",
    ))


def _drawdown_killed(facts: Facts, cfg: dict) -> bool:
    risk = cfg.get("risk") or {}
    daily = float(risk.get("daily_max_drawdown", 0.015))
    global_dd = float(risk.get("global_max_drawdown", 0.03))
    return bool(facts.kill_switch) or facts.daily_drawdown >= daily or facts.global_drawdown >= global_dd


def _kill_if_needed(ops: dict, facts: Facts, cfg: dict) -> None:
    if ops["state"] == "SHADOW" or ops["state"] == "KILLED":
        return
    if not _drawdown_killed(facts, cfg):
        return
    before = ops["state"]
    ops["state"] = "KILLED"
    _queue(ops, _note_row(
        ops, action="A7_BEGIN_FLATTEN_EARLY", before=before, after="KILLED", playbook="B5",
        note="daily -1.5% or global -3% kill. Exits flatten. No strategy number changed.",
        param="state", old=before, new="KILLED",
    ))


def _fee_halt_if_needed(ops: dict, facts: Facts, cfg: dict) -> None:
    if facts.fee_hurdle is None or ops["state"] in {"SHADOW", "HALT", "KILLED", "FLATTEN"}:
        return
    prior = frozen_gross_prior(cfg)
    strategy_prior = float((cfg.get("strategy") or {}).get("expected_gross", 0.009))
    maker_cap = float((cfg.get("ops") or {}).get("fee_maker_bps_max", 25))
    taker_cap = float((cfg.get("ops") or {}).get("fee_taker_bps_max", 30))
    override = bool(ops.get("locked") and ops.get("ev_override"))
    worse_fee = (facts.fee_maker_bps or 0) > maker_cap or (facts.fee_taker_bps or 0) > taker_cap
    halt = False
    note = ""
    if override:
        if worse_fee and facts.fee_hurdle > strategy_prior:
            halt = True
            note = "refreshed fee is worse than 25/30 bps and the hurdle exceeds the strategy prior"
    elif facts.fee_hurdle > prior:
        halt = True
        note = "fee hurdle exceeds the frozen 0.45% prior"
    if not halt:
        return
    before = ops["state"]
    ops["state"] = "HALT"
    _queue(ops, _note_row(
        ops, action="A4_REFRESH_FEES", before=before, after=before, playbook="B4",
        note="stored the refreshed fee tier",
        param="fee_hurdle", old=None, new=facts.fee_hurdle,
    ))
    _queue(ops, _note_row(
        ops, action="A1_HALT_ENTRIES", before=before, after="HALT", playbook="B4",
        note=note, param="fee_hurdle", old=prior, new=facts.fee_hurdle,
    ))


def _flatten_window(ops: dict, facts: Facts) -> None:
    if facts.phase not in {"passive", "aggressive"}:
        return
    if ops["state"] not in {"LIVE_ARMED", "LIVE_MANAGE", "HALT"}:
        return
    before = ops["state"]
    ops["state"] = "FLATTEN"
    _queue(ops, _note_row(
        ops, action="A0_NO_CHANGE", before=before, after="FLATTEN", playbook="FLATTEN_SCHEDULE",
        note=f"{facts.phase} liquidation window. Passive from Fri 22:50, aggressive from 23:20.",
        param="state", old=before, new="FLATTEN",
    ))


def _log_zscore_block(ops: dict, facts: Facts, cfg: dict) -> None:
    minimum = int((cfg.get("ops") or {}).get("zscore_min_n", 18))
    if ops["state"] != "LIVE_ARMED" or int(facts.zscore_n) >= minimum or ops.get("zscore_block_logged"):
        return
    ops["zscore_block_logged"] = True
    _queue(ops, _note_row(
        ops, action="A0_NO_CHANGE", before=ops["state"], after=ops["state"], playbook="B3",
        note="zscore_n < 18 blocks entries. Exits still run. The filter stays on.",
        param="entries_allowed", old=True, new=False,
    ))


def _drop_symbols(ops: dict, facts: Facts) -> None:
    held = set(ops.get("dropped_symbols") or [])
    for symbol in facts.dropped:
        name = str(symbol)
        if name in held:
            continue
        held.add(name)
        ops["dropped_symbols"] = sorted(held)
        _queue(ops, _note_row(
            ops, action="A3_DROP_SYMBOL", before=ops["state"], after=ops["state"], playbook="B8",
            note="symbol left the package allowlist or hit the denylist",
            param="universe", old=name, new=None,
        ))


def _reject_halt(ops: dict, facts: Facts) -> None:
    if facts.order_rejects <= 0 or ops["state"] in {"SHADOW", "HALT", "KILLED"}:
        return
    before = ops["state"]
    ops["state"] = "HALT"
    _queue(ops, _note_row(
        ops, action="A1_HALT_ENTRIES", before=before, after="HALT", playbook="B1",
        note="order rejected. No strategy number changed.",
    ))


def _taker_halt(ops: dict, facts: Facts) -> None:
    if facts.entry_taker_fills <= 0 or ops["state"] in {"SHADOW", "HALT", "KILLED"}:
        return
    before = ops["state"]
    ops["state"] = "HALT"
    _queue(ops, _note_row(
        ops, action="A1_HALT_ENTRIES", before=before, after="HALT", playbook="B2",
        note="an entry fill was taker. Maker entries stay maker.",
    ))


def _size_cut(ops: dict, facts: Facts, cfg: dict) -> None:
    if ops.get("size_cut_done") or ops["state"] in {"SHADOW", "KILLED"}:
        return
    if facts.pnl_from_golive_pct is None:
        return
    trigger = float((cfg.get("ops") or {}).get("size_cut_pnl", -0.005))
    if facts.pnl_from_golive_pct > trigger:
        return
    current = float(ops.get("size_multiplier") or 1.0)
    if current <= 0.5:
        ops["size_cut_done"] = True
        return
    ops["size_multiplier"] = 0.5
    ops["size_cut_done"] = True
    _queue(ops, _note_row(
        ops, action="A2_SIZE_MULT_DOWN", before=ops["state"], after=ops["state"], playbook="B6",
        note="equity is <= -0.5% from go-live. Size multiplier 1.0 -> 0.5, once.",
        param="size_multiplier", old=current, new=0.5,
    ))


def _entry_cutoff(ops: dict, facts: Facts, cfg: dict) -> None:
    if ops["state"] != "LIVE_ARMED":
        return
    if to_tehran(facts.now) < entry_cutoff(cfg):
        return
    before = ops["state"]
    ops["state"] = "LIVE_MANAGE"
    _queue(ops, _note_row(
        ops, action="A0_NO_CHANGE", before=before, after="LIVE_MANAGE", playbook="B7",
        note="Thu 23:30 Asia/Tehran entry cutoff. entries_allowed false. cutoff_reason entry_cutoff.",
        param="entries_allowed", old=True, new=False,
    ))


def propose(ops: dict, proposal: dict, cfg: dict, now: datetime) -> dict:
    """Apply one operator or bot change. A loosening attempt is logged and not applied."""
    action = str(proposal.get("action") or "")
    actor = str(proposal.get("actor") or "operator:ali")
    note = str(proposal.get("note") or "")
    before = ops["state"]
    rejected = _loosening_reason(ops, proposal, cfg, now)
    if rejected:
        row = _note_row(
            ops, action=action, before=before, after=before, playbook=str(proposal.get("playbook_rule") or ""),
            note=note or rejected, actor=actor, rejected=rejected,
            param=proposal.get("param"), old=proposal.get("old"), new=proposal.get("new"),
        )
        return row
    _apply_allowed(ops, proposal, cfg, now)
    row = _note_row(
        ops, action=action, before=before, after=ops["state"], playbook=str(proposal.get("playbook_rule") or ""),
        note=note, actor=actor, param=proposal.get("param"), old=proposal.get("old"), new=proposal.get("new"),
    )
    return row


def _loosening_reason(ops: dict, proposal: dict, cfg: dict, now: datetime) -> str | None:
    action = str(proposal.get("action") or "")
    if action not in ACTIONS:
        return action or "unknown_action"
    if action == "A2_SIZE_MULT_DOWN":
        current = float(ops.get("size_multiplier") or 1.0)
        try:
            new = float(proposal.get("new"))
        except (TypeError, ValueError):
            return "raise_size"
        if new not in SIZE_LADDER or new >= current:
            return "raise_size"
        return None
    if action == "A3_DROP_SYMBOL":
        symbol = str(proposal.get("symbol") or proposal.get("new") or "")
        if not symbol:
            return "add_symbol"
        if symbol in set(ops.get("dropped_symbols") or []):
            return None
        return None
    if action == "A5_ARM_ENTRIES":
        if ops["state"] != "HALT":
            return "arm_entries"
        if to_tehran(now) >= entry_cutoff(cfg):
            return "arm_entries"
        actor = str(proposal.get("actor") or "")
        if not actor.startswith("operator"):
            return "arm_entries"
        if not str(proposal.get("note") or "").strip():
            return "arm_entries"
        if "pnl" in str(proposal.get("note") or "").lower() and "recover" in str(proposal.get("note") or "").lower():
            return "clear_kill_on_pnl"
        return None
    if action == "A8_KILL_FRESH":
        if ops["state"] != "KILLED":
            return "clear_kill_on_pnl"
        actor = str(proposal.get("actor") or "")
        if "ali" not in actor.lower():
            return "clear_kill_on_pnl"
        text = str(proposal.get("note") or "")
        if not text.strip():
            return "clear_kill_on_pnl"
        if "recover" in text.lower():
            return "clear_kill_on_pnl"
        return None
    if action == "A1_HALT_ENTRIES":
        return None
    if action == "A7_BEGIN_FLATTEN_EARLY":
        return None
    if action in {"A0_NO_CHANGE", "A4_REFRESH_FEES", "A6_RESTART_SAME_SHA"}:
        return None
    return action


def _apply_allowed(ops: dict, proposal: dict, cfg: dict, now: datetime) -> None:
    action = str(proposal.get("action") or "")
    if action == "A1_HALT_ENTRIES":
        ops["state"] = "HALT"
    elif action == "A2_SIZE_MULT_DOWN":
        ops["size_multiplier"] = float(proposal.get("new"))
    elif action == "A3_DROP_SYMBOL":
        symbol = str(proposal.get("symbol") or proposal.get("old") or proposal.get("new") or "")
        dropped = set(ops.get("dropped_symbols") or [])
        if symbol:
            dropped.add(symbol)
        ops["dropped_symbols"] = sorted(dropped)
    elif action == "A5_ARM_ENTRIES":
        ops["state"] = "LIVE_ARMED"
    elif action == "A7_BEGIN_FLATTEN_EARLY":
        if proposal.get("symbol"):
            dropped = set(ops.get("dropped_symbols") or [])
            # A symbol flatten does not drop it from the universe. The caller sells it.
            ops["flatten_symbol"] = str(proposal.get("symbol"))
        else:
            ops["state"] = "FLATTEN"
    elif action == "A8_KILL_FRESH":
        ops["state"] = "LIVE_MANAGE" if to_tehran(now) >= entry_cutoff(cfg) else "LIVE_ARMED"
    elif action == "A4_REFRESH_FEES":
        if proposal.get("new") is not None:
            ops["fee_hurdle"] = proposal.get("new")


def apply_frozen_cost_gate(features: list, cfg: dict, *, locked_override: bool) -> None:
    """Shadow and live arm buys on the frozen 0.45% prior.

    A locked ev_override restores the strategy prior (0.9%). Backtest does
    not call this. It stays rules-only on strategy.expected_gross.
    """
    if locked_override:
        return
    prior = frozen_gross_prior(cfg)
    multiple = float(cfg["strategy"]["edge_multiple"])
    for row in features:
        blockers = [item for item in row.blockers if not str(item).startswith("edge ")]
        row.expected_gross = prior
        if prior <= float(row.round_trip_cost) * multiple:
            blockers.append(f"edge {prior:.2%} does not clear cost {row.round_trip_cost:.2%}")
        row.blockers = blockers
        row.signal = not blockers
        if not row.signal:
            row.score = 0.0


def cost_gate_view(row, cfg: dict, *, maker: float, taker: float) -> dict:
    from trader.strategy.quotes import half_spread

    prior = float(getattr(row, "expected_gross", frozen_gross_prior(cfg)))
    cost = float(getattr(row, "round_trip_cost", 0.0))
    multiple = float(cfg["strategy"]["edge_multiple"])
    spread = getattr(row, "spread_bps", None)
    hurdle = cost * multiple
    return {
        "prior": prior,
        "fee_in_bps": round(maker * 10_000, 4),
        "fee_out_bps": round(taker * 10_000, 4),
        "half_spread_bps": None if spread is None else round(float(spread) / 2.0, 4),
        "buffer_bps": float((cfg.get("fees") or {}).get("slippage_bps", 3)),
        "hurdle": hurdle,
        "pass": prior > hurdle,
        "half_spread": half_spread(spread),
    }


def gate_trace(results: dict) -> list[dict]:
    trace = []
    for gate in GATES:
        passed, value, threshold = results.get(gate, (True, None, None))
        trace.append({"gate": gate, "pass": bool(passed), "value": value, "threshold": threshold})
    return trace


def first_block(trace: list[dict]) -> str | None:
    for gate in trace:
        if not gate["pass"]:
            return gate["gate"]
    return None


def gates_from_feature(feature: dict, cfg: dict, extra: dict | None = None) -> dict:
    """Map a feature snapshot onto the stable gate ids."""
    extra = extra or {}
    blockers = " ".join(str(item) for item in (feature.get("blockers") or [])).lower()
    spread_cap = float(cfg["universe"]["max_spread_bps"])
    spread = feature.get("spread_bps")
    results = {
        "ban": (not extra.get("banned", False), extra.get("symbol"), None),
        "denylist": (not extra.get("denied", False), extra.get("symbol"), None),
        "quote_tmn": (not extra.get("off_quote", False), extra.get("quote"), "TMN"),
        "volume": ("volume" not in blockers, feature.get("quote_volume"), cfg["universe"].get("min_quote_volume_tmn")),
        "spread": (spread is None or float(spread) <= spread_cap, spread, spread_cap),
        "universe_cap": (not extra.get("capped", False), None, cfg["universe"].get("max_symbols")),
        "momentum_72h": ("slow momentum" not in blockers, feature.get("roc_72h"), cfg["strategy"].get("slow_roc_min")),
        "ema": ("trend structure" not in blockers, feature.get("ema_fast"), feature.get("ema_slow")),
        "return_24h": ("24h return" not in blockers, feature.get("roc_24h"), cfg["strategy"].get("trend_roc_min")),
        "pullback_4h": ("pullback" not in blockers and "4h drop" not in blockers, feature.get("roc_4h"), cfg["strategy"].get("pullback_roc_max")),
        "atr": ("atr " not in blockers and not blockers.startswith("atr"), feature.get("atr_pct"), cfg["strategy"].get("max_atr_pct")),
        "imbalance": ("imbalance" not in blockers, feature.get("imbalance"), cfg["strategy"].get("imbalance_block")),
        "ret_15m": ("15m" not in blockers, feature.get("micro_return"), cfg["strategy"].get("min_micro_return")),
        "cost_gate": ("does not clear" not in blockers, feature.get("expected_gross"), feature.get("round_trip_cost")),
        "depth": ("depth" not in blockers, feature.get("depth_100_tmn"), (cfg.get("liquidity") or {}).get("min_depth_tmn")),
        "zscore": ("z-score" not in blockers and "zscore" not in blockers, feature.get("volume_z"), (cfg.get("liquidity") or {}).get("z_block")),
        "order_vs_depth": ("10%" not in blockers and "depth" not in blockers, feature.get("liquidity_scale"), (cfg.get("liquidity") or {}).get("max_order_fraction_of_depth")),
    }
    for gate, value in (extra.get("gates") or {}).items():
        results[gate] = value
    return results


def tighter_or_equal(operator: str, suggested: str) -> bool:
    if operator not in TIGHTNESS or suggested not in TIGHTNESS:
        return False
    return TIGHTNESS[operator] >= TIGHTNESS[suggested]


def suggest_action(checkpoint: str, summary: dict) -> tuple[str, str]:
    """Pure playbook. No model call."""
    checkpoint = checkpoint.upper()
    def get(name, default=0):
        value = summary.get(name, default)
        return default if value is None else value

    if checkpoint == "C0":
        if get("n_exceptions") or get("n_orders_submitted") or get("fee_fetch_missing") or get("ban_fetch_missing"):
            return "A0_NO_CHANGE", "C0-R1"
        if float(get("median_loop_latency_ms", 0)) > 30_000 or get("n_cycle_errors"):
            return "A0_NO_CHANGE", "C0-R2"
        if int(get("zscore_n", 0)) < 18:
            return "A0_NO_CHANGE", "C0-R3"
        if int(get("n_quant_pass", 0)) == 0:
            return "A0_NO_CHANGE", "C0-R4"
        if float(get("llm_fallback_rate", 0)) >= 1:
            return "A0_NO_CHANGE", "C0-R5"
        return "A0_NO_CHANGE", "C0-R6"
    if checkpoint == "C1":
        if int(get("n_orders_rejected", 0)) > 0:
            return "A1_HALT_ENTRIES", "C1-R1"
        if int(get("n_entry_taker_fills", 0)) > 0:
            return "A1_HALT_ENTRIES", "C1-R2"
        maker = float(get("fee_maker_bps", 0) or 0)
        taker = float(get("fee_taker_bps", 0) or 0)
        hurdle = float(get("fee_hurdle", 0) or 0)
        prior = float(get("frozen_prior", 0.0045) or 0.0045)
        if (maker > 25 or taker > 30) and hurdle > prior:
            return "A1_HALT_ENTRIES", "C1-R3"
        if get("kill_switch"):
            return "A0_NO_CHANGE", "C1-R4"
        if float(get("pnl_from_golive_pct", 0) or 0) <= -0.005:
            if get("stop_fired"):
                return "A1_HALT_ENTRIES", "C1-R5"
            return "A2_SIZE_MULT_DOWN", "C1-R5"
        if not get("zscore_ready", True):
            return "A0_NO_CHANGE", "C1-R6"
        if int(get("n_quant_pass", 0)) == 0:
            return "A0_NO_CHANGE", "C1-R7"
        return "A0_NO_CHANGE", "C1-R8"
    if checkpoint == "C2":
        if summary.get("state") == "KILLED" or get("kill_switch"):
            return "A0_NO_CHANGE", "C2"
        if summary.get("state") == "HALT" or get("halted"):
            return "A1_HALT_ENTRIES", "C2"
        return "A0_NO_CHANGE", "C2"
    if checkpoint == "C3":
        if int(get("unprotected_positions", 0) or 0) > 0:
            return "A7_BEGIN_FLATTEN_EARLY", "C3-R1"
        if int(get("n_cancel_errors", 0) or 0) > 1:
            return "A7_BEGIN_FLATTEN_EARLY", "C3-R2"
        if int(get("n_cancel_errors", 0) or 0) == 1:
            return "A6_RESTART_SAME_SHA", "C3-R2"
        if int(get("n_gaps_over_5min", 0) or 0) > 0 and get("stop_missed_during_gap"):
            return "A7_BEGIN_FLATTEN_EARLY", "C3-R3"
        if int(get("n_gaps_over_5min", 0) or 0) > 0:
            return "A6_RESTART_SAME_SHA", "C3-R3"
        if int(get("n_positions", 0) or 0) == 0 and int(get("n_cycle_errors", 0) or 0) == 0:
            return "A0_NO_CHANGE", "C3-R4"
        return "A0_NO_CHANGE", "C3-R5"
    if checkpoint == "C4":
        if summary.get("git_sha") and summary.get("go_live_sha") and summary.get("git_sha") != summary.get("go_live_sha"):
            return "A1_HALT_ENTRIES", "C4-R1"
        if int(get("n_gaps_over_5min", 0) or 0) > 0 and get("stop_missed_during_gap"):
            return "A7_BEGIN_FLATTEN_EARLY", "C4-R2"
        if int(get("n_gaps_over_5min", 0) or 0) > 0:
            return "A6_RESTART_SAME_SHA", "C4-R2"
        if int(get("n_fills", 0) or 0) == 0 and int(get("n_orders_submitted", 0) or 0) == 0:
            return "A0_NO_CHANGE", "C4-R3"
        return "A0_NO_CHANGE", "C4-R4"
    raise ValueError(f"unknown checkpoint {checkpoint}")


def _parse_tehran(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_tehran(parse_ts(str(value)))
    except (TypeError, ValueError):
        return None


def filter_cycles(cycles: list[dict], checkpoint: str) -> list[dict]:
    checkpoint = checkpoint.upper()
    start = _parse_tehran(CHECKPOINT_START[checkpoint])
    end = _parse_tehran(CHECKPOINT_END[checkpoint])
    chosen = []
    for row in cycles:
        stamp_at = _parse_tehran(row.get("ts_tehran") or row.get("timestamp"))
        if stamp_at is None:
            continue
        if start and stamp_at < start:
            continue
        if end and stamp_at > end:
            continue
        chosen.append(row)
    return chosen


def summarize(cycles: list[dict], decisions: list[dict], checkpoint: str) -> dict:
    checkpoint = checkpoint.upper()
    cycles = filter_cycles(cycles, checkpoint)
    symbol_rows = [row for row in decisions if row.get("record") == "symbol" or row.get("first_block") is not None and row.get("gate_trace")]
    window_ids = {row.get("cycle_id") for row in cycles}
    if window_ids:
        symbol_rows = [row for row in symbol_rows if row.get("cycle_id") in window_ids or not row.get("cycle_id")]
    counts: dict[str, int] = {}
    for row in symbol_rows:
        gate = row.get("first_block")
        if gate:
            counts[gate] = counts.get(gate, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    latencies = sorted(float(row["loop_latency_ms"]) for row in cycles if row.get("loop_latency_ms") is not None)
    gaps = 0
    previous = None
    for row in cycles:
        stamp_at = _parse_tehran(row.get("ts_tehran"))
        if previous and stamp_at and (stamp_at - previous).total_seconds() > 300:
            gaps += 1
        if stamp_at:
            previous = stamp_at
    equities = [float(row["equity_tmn"]) for row in cycles if row.get("equity_tmn") is not None]
    peak = equities[0] if equities else 0.0
    max_dd = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)
    fallback = 0
    llm_rows = 0
    for row in cycles:
        source = row.get("llm_source")
        if source:
            llm_rows += 1
            if source == "fallback":
                fallback += 1
    z_ready_at = None
    for row in cycles:
        if row.get("zscore_ready") and not z_ready_at:
            z_ready_at = row.get("ts_tehran")
    fee_missing = any(row.get("fee_fetched_at") in (None, "") for row in cycles) if cycles else True
    ban_missing = any(not row.get("ban_list_hash") for row in cycles) if cycles else True
    last = cycles[-1] if cycles else {}
    summary = {
        "checkpoint": checkpoint,
        "window_start": cycles[0].get("ts_tehran") if cycles else CHECKPOINT_START[checkpoint],
        "window_end": cycles[-1].get("ts_tehran") if cycles else CHECKPOINT_END[checkpoint],
        "n_cycles": len(cycles),
        "n_gaps_over_5min": gaps,
        "n_quant_pass": sum(int(row.get("n_quant_pass") or 0) for row in cycles),
        "n_cost_pass": sum(int(row.get("n_cost_pass") or 0) for row in cycles),
        "n_orders_submitted": sum(int(row.get("n_orders_submitted") or 0) for row in cycles),
        "n_orders_rejected": sum(int(row.get("n_orders_rejected") or 0) for row in cycles),
        "n_fills": sum(int(row.get("n_fills") or 0) for row in cycles),
        "n_entry_taker_fills": sum(int(row.get("n_entry_taker_fills") or 0) for row in cycles),
        "n_exceptions": sum(len(row.get("errors") or []) for row in cycles),
        "n_cycle_errors": sum(1 for row in cycles if row.get("errors")),
        "n_cancel_errors": sum(int(row.get("n_cancel_errors") or 0) for row in cycles),
        "equity_start_tmn": equities[0] if equities else None,
        "equity_end_tmn": equities[-1] if equities else None,
        "max_drawdown_pct": max_dd,
        "fee_maker_bps": last.get("fee_maker_bps"),
        "fee_taker_bps": last.get("fee_taker_bps"),
        "fee_hurdle": last.get("fee_hurdle"),
        "fee_fetch_missing": fee_missing,
        "ban_fetch_missing": ban_missing,
        "zscore_n": min((int(row.get("zscore_n") or 0) for row in cycles), default=0),
        "zscore_ready": any(row.get("zscore_ready") for row in cycles),
        "zscore_ready_at": z_ready_at,
        "llm_fallback_rate": (fallback / llm_rows) if llm_rows else 0.0,
        "llm_usd": sum(float(row.get("llm_usd") or 0) for row in cycles),
        "top_blocks": [{"gate": gate, "count": count} for gate, count in ranked[:8]],
        "top_blocks_text": ", ".join(f"{gate} {count}" for gate, count in ranked[:5]) or "none",
        "git_sha": last.get("git_sha"),
        "go_live_sha": last.get("go_live_sha"),
        "config_hash": last.get("config_hash"),
        "median_loop_latency_ms": latencies[len(latencies) // 2] if latencies else 0,
        "pnl_from_golive_pct": last.get("pnl_from_golive_pct"),
        "kill_switch": any(row.get("kill_switch") for row in cycles),
        "state": last.get("state"),
        "halted": last.get("state") == "HALT",
        "stop_fired": any(row.get("stop_fired") for row in cycles),
        "unprotected_positions": int(last.get("unprotected_positions") or 0),
        "n_positions": len(last.get("open_positions") or []),
        "stop_missed_during_gap": any(row.get("stop_missed_during_gap") for row in cycles),
        "frozen_prior": frozen_gross_prior({}),
        "entries_allowed": last.get("entries_allowed"),
    }
    return summary


def render_cycle_review(summary: dict, suggested: str, rule: str, operator: str | None) -> str:
    lines = [
        f"Checkpoint: {summary['checkpoint']}",
        f"Window: {summary['window_start']} → {summary['window_end']} Asia/Tehran",
        f"Cycles: {summary['n_cycles']}",
        f"Gaps over 5 min: {summary['n_gaps_over_5min']}",
        f"Quant passes: {summary['n_quant_pass']}",
        f"Cost passes: {summary['n_cost_pass']}",
        f"Orders submitted: {summary['n_orders_submitted']}",
        f"Orders rejected: {summary['n_orders_rejected']}",
        f"Fills: {summary['n_fills']}",
        f"Entry taker fills: {summary['n_entry_taker_fills']}",
        f"Equity: {summary['equity_start_tmn']} → {summary['equity_end_tmn']} TMN",
        f"Max drawdown: {summary['max_drawdown_pct']}",
        f"Suggested action: {suggested} ({rule})",
        f"Action taken: {operator or 'pending'}",
        f"Top blocks: {summary['top_blocks_text']}",
    ]
    return "\n".join(lines) + "\n"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_checkpoint(
    log_dir: Path,
    checkpoint: str,
    *,
    out_dir: Path | None = None,
    operator_action: str | None = None,
    operator_rule: str | None = None,
    note: str | None = None,
) -> Path:
    """Draft a checkpoint from local jsonl. No Wallex key and no LLM call."""
    checkpoint = checkpoint.upper()
    if checkpoint not in CHECKPOINT_END:
        raise ValueError(f"checkpoint must be one of {', '.join(CHECKPOINT_END)}")
    log_dir = Path(log_dir)
    cycles = read_jsonl(log_dir / "cycles.jsonl")
    decisions = read_jsonl(log_dir / "decisions.jsonl")
    summary = summarize(cycles, decisions, checkpoint)
    suggested, rule = suggest_action(checkpoint, summary)
    rejected = None
    if operator_action:
        if not tighter_or_equal(operator_action, suggested):
            rejected = operator_action
    review = stamp({
        "record": "review",
        "checkpoint": checkpoint,
        "draft": operator_action is None or rejected is not None,
        "suggested_action": suggested,
        "suggested_rule": rule,
        "operator_action": None if rejected or not operator_action else operator_action,
        "operator_rule": operator_rule,
        "note": note,
        "rejected_action": rejected,
        **{key: summary[key] for key in (
            "window_start", "window_end", "n_cycles", "n_gaps_over_5min", "n_quant_pass",
            "n_cost_pass", "n_orders_submitted", "n_orders_rejected", "n_fills",
            "n_entry_taker_fills", "equity_start_tmn", "equity_end_tmn", "max_drawdown_pct",
            "fee_maker_bps", "fee_taker_bps", "zscore_ready_at", "llm_fallback_rate", "llm_usd",
            "top_blocks", "git_sha", "go_live_sha", "config_hash",
        )},
    }, go_live_sha=summary.get("go_live_sha"))
    append_jsonl(log_dir / "reviews.jsonl", review)
    if rejected:
        append_jsonl(log_dir / "adjustments.jsonl", stamp({
            "record": "adjustment",
            "action": "A0_NO_CHANGE",
            "rejected_action": rejected,
            "applied": False,
            "checkpoint": checkpoint,
            "actor": "operator:ali",
            "playbook_rule": rule,
            "note": f"operator_action {rejected} is looser than {suggested}",
            "state_before": summary.get("state"),
            "state_after": summary.get("state"),
            "evidence_cycle_ids": [],
            "corrects": None,
        }, go_live_sha=summary.get("go_live_sha")))
    target = Path(out_dir) if out_dir else Path("results/shadow" if checkpoint == "C0" else "results/live")
    target.mkdir(parents=True, exist_ok=True)
    text = render_cycle_review(summary, suggested, rule, None if rejected else operator_action)
    path = target / "cycle-review.md"
    path.write_text(text, encoding="utf-8")
    if rejected:
        raise SystemExit(f"rejected_action {rejected} is looser than {suggested}")
    return path


def replay_offline(log_dir: Path) -> list[dict]:
    """Re-read saved prompts and responses. Does not call BoxAPI."""
    from trader.strategy.parser import extract_json

    rows = read_jsonl(Path(log_dir) / "replay.jsonl")
    report = []
    for row in rows:
        response = str(row.get("response") or "")
        prompt = str(row.get("prompt") or "")
        try:
            payload = extract_json(response)
            parsed = True
            error = ""
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            payload = {}
            parsed = False
            error = str(exc)
        digest = text_hash(response)
        report.append({
            "prompt_hash": row.get("prompt_hash") or text_hash(prompt),
            "response_hash": digest,
            "hash_matches_saved": not row.get("response_hash") or row.get("response_hash") == digest,
            "parsed": parsed,
            "error": error,
            "action_id": payload.get("action_id"),
            "decisions": payload.get("decisions"),
            "source": "replay",
        })
    return report


def next_client_id(state: dict) -> str:
    """Client ids stay unique when the process restarts, including after a lost seq."""
    ops = load_ops(state)
    if not ops.get("client_boot"):
        import secrets
        ops["client_boot"] = secrets.token_hex(4)
    state["client_seq"] = int(state.get("client_seq") or 0) + 1
    return f"SC{ops['client_boot']}{state['client_seq']}"


def emit_cycle_logs(
    directory: Path,
    cycle: dict,
    symbols: list[dict],
    adjustments: list[dict],
    replay: dict | None,
) -> None:
    directory = Path(directory)
    go_live = cycle.get("go_live_sha")
    append_jsonl(directory / "cycles.jsonl", stamp(cycle, go_live_sha=go_live))
    for row in symbols:
        append_jsonl(directory / "decisions.jsonl", stamp(row, go_live_sha=go_live))
    for row in adjustments:
        append_jsonl(directory / "adjustments.jsonl", stamp(row, go_live_sha=go_live))
    if replay and (replay.get("prompt") or replay.get("response")):
        prompt = str(replay.get("prompt") or "")
        response = str(replay.get("response") or "")
        append_jsonl(directory / "replay.jsonl", stamp({
            "record": "replay",
            "prompt": prompt,
            "response": response,
            "prompt_hash": text_hash(prompt),
            "response_hash": text_hash(response),
            "model": replay.get("model"),
        }, go_live_sha=go_live))


def attach_evidence(rows: list[dict], cycle_id: str) -> list[dict]:
    attached = []
    for row in rows:
        item = dict(row)
        if item.get("action") not in {"A0_NO_CHANGE", "A6_RESTART_SAME_SHA"} and not item.get("evidence_cycle_ids"):
            item["evidence_cycle_ids"] = [cycle_id]
        attached.append(item)
    return attached
