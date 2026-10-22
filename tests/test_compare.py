"""HEAD replay of the stored candles must match the recorded trade list."""

import tempfile
import unittest
from pathlib import Path

from trader.compare import diff_trades, replay, scale_fees
from trader.config import load_config


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

    def test_head_replay_matches_the_recorded_trades(self):
        cfg = load_config("config/default.yaml")
        out = Path(tempfile.mkdtemp())
        summary = replay(cfg, Path("results/backtest"), out)
        recorded = (Path("results/backtest") / "trades.csv").read_bytes()
        self.assertEqual((out / "trades.csv").read_bytes(), recorded)
        self.assertEqual(summary["closed_trades"], 17)
        self.assertEqual(summary["return_pct"], -0.14)
        self.assertAlmostEqual(summary["ending_equity"], 4993023.91105763, places=4)


if __name__ == "__main__":
    unittest.main()
