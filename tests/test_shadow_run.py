"""Shadow-run findings: warm-up survives a candle refresh, spend stays a float."""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from trader.dashboard import _llm_panel
from trader.engine.runtime import (
    _cycle_llm_usd,
    _load_history,
    _restore_warmup,
    _seed_warmup,
    _warmup_snapshot,
    _zscore_n,
    merge_bars,
)
from trader.engine.state import _trim_liquidity
from trader.strategy.liquidity import _quote_volume, _volume_window, measure_liquidity
from trader.types import Bar
from trader.wallex.mcp import WallexMcp, candle_data_path


def _bars(n: int, volume: float = 2.0, start: int = 0) -> list[Bar]:
    rows = []
    price = 100.0
    for i in range(n):
        rows.append(Bar(start + i * 3600, price, price, price, price, volume))
    return rows


class WarmupTests(unittest.TestCase):
    def test_candle_refresh_keeps_depth_and_merges_a_short_refetch(self):
        state = {
            "liquidity": {
                "BTCTMN": {
                    "depth": [float(i) for i in range(10)],
                    "spread": [8.0] * 10,
                    "volume": [3.0] * 10,
                    "volume_ts": list(range(10)),
                }
            }
        }
        previous = {"BTCTMN": _bars(40)}
        cfg = {"runtime": {"history_days": 29}}

        class Client:
            def get_ohlcv(self, symbol, timeframe, start, end):
                state["liquidity"].clear()
                return _bars(4, start=36 * 3600)

        snap = _warmup_snapshot(state)
        hourly, micro = _load_history(
            Client(), ["BTCTMN"], cfg, previous=previous, previous_micro=previous,
        )
        _restore_warmup(state, snap)
        self.assertGreaterEqual(len(hourly["BTCTMN"]), 40)
        self.assertGreaterEqual(len(micro["BTCTMN"]), 40)
        self.assertEqual(len(state["liquidity"]["BTCTMN"]["depth"]), 10)
        self.assertEqual(_zscore_n(state, ["BTCTMN"]), 9)
        self.assertEqual(hourly["BTCTMN"][-1].time, 39 * 3600)

    def test_hourly_history_seeds_the_warm_up_immediately(self):
        state = {"liquidity": {}}
        hourly = {
            "BTCTMN": _bars(40),
            "ETHTMN": _bars(40),
            "SOLTMN": _bars(4),
        }
        cfg = {"runtime": {"history_days": 29}, "liquidity": {"history": 24}, "ops": {"zscore_min_n": 18}}
        _seed_warmup(state, hourly, cfg)
        self.assertGreaterEqual(_zscore_n(state, ["BTCTMN", "ETHTMN"]), 18)
        self.assertLess(_zscore_n(state, ["SOLTMN"]), 18)
        # A name that just joined does not pull the seeded cohort under the gate.
        state["liquidity"]["ADATMN"] = {"depth": [1.0]}
        self.assertGreaterEqual(_zscore_n(state, ["BTCTMN", "ETHTMN", "ADATMN"]), 18)
        saved = _trim_liquidity(state["liquidity"])
        self.assertGreaterEqual(saved["BTCTMN"]["seed_n"], 18)

        class Client:
            def get_ohlcv(self, symbol, timeframe, start, end):
                state["liquidity"].clear()
                return _bars(3, start=37 * 3600)

        snap = _warmup_snapshot(state)
        merged, _micro = _load_history(
            Client(), ["BTCTMN"], cfg, previous={"BTCTMN": hourly["BTCTMN"]},
            previous_micro={"BTCTMN": hourly["BTCTMN"]},
        )
        _restore_warmup(state, snap)
        _seed_warmup(state, merged, cfg)
        self.assertGreaterEqual(len(merged["BTCTMN"]), 40)
        self.assertGreaterEqual(_zscore_n(state, ["BTCTMN"]), 18)

    def test_a_new_symbol_does_not_zero_the_warmed_cohort(self):
        state = {
            "liquidity": {
                "BTCTMN": {"depth": [1.0] * 11},
                "ETHTMN": {"depth": [1.0] * 11},
                "SOLTMN": {"depth": [1.0]},
            }
        }
        self.assertEqual(_zscore_n(state, ["BTCTMN", "ETHTMN", "SOLTMN"]), 10)
        locked = {
            "liquidity": {
                "BTCTMN": {"depth": [1.0] * 19},
                "ETHTMN": {"depth": [1.0] * 19},
            }
        }
        self.assertEqual(_zscore_n(locked, ["BTCTMN", "ETHTMN"]), 18)

    def test_short_bars_do_not_shrink_the_volume_window(self):
        bars = _bars(40)
        history: dict = {}
        window = _volume_window(history, bars, 26)
        self.assertEqual(window, _quote_volume(bars, 26))
        short = _volume_window(history, bars[-3:], 26)
        self.assertEqual(len(short), 26)
        self.assertEqual(short, _quote_volume(bars, 26))
        untouched = measure_liquidity(bars, {})
        stored = measure_liquidity(bars, {}, history={})
        self.assertEqual(untouched.volume_z, stored.volume_z)

    def test_saved_state_keeps_the_volume_window(self):
        trimmed = _trim_liquidity({
            "BTCTMN": {
                "depth": [1.0] * 60,
                "spread": [1.0],
                "volume": [2.0] * 60,
                "volume_ts": list(range(60)),
            }
        })
        self.assertEqual(len(trimmed["BTCTMN"]["depth"]), 48)
        self.assertEqual(len(trimmed["BTCTMN"]["volume"]), 48)
        self.assertEqual(trimmed["BTCTMN"]["volume_ts"][0], 12)

    def test_merge_keeps_the_longer_series(self):
        previous = _bars(40)
        self.assertEqual(len(merge_bars(previous, [])), 40)
        self.assertEqual(len(merge_bars(previous, _bars(4, start=36 * 3600))), 40)
        self.assertEqual(len(merge_bars(previous, _bars(50))), 50)


class SpendTests(unittest.TestCase):
    def test_cycle_cost_stays_a_precise_float(self):
        cost, today, total = _cycle_llm_usd([
            {"usd": 0.000091, "spent_usd": 0.000091, "spent_today_usd": 0.000091},
            {"skipped": "menu is hold only"},
        ])
        self.assertAlmostEqual(cost, 0.000091, places=9)
        self.assertAlmostEqual(today, 0.000091, places=9)
        self.assertAlmostEqual(total, 0.000091, places=9)
        payload = json.loads(json.dumps({
            "llm_usd": cost,
            "llm_usd_today": today,
            "llm_usd_total": total,
        }))
        self.assertIsInstance(payload["llm_usd"], float)
        self.assertAlmostEqual(payload["llm_usd"], 0.000091, places=9)
        self.assertNotEqual(payload["llm_usd"], 0)

    def test_dashboard_shows_six_decimals(self):
        html = _llm_panel(
            {"llm_usd_today": 0.000091, "llm_tier": "routine", "llm_model": "m", "llm_source": "llm"},
            0.000091,
        )
        self.assertIn("$0.000091", html)
        self.assertNotIn("$0.0000<", html)
        cap = _llm_panel({}, 2.5)
        self.assertIn("$2.50", cap)


class McpPathTests(unittest.TestCase):
    def test_code_1203_keeps_candles_on_rest_and_does_not_retry(self):
        calls = {"n": 0}

        def urlopen(request, timeout=0):
            calls["n"] += 1
            body = b'{"code": 1203}'
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(body),
            )

        mcp = WallexMcp(url="https://api.wallex.ir/mcp", api_key="not-a-key")
        with patch("trader.wallex.mcp.urllib.request.urlopen", urlopen):
            self.assertIsNone(mcp.call_tool("wallex_basic_candle_history", {"symbol": "BTCTMN"}))
            seen = calls["n"]
            self.assertIsNone(mcp.call_tool("wallex_basic_candle_history", {"symbol": "BTCTMN"}))
        self.assertEqual(calls["n"], seen)
        self.assertGreaterEqual(seen, 1)
        self.assertTrue(mcp.disabled)
        self.assertIn("candles stay on REST", mcp.disabled_reason)
        self.assertNotIn("with" + "draw", mcp.disabled_reason)
        path = candle_data_path(mcp)
        self.assertIn("REST /v1/udf/history", path)
        self.assertIn("candles stay on REST", path)

    def test_nested_error_code_1203_is_the_same_refusal(self):
        def urlopen(request, timeout=0):
            body = b'{"result": {"error_code": 1203}}'
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(body),
            )

        mcp = WallexMcp(url="https://api.wallex.ir/mcp", api_key="not-a-key")
        with patch("trader.wallex.mcp.urllib.request.urlopen", urlopen):
            self.assertIsNone(mcp.call_tool("wallex_basic_candle_history", {}))
        self.assertTrue(mcp.disabled)
        self.assertIn("REST /v1/udf/history", candle_data_path(mcp))


if __name__ == "__main__":
    unittest.main()
