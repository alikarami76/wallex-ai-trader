"""Hard risk layer.

Stops, the drawdown kill switch, exposure caps, the fee gate's size, and the
liquidation window are applied after the model speaks. A BUY the model
invented, or a size above the quant proposal, is cut here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.engine.portfolio import Portfolio
from trader.strategy.parser import AgentDecision
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
            new_reason = f"daily drawdown {daily:.2%} hit the kill switch"
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
    ) -> RiskResult:
        actions: list[Action] = []
        rejected: list[dict] = []
        risk = self.cfg["risk"]
        held = set(portfolio.positions)

        for symbol, position in list(portfolio.positions.items()):
            price = marks.get(symbol, position.entry_price)
            forced = self._forced_exit(symbol, position, price, now_unix, view)
            if forced:
                actions.append(forced)

        forced_symbols = {action.symbol for action in actions}
        if view.phase != "normal" or view.kill_switch:
            for symbol in held:
                if symbol in forced_symbols:
                    continue
                actions.append(self._flatten_action(symbol, view))

        candidate_by_symbol = {item.symbol: item for item in candidates}
        entries_this_hour = _count_since(order_times, now_unix, 3600)
        entry_cap = int(risk["max_entries_per_hour"])
        order_cap = int(risk["max_orders_per_hour"])

        if not agent.veto_all and view.phase == "normal" and not view.kill_switch:
            for decision in agent.decisions:
                if decision.action == "HOLD" or decision.veto:
                    continue
                if decision.action in {"SELL", "CLOSE"}:
                    if decision.symbol in held and decision.symbol not in forced_symbols:
                        actions.append(
                            Action(
                                symbol=decision.symbol,
                                side="SELL",
                                order_type="LIMIT",
                                size_fraction=1.0,
                                reason=decision.rationale or "model requested an early exit",
                                source="agent_close",
                                price_direction="up",
                            )
                        )
                    else:
                        rejected.append({"symbol": decision.symbol, "reason": "close ignored; no position"})
                    continue
                if decision.action != "BUY":
                    continue
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
                    continue
                candidate = candidate_by_symbol[decision.symbol]
                size = decision.size_fraction if decision.size_fraction is not None else candidate.size_fraction
                size = min(size, candidate.size_fraction, float(risk["max_fraction_per_trade"]))
                room = float(risk["max_gross_exposure"]) - view.gross_exposure
                size = min(size, room)
                if size <= 0:
                    rejected.append({"symbol": decision.symbol, "reason": "no exposure room"})
                    continue
                actions.append(
                    Action(
                        symbol=decision.symbol,
                        side="BUY",
                        order_type="LIMIT",
                        size_fraction=size,
                        reason=decision.rationale or candidate.setup,
                        source=agent.source,
                        price_direction="up",
                        aggressive=True,
                    )
                )
                entries_this_hour += 1
                # one new entry per cycle, even if the cap allows more
                break
        elif agent.veto_all:
            rejected.append({"symbol": "*", "reason": "model vetoed every new entry"})

        return RiskResult(view=view, actions=actions, rejected=rejected)

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
        if symbol in unavailable:
            return {"symbol": symbol, "reason": "symbol is on the credit-plan banned list"}
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


def roll_day(day: str, now: datetime, equity: float) -> tuple[str, float]:
    current = day_key(now)
    if current != day:
        return current, equity
    return day, equity


def _count_since(times: list[float], now: float, window: float) -> int:
    return sum(1 for ts in times if now - ts <= window)
