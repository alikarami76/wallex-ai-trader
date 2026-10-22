"""Offline scorecard: stored candles, no keys, shadow rows included."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from trader.config import load_config
from trader.scorecard import build_scorecard, path_return, seal_signals, write_scorecard
from trader.strategy.parser import AgentDecision
from trader.strategy.signals import Candidate, SymbolFeatures
from trader.types import Bar


def _bars(start: int, closes: list[float], *, high_pad: float = 0.0, low_pad: float = 0.0) -> list[Bar]:
    bars = []
    for index, close in enumerate(closes):
        bars.append(Bar(start + index * 3600, close, close + high_pad, close - low_pad, close, 1))
    return bars


def _feature(symbol: str, close: float, bar_time: int, signal: bool = True) -> SymbolFeatures:
    return SymbolFeatures(
        symbol=symbol, bar_time=bar_time, close=close, ema_fast=close, ema_slow=close * 0.99,
        roc_pullback=-0.008, roc_trend=0.02, atr_pct=0.004, rsi=48, spread_bps=10,
        imbalance=0.1, micro_return=0.0, expected_gross=0.009, round_trip_cost=0.0058,
        signal=signal, blockers=[], score=0.7,
    )


class _Action:
    def __init__(self, symbol, side, size):
        self.symbol = symbol
        self.side = side
        self.size_fraction = size


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("config/default.yaml")

    def test_stop_trail_and_time_are_the_exits(self):
        start = 1_700_000_000
        crash = _bars(start, [100] + [90] * 3)
        stopped = path_return(crash, start, 100, self.cfg)
        self.assertEqual(stopped["exit_reason"], "stop")
        self.assertAlmostEqual(stopped["exit_price"], 100 * (1 - 0.045))

        # Arm the trail (+2.5%) and print a low 1.2% under that peak.
        trail = path_return(_bars(start, [100, 103], low_pad=2), start, 100, self.cfg)
        self.assertEqual(trail["exit_reason"], "trail")

        flat = _bars(start, [100 + index * 0.01 for index in range(30)])
        held = path_return(flat, start, 100, self.cfg)
        self.assertEqual(held["exit_reason"], "time")
        self.assertLess(held["net_return"], 0.01)

    def test_veto_shrink_and_accept_against_the_rules_baseline(self):
        start = 1_700_000_000
        # A steady climb that survives to the time stop.
        closes = [100 + index * 0.2 for index in range(30)]
        candles = {"BTCTMN": _bars(start, closes)}
        feature = _feature("BTCTMN", 100, start)
        decision = AgentDecision(source="llm", veto_all=False, regime="trend", comment="hold", decisions=[])
        candidate = Candidate("BTCTMN", "uptrend_pullback", 0.10, 0.7, 0.009, 0.005, 0.02, -0.008, 100, start, {})
        accepted = seal_signals(
            [feature], decision, type("P", (), {"actions": [_Action("BTCTMN", "BUY", 0.10)]})(),
            [candidate], 1_000_000, prompt="prompt-a", response="response-a", mode="shadow",
        )
        shrunk = seal_signals(
            [feature], decision, type("P", (), {"actions": [_Action("BTCTMN", "BUY", 0.05)]})(),
            [candidate], 1_000_000, prompt="prompt-a", response="response-a", mode="shadow",
        )
        vetoed = seal_signals(
            [feature], decision, type("P", (), {"actions": []})(),
            [candidate], 1_000_000, prompt="prompt-a", response="", mode="shadow",
        )
        self.assertEqual(accepted[0]["disposition"], "accepted")
        self.assertEqual(shrunk[0]["disposition"], "shrunk")
        self.assertEqual(shrunk[0]["size_mult"], 0.5)
        self.assertEqual(vetoed[0]["disposition"], "vetoed")
        self.assertEqual(vetoed[0]["action"], "HOLD")
        self.assertEqual(len(accepted[0]["prompt_hash"]), 64)
        self.assertNotEqual(accepted[0]["prompt_hash"], vetoed[0]["response_hash"])
        self.assertIn("bar_time", accepted[0]["signal"])

        rows = [
            {"mode": "shadow", "event": "startup"},
            {"mode": "shadow", "sealed": accepted},
            {"mode": "shadow", "sealed": shrunk, "features": [feature.compact()]},
            {"mode": "live", "sealed": vetoed},
        ]
        # Same bar is deduped per disposition, so each bucket keeps one trade.
        report = build_scorecard(rows, candles, self.cfg)
        self.assertEqual(report["shadow_rows"], 3)
        self.assertEqual(report["buckets"]["accepted"]["n"], 1)
        self.assertEqual(report["buckets"]["shrunk"]["n"], 1)
        self.assertEqual(report["buckets"]["vetoed"]["n"], 1)
        self.assertAlmostEqual(report["buckets"]["vetoed"]["taken_pnl_tmn"], 0.0)
        self.assertGreater(report["buckets"]["accepted"]["taken_pnl_tmn"], 0)
        self.assertAlmostEqual(
            report["buckets"]["shrunk"]["taken_pnl_tmn"] * 2,
            report["buckets"]["accepted"]["taken_pnl_tmn"],
            delta=0.05,
        )
        self.assertGreater(report["buckets"]["vetoed"]["rules_pnl_tmn"], 0)
        self.assertNotIn("WALLEX", json.dumps(report))

    def test_cli_writer_is_offline(self):
        start = 1_700_000_000
        root = Path(tempfile.mkdtemp())
        log_dir = root / "journal"
        candle_dir = root / "candles"
        log_dir.mkdir()
        candle_dir.mkdir()
        (candle_dir / "BTCTMN.csv").write_text(
            "time,open,high,low,close,volume\n" + "\n".join(
                f"{start + index * 3600},100,101,99,{100 + index * 0.1},1" for index in range(30)
            ),
            encoding="utf-8",
        )
        feature = _feature("BTCTMN", 100, start)
        decision = AgentDecision(source="fallback", veto_all=True, regime="invalid", comment="veto", decisions=[])
        candidate = Candidate("BTCTMN", "uptrend_pullback", 0.12, 0.7, 0.009, 0.005, 0.02, -0.008, 100, start, {})
        sealed = seal_signals(
            [feature], decision, type("P", (), {"actions": []})(),
            [candidate], 5_000_000, prompt="", response="", mode="shadow",
        )
        with (log_dir / "decisions.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"mode": "shadow", "sealed": sealed}) + "\n")
        saved = {key: os.environ.get(key) for key in ("WALLEX_API_KEY", "LLM_API_KEY")}
        os.environ.pop("WALLEX_API_KEY", None)
        os.environ.pop("LLM_API_KEY", None)
        try:
            report = write_scorecard(log_dir, candle_dir, self.cfg, root / "out")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(report["buckets"]["vetoed"]["n"], 1)
        self.assertEqual(report["shadow_rows"], 1)
        self.assertTrue((root / "out" / "scorecard.md").exists())
        self.assertTrue((root / "out" / "scorecard.json").exists())
        text = (root / "out" / "scorecard.md").read_text(encoding="utf-8")
        self.assertIn("Shadow-mode rows: 1", text)
        self.assertIn("vetoed", text)


if __name__ == "__main__":
    unittest.main()
