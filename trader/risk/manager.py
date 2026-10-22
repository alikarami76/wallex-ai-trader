"""Hard risk layer.

Stops, the drawdown kill switch, exposure caps, the fee gate's size, and the
liquidation window are applied after the model speaks. A BUY the model
invented, or a size above the quant proposal, is cut here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.engine.portfolio import Portfolio
from trader.engine.universe import symbol_block
from trader.strategy.liquidity import Liquidity, liquidity_settings
from trader.strategy.menu import MenuAction, hold_action
from trader.strategy.parser import AgentDecision, ParsedDecision
from trader.strategy.quotes import fractional_kelly
from trader.strategy.signals import Candidate
from trader.timeutil import day_key, to_tehran
from trader.types import MarketRule


@dataclass
class RiskView:
    equity: float
    peak_equity: float
    day_start_equity: float
    drawdown: float
    daily_drawdown: float
    gross_exposure: float
    phase: str
    entries_blocked: bool
    block_reason: str
    kill_switch: bool
    kill_reason: str

    def compact(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown": round(self.drawdown, 5),
            "daily_drawdown": round(self.daily_drawdown, 5),
            "gross_exposure": round(self.gross_exposure, 4),
            "phase": self.phase,
            "entries_blocked": self.entries_blocked,
            "block_reason": self.block_reason,
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
        }


@dataclass
class Action:
    symbol: str
    side: str
    order_type: str
    size_fraction: float
    reason: str
    source: str
    price_direction: str
    aggressive: bool = False
    client_order_id: str = ""


@dataclass
class RiskResult:
    view: RiskView
    actions: list[Action] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


def liquidation_phase(now: datetime, cfg: dict) -> str:
    from trader.timeutil import parse_ts

    deadline = parse_ts(cfg["liquidation"]["deadline"])
    start = deadline - timedelta(minutes=float(cfg["liquidation"]["start_before_minutes"]))
    aggressive = deadline - timedelta(minutes=float(cfg["liquidation"]["aggressive_before_minutes"]))
    current = to_tehran(now)
    if current < start:
        return "normal"
    if current < aggressive:
        return "passive"
    return "aggressive"


def hours_until_flat(now: datetime, cfg: dict) -> float:
    from trader.timeutil import parse_ts

    flat_by = parse_ts(cfg["liquidation"]["flat_by"])
    return (flat_by - to_tehran(now)).total_seconds() / 3600


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def view(
        self,
        portfolio: Portfolio,
        marks: dict[str, float],
        now: datetime,
        *,
        peak_equity: float,
        day_start_equity: float,
        kill_switch: bool,
        kill_reason: str,
    ) -> RiskView:
        equity = portfolio.equity(marks)
        peak = max(peak_equity, equity)
        day_start = day_start_equity or equity
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        daily = (day_start - equity) / day_start if day_start > 0 else 0.0
        exposure = portfolio.exposure(marks)
        phase = liquidation_phase(now, self.cfg)
        risk = self.cfg["risk"]
        blocked = False
        reason = ""
        new_kill = kill_switch
        new_reason = kill_reason
        if daily >= float(risk["daily_max_drawdown"]):
            blocked = True
            new_kill = True
            new_reason = f"daily max-loss {daily:.2%} hit the kill switch"
            reason = new_reason
        elif drawdown >= float(risk["global_max_drawdown"]):
            blocked = True
            new_kill = True
            new_reason = f"drawdown {drawdown:.2%} hit the global kill switch"
            reason = new_reason
        elif kill_switch:
            blocked = True
            reason = kill_reason or "kill switch is on"
        elif phase != "normal":
            blocked = True
            reason = f"liquidation phase {phase}"
        elif hours_until_flat(now, self.cfg) < float(self.cfg["liquidation"]["min_hours_to_enter"]):
            blocked = True
            reason = "not enough time left to hold the setup before flatten"
        return RiskView(
            equity=equity,
            peak_equity=peak,
            day_start_equity=day_start,
            drawdown=max(0.0, drawdown),
            daily_drawdown=max(0.0, daily),
            gross_exposure=exposure,
            phase=phase,
            entries_blocked=blocked,
            block_reason=reason,
            kill_switch=new_kill,
            kill_reason=new_reason,
        )

    def plan(
        self,
        *,
        portfolio: Portfolio,
        agent: AgentDecision,
        candidates: list[Candidate],
        marks: dict[str, float],
        rules: dict[str, MarketRule],
        unavailable: set[str],
        now: datetime,
        view: RiskView,
        order_times: list[float],
        cooldowns: dict[str, float],
        now_unix: float,
        liquidity: dict[str, Liquidity] | None = None,
        features: list | None = None,
    ) -> RiskResult:
        actions: list[Action] = []
        rejected: list[dict] = []
        risk = self.cfg["risk"]
        held = set(portfolio.positions)

        for symbol, position in list(portfolio.positions.items()):
            price = marks.get(symbol, position.entry_price)
            forced = self._forced_exit(symbol, position, price, now_unix, view)
            if not forced:
                continue
            if self._defer_exit(forced, position, view, liquidity, now_unix):
                rejected.append({"symbol": symbol, "reason": "exit deferred until the book is liquid"})
                continue
            actions.append(forced)

        forced_symbols = {action.symbol for action in actions}
        if view.phase != "normal" or view.kill_switch:
            for symbol in held:
                if symbol in forced_symbols:
                    continue
                flatten = self._flatten_action(symbol, view)
                position = portfolio.positions.get(symbol)
                if position is not None and self._defer_exit(flatten, position, view, liquidity, now_unix):
                    rejected.append({"symbol": symbol, "reason": "liquidation deferred until the book is liquid"})
                    continue
                actions.append(flatten)

        candidate_by_symbol = {item.symbol: item for item in candidates}
        entries_this_hour = _count_since(order_times, now_unix, 3600)
        entry_cap = int(risk["max_entries_per_hour"])
        order_cap = int(risk["max_orders_per_hour"])

        hostile = (agent.regime or "").lower() == "hostile"
        if agent.menu_bound:
            for decision in agent.decisions:
                if decision.action != "CANCEL":
                    continue
                if view.phase != "normal" or view.kill_switch or not decision.client_order_id:
                    rejected.append({"symbol": decision.symbol or "*", "reason": "cancel vetoed by the risk layer"})
                    continue
                actions.append(
                    Action(
                        symbol=decision.symbol,
                        side="CANCEL",
                        order_type="LIMIT",
                        size_fraction=0.0,
                        reason=decision.rationale or "cancel a resting maker order",
                        source="cancel",
                        price_direction="down",
                        client_order_id=decision.client_order_id,
                    )
                )
        if view.phase == "normal" and not view.kill_switch:
            for decision in agent.decisions:
                if decision.action in {"SELL", "CLOSE"} and not decision.veto:
                    if decision.symbol in held and decision.symbol not in forced_symbols:
                        if agent.menu_bound and features is not None and not self._close_condition(decision.symbol, features):
                            rejected.append({
                                "symbol": decision.symbol,
                                "reason": "close vetoed; the quant exit condition is gone",
                            })
                            continue
                        fraction = 1.0
                        if agent.menu_bound and decision.size_fraction is not None:
                            fraction = min(1.0, max(0.0, float(decision.size_fraction)))
                        if fraction <= 0:
                            rejected.append({"symbol": decision.symbol, "reason": "reduce size is zero"})
                            continue
                        actions.append(
                            Action(
                                symbol=decision.symbol,
                                side="SELL",
                                order_type="LIMIT",
                                size_fraction=fraction,
                                reason=decision.rationale or "model requested an early exit",
                                source="agent_reduce" if fraction < 0.999 else "agent_close",
                                price_direction="up",
                            )
                        )
                    else:
                        rejected.append({"symbol": decision.symbol, "reason": "close ignored; no position"})
            if agent.veto_all or hostile:
                rejected.append({"symbol": "*", "reason": "model vetoed the regime"})
            elif agent.menu_bound:
                for decision in agent.decisions:
                    if decision.action != "BUY" or decision.veto:
                        continue
                    self._append_buy(
                        decision, actions, rejected, candidate_by_symbol, portfolio, view,
                        unavailable, rules, cooldowns, now_unix, entries_this_hour,
                        order_cap, entry_cap, liquidity, agent.source, menu_size=True,
                    )
                    if any(item.side == "BUY" for item in actions):
                        break
            else:
                for decision in _entry_decisions(agent, candidates):
                    self._append_buy(
                        decision, actions, rejected, candidate_by_symbol, portfolio, view,
                        unavailable, rules, cooldowns, now_unix, entries_this_hour,
                        order_cap, entry_cap, liquidity, agent.source, menu_size=False,
                    )
                    if any(item.side == "BUY" for item in actions):
                        break

        return RiskResult(view=view, actions=actions, rejected=rejected)

    def legal_menu(
        self,
        *,
        portfolio: Portfolio,
        candidates: list[Candidate],
        marks: dict[str, float],
        rules: dict[str, MarketRule],
        unavailable: set[str],
        now: datetime,
        view: RiskView,
        order_times: list[float],
        cooldowns: dict[str, float],
        now_unix: float,
        liquidity: dict[str, Liquidity] | None = None,
        features: list | None = None,
        pending: dict | None = None,
    ) -> list[MenuAction]:
        """Actions the model is allowed to pick. Stops and liquidation stay off the list.

        A kill switch or a liquidation phase leaves only HOLD. The flatten
        itself is applied by plan() and is not something the model can vote down.
        """
        menu = [hold_action()]
        if view.phase != "normal" or view.kill_switch:
            return menu
        risk = self.cfg["risk"]
        entries_this_hour = _count_since(order_times, now_unix, 3600)
        entry_cap = int(risk["max_entries_per_hour"])
        order_cap = int(risk["max_orders_per_hour"])
        for candidate in candidates:
            probe = ParsedDecision(
                action="BUY",
                symbol=candidate.symbol,
                size_fraction=None,
                confidence=0.7,
                veto=False,
                rationale="quant candidate cleared the cost gate",
            )
            if self._reject_buy(
                probe, {candidate.symbol: candidate}, portfolio, view, unavailable, rules,
                cooldowns, now_unix, entries_this_hour, order_cap, entry_cap,
            ):
                continue
            size, _why = self._sized_entry(probe, candidate, view, liquidity)
            if size is None:
                continue
            menu.append(
                MenuAction(
                    id=f"buy_{candidate.symbol}",
                    kind="BUY",
                    symbol=candidate.symbol,
                    size_fraction=size,
                    summary=f"BUY {candidate.symbol} at the maker bid, size {size:.4f} of equity",
                )
            )
        for symbol, position in portfolio.positions.items():
            price = marks.get(symbol, position.entry_price)
            if self._forced_exit(symbol, position, price, now_unix, view):
                continue
            if not self._close_condition(symbol, features):
                continue
            menu.append(
                MenuAction(
                    id=f"sell_{symbol}",
                    kind="SELL",
                    symbol=symbol,
                    size_fraction=1.0,
                    summary=f"SELL {symbol}, the full position, at the maker offer",
                )
            )
            menu.append(
                MenuAction(
                    id=f"reduce_{symbol}",
                    kind="REDUCE",
                    symbol=symbol,
                    size_fraction=0.5,
                    summary=f"REDUCE {symbol} by half at the maker offer",
                )
            )
        for client_id, meta in (pending or {}).items():
            if not isinstance(meta, dict) or meta.get("side") not in {"BUY", "SELL"}:
                continue
            symbol = str(meta.get("symbol") or "")
            menu.append(
                MenuAction(
                    id=f"cancel_{client_id}",
                    kind="CANCEL",
                    symbol=symbol,
                    summary=f"CANCEL order {client_id}",
                    client_order_id=str(client_id),
                )
            )
        return menu

    def _append_buy(
        self,
        decision,
        actions,
        rejected,
        candidate_by_symbol,
        portfolio,
        view,
        unavailable,
        rules,
        cooldowns,
        now_unix,
        entries_this_hour,
        order_cap,
        entry_cap,
        liquidity,
        source,
        menu_size: bool,
    ) -> None:
        rejection = self._reject_buy(
            decision,
            candidate_by_symbol,
            portfolio,
            view,
            unavailable,
            rules,
            cooldowns,
            now_unix,
            entries_this_hour,
            order_cap,
            entry_cap,
        )
        if rejection:
            rejected.append(rejection)
            return
        candidate = candidate_by_symbol[decision.symbol]
        fresh, why = self._sized_entry(
            ParsedDecision(
                action="BUY",
                symbol=decision.symbol,
                size_fraction=None if menu_size else decision.size_fraction,
                confidence=decision.confidence,
                veto=False,
                rationale=decision.rationale,
            ),
            candidate,
            view,
            liquidity,
        )
        if fresh is None:
            rejected.append({"symbol": decision.symbol, "reason": why})
            return
        size = fresh
        if menu_size:
            # The menu size is already post-Kelly and post-liquidity. Take the
            # smaller of that quote and a fresh check. Do not scale twice.
            offered = decision.size_fraction if decision.size_fraction is not None else fresh
            size = min(float(offered), fresh)
        if size <= 0:
            rejected.append({"symbol": decision.symbol, "reason": "no exposure room"})
            return
        if size * view.equity < float(self.cfg["risk"]["min_notional_tmn"]):
            rejected.append({
                "symbol": decision.symbol,
                "reason": "size after the liquidity cap is below the minimum order",
            })
            return
        actions.append(
            Action(
                symbol=decision.symbol,
                side="BUY",
                order_type="LIMIT",
                size_fraction=size,
                reason=decision.rationale or candidate.setup,
                source=source,
                price_direction="down",
                aggressive=False,
            )
        )

    def _sized_entry(self, decision, candidate, view, liquidity) -> tuple[float | None, str]:
        """Post-cap, post-Kelly, post-liquidity fraction. None when it cannot trade."""
        risk = self.cfg["risk"]
        size = decision.size_fraction if decision.size_fraction is not None else candidate.size_fraction
        kelly = fractional_kelly(self.cfg)
        per_symbol = float(risk.get("max_fraction_per_symbol", risk["max_fraction_per_trade"]))
        size = min(size, candidate.size_fraction, float(risk["max_fraction_per_trade"]), per_symbol)
        if kelly is not None:
            size = min(size, kelly)
        room = float(risk["max_gross_exposure"]) - view.gross_exposure
        size = min(size, room)
        size = self._liquidity_size(size, decision.symbol, view.equity, liquidity)
        if size <= 0:
            return None, "no exposure room"
        if size * view.equity < float(risk["min_notional_tmn"]):
            return None, "size after the liquidity cap is below the minimum order"
        return size, ""

    def _close_condition(self, symbol: str, features: list | None) -> bool:
        """Early exit the quant layer already allowed: close under the slow EMA and a negative 24h return."""
        if not features:
            return False
        row = next((item for item in features if getattr(item, "symbol", None) == symbol), None)
        if row is None or row.ema_slow is None or row.roc_trend is None:
            return False
        return float(row.close) < float(row.ema_slow) and float(row.roc_trend) < 0

    def _forced_exit(self, symbol, position, price, now_unix, view) -> Action | None:
        risk = self.cfg["risk"]
        if price <= position.stop_price:
            return Action(
                symbol=symbol,
                side="SELL",
                order_type="MARKET",
                size_fraction=1.0,
                reason=f"stop {position.stop_price:.8g} hit at {price:.8g}",
                source="stop",
                price_direction="down",
                aggressive=True,
            )
        if position.peak_price >= position.entry_price * (1 + float(risk["trail_arm_pct"])):
            trail = position.peak_price * (1 - float(risk["trail_distance_pct"]))
            if price <= trail:
                return Action(
                    symbol=symbol,
                    side="SELL",
                    order_type="LIMIT",
                    size_fraction=1.0,
                    reason=f"trailing stop {trail:.8g} hit from peak {position.peak_price:.8g}",
                    source="trail",
                    price_direction="down",
                    aggressive=True,
                )
        held_hours = (now_unix - position.entry_time) / 3600
        if held_hours >= float(risk["time_stop_hours"]):
            return Action(
                symbol=symbol,
                side="SELL",
                order_type="LIMIT",
                size_fraction=1.0,
                reason=f"time stop after {held_hours:.1f}h",
                source="time",
                price_direction="up",
            )
        return None

    def _flatten_action(self, symbol: str, view: RiskView) -> Action:
        aggressive = view.phase == "aggressive" or view.kill_switch
        if view.kill_switch:
            reason = view.kill_reason or "kill switch flatten"
            source = "kill"
        else:
            reason = f"liquidation flatten ({view.phase})"
            source = "liquidation"
        return Action(
            symbol=symbol,
            side="SELL",
            order_type="MARKET" if aggressive else "LIMIT",
            size_fraction=1.0,
            reason=reason,
            source=source,
            price_direction="down" if aggressive else "up",
            aggressive=aggressive,
        )

    def _reject_buy(
        self,
        decision,
        candidate_by_symbol,
        portfolio,
        view,
        unavailable,
        rules,
        cooldowns,
        now_unix,
        entries_this_hour,
        order_cap,
        entry_cap,
    ) -> dict | None:
        symbol = decision.symbol
        if view.entries_blocked:
            return {"symbol": symbol, "reason": view.block_reason or "entries blocked"}
        blocked = symbol_block(symbol, self.cfg, unavailable, rules.get(symbol))
        if blocked:
            return {"symbol": symbol, "reason": blocked}
        if symbol not in candidate_by_symbol:
            return {"symbol": symbol, "reason": "model selected a symbol the quant layer did not propose"}
        if symbol not in rules or rules[symbol].quote_asset != "TMN":
            return {"symbol": symbol, "reason": "symbol is not a known TMN market"}
        if symbol in portfolio.positions:
            return {"symbol": symbol, "reason": "already in a position"}
        if len(portfolio.positions) >= int(self.cfg["risk"]["max_positions"]):
            return {"symbol": symbol, "reason": "max open positions reached"}
        if decision.confidence < float(self.cfg["risk"]["min_confidence"]):
            return {"symbol": symbol, "reason": f"confidence {decision.confidence:.2f} below minimum"}
        if not decision.rationale:
            return {"symbol": symbol, "reason": "entry has no logged rationale"}
        cooldown = cooldowns.get(symbol, 0)
        if cooldown > now_unix:
            return {"symbol": symbol, "reason": "symbol is in the post-stop cooldown"}
        if entries_this_hour >= entry_cap or entries_this_hour >= order_cap:
            return {"symbol": symbol, "reason": "hourly order cap reached"}
        return None

    def _liquidity_size(self, size: float, symbol: str, equity: float, liquidity: dict | None) -> float:
        """Cut the post-Kelly size when the book is thin. Never raise it."""
        liq = (liquidity or {}).get(symbol)
        if liq is None or equity <= 0:
            return size
        settings = liquidity_settings(self.cfg)
        scaled = size * float(liq.scale)
        depth = liq.depth_100_tmn
        if depth is not None and depth > 0:
            cap = float(settings["max_order_fraction_of_depth"]) * depth / equity
            scaled = min(scaled, cap)
        return max(0.0, scaled)

    def _defer_exit(self, action: Action, position, view: RiskView, liquidity: dict | None, now_unix: float) -> bool:
        """Wait for a usable book on time stops and the passive flatten.

        Stops, trails, the kill switch, and the aggressive liquidation window
        still trade through a thin book.
        """
        if action.aggressive or view.phase == "aggressive" or view.kill_switch:
            return False
        if action.source not in {"time", "liquidation"} and view.phase == "normal":
            return False
        liq = (liquidity or {}).get(action.symbol)
        if liq is None or liq.exit_ready:
            return False
        if action.source == "time":
            settings = liquidity_settings(self.cfg)
            limit = float(self.cfg["risk"]["time_stop_hours"]) + float(settings["exit_defer_hours"])
            if (now_unix - position.entry_time) / 3600 >= limit:
                return False
        return True


def _entry_decisions(agent: AgentDecision, candidates: list[Candidate]) -> list[ParsedDecision]:
    """Quant candidates are the trigger. The model only confirms, cuts, or vetoes.

    An explicit BUY is a confirmation of that symbol and does not fall through
    to a different name. An empty decision list, or only HOLD, is an abstention:
    the highest-scoring candidate is allowed through. veto_all and a hostile
    regime are handled by the caller.
    """
    explicit = [item for item in agent.decisions if item.action == "BUY" and not item.veto]
    if explicit:
        return explicit
    vetoed = {item.symbol for item in agent.decisions if item.veto}
    pool = [item for item in candidates if item.symbol not in vetoed]
    if not pool:
        return []
    top = pool[0]
    return [
        ParsedDecision(
            action="BUY",
            symbol=top.symbol,
            size_fraction=None,
            confidence=0.55,
            veto=False,
            rationale="quant trigger; the model abstained",
        )
    ]


def roll_day(day: str, now: datetime, equity: float, day_start: float | None = None) -> tuple[str, float]:
    """Reset the daily baseline at 00:00 Asia/Tehran (UTC+03:30).

    The same Tehran date keeps `day_start`. A new date, including the instant
    of midnight, adopts the equity passed in. A +03:00 clock would still be on
    the previous date at 20:30 UTC; Tehran is already the next day then.
    """
    current = day_key(now)
    if not day or current != day:
        return current, equity
    if day_start is None:
        return day, equity
    return day, day_start


def _count_since(times: list[float], now: float, window: float) -> int:
    return sum(1 for ts in times if now - ts <= window)
