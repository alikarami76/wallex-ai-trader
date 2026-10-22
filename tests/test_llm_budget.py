import json
import os
import unittest
from pathlib import Path

from trader.strategy.agent import LlmBudget, TradingAgent, llm_settings, request_body
from trader.strategy.signals import Candidate


def _candidate(symbol: str) -> Candidate:
    return Candidate(
        symbol=symbol, setup="uptrend_pullback", size_fraction=0.12, score=0.7,
        expected_gross=0.009, round_trip_cost=0.0058, roc_trend=0.02, roc_pullback=-0.008,
        close=100, bar_time=1_700_000_000, facts={},
    )


class LlmBudgetTests(unittest.TestCase):
    def test_request_omits_temperature_unless_asked(self):
        settings = llm_settings({"llm": {}})
        body = request_body("gpt-5-mini", [{"role": "user", "content": "{}"}], settings)
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)
        self.assertEqual(body["model"], "gpt-5-mini")
        settings["send_temperature"] = True
        self.assertIn("temperature", request_body("gpt-5-mini", [], settings))

    def test_one_call_per_cycle_then_cache_and_hourly_cap(self):
        path = Path("data/test_llm_usage.json")
        if path.exists():
            path.unlink()
        calls = {"n": 0, "bodies": []}

        def post(url, headers, body):
            calls["n"] += 1
            calls["bodies"].append(body)
            content = json.dumps({
                "veto_all": False,
                "regime": "trend",
                "comment": "btc over eth",
                "decisions": [{
                    "action": "BUY", "symbol": "BTCTMN", "size_fraction": 0.08,
                    "confidence": 0.7, "veto": False, "rationale": "best 24h trend",
                }],
            })
            return content, {"prompt_tokens": 400, "completion_tokens": 80, "model": "gpt-5-mini"}

        cfg = {"llm": {"usage_path": str(path), "max_llm_calls_per_hour": 1, "cache_seconds": 3600}}
        os.environ["LLM_BASE_URL"] = "https://ai.boxapi.ir/ai/v1"
        os.environ["LLM_API_KEY"] = "test"
        os.environ["LLM_MODEL"] = "gpt-5-mini"
        try:
            agent = TradingAgent(cfg, post=post)
            args = ([_fake_feature("BTCTMN"), _fake_feature("ETHTMN")], [_candidate("BTCTMN"), _candidate("ETHTMN")], [], {"equity": 1}, 1_700_003_600)
            first = agent.decide(*args)
            second = agent.decide(*args)
            self.assertEqual(calls["n"], 1)
            self.assertNotIn("temperature", calls["bodies"][0])
            self.assertEqual(first.source, "llm")
            self.assertEqual(second.source, "llm_cache")
            self.assertEqual(first.usage["prompt_tokens"], 400)
            self.assertEqual(first.decisions[0].symbol, "BTCTMN")
            # A new hour misses the cache. The hourly cap is 1, so the next call is refused.
            third = agent.decide(
                [_fake_feature("BTCTMN")], [_candidate("BTCTMN")], [], {"equity": 1}, 1_700_003_600 + 3600,
            )
            self.assertEqual(calls["n"], 1)
            self.assertEqual(third.source, "fallback")
            self.assertIn("cap", third.comment)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["calls"]), 1)
            self.assertIn("prompt_tokens", saved["calls"][0])
        finally:
            os.environ.pop("LLM_API_KEY", None)
            os.environ.pop("LLM_MODEL", None)
            if path.exists():
                path.unlink()


def _fake_feature(symbol: str):
    from trader.strategy.signals import SymbolFeatures

    return SymbolFeatures(
        symbol=symbol, bar_time=1_700_000_000, close=100, ema_fast=101, ema_slow=99,
        roc_pullback=-0.008, roc_trend=0.02, atr_pct=0.004, rsi=48, spread_bps=10,
        imbalance=0.1, micro_return=0.0, expected_gross=0.009, round_trip_cost=0.0058,
        signal=True, blockers=[], score=0.7,
    )


if __name__ == "__main__":
    unittest.main()
