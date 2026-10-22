import unittest

from trader.engine.dust import apply_paper, plan_dust_flatten
from trader.engine.portfolio import Portfolio
from trader.engine.runtime import _try_dust_flatten
from trader.order_guard import OrderIntent, assess
from trader.risk.manager import Action
from trader.types import MarketRule
from trader.wallex.client import WallexClient


def _portfolio(cash: float, qty: float, price: float = 100.0) -> Portfolio:
    portfolio = Portfolio(cash + price * qty)
    portfolio.top_up(
        symbol="BTCTMN", price=price, qty=qty, fee_rate=0.0,
        reason="seed", client_id="SEED", order_type="LIMIT",
    )
    return portfolio


class DustFlattenTests(unittest.TestCase):
    def test_topup_then_sell_clears_the_minimum(self):
        portfolio = _portfolio(200_000, qty=100)  # 10,000 TMN of coin
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=portfolio.cash, buy_fee=0.003,
            market_orders_enabled=True, buy_allowed=True,
        )
        self.assertEqual(plan.kind, "topup")
        self.assertGreaterEqual(plan.buy_qty * 100, 50_000)
        self.assertGreaterEqual(plan.sell_qty * 100, 50_000)
        fills = apply_paper(
            portfolio, plan, symbol="BTCTMN", buy_price=100, sell_price=100,
            buy_fee=0.003, sell_fee=0.003, reason="liquidation flatten",
            buy_id="BUY1", sell_id="SELL1",
        )
        self.assertEqual([fill.side for fill in fills], ["BUY", "SELL"])
        self.assertNotIn("BTCTMN", portfolio.positions)
        self.assertGreaterEqual(fills[0].notional_tmn, 50_000)
        self.assertGreaterEqual(fills[1].notional_tmn, 50_000)

    def test_residual_when_cash_cannot_top_up_and_market_is_disabled(self):
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=1_000, buy_fee=0.003,
            market_orders_enabled=False, buy_allowed=True,
        )
        self.assertEqual(plan.kind, "residual")
        row = plan.residual("BTCTMN")
        self.assertEqual(row["symbol"], "BTCTMN")
        self.assertEqual(row["qty"], 100)
        self.assertAlmostEqual(row["mark_tmn"], 10_000)
        self.assertIn("residual", row["reason"])

    def test_market_sell_when_topup_is_unfunded_and_market_orders_are_allowed(self):
        portfolio = _portfolio(1_000, qty=100)
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=portfolio.cash, buy_fee=0.003,
            market_orders_enabled=True, buy_allowed=True,
        )
        self.assertEqual(plan.kind, "market")
        fills = apply_paper(
            portfolio, plan, symbol="BTCTMN", buy_price=100, sell_price=99,
            buy_fee=0.003, sell_fee=0.003, reason="liquidation flatten",
            buy_id="BUY1", sell_id="SELL1",
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].side, "SELL")
        self.assertNotIn("BTCTMN", portfolio.positions)

    def test_hard_denied_coin_is_not_topped_up(self):
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=500_000, buy_fee=0.003,
            market_orders_enabled=False, buy_allowed=False,
        )
        self.assertEqual(plan.kind, "residual")
        self.assertIn("OTC", plan.reason)

    def test_a_position_above_the_minimum_stays_a_normal_sell(self):
        plan = plan_dust_flatten(
            qty=600, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=10_000, buy_fee=0.003,
            market_orders_enabled=False, buy_allowed=True,
        )
        self.assertEqual(plan.kind, "sell")

    def test_topup_orders_are_spot_posts(self):
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=200_000, buy_fee=0.003,
            market_orders_enabled=True, buy_allowed=True,
        )
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, market_orders_enabled=True)
        client = WallexClient(dry_run=True, api_key="test")
        buy = client.place_order(
            symbol="BTCTMN", side="BUY", order_type="LIMIT", quantity=plan.buy_qty, price=100,
            client_id="SCDUSTBUY", rule=rule, price_direction="up",
        )
        sell = client.place_order(
            symbol="BTCTMN", side="SELL", order_type="MARKET", quantity=plan.sell_qty, price=None,
            client_id="SCDUSTSELL", rule=rule, price_direction="down",
        )
        for placed in (buy, sell):
            self.assertEqual(placed["request"]["method"], "POST")
            self.assertTrue(placed["request"]["url"].endswith("/v1/account/orders"))

    def test_flatten_and_killed_log_dust_and_never_buy(self):
        portfolio = _portfolio(200_000, qty=100)
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, market_orders_enabled=True, last_price=100)
        cfg = {
            "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
            "execution": {"stop_slippage_bps": 5},
            "risk": {"min_notional_tmn": 50_000},
            "universe": {"allowlist": ["BTCTMN"], "denylist": ["TON"]},
        }
        action = Action("BTCTMN", "SELL", "LIMIT", 1.0, "liquidation flatten (passive)", "liquidation", "up")
        client = WallexClient(dry_run=True, api_key="test")
        state = {"client_seq": 4}
        blocks = []
        for ops_state in ("FLATTEN", "KILLED"):
            result = _try_dust_flatten(
                cfg, client, portfolio, action, {}, {"BTCTMN": 100}, {"BTCTMN": rule},
                state, False, {}, set(), ops_state=ops_state, guard_blocks=blocks,
            )
            self.assertIsNotNone(result)
            fills, requests, residual = result
            self.assertEqual(fills, [])
            self.assertEqual(requests, [])
            self.assertNotIn("BTCTMN", portfolio.positions)
            self.assertIn("BTCTMN", portfolio.dust)
            self.assertAlmostEqual(portfolio.equity({"BTCTMN": 100}), portfolio.cash + 10_000)
            self.assertNotIn("order_guard", residual["reason"])
            self.assertAlmostEqual(residual["dust_tmn"], 10_000)
            self.assertEqual(state["client_seq"], 4)
            self.assertEqual(blocks, [])
        again = _try_dust_flatten(
            cfg, client, portfolio, action, {}, {"BTCTMN": 100}, {"BTCTMN": rule},
            state, False, {}, set(), ops_state="KILLED", guard_blocks=blocks,
        )
        self.assertTrue(again[2]["repeat"])
        self.assertEqual(again[1], [])
        self.assertEqual(state["client_seq"], 4)
        killed_buy = assess(OrderIntent(
            symbol="BTCTMN", side="BUY", order_type="LIMIT", qty=600, price=100, mark=100,
            endpoint="/v1/account/orders", post_only=True, ops_state="KILLED", held_qty=0,
            symbol_notional_tmn=10_000, gross_notional_tmn=10_000, equity_tmn=210_000,
            quote="TMN", base="BTC", allowlist=("BTCTMN",), deny_bases=(), unavailable=(),
            size_cap=0.12, gross_cap=0.30, min_notional_tmn=50_000,
        ))
        self.assertFalse(killed_buy.ok)
        self.assertIn("LIVE_ARMED", killed_buy.reason)

    def test_flatten_sells_a_position_that_clears_the_minimum(self):
        portfolio = _portfolio(10_000, qty=600)
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, market_orders_enabled=True, last_price=100)
        cfg = {
            "fees": {"maker_rate": 0.0025, "taker_rate": 0.003},
            "execution": {"stop_slippage_bps": 5},
            "risk": {"min_notional_tmn": 50_000},
            "universe": {"allowlist": ["BTCTMN"]},
        }
        action = Action("BTCTMN", "SELL", "LIMIT", 1.0, "kill switch flatten", "kill", "down", True)
        result = _try_dust_flatten(
            cfg, WallexClient(dry_run=True, api_key="test"), portfolio, action, {},
            {"BTCTMN": 100}, {"BTCTMN": rule}, {"client_seq": 0}, False, {}, set(),
            ops_state="FLATTEN",
        )
        self.assertIsNone(result)

    def test_disabled_topup_is_dust_even_when_cash_can_fund_it(self):
        plan = plan_dust_flatten(
            qty=100, buy_price=100, sell_price=100, min_notional=50_000, min_qty=0.01,
            qty_decimals=2, cash=200_000, buy_fee=0.003,
            market_orders_enabled=True, buy_allowed=True, allow_topup=False,
        )
        self.assertEqual(plan.kind, "dust")
        self.assertEqual(plan.buy_qty, 0.0)
        self.assertAlmostEqual(plan.mark_tmn, 10_000)

    def test_shadow_reports_residual_when_the_topup_cannot_be_funded(self):
        portfolio = _portfolio(1_000, qty=100)
        rule = MarketRule(
            "BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000,
            market_orders_enabled=False, last_price=100,
        )
        cfg = {
            "fees": {"maker_rate": 0.0025, "taker_rate": 0.003},
            "execution": {"stop_slippage_bps": 5},
            "risk": {"min_notional_tmn": 50_000},
            "universe": {"allowlist": ["BTCTMN"]},
        }
        action = Action("BTCTMN", "SELL", "MARKET", 1.0, "liquidation flatten (aggressive)", "liquidation", "down", True)
        client = WallexClient(dry_run=True, api_key="test")
        fills, requests, residual = _try_dust_flatten(
            cfg, client, portfolio, action, {}, {"BTCTMN": 100}, {"BTCTMN": rule},
            {"client_seq": 0}, False, {}, set(),
        )
        self.assertEqual(fills, [])
        self.assertEqual(requests, [])
        self.assertEqual(residual["symbol"], "BTCTMN")
        self.assertIn("residual", residual["reason"])
        self.assertIn("BTCTMN", portfolio.positions)

    def test_global_kill_counts_dust_and_dust_does_not_block_it(self):
        from datetime import datetime

        from trader.config import load_config
        from trader.engine.ops import Facts, advance, fresh_ops
        from trader.risk.manager import RiskManager
        from trader.timeutil import TEHRAN

        cfg = load_config("config/default.yaml")
        portfolio = Portfolio(4_700_000)
        portfolio.dust["BTCTMN"] = {
            "qty": 1.0,
            "entry_price": 300_000,
            "mark_price": 150_000,
            "cost_tmn": 300_000,
        }
        marks = {"BTCTMN": 150_000}
        self.assertAlmostEqual(portfolio.equity(marks), 4_850_000)
        restored = Portfolio.from_dict(portfolio.to_dict())
        self.assertAlmostEqual(restored.equity(marks), 4_850_000)
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        view = RiskManager(cfg).view(
            portfolio, marks, now,
            peak_equity=5_000_000,
            day_start_equity=4_850_000,
            kill_switch=False,
            kill_reason="",
        )
        self.assertAlmostEqual(view.daily_drawdown, 0.0)
        self.assertAlmostEqual(view.drawdown, 0.03)
        self.assertTrue(view.kill_switch)
        self.assertIn("global", view.kill_reason)
        ops = fresh_ops()
        ops["state"] = "FLATTEN"
        ops["locked"] = True
        facts = Facts(
            now=now,
            mode="live",
            send_orders=True,
            equity=view.equity,
            daily_drawdown=view.daily_drawdown,
            global_drawdown=view.drawdown,
            kill_switch=view.kill_switch,
            phase=view.phase,
        )
        advance(ops, facts, cfg, stage="pre")
        self.assertEqual(ops["state"], "KILLED")


if __name__ == "__main__":
    unittest.main()
