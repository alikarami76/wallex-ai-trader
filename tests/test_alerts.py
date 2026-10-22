"""Telegram alerts are optional, rate-limited, and never include the token."""

import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import trader.alerts as alerts
import trader.engine.runtime as runtime
from trader.alerts import _last_sent, notify, report_cycle, send_decision, send_skips, send_update
from trader.engine.runtime import _relay_orders
from trader.risk.manager import Action


class AlertTests(unittest.TestCase):
    def setUp(self):
        _last_sent.clear()
        alerts._skip_sent = None
        self._env = {
            "TELEGRAM_ALERT_BOT_TOKEN": os.environ.get("TELEGRAM_ALERT_BOT_TOKEN"),
            "TELEGRAM_ALERT_CHAT_ID": os.environ.get("TELEGRAM_ALERT_CHAT_ID"),
        }
        os.environ.pop("TELEGRAM_ALERT_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _last_sent.clear()
        alerts._skip_sent = None

    def _arm(self):
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:secret-token"
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "4242"

    def test_unset_env_sends_nothing(self):
        calls = []
        self.assertEqual(
            report_cycle(
                gap_seconds=900, ops_state="HALT", order_rejects=2, entry_taker_fills=1,
                post=lambda url, body: calls.append(body), now=1,
            ),
            [],
        )
        self.assertEqual(calls, [])
        self.assertFalse(notify("cycle_gap", "cycle gap 900s", post=lambda url, body: calls.append(1), now=1))

    def test_the_four_triggers_send_once_and_then_wait(self):
        calls = []

        def post(url, body):
            calls.append(json.loads(body.decode()))

        self._arm()
        first = report_cycle(
            gap_seconds=301, ops_state="KILLED", order_rejects=1, entry_taker_fills=1,
            post=post, now=10,
        )
        self.assertEqual(first, ["cycle_gap", "KILLED", "order_reject", "taker_entry"])
        self.assertEqual(len(calls), 4)
        texts = [row["text"] for row in calls]
        self.assertIn("cycle gap 301s", texts)
        self.assertIn("ops state KILLED", texts)
        self.assertIn("order rejects 1", texts)
        self.assertIn("taker entry fills 1", texts)
        for row in calls:
            self.assertEqual(row["chat_id"], "4242")
            self.assertNotIn("secret-token", row["text"])
        again = report_cycle(
            gap_seconds=900, ops_state="HALT", order_rejects=3, entry_taker_fills=2,
            post=post, now=20,
        )
        self.assertEqual(again, ["HALT"])
        quiet = report_cycle(
            gap_seconds=None, ops_state="SHADOW", order_rejects=0, entry_taker_fills=0,
            post=post, now=30,
        )
        self.assertEqual(quiet, [])
        self.assertEqual(len(calls), 5)
        later = notify("cycle_gap", "cycle gap 10s", post=post, now=10 + 300)
        self.assertTrue(later)
        self.assertEqual(calls[-1]["text"], "cycle gap 10s")

    def test_a_failed_send_does_not_log_the_token(self):
        self._arm()

        def post(url, body):
            raise RuntimeError(url)

        with self.assertLogs("trader.alerts", level="WARNING") as captured:
            sent = notify("order_reject", "order rejects 1", post=post, now=1)
        self.assertFalse(sent)
        blob = "\n".join(captured.output)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("123456", blob)
        self.assertIn("RuntimeError", blob)


class DecisionPostTests(unittest.TestCase):
    def setUp(self):
        _last_sent.clear()
        alerts._skip_sent = None
        self._env = {
            "TELEGRAM_ALERT_BOT_TOKEN": os.environ.get("TELEGRAM_ALERT_BOT_TOKEN"),
            "TELEGRAM_ALERT_CHAT_ID": os.environ.get("TELEGRAM_ALERT_CHAT_ID"),
        }
        os.environ.pop("TELEGRAM_ALERT_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)
        self.when = datetime(2026, 10, 23, 17, 31, tzinfo=timezone.utc)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _last_sent.clear()
        alerts._skip_sent = None

    def _arm(self):
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:secret-token"
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "4242"

    def _capture(self):
        calls = []

        def post(url, body):
            calls.append(json.loads(body.decode()))

        return calls, post

    def test_unset_env_is_a_noop(self):
        calls, post = self._capture()
        self.assertFalse(send_decision(
            mode="shadow", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            price=100, qty=0.01, reason="uptrend pullback", edge=0.0045, hurdle=0.0072,
            sl=95.5, tp=102.5, client_id="SCabc", post=post,
        ))
        self.assertFalse(send_update(
            mode="shadow", when=self.when, symbol="BTCTMN", side="SELL", order_type="MARKET",
            status="FILL", price=100, qty=0.01, qty_total=0.01, pnl=12.5, client_id="SCabc", post=post,
        ))
        self.assertFalse(send_skips(
            mode="shadow", when=self.when, rows=[("BTCTMN", "fee hurdle")], post=post, now=1,
        ))
        self.assertEqual(calls, [])

    def test_a_buy_carries_the_decision_and_a_sell_carries_pnl(self):
        calls, post = self._capture()
        self._arm()
        self.assertTrue(send_decision(
            mode="shadow", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            price=100, qty=0.01, reason="uptrend pullback", edge=0.0045, hurdle=0.0072,
            sl=95.5, tp=102.5, client_id="SCabc", post=post,
        ))
        self.assertTrue(send_decision(
            mode="live", when=self.when, symbol="ETH TMN", side="SELL", order_type="MARKET",
            price=200, qty=1, reason="time stop at flat_by after 4.0h", edge=None, hurdle=None,
            sl=190, tp=205, client_id="SCdef", post=post,
        ))
        self.assertTrue(send_update(
            mode="shadow", when=self.when, symbol="BTCTMN", side="SELL", order_type="LIMIT",
            status="FILL", price=110, qty=0.01, qty_total=0.01, pnl=12.5, client_id="SCabc", post=post,
        ))
        self.assertTrue(send_update(
            mode="live", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            status="PARTIAL", price=100, qty=0.004, qty_total=0.01, pnl=None, client_id="SCabc", post=post,
        ))
        self.assertTrue(send_update(
            mode="shadow", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            status="CANCEL", price=None, qty=None, qty_total=None, pnl=None, client_id="SCabc", post=post,
        ))
        texts = [row["text"] for row in calls]
        buy = texts[0]
        self.assertTrue(buy.startswith("[SHADOW] "))
        self.assertIn("2026-10-23 21:01 +0330", buy)
        self.assertIn("BUY BTCTMN LIMIT", buy)
        self.assertIn("price 100", buy)
        self.assertIn("qty 0.01", buy)
        self.assertIn("1.00 TMN", buy)
        self.assertIn("signal uptrend pullback", buy)
        self.assertIn("edge 0.450% vs hurdle 0.720%", buy)
        self.assertIn("SL 95.5", buy)
        self.assertIn("TP 102.5", buy)
        self.assertIn("id SCabc", buy)
        self.assertNotIn("secret-token", buy)
        sell = texts[1]
        self.assertFalse(sell.startswith("[SHADOW]"))
        self.assertIn("SELL ETH TMN MARKET", sell)
        self.assertIn("time stop at flat_by after 4.0h", sell)
        self.assertNotIn("edge", sell)
        filled = texts[2]
        self.assertIn("FILL SELL BTCTMN LIMIT", filled)
        self.assertIn("pnl +12.50 TMN after fees", filled)
        self.assertIn("PARTIAL BUY BTCTMN LIMIT", texts[3])
        self.assertNotIn("pnl", texts[3])
        self.assertIn("CANCEL BUY BTCTMN LIMIT", texts[4])
        self.assertNotIn("pnl", texts[4])

    def test_skipped_entries_wait_half_an_hour(self):
        calls, post = self._capture()
        self._arm()
        self.assertTrue(send_skips(
            mode="shadow", when=self.when, rows=[("BTCTMN", "fee hurdle"), ("ETH", "spread")],
            post=post, now=10,
        ))
        self.assertFalse(send_skips(
            mode="shadow", when=self.when, rows=[("BTCTMN", "fee hurdle")], post=post, now=20,
        ))
        self.assertEqual(len(calls), 1)
        self.assertIn("skipped entries", calls[0]["text"])
        self.assertIn("BTCTMN: fee hurdle", calls[0]["text"])
        self.assertTrue(calls[0]["text"].startswith("[SHADOW] "))
        self.assertTrue(send_skips(
            mode="shadow", when=self.when, rows=[("BTCTMN", "fee hurdle")], post=post, now=10 + 1800,
        ))
        self.assertEqual(len(calls), 2)

    def test_a_failed_decision_does_not_log_the_token(self):
        self._arm()

        def post(url, body):
            raise RuntimeError(url)

        with self.assertLogs("trader.alerts", level="WARNING") as captured:
            sent = send_decision(
                mode="shadow", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
                price=100, qty=0.01, reason="123456:secret-token", edge=0.0045, hurdle=0.0072,
                sl=95.5, tp=102.5, client_id="SCabc", post=post,
            )
        self.assertFalse(sent)
        blob = "\n".join(captured.output)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("123456", blob)
        self.assertIn("RuntimeError", blob)

    def test_the_runtime_hook_posts_the_order_it_just_sent(self):
        calls, post = self._capture()
        self._arm()
        action = Action(
            symbol="BTCTMN", side="BUY", order_type="LIMIT", size_fraction=0.1,
            reason="model confirm", source="agent", price_direction="down",
        )
        request = {"body": {
            "symbol": "BTCTMN", "side": "BUY", "type": "LIMIT",
            "quantity": "0.01", "price": "100", "client_id": "SCabc",
        }}
        feature = SimpleNamespace(expected_gross=0.0045, signal=True)
        cfg = {"risk": {"stop_loss_pct": 0.045, "trail_arm_pct": 0.025}}
        original = runtime.send_decision
        runtime.send_decision = lambda **kwargs: original(**kwargs, post=post)
        try:
            _relay_orders(
                "shadow", self.when, action, None, [request], [], feature, cfg, 0.0072, {"BTCTMN": 100},
            )
        finally:
            runtime.send_decision = original
        self.assertEqual(len(calls), 1)
        text = calls[0]["text"]
        self.assertIn("[SHADOW]", text)
        self.assertIn("BUY BTCTMN LIMIT", text)
        self.assertIn("SL 95.5", text)
        self.assertIn("TP 102.5", text)
        self.assertIn("id SCabc", text)
        self.assertNotIn("secret-token", calls[0]["text"])


if __name__ == "__main__":
    unittest.main()
