"""Hourly backtest. The same signal, agent, and risk code as live mode."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from trader.engine.journal import Journal
from trader.engine.portfolio import Portfolio
from trader.risk.manager import RiskManager, roll_day
from trader.strategy.agent import TradingAgent
from trader.strategy.signals import build_candidates, evaluate_symbol
from trader.timeutil import TEHRAN, iso
from trader.types import Bar, MarketRule
from trader.wallex.precision import OrderRejected, round_price, round_qty

log = logging.getLogger("trader.backtest")


def run_backtest(
    cfg: dict,
    candles: dict[str, list[Bar]],
    rules: dict[str, MarketRule],
    unavailable: set[str],
    out_dir: Path,
    spreads: dict[str, float] | None = None,
) -> dict:
    spreads = spreads or {}
    symbols = [symbol for symbol in candles if symbol not in unavailable and symbol in rules]
    portfolio = Portfolio(float(cfg["account"]["initial_equity_tmn"]))
    risk = RiskManager(cfg)
    agent = TradingAgent(cfg, mcp=None)
    journal = Journal(out_dir)
    state = {
        "peak_equity": portfolio.cash,
        "day_start_equity": portfolio.cash,
        "day_key": "",
        "kill_switch": False,
        "kill_reason": "",
        "cooldowns": {},
        "order_times": [],
        "client_seq": 1,
    }
    history: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    indexed = {symbol: {bar.time: bar for bar in candles[symbol]} for symbol in symbols}
    timeline = sorted({bar.time for bars in candles.values() for bar in bars})
    closes: dict[str, float] = {}

    for stamp in timeline:
        for symbol in symbols:
            bar = indexed[symbol].get(stamp)
            if bar is not None:
                history[symbol].append(bar)
                closes[symbol] = bar.close
        if not closes:
            continue
        now = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(TEHRAN)
        state["day_key"], state["day_start_equity"] = roll_day(
            state["day_key"], now, portfolio.equity(closes)
        )
        _exits(cfg, portfolio, history, indexed, stamp, rules, state, journal, risk)
        features = []
        for symbol in symbols:
            if stamp not in indexed[symbol]:
                continue
            maker, taker = _fees(cfg, rules[symbol])
            features.append(
                evaluate_symbol(
                    symbol,
                    history[symbol],
                    cfg,
                    spread_bps=_spread(symbol, rules, spreads, cfg),
                    imbalance=None,
                    micro_return=None,
                    maker_fee=maker,
                    taker_fee=taker,
                )
            )
        candidates = build_candidates(features, cfg)
        view = risk.view(
            portfolio,
            closes,
            now,
            peak_equity=state["peak_equity"],
            day_start_equity=state["day_start_equity"],
            kill_switch=state["kill_switch"],
            kill_reason=state["kill_reason"],
        )
        state["peak_equity"] = view.peak_equity
        state["kill_switch"] = view.kill_switch
        state["kill_reason"] = view.kill_reason
        decision = agent.decide(
            features,
            candidates,
            [p.to_dict() for p in portfolio.positions.values()],
            view.compact(),
            stamp,
            use_llm=False,
        )
        planned = risk.plan(
            portfolio=portfolio,
            agent=decision,
            candidates=candidates,
            marks=closes,
            rules=rules,
            unavailable=unavailable,
            now=now,
            view=view,
            order_times=state["order_times"],
            cooldowns=state["cooldowns"],
            now_unix=stamp,
        )
        fills = []
        for action in planned.actions:
            if action.side == "SELL":
                fill = _exit_action(cfg, portfolio, action, indexed, stamp, rules, state)
            else:
                fill = _enter(cfg, portfolio, action, indexed, stamp, rules, spreads, state)
            if fill:
                fills.append(fill)
                state["order_times"].append(stamp)
                journal.trade(_trade_row(fill, now, "backtest"))
                if fill.side == "SELL" and action.source == "stop":
                    state["cooldowns"][action.symbol] = stamp + float(cfg["risk"]["cooldown_after_stop_seconds"])
        equity = portfolio.equity(closes)
        state["peak_equity"] = max(state["peak_equity"], equity)
        journal.equity({
            "timestamp": iso(now),
            "equity_tmn": round(equity, 2),
            "cash_tmn": round(portfolio.cash, 2),
            "exposure": round(portfolio.exposure(closes), 4),
            "drawdown": round(planned.view.drawdown, 5),
            "peak_tmn": round(state["peak_equity"], 2),
        })
        journal.decision({
            "timestamp": iso(now),
            "mode": "backtest",
            "equity_tmn": round(equity, 2),
            "phase": planned.view.phase,
            "features": [row.compact() for row in features],
            "candidates": [row.compact() for row in candidates],
            "agent": decision.as_log(),
            "risk": {
                "rejected": planned.rejected,
                "actions": [a.__dict__ for a in planned.actions],
                "view": planned.view.compact(),
            },
            "fills": [f.__dict__ for f in fills],
        })

    journal.close()
    summary = {
        "ending_equity": portfolio.equity(closes) if closes else portfolio.cash,
        "fees_paid": portfolio.fees_paid,
        "realized_pnl": portfolio.realized_pnl,
        "bars": len(timeline),
        "symbols": symbols,
    }
    log.info("backtest finished: %s", summary)
    return summary


def _exits(cfg, portfolio, history, indexed, stamp, rules, state, journal, risk) -> None:
    for symbol, position in list(portfolio.positions.items()):
        bar = indexed[symbol].get(stamp)
        if bar is None or bar.time <= position.entry_time:
            continue
        maker, taker = _fees(cfg, rules[symbol])
        stop_slip = float(cfg["execution"]["stop_slippage_bps"]) / 10_000
        fill = None
        reason = ""
        order_type = "LIMIT"
        if bar.low <= position.stop_price:
            price = position.stop_price * (1 - stop_slip)
            fill = portfolio.sell(
                symbol=symbol, price=price, fee_rate=taker, reason="stop",
                client_id=_cid(state), order_type="MARKET",
            )
            reason = "stop"
            order_type = "MARKET"
            state["cooldowns"][symbol] = stamp + float(cfg["risk"]["cooldown_after_stop_seconds"])
        else:
            if bar.high > position.peak_price:
                position.peak_price = bar.high
            trail_arm = position.entry_price * (1 + float(cfg["risk"]["trail_arm_pct"]))
            trail_px = position.peak_price * (1 - float(cfg["risk"]["trail_distance_pct"]))
            if position.peak_price >= trail_arm and bar.low <= trail_px:
                fill = portfolio.sell(
                    symbol=symbol, price=trail_px * (1 - stop_slip), fee_rate=taker,
                    reason="trailing stop", client_id=_cid(state), order_type="MARKET",
                )
                reason = "trail"
                order_type = "MARKET"
        if fill:
            state["order_times"].append(stamp)
            now = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(TEHRAN)
            journal.trade(_trade_row(fill, now, "backtest"))
            journal.decision({
                "timestamp": iso(now),
                "mode": "backtest",
                "event": "exit",
                "reason": reason,
                "order_type": order_type,
                "fill": fill.__dict__,
            })


def _enter(cfg, portfolio, action, indexed, stamp, rules, spreads, state):
    bar = indexed[action.symbol].get(stamp)
    rule = rules.get(action.symbol)
    if bar is None or rule is None:
        return None
    spread = _spread(action.symbol, rules, spreads, cfg) or 0.0
    raw_price = bar.close * (1 + spread / 2 / 10_000)
    try:
        price = float(round_price(raw_price, rule.price_decimals, "up"))
        notional = portfolio.equity(_last_closes(indexed, stamp, portfolio)) * action.size_fraction
        if notional < rule.min_notional:
            return None
        qty = float(round_qty(notional / price, rule.qty_decimals))
        if qty < rule.min_qty or qty * price < rule.min_notional:
            return None
    except (OrderRejected, ZeroDivisionError):
        return None
    maker, taker = _fees(cfg, rule)
    fee = taker if cfg["execution"]["entry"] == "marketable_limit" else maker
    stop = price * (1 - float(cfg["risk"]["stop_loss_pct"]))
    return portfolio.buy(
        symbol=action.symbol,
        price=price,
        qty=qty,
        fee_rate=fee,
        time=stamp,
        stop_price=stop,
        setup="uptrend_pullback",
        reason=action.reason,
        client_id=_cid(state),
        order_type="LIMIT",
    )


def _exit_action(cfg, portfolio, action, indexed, stamp, rules, state):
    """Time-stop, kill-switch, and liquidation sells. Intrabar stops are handled earlier."""
    position = portfolio.positions.get(action.symbol)
    bar = indexed.get(action.symbol, {}).get(stamp)
    rule = rules.get(action.symbol)
    if position is None or bar is None or rule is None:
        return None
    maker, taker = _fees(cfg, rule)
    slip = float(cfg["fees"]["slippage_bps"]) / 10_000
    protective = action.source in {"kill", "liquidation", "stop", "trail"} or action.aggressive
    price = bar.close * (1 - slip)
    fee = taker if protective else maker
    return portfolio.sell(
        symbol=action.symbol,
        price=price,
        fee_rate=fee,
        reason=action.reason,
        client_id=_cid(state),
        order_type="MARKET" if protective else "LIMIT",
    )


def _last_closes(indexed, stamp, portfolio) -> dict[str, float]:
    marks = {}
    for symbol, bars in indexed.items():
        # bars is a dict
        if stamp in bars:
            marks[symbol] = bars[stamp].close
    for symbol, position in portfolio.positions.items():
        marks.setdefault(symbol, position.entry_price)
    return marks


def _spread(symbol, rules, spreads, cfg) -> float | None:
    if symbol in spreads:
        return float(spreads[symbol])
    snapshot = (cfg.get("universe") or {}).get("spread_bps_snapshot") or {}
    if symbol in snapshot:
        return float(snapshot[symbol])
    rule = rules.get(symbol)
    if rule and rule.spread_bps is not None:
        return rule.spread_bps
    return None


def _fees(cfg, rule: MarketRule) -> tuple[float, float]:
    if rule.is_zero_fee:
        return 0.0, 0.0
    return float(cfg["fees"]["maker_rate"]), float(cfg["fees"]["taker_rate"])


def _cid(state) -> str:
    state["client_seq"] += 1
    return f"SCBT{state['client_seq']}"


def _trade_row(fill, now, mode) -> dict:
    return {
        "timestamp": iso(now),
        "mode": mode,
        "symbol": fill.symbol,
        "side": fill.side,
        "price": fill.price,
        "quantity": fill.qty,
        "notional_tmn": round(fill.notional_tmn, 2),
        "fee_tmn": round(fill.fee_tmn, 2),
        "fee_rate": fill.fee_rate,
        "pnl_tmn": "" if fill.pnl_tmn is None else round(fill.pnl_tmn, 2),
        "reason": fill.reason,
        "order_type": fill.order_type,
        "client_id": fill.client_id,
    }
