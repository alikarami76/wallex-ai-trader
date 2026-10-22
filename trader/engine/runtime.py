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

from trader.config import wallex_credentials
from trader.health import configure_status, serve_in_background, update_status
from trader.engine.backtest import _trade_row
from trader.engine.dust import FORCED_FLATTEN, apply_paper, plan_dust_flatten
from trader.engine.journal import Journal
from trader.engine.portfolio import Portfolio
from trader.engine.state import load_state, save_state
from trader.engine.universe import explain_universe, select_universe, symbol_block
from trader.risk.manager import RiskManager, roll_day
from trader.sentiment import SentimentFeed
from trader.strategy.agent import TradingAgent
from trader.strategy.quotes import inventory_skew, maker_buy_limit, maker_sell_limit
from trader.strategy.signals import build_candidates, evaluate_symbol
from trader.timeutil import iso, now_tehran
from trader.types import Bar, MarketRule
from trader.wallex.client import WallexClient, WallexError
from trader.wallex.mcp import WallexMcp
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
    serve_in_background(host, port)
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
    )
    log.info("starting %s dry_run=%s data=%s", mode, not send_orders, out_dir)
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
    if send_orders:
        try:
            account_fees = client.get_fees()
            log.info("loaded account fees for %s symbols", len(account_fees))
        except WallexError as exc:
            log.warning("account fee endpoint unavailable, using config fees: %s", exc)
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
    hourly, micro = _load_history(client, universe, cfg)
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
                hourly, micro = _load_history(client, universe, cfg, previous=hourly)
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


def _cycle(
    cfg, mode, client, agent, risk, journal, state, state_path,
    rules, unavailable, account_fees, universe, spreads, hourly, micro,
    send_orders: bool, sentiment: SentimentFeed | None = None,
) -> None:
    started = time.perf_counter()
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
    if state.get("pending"):
        if send_orders:
            _sync_pending(client, portfolio, state)
        else:
            _sync_paper(cfg, portfolio, state, books)
    portfolio.mark_peaks(marks)
    now = now_tehran()
    now_unix = time.time()
    equity_now = portfolio.equity(marks) if marks else portfolio.cash
    state["day_key"], state["day_start_equity"] = roll_day(state["day_key"], now, equity_now)
    if not state["day_key"]:
        state["day_key"], state["day_start_equity"] = roll_day("", now, equity_now)

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
    liquidity = {row.symbol: row.liquidity for row in features if row.liquidity is not None}
    menu = risk.legal_menu(
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
    fills = []
    requests = []
    residuals = []
    pending_symbols = {row.get("symbol") for row in state.get("pending", {}).values()}
    for action in planned.actions:
        if action.source == "cancel":
            cancelled = _cancel_resting(client, state, action, send_orders)
            if cancelled is None:
                planned.rejected.append({"symbol": action.symbol, "reason": "cancel ignored; order is not working"})
            else:
                requests.append(cancelled)
                if cancelled.get("error"):
                    planned.rejected.append({"symbol": action.symbol, "reason": cancelled["error"]})
            continue
        if action.symbol in pending_symbols:
            planned.rejected.append({"symbol": action.symbol, "reason": "an order for this symbol is already working"})
            continue
        dust = _try_dust_flatten(
            cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees, unavailable,
        )
        if dust is not None:
            dust_fills, dust_requests, residual = dust
            requests.extend(dust_requests)
            if residual:
                residuals.append(residual)
                planned.rejected.append({"symbol": action.symbol, "reason": residual["reason"]})
                log.warning(
                    "residual %s qty=%s mark_tmn=%s %s",
                    residual.get("symbol"), residual.get("qty"), residual.get("mark_tmn"), residual.get("reason"),
                )
            for fill in dust_fills:
                fills.append(fill)
                state["order_times"].append(now_unix)
                journal.trade(_trade_row(fill, now, mode))
            continue
        try:
            fill, request = _execute(
                cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees,
                feature_by_symbol.get(action.symbol), unavailable,
            )
        except (WallexError, OrderRejected) as exc:
            planned.rejected.append({"symbol": action.symbol, "reason": str(exc)})
            continue
        if request:
            requests.append(request)
        if fill:
            fills.append(fill)
            state["order_times"].append(now_unix)
            journal.trade(_trade_row(fill, now, mode))
            if fill.side == "SELL" and action.source == "stop":
                state["cooldowns"][action.symbol] = now_unix + float(cfg["risk"]["cooldown_after_stop_seconds"])
    equity = portfolio.equity(marks) if marks else portfolio.cash
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
        positions=[
            {"symbol": row.symbol, "qty": row.qty}
            for row in portfolio.positions.values()
        ],
        residuals=residuals,
        last_error="",
    )
    log.info(
        "%s equity=%.0f exposure=%.1f%% phase=%s candidates=%s actions=%s source=%s tier=%s pick=%s",
        mode, equity, view.gross_exposure * 100, view.phase, len(candidates),
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


def _try_dust_flatten(cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees, unavailable):
    """Flatten a forced exit that is under the minimum notional.

    Returns None when the held coin already clears the minimum and the normal
    sell should run. Otherwise returns (fills, requests, residual or None).
    """
    if action.side != "SELL" or action.source not in FORCED_FLATTEN:
        return None
    position = portfolio.positions.get(action.symbol)
    rule = rules.get(action.symbol)
    if position is None or rule is None:
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
        buy_allowed=blocked is None,
    )
    if plan.kind == "sell":
        return None
    if plan.kind == "residual":
        return [], [], plan.residual(action.symbol, position.qty)
    slip = float(cfg["execution"].get("stop_slippage_bps", 5)) / 10_000
    paper_sell = float(sell_price) * (1 - slip)
    reason = f"{action.reason}; {plan.reason}"
    requests = []
    buy_fills = []
    try:
        if plan.kind == "topup":
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
        sell_id = _next_id(state)
        order_type = "MARKET" if rule.market_orders_enabled else "LIMIT"
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
        if filled is None or filled.executed_qty <= 0 or action.symbol not in portfolio.positions:
            left_qty = portfolio.positions[action.symbol].qty if action.symbol in portfolio.positions else held.qty
            return buy_fills, requests, plan.residual(action.symbol, left_qty)
        fill = portfolio.sell(
            symbol=action.symbol, price=filled.executed_price or paper_sell, fee_rate=taker,
            reason=reason, client_id=sell_id, order_type=order_type,
            qty=min(filled.executed_qty, sell_qty, portfolio.positions[action.symbol].qty),
        )
        fills = buy_fills + ([] if fill is None else [fill])
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


def _execute(cfg, client, portfolio, action, books, marks, rules, state, send_orders, account_fees, feature=None, unavailable=None):
    rule: MarketRule | None = rules.get(action.symbol)
    if rule is None:
        raise OrderRejected(f"no market rules for {action.symbol}")
    book = books.get(action.symbol)
    mark = marks.get(action.symbol) or rule.last_price
    maker, taker = _fee_pair(cfg, rule, account_fees.get(action.symbol))
    client_id = _next_id(state)
    skew = _quote_skew(cfg, portfolio, marks, feature)
    if action.side == "BUY":
        blocked = symbol_block(action.symbol, cfg, unavailable or set(), rule)
        if blocked:
            raise OrderRejected(blocked)
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
        prepared = client.prepare_order(
            symbol=action.symbol, side="BUY", order_type="LIMIT", quantity=qty, price=price,
            client_id=client_id, rule=rule, price_direction=direction,
        )
        request = prepared.redacted()
        if send_orders:
            client.place_order(
                symbol=action.symbol, side="BUY", order_type="LIMIT", quantity=qty, price=price,
                client_id=client_id, rule=rule, price_direction=direction,
            )
            filled = _consume_live_order(client, client_id)
            fill = None
            if filled and filled.executed_qty > 0:
                fill_price = filled.executed_price or price
                fee_paid = taker if not maker_entry else maker
                fill = portfolio.buy(
                    symbol=action.symbol, price=fill_price, qty=filled.executed_qty, fee_rate=fee_paid,
                    time=int(time.time()),
                    stop_price=fill_price * (1 - float(cfg["risk"]["stop_loss_pct"])),
                    setup="uptrend_pullback", reason=action.reason, client_id=client_id, order_type="LIMIT",
                )
            elif maker_entry:
                state.setdefault("pending", {})[client_id] = {
                    "symbol": action.symbol, "side": "BUY", "price": price, "qty": qty,
                    "fee": maker, "reason": action.reason, "order_type": "LIMIT", "created": time.time(),
                }
            else:
                try:
                    client.cancel_order(client_id)
                except WallexError as exc:
                    log.warning("cancel unfilled buy %s failed: %s", client_id, exc)
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
        client.place_order(
            symbol=action.symbol, side="SELL", order_type=order_type,
            quantity=sell_qty, price=None if use_market else price,
            client_id=client_id, rule=rule, price_direction="down" if aggressive else "up",
        )
        filled = _consume_live_order(client, client_id)
        fill = None
        if filled and filled.executed_qty > 0 and action.symbol in portfolio.positions:
            fill = portfolio.sell(
                symbol=action.symbol, price=filled.executed_price or price, fee_rate=fee,
                reason=action.reason, client_id=client_id, order_type=order_type,
                qty=min(filled.executed_qty, sell_qty, portfolio.positions[action.symbol].qty),
            )
        elif not aggressive:
            state.setdefault("pending", {})[client_id] = {
                "symbol": action.symbol, "side": "SELL", "price": price, "qty": sell_qty, "fee": fee,
                "reason": action.reason, "order_type": order_type, "created": time.time(),
            }
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


def _sync_pending(client: WallexClient, portfolio: Portfolio, state: dict) -> None:
    """Apply fills for resting exits and cancel them once they are done or stale."""
    for client_id, meta in list(state.get("pending", {}).items()):
        order = _consume_live_order(client, client_id)
        if order is None:
            continue
        if meta.get("side") == "BUY" and order.executed_qty > 0 and meta["symbol"] not in portfolio.positions:
            fill_price = order.executed_price or float(meta.get("price") or 0)
            if fill_price > 0:
                portfolio.buy(
                    symbol=meta["symbol"], price=fill_price, qty=order.executed_qty,
                    fee_rate=float(meta.get("fee") or 0), time=int(time.time()),
                    stop_price=fill_price * 0.955, setup="uptrend_pullback",
                    reason=meta.get("reason") or "resting buy fill", client_id=client_id, order_type="LIMIT",
                )
            state["pending"].pop(client_id, None)
            continue
        position = portfolio.positions.get(meta["symbol"])
        if order.executed_qty > 0 and position is not None:
            portfolio.sell(
                symbol=meta["symbol"],
                price=order.executed_price or float(meta.get("price") or position.entry_price),
                fee_rate=float(meta.get("fee") or 0),
                reason=meta.get("reason") or "resting order fill",
                client_id=client_id,
                order_type=order.order_type or meta.get("order_type") or "LIMIT",
                qty=min(order.executed_qty, position.qty),
            )
        age = time.time() - float(meta.get("created") or time.time())
        if not order.active or age > 600:
            if order.active:
                try:
                    client.cancel_order(client_id)
                except WallexError as exc:
                    log.warning("cancel %s failed: %s", client_id, exc)
            state["pending"].pop(client_id, None)


def _sync_paper(cfg, portfolio: Portfolio, state: dict, books: dict) -> None:
    """A resting maker quote fills only after the touch trades through it."""
    for client_id, meta in list(state.get("pending", {}).items()):
        book = books.get(meta.get("symbol"))
        age = time.time() - float(meta.get("created") or time.time())
        price = float(meta.get("price") or 0)
        filled = False
        if book and price > 0 and meta.get("side") == "BUY" and book.best_bid and book.best_bid < price:
            if meta["symbol"] not in portfolio.positions:
                portfolio.buy(
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
                portfolio.sell(
                    symbol=meta["symbol"], price=price, fee_rate=float(meta.get("fee") or 0),
                    reason=meta.get("reason") or "paper maker fill", client_id=client_id, order_type="LIMIT",
                    qty=float(raw_qty) if raw_qty else None,
                )
            filled = True
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
        if asset == "TMN" or balance.available <= 0:
            continue
        symbol = asset if asset.endswith("TMN") else f"{asset}TMN"
        seen.add(symbol)
        if symbol in portfolio.positions:
            portfolio.positions[symbol].qty = balance.available
            continue
        rule = rules.get(symbol)
        last = rule.last_price if rule else 0
        if last <= 0:
            continue
        portfolio.positions[symbol] = portfolio.positions.get(symbol) or _adopt(symbol, balance.available, last)
    for symbol in list(portfolio.positions):
        if symbol not in seen:
            log.info("reconcile dropped %s; exchange balance is zero", symbol)
            del portfolio.positions[symbol]
    try:
        open_orders = client.get_open_orders()
    except WallexError as exc:
        log.warning("open orders reconcile failed: %s", exc)
        open_orders = []
    state["pending"] = {
        order.client_order_id: {"symbol": order.symbol, "side": order.side, "status": order.status}
        for order in open_orders
    }
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


def _load_history(client, universe, cfg, previous: dict | None = None) -> tuple[dict, dict]:
    end = int(time.time())
    start = end - int(float(cfg["runtime"]["history_days"]) * 86400)
    micro_start = end - 36 * 3600
    hourly: dict[str, list[Bar]] = {}
    micro: dict[str, list[Bar]] = {}
    for symbol in universe:
        try:
            hourly[symbol] = client.get_ohlcv(symbol, "1h", start, end)
        except WallexError as exc:
            log.warning("hourly history %s failed: %s", symbol, exc)
            hourly[symbol] = (previous or {}).get(symbol) or []
        try:
            micro[symbol] = client.get_ohlcv(symbol, "15m", micro_start, end)
        except WallexError as exc:
            log.warning("15m history %s failed: %s", symbol, exc)
            micro[symbol] = []
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
    state["client_seq"] = int(state.get("client_seq") or 0) + 1
    return f"SC{int(time.time())}{state['client_seq']}"


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
