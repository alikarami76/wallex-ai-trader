import json
import random
import unittest
from datetime import datetime
from pathlib import Path

from trader.engine.portfolio import Portfolio
from trader.engine.runtime import _execute
from trader.order_guard import TRADING_ENDPOINTS, OrderIntent, assess
from trader.risk.manager import Action, RiskManager
from trader.strategy.agent import TradingAgent
from trader.strategy.menu import MenuAction, hold_action, parse_menu_choice
from trader.strategy.signals import Candidate
from trader.timeutil import TEHRAN
from trader.types import MarketRule
from trader.wallex.client import WallexClient


def _intent(**overrides) -> OrderIntent:
    base = dict(
        symbol="BTCTMN",
        side="BUY",
        order_type="LIMIT",
        qty=1000,
        price=100,
        mark=100,
        endpoint="/v1/account/orders",
        post_only=True,
        ops_state="LIVE_ARMED",
        held_qty=0,
        symbol_notional_tmn=0,
        gross_notional_tmn=0,
        equity_tmn=5_000_000,
        quote="TMN",
        base="BTC",
        allowlist=("BTCTMN", "ETHTTMN"),
        deny_bases=("TON", "NEAR", "FET", "INJ", "TAO", "WIF", "BONK", "PENDLE", "RENDER"),
        unavailable=(),
        size_cap=0.12,
        gross_cap=0.30,
        min_notional_tmn=50_000,
    )
    base.update(overrides)
    return OrderIntent(**base)


def _cfg():
    return {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {"entry": "limit_maker", "exit": "limit_maker", "quote_gamma": 0.5},
        "universe": {
            "allowlist": ["BTCTMN"],
            "denylist": ["TON", "NEAR", "FET", "INJ", "TAO", "WIF", "BONK", "PENDLE", "RENDER"],
        },
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
            "kelly_fraction": 0.25,
            "kelly_win_prob": 0.55,
            "kelly_payoff": 1.0,
        },
        "liquidation": {
            "deadline": "2026-10-23T23:50:00+03:30",
            "start_before_minutes": 60,
            "aggressive_before_minutes": 30,
            "flat_by": "2026-10-23T23:30:00+03:30",
            "min_hours_to_enter": 8,
        },
        "llm": {"enabled": False},
    }


class RecordingClient(WallexClient):
    def __init__(self):
        super().__init__(dry_run=True, api_key="test-key")
        self.placed = []

    def get_order(self, client_order_id: str):
        return None

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return super().place_order(**kwargs)


class OrderGuardTests(unittest.TestCase):
    def test_a_clean_live_entry_passes(self):
        verdict = assess(_intent())
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, "")
        self.assertEqual(TRADING_ENDPOINTS, frozenset({"/v1/account/orders"}))

    def test_each_condition_can_refuse_without_a_side_effect(self):
        cases = [
            {"quote": "USDT", "symbol": "BTCUSDT"},
            {"endpoint": "/v1/account/wallet/cashout"},
            {"symbol": "TONTMN", "base": "TON"},
            {"unavailable": ("BTCTMN",)},
            {"allowlist": ("ETHTTMN",)},
            {"ops_state": "SHADOW"},
            {"ops_state": "LIVE_MANAGE"},
            {"post_only": False},
            {"order_type": "MARKET", "post_only": False},
            {"qty": 10, "price": 100},
            {"qty": 8000, "price": 100},
            {"gross_notional_tmn": 1_500_000},
            {"side": "SELL", "qty": 5, "held_qty": 1, "price": 100_000, "symbol_notional_tmn": 100_000},
        ]
        for overrides in cases:
            verdict = assess(_intent(**overrides))
            self.assertFalse(verdict.ok, overrides)
            self.assertTrue(verdict.reason)

    def test_a_full_sell_outside_live_armed_can_pass(self):
        verdict = assess(_intent(
            side="SELL",
            order_type="MARKET",
            post_only=False,
            ops_state="FLATTEN",
            qty=1,
            held_qty=1,
            price=None,
            mark=100_000,
            symbol_notional_tmn=100_000,
            gross_notional_tmn=100_000,
        ))
        self.assertTrue(verdict.ok, verdict.reason)

    def test_execute_does_not_send_or_touch_state_when_the_guard_refuses(self):
        client = RecordingClient()
        portfolio = Portfolio(5_000_000)
        state = {"client_seq": 0}
        blocks = []
        action = Action("BTCTMN", "BUY", "LIMIT", 0.02, "setup", "llm", "down")
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, last_price=100)
        fill, request = _execute(
            _cfg(), client, portfolio, action, {}, {"BTCTMN": 100}, {"BTCTMN": rule},
            state, True, {}, unavailable=set(), ops_state="SHADOW", guard_blocks=blocks,
        )
        self.assertIsNone(fill)
        self.assertIsNone(request)
        self.assertEqual(client.placed, [])
        self.assertEqual(state, {"client_seq": 0})
        self.assertEqual(portfolio.cash, 5_000_000)
        self.assertEqual(blocks[0]["symbol"], "BTCTMN")
        self.assertIn("LIVE_ARMED", blocks[0]["reason"])

    def test_execute_sends_a_post_only_buy_only_when_live_armed(self):
        client = RecordingClient()
        portfolio = Portfolio(5_000_000)
        state = {"client_seq": 0}
        blocks = []
        action = Action("BTCTMN", "BUY", "LIMIT", 0.02, "setup", "llm", "down")
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, last_price=100)
        fill, request = _execute(
            _cfg(), client, portfolio, action, {}, {"BTCTMN": 100}, {"BTCTMN": rule},
            state, True, {}, unavailable=set(), ops_state="LIVE_ARMED", guard_blocks=blocks,
        )
        self.assertEqual(blocks, [])
        self.assertEqual(len(client.placed), 1)
        self.assertEqual(client.placed[0]["side"], "BUY")
        self.assertEqual(client.placed[0]["order_type"], "LIMIT")
        self.assertIn("/v1/account/orders", request["url"])
        self.assertGreater(state["client_seq"], 0)

    def test_no_module_references_a_withdraw_endpoint(self):
        needle = "with" + "draw"
        root = Path("trader")
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn(needle, text, str(path))

    def test_random_llm_output_cannot_raise_size_or_flip_a_veto_into_a_buy(self):
        rng = random.Random(11)
        cfg = _cfg()
        risk = RiskManager(cfg)
        agent = TradingAgent(cfg)
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        portfolio = Portfolio(5_000_000)
        view = risk.view(
            portfolio, {"BTCTMN": 100}, now,
            peak_equity=portfolio.cash, day_start_equity=portfolio.cash,
            kill_switch=False, kill_reason="",
        )
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, last_price=100)
        offered = 0.04
        candidate = Candidate("BTCTMN", "uptrend_pullback", offered, 0.7, 0.009, 0.005, 0.02, -0.008, 100, 0, {})
        menu = [hold_action(), MenuAction("buy_BTCTMN", "BUY", "BTCTMN", offered, "buy the bid")]
        for _ in range(50):
            size = rng.choice([0.01, 0.2, 0.9, 2, -0.1, None, "large"])
            veto = rng.choice([True, False, 1, 0])
            payload = {
                "veto_all": rng.choice([False, False, True]),
                "regime": rng.choice(["trend", "hostile", "chop"]),
                "comment": "noise",
                "decisions": [{
                    "action": rng.choice(["BUY", "SELL", "HOLD", "YOLO"]),
                    "symbol": rng.choice(["BTCTMN", "TONTMN"]),
                    "size_fraction": size,
                    "confidence": rng.random(),
                    "veto": veto,
                    "rationale": "24h return is the cited number" if rng.random() > 0.3 else "",
                }],
            }
            raw = json.dumps(payload) if rng.random() > 0.15 else rng.choice(["", "nope", "{", "[]"])
            decision = agent._parse_or_fallback(raw, [candidate])
            planned = risk.plan(
                portfolio=portfolio, agent=decision, candidates=[candidate],
                marks={"BTCTMN": 100}, rules={"BTCTMN": rule}, unavailable=set(),
                now=now, view=view, order_times=[], cooldowns={}, now_unix=now.timestamp(),
            )
            vetoed = {item.symbol for item in decision.decisions if item.veto and item.symbol}
            if decision.veto_all:
                self.assertFalse(any(item.side == "BUY" for item in planned.actions))
            for action in planned.actions:
                if action.side != "BUY":
                    continue
                self.assertLessEqual(action.size_fraction, offered + 1e-12)
                self.assertLessEqual(action.size_fraction, 0.12)
                self.assertNotIn(action.symbol, vetoed)
            menu_raw = json.dumps({
                "action_id": "buy_BTCTMN",
                "rationale": "the pullback is 0.8 percent",
                "size_fraction": rng.choice([0.5, 0.99, 5]),
                "veto": rng.choice([True, False]),
            })
            chosen, note = parse_menu_choice(menu_raw, menu)
            if '"veto": true' in menu_raw:
                self.assertEqual(chosen.kind, "HOLD")
                self.assertEqual(note, "veto")
            else:
                self.assertEqual(chosen.size_fraction, offered)
                self.assertLessEqual(chosen.size_fraction, 0.12)


if __name__ == "__main__":
    unittest.main()
