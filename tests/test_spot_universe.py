import unittest

from trader.engine.universe import is_hard_denied, select_universe, symbol_block
from trader.risk.manager import RiskManager
from trader.strategy.parser import AgentDecision, ParsedDecision
from trader.strategy.signals import Candidate
from trader.timeutil import TEHRAN
from trader.types import MarketRule
from trader.wallex.client import WallexClient, WallexError, assert_spot_request
from datetime import datetime


def _rule(symbol: str, base: str, quote: str = "TMN") -> MarketRule:
    return MarketRule(
        symbol, base, quote, 0, 2, 0.01, 50000,
        quote_volume_24h=50_000_000_000, bid_price=100, ask_price=100.01, last_price=100,
    )


def _cfg():
    return {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {"entry": "marketable_limit", "exit": "limit_maker"},
        "universe": {
            "allowlist": ["BTCTMN", "TONTMN", "USDTTMN"],
            "hard_deny_bases": ["TON", "NEAR", "FET", "INJ", "TAO", "WIF", "BONK", "PENDLE", "RENDER"],
            "candidates": ["BTCTMN", "TONTMN", "TONUSDT", "PEPETMN", "RENDERUSDT", "ETHTMN", "USDTTMN"],
            "max_symbols": 6,
            "min_quote_volume_tmn": 1,
            "max_spread_bps": 50,
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
        },
        "liquidation": {
            "deadline": "2026-10-23T23:50:00+03:30",
            "start_before_minutes": 60,
            "aggressive_before_minutes": 30,
            "flat_by": "2026-10-23T23:30:00+03:30",
            "min_hours_to_enter": 8,
        },
    }


class SpotPathTests(unittest.TestCase):
    def test_only_the_spot_order_endpoint_can_be_called(self):
        assert_spot_request("POST", "/v1/account/orders")
        assert_spot_request("DELETE", "/v1/account/orders/SC1")
        assert_spot_request("GET", "/v1/account/orders/SC1")
        assert_spot_request("GET", "/v1/prop/packages/183")
        assert_spot_request("GET", "/v2/depth/all")
        refused = [
            ("POST", "/v1/account/margin/orders"),
            ("POST", "/v1/otc/orders"),
            ("POST", "/v1/futures/orders"),
            ("POST", "/v1/easy-trade"),
            ("POST", "/v1/account/credit-margin/orders"),
            ("DELETE", "/v1/margin/orders/1"),
            ("PUT", "/v1/account/orders"),
            ("POST", "/v1/account/orders/extra"),
            ("GET", "/v1/account/leverage"),
        ]
        for method, path in refused:
            with self.assertRaises(WallexError, msg=path):
                assert_spot_request(method, path)
        client = WallexClient(dry_run=True, api_key="test")
        with self.assertRaises(WallexError):
            client._request("POST", "/v1/account/leverage/orders", body={"symbol": "BTCTMN"}, auth=True, is_order=True)


class AllowlistTests(unittest.TestCase):
    def _rules(self):
        names = [
            ("BTCTMN", "BTC", "TMN"),
            ("TONTMN", "TON", "TMN"),
            ("TONUSDT", "TON", "USDT"),
            ("PEPETMN", "PEPE", "TMN"),
            ("RENDERUSDT", "RENDER", "USDT"),
            ("ETHTMN", "ETH", "TMN"),
            ("USDTTMN", "USDT", "TMN"),
            ("WIFTMN", "WIF", "TMN"),
        ]
        return {symbol: _rule(symbol, base, quote) for symbol, base, quote in names}

    def test_allowlist_intersects_the_live_list_and_hard_deny_wins(self):
        cfg = _cfg()
        rules = self._rules()
        spreads = {symbol: 5.0 for symbol in rules}
        chosen = select_universe(cfg, rules, set(), spreads)
        self.assertEqual(chosen, ["BTCTMN", "USDTTMN"])
        self.assertIsNotNone(symbol_block("PEPETMN", cfg, set(), rules["PEPETMN"]))
        self.assertIn("allowlist", symbol_block("PEPETMN", cfg, set(), rules["PEPETMN"]))
        for symbol in ("TONTMN", "TONUSDT", "RENDERUSDT", "WIFTMN"):
            self.assertTrue(is_hard_denied(symbol, cfg, rules.get(symbol)))
            self.assertIn("hard-denied", symbol_block(symbol, cfg, set(), rules.get(symbol)))
        banned = select_universe(cfg, rules, {"BTCTMN", "USDTTMN"}, spreads)
        self.assertEqual(banned, [])
        self.assertIn("banned", symbol_block("BTCTMN", cfg, {"BTCTMN"}, rules["BTCTMN"]))
        self.assertTrue(is_hard_denied("BONKUSDT", {"universe": {}}))

    def test_risk_refuses_a_hard_denied_buy_even_when_the_package_allows_it(self):
        cfg = _cfg()
        risk = RiskManager(cfg)
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TEHRAN)
        from trader.engine.portfolio import Portfolio

        portfolio = Portfolio(5_000_000)
        view = risk.view(
            portfolio, {}, now, peak_equity=portfolio.cash, day_start_equity=portfolio.cash,
            kill_switch=False, kill_reason="",
        )
        decision = AgentDecision(
            source="llm", veto_all=False, regime="menu", comment="buy ton",
            decisions=[ParsedDecision("BUY", "TONTMN", 0.02, 0.8, False, "package did not ban it")],
            menu_bound=True, picked_id="buy_TONTMN",
        )
        candidate = Candidate("TONTMN", "uptrend_pullback", 0.12, 0.7, 0.009, 0.005, 0.02, -0.008, 100, 0, {})
        planned = risk.plan(
            portfolio=portfolio, agent=decision, candidates=[candidate],
            marks={"TONTMN": 100}, rules={"TONTMN": _rule("TONTMN", "TON")},
            unavailable=set(), now=now, view=view, order_times=[], cooldowns={}, now_unix=now.timestamp(),
        )
        self.assertFalse(any(action.side == "BUY" for action in planned.actions))
        self.assertTrue(any("hard-denied" in row["reason"] for row in planned.rejected))


if __name__ == "__main__":
    unittest.main()
