import unittest
from datetime import datetime, timedelta, timezone

from trader.engine.portfolio import Portfolio
from trader.risk.manager import RiskManager
from trader.strategy.parser import AgentDecision, ParsedDecision
from trader.strategy.signals import Candidate
from trader.timeutil import TEHRAN
from trader.types import MarketRule


def cfg():
    return {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {"entry": "marketable_limit", "exit": "limit_maker"},
        "risk": {
            "max_fraction_per_trade": 0.12,
            "max_fraction_per_symbol": 0.12,
            "max_gross_exposure": 0.30,
            "max_positions": 2,
            "stop_loss_pct": 0.045,
            "time_stop_hours": 24,
            "trail_arm_pct": 0.025,
            "trail_distance_pct": 0.012,
            "cooldown_after_stop_seconds": 21600,
            "max_orders_per_hour": 8,
            "max_entries_per_hour": 2,
            "daily_max_drawdown": 0.015,
            "global_max_drawdown": 0.03,
            "min_notional_tmn": 50000,
            "min_confidence": 0.55,
        },
        "liquidation": {
            "deadline": "2026-10-23T23:50:00+03:30",
            "start_before_minutes": 60,
            "aggressive_before_minutes": 30,
            "flat_by": "2026-10-23T23:30:00+03:30",
            "min_hours_to_enter": 8,
        },
    }


def rule(symbol="BTCTMN"):
    return MarketRule(symbol, "BTC", "TMN", 0, 8, 1e-8, 50000, last_price=100)


def candidate(symbol="BTCTMN", size=0.12):
    return Candidate(symbol, "uptrend_pullback", size, 0.7, 0.009, 0.0058, 0.02, -0.008, 100, 0, {})


def buy(symbol="BTCTMN", size=0.5, confidence=0.8):
    return AgentDecision(
        source="llm", veto_all=False, regime="trend", comment="",
        decisions=[ParsedDecision("BUY", symbol, size, confidence, False, "24h trend 2%, pullback 0.8%, cost cleared")],
    )


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        self.unix = self.now.timestamp()
        self.risk = RiskManager(cfg())

    def _view(self, portfolio, kill=False):
        return self.risk.view(
            portfolio, {}, self.now, peak_equity=portfolio.cash, day_start_equity=portfolio.cash,
            kill_switch=kill, kill_reason="test" if kill else "",
        )

    def test_model_cannot_raise_size_or_invent_a_symbol(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        planned = self.risk.plan(
            portfolio=portfolio, agent=buy(size=0.5), candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        buys = [a for a in planned.actions if a.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertLessEqual(buys[0].size_fraction, 0.12)

        invented = self.risk.plan(
            portfolio=portfolio, agent=buy(symbol="SOLTMN"), candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule(), "SOLTMN": rule("SOLTMN")},
            unavailable=set(), now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(a.side == "BUY" for a in invented.actions))
        self.assertTrue(any("quant" in row["reason"] for row in invented.rejected))

    def test_banned_symbol_and_all_in_are_refused(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        banned = self.risk.plan(
            portfolio=portfolio, agent=buy(), candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable={"BTCTMN"},
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(a.side == "BUY" for a in banned.actions))
        self.assertTrue(any("banned" in row["reason"] for row in banned.rejected))

        portfolio.buy(
            symbol="ETHTMN", price=100, qty=12_000, fee_rate=0, time=int(self.unix),
            stop_price=90, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        # 1.2M notional on 5M is 24%. A 12% add would push through the 30% cap and must be cut.
        view = self.risk.view(
            portfolio, {"ETHTMN": 100}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        capped = self.risk.plan(
            portfolio=portfolio, agent=buy(), candidates=[candidate()],
            marks={"ETHTMN": 100, "BTCTMN": 100},
            rules={"BTCTMN": rule(), "ETHTMN": rule("ETHTMN")},
            unavailable=set(), now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        buys = [a for a in capped.actions if a.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertLess(buys[0].size_fraction, 0.12)
        self.assertLessEqual(view.gross_exposure + buys[0].size_fraction, 0.30 + 1e-9)

    def test_stop_fires_and_kill_switch_blocks_entries_but_flattens(self):
        portfolio = Portfolio(4_000_000)
        portfolio.positions  # touch
        portfolio.buy(
            symbol="BTCTMN", price=100, qty=100, fee_rate=0, time=int(self.unix - 3600),
            stop_price=95, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        view = self._view(portfolio)
        # price through the stop
        planned = self.risk.plan(
            portfolio=portfolio, agent=buy(confidence=0.9), candidates=[candidate()],
            marks={"BTCTMN": 94}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertTrue(any(a.source == "stop" and a.side == "SELL" for a in planned.actions))

        killed = self.risk.view(
            Portfolio(4_900_000), {}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        self.assertTrue(killed.kill_switch)
        self.assertTrue(killed.entries_blocked)
        held = Portfolio(4_900_000)
        held.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0, time=int(self.unix),
            stop_price=50, setup="x", reason="seed", client_id="SC2", order_type="LIMIT",
        )
        # keep the kill on explicitly so the flatten path is what we assert
        view = self.risk.view(
            held, {"BTCTMN": 100}, self.now, peak_equity=5_000_000, day_start_equity=5_000_000,
            kill_switch=True, kill_reason="daily drawdown",
        )
        flat = self.risk.plan(
            portfolio=held, agent=buy(), candidates=[candidate()], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=self.now, view=view,
            order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertTrue(any(a.source == "kill" and a.side == "SELL" for a in flat.actions))
        self.assertFalse(any(a.side == "BUY" for a in flat.actions))

    def test_liquidation_blocks_buys(self):
        late = datetime(2026, 10, 23, 22, 55, tzinfo=TEHRAN)
        portfolio = Portfolio(5_000_000)
        view = self.risk.view(
            portfolio, {}, late, peak_equity=5_000_000, day_start_equity=5_000_000,
            kill_switch=False, kill_reason="",
        )
        self.assertEqual(view.phase, "passive")
        planned = self.risk.plan(
            portfolio=portfolio, agent=buy(), candidates=[candidate()], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=late, view=view,
            order_times=[], cooldowns={}, now_unix=late.timestamp(),
        )
        self.assertFalse(any(a.side == "BUY" for a in planned.actions))

    def test_abstaining_model_lets_the_quant_candidate_through(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        abstain = AgentDecision(source="llm", veto_all=False, regime="trend", comment="no view", decisions=[])
        planned = self.risk.plan(
            portfolio=portfolio, agent=abstain, candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        buys = [a for a in planned.actions if a.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].symbol, "BTCTMN")

    def test_hostile_regime_is_a_veto(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        hostile = AgentDecision(source="llm", veto_all=False, regime="hostile", comment="book is offered", decisions=[])
        planned = self.risk.plan(
            portfolio=portfolio, agent=hostile, candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(a.side == "BUY" for a in planned.actions))
        self.assertTrue(any("veto" in row["reason"] for row in planned.rejected))

    def test_quarter_kelly_cuts_the_fixed_fraction(self):
        settings = cfg()
        settings["risk"]["kelly_fraction"] = 0.25
        settings["risk"]["kelly_win_prob"] = 0.55
        settings["risk"]["kelly_payoff"] = 1.0
        risk = RiskManager(settings)
        portfolio = Portfolio(5_000_000)
        view = risk.view(
            portfolio, {}, self.now, peak_equity=portfolio.cash, day_start_equity=portfolio.cash,
            kill_switch=False, kill_reason="",
        )
        planned = risk.plan(
            portfolio=portfolio, agent=buy(size=0.12), candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        buys = [a for a in planned.actions if a.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0].size_fraction, 0.025)

    def test_hourly_entry_cap(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        planned = self.risk.plan(
            portfolio=portfolio, agent=buy(), candidates=[candidate()], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=self.now, view=view,
            order_times=[self.unix - 10, self.unix - 20], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(a.side == "BUY" for a in planned.actions))
        self.assertTrue(any("cap" in row["reason"] for row in planned.rejected))


if __name__ == "__main__":
    unittest.main()
