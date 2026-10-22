"""HEAD replay of the stored candles must match the recorded trade list."""

import tempfile
import unittest
from pathlib import Path

from trader.compare import diff_trades, replay, scale_fees
from trader.config import load_config
from trader.engine.backtest import _exits, _fill_resting
from trader.engine.portfolio import Portfolio
from trader.types import Bar, MarketRule


class CompareTests(unittest.TestCase):
    def test_fee_scaling_copies_the_config(self):
        cfg = {"fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3}}
        scaled = scale_fees(cfg, 1.5, 5)
        self.assertEqual(cfg["fees"]["maker_rate"], 0.0025)
        self.assertEqual(cfg["fees"]["slippage_bps"], 3)
        self.assertAlmostEqual(scaled["fees"]["maker_rate"], 0.00375)
        self.assertAlmostEqual(scaled["fees"]["taker_rate"], 0.0045)
        self.assertEqual(scaled["fees"]["slippage_bps"], 8)

    def test_trade_diff_detects_a_changed_row(self):
        text = "timestamp,symbol\n2026,BTCTMN\n"
        same = diff_trades(text, text)
        self.assertTrue(same["identical"])
        changed = diff_trades(text, "timestamp,symbol\n2026,ETHTMN\n")
        self.assertFalse(changed["identical"])
        self.assertEqual(changed["deltas"][0]["row"], 1)

    def test_bar_touching_stop_and_target_resolves_stop_first(self):
        cfg = load_config("config/default.yaml")
        portfolio = Portfolio(5_000_000)
        rule = MarketRule("BTCTMN", "BTC", "TMN", 0, 8, 1e-8, 50_000)
        portfolio.buy(
            symbol="BTCTMN", price=100, qty=10, fee_rate=0.0025, time=1_000,
            stop_price=95.5, setup="uptrend_pullback", reason="seed",
            client_id="SC1", order_type="LIMIT",
        )
        position = portfolio.positions["BTCTMN"]
        position.peak_price = 103
        bar = Bar(5_000, 100, 110, 95, 100, 1)
        indexed = {"BTCTMN": {5_000: bar}}
        state = {
            "client_seq": 1,
            "cooldowns": {},
            "order_times": [],
            "resting_exit": {
                "symbol": "BTCTMN",
                "price": 110,
                "reason": "time stop",
                "signal_time": 1_000,
            },
        }
        journal = _Capture()
        _fill_resting(cfg, portfolio, indexed, 5_000, {"BTCTMN": rule}, state, journal, {})
        _exits(cfg, portfolio, {}, indexed, 5_000, {"BTCTMN": rule}, state, journal, None)
        reasons = [row.get("reason") for row in journal.trades]
        self.assertEqual(reasons, ["stop"])
        self.assertNotIn("BTCTMN", portfolio.positions)

    def test_head_replay_matches_the_recorded_trades(self):
        cfg = load_config("config/default.yaml")
        out = Path(tempfile.mkdtemp())
        summary = replay(cfg, Path("results/backtest"), out)
        recorded = (Path("results/backtest") / "trades.csv").read_bytes()
        self.assertEqual((out / "trades.csv").read_bytes(), recorded)
        self.assertEqual(summary["closed_trades"], 23)
        self.assertEqual(summary["return_pct"], -0.335)
        self.assertAlmostEqual(summary["ending_equity"], 4983233.3104847735, places=4)


class _Capture:
    def __init__(self):
        self.trades = []

    def decision(self, *_args, **_kwargs):
        return None

    def trade(self, row):
        self.trades.append(row)

    def equity(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
