"""Shadow and live loop.

Shadow uses live public data and a paper portfolio. Live sends orders unless
`--dry-run` is set, in which case the request is built and logged only.
Both modes call the same signal, agent, and risk code as the backtest.
"""

from __future__ import annotations

import logging
import os
import signal
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from trader.config import credential_status, wallex_credentials
from trader.health import configure_status, serve_in_background, update_status
from trader.engine.backtest import _trade_row
from trader.engine.dust import FORCED_FLATTEN, apply_paper, plan_dust_flatten
from trader.engine.journal import Journal
from trader.engine.portfolio import Portfolio
from trader.engine.state import load_state, save_state
from trader.engine.universe import (
    allowlist_symbols,
    base_asset,
    explain_universe,
    hard_deny_bases,
    select_universe,
    symbol_block,
)
from trader.engine.ops import (
    Facts,
    apply_frozen_cost_gate,
    attach_evidence,
    cost_gate_view,
    clock_status,
    entries_allowed,
    emit_cycle_logs,
    first_block,
    gate_trace,
    gates_from_feature,
    advance,
    key_presence,
    load_ops,
    llm_override_warning,
    _note_row,
    next_client_id,
    set_log_context,
    stamp,
    text_hash,
)
from trader.risk.manager import Action, RiskManager, roll_day
from trader.sentiment import SentimentFeed
from trader.strategy.agent import TradingAgent
from trader.strategy.menu import hold_action
from trader.strategy.quotes import execution_cost, inventory_skew, maker_buy_limit, maker_sell_limit
from trader.strategy.signals import build_candidates, evaluate_symbol
from trader.timeutil import iso, now_tehran
from trader.types import Bar, MarketRule
from trader.alerts import classify_hold, notify, report_cycle, send_decision, send_hold, send_skips, send_update
from trader.order_guard import OrderIntent, assess
from trader.scorecard import seal_signals
from trader.wallex.client import (
    FATAL_WALLEX_CODES,
    SPOT_ORDER_PATH,
    WallexClient,
    WallexError,
    fill_totals,
    subaccount_blocked,
)
from trader.wallex.mcp import WallexMcp, candle_data_path
from trader.wallex.precision import OrderRejected, round_price, round_qty

log = logging.getLogger("trader.runtime")


def _request_stop(signum, frame) -> None:
    """Liara stop and restart send SIGTERM. Treat it as a clean shutdown."""
    raise KeyboardInterrupt


def _start_health(directory: Path) -> None:
    """Bind the platform port when TRADER_HEALTH_PORT is set. Liara curls /health."""
    configure_status(directory / "status.json")
    raw = os.getenv("TRADER_HEALTH_PORT", "").strip()
    if not raw:
        return
    host = os.getenv("TRADER_HEALTH_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"TRADER_HEALTH_PORT must be an integer, got {raw!r}") from exc
    serve_in_background(host, port, directory)
    log.info("health listening on http://%s:%s/health", host, port)


def run_runtime(
    cfg: dict,
    *,
    mode: str,
    out_dir: Path,
    duration: float | None,
    once: bool,
    dry_run: bool,
    interval: float | None,
    fresh: bool,
    state_path: Path | None = None,
) -> None:
    if mode not in {"shadow", "live"}:
        raise ValueError(mode)
    api_key, subaccount = wallex_credentials()
    log.info("%s", credential_status())
    send_orders = mode == "live" and not dry_run
    if send_orders and not api_key:
        raise SystemExit("live mode needs WALLEX_API_KEY. Use --dry-run to build requests without sending.")

    client = WallexClient(
        base_url=cfg["exchange"]["base_url"],
        api_key=api_key,
        subaccount_id=subaccount,
        timeout=float(cfg["exchange"]["timeout_seconds"]),
        max_retries=int(cfg["exchange"]["max_retries"]),
        dry_run=not send_orders,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_path or (out_dir / "state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and state_path.exists():
        state_path.unlink()
    state = load_state(state_path, float(cfg["account"]["initial_equity_tmn"]))
    journal = Journal(out_dir)
    _start_health(out_dir)
    update_status(
        mode=mode,
        dry_run=not send_orders,
        data_dir=str(out_dir),
        state_path=str(state_path),
        started_at=iso(),
        positions=[row.symbol for row in state["portfolio"].positions.values()],
        **clock_status(cfg),
    )
    log.info("starting %s dry_run=%s data=%s", mode, not send_orders, out_dir)
    warning = llm_override_warning()
    if warning:
        log.warning("%s", warning)
    set_log_context(cfg, load_ops(state).get("go_live_sha"))
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        _run_loop(
            cfg, mode, client, state, state_path, journal,
            api_key=api_key, subaccount=subaccount,
            duration=duration, once=once, send_orders=send_orders, interval=interval,
        )
    except KeyboardInterrupt:
        log.info("stopped by user")
        save_state(state_path, state)
    journal.close()


def _run_loop(
    cfg, mode, client, state, state_path, journal,
    *, api_key: str, subaccount: str, duration, once, send_orders, interval,
) -> None:
    risk = RiskManager(cfg)
    mcp = None
    if cfg.get("mcp", {}).get("enabled", False):
        mcp = WallexMcp(
            url=cfg["mcp"].get("url", "https://api.wallex.ir/mcp"),
            api_key=api_key,
            timeout=float(cfg["mcp"].get("timeout_seconds", 8)),
        )
    agent = TradingAgent(cfg, mcp=mcp)
    sentiment = SentimentFeed(cfg)
    loop_seconds = float(interval if interval is not None else cfg["runtime"]["loop_interval_seconds"])
    deadline = time.time() + duration if duration else None

    rules = _load_rules(client)
    unavailable = _load_bans(client, int(cfg["exchange"]["package_id"]))
    account_fees: dict[str, tuple[float, float]] = {}
    if api_key:
        _check_subaccount(client, send_orders)
        try:
            account_fees = client.get_fees()
            ops = load_ops(state)
            rates = list(account_fees.values())
            if rates:
                ops["fee_maker_bps"] = max(pair[0] for pair in rates) * 10_000
                ops["fee_taker_bps"] = max(pair[1] for pair in rates) * 10_000
            ops["fee_fetched_at"] = iso()
            log.info("loaded account fees for %s symbols", len(account_fees))
        except WallexError as exc:
            log.warning("account fee endpoint unavailable, using config fees: %s", exc)
    if send_orders:
        _reconcile(client, state, rules)

    universe, spreads = _refresh_universe(client, cfg, rules, unavailable)
    log.info("universe: %s", universe)
    journal.decision({
        "timestamp": iso(),
        "mode": mode,
        "event": "startup",
        "dry_run": not send_orders,
        "universe": universe,
        "universe_audit": explain_universe(cfg, rules, unavailable, spreads),
        "banned_count": len(unavailable),
        "subaccount_header": bool(subaccount),
    })
    if mcp is not None and universe:
        probe = mcp.call_tool(
            "wallex_basic_candle_history",
            {"symbol": universe[0], "resolution": "60", "from": int(time.time()) - 6 * 3600, "to": int(time.time())},
        )
        journal.decision({
            "timestamp": iso(),
            "mode": mode,
            "event": "mcp_probe",
            "tool": "wallex_basic_candle_history",
            "ok": bool(probe),
            "excerpt": (probe or "")[:400],
        })
    log.info("%s", candle_data_path(mcp))
    hourly, micro = _load_history(client, universe, cfg)
    _seed_warmup(state, hourly, cfg)
    markets_at = time.time()
    candles_at = time.time()
    backoff = 1.0

    while True:
        try:
            if time.time() - markets_at > 300:
                rules = _load_rules(client)
                unavailable = _load_bans(client, int(cfg["exchange"]["package_id"]))
                universe, spreads = _refresh_universe(client, cfg, rules, unavailable)
                markets_at = time.time()
            if time.time() - candles_at > float(cfg["runtime"]["candle_refresh_seconds"]):
                # The refresh replaces candle series. Depth and volume samples
                # live on `state` and must outlive that replacement.
                warm = _warmup_snapshot(state)
                hourly, micro = _load_history(
                    client, universe, cfg, previous=hourly, previous_micro=micro,
                )
                _restore_warmup(state, warm)
                _seed_warmup(state, hourly, cfg)
                candles_at = time.time()
            _cycle(
                cfg, mode, client, agent, risk, journal, state, state_path,
                rules, unavailable, account_fees, universe, spreads, hourly, micro,
                send_orders=send_orders, sentiment=sentiment,
            )
            backoff = 1.0
        except Exception as exc:
            log.exception("cycle failed")
            journal.decision({
                "timestamp": iso(),
                "mode": mode,
                "event": "error",
                "error": str(exc),
                "traceback": traceback.format_exc()[-2000:],
            })
            update_status(last_error=str(exc), last_cycle_at=iso())
            save_state(state_path, state)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        if once:
            break
        if deadline is not None and time.time() >= deadline:
            break
        time.sleep(loop_seconds)


def _emit_contract(
    directory, cfg, state, ops, facts, now, mode, send_orders, equity, portfolio,
    view, universe, unavailable, features, decision, planned, requests, fills, residuals,
    latency_ms, allowed, cutoff_reason, adjustment_rows, order_errors, order_rejects,
    entry_taker_fills, cancel_errors, b9_skips, maker_rate, taker_rate, hurdle, guard_blocks=None,
    seals=None,
    llm_calls=None,
) -> None:
    """Append the contract logs. Rows are stamped and secrets are presence-only."""
    ops["cycle_seq"] = int(ops.get("cycle_seq") or 0) + 1
    cycle_id = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}#{ops['cycle_seq']:05d}"
    usage = decision.usage or {}
    llm_rows = [row for row in (llm_calls if llm_calls is not None else [usage]) if isinstance(row, dict)]
    llm_cost, llm_today, llm_total = _cycle_llm_usd(llm_rows)
    if decision.source == "llm_cache":
        llm_source = "llm_cache"
    elif decision.source == "llm":
        llm_source = "llm"
    else:
        llm_source = "fallback"
    minimum = int((cfg.get("ops") or {}).get("zscore_min_n", 18))
    ban_hash = text_hash("\n".join(sorted(unavailable))) if unavailable else ""
    dust, dust_tmn = _dust_report(state, portfolio, float(cfg["risk"]["min_notional_tmn"]))
    mode_label = "dry-run" if mode == "live" and not send_orders else mode
    golive = ops.get("go_live_equity")
    pnl_pct = None if not golive else (equity / float(golive) - 1)
    symbol_rows = []
    block_counts: dict[str, int] = {}
    n_cost = 0
    for row in features:
        compact = row.compact()
        extra_gates = {}
        veto = any(item.symbol == row.symbol and item.veto for item in decision.decisions)
        extra_gates["llm_veto"] = (not veto, decision.picked_id or decision.source, None)
        low_conf = any(
            item.symbol == row.symbol and item.action == "BUY" and item.confidence < float(cfg["risk"]["min_confidence"])
            for item in decision.decisions
        )
        extra_gates["llm_confidence"] = (not low_conf, None, cfg["risk"]["min_confidence"])
        shrunk = any(item.symbol == row.symbol and item.size_fraction not in (None, 1) for item in decision.decisions)
        extra_gates["llm_shrink"] = (True, None, None)
        if shrunk:
            extra_gates["llm_shrink"] = (True, "shrunk", None)
        reasons = " ".join(
            str(item.get("reason") or "") for item in planned.rejected if item.get("symbol") == row.symbol
        ).lower()
        extra_gates["risk_cap"] = ("cap" not in reasons, None, cfg["risk"].get("max_fraction_per_trade"))
        extra_gates["risk_gross"] = ("gross" not in reasons and "exposure" not in reasons, None, cfg["risk"].get("max_gross_exposure"))
        extra_gates["risk_throttle"] = ("hour" not in reasons and "cooldown" not in reasons, None, None)
        extra_gates["risk_kill"] = (not view.kill_switch, view.kill_reason, cfg["risk"].get("daily_max_drawdown"))
        fill_for_gate = next((item for item in fills if item.symbol == row.symbol), None)
        time_stop = (
            fill_for_gate is not None
            and fill_for_gate.side == "SELL"
            and "time stop" in str(fill_for_gate.reason or "").lower()
        ) or any(
            item.symbol == row.symbol and item.side == "SELL" and item.source == "time"
            for item in planned.actions
        )
        extra_gates["risk_cutoff"] = _risk_cutoff_gate(allowed, cutoff_reason, time_stop=time_stop)
        below_min = "b9" in reasons or "notional" in reasons
        extra_gates["min_notional"] = (not below_min, None, cfg["risk"].get("min_notional_tmn"))
        trace = gate_trace(gates_from_feature(compact, cfg, {"symbol": row.symbol, "gates": extra_gates}))
        guard_hit = next((item for item in (guard_blocks or []) if item.get("symbol") == row.symbol), None)
        if guard_hit:
            trace.append({
                "gate": "order_guard",
                "pass": False,
                "value": guard_hit.get("reason"),
                "threshold": None,
            })
            blocked = "order_guard"
        else:
            blocked = first_block(trace)
        if blocked:
            block_counts[blocked] = block_counts.get(blocked, 0) + 1
        if compact.get("expected_gross") and compact.get("round_trip_cost"):
            if float(compact["expected_gross"]) > float(compact["round_trip_cost"]) * float(cfg["strategy"]["edge_multiple"]):
                n_cost += 1
        position = portfolio.positions.get(row.symbol)
        fill = next((item for item in fills if item.symbol == row.symbol), None)
        reject = next((item for item in planned.rejected if item.get("symbol") == row.symbol), None)
        seal = next((item for item in (seals or []) if item.get("symbol") == row.symbol), None) or {}
        symbol_rows.append({
            "record": "symbol",
            "cycle_id": cycle_id,
            "symbol": row.symbol,
            "prompt_hash": seal.get("prompt_hash"),
            "response_hash": seal.get("response_hash"),
            "action": seal.get("action"),
            "size_mult": seal.get("size_mult"),
            "disposition": seal.get("disposition"),
            "signal": seal.get("signal"),
            "gate_trace": trace,
            "first_block": blocked,
            "cost_gate": cost_gate_view(row, cfg, maker=maker_rate, taker=taker_rate),
            "llm": {
                "tier": usage.get("tier"),
                "model": usage.get("model"),
                "source": llm_source,
                "confidence": next((item.confidence for item in decision.decisions if item.symbol == row.symbol), None),
                "action": seal.get("action"),
                "size_mult": seal.get("size_mult"),
                "veto": veto or seal.get("disposition") == "vetoed",
                "prompt_hash": seal.get("prompt_hash"),
                "response_hash": seal.get("response_hash"),
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens"),
                "usage_cost_usd": usage.get("usd"),
                "rationale": next((item.rationale for item in decision.decisions if item.symbol == row.symbol), decision.comment),
            },
            "risk": {
                "verdict": "reject" if reject else "pass",
                "size_before": seal.get("size_before"),
                "size_after": seal.get("size_after"),
                "reason_codes": [reject["reason"]] if reject else [],
            },
            "liquidity": {
                "depth_1pct_tmn": compact.get("depth_100_tmn"),
                "spread_bps": compact.get("spread_bps"),
                "z_depth": compact.get("depth_z"),
                "z_spread": compact.get("spread_z"),
                "z_volume": compact.get("volume_z"),
                "z_n": facts.zscore_n,
                "size_scale": compact.get("liquidity_scale"),
            },
            "order": None if fill is None and reject is None else {
                "client_id": None if fill is None else fill.client_id,
                "side": None if fill is None else fill.side,
                "type": None if fill is None else fill.order_type,
                "limit_price": None if fill is None else fill.price,
                "qty": None if fill is None else fill.qty,
                "notional_tmn": None if fill is None else fill.notional_tmn,
                "status": "rejected" if fill is None else "filled",
                "reject_reason": None if reject is None else reject.get("reason"),
                "filled_qty": None if fill is None else fill.qty,
                "fee_tmn": None if fill is None else fill.fee_tmn,
                "liquidity_flag": None if fill is None else ("taker" if fill.order_type == "MARKET" else "maker"),
            },
            "position_after": None if position is None else {
                "qty": position.qty,
                "avg_px": position.entry_price,
                "unrealized_tmn": None,
                "hold_minutes": round((time.time() - position.entry_time) / 60, 1),
            },
        })
    seen_symbols = {row.get("symbol") for row in symbol_rows}
    for item in guard_blocks or []:
        if item.get("symbol") in seen_symbols:
            continue
        seen_symbols.add(item.get("symbol"))
        symbol_rows.append({
            "record": "symbol",
            "cycle_id": cycle_id,
            "symbol": item.get("symbol"),
            "gate_trace": [{
                "gate": "order_guard",
                "pass": False,
                "value": item.get("reason"),
                "threshold": None,
            }],
            "first_block": "order_guard",
            "cost_gate": None,
            "llm": None,
            "risk": {"verdict": "reject", "size_before": None, "size_after": None, "reason_codes": ["order_guard"]},
            "liquidity": None,
            "order": None,
            "position_after": None,
        })
        block_counts["order_guard"] = block_counts.get("order_guard", 0) + 1
    if b9_skips:
        adjustment_rows = list(adjustment_rows) + [{
            "action": "A0_NO_CHANGE",
            "state_before": ops["state"],
            "state_after": ops["state"],
            "param": "min_notional",
            "old": b9_skips,
            "new": 0,
            "evidence_cycle_ids": [cycle_id],
            "actor": "bot",
            "playbook_rule": "B9",
            "note": "notional below 50,000 TMN was skipped and not sent",
            "corrects": None,
            "rejected_action": None,
            "applied": True,
        }]
    cycle = {
        "record": "cycle",
        "cycle_id": cycle_id,
        "mode": mode_label,
        "state": ops["state"],
        "loop_latency_ms": round(latency_ms, 1),
        "universe": list(universe),
        "ban_list_hash": ban_hash,
        "denylist_hits": list(facts.dropped),
        "fee_maker_bps": round(maker_rate * 10_000, 4),
        "fee_taker_bps": round(taker_rate * 10_000, 4),
        "fee_fetched_at": ops.get("fee_fetched_at"),
        "fee_hurdle": hurdle,
        "equity_tmn": round(equity, 2),
        "free_tmn": round(portfolio.cash, 2),
        "coin_notional_tmn": round(max(0.0, equity - portfolio.cash), 2),
        "dust": dust,
        "dust_tmn": round(dust_tmn, 2),
        "pnl_from_golive_tmn": None if pnl_pct is None else round(equity - float(golive), 2),
        "pnl_from_golive_pct": pnl_pct,
        "daily_pnl_pct": (equity / view.day_start_equity - 1) if view.day_start_equity else None,
        "drawdown_pct": view.drawdown,
        "kill_switch": view.kill_switch,
        "entries_allowed": allowed,
        "cutoff_reason": cutoff_reason or None,
        "size_multiplier": ops.get("size_multiplier"),
        "ev_override": bool(ops.get("locked") and ops.get("ev_override")),
        "open_positions": [position.symbol for position in portfolio.positions.values()],
        "open_orders": list((state.get("pending") or {}).keys()),
        "n_quant_pass": sum(1 for row in features if row.signal),
        "n_cost_pass": n_cost,
        "n_llm_veto": sum(1 for item in decision.decisions if item.veto),
        "n_orders_submitted": len(requests),
        "n_orders_rejected": order_rejects,
        "n_fills": len(fills),
        "n_entry_taker_fills": entry_taker_fills,
        "n_cancel_errors": cancel_errors,
        "block_counts": block_counts,
        "llm_tier": usage.get("tier"),
        "llm_model": usage.get("model"),
        "llm_source": llm_source,
        "llm_usd": llm_cost,
        "llm_usd_today": llm_today,
        "llm_usd_total": llm_total,
        "veto_all": decision.veto_all,
        "zscore_n": facts.zscore_n,
        "zscore_ready": facts.zscore_n >= minimum,
        "errors": order_errors,
        "stop_fired": any(action.source == "stop" for action in planned.actions),
        "unprotected_positions": len(facts.unprotected),
        "residuals": residuals,
        **key_presence(),
    }
    replay = getattr(decision, "replay", None) or {}
    if replay:
        replay = {**replay, "model": usage.get("model")}
    emit_cycle_logs(directory, cycle, symbol_rows, attach_evidence(adjustment_rows, cycle_id), replay or None)


def _cycle_llm_usd(rows) -> tuple[float, float, float]:
    """This cycle's model cost, today's ledger, and the running ledger.

    Each figure stays a float. A sub-millidollar call must not collapse to 0.
    """
    cost = 0.0
    spent = 0.0
    today = 0.0
    seen_spent = False
    seen_today = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        cost += float(row.get("usd") or 0.0)
        if row.get("spent_usd") is not None:
            spent = max(spent, float(row["spent_usd"]))
            seen_spent = True
        today_value = row.get("spent_today_usd")
        if today_value is None:
            today_value = row.get("spent_usd")
        if today_value is not None:
            today = max(today, float(today_value))
            seen_today = True
    if not seen_spent:
        spent = cost
    if not seen_today:
        today = cost
    return cost, today, spent


def _zscore_n(state: dict, universe: list[str]) -> int:
    """Samples behind the current point, for names that have been warming together.

    The count is the longer of the live depth tape and the hourly-bar seed.
    A symbol that joins on a universe refresh has one sample and would otherwise
    pull the minimum back to 0. Names more than one sample behind the leader are
    still recorded; they do not reset the cohort that is already counting.
    """
    liquidity = state.get("liquidity") or {}
    counts = []
    for symbol in universe:
        row = liquidity.get(symbol)
        if not isinstance(row, dict):
            continue
        depth = row.get("depth") or []
        live = max(0, len(depth) - 1) if depth else 0
        try:
            seed = max(0, int(row.get("seed_n") or 0))
        except (TypeError, ValueError):
            seed = 0
        observed = max(live, seed)
        if observed or depth:
            counts.append(observed)
    if not counts:
        return 0
    leader = max(counts)
    core = [n for n in counts if leader - n <= 1]
    return min(core)


def _seed_warmup(state: dict, hourly: dict, cfg: dict) -> None:
    """Fill the warm-up count from closed hourly bars when that history exists.

    The threshold stays `zscore_min_n`. A series shorter than the window does
    not pretend to be ready. A later short refetch cannot shrink the seed.
    """
    from trader.strategy.liquidity import _quote_points, liquidity_settings

    keep = int(liquidity_settings(cfg)["history"]) + 2
    liquidity = state.setdefault("liquidity", {})
    ready = 0
    for symbol, bars in (hourly or {}).items():
        points = _quote_points(_closed(list(bars or []), 3600), keep)
        seeded = max(0, len(points) - 1)
        row = liquidity.get(symbol)
        if not isinstance(row, dict):
            row = {}
            liquidity[symbol] = row
        row["seed_n"] = max(int(row.get("seed_n") or 0), seeded)
        if bars:
            ready += 1
    if ready:
        counts = [
            int((liquidity.get(symbol) or {}).get("seed_n") or 0)
            for symbol in hourly
            if isinstance(liquidity.get(symbol), dict)
        ]
        log.info("z-score warm-up seeded from %s hourly series, min seed %s", ready, min(counts) if counts else 0)


def _warmup_snapshot(state: dict) -> dict:
    out = {}
    for symbol, row in (state.get("liquidity") or {}).items():
        if not isinstance(row, dict):
            continue
        try:
            seed_n = max(0, int(row.get("seed_n") or 0))
        except (TypeError, ValueError):
            seed_n = 0
        out[symbol] = {
            "depth": list(row.get("depth") or []),
            "spread": list(row.get("spread") or []),
            "volume": list(row.get("volume") or []),
            "volume_ts": list(row.get("volume_ts") or []),
            "seed_n": seed_n,
        }
    return out


def _restore_warmup(state: dict, snap: dict) -> None:
    """Put back any warm-up series that a candle reload shortened or dropped."""
    liquidity = state.setdefault("liquidity", {})
    for symbol, row in snap.items():
        current = liquidity.get(symbol)
        if not isinstance(current, dict):
            current = {}
            liquidity[symbol] = current
        for key, values in row.items():
            if key == "seed_n":
                try:
                    seeded = max(0, int(values or 0))
                except (TypeError, ValueError):
                    seeded = 0
                try:
                    have_seed = max(0, int(current.get("seed_n") or 0))
                except (TypeError, ValueError):
                    have_seed = 0
                current["seed_n"] = max(have_seed, seeded)
                continue
            have = current.get(key) or []
            if len(values) > len(have):
                current[key] = list(values)


def merge_bars(previous, fresh) -> list:
    """Keep the longer candle series. A short refetch must not wipe warm-up bars."""
    prev = list(previous or [])
    new = list(fresh or [])
    if not new:
        return prev
    if len(new) < len(prev):
        by_time = {int(bar.time): bar for bar in prev}
        for bar in new:
            by_time[int(bar.time)] = bar
        return [by_time[key] for key in sorted(by_time)]
    return new


def _unprotected(state: dict, marks: dict, threshold: float) -> list[str]:
    pending_stops = {
        row.get("symbol")
        for row in (state.get("pending") or {}).values()
        if str(row.get("order_type") or "").upper().startswith("STOP")
    }
    found = []
    for symbol, position in state["portfolio"].positions.items():
        if symbol in pending_stops or position.entry_price <= 0:
            continue
        mark = marks.get(symbol, position.entry_price)
        if mark / position.entry_price - 1 <= threshold:
            found.append(symbol)
    return found


def _cycle(
    cfg, mode, client, agent, risk, journal, state, state_path,
    rules, unavailable, account_fees, universe, spreads, hourly, micro,
    send_orders: bool, sentiment: SentimentFeed | None = None,
) -> None:
    started = time.perf_counter()
    ops = load_ops(state)
    denied_now = [
        symbol for symbol in universe
        if symbol_block(symbol, cfg, unavailable, rules.get(symbol))
    ]
    dropped = set(ops.get("dropped_symbols") or [])
    universe = [symbol for symbol in universe if symbol not in dropped and symbol not in denied_now]
    books = _books_for(client, universe)
    marks = {}
    for symbol in universe:
        book = books.get(symbol)
        if book and book.mid:
            marks[symbol] = book.mid
        elif hourly.get(symbol):
            marks[symbol] = hourly[symbol][-1].close
    for symbol, position in state["portfolio"].positions.items():
        marks.setdefault(symbol, position.entry_price)
    portfolio: Portfolio = state["portfolio"]
    for symbol, lot in portfolio.dust.items():
        marks.setdefault(symbol, float(lot.get("mark_price") or lot.get("entry_price") or 0))
    portfolio.remark_dust(marks)
    _refresh_dust_book(state, portfolio, marks)
    if state.get("pending"):
        if send_orders:
            _sync_pending(client, portfolio, state, mode=mode)
        else:
            _sync_paper(cfg, portfolio, state, books, mode=mode)
    portfolio.mark_peaks(marks)
    now = now_tehran()
    now_unix = time.time()
    previous_cycle = float(ops.get("last_cycle_unix") or 0)
    gap_seconds = (now_unix - previous_cycle) if previous_cycle else None
    equity_now = portfolio.equity(marks) if marks else portfolio.cash
    state["day_key"], state["day_start_equity"] = roll_day(
        state["day_key"], now, equity_now, state.get("day_start_equity"),
    )

    mood = sentiment.snapshot(universe) if sentiment is not None else None
    sentiment_calls: list[dict] = []
    if mood is not None:
        sentiment_calls = agent.apply_sentiment(mood, now_unix, allow_network=False)
    feature_by_symbol = {}
    features = []
    for symbol in universe:
        bars = _closed(hourly.get(symbol) or [], 3600)
        if not bars:
            continue
        book = books.get(symbol)
        maker, taker = _fee_pair(cfg, rules.get(symbol), account_fees.get(symbol))
        history = state.setdefault("liquidity", {}).setdefault(symbol, {})
        features.append(
            evaluate_symbol(
                symbol,
                bars,
                cfg,
                spread_bps=book.spread_bps if book else spreads.get(symbol),
                imbalance=book.imbalance() if book else None,
                micro_return=_micro_return(micro.get(symbol) or []),
                maker_fee=maker,
                taker_fee=taker,
                fear_greed=None if mood is None else mood.fear_greed,
                fear_greed_change=None if mood is None else mood.fear_greed_change,
                news_score=None if mood is None else mood.news_score.get(symbol),
                news_change=None if mood is None else mood.news_change.get(symbol),
                news_headlines=None if mood is None else mood.headlines.get(symbol),
                book=book,
                liquidity_history=history,
            )
        )
        feature_by_symbol[symbol] = features[-1]
    apply_frozen_cost_gate(
        features, cfg, locked_override=bool(ops.get("locked") and ops.get("ev_override")),
    )
    candidates = build_candidates(features, cfg)
    if mood is not None and not sentiment_calls and not candidates and not portfolio.positions:
        sentiment_calls = agent.apply_sentiment(mood, now_unix, allow_network=True)
        for row in features:
            row.news_score = mood.news_score.get(row.symbol)
            row.news_change = mood.news_change.get(row.symbol)
            row.news_headlines = list(mood.headlines.get(row.symbol) or [])
    view = risk.view(
        portfolio, marks, now,
        peak_equity=state["peak_equity"],
        day_start_equity=state["day_start_equity"],
        kill_switch=state["kill_switch"],
        kill_reason=state["kill_reason"],
    )
    state["peak_equity"] = view.peak_equity
    state["kill_switch"] = view.kill_switch
    state["kill_reason"] = view.kill_reason
    kindest = min((value for value in spreads.values() if value is not None), default=0.0)
    maker_rate = float(ops["fee_maker_bps"]) / 10_000 if ops.get("fee_maker_bps") is not None else float(cfg["fees"]["maker_rate"])
    taker_rate = float(ops["fee_taker_bps"]) / 10_000 if ops.get("fee_taker_bps") is not None else float(cfg["fees"]["taker_rate"])
    hurdle = float(cfg["strategy"]["edge_multiple"]) * execution_cost(cfg, maker_rate, taker_rate, kindest)
    threshold = float((cfg.get("ops") or {}).get("unprotected_stop_pnl", -0.02))
    facts = Facts(
        now=now,
        mode=mode,
        send_orders=send_orders,
        equity=equity_now,
        daily_drawdown=view.daily_drawdown,
        global_drawdown=view.drawdown,
        kill_switch=view.kill_switch,
        zscore_n=_zscore_n(state, universe),
        fee_hurdle=hurdle,
        fee_maker_bps=maker_rate * 10_000,
        fee_taker_bps=taker_rate * 10_000,
        phase=view.phase,
        dropped=tuple(denied_now),
        unprotected=tuple(_unprotected(state, marks, threshold)),
    )
    pre_rows = advance(ops, facts, cfg, stage="pre")
    set_log_context(cfg, ops.get("go_live_sha"))
    allowed, cutoff_reason = entries_allowed(ops, facts, cfg)
    if not allowed:
        view.entries_blocked = True
        view.block_reason = view.block_reason or cutoff_reason
    liquidity = {row.symbol: row.liquidity for row in features if row.liquidity is not None}
    menu_kwargs = dict(
        portfolio=portfolio,
        candidates=candidates,
        marks=marks,
        rules=rules,
        unavailable=unavailable,
        now=now,
        view=view,
        order_times=state["order_times"],
        cooldowns={k: float(v) for k, v in state["cooldowns"].items()},
        now_unix=now_unix,
        liquidity=liquidity,
        features=features,
        pending=state.get("pending") or {},
    )
    # The menu is fixed before any exit. A stop that frees a slot cannot add a buy
    # the model was not already allowed to take. Gross exposure on `view` is also
    # left stale so a same-cycle entry can only shrink.
    menu_before = risk.legal_menu(**menu_kwargs)
    fills = []
    requests = []
    residuals = []
    order_errors = []
    order_rejects = 0
    entry_taker_fills = 0
    cancel_errors = 0
    b9_skips = 0
    guard_blocks: list[dict] = []
    pending_symbols = {row.get("symbol") for row in state.get("pending", {}).values()}
    early_rejected: list[dict] = []

    def _run_actions(actions, rejected) -> None:
        nonlocal order_rejects, entry_taker_fills, cancel_errors
        for action in actions:
            if action.source == "cancel":
                cancelled = _cancel_resting(client, state, action, send_orders)
                if cancelled is None:
                    rejected.append({"symbol": action.symbol, "reason": "cancel ignored; order is not working"})
                else:
                    requests.append(cancelled)
                    if cancelled.get("error"):
                        cancel_errors += 1
                        rejected.append({"symbol": action.symbol, "reason": cancelled["error"]})
                    else:
                        _relay_cancel(mode, now, cancelled)
                continue
            if action.symbol in pending_symbols:
                rejected.append({"symbol": action.symbol, "reason": "an order for this symbol is already working"})
                continue
            noted_requests = len(requests)
            noted_fills = len(fills)
            held = portfolio.positions.get(action.symbol)
            dust = _try_dust_flatten(
                cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees, unavailable,
                ops_state=ops["state"], guard_blocks=guard_blocks,
            )
            if dust is not None:
                dust_fills, dust_requests, residual = dust
                requests.extend(dust_requests)
                if residual:
                    residuals.append(residual)
                    if not residual.get("repeat"):
                        rejected.append({"symbol": action.symbol, "reason": residual["reason"]})
                        log.warning(
                            "dust %s qty=%s dust_tmn=%s %s",
                            residual.get("symbol"), residual.get("qty"),
                            residual.get("dust_tmn", residual.get("mark_tmn")), residual.get("reason"),
                        )
                for fill in dust_fills:
                    fills.append(fill)
                    state["order_times"].append(now_unix)
                    journal.trade(_trade_row(fill, now, mode))
                _relay_orders(
                    mode, now, action, held, requests[noted_requests:], fills[noted_fills:],
                    feature_by_symbol.get(action.symbol), cfg, hurdle, marks,
                )
                for cancelled in state.pop("_telegram_cancels", []):
                    _relay_cancel(mode, now, cancelled)
                continue
            try:
                before_guard = len(guard_blocks)
                fill, request = _execute(
                    cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees,
                    feature_by_symbol.get(action.symbol), unavailable,
                    ops_state=ops["state"], guard_blocks=guard_blocks,
                )
                if len(guard_blocks) > before_guard:
                    rejected.append({
                        "symbol": action.symbol,
                        "reason": f"order_guard: {guard_blocks[-1]['reason']}",
                    })
                    continue
            except (WallexError, OrderRejected) as exc:
                fatal = isinstance(exc, WallexError) and exc.code in FATAL_WALLEX_CODES
                if fatal:
                    _halt_fatal(state, exc)
                order_rejects += 1
                order_errors.append({
                    "endpoint": "/v1/account/orders",
                    "http_status": getattr(exc, "status", None),
                    "client_id": action.client_order_id or None,
                    "message": str(exc),
                })
                rejected.append({"symbol": action.symbol, "reason": str(exc)})
                if fatal:
                    return
                continue
            if request:
                requests.append(request)
            for extra in state.pop("_extra_fills", []):
                fills.append(extra)
                state["order_times"].append(now_unix)
                journal.trade(_trade_row(extra, now, mode))
            if fill:
                fills.append(fill)
                state["order_times"].append(now_unix)
                journal.trade(_trade_row(fill, now, mode))
                if fill.side == "SELL" and action.source == "stop":
                    state["cooldowns"][action.symbol] = now_unix + float(cfg["risk"]["cooldown_after_stop_seconds"])
                if fill.side == "BUY" and _entry_was_taker(action, request):
                    entry_taker_fills += 1
                if fill.side == "SELL" and str(ops["state"]) in {"FLATTEN", "KILLED"}:
                    _note_unsellable_remainder(state, portfolio, marks, action.symbol, float(cfg["risk"]["min_notional_tmn"]))
            _relay_orders(
                mode, now, action, held, requests[noted_requests:], fills[noted_fills:],
                feature_by_symbol.get(action.symbol), cfg, hurdle, marks,
            )
            for cancelled in state.pop("_telegram_cancels", []):
                _relay_cancel(mode, now, cancelled)

    protective, _deferred = risk.protective_actions(
        portfolio=portfolio, marks=marks, view=view, now_unix=now_unix, liquidity=liquidity,
    )
    for symbol in facts.unprotected:
        if any(item.symbol == symbol and item.side == "SELL" for item in protective):
            continue
        if symbol not in portfolio.positions:
            continue
        protective.append(Action(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            size_fraction=1.0,
            reason="B10 no working stop and position is at or below -2%",
            source="b10",
            price_direction="down",
            aggressive=True,
        ))
    # Stops, trails, and flattens are sent before the model is asked anything.
    _run_actions(protective, early_rejected)
    dispatched = {(action.symbol, action.source) for action in protective}
    pending_symbols = {row.get("symbol") for row in state.get("pending", {}).values()}
    menu_kwargs["pending"] = state.get("pending") or {}
    menu_after = risk.legal_menu(**menu_kwargs)
    already = {item.id for item in menu_before}
    menu = [item for item in menu_after if item.id in already]
    if not any(item.kind == "HOLD" for item in menu):
        menu = [hold_action(), *menu]
    decision = agent.decide(
        features, candidates,
        [p.to_dict() for p in portfolio.positions.values()],
        view.compact(), now_unix,
        menu=menu,
    )
    planned = risk.plan(
        portfolio=portfolio,
        agent=decision,
        candidates=candidates,
        marks=marks,
        rules=rules,
        unavailable=unavailable,
        now=now,
        view=view,
        order_times=state["order_times"],
        cooldowns={k: float(v) for k, v in state["cooldowns"].items()},
        now_unix=now_unix,
        liquidity=liquidity,
        features=features,
    )
    planned.rejected.extend(early_rejected)
    legal_buys = {item.symbol for item in menu_before if item.kind == "BUY"}
    kept_actions = []
    min_notional = float(cfg["risk"]["min_notional_tmn"])
    multiplier = float(ops.get("size_multiplier") or 1.0)
    for action in planned.actions:
        if (action.symbol, action.source) in dispatched:
            continue
        if action.side == "BUY" and action.symbol not in legal_buys:
            planned.rejected.append({
                "symbol": action.symbol,
                "reason": "entry held; the pre-stop menu did not include this buy",
            })
            continue
        if action.side == "BUY":
            action.size_fraction = float(action.size_fraction) * multiplier
            notional = float(action.size_fraction) * float(view.equity)
            if notional < min_notional:
                planned.rejected.append({"symbol": action.symbol, "reason": "B9 notional below 50000 TMN"})
                b9_skips += 1
                continue
        kept_actions.append(action)
    _run_actions(kept_actions, planned.rejected)
    planned.actions = list(protective) + kept_actions
    equity = portfolio.equity(marks) if marks else portfolio.cash
    replay = getattr(decision, "replay", None) or {}
    seals = seal_signals(
        features, decision, planned, candidates, equity,
        prompt=replay.get("prompt") or "",
        response=replay.get("response") or "",
        mode=mode,
    )
    state["peak_equity"] = max(state["peak_equity"], equity)
    journal.equity({
        "timestamp": iso(now),
        "equity_tmn": round(equity, 2),
        "cash_tmn": round(portfolio.cash, 2),
        "exposure": round(portfolio.exposure(marks) if marks else 0, 4),
        "drawdown": round(view.drawdown, 5),
        "peak_tmn": round(state["peak_equity"], 2),
    })
    latency_ms = (time.perf_counter() - started) * 1000
    golive_equity = ops.get("go_live_equity")
    facts.order_rejects = order_rejects
    facts.entry_taker_fills = entry_taker_fills
    facts.equity = equity
    facts.pnl_from_golive_pct = None if not golive_equity else (equity / float(golive_equity) - 1)
    post_rows = advance(ops, facts, cfg, stage="post")
    allowed, cutoff_reason = entries_allowed(ops, facts, cfg)
    report_cycle(
        gap_seconds=gap_seconds,
        ops_state=ops["state"],
        order_rejects=order_rejects,
        entry_taker_fills=entry_taker_fills,
    )
    _relay_skips(mode, now, candidates, features, planned.rejected)
    _relay_hold(
        mode, now, ops, view, facts, cfg, features, candidates, decision,
        planned, allowed, cutoff_reason, portfolio,
    )
    ops["last_cycle_unix"] = now_unix
    _emit_contract(
        journal.directory, cfg, state, ops, facts, now, mode, send_orders, equity, portfolio,
        view, universe, unavailable, features, decision, planned, requests, fills, residuals,
        latency_ms, allowed, cutoff_reason, pre_rows + post_rows, order_errors, order_rejects,
        entry_taker_fills, cancel_errors, b9_skips, maker_rate, taker_rate, hurdle, guard_blocks,
        seals,
        llm_calls=[*sentiment_calls, decision.usage] if decision.usage else list(sentiment_calls),
    )
    journal.decision({
        "timestamp": iso(now),
        "mode": mode,
        "dry_run": not send_orders,
        "latency_ms": round(latency_ms, 1),
        "sentiment": None if mood is None else mood.public(),
        "equity_tmn": round(equity, 2),
        "positions": [p.to_dict() for p in portfolio.positions.values()],
        "features": [row.compact() for row in features],
        "candidates": [row.compact() for row in candidates],
        "agent": decision.as_log(),
        "sealed": seals,
        "llm_calls": [*sentiment_calls, decision.usage] if decision.usage else list(sentiment_calls),
        "risk": {
            "rejected": planned.rejected,
            "actions": [a.__dict__ for a in planned.actions],
            "view": planned.view.compact(),
        },
        "requests": requests,
        "fills": [f.__dict__ for f in fills],
        "residuals": residuals,
    })
    save_state(state_path, state)
    update_status(
        mode=mode,
        dry_run=not send_orders,
        last_cycle_at=iso(now),
        equity_tmn=round(equity, 2),
        cash_tmn=round(portfolio.cash, 2),
        phase=view.phase,
        kill_switch=view.kill_switch,
        ops_state=ops["state"],
        entries_allowed=allowed,
        positions=[
            {"symbol": row.symbol, "qty": row.qty}
            for row in portfolio.positions.values()
        ],
        residuals=residuals,
        last_error="",
        **clock_status(cfg),
    )
    log.info(
        "%s state=%s equity=%.0f exposure=%.1f%% phase=%s candidates=%s actions=%s source=%s tier=%s pick=%s",
        mode, ops["state"], equity, view.gross_exposure * 100, view.phase, len(candidates),
        len(planned.actions), decision.source, (decision.usage or {}).get("tier") or "-",
        decision.picked_id or "-",
    )
    log.info("cycle latency %.0f ms", latency_ms)


def _order_qty(position_qty: float, size_fraction: float | None, qty_decimals: int) -> float:
    """Full position unless the action is a partial reduce."""
    fraction = 1.0 if size_fraction is None else float(size_fraction)
    if fraction >= 0.999:
        return position_qty
    qty = float(round_qty(position_qty * fraction, qty_decimals))
    if qty <= 0 or qty > position_qty:
        return position_qty
    return qty


def _cancel_resting(client, state, action, send_orders) -> dict | None:
    client_id = action.client_order_id
    pending = state.setdefault("pending", {})
    meta = pending.get(client_id)
    if not isinstance(meta, dict):
        return None
    if send_orders:
        try:
            client.cancel_order(client_id)
        except WallexError as exc:
            log.warning("cancel %s failed: %s", client_id, exc)
            return {
                "op": "cancel",
                "client_id": client_id,
                "symbol": meta.get("symbol"),
                "error": str(exc),
            }
    pending.pop(client_id, None)
    return {
        "op": "cancel",
        "client_id": client_id,
        "symbol": meta.get("symbol"),
        "side": meta.get("side"),
    }


def _note_guard(guard_blocks, symbol: str, reason: str, side: str) -> None:
    if guard_blocks is None:
        return
    guard_blocks.append({"symbol": symbol, "side": side, "reason": reason})
    log.info("order_guard blocked %s %s: %s", side, symbol, reason)


def _position_notionals(portfolio, marks: dict, symbol: str) -> tuple[float, float]:
    gross = 0.0
    named = 0.0
    for held, position in portfolio.positions.items():
        price = marks.get(held, position.entry_price)
        notional = position.qty * price
        gross += notional
        if held == symbol:
            named = notional
    for held, lot in getattr(portfolio, "dust", {}).items():
        if held in portfolio.positions:
            continue
        price = marks.get(held, lot.get("mark_price") or lot.get("entry_price") or 0)
        notional = float(lot.get("qty") or 0) * float(price or 0)
        gross += notional
        if held == symbol:
            named += notional
    return named, gross


def _order_guard_reason(
    cfg, portfolio, marks, *, symbol, side, order_type, qty, price, post_only, ops_state, unavailable, rule,
) -> str | None:
    """None when the order may be requested. Otherwise the guard's reason."""
    mark = marks.get(symbol) or (rule.last_price if rule is not None else 0) or 0
    symbol_notional, gross = _position_notionals(portfolio, marks, symbol)
    equity = portfolio.equity(marks) if marks or portfolio.positions else portfolio.cash
    held = portfolio.positions.get(symbol)
    intent = OrderIntent(
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=float(qty),
        price=None if price is None else float(price),
        mark=float(mark or 0),
        endpoint=SPOT_ORDER_PATH,
        post_only=bool(post_only),
        ops_state=str(ops_state or ""),
        held_qty=0.0 if held is None else float(held.qty),
        symbol_notional_tmn=symbol_notional,
        gross_notional_tmn=gross,
        equity_tmn=float(equity),
        quote="" if rule is None else str(rule.quote_asset or ""),
        base=base_asset(symbol, rule),
        allowlist=tuple(allowlist_symbols(cfg)),
        deny_bases=tuple(sorted(hard_deny_bases(cfg))),
        unavailable=tuple(sorted(unavailable or ())),
        size_cap=float((cfg.get("risk") or {}).get("max_fraction_per_symbol") or (cfg.get("risk") or {}).get("max_fraction_per_trade") or 0.12),
        gross_cap=float((cfg.get("risk") or {}).get("max_gross_exposure", 0.30)),
        min_notional_tmn=float((cfg.get("risk") or {}).get("min_notional_tmn", 50_000)),
        stop_loss_pct=(cfg.get("risk") or {}).get("stop_loss_pct"),
        entry_fee_rate=(cfg.get("fees") or {}).get("maker_rate"),
    )
    verdict = assess(intent)
    if verdict.ok:
        return None
    return verdict.reason


def _risk_cutoff_gate(allowed, cutoff_reason, *, time_stop: bool):
    """Entry cutoff does not fail a time-stop exit. The trace says time_stop."""
    if time_stop:
        return True, "time_stop", None
    passed = bool(allowed) or cutoff_reason not in {"entry_cutoff", "flatten", "halt"}
    return passed, cutoff_reason, None


def _dust_report(state, portfolio, min_notional: float) -> tuple[list[dict], float]:
    """Logged unsellable dust, plus any other held coin still under the minimum."""
    book = state.get("dust_book") or {}
    dust = []
    total = 0.0
    seen = set()
    for symbol, row in book.items():
        value = float(row.get("dust_tmn") or 0)
        dust.append({
            "symbol": symbol,
            "notional_tmn": round(value, 2),
            "dust_tmn": round(value, 2),
            "qty": row.get("qty"),
        })
        total += value
        seen.add(symbol)
    for position in portfolio.positions.values():
        if position.symbol in seen:
            continue
        notional = position.qty * position.entry_price
        if notional < min_notional:
            dust.append({"symbol": position.symbol, "notional_tmn": round(notional, 2)})
    return dust, total


def _touch_dust(state, symbol: str, qty: float, notional: float, portfolio=None) -> dict:
    """Remember an unsellable remainder. A later cycle reports it and does not send an order."""
    book = state.setdefault("dust_book", {})
    fresh = symbol not in book
    row = {
        "symbol": symbol,
        "qty": qty,
        "dust_tmn": round(float(notional), 2),
        "mark_tmn": round(float(notional), 2),
        "notional_tmn": round(float(notional), 2),
        "reason": "unsellable dust; a top-up buy is not used",
        "repeat": not fresh,
    }
    book[symbol] = row
    if portfolio is not None and symbol in getattr(portfolio, "positions", {}):
        mark = (float(notional) / float(qty)) if qty else 0.0
        portfolio.park_dust(symbol, mark)
    return row


def _refresh_dust_book(state, portfolio, marks: dict) -> None:
    """Keep the logged dust_tmn on the live mark so a later drawdown still sees it."""
    if not getattr(portfolio, "dust", None):
        return
    book = state.setdefault("dust_book", {})
    for symbol, lot in portfolio.dust.items():
        price = marks.get(symbol, lot.get("mark_price") or lot.get("entry_price") or 0)
        value = float(lot.get("qty") or 0) * float(price or 0)
        prior = dict(book.get(symbol) or {})
        prior.update({
            "symbol": symbol,
            "qty": lot.get("qty"),
            "dust_tmn": round(value, 2),
            "mark_tmn": round(value, 2),
            "notional_tmn": round(value, 2),
            "reason": prior.get("reason") or "unsellable dust; a top-up buy is not used",
            "repeat": True,
        })
        book[symbol] = prior


def _note_unsellable_remainder(state, portfolio, marks, symbol: str, min_notional: float) -> None:
    left = portfolio.positions.get(symbol)
    if left is None:
        book = state.get("dust_book") or {}
        book.pop(symbol, None)
        return
    mark = marks.get(symbol) or left.entry_price or 0
    if left.qty * float(mark) + 1e-6 >= min_notional:
        return
    _touch_dust(state, symbol, left.qty, left.qty * float(mark), portfolio)


def _try_dust_flatten(
    cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees, unavailable,
    ops_state="SHADOW", guard_blocks=None,
):
    """Flatten a forced exit that is under the minimum notional.

    Returns None when the held coin already clears the minimum and the normal
    sell should run. Otherwise returns (fills, requests, residual or None).
    """
    if action.side != "SELL" or action.source not in FORCED_FLATTEN:
        return None
    position = portfolio.positions.get(action.symbol)
    rule = rules.get(action.symbol)
    if position is None or rule is None:
        logged = (state.get("dust_book") or {}).get(action.symbol)
        if position is None and logged and str(ops_state) in {"FLATTEN", "KILLED"}:
            return [], [], {**logged, "repeat": True}
        return None
    book = books.get(action.symbol)
    mark = marks.get(action.symbol) or rule.last_price or 0
    buy_price = book.best_ask if book and book.best_ask else mark
    sell_price = book.best_bid if book and book.best_bid else mark
    if not buy_price or not sell_price:
        return None
    _ignored, taker = _fee_pair(cfg, rule, account_fees.get(action.symbol))
    floor = float(rule.min_notional or cfg["risk"].get("min_notional_tmn") or 50_000)
    blocked = symbol_block(action.symbol, cfg, unavailable or set(), rule)
    no_topup = str(ops_state) in {"FLATTEN", "KILLED"}
    plan = plan_dust_flatten(
        qty=position.qty,
        buy_price=float(buy_price),
        sell_price=float(sell_price),
        min_notional=floor,
        min_qty=float(rule.min_qty or 0),
        qty_decimals=int(rule.qty_decimals),
        cash=portfolio.cash,
        buy_fee=taker,
        market_orders_enabled=bool(rule.market_orders_enabled),
        buy_allowed=blocked is None and not no_topup,
        allow_topup=not no_topup,
    )
    if plan.kind == "sell":
        if no_topup:
            rounded = float(round_qty(position.qty, int(rule.qty_decimals)))
            if rounded <= 0 or rounded * float(sell_price) + 1e-6 < floor:
                return [], [], _touch_dust(state, action.symbol, position.qty, plan.mark_tmn, portfolio)
            book = state.get("dust_book") or {}
            book.pop(action.symbol, None)
        return None
    if plan.kind == "dust":
        return [], [], _touch_dust(state, action.symbol, position.qty, plan.mark_tmn, portfolio)
    if plan.kind == "residual":
        return [], [], plan.residual(action.symbol, position.qty)
    slip = float(cfg["execution"].get("stop_slippage_bps", 5)) / 10_000
    paper_sell = float(sell_price) * (1 - slip)
    reason = f"{action.reason}; {plan.reason}"
    requests = []
    buy_fills = []
    try:
        if plan.kind == "topup":
            blocked = _order_guard_reason(
                cfg, portfolio, marks, symbol=action.symbol, side="BUY", order_type="LIMIT",
                qty=plan.buy_qty, price=float(buy_price), post_only=False, ops_state=ops_state,
                unavailable=unavailable, rule=rule,
            )
            if blocked:
                _note_guard(guard_blocks, action.symbol, blocked, "BUY")
                return [], [], {
                    "symbol": action.symbol,
                    "qty": position.qty,
                    "mark_tmn": round(position.qty * float(mark or 0), 2),
                    "reason": f"order_guard: {blocked}",
                }
            buy_id = _next_id(state)
            buy_response = client.place_order(
                symbol=action.symbol, side="BUY", order_type="LIMIT", quantity=plan.buy_qty,
                price=float(buy_price), client_id=buy_id, rule=rule, price_direction="up",
            )
            requests.append(buy_response.get("request") or {})
            if not send_orders:
                sell_id = _next_id(state)
                preview = "MARKET" if rule.market_orders_enabled else "LIMIT"
                sell_response = client.place_order(
                    symbol=action.symbol, side="SELL", order_type=preview, quantity=plan.sell_qty,
                    price=None if preview == "MARKET" else float(sell_price),
                    client_id=sell_id, rule=rule, price_direction="down",
                )
                requests.append(sell_response.get("request") or {})
                fills = apply_paper(
                    portfolio, plan, symbol=action.symbol, buy_price=float(buy_price),
                    sell_price=paper_sell, buy_fee=taker, sell_fee=taker, reason=reason,
                    buy_id=buy_id, sell_id=sell_id,
                )
                if len(fills) < 2 or action.symbol in portfolio.positions:
                    return fills, requests, plan.residual(action.symbol, portfolio.positions.get(action.symbol).qty if action.symbol in portfolio.positions else 0)
                return fills, requests, None
            filled = _wait_fill(client, buy_id)
            if filled is None or filled.executed_qty <= 0:
                _cancel_quiet(client, buy_id)
                return [], requests, plan.residual(action.symbol, position.qty)
            bought = portfolio.top_up(
                symbol=action.symbol, price=filled.executed_price or float(buy_price),
                qty=filled.executed_qty, fee_rate=taker, reason=reason, client_id=buy_id, order_type="LIMIT",
            )
            buy_fills = [] if bought is None else [bought]
        held = portfolio.positions.get(action.symbol)
        if held is None:
            return buy_fills, requests, None
        order_type = "MARKET" if rule.market_orders_enabled else "LIMIT"
        sell_qty_preview = float(round_qty(held.qty, rule.qty_decimals))
        sell_price_preview = None if order_type == "MARKET" else float(sell_price)
        blocked = _order_guard_reason(
            cfg, portfolio, marks, symbol=action.symbol, side="SELL", order_type=order_type,
            qty=sell_qty_preview, price=sell_price_preview, post_only=order_type == "LIMIT" and not action.aggressive,
            ops_state=ops_state, unavailable=unavailable, rule=rule,
        )
        if blocked:
            _note_guard(guard_blocks, action.symbol, blocked, "SELL")
            return buy_fills, requests, {
                "symbol": action.symbol,
                "qty": held.qty,
                "mark_tmn": round(held.qty * float(mark or 0), 2),
                "reason": f"order_guard: {blocked}",
            }
        sell_id = _next_id(state)
        sell_qty = float(round_qty(held.qty, rule.qty_decimals))
        sell_response = client.place_order(
            symbol=action.symbol, side="SELL", order_type=order_type, quantity=sell_qty,
            price=None if order_type == "MARKET" else float(sell_price),
            client_id=sell_id, rule=rule, price_direction="down",
        )
        requests.append(sell_response.get("request") or {})
        if not send_orders:
            fills = apply_paper(
                portfolio, plan, symbol=action.symbol, buy_price=float(buy_price),
                sell_price=paper_sell, buy_fee=taker, sell_fee=taker, reason=reason,
                buy_id=sell_id, sell_id=sell_id,
            )
            if action.symbol in portfolio.positions:
                return fills, requests, plan.residual(action.symbol, portfolio.positions[action.symbol].qty)
            return fills, requests, None
        filled = _wait_fill(client, sell_id)
        if (filled is None or filled.executed_qty <= 0 or action.symbol not in portfolio.positions) and not (
            order_type == "MARKET" and _market_needs_fallback(filled, sell_qty)
        ):
            left_qty = portfolio.positions[action.symbol].qty if action.symbol in portfolio.positions else held.qty
            return buy_fills, requests, plan.residual(action.symbol, left_qty)
        fill = None
        if filled and filled.executed_qty > 0 and action.symbol in portfolio.positions:
            fill = portfolio.sell(
                symbol=action.symbol, price=filled.executed_price or paper_sell, fee_rate=taker,
                reason=reason, client_id=sell_id, order_type=order_type,
                qty=min(filled.executed_qty, sell_qty, portfolio.positions[action.symbol].qty),
            )
        fills = buy_fills + ([] if fill is None else [fill])
        if order_type == "MARKET" and _market_needs_fallback(filled, sell_qty):
            extra = _aggressive_fallback(
                cfg, client, portfolio, action, book, marks, rule, state, taker,
            )
            if extra is not None:
                fills.append(extra)
        left = portfolio.positions.get(action.symbol)
        residual = None if left is None else plan.residual(action.symbol, left.qty)
        if residual:
            residual["reason"] = "residual coin after the dust sell"
        return fills, requests, residual
    except (WallexError, OrderRejected) as exc:
        left = portfolio.positions.get(action.symbol)
        payload = plan.residual(action.symbol, left.qty if left else position.qty)
        payload["reason"] = f"residual coin; dust flatten failed: {exc}"
        return buy_fills, requests, payload


def _pending_meta(symbol, side, price, qty, fee, reason, order_type) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "fee": fee,
        "reason": reason,
        "order_type": order_type,
        "created": time.time(),
        "applied_qty": 0.0,
        "applied_quote": 0.0,
        "applied_base_fee": 0.0,
        "applied_quote_fee": 0.0,
    }


def _partial_buy(order, requested: float) -> bool:
    if order is None or str(order.side or "").upper() not in {"", "BUY"}:
        return False
    done = float(order.executed_qty or 0)
    target = float(order.orig_qty or requested or 0)
    return done > 0 and target > done + 1e-12


def _market_needs_fallback(order, requested: float) -> bool:
    """Wallex cancels a market order when the book moves more than about 2%."""
    if order is None:
        return False
    status = str(order.status or "").upper()
    if status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
        return True
    return float(order.executed_qty or 0) + 1e-9 < float(requested) and not order.active


def _entry_was_taker(action, request) -> bool:
    role = request.get("liquidity") if isinstance(request, dict) else None
    if role == "taker":
        return True
    if role == "maker":
        return False
    return action.order_type == "MARKET" or action.aggressive


def _halt_fatal(state: dict, exc: WallexError) -> None:
    ops = load_ops(state)
    before = ops.get("state")
    ops["state"] = "HALT"
    ops["fatal_exchange_code"] = exc.code
    ops.setdefault("_queued", []).append(_note_row(
        ops, action="A1_HALT_ENTRIES", before=str(before), after="HALT", playbook="WALLEX",
        note=f"Wallex error {exc.code} is fatal. Entries halted.",
        param="fatal_exchange_code", old=None, new=exc.code,
    ))
    notify(f"fatal:{exc.code}", f"Wallex error {exc.code}: trading halted")
    log.error("fatal wallex code %s; HALT", exc.code)


def _check_subaccount(client: WallexClient, send_orders: bool) -> None:
    try:
        status = client.account_status()
    except WallexError as exc:
        if send_orders:
            raise SystemExit(f"refusing to arm live: sub-account check failed: {exc}")
        log.warning("sub-account check failed: %s", exc)
        return
    if status:
        log.info(
            "sub-account title=%s clientId=%s isActive=%s isSuspend=%s tmn=%s",
            status.get("title"),
            status.get("clientId"),
            status.get("isActive"),
            status.get("isSuspend"),
            status.get("tmn_available"),
        )
    if not send_orders:
        return
    reason = subaccount_blocked(status, client.subaccount_id)
    if reason:
        raise SystemExit(f"refusing to arm live: {reason}")


def _book_pending(portfolio: Portfolio, meta: dict, order, client_id: str):
    price_hint = float(meta.get("price") or order.price or 0)
    return _apply_fill_delta(
        portfolio,
        side=str(meta.get("side") or order.side or "").upper(),
        symbol=meta.get("symbol") or order.symbol,
        order=order,
        meta=meta,
        totals=fill_totals(order),
        stop_price=price_hint * 0.955 if price_hint else 0,
        setup="uptrend_pullback",
        reason=meta.get("reason") or "resting order fill",
        client_id=client_id,
        order_type=order.order_type or meta.get("order_type") or "LIMIT",
        fee_rate=float(meta.get("fee") or 0),
        when=int(time.time()),
    )


def _delta_from(meta: dict, totals: dict) -> dict:
    return {
        "qty": max(0.0, float(totals["qty"]) - float(meta.get("applied_qty") or 0)),
        "quote": max(0.0, float(totals["quote"]) - float(meta.get("applied_quote") or 0)),
        "base_fee": max(0.0, float(totals["base_fee"]) - float(meta.get("applied_base_fee") or 0)),
        "quote_fee": max(0.0, float(totals["quote_fee"]) - float(meta.get("applied_quote_fee") or 0)),
    }


def _remember_fill(meta: dict, totals: dict) -> None:
    meta["applied_qty"] = float(totals["qty"])
    meta["applied_quote"] = float(totals["quote"])
    meta["applied_base_fee"] = float(totals["base_fee"])
    meta["applied_quote_fee"] = float(totals["quote_fee"])
    if totals.get("liquidity"):
        meta["liquidity"] = totals["liquidity"]


def _fill_price(order, meta: dict, delta: dict) -> float:
    if order is not None and order.executed_price:
        return float(order.executed_price)
    if delta["qty"] > 0 and delta["quote"] > 0:
        return delta["quote"] / delta["qty"]
    return float(meta.get("price") or 0)


def _apply_fill_delta(
    portfolio, *, side, symbol, order, meta, totals, stop_price, setup, reason, client_id, order_type, fee_rate, when,
):
    delta = _delta_from(meta, totals)
    if delta["qty"] <= 1e-12:
        _remember_fill(meta, totals)
        return None
    price = _fill_price(order, meta, delta)
    if price <= 0 or not symbol:
        return None
    fill = None
    if side == "BUY":
        kwargs = {}
        rate = fee_rate
        if totals.get("from_fills"):
            kwargs = {
                "quote_spent": delta["quote"],
                "base_fee": delta["base_fee"],
                "quote_fee": delta["quote_fee"],
            }
            rate = 0.0
        if symbol in portfolio.positions:
            fill = portfolio.top_up(
                symbol=symbol, price=price, qty=delta["qty"], fee_rate=rate,
                reason=reason, client_id=client_id, order_type=order_type, **kwargs,
            )
        else:
            fill = portfolio.buy(
                symbol=symbol, price=price, qty=delta["qty"], fee_rate=rate, time=when,
                stop_price=stop_price or price, setup=setup or "uptrend_pullback",
                reason=reason, client_id=client_id, order_type=order_type, **kwargs,
            )
    elif symbol in portfolio.positions:
        if totals.get("from_fills"):
            fill = portfolio.sell(
                symbol=symbol, price=price, fee_rate=0, reason=reason, client_id=client_id,
                order_type=order_type, qty=delta["qty"], proceeds_tmn=delta["quote"],
                fee_tmn_paid=delta["quote_fee"],
            )
        else:
            fill = portfolio.sell(
                symbol=symbol, price=price, fee_rate=fee_rate, reason=reason, client_id=client_id,
                order_type=order_type, qty=delta["qty"],
            )
    if fill is not None or delta["qty"] <= 1e-12:
        _remember_fill(meta, totals)
    return fill


def _aggressive_fallback(cfg, client, portfolio, action, book, marks, rule, state, fee):
    """Replace a cancelled or partial market exit with a limit through the bid."""
    position = portfolio.positions.get(action.symbol)
    if position is None or rule is None:
        return None
    mark = float(marks.get(action.symbol) or (book.mid if book is not None else 0) or rule.last_price or 0)
    floor = float((cfg.get("risk") or {}).get("min_notional_tmn") or 50_000)
    left = float(round_qty(position.qty, rule.qty_decimals))
    if left <= 0 or left * mark + 1e-6 < floor:
        log.info("%s market remainder is below 50000 TMN at the mark; no fallback order", action.symbol)
        return None
    raw = (book.best_bid if book is not None and book.best_bid else mark) * (1 - 0.003)
    price = float(round_price(raw, rule.price_decimals, "down"))
    if price <= 0:
        return None
    blocked = _order_guard_reason(
        cfg, portfolio, marks, symbol=action.symbol, side="SELL", order_type="LIMIT",
        qty=left, price=price, post_only=False, ops_state="FLATTEN", unavailable=set(), rule=rule,
    )
    if blocked:
        log.info("aggressive fallback for %s refused: %s", action.symbol, blocked)
        return None
    client_id = _next_id(state)
    client.place_order(
        symbol=action.symbol, side="SELL", order_type="LIMIT", quantity=left, price=price,
        client_id=client_id, rule=rule, price_direction="down", maker=False,
    )
    filled = _wait_fill(client, client_id)
    if filled is None or filled.executed_qty <= 0 or action.symbol not in portfolio.positions:
        return None
    meta = _pending_meta(action.symbol, "SELL", price, left, fee, action.reason, "LIMIT")
    return _apply_fill_delta(
        portfolio, side="SELL", symbol=action.symbol, order=filled, meta=meta,
        totals=fill_totals(filled), stop_price=0, setup="", reason=action.reason,
        client_id=client_id, order_type="LIMIT", fee_rate=fee, when=int(time.time()),
    )


def _wait_fill(client: WallexClient, client_id: str):
    order = None
    for _ in range(3):
        order = _consume_live_order(client, client_id)
        if order is not None and (order.executed_qty > 0 or order.status in {"FILLED", "CANCELED"}):
            return order
        time.sleep(0.3)
    return order


def _cancel_quiet(client: WallexClient, client_id: str) -> None:
    try:
        client.cancel_order(client_id)
    except WallexError as exc:
        log.warning("cancel %s failed: %s", client_id, exc)


def _execute(
    cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees,
    feature=None, unavailable=None, ops_state="SHADOW", guard_blocks=None,
):
    rule: MarketRule | None = rules.get(action.symbol)
    if rule is None:
        raise OrderRejected(f"no market rules for {action.symbol}")
    book = books.get(action.symbol)
    mark = marks.get(action.symbol) or rule.last_price
    maker, taker = _fee_pair(cfg, rule, account_fees.get(action.symbol))
    skew = _quote_skew(cfg, portfolio, marks, feature)
    if action.side == "BUY":
        blocked = symbol_block(action.symbol, cfg, unavailable or set(), rule)
        if blocked:
            _note_guard(guard_blocks, action.symbol, blocked, "BUY")
            return None, None
        maker_entry = cfg["execution"]["entry"] != "marketable_limit" and not action.aggressive
        if maker_entry:
            raw_price = maker_buy_limit(
                book.best_bid if book else None,
                book.best_ask if book else None,
                book.mid if book else mark,
                skew,
            )
            direction = "down"
        else:
            raw_price = book.best_ask if book and book.best_ask else mark
            direction = "up"
        if not raw_price:
            raise OrderRejected(f"no maker bid for {action.symbol}")
        price = float(round_price(raw_price, rule.price_decimals, direction))
        notional = portfolio.equity(marks) * action.size_fraction
        qty = float(round_qty(notional / price, rule.qty_decimals))
        blocked_guard = _order_guard_reason(
            cfg, portfolio, marks, symbol=action.symbol, side="BUY", order_type="LIMIT",
            qty=qty, price=price, post_only=maker_entry, ops_state=ops_state,
            unavailable=unavailable, rule=rule,
        )
        if blocked_guard:
            _note_guard(guard_blocks, action.symbol, blocked_guard, "BUY")
            return None, None
        client_id = _next_id(state)
        prepared = client.prepare_order(
            symbol=action.symbol, side="BUY", order_type="LIMIT", quantity=qty, price=price,
            client_id=client_id, rule=rule, price_direction=direction,
        )
        request = prepared.redacted()
        if send_orders:
            response = client.place_order(
                symbol=action.symbol, side="BUY", order_type="LIMIT", quantity=qty, price=price,
                client_id=client_id, rule=rule, price_direction=direction, maker=maker_entry,
            )
            if response.get("liquidity"):
                request = {**request, "liquidity": response["liquidity"]}
            filled = _consume_live_order(client, client_id)
            fee_paid = taker if not maker_entry else maker
            meta = _pending_meta(
                action.symbol, "BUY", price, qty, fee_paid, action.reason, "LIMIT",
            )
            fill = None
            if filled and filled.executed_qty > 0:
                fill = _apply_fill_delta(
                    portfolio, side="BUY", symbol=action.symbol, order=filled, meta=meta,
                    totals=fill_totals(filled),
                    stop_price=price * (1 - float(cfg["risk"]["stop_loss_pct"])),
                    setup="uptrend_pullback", reason=action.reason, client_id=client_id,
                    order_type="LIMIT", fee_rate=fee_paid, when=int(time.time()),
                )
                if _partial_buy(filled, qty):
                    _cancel_quiet(client, client_id)
                    again = _consume_live_order(client, client_id)
                    if again is not None:
                        extra = _apply_fill_delta(
                            portfolio, side="BUY", symbol=action.symbol, order=again, meta=meta,
                            totals=fill_totals(again),
                            stop_price=price * (1 - float(cfg["risk"]["stop_loss_pct"])),
                            setup="uptrend_pullback", reason=action.reason, client_id=client_id,
                            order_type="LIMIT", fee_rate=fee_paid, when=int(time.time()),
                        )
                        fill = extra or fill
                    _queue_cancel_note(
                        state, client_id=client_id, symbol=action.symbol, side="BUY", order_type="LIMIT",
                    )
            elif maker_entry:
                state.setdefault("pending", {})[client_id] = meta
            else:
                _cancel_quiet(client, client_id)
                _queue_cancel_note(
                    state, client_id=client_id, symbol=action.symbol, side="BUY", order_type="LIMIT",
                )
            return fill, request
        fee = taker if not maker_entry else maker
        if maker_entry:
            state.setdefault("pending", {})[client_id] = {
                "symbol": action.symbol, "side": "BUY", "price": price, "qty": qty,
                "fee": fee, "reason": action.reason, "order_type": "LIMIT", "created": time.time(),
            }
            return None, request
        fill = portfolio.buy(
            symbol=action.symbol, price=price, qty=qty, fee_rate=fee, time=int(time.time()),
            stop_price=price * (1 - float(cfg["risk"]["stop_loss_pct"])),
            setup="uptrend_pullback", reason=action.reason, client_id=client_id, order_type="LIMIT",
        )
        return fill, request

    position = portfolio.positions.get(action.symbol)
    if position is None:
        return None, None
    sell_qty = _order_qty(position.qty, action.size_fraction, rule.qty_decimals)
    aggressive = action.aggressive or action.order_type == "MARKET"
    use_market = aggressive and rule.market_orders_enabled and action.order_type == "MARKET"
    if use_market:
        guard_type, guard_price, guard_post = "MARKET", None, False
    elif aggressive:
        guard_type, guard_price, guard_post = "LIMIT", None, False
    else:
        guard_type, guard_price, guard_post = "LIMIT", None, True
    # Price is filled in below. The notional check uses the mark when the limit is not known yet.
    blocked_guard = _order_guard_reason(
        cfg, portfolio, marks, symbol=action.symbol, side="SELL", order_type=guard_type,
        qty=sell_qty, price=guard_price if guard_price is not None else (None if use_market or aggressive else mark),
        post_only=guard_post, ops_state=ops_state, unavailable=unavailable, rule=rule,
    )
    if blocked_guard:
        _note_guard(guard_blocks, action.symbol, blocked_guard, "SELL")
        return None, None
    client_id = _next_id(state)
    if not use_market and not aggressive:
        raw = maker_sell_limit(
            book.best_bid if book else None,
            book.best_ask if book else None,
            book.mid if book else mark,
            skew,
        ) or (book.best_ask if book and book.best_ask else mark)
        direction = "up"
        price = float(round_price(raw, rule.price_decimals, direction))
        prepared = client.prepare_order(
            symbol=action.symbol, side="SELL", order_type="LIMIT", quantity=sell_qty,
            price=price, client_id=client_id, rule=rule, price_direction=direction,
        )
        request = prepared.redacted()
        fee = maker
        order_type = "LIMIT"
    elif use_market:
        prepared = client.prepare_order(
            symbol=action.symbol, side="SELL", order_type="MARKET", quantity=sell_qty,
            price=None, client_id=client_id, rule=rule, price_direction="down",
        )
        request = prepared.redacted()
        price = (book.best_bid if book and book.best_bid else mark) * (1 - float(cfg["execution"]["stop_slippage_bps"]) / 10_000)
        fee = taker
        order_type = "MARKET"
    else:
        if aggressive:
            raw = (book.best_bid if book and book.best_bid else mark) * (1 - 0.003)
            direction = "down"
        else:
            raw = book.best_ask if book and book.best_ask else mark
            direction = "up"
        price = float(round_price(raw, rule.price_decimals, direction))
        prepared = client.prepare_order(
            symbol=action.symbol, side="SELL", order_type="LIMIT", quantity=sell_qty,
            price=price, client_id=client_id, rule=rule, price_direction=direction,
        )
        request = prepared.redacted()
        fee = taker if aggressive else maker
        order_type = "LIMIT"
    if send_orders:
        response = client.place_order(
            symbol=action.symbol, side="SELL", order_type=order_type,
            quantity=sell_qty, price=None if use_market else price,
            client_id=client_id, rule=rule, price_direction="down" if aggressive else "up",
            maker=not aggressive and not use_market,
        )
        if response.get("liquidity"):
            request = {**request, "liquidity": response["liquidity"]}
        filled = _wait_fill(client, client_id) if use_market else _consume_live_order(client, client_id)
        meta = _pending_meta(action.symbol, "SELL", price, sell_qty, fee, action.reason, order_type)
        fill = None
        if filled and filled.executed_qty > 0 and action.symbol in portfolio.positions:
            fill = _apply_fill_delta(
                portfolio, side="SELL", symbol=action.symbol, order=filled, meta=meta,
                totals=fill_totals(filled), stop_price=0, setup="", reason=action.reason,
                client_id=client_id, order_type=order_type, fee_rate=fee, when=int(time.time()),
            )
        if use_market and _market_needs_fallback(filled, sell_qty):
            extra = _aggressive_fallback(
                cfg, client, portfolio, action, book, marks, rule, state, fee,
            )
            if extra is not None and fill is not None:
                state.setdefault("_extra_fills", []).append(fill)
                return extra, request
            return extra or fill, request
        if fill is None and not aggressive:
            state.setdefault("pending", {})[client_id] = meta
        return fill, request
    if not aggressive:
        state.setdefault("pending", {})[client_id] = {
            "symbol": action.symbol, "side": "SELL", "price": price, "qty": sell_qty, "fee": fee,
            "reason": action.reason, "order_type": order_type, "created": time.time(),
        }
        return None, request
    fill = portfolio.sell(
        symbol=action.symbol, price=price, fee_rate=fee, reason=action.reason,
        client_id=client_id, order_type=order_type, qty=sell_qty,
    )
    return fill, request


def _request_body(request) -> dict:
    if not isinstance(request, dict) or request.get("op") == "cancel":
        return {}
    body = request.get("body")
    if isinstance(body, dict) and body.get("side"):
        return body
    nested = request.get("request")
    if isinstance(nested, dict) and isinstance(nested.get("body"), dict):
        return nested["body"]
    return {}


def _telegram_levels(cfg, price, position, side: str):
    risk = (cfg or {}).get("risk") or {}
    try:
        stop_pct = float(risk.get("stop_loss_pct") or 0)
        arm = float(risk.get("trail_arm_pct") or 0)
    except (TypeError, ValueError):
        return None, None
    if str(side).upper() == "SELL" and position is not None:
        entry = getattr(position, "entry_price", None) or price
        try:
            tp = float(entry) * (1 + arm) if entry else None
        except (TypeError, ValueError):
            tp = None
        return getattr(position, "stop_price", None), tp
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None, None
    return px * (1 - stop_pct), px * (1 + arm)


def _relay_orders(mode, when, action, held, new_requests, new_fills, feature, cfg, hurdle, marks=None) -> None:
    """Tell Telegram about orders this action just produced. Never affects the book."""
    try:
        ordered = {}
        for request in new_requests:
            body = _request_body(request)
            side = str(body.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            price = body.get("price") or (marks or {}).get(str(body.get("symbol") or action.symbol))
            edge = getattr(feature, "expected_gross", None) if side == "BUY" else None
            reason = action.reason or action.source or "signal"
            if side == "BUY" and edge is not None:
                reason = "uptrend pullback"
            sl, tp = _telegram_levels(cfg, price, held if side == "SELL" else None, side)
            client_id = str(body.get("client_id") or "")
            ordered[client_id] = body.get("quantity")
            send_decision(
                mode=mode, when=when, symbol=str(body.get("symbol") or action.symbol),
                side=side, order_type=str(body.get("type") or action.order_type or ""),
                price=price, qty=body.get("quantity"), reason=reason,
                edge=edge, hurdle=hurdle if side == "BUY" else None,
                sl=sl, tp=tp, client_id=client_id,
            )
        for fill in new_fills:
            target = ordered.get(fill.client_id)
            _relay_fill(mode, fill, {"qty": target, "order_type": fill.order_type}, None, when=when)
    except Exception as exc:
        log.warning("telegram note failed: %s", type(exc).__name__)


def _relay_fill(mode, fill, meta, order, when=None) -> None:
    if fill is None:
        return
    try:
        target = None
        status = "FILL"
        if order is not None:
            target = getattr(order, "orig_qty", None) or (meta or {}).get("qty")
            done = getattr(order, "executed_qty", None)
            if done and target and float(done) + 1e-12 < float(target):
                status = "PARTIAL"
        else:
            target = (meta or {}).get("qty") or fill.qty
            try:
                if target and float(fill.qty) < float(target) * 0.98:
                    status = "PARTIAL"
            except (TypeError, ValueError):
                status = "FILL"
        send_update(
            mode=mode, when=when or now_tehran(), symbol=fill.symbol, side=fill.side,
            order_type=fill.order_type or (meta or {}).get("order_type") or "",
            status=status, price=fill.price, qty=fill.qty, qty_total=target or fill.qty,
            pnl=fill.pnl_tmn, fee=getattr(fill, "fee_tmn", None), client_id=fill.client_id,
        )
    except Exception as exc:
        log.warning("telegram note failed: %s", type(exc).__name__)


def _queue_cancel_note(state: dict, *, client_id: str, symbol, side, order_type) -> None:
    state.setdefault("_telegram_cancels", []).append({
        "client_id": client_id,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
    })


def _relay_cancel(mode, when, row: dict) -> None:
    try:
        send_update(
            mode=mode, when=when or now_tehran(), symbol=str(row.get("symbol") or ""),
            side=str(row.get("side") or ""), order_type=str(row.get("order_type") or ""),
            status="CANCEL", price=None, qty=None, qty_total=None, pnl=None, fee=None,
            client_id=str(row.get("client_id") or ""),
        )
    except Exception as exc:
        log.warning("telegram note failed: %s", type(exc).__name__)


def _relay_hold(
    mode, when, ops, view, facts, cfg, features, candidates, decision, planned, allowed, cutoff_reason, portfolio,
) -> None:
    """Tell the trades channel why this cycle held. Never affects the book."""
    try:
        if any(getattr(action, "side", "") in {"BUY", "SELL"} for action in planned.actions):
            return
        reason, names = classify_hold(
            features=features,
            candidates=candidates,
            zscore_n=getattr(facts, "zscore_n", 0),
            zscore_min=int((cfg.get("ops") or {}).get("zscore_min_n", 18)),
            entries_allowed=bool(allowed),
            entries_blocked=bool(getattr(view, "entries_blocked", False)),
            block_reason=str(getattr(view, "block_reason", "") or ""),
            cutoff_reason=str(cutoff_reason or ""),
            decision=decision,
            rejected=getattr(planned, "rejected", None),
        )
        send_hold(
            mode=mode,
            ops_state=str(ops.get("state") or ""),
            when=when,
            reason=reason,
            candidates=names,
            cash=portfolio.cash,
            positions=[(item.symbol, item.qty) for item in portfolio.positions.values()],
        )
    except Exception as exc:
        log.warning("telegram note failed: %s", type(exc).__name__)


def _relay_skips(mode, when, candidates, features, rejected) -> None:
    try:
        rows = []
        seen = set()
        names = {getattr(item, "symbol", None) for item in candidates or []}
        for row in rejected or []:
            symbol = str(row.get("symbol") or "")
            if not symbol or symbol in seen or symbol not in names:
                continue
            rows.append((symbol, str(row.get("reason") or "skipped")))
            seen.add(symbol)
        for feature in features or []:
            if getattr(feature, "signal", True):
                continue
            symbol = str(getattr(feature, "symbol", "") or "")
            if not symbol or symbol in seen:
                continue
            blockers = list(getattr(feature, "blockers", None) or [])
            if not blockers:
                continue
            rows.append((symbol, str(blockers[0])))
            seen.add(symbol)
        send_skips(mode=mode, when=when, rows=rows[:8])
    except Exception as exc:
        log.warning("telegram note failed: %s", type(exc).__name__)


def _sync_pending(client: WallexClient, portfolio: Portfolio, state: dict, mode: str = "") -> None:
    """Apply only the executedQty added since the last sync, then drop finished orders."""
    for client_id, meta in list(state.get("pending", {}).items()):
        order = _consume_live_order(client, client_id)
        if order is None:
            continue
        fill = _book_pending(portfolio, meta, order, client_id)
        _relay_fill(mode, fill, meta, order)
        partial_buy = _partial_buy(order, float(meta.get("qty") or order.orig_qty or 0))
        age = time.time() - float(meta.get("created") or time.time())
        finished = (not order.active) or str(order.status or "").upper() in {
            "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED",
        }
        if partial_buy and order.active:
            _cancel_quiet(client, client_id)
            again = _consume_live_order(client, client_id)
            extra = None
            if again is not None:
                extra = _book_pending(portfolio, meta, again, client_id)
                _relay_fill(mode, extra, meta, again)
            _relay_cancel(mode, None, {
                "client_id": client_id,
                "symbol": meta.get("symbol"),
                "side": meta.get("side"),
                "order_type": meta.get("order_type"),
            })
            state["pending"].pop(client_id, None)
            continue
        if finished or age > 600:
            if order.active:
                _cancel_quiet(client, client_id)
                again = _consume_live_order(client, client_id)
                if again is not None:
                    extra = _book_pending(portfolio, meta, again, client_id)
                    _relay_fill(mode, extra, meta, again)
                _relay_cancel(mode, None, {
                    "client_id": client_id,
                    "symbol": meta.get("symbol"),
                    "side": meta.get("side"),
                    "order_type": meta.get("order_type"),
                })
            elif str(order.status or "").upper() in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
                _relay_cancel(mode, None, {
                    "client_id": client_id,
                    "symbol": meta.get("symbol"),
                    "side": meta.get("side"),
                    "order_type": meta.get("order_type"),
                })
            state["pending"].pop(client_id, None)


def _sync_paper(cfg, portfolio: Portfolio, state: dict, books: dict, mode: str = "") -> None:
    """A resting maker quote fills only after the touch trades through it."""
    for client_id, meta in list(state.get("pending", {}).items()):
        book = books.get(meta.get("symbol"))
        age = time.time() - float(meta.get("created") or time.time())
        price = float(meta.get("price") or 0)
        filled = False
        paper_fill = None
        if book and price > 0 and meta.get("side") == "BUY" and book.best_bid and book.best_bid < price:
            if meta["symbol"] not in portfolio.positions:
                paper_fill = portfolio.buy(
                    symbol=meta["symbol"], price=price, qty=float(meta.get("qty") or 0),
                    fee_rate=float(meta.get("fee") or 0), time=int(time.time()),
                    stop_price=price * (1 - float(cfg["risk"]["stop_loss_pct"])),
                    setup="uptrend_pullback", reason=meta.get("reason") or "paper maker fill",
                    client_id=client_id, order_type="LIMIT",
                )
            filled = True
        elif book and price > 0 and meta.get("side") == "SELL" and book.best_ask and book.best_ask > price:
            position = portfolio.positions.get(meta["symbol"])
            if position is not None:
                raw_qty = meta.get("qty")
                paper_fill = portfolio.sell(
                    symbol=meta["symbol"], price=price, fee_rate=float(meta.get("fee") or 0),
                    reason=meta.get("reason") or "paper maker fill", client_id=client_id, order_type="LIMIT",
                    qty=float(raw_qty) if raw_qty else None,
                )
            filled = True
        if paper_fill is not None:
            _relay_fill(mode, paper_fill, meta, None)
        elif age > 600 and not filled:
            _relay_cancel(mode, None, {
                "client_id": client_id,
                "symbol": meta.get("symbol"),
                "side": meta.get("side"),
                "order_type": meta.get("order_type"),
            })
        if filled or age > 600:
            state["pending"].pop(client_id, None)


def _books_for(client: WallexClient, universe: list[str]) -> dict:
    books = {}
    try:
        all_books = client.get_depth_all()
    except WallexError as exc:
        log.warning("depth/all failed, falling back to per-symbol books: %s", exc)
        all_books = {}
    for symbol in universe:
        book = all_books.get(symbol)
        if book is None or not book.bids:
            try:
                book = client.get_depth(symbol)
            except WallexError as exc:
                log.warning("depth %s failed: %s", symbol, exc)
                continue
        books[symbol] = book
    return books


def _quote_skew(cfg, portfolio, marks, feature) -> float:
    sigma = 0.01
    if feature is not None and feature.atr_pct:
        sigma = float(feature.atr_pct)
    gamma = float((cfg.get("execution") or {}).get("quote_gamma", 0.5))
    tau = float(cfg["risk"]["time_stop_hours"])
    return inventory_skew(portfolio.exposure(marks) if marks else 0.0, sigma, gamma, tau)


def _consume_live_order(client: WallexClient, client_id: str):
    try:
        return client.get_order(client_id)
    except WallexError as exc:
        log.warning("could not read order %s: %s", client_id, exc)
        return None


def _reconcile(client: WallexClient, state: dict, rules: dict[str, MarketRule]) -> None:
    balances = client.get_balances()
    portfolio: Portfolio = state["portfolio"]
    tmn = balances.get("TMN")
    if tmn is not None:
        portfolio.cash = tmn.available
    seen = set()
    for asset, balance in balances.items():
        if asset == "TMN" or balance.total <= 0:
            continue
        symbol = asset if asset.endswith("TMN") else f"{asset}TMN"
        seen.add(symbol)
        if symbol in portfolio.positions:
            portfolio.positions[symbol].qty = balance.total
            continue
        rule = rules.get(symbol)
        last = rule.last_price if rule else 0
        if last <= 0:
            continue
        portfolio.positions[symbol] = portfolio.positions.get(symbol) or _adopt(symbol, balance.total, last)
    for symbol in list(portfolio.positions):
        if symbol not in seen:
            log.info("reconcile dropped %s; exchange balance is zero", symbol)
            del portfolio.positions[symbol]
    try:
        open_orders = client.get_open_orders()
    except WallexError as exc:
        log.warning("open orders reconcile failed: %s", exc)
        open_orders = []
    fee_rates: dict = {}
    try:
        fee_rates = client.get_fees()
    except WallexError as exc:
        log.warning("fee reconcile failed: %s", exc)
    pending = {}
    for order in open_orders:
        maker_fee = fee_rates.get(order.symbol, (0.0025, 0.003))[0]
        meta = _pending_meta(
            order.symbol,
            order.side,
            order.price,
            order.orig_qty,
            maker_fee,
            "reconciled open order",
            order.order_type or "LIMIT",
        )
        meta["status"] = order.status
        meta["origQty"] = order.orig_qty
        if order.created_at is not None:
            meta["created"] = order.created_at
        meta["applied_qty"] = order.executed_qty
        meta["applied_quote"] = order.executed_sum
        pending[order.client_order_id] = meta
    state["pending"] = pending
    log.info("reconciled cash=%s positions=%s open_orders=%s", portfolio.cash, list(portfolio.positions), len(open_orders))


def _adopt(symbol: str, qty: float, last: float):
    from trader.engine.portfolio import Position

    return Position(
        symbol=symbol, qty=qty, entry_price=last, entry_time=int(time.time()),
        cost_tmn=qty * last, stop_price=last * 0.955, peak_price=last,
        setup="reconcile", reason="adopted from exchange balance on startup",
    )


def _load_rules(client: WallexClient) -> dict[str, MarketRule]:
    rules = client.get_markets()
    if not rules:
        raise WallexError("market list was empty")
    return rules


def _load_bans(client: WallexClient, package_id: int) -> set[str]:
    last_error: Exception | None = None
    for _ in range(3):
        try:
            return client.get_unavailable_markets(package_id)
        except WallexError as exc:
            last_error = exc
            time.sleep(1)
    raise SystemExit(f"refusing to trade without the credit-plan ban list: {last_error}")


def _refresh_universe(client, cfg, rules, unavailable):
    preliminary = []
    for symbol in cfg["universe"]["candidates"]:
        symbol = symbol.upper()
        rule = rules.get(symbol)
        if rule and rule.quote_asset == "TMN" and symbol_block(symbol, cfg, unavailable, rule) is None:
            preliminary.append(symbol)
    books = _books_for(client, preliminary)
    spreads = {symbol: book.spread_bps for symbol, book in books.items() if book.spread_bps is not None}
    universe = select_universe(cfg, rules, unavailable, spreads)
    return universe, spreads


def _load_history(
    client, universe, cfg, previous: dict | None = None, previous_micro: dict | None = None,
) -> tuple[dict, dict]:
    end = int(time.time())
    start = end - int(float(cfg["runtime"]["history_days"]) * 86400)
    micro_start = end - 36 * 3600
    hourly: dict[str, list[Bar]] = {}
    micro: dict[str, list[Bar]] = {}
    for symbol in universe:
        try:
            hourly[symbol] = merge_bars(
                (previous or {}).get(symbol),
                client.get_ohlcv(symbol, "1h", start, end),
            )
        except WallexError as exc:
            log.warning("hourly history %s failed: %s", symbol, exc)
            hourly[symbol] = list((previous or {}).get(symbol) or [])
        try:
            micro[symbol] = merge_bars(
                (previous_micro or {}).get(symbol),
                client.get_ohlcv(symbol, "15m", micro_start, end),
            )
        except WallexError as exc:
            log.warning("15m history %s failed: %s", symbol, exc)
            micro[symbol] = list((previous_micro or {}).get(symbol) or [])
    return hourly, micro


def _closed(bars: list[Bar], bucket: int) -> list[Bar]:
    now = int(time.time())
    return [bar for bar in bars if bar.time + bucket <= now]


def _micro_return(bars: list[Bar]) -> float | None:
    closed = _closed(bars, 900)
    if len(closed) < 2 or closed[-2].close == 0:
        return None
    return closed[-1].close / closed[-2].close - 1


def _fee_pair(cfg, rule, account) -> tuple[float, float]:
    if rule is not None and rule.is_zero_fee:
        return 0.0, 0.0
    if account:
        return account
    return float(cfg["fees"]["maker_rate"]), float(cfg["fees"]["taker_rate"])


def _next_id(state) -> str:
    return next_client_id(state)


def _public_response(response: dict) -> dict:
    result = response.get("result") or {}
    return {
        "clientOrderId": result.get("clientOrderId"),
        "status": result.get("status"),
        "executedQty": result.get("executedQty"),
        "dry_run": response.get("dry_run", False),
    }


def check_order(cfg: dict, symbol: str, side: str, notional: float) -> dict:
    """Build one order body from live market rules and do not send it."""
    api_key, subaccount = wallex_credentials()
    client = WallexClient(
        base_url=cfg["exchange"]["base_url"],
        api_key=api_key,
        subaccount_id=subaccount,
        dry_run=True,
    )
    rules = client.get_markets()
    unavailable = client.get_unavailable_markets(int(cfg["exchange"]["package_id"]))
    symbol = symbol.upper()
    if side.upper() == "BUY":
        blocked = symbol_block(symbol, cfg, unavailable, rules.get(symbol))
        if blocked:
            raise SystemExit(blocked)
    rule = rules[symbol]
    book = client.get_depth(symbol)
    price = book.best_ask if side.upper() == "BUY" else book.best_bid
    if not price:
        raise SystemExit(f"no top of book for {symbol}")
    direction = "up" if side.upper() == "BUY" else "down"
    qty = notional / price
    prepared = client.prepare_order(
        symbol=symbol, side=side.upper(), order_type="LIMIT", quantity=qty, price=price,
        client_id=f"SCCHECK{int(time.time())}", rule=rule, price_direction=direction,
    )
    return prepared.redacted()
