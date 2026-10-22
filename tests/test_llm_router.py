import json
import os
import unittest
from pathlib import Path

from trader.sentiment import SentimentSnapshot
from trader.strategy.agent import LlmHTTPError, TradingAgent, _price, adapt_rejected_body, llm_settings, request_body
from trader.strategy.router import choose_tier, quoted_call_usd
from trader.strategy.signals import Candidate, SymbolFeatures


def _candidate(symbol: str = "BTCTMN") -> Candidate:
    return Candidate(
        symbol=symbol, setup="uptrend_pullback", size_fraction=0.12, score=0.7,
        expected_gross=0.009, round_trip_cost=0.0058, roc_trend=0.02, roc_pullback=-0.008,
        close=100, bar_time=1_700_000_000, facts={},
    )


def _feature(symbol: str = "BTCTMN") -> SymbolFeatures:
    return SymbolFeatures(
        symbol=symbol, bar_time=1_700_000_000, close=100, ema_fast=101, ema_slow=99,
        roc_pullback=-0.008, roc_trend=0.02, atr_pct=0.004, rsi=48, spread_bps=10,
        imbalance=0.1, micro_return=0.0, expected_gross=0.009, round_trip_cost=0.0058,
        signal=True, blockers=[], score=0.7,
    )


def _reply(symbol: str = "BTCTMN") -> str:
    return json.dumps({
        "veto_all": False,
        "regime": "trend",
        "comment": "ok",
        "decisions": [{
            "action": "BUY", "symbol": symbol, "size_fraction": 0.05,
            "confidence": 0.7, "veto": False, "rationale": "cost gate already passed",
        }],
    })


class RouterTests(unittest.TestCase):
    def setUp(self):
        os.environ["LLM_BASE_URL"] = "https://ai.boxapi.ir/ai/v1"
        os.environ["LLM_API_KEY"] = "test"
        os.environ.pop("LLM_MODEL", None)
        self.path = Path("data/test_llm_router.json")
        if self.path.exists():
            self.path.unlink()

    def tearDown(self):
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_MODEL", None)
        if self.path.exists():
            self.path.unlink()

    def _agent(self, post, calls=None, **llm):
        settings = {
            "usage_path": str(self.path),
            "min_interval_seconds": 0,
            "max_retries": 3,
            "retry_backoff_seconds": 0.5,
            "cache_seconds": 3600,
            "sentiment_cache_seconds": 900,
        }
        settings.update(llm)

        def wrapped(url, headers, body, timeout=None):
            if calls is not None:
                calls.setdefault("bodies", []).append(body)
                calls["n"] = calls.get("n", 0) + 1
            return post(url, headers, body, timeout)

        return TradingAgent({"llm": settings}, post=wrapped, sleep=lambda _seconds: None)

    def test_default_decision_is_luna_and_sol_is_high_stakes(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            return _reply(), {"prompt_tokens": 20, "completion_tokens": 10, "cost": "3e-6", "model": body["model"]}

        agent = self._agent(post, calls)
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1, "gross_exposure": 0}, 1_700_000_000)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(calls["bodies"][0]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls["bodies"][0]["reasoning_effort"], "medium")
        self.assertNotIn("temperature", calls["bodies"][0])
        self.assertNotIn("max_tokens", calls["bodies"][0])
        self.assertEqual(decision.usage["tier"], "trade")
        self.assertEqual(decision.usage["model"], "openai/gpt-6-luna")
        self.assertEqual(decision.usage["cost_source"], "usage.cost")
        self.assertAlmostEqual(decision.usage["usd"], 3e-6)
        self.assertAlmostEqual(decision.usage["spent_usd"], 3e-6)
        self.assertEqual(decision.usage["budget_usd"], 3.0)
        self.assertEqual(decision.usage["prompt_tokens"], 20)

        held = agent.decide([_feature()], [], [{"symbol": "BTCTMN"}], {"equity": 1}, 1_700_003_000)
        self.assertEqual(calls["bodies"][-1]["model"], "openai/gpt-6-luna")
        self.assertEqual(calls["bodies"][-1]["reasoning_effort"], "medium")
        self.assertEqual(held.usage["tier"], "signal")

        stressed = agent.decide(
            [_feature()], [_candidate()], [{"symbol": "ETHTMN"}],
            {"equity": 1, "gross_exposure": 0.08}, 1_700_006_000,
        )
        self.assertEqual(calls["bodies"][-1]["model"], "openai/gpt-6-sol")
        self.assertEqual(calls["bodies"][-1]["reasoning_effort"], "high")
        self.assertEqual(stressed.usage["tier"], "stakes")

    def test_downgrade_steps_toward_the_cheap_tier(self):
        settings = llm_settings({})
        cheap = choose_tier("trade", settings, spent_total=2.05, spent_today=0.1)
        self.assertEqual(cheap["tier"], "signal")
        self.assertEqual(cheap["model"], "openai/gpt-6-luna")
        self.assertTrue(cheap["downgraded"])
        forced = choose_tier("trade", settings, spent_total=2.35, spent_today=0.1)
        self.assertEqual(forced["tier"], "routine")
        self.assertEqual(forced["reasoning_effort"], "low")
        daily = choose_tier("trade", settings, spent_total=0.2, spent_today=1.10)
        self.assertEqual(daily["tier"], "routine")

    def test_daily_cap_uses_the_deterministic_fallback(self):
        def post(url, headers, body, timeout=None):
            raise AssertionError("budget should refuse before the request")

        agent = self._agent(post, max_spend_usd_per_day=1.25, max_llm_calls_per_hour=10)
        agent.budget.calls.append({"ts": 1_700_000_000, "usd": 1.25})
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1}, 1_700_000_100)
        self.assertEqual(decision.source, "fallback")
        self.assertIn("daily", decision.usage["skipped"])
        self.assertIn("DETERMINISTIC_FALLBACK", decision.decisions[0].rationale)

    def test_http_400_retries_without_reasoning_effort(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            if "reasoning_effort" in body:
                raise LlmHTTPError(400, "unsupported reasoning_effort")
            return _reply(), {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.000001, "model": body["model"]}

        agent = self._agent(post, calls)
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1}, 1_700_000_000)
        self.assertEqual(calls["n"], 2)
        self.assertNotIn("reasoning_effort", calls["bodies"][-1])
        self.assertEqual(decision.usage["reasoning_effort"], "omitted")
        self.assertEqual(decision.source, "llm")

    def test_429_backs_off_then_succeeds(self):
        calls = {"n": 0}
        slept = []

        def post(url, headers, body, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise LlmHTTPError(429, "slow down")
            return _reply(), {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.000002, "model": body["model"]}

        agent = self._agent(post)
        agent.llm._sleep = slept.append
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1}, 1_700_000_000)
        self.assertEqual(decision.source, "llm")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [0.5, 1.0])

    def test_flat_cycle_scores_headlines_on_tier0_only(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            self.assertEqual(body["model"], "openai/gpt-6-luna")
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertLessEqual(timeout, 8)
            return json.dumps({"scores": {"BTCTMN": -0.4}}), {
                "prompt_tokens": 12, "completion_tokens": 8, "cost": "3e-6", "model": body["model"],
            }

        agent = self._agent(post, calls)
        mood = SentimentSnapshot(
            news_score={"BTCTMN": 0.0},
            news_change={"BTCTMN": None},
            headlines={"BTCTMN": ["Bitcoin slides as ETF flows wobble"]},
        )
        missed = agent.apply_sentiment(mood, 1_700_000_000, allow_network=False)
        self.assertEqual(missed, [])
        self.assertEqual(mood.news_score["BTCTMN"], 0.0)
        records = agent.apply_sentiment(mood, 1_700_000_000, allow_network=True)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(mood.news_score["BTCTMN"], -0.4)
        self.assertEqual(records[0]["tier"], "routine")
        self.assertAlmostEqual(records[0]["usd"], 3e-6)
        again = agent.apply_sentiment(mood, 1_700_000_100, allow_network=True)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(again[0]["cached"])

    def test_request_effort_is_optional(self):
        body = request_body("openai/gpt-6-luna", [], llm_settings({}), "low")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("reasoning_effort", request_body("openai/gpt-6-luna", [], llm_settings({})))
        capped = request_body("openai/gpt-6-luna", [], llm_settings({"llm": {"max_tokens": 800}}), None)
        self.assertEqual(capped["max_completion_tokens"], 800)
        self.assertNotIn("max_tokens", capped)

    def test_list_price_for_a_typical_decision(self):
        settings = llm_settings({})
        self.assertAlmostEqual(quoted_call_usd("openai/gpt-6-luna", settings), 0.0006325)
        self.assertAlmostEqual(quoted_call_usd("gpt-6-sol", settings), 0.01265)
        luna = _price(
            {"prompt_tokens": 3000, "completion_tokens": 100, "reasoning_tokens": 400},
            settings,
            "openai/gpt-6-luna",
        )
        # 400 reasoning tokens sit above the 100 completion tokens, so output bills 500.
        self.assertEqual(luna["cost_source"], "estimated_tokens")
        self.assertAlmostEqual(luna["usd"], 0.0006325)
        exact = _price(
            {"cost": "0.0007", "prompt_tokens": 9000, "completion_tokens": 9000},
            settings,
            "openai/gpt-6-sol",
        )
        self.assertEqual(exact["cost_source"], "usage.cost")
        self.assertAlmostEqual(exact["usd"], 0.0007)

    def test_hard_cap_degrades_then_stops(self):
        settings = llm_settings({})
        full = choose_tier("stakes", settings, spent_total=0.0, spent_today=0.0)
        self.assertEqual(full["tier"], "stakes")
        self.assertEqual(full["model"], "openai/gpt-6-sol")
        self.assertFalse(full["downgraded"])
        stepped = choose_tier("stakes", settings, spent_total=2.05, spent_today=0.1)
        self.assertEqual(stepped["tier"], "trade")
        self.assertEqual(stepped["model"], "openai/gpt-6-luna")
        self.assertTrue(stepped["downgraded"])
        cheap = choose_tier("stakes", settings, spent_total=2.35, spent_today=0.1)
        self.assertEqual(cheap["tier"], "routine")
        self.assertEqual(cheap["model"], "openai/gpt-6-luna")

        def post(url, headers, body, timeout=None):
            raise AssertionError("the hard cap must stop the call")

        agent = self._agent(post, max_spend_usd=2.50, max_llm_calls_per_hour=10, max_spend_usd_per_day=3)
        agent.budget.calls.append({"ts": 1_700_000_000, "usd": 2.50})
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1}, 1_700_000_100)
        self.assertEqual(decision.source, "fallback")
        self.assertIn("cap", decision.usage["skipped"])
        self.assertIn("DETERMINISTIC_FALLBACK", decision.decisions[0].rationale)
        self.assertAlmostEqual(decision.usage["spent_usd"], 2.50)
        self.assertEqual(decision.usage["budget_usd"], 3.0)
        self.assertEqual(decision.usage["cap_usd"], 2.50)

    def test_max_tokens_400_retries_as_max_completion_tokens(self):
        calls = {}

        def post(url, headers, body, timeout=None):
            if "max_tokens" in body:
                raise LlmHTTPError(400, "Unsupported parameter: max_tokens")
            self.assertEqual(body.get("max_completion_tokens"), 400)
            self.assertNotIn("max_tokens", body)
            return _reply(), {"prompt_tokens": 4, "completion_tokens": 4, "cost": 0.000001, "model": body["model"]}

        agent = self._agent(post, calls)
        content, usage, error = agent.llm._send(
            "https://ai.boxapi.ir/ai/v1/chat/completions",
            {},
            {"model": "openai/gpt-6-luna", "max_tokens": 400, "messages": []},
            5,
        )
        self.assertEqual(error, "")
        self.assertTrue(content)
        self.assertEqual(calls["n"], 2)
        rewritten = {"model": "openai/gpt-6-luna", "max_tokens": 50}
        self.assertEqual(adapt_rejected_body(rewritten, "Unsupported parameter: max_tokens"), "max_completion_tokens")
        self.assertEqual(rewritten["max_completion_tokens"], 50)
        self.assertNotIn("max_tokens", rewritten)


if __name__ == "__main__":
    unittest.main()
