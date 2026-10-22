import unittest

from trader.strategy.signals import build_candidates, evaluate_symbol, round_trip_cost
from trader.types import Bar
from trader.wallex.client import aggregate_bars, median_spacing


def base_cfg(**overrides):
    cfg = {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {"entry": "marketable_limit", "exit": "limit_maker"},
        "universe": {"max_spread_bps": 22},
        "strategy": {
            "ema_fast": 8,
            "ema_slow": 24,
            "pullback_bars": 4,
            "pullback_roc_max": -0.004,
            "pullback_roc_min": -0.025,
            "trend_bars": 24,
            "trend_roc_min": 0.01,
            "rsi_period": 14,
            "atr_period": 14,
            "max_atr_pct": 0.03,
            "expected_gross": 0.009,
            "edge_multiple": 1.2,
            "min_micro_return": -0.004,
            "entry_size_fraction": 0.12,
            "imbalance_block": -0.55,
        },
    }
    cfg["strategy"].update(overrides)
    return cfg


def rising_then_dip():
    price = 100.0
    bars = []
    for i in range(36):
        drift = 0.0022 if i < 32 else -0.0022
        nxt = price * (1 + drift)
        bars.append(Bar(i * 3600, price, max(price, nxt), min(price, nxt), nxt, 1))
        price = nxt
    return bars


class SignalTests(unittest.TestCase):
    def test_fee_gate_blocks_when_the_edge_cannot_pay_the_round_trip(self):
        bars = rising_then_dip()
        costly = base_cfg(expected_gross=0.001, edge_multiple=1.2)
        row = evaluate_symbol(
            "BTCTMN", bars, costly, spread_bps=10, imbalance=0.1, micro_return=0.0,
            maker_fee=0.0025, taker_fee=0.003,
        )
        self.assertFalse(row.signal)
        self.assertTrue(any("edge" in item for item in row.blockers))
        self.assertEqual(build_candidates([row], costly), [])

    def test_round_trip_uses_taker_entry_and_maker_exit(self):
        cost = round_trip_cost(base_cfg(), 0.0025, 0.003)
        self.assertAlmostEqual(cost, 0.003 + 0.0025 + 0.0003)

    def test_setup_can_pass_when_trend_pullback_and_cost_line_up(self):
        bars = rising_then_dip()
        row = evaluate_symbol(
            "BTCTMN", bars, base_cfg(), spread_bps=10, imbalance=0.2, micro_return=0.0,
            maker_fee=0.0025, taker_fee=0.003,
        )
        if not row.signal:
            self.fail("expected a signal, blockers=" + ", ".join(row.blockers))
        candidates = build_candidates([row], base_cfg())
        self.assertEqual(candidates[0].symbol, "BTCTMN")
        self.assertLessEqual(candidates[0].size_fraction, 0.12)

    def test_crash_pullback_and_wide_spread_are_refused(self):
        bars = rising_then_dip()
        # overwrite the last 4 closes with a 6% crash
        price = bars[-5].close
        for i, bar in enumerate(bars[-4:]):
            price *= 0.985
            bars[-4 + i] = Bar(bar.time, bar.open, bar.high, price, price, 1)
        row = evaluate_symbol(
            "BTCTMN", bars, base_cfg(), spread_bps=10, imbalance=0.0, micro_return=None,
            maker_fee=0.0025, taker_fee=0.003,
        )
        self.assertFalse(row.signal)
        wide = evaluate_symbol(
            "BTCTMN", rising_then_dip(), base_cfg(), spread_bps=40, imbalance=0.0,
            micro_return=None, maker_fee=0.0025, taker_fee=0.003,
        )
        self.assertTrue(any("spread" in item for item in wide.blockers))

    def test_aggregate_minutes_into_hours(self):
        minutes = []
        start = 1_699_999_200  # exact hour boundary
        for i in range(180):
            price = 100 + (i % 7)
            minutes.append(Bar(start + i * 60, price, price + 1, price - 1, price + 0.5, 2))
        hours = aggregate_bars(minutes, 3600)
        self.assertEqual(len(hours), 3)
        self.assertEqual(hours[0].open, minutes[0].open)
        self.assertEqual(hours[0].close, minutes[59].close)
        self.assertEqual(hours[0].high, max(b.high for b in minutes[:60]))
        self.assertEqual(hours[0].low, min(b.low for b in minutes[:60]))
        self.assertAlmostEqual(hours[0].volume, 120)
        self.assertEqual(median_spacing(minutes), 60)


if __name__ == "__main__":
    unittest.main()
