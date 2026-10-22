"""Strategy profiles and take-profit exits."""

import unittest

from trader.config import load_config
from trader.risk.manager import RiskManager
from trader.strategy.profiles import apply_strategy_profile
from trader.engine.portfolio import Position


class ProfileTests(unittest.TestCase):
    def test_default_stays_on_uptrend_pullback(self):
        cfg = load_config("config/default.yaml")
        self.assertEqual(cfg["strategy"]["name"], "uptrend_pullback")
        self.assertEqual(float(cfg["risk"]["stop_loss_pct"]), 0.045)

    def test_fast_scalp_sets_maker_tp_and_short_hold(self):
        cfg = apply_strategy_profile(load_config("config/default.yaml"), "fast_scalp")
        self.assertEqual(cfg["strategy"]["name"], "fast_scalp")
        self.assertEqual(cfg["execution"]["entry"], "limit_maker")
        self.assertEqual(float(cfg["risk"]["take_profit_pct"]), 0.012)
        self.assertEqual(float(cfg["risk"]["stop_loss_pct"]), 0.008)
        self.assertEqual(float(cfg["risk"]["time_stop_hours"]), 2)

    def test_take_profit_fires_before_the_trail(self):
        cfg = apply_strategy_profile(load_config("config/default.yaml"), "fast_scalp")
        risk = RiskManager(cfg)
        position = Position(
            symbol="BTCTMN", qty=1, entry_price=100, entry_time=0,
            cost_tmn=100, stop_price=99.2, peak_price=100.5, setup="fast_scalp", reason="x",
        )
        action = risk._forced_exit("BTCTMN", position, 101.3, now_unix=100, view=None)
        self.assertIsNotNone(action)
        self.assertEqual(action.source, "take_profit")


if __name__ == "__main__":
    unittest.main()
