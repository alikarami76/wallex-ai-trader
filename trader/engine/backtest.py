"""Hourly backtest. The same signal, agent, and risk code as live mode."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from trader.engine.journal import Journal
from trader.engine.portfolio import Portfolio
from trader.engine.validation import deflated_sharpe
from trader.risk.manager import RiskManager, roll_day
from trader.strategy.agent import TradingAgent
from trader.strategy.indicators import atr_percent
from trader.strategy.quotes import half_spread, inventory_skew
from trader.sentiment import fear_greed_at
from trader.strategy.signals import build_candidates, evaluate_symbol
from trader.timeutil import TEHRAN, iso
from trader.types import Bar, MarketRule
from trader.wallex.precision import OrderRejected, round_price, round_qty

log = logging.getLogger("trader.backtest")


class _SilentJournal:
    def decision(self, *_args, **_kwargs) -> None:
        return None

    def trade(self, *_args, **_kwargs) -> None:
        return None

    def equity(self, *_args, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def run_backtest(
    cfg: dict,
    candles: dict[str, list[Bar]],
    rules: dict[str, MarketRule],
    unavailable: set[str],
    out_dir: Path | None,
    spreads: dict[str, float] | None = None,
    trade_after: int | None = None,
    trade_before: int | None = None,
    realize_open: bool = False,
    fear_greed: list[tuple[int, float, str]] | None = None,
) -> dict:
    spreads = spreads or {}
    symbols = [symbol for symbol in candles if symbol not in unavailable and symbol in rules]
    portfolio = Portfolio(float(cfg["account"]["initial_equity_tmn"]))
    risk = RiskManager(cfg)
    agent = TradingAgent(cfg, mcp=None)
    journal = Journal(out_dir) if out_dir is not None else _SilentJournal()
    equity_curve: list[float] = []
    state = {
        "peak_equity": portfolio.cash,
        "day_start_equity": portfolio.cash,
        "day_key": "",
        "kill_switch": False,
        "kill_reason": "",
        "cooldowns": {},
        "order_times": [],
        "client_seq": 1,
        "resting_entry": None,
        "resting_exit": None,
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
        _fill_resting(cfg, portfolio, indexed, stamp, rules, state, journal, spreads)
        _exits(cfg, portfolio, history, indexed, stamp, rules, state, journal, risk)
        features = []
        for symbol in symbols:
            if stamp not in indexed[symbol]:
                continue
            maker, taker = _fees(cfg, rules[symbol])
            fg, fg_change, _label = fear_greed_at(fear_greed, stamp)
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
                    fear_greed=fg,
                    fear_greed_change=fg_change,
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
        if trade_after is not None and stamp < trade_after:
            planned.actions = [item for item in planned.actions if item.side != "BUY"]
        if trade_before is not None and stamp >= trade_before:
            planned.actions = [item for item in planned.actions if item.side != "BUY"]
        fills = []
        for action in planned.actions:
            if action.side == "SELL":
                fill = _exit_action(cfg, portfolio, action, indexed, stamp, rules, state, history, spreads)
            else:
                fill = _enter(cfg, portfolio, action, indexed, stamp, rules, spreads, state, history)
            if fill:
                fills.append(fill)
                state["order_times"].append(stamp)
                journal.trade(_trade_row(fill, now, "backtest"))
                if fill.side == "SELL" and action.source == "stop":
                    state["cooldowns"][action.symbol] = stamp + float(cfg["risk"]["cooldown_after_stop_seconds"])
        equity = portfolio.equity(closes)
        in_window = (trade_after is None or stamp >= trade_after) and (trade_before is None or stamp < trade_before)
        if in_window:
            equity_curve.append(equity)
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

    if realize_open:
        _realize_open(cfg, portfolio, indexed, timeline[-1] if timeline else 0, rules, state, journal, spreads)
        if closes:
            equity_curve.append(portfolio.equity(closes))
    journal.close()
    ending = portfolio.equity(closes) if closes else portfolio.cash
    bar_returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev:
            bar_returns.append(equity_curve[i] / prev - 1)
    trials = int((cfg.get("validation") or {}).get("n_trials", 6))
    start_equity = float(cfg["account"]["initial_equity_tmn"])
    peak = start_equity
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak)
    summary = {
        "ending_equity": ending,
        "fees_paid": portfolio.fees_paid,
        "realized_pnl": portfolio.realized_pnl,
        "bars": len(timeline),
        "symbols": symbols,
        "return_pct": round((ending / start_equity - 1) * 100, 3) if start_equity else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 3),
        "closed_trades": portfolio.closed_trades,
        "deflated_sharpe": deflated_sharpe(bar_returns, trials),
    }
    log.info("backtest finished: %s", {k: summary[k] for k in ("ending_equity", "fees_paid", "realized_pnl", "bars")})
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


def _fill_resting(cfg, portfolio, indexed, stamp, rules, state, journal, spreads) -> None:
    """Maker quotes live for one bar. A buy fills only if that bar trades down to it."""
    entry = state.get("resting_entry")
    if entry and entry["signal_time"] < stamp:
        state["resting_entry"] = None
        bar = indexed.get(entry["symbol"], {}).get(stamp)
        if bar is not None and bar.low <= entry["price"] and entry["symbol"] not in portfolio.positions:
            fill = portfolio.buy(
                symbol=entry["symbol"],
                price=entry["price"],
                qty=entry["qty"],
                fee_rate=entry["fee"],
                time=stamp,
                stop_price=entry["price"] * (1 - float(cfg["risk"]["stop_loss_pct"])),
                setup="uptrend_pullback",
                reason=entry["reason"],
                client_id=_cid(state),
                order_type="LIMIT",
            )
            if fill:
                _record_fill(journal, fill, stamp, state)
    exit_order = state.get("resting_exit")
    if exit_order and exit_order["signal_time"] < stamp:
        state["resting_exit"] = None
        if exit_order["symbol"] not in portfolio.positions:
            return
        bar = indexed.get(exit_order["symbol"], {}).get(stamp)
        rule = rules.get(exit_order["symbol"])
        if bar is None or rule is None:
            return
        position = portfolio.positions[exit_order["symbol"]]
        if bar.low <= position.stop_price:
            return
        maker, taker = _fees(cfg, rule)
        slip = float(cfg["fees"]["slippage_bps"]) / 10_000
        if bar.high >= exit_order["price"]:
            price, fee, order_type = exit_order["price"], maker, "LIMIT"
            reason = exit_order["reason"]
        else:
            price = bar.close * (1 - slip)
            fee, order_type = taker, "MARKET"
            reason = exit_order["reason"] + "; maker quote missed, crossed the spread"
        fill = portfolio.sell(
            symbol=exit_order["symbol"],
            price=price,
            fee_rate=fee,
            reason=reason,
            client_id=_cid(state),
            order_type=order_type,
        )
        if fill:
            _record_fill(journal, fill, stamp, state)


def _enter(cfg, portfolio, action, indexed, stamp, rules, spreads, state, history):
    bar = indexed[action.symbol].get(stamp)
    rule = rules.get(action.symbol)
    if bar is None or rule is None:
        return None
    spread = _spread(action.symbol, rules, spreads, cfg) or 0.0
    maker_entry = cfg["execution"]["entry"] != "marketable_limit"
    skew = _skew(cfg, portfolio, indexed, stamp, history, action.symbol)
    if maker_entry:
        raw_price = bar.close * (1 - half_spread(spread)) * (1 - skew)
        direction = "down"
    else:
        raw_price = bar.close * (1 + half_spread(spread))
        direction = "up"
    try:
        price = float(round_price(raw_price, rule.price_decimals, direction))
        notional = portfolio.equity(_last_closes(indexed, stamp, portfolio)) * action.size_fraction
        if notional < rule.min_notional:
            return None
        qty = float(round_qty(notional / price, rule.qty_decimals))
        if qty < rule.min_qty or qty * price < rule.min_notional:
            return None
    except (OrderRejected, ZeroDivisionError):
        return None
    maker, taker = _fees(cfg, rule)
    fee = maker if maker_entry else taker
    if maker_entry:
        state["resting_entry"] = {
            "symbol": action.symbol,
            "price": price,
            "qty": qty,
            "fee": fee,
            "reason": action.reason,
            "signal_time": stamp,
        }
        return None
    return portfolio.buy(
        symbol=action.symbol,
        price=price,
        qty=qty,
        fee_rate=fee,
        time=stamp,
        stop_price=price * (1 - float(cfg["risk"]["stop_loss_pct"])),
        setup="uptrend_pullback",
        reason=action.reason,
        client_id=_cid(state),
        order_type="LIMIT",
    )


def _exit_action(cfg, portfolio, action, indexed, stamp, rules, state, history, spreads):
    """Protective sells cross. A time exit rests one bar as a maker quote."""
    position = portfolio.positions.get(action.symbol)
    bar = indexed.get(action.symbol, {}).get(stamp)
    rule = rules.get(action.symbol)
    if position is None or bar is None or rule is None:
        return None
    maker, taker = _fees(cfg, rule)
    slip = float(cfg["fees"]["slippage_bps"]) / 10_000
    protective = action.source in {"kill", "liquidation", "stop", "trail"} or action.aggressive
    if protective:
        return portfolio.sell(
            symbol=action.symbol,
            price=bar.close * (1 - slip),
            fee_rate=taker,
            reason=action.reason,
            client_id=_cid(state),
            order_type="MARKET",
        )
    spread = _spread(action.symbol, rules, spreads, cfg) or 0.0
    skew = _skew(cfg, portfolio, indexed, stamp, history, action.symbol)
    ask = bar.close * (1 + half_spread(spread))
    bid = bar.close * (1 - half_spread(spread))
    raw = ask * (1 - skew)
    if raw <= bid:
        raw = ask
    try:
        price = float(round_price(raw, rule.price_decimals, "up"))
    except (OrderRejected, ZeroDivisionError):
        price = raw
    state["resting_exit"] = {
        "symbol": action.symbol,
        "price": price,
        "reason": action.reason,
        "signal_time": stamp,
    }
    return None


def _skew(cfg, portfolio, indexed, stamp, history, symbol) -> float:
    marks = _last_closes(indexed, stamp, portfolio)
    equity = portfolio.equity(marks)
    inventory = portfolio.exposure(marks) if equity else 0.0
    sigma = _atr(history.get(symbol) or [], cfg)
    gamma = float((cfg.get("execution") or {}).get("quote_gamma", 0.5))
    tau = float(cfg["risk"]["time_stop_hours"])
    return inventory_skew(inventory, sigma, gamma, tau)


def _atr(bars: list[Bar], cfg: dict) -> float:
    period = int((cfg.get("strategy") or {}).get("atr_period", 14))
    if len(bars) <= period:
        return 0.01
    value = atr_percent([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], period)[-1]
    return float(value) if value else 0.01


def _record_fill(journal, fill, stamp, state) -> None:
    state["order_times"].append(stamp)
    now = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(TEHRAN)
    journal.trade(_trade_row(fill, now, "backtest"))
    journal.decision({
        "timestamp": iso(now),
        "mode": "backtest",
        "event": "resting_fill",
        "fill": fill.__dict__,
    })


def _realize_open(cfg, portfolio, indexed, stamp, rules, state, journal, spreads) -> None:
    """Close whatever a walk-forward fold still holds, as a taker, so the fold PnL is realized."""
    slip = float(cfg["fees"]["slippage_bps"]) / 10_000
    for symbol in list(portfolio.positions):
        bar = indexed.get(symbol, {}).get(stamp)
        rule = rules.get(symbol)
        if bar is None or rule is None:
            continue
        _maker, taker = _fees(cfg, rule)
        spread = _spread(symbol, rules, spreads, cfg) or 0.0
        fill = portfolio.sell(
            symbol=symbol,
            price=bar.close * (1 - half_spread(spread) - slip),
            fee_rate=taker,
            reason="walk-forward fold flatten",
            client_id=_cid(state),
            order_type="MARKET",
        )
        if fill:
            _record_fill(journal, fill, stamp, state)


def run_walk_forward(cfg, candles, rules, unavailable, spreads=None, fear_greed=None) -> dict:
    """Three sequential folds, same frozen parameters, fresh cash each fold."""
    timeline = sorted({bar.time for bars in candles.values() for bar in bars})
    validation = cfg.get("validation") or {}
    warmup = int(validation.get("warmup_bars", 80))
    folds = int(validation.get("folds", 3))
    if len(timeline) <= warmup + folds:
        return {"ok": False, "reason": "not enough bars for a walk-forward"}
    tradable = timeline[warmup:]
    fold_size = max(1, len(tradable) // folds)
    rows = []
    for index in range(folds):
        start = tradable[index * fold_size]
        end = tradable[(index + 1) * fold_size] if index < folds - 1 else tradable[-1] + 1
        summary = run_backtest(
            cfg, candles, rules, unavailable, None,
            spreads=spreads, trade_after=start, trade_before=end, realize_open=True,
            fear_greed=fear_greed,
        )
        start_equity = float(cfg["account"]["initial_equity_tmn"])
        rows.append({
            "fold": index + 1,
            "trade_after": start,
            "trade_before": end,
            "ending_equity": round(summary["ending_equity"], 2),
            "return_pct": round((summary["ending_equity"] / start_equity - 1) * 100, 3),
            "fees_paid": round(summary["fees_paid"], 2),
            "realized_pnl": round(summary["realized_pnl"], 2),
            "deflated_sharpe_probability": (summary.get("deflated_sharpe") or {}).get("deflated_sharpe_probability"),
        })
    return {"ok": True, "folds": folds, "warmup_bars": warmup, "folds_result": rows}


def sentiment_ablation(cfg, candles, rules, unavailable, fear_greed) -> dict:
    """One pre-specified comparison: block entries when Fear & Greed is at least 80.

    The gate helps only when it raises return by 0.05 percentage points, does
    not worsen max drawdown by more than 0.10 points, and still closes at
    least 8 trades. That rule was fixed before reading the result.
    """
    import copy

    without = copy.deepcopy(cfg)
    with_gate = copy.deepcopy(cfg)
    without.setdefault("sentiment", {})["block_extreme_greed"] = False
    with_gate.setdefault("sentiment", {})["block_extreme_greed"] = True
    with_gate["sentiment"]["extreme_greed"] = 80
    off = run_backtest(without, candles, rules, unavailable, None, fear_greed=fear_greed)
    on = run_backtest(with_gate, candles, rules, unavailable, None, fear_greed=fear_greed)
    helps = (
        on["return_pct"] >= off["return_pct"] + 0.05
        and on["max_drawdown_pct"] <= off["max_drawdown_pct"] + 0.10
        and on["closed_trades"] >= 8
    )
    def pack(summary):
        return {
            "ending_equity": round(summary["ending_equity"], 2),
            "return_pct": summary["return_pct"],
            "max_drawdown_pct": summary["max_drawdown_pct"],
            "realized_pnl": round(summary["realized_pnl"], 2),
            "fees_paid": round(summary["fees_paid"], 2),
            "closed_trades": summary["closed_trades"],
        }
    return {
        "hypothesis": "block new entries when lagged Fear & Greed is >= 80",
        "without_gate": pack(off),
        "with_gate": pack(on),
        "gate_helps": helps,
        "decision_rule": "keep the gate only if return rises by >= 0.05pp, drawdown is not worse by > 0.10pp, and closed trades stay >= 8",
    }


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
