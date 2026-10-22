import json
import os
import unittest
from datetime import datetime
from pathlib import Path

from trader.engine.portfolio import Portfolio
from trader.engine.runtime import _order_qty
from trader.risk.manager import RiskManager
from trader.strategy.agent import TradingAgent
from trader.strategy.liquidity import Liquidity
from trader.strategy.menu import MenuAction, hold_action, parse_menu_choice
from trader.strategy.parser import AgentDecision, ParsedDecision
from trader.strategy.signals import Candidate, SymbolFeatures
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


def _feature(symbol="BTCTMN", close=90.0, ema_slow=100.0, roc_trend=-0.01) -> SymbolFeatures:
    return SymbolFeatures(
        symbol=symbol, bar_time=1_700_000_000, close=close, ema_fast=95, ema_slow=ema_slow,
        roc_pullback=-0.01, roc_trend=roc_trend, atr_pct=0.004, rsi=48, spread_bps=10,
        imbalance=0.1, micro_return=0.0, expected_gross=0.009, round_trip_cost=0.0058,
        signal=False, blockers=[], score=0.2,
    )


class MenuParseTests(unittest.TestCase):
    def setUp(self):
        self.menu = [
            hold_action(),
            MenuAction("buy_BTCTMN", "BUY", "BTCTMN", 0.025, "BUY BTCTMN at the maker bid, size 0.0250 of equity"),
        ]

    def test_invalid_json_unknown_id_and_empty_rationale_are_hold(self):
        for text in ("not json", '{"action_id": "buy_ETH", "rationale": "go"}', '{"action_id": "buy_BTCTMN", "rationale": "  "}'):
            chosen, note = parse_menu_choice(text, self.menu)
            self.assertEqual(chosen.id, "hold", text)
            self.assertEqual(note, "invalid output")

    def test_known_id_is_returned(self):
        chosen, note = parse_menu_choice(
            '{"action_id": "buy_BTCTMN", "rationale": "24h return is 2%"}',
            self.menu,
        )
        self.assertEqual(note, "")
        self.assertEqual(chosen.kind, "BUY")
        self.assertAlmostEqual(chosen.size_fraction, 0.025)


class MenuRiskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        self.unix = self.now.timestamp()
        self.risk = RiskManager(cfg())

    def _view(self, portfolio, kill=False):
        return self.risk.view(
            portfolio, {}, self.now, peak_equity=portfolio.cash, day_start_equity=portfolio.cash,
            kill_switch=kill, kill_reason="test" if kill else "",
        )

    def _menu(self, portfolio, view, candidates=None, features=None, pending=None, liquidity=None, unavailable=None, order_times=None):
        return self.risk.legal_menu(
            portfolio=portfolio,
            candidates=candidates or [],
            marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()},
            unavailable=unavailable or set(),
            now=self.now,
            view=view,
            order_times=order_times or [],
            cooldowns={},
            now_unix=self.unix,
            liquidity=liquidity,
            features=features,
            pending=pending,
        )

    def test_menu_offers_a_sized_buy_and_always_hold(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        menu = self._menu(portfolio, view, candidates=[candidate()])
        kinds = [item.kind for item in menu]
        self.assertEqual(kinds[0], "HOLD")
        buy_row = next(item for item in menu if item.kind == "BUY")
        self.assertEqual(buy_row.id, "buy_BTCTMN")
        self.assertAlmostEqual(buy_row.size_fraction, 0.12)
        self.assertIn("maker bid", buy_row.summary)

    def test_banned_cap_and_kill_drop_the_buy(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        banned = self._menu(portfolio, view, candidates=[candidate()], unavailable={"BTCTMN"})
        self.assertEqual([item.kind for item in banned], ["HOLD"])
        capped = self._menu(portfolio, view, candidates=[candidate()], order_times=[self.unix - 10, self.unix - 20])
        self.assertEqual([item.kind for item in capped], ["HOLD"])
        killed = self._view(portfolio, kill=True)
        pending = {"SCBUY1": {"symbol": "BTCTMN", "side": "BUY", "price": 100}}
        flat = self._menu(portfolio, killed, candidates=[candidate()], pending=pending)
        self.assertEqual([item.kind for item in flat], ["HOLD"])

    def test_sell_reduce_and_cancel_follow_the_quant_rule(self):
        portfolio = Portfolio(5_000_000)
        portfolio.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0, time=int(self.unix - 3600),
            stop_price=90, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        view = self.risk.view(
            portfolio, {"BTCTMN": 100}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        quiet = self._menu(portfolio, view, features=[_feature(close=110, ema_slow=100, roc_trend=0.02)])
        self.assertEqual([item.kind for item in quiet], ["HOLD"])
        offered = self._menu(
            portfolio, view,
            features=[_feature()],
            pending={"SCBUY1": {"symbol": "ETHTMN", "side": "BUY", "price": 50}},
        )
        ids = [item.id for item in offered]
        self.assertIn("sell_BTCTMN", ids)
        self.assertIn("reduce_BTCTMN", ids)
        self.assertIn("cancel_SCBUY1", ids)
        self.assertNotIn("buy_BTCTMN", ids)

    def test_a_live_stop_is_not_offered_as_a_discretionary_sell(self):
        portfolio = Portfolio(5_000_000)
        portfolio.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0, time=int(self.unix - 3600),
            stop_price=95, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        view = self.risk.view(
            portfolio, {"BTCTMN": 94}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        menu = self.risk.legal_menu(
            portfolio=portfolio, candidates=[], marks={"BTCTMN": 94}, rules={"BTCTMN": rule()},
            unavailable=set(), now=self.now, view=view, order_times=[], cooldowns={},
            now_unix=self.unix, features=[_feature(close=94, ema_slow=100, roc_trend=-0.06)],
        )
        self.assertEqual([item.kind for item in menu], ["HOLD"])

    def test_invalid_menu_hold_does_not_buy_the_candidate(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        decision = AgentDecision(
            source="llm", veto_all=False, regime="menu", comment="invalid output",
            decisions=[ParsedDecision("HOLD", "", None, 1.0, False, "invalid output")],
            menu_bound=True, picked_id="hold", error="invalid output",
        )
        planned = self.risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(action.side == "BUY" for action in planned.actions))

    def test_menu_size_is_not_liquidity_scaled_twice(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        thin = {"BTCTMN": Liquidity(scale=0.5, depth_100_tmn=5_000_000_000, exit_ready=True)}
        menu = self._menu(portfolio, view, candidates=[candidate()], liquidity=thin)
        offered = next(item for item in menu if item.kind == "BUY")
        self.assertAlmostEqual(offered.size_fraction, 0.06)
        decision = AgentDecision(
            source="llm", veto_all=False, regime="menu", comment="book is usable",
            decisions=[ParsedDecision("BUY", "BTCTMN", offered.size_fraction, 0.7, False, "depth still clears the order")],
            menu_bound=True, picked_id=offered.id,
        )
        planned = self.risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable=set(),
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
            liquidity=thin,
        )
        buys = [action for action in planned.actions if action.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0].size_fraction, 0.06)

    def test_risk_still_vetoes_a_menu_pick(self):
        portfolio = Portfolio(5_000_000)
        view = self._view(portfolio)
        decision = AgentDecision(
            source="llm", veto_all=False, regime="menu", comment="buy",
            decisions=[ParsedDecision("BUY", "BTCTMN", 0.12, 0.7, False, "cost gate passed")],
            menu_bound=True, picked_id="buy_BTCTMN",
        )
        planned = self.risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate()],
            marks={"BTCTMN": 100}, rules={"BTCTMN": rule()}, unavailable={"BTCTMN"},
            now=self.now, view=view, order_times=[], cooldowns={}, now_unix=self.unix,
        )
        self.assertFalse(any(action.side == "BUY" for action in planned.actions))
        self.assertTrue(any("banned" in row["reason"] for row in planned.rejected))

        held = Portfolio(5_000_000)
        held.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0, time=int(self.unix),
            stop_price=90, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        held_view = self.risk.view(
            held, {"BTCTMN": 100}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        reduce = AgentDecision(
            source="llm", veto_all=False, regime="menu", comment="cut",
            decisions=[ParsedDecision("SELL", "BTCTMN", 0.5, 0.7, False, "close is under the slow average")],
            menu_bound=True, picked_id="reduce_BTCTMN",
        )
        kept = self.risk.plan(
            portfolio=held, agent=reduce, candidates=[], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=self.now, view=held_view,
            order_times=[], cooldowns={}, now_unix=self.unix,
            features=[_feature(close=110, ema_slow=100, roc_trend=0.01)],
        )
        self.assertFalse(any(action.side == "SELL" for action in kept.actions))
        allowed = self.risk.plan(
            portfolio=held, agent=reduce, candidates=[], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=self.now, view=held_view,
            order_times=[], cooldowns={}, now_unix=self.unix,
            features=[_feature()],
        )
        sells = [action for action in allowed.actions if action.side == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0].size_fraction, 0.5)
        self.assertEqual(sells[0].source, "agent_reduce")

    def test_old_close_stays_a_full_exit(self):
        portfolio = Portfolio(5_000_000)
        portfolio.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0, time=int(self.unix),
            stop_price=90, setup="x", reason="seed", client_id="SC1", order_type="LIMIT",
        )
        view = self.risk.view(
            portfolio, {"BTCTMN": 100}, self.now, peak_equity=5_000_000,
            day_start_equity=5_000_000, kill_switch=False, kill_reason="",
        )
        decision = AgentDecision(
            source="llm", veto_all=False, regime="trend", comment="",
            decisions=[ParsedDecision("SELL", "BTCTMN", 0.08, 0.7, False, "early exit")],
        )
        planned = self.risk.plan(
            portfolio=portfolio, agent=decision, candidates=[], marks={"BTCTMN": 100},
            rules={"BTCTMN": rule()}, unavailable=set(), now=self.now, view=view,
            order_times=[], cooldowns={}, now_unix=self.unix,
        )
        sells = [action for action in planned.actions if action.side == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0].size_fraction, 1.0)

    def test_partial_qty_and_full_qty(self):
        self.assertEqual(_order_qty(2.0, 1.0, 4), 2.0)
        self.assertEqual(_order_qty(2.0, None, 4), 2.0)
        self.assertAlmostEqual(_order_qty(2.0, 0.5, 4), 1.0)


class MenuAgentTests(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_BASE_URL"] = "https://ai.boxapi.ir/ai/v1"
        os.environ["LLM_API_KEY"] = "test"
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("LLM_MODEL_CHEAP", None)
        os.environ.pop("LLM_MODEL_STRONG", None)
        self.path = Path("data/test_menu_llm.json")
        if self.path.exists():
            self.path.unlink()
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)

    def tearDown(self):
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("LLM_MODEL_CHEAP", None)
        os.environ.pop("LLM_MODEL_STRONG", None)
        if self.path.exists():
            self.path.unlink()

    def _agent(self, post, calls=None, **llm):
        settings = {
            "usage_path": str(self.path),
            "min_interval_seconds": 0,
            "max_retries": 1,
            "retry_backoff_seconds": 0,
            "cache_seconds": 3600,
        }
        settings.update(llm)

        def wrapped(url, headers, body, timeout=None):
            if calls is not None:
                calls.setdefault("bodies", []).append(body)
                calls["n"] = calls.get("n", 0) + 1
            return post(url, headers, body, timeout)

        return TradingAgent({"llm": settings}, post=wrapped, sleep=lambda _seconds: None)

    def _buy_menu(self):
        return [
            hold_action(),
            MenuAction("buy_BTCTMN", "BUY", "BTCTMN", 0.025, "BUY BTCTMN at the maker bid, size 0.0250 of equity"),
        ]

    def test_a_buy_on_the_menu_uses_the_trade_tier(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            self.assertNotIn("temperature", body)
            return json.dumps({"action_id": "buy_BTCTMN", "rationale": "24h return is 2%"}), {
                "prompt_tokens": 30, "completion_tokens": 12, "cost": "3e-6", "model": body["model"],
            }

        agent = self._agent(post, calls)
        decision = agent.decide(
            [_feature()], [candidate()], [], {"equity": 5_000_000, "phase": "normal"},
            1_700_000_000, menu=self._buy_menu(),
        )
        self.assertEqual(calls["bodies"][0]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls["bodies"][0]["reasoning_effort"], "medium")
        self.assertNotIn("max_tokens", calls["bodies"][0])
        self.assertIn("action_id", calls["bodies"][0]["messages"][0]["content"])
        self.assertTrue(decision.menu_bound)
        self.assertEqual(decision.picked_id, "buy_BTCTMN")
        self.assertEqual(decision.decisions[0].action, "BUY")
        self.assertAlmostEqual(decision.decisions[0].size_fraction, 0.025)
        self.assertEqual(decision.usage["tier"], "trade")
        logged = decision.as_log()
        self.assertEqual(logged["picked_id"], "buy_BTCTMN")
        self.assertEqual(logged["menu"][1]["id"], "buy_BTCTMN")

    def test_sell_or_cancel_without_a_buy_stays_on_the_signal_tier(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            return json.dumps({"action_id": "reduce_BTCTMN", "rationale": "close is under the slow EMA"}), {
                "prompt_tokens": 10, "completion_tokens": 8, "cost": "3e-6", "model": body["model"],
            }

        agent = self._agent(post, calls)
        menu = [
            hold_action(),
            MenuAction("reduce_BTCTMN", "REDUCE", "BTCTMN", 0.5, "REDUCE BTCTMN by half"),
            MenuAction("cancel_SC1", "CANCEL", "BTCTMN", summary="CANCEL order SC1", client_order_id="SC1"),
        ]
        decision = agent.decide([_feature()], [], [{"symbol": "BTCTMN"}], {"phase": "normal"}, 1_700_000_000, menu=menu)
        self.assertEqual(calls["bodies"][0]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls["bodies"][0]["reasoning_effort"], "medium")
        self.assertEqual(decision.usage["tier"], "signal")
        self.assertEqual(decision.decisions[0].action, "SELL")
        self.assertAlmostEqual(decision.decisions[0].size_fraction, 0.5)

    def test_a_buy_while_already_exposed_uses_sol(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            return json.dumps({"action_id": "buy_BTCTMN", "rationale": "second name, day is still flat"}), {
                "prompt_tokens": 20, "completion_tokens": 10, "cost": "0.013", "model": body["model"],
            }

        agent = self._agent(post, calls)
        decision = agent.decide(
            [_feature()], [candidate()], [{"symbol": "ETHTMN"}],
            {"equity": 5_000_000, "gross_exposure": 0.08, "phase": "normal"},
            1_700_000_000, menu=self._buy_menu(),
        )
        self.assertEqual(calls["bodies"][0]["model"], "openai/gpt-6-sol")
        self.assertEqual(calls["bodies"][0]["reasoning_effort"], "high")
        self.assertEqual(decision.usage["tier"], "stakes")
        self.assertEqual(decision.usage["cost_source"], "usage.cost")
        self.assertEqual(decision.decisions[0].action, "BUY")

    def test_invalid_output_holds_and_a_dead_model_falls_back(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            return "buy something", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.000001, "model": body["model"]}

        agent = self._agent(post, calls)
        decision = agent.decide(
            [_feature()], [candidate()], [], {"equity": 1}, 1_700_000_000, menu=self._buy_menu(),
        )
        self.assertEqual(calls["n"], 1)
        self.assertTrue(decision.menu_bound)
        self.assertEqual(decision.picked_id, "hold")
        self.assertEqual(decision.decisions[0].action, "HOLD")
        self.assertEqual(decision.error, "invalid output")
        self.assertFalse(any(item.action == "BUY" for item in decision.decisions))

        def down(url, headers, body, timeout=None):
            raise RuntimeError("boxapi down")

        agent = self._agent(down, max_retries=1)
        failed = agent.decide(
            [_feature(symbol="ETHTMN")], [candidate()], [], {"equity": 1}, 1_700_003_600, menu=self._buy_menu(),
        )
        self.assertFalse(failed.menu_bound)
        self.assertEqual(failed.source, "fallback")
        self.assertEqual(failed.decisions[0].action, "BUY")
        self.assertTrue(failed.menu)

    def test_hold_only_skips_the_call(self):
        def post(url, headers, body, timeout=None):
            raise AssertionError("hold-only menu must not call the model")

        agent = self._agent(post)
        decision = agent.decide([_feature()], [], [], {"phase": "normal"}, 1_700_000_000, menu=[hold_action()])
        self.assertTrue(decision.menu_bound)
        self.assertEqual(decision.picked_id, "hold")
        self.assertEqual(decision.usage["skipped"], "menu is hold only")
        self.assertEqual(decision.error, "")

    def test_no_menu_keeps_the_old_prompt(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            return json.dumps({
                "veto_all": False, "regime": "trend", "comment": "ok",
                "decisions": [{
                    "action": "BUY", "symbol": "BTCTMN", "size_fraction": 0.05,
                    "confidence": 0.7, "veto": False, "rationale": "cost gate already passed",
                }],
            }), {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.000001, "model": body["model"]}

        agent = self._agent(post, calls)
        decision = agent.decide([_feature()], [candidate()], [], {"equity": 1}, 1_700_000_000)
        self.assertFalse(decision.menu_bound)
        self.assertIn("decisions", calls["bodies"][0]["messages"][0]["content"])
        self.assertEqual(decision.decisions[0].action, "BUY")
        self.assertEqual(decision.usage["tier"], "trade")


if __name__ == "__main__":
    unittest.main()
