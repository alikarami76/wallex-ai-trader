import unittest
from datetime import datetime

from trader.engine.portfolio import Portfolio
from trader.risk.manager import RiskManager
from trader.strategy.liquidity import Liquidity, measure_liquidity
from trader.strategy.signals import evaluate_symbol
from trader.timeutil import TEHRAN
from trader.types import Bar, BookLevel, MarketRule, OrderBook


def _cfg():
    return {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {"entry": "limit_maker", "exit": "limit_maker"},
        "universe": {"max_spread_bps": 22},
        "strategy": {
            "ema_fast": 8, "ema_slow": 24, "pullback_bars": 4,
            "pullback_roc_max": -0.004, "pullback_roc_min": -0.025,
            "trend_bars": 24, "trend_roc_min": 0.01, "rsi_period": 14,
            "atr_period": 14, "max_atr_pct": 0.03, "expected_gross": 0.009,
            "edge_multiple": 1.2, "min_micro_return": -0.004,
            "entry_size_fraction": 0.12, "imbalance_block": -0.55,
        },
        "risk": {
            "max_fraction_per_trade": 0.12, "max_fraction_per_symbol": 0.12,
            "max_gross_exposure": 0.30, "max_positions": 2, "stop_loss_pct": 0.045,
            "time_stop_hours": 24, "trail_arm_pct": 0.025, "trail_distance_pct": 0.012,
            "cooldown_after_stop_seconds": 21600, "max_orders_per_hour": 8,
            "max_entries_per_hour": 2, "daily_max_drawdown": 0.015,
            "global_max_drawdown": 0.03, "min_notional_tmn": 50000, "min_confidence": 0.55,
        },
        "liquidation": {
            "deadline": "2026-10-23T23:50:00+03:30",
            "start_before_minutes": 60, "aggressive_before_minutes": 30,
            "flat_by": "2026-10-23T23:30:00+03:30", "min_hours_to_enter": 8,
        },
    }


def _book(bid_qty: float, ask_qty: float, bid: float = 99.9, ask: float = 100.1) -> OrderBook:
    return OrderBook(
        "BTCTMN",
        bids=[BookLevel(bid, bid_qty), BookLevel(bid * 0.98, bid_qty)],
        asks=[BookLevel(ask, ask_qty), BookLevel(ask * 1.02, ask_qty)],
    )


def _bars(n: int, volume: float = 1.0) -> list[Bar]:
    price = 100.0
    bars = []
    for i in range(n):
        nxt = price * 1.001
        bars.append(Bar(i * 3600, price, nxt, price, nxt, volume))
        price = nxt
    return bars


class LiquidityTests(unittest.TestCase):
    def test_depth_keeps_only_the_band_around_mid(self):
        book = _book(1.0, 1.0)
        inside = book.depth_notional(0.01)
        self.assertIsNotNone(inside)
        bid, ask = inside
        self.assertAlmostEqual(bid, 99.9)
        self.assertAlmostEqual(ask, 100.1)
        self.assertGreater(book.depth_notional(0.03)[0], bid)

    def test_mild_depth_drop_scales_size_and_a_shock_blocks(self):
        cfg = _cfg()
        cfg["liquidity"] = {"min_depth_tmn": 1}
        book = OrderBook("BTCTMN", bids=[BookLevel(99.95, 0.5)], asks=[BookLevel(100.05, 0.5)])
        history = {"depth": [100.0 if i % 2 == 0 else 140.0 for i in range(18)], "spread": [10.0] * 18}
        mild = measure_liquidity(_bars(2), cfg, book=book, history=history, max_spread_bps=22)
        self.assertEqual(mild.block_reason, "")
        self.assertAlmostEqual(mild.scale, 0.79, places=2)
        self.assertTrue(mild.exit_ready)

        shocked = {"depth": [50_000_000.0] * 20, "spread": [10.0] * 20}
        blocked = measure_liquidity(
            _bars(2), _cfg(), book=_book(10_000, 10_000), history=shocked, max_spread_bps=22,
        )
        self.assertIn("thinned", blocked.block_reason)
        self.assertFalse(blocked.exit_ready)

    def test_dry_volume_blocks_the_signal(self):
        bars = _bars(24, volume=10.0)
        bars[-1] = Bar(bars[-1].time, bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 0.0001)
        row = evaluate_symbol(
            "BTCTMN", bars, _cfg(), spread_bps=10, imbalance=0.1, micro_return=0.0,
            maker_fee=0.0025, taker_fee=0.003,
        )
        self.assertFalse(row.signal)
        self.assertTrue(any("volume" in item for item in row.blockers))
        self.assertIsNotNone(row.liquidity.volume_z)
        self.assertIn("volume_z", row.compact())

    def test_missing_book_does_not_block(self):
        row = evaluate_symbol(
            "BTCTMN", _bars(4), _cfg(), spread_bps=10, imbalance=None, micro_return=None,
            maker_fee=0.0025, taker_fee=0.003,
        )
        self.assertNotIn("depth", row.liquidity.block_reason)
        self.assertTrue(row.liquidity.exit_ready)
        self.assertEqual(row.liquidity.scale, 1.0)

    def test_scale_applies_after_the_cap_and_passive_exit_waits(self):
        from trader.strategy.parser import AgentDecision, ParsedDecision
        from trader.strategy.signals import Candidate

        risk = RiskManager(_cfg())
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        portfolio = Portfolio(5_000_000)
        view = risk.view(
            portfolio, {}, now, peak_equity=5_000_000, day_start_equity=5_000_000,
            kill_switch=False, kill_reason="",
        )
        candidate = Candidate("BTCTMN", "uptrend_pullback", 0.12, 0.7, 0.009, 0.0058, 0.02, -0.008, 100, 0, {})
        decision = AgentDecision(
            source="fallback", veto_all=False, regime="trend", comment="",
            decisions=[ParsedDecision("BUY", "BTCTMN", None, 0.8, False, "quant candidate")],
        )
        thin = Liquidity(scale=0.5, depth_100_tmn=5_000_000_000, exit_ready=True)
        planned = risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate], marks={"BTCTMN": 100},
            rules={"BTCTMN": MarketRule("BTCTMN", "BTC", "TMN", 0, 8, 1e-8, 50000)},
            unavailable=set(), now=now, view=view, order_times=[], cooldowns={},
            now_unix=now.timestamp(), liquidity={"BTCTMN": thin},
        )
        buy = next(action for action in planned.actions if action.side == "BUY")
        self.assertAlmostEqual(buy.size_fraction, 0.06)

        dust = Liquidity(scale=1.0, depth_100_tmn=200_000, exit_ready=True)
        refused = risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate], marks={"BTCTMN": 100},
            rules={"BTCTMN": MarketRule("BTCTMN", "BTC", "TMN", 0, 8, 1e-8, 50000)},
            unavailable=set(), now=now, view=view, order_times=[], cooldowns={},
            now_unix=now.timestamp(), liquidity={"BTCTMN": dust},
        )
        self.assertFalse(any(action.side == "BUY" for action in refused.actions))
        self.assertTrue(any("liquidity cap" in row["reason"] for row in refused.rejected))

        passive_now = datetime(2026, 10, 23, 23, 0, tzinfo=TEHRAN)
        held = Portfolio(5_000_000)
        held.buy(
            symbol="BTCTMN", price=100, qty=100, fee_rate=0, time=int(passive_now.timestamp()) - 3600,
            stop_price=90, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        passive = risk.view(
            held, {"BTCTMN": 100}, passive_now, peak_equity=5_000_000, day_start_equity=5_000_000,
            kill_switch=False, kill_reason="",
        )
        waiting = risk.plan(
            portfolio=held, agent=AgentDecision("fallback", False, "trend", "", []),
            candidates=[], marks={"BTCTMN": 100},
            rules={"BTCTMN": MarketRule("BTCTMN", "BTC", "TMN", 0, 8, 1e-8, 50000)},
            unavailable=set(), now=passive_now, view=passive, order_times=[], cooldowns={},
            now_unix=passive_now.timestamp(),
            liquidity={"BTCTMN": Liquidity(exit_ready=False, depth_100_tmn=10_000)},
        )
        self.assertFalse(any(action.side == "SELL" for action in waiting.actions))
        self.assertTrue(any("deferred" in row["reason"] for row in waiting.rejected))

        aggressive_now = datetime(2026, 10, 23, 23, 25, tzinfo=TEHRAN)
        aggressive = risk.view(
            held, {"BTCTMN": 100}, aggressive_now, peak_equity=5_000_000, day_start_equity=5_000_000,
            kill_switch=False, kill_reason="",
        )
        forced = risk.plan(
            portfolio=held, agent=AgentDecision("fallback", False, "trend", "", []),
            candidates=[], marks={"BTCTMN": 100},
            rules={"BTCTMN": MarketRule("BTCTMN", "BTC", "TMN", 0, 8, 1e-8, 50000)},
            unavailable=set(), now=aggressive_now, view=aggressive, order_times=[], cooldowns={},
            now_unix=aggressive_now.timestamp(),
            liquidity={"BTCTMN": Liquidity(exit_ready=False, depth_100_tmn=10_000)},
        )
        self.assertTrue(any(action.source == "liquidation" and action.aggressive for action in forced.actions))


if __name__ == "__main__":
    unittest.main()
