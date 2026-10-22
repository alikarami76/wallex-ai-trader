"""Random-entry baseline keeps the denylist and is stable for a seed."""

import unittest

from trader.baseline import random_entry_baseline
from trader.config import load_config
from trader.types import Bar


def _series(start: int, closes: list[float], *, low_scale: float = 0.99, high_scale: float = 1.01) -> list[Bar]:
    rows = []
    for index, close in enumerate(closes):
        rows.append(Bar(
            start + index * 3600,
            close,
            close * high_scale,
            close * low_scale,
            close,
            1.0,
        ))
    return rows


class BaselineTests(unittest.TestCase):
    def test_seed_is_stable_and_the_denylist_is_not_traded(self):
        cfg = load_config("config/default.yaml")
        start = 1_700_000_000
        up = [100.0 * (1.02 ** index) for index in range(40)]
        flat = [100.0 for _ in range(40)]
        candles = {
            "BTCTMN": _series(start, flat),
            "TONTMN": _series(start, up, high_scale=1.05),
        }
        kwargs = dict(
            n_runs=30,
            seed=11,
            target_entries=8,
            observed_return_pct=0.0,
            observed_sharpe=0.0,
        )
        first = random_entry_baseline(candles, cfg, **kwargs)
        second = random_entry_baseline(candles, cfg, **kwargs)
        self.assertEqual(first["return_percentile"], second["return_percentile"])
        self.assertEqual(first["return_quantiles"], second["return_quantiles"])
        self.assertEqual(first["symbols"], ["BTCTMN"])
        self.assertNotIn("TONTMN", first["symbols_entered"])
        self.assertEqual(first["n_runs"], 30)
        self.assertIn("p50", first["return_quantiles"])


if __name__ == "__main__":
    unittest.main()
