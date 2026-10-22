"""The stop loop must not wait on BoxAPI, and a bad model cannot buy."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from trader.config import load_config
from trader.engine.journal import Journal
from trader.engine.portfolio import Portfolio, Position
from trader.engine.runtime import _cycle
from trader.engine.state import fresh_state
from trader.risk.manager import RiskManager
from trader.strategy.agent import SPEND_GUARD_USD, TradingAgent
from trader.strategy.signals import Candidate
from trader.types import Bar, MarketRule
from trader.wallex.client import WallexClient, WallexError


def _candidate() -> Candidate:
    return Candidate(
        symbol="BTCTMN", setup="uptrend_pullback", size_fraction=0.04, score=0.7,
        expected_gross=0.009, round_trip_cost=0.0058, roc_trend=0.02, roc_pullback=-0.008,
        close=100, bar_time=1_700_000_000, facts={},
    )


def _feature():
    from trader.strategy.signals import SymbolFeatures

    return SymbolFeatures(
        symbol="BTCTMN", bar_time=1_700_000_000, close=100, ema_fast=101, ema_slow=99,
        roc_pullback=-0.008, roc_trend=0.02, atr_pct=0.004, rsi=48, spread_bps=10,
        imbalance=0.1, micro_return=0.0, expected_gross=0.009, round_trip_cost=0.0058,
        signal=True, blockers=[], score=0.7,
    )


class _Hang(WallexClient):
    def __init__(self, events: list[str]):
        super().__init__(dry_run=True, api_key="test-key")
        self.events = events

    def get_depth_all(self):
        return {}

    def get_depth(self, symbol: str):
        raise WallexError("no book")

    def get_order(self, client_order_id: str):
        return None

    def place_order(self, **kwargs):
        self.events.append("sell" if kwargs.get("side") == "SELL" else "order")
        return super().place_order(**kwargs)


class HotPathTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            "LLM_BASE_URL": os.environ.get("LLM_BASE_URL"),
            "LLM_API_KEY": os.environ.get("LLM_API_KEY"),
        }
        os.environ["LLM_BASE_URL"] = "https://ai.boxapi.ir/ai/v1"
        os.environ["LLM_API_KEY"] = "test-key"

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _agent(self, post, **llm):
        path = Path(tempfile.mkdtemp()) / "usage.json"
        cfg = {"llm": {
            "usage_path": str(path),
            "timeout_seconds": 0.2,
            "min_interval_seconds": 0,
            "max_retries": 1,
            "retry_backoff_seconds": 0,
            "enabled": True,
            **llm,
        }}
        return TradingAgent(cfg, post=post)

    def test_stop_is_sent_before_a_hanging_model_returns(self):
        events: list[str] = []
        release = threading.Event()
        entered = threading.Event()

        def post(url, headers, body, timeout=None):
            events.append("llm")
            entered.set()
            release.wait(30)
            return "{}", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0, "model": "openai/gpt-6-luna"}

        cfg = load_config("config/default.yaml")
        cfg["llm"]["timeout_seconds"] = 0.25
        cfg["llm"]["min_interval_seconds"] = 0
        cfg["llm"]["max_retries"] = 1
        cfg["llm"]["retry_backoff_seconds"] = 0
        cfg["llm"]["usage_path"] = str(Path(tempfile.mkdtemp()) / "usage.json")
        cfg["mcp"]["enabled"] = False
        agent = TradingAgent(cfg, post=post)
        risk = RiskManager(cfg)
        now = int(time.time())
        state = fresh_state(5_000_000)
        state["portfolio"] = Portfolio(5_000_000)
        state["portfolio"].positions["BTCTMN"] = Position(
            "BTCTMN", 600, 100, now - 3600, 60_000, 101, 100, "uptrend_pullback", "test",
        )
        state["portfolio"].positions["ETHTMN"] = Position(
            "ETHTMN", 20, 90, now - 3600, 1_800, 50, 90, "uptrend_pullback", "test",
        )
        bars = []
        price = 100.0
        for index in range(80):
            price *= 0.998
            bars.append(Bar(now - (80 - index) * 3600, price, price * 1.001, price * 0.999, price, 1_000))
        rules = {
            "BTCTMN": MarketRule("BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000, last_price=100),
            "ETHTMN": MarketRule("ETHTMN", "ETH", "TMN", 0, 2, 0.01, 50_000, last_price=price),
        }
        directory = Path(tempfile.mkdtemp())
        journal = Journal(directory)
        client = _Hang(events)
        started = time.monotonic()
        try:
            _cycle(
                cfg, "shadow", client, agent, risk, journal, state, directory / "state.json",
                rules, set(), {}, ["BTCTMN", "ETHTMN"],
                {"BTCTMN": 10.0, "ETHTMN": 10.0},
                {"ETHTMN": bars}, {},
                True, None,
            )
        finally:
            release.set()
            journal.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5, events)
        self.assertIn("sell", events)
        self.assertIn("llm", events)
        self.assertLess(events.index("sell"), events.index("llm"))
        self.assertEqual(client.events[0], "sell")
        self.assertTrue(entered.is_set())
        blob = (directory / "decisions.jsonl").read_text(encoding="utf-8")
        self.assertIn('"source": "fallback"', blob)

    def test_hard_timeout_uses_the_deterministic_fallback(self):
        def post(url, headers, body, timeout=None):
            time.sleep(5)
            return "{}", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}

        agent = self._agent(post)
        started = time.monotonic()
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1, "phase": "normal"}, 1_700_000_000)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(decision.source, "fallback")
        self.assertIn("hard timeout", decision.error)
        self.assertEqual(decision.decisions[0].action, "BUY")
        self.assertIn("DETERMINISTIC_FALLBACK", decision.decisions[0].rationale)

    def test_invalid_schema_vetoes_instead_of_buying(self):
        def post(url, headers, body, timeout=None):
            return "buy the top name", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0001, "model": "x"}

        agent = self._agent(post)
        decision = agent.decide([_feature()], [_candidate()], [], {"equity": 1, "phase": "normal"}, 1_700_000_000)
        self.assertEqual(decision.source, "fallback")
        self.assertTrue(decision.veto_all)
        self.assertEqual(decision.decisions, [])
        self.assertIn("invalid", decision.comment)

    def test_spend_guard_skips_the_model_and_logs_the_total(self):
        calls = {"n": 0}

        def post(url, headers, body, timeout=None):
            calls["n"] += 1
            raise AssertionError("model must not be called once the ledger reaches the guard")

        agent = self._agent(post)
        agent.budget.calls.append({"ts": 1_700_000_000, "usd": SPEND_GUARD_USD})
        with self.assertLogs("trader.agent", level="INFO") as captured:
            decision = agent.decide(
                [_feature()], [_candidate()], [], {"equity": 1, "phase": "normal"}, 1_700_000_000,
            )
        self.assertEqual(calls["n"], 0)
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.usage["skipped"], "spend guard 2.25")
        self.assertTrue(any("llm_usd_total" in line and "2.25" in line for line in captured.output))
        self.assertNotIn("test-key", "\n".join(captured.output))
        raw = json.dumps(decision.as_log())
        self.assertNotIn("test-key", raw)


if __name__ == "__main__":
    unittest.main()
