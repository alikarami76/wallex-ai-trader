"""Telegram alerts are optional, rate-limited, and never include the token."""

import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import trader.alerts as alerts
import trader.engine.runtime as runtime
from trader.alerts import (
    _last_sent,
    classify_hold,
    format_hold,
    notify,
    report_cycle,
    send_decision,
    send_hold,
    send_skips,
    send_update,
)
from trader.engine.portfolio import Fill
from trader.engine.runtime import _queue_cancel_note, _relay_cancel, _relay_fill, _relay_orders
from trader.risk.manager import Action


class AlertTests(unittest.TestCase):
    def setUp(self):
        _last_sent.clear()
        alerts._skip_sent = None
        alerts.reset_hold_notice()
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
        alerts.reset_hold_notice()

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
        alerts.reset_hold_notice()
        self._env = {
            "TELEGRAM_ALERT_BOT_TOKEN": os.environ.get("TELEGRAM_ALERT_BOT_TOKEN"),
            "TELEGRAM_ALERT_CHAT_ID": os.environ.get("TELEGRAM_ALERT_CHAT_ID"),
            "TELEGRAM_TRADES_CHAT_ID": os.environ.get("TELEGRAM_TRADES_CHAT_ID"),
        }
        os.environ.pop("TELEGRAM_ALERT_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)
        os.environ.pop("TELEGRAM_TRADES_CHAT_ID", None)
        self.when = datetime(2026, 10, 23, 17, 31, tzinfo=timezone.utc)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _last_sent.clear()
        alerts._skip_sent = None
        alerts.reset_hold_notice()

    def _arm(self):
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:secret-token"
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "4242"
        os.environ["TELEGRAM_TRADES_CHAT_ID"] = "-1003898687684"

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
        for row in calls:
            self.assertEqual(row["chat_id"], "-1003898687684")
            self.assertNotIn("4242", row["text"])
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

    def test_a_fill_post_includes_the_fee_from_the_fill(self):
        calls, post = self._capture()
        self._arm()
        self.assertTrue(send_update(
            mode="live", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            status="FILL", price=100_000_000, qty=0.001, qty_total=0.001, pnl=None,
            fee=300.0, client_id="SCfee", post=post,
        ))
        text = calls[0]["text"]
        self.assertIn("FILL BUY BTCTMN LIMIT", text)
        self.assertIn("fee 300.00 TMN", text)
        self.assertIn("id SCfee", text)
        self.assertNotIn("pnl", text)
        self.assertTrue(send_update(
            mode="live", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            status="CANCEL", price=None, qty=None, qty_total=None, pnl=None,
            fee=12.0, client_id="SCcnx", post=post,
        ))
        self.assertNotIn("fee ", calls[1]["text"])

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
        self.assertEqual(calls[0]["chat_id"], "-1003898687684")
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
        self.assertEqual(calls[0]["chat_id"], "-1003898687684")

    def test_runtime_fill_posts_fee_and_a_failed_send_stays_quiet(self):
        calls, post = self._capture()
        self._arm()
        fill = Fill(
            symbol="BTCTMN", side="BUY", price=100, qty=0.01, fee_tmn=0.3,
            fee_rate=0.003, notional_tmn=1.0, pnl_tmn=None, reason="entry",
            order_type="LIMIT", client_id="SCfee1",
        )
        original_update = runtime.send_update
        original_decision = runtime.send_decision
        runtime.send_update = lambda **kwargs: original_update(**kwargs, post=post)
        try:
            _relay_fill("live", fill, {"qty": 0.01, "order_type": "LIMIT"}, None, when=self.when)
        finally:
            runtime.send_update = original_update
        self.assertEqual(len(calls), 1)
        self.assertIn("fee 0.30 TMN", calls[0]["text"])
        self.assertIn("FILL BUY BTCTMN LIMIT", calls[0]["text"])

        def boom(url, body):
            raise RuntimeError(url)

        runtime.send_update = lambda **kwargs: original_update(**kwargs, post=boom)
        runtime.send_decision = lambda **kwargs: original_decision(**kwargs, post=boom)
        try:
            with self.assertLogs("trader.alerts", level="WARNING"):
                _relay_fill("live", fill, {"qty": 0.01}, None, when=self.when)
                _relay_orders(
                    "live", self.when,
                    Action(
                        symbol="BTCTMN", side="BUY", order_type="LIMIT", size_fraction=0.1,
                        reason="x", source="agent", price_direction="down",
                    ),
                    None, [{"body": {
                        "symbol": "BTCTMN", "side": "BUY", "type": "LIMIT",
                        "quantity": "0.01", "price": "100", "client_id": "SCboom",
                    }}], [fill], None, {"risk": {"stop_loss_pct": 0.045, "trail_arm_pct": 0.025}},
                    0.0072, {"BTCTMN": 100},
                )
        finally:
            runtime.send_update = original_update
            runtime.send_decision = original_decision

    def test_partial_buy_cancel_is_queued_for_the_trades_channel(self):
        calls, post = self._capture()
        self._arm()
        state = {}
        _queue_cancel_note(state, client_id="SCpart", symbol="BTCTMN", side="BUY", order_type="LIMIT")
        self.assertEqual(state["_telegram_cancels"][0]["client_id"], "SCpart")
        original = runtime.send_update
        runtime.send_update = lambda **kwargs: original(**kwargs, post=post)
        try:
            for row in state.pop("_telegram_cancels", []):
                _relay_cancel("live", self.when, row)
        finally:
            runtime.send_update = original
        self.assertEqual(len(calls), 1)
        self.assertIn("CANCEL BUY BTCTMN LIMIT", calls[0]["text"])
        self.assertIn("id SCpart", calls[0]["text"])
        self.assertEqual(calls[0]["chat_id"], "-1003898687684")

    def test_the_alert_group_does_not_receive_the_trade_log(self):
        calls, post = self._capture()
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:secret-token"
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "4242"
        self.assertFalse(send_decision(
            mode="shadow", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            price=100, qty=0.01, reason="uptrend pullback", edge=0.0045, hurdle=0.0072,
            sl=95.5, tp=102.5, client_id="SCabc", post=post,
        ))
        sent = report_cycle(
            gap_seconds=None, ops_state="SHADOW", order_rejects=1, entry_taker_fills=0,
            post=post, now=1,
        )
        self.assertEqual(sent, ["order_reject"])
        self.assertEqual(calls[0]["chat_id"], "4242")
        self.assertNotIn("BUY", calls[0]["text"])
        os.environ["TELEGRAM_TRADES_CHAT_ID"] = "-1003898687684"
        self.assertTrue(send_decision(
            mode="live", when=self.when, symbol="BTCTMN", side="BUY", order_type="LIMIT",
            price=100, qty=0.01, reason="uptrend pullback", edge=0.0045, hurdle=0.0072,
            sl=95.5, tp=102.5, client_id="SCabc", post=post,
        ))
        self.assertEqual(calls[-1]["chat_id"], "-1003898687684")
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)
        self.assertTrue(send_update(
            mode="live", when=self.when, symbol="BTCTMN", side="SELL", order_type="LIMIT",
            status="FILL", price=110, qty=0.01, qty_total=0.01, pnl=1, client_id="SCabc", post=post,
        ))
        self.assertEqual(calls[-1]["chat_id"], "-1003898687684")
        self.assertEqual(
            report_cycle(
                gap_seconds=900, ops_state="HALT", order_rejects=1, entry_taker_fills=1,
                post=post, now=2,
            ),
            [],
        )


class HoldPostTests(unittest.TestCase):
    def setUp(self):
        alerts.reset_hold_notice()
        self._env = {
            "TELEGRAM_ALERT_BOT_TOKEN": os.environ.get("TELEGRAM_ALERT_BOT_TOKEN"),
            "TELEGRAM_ALERT_CHAT_ID": os.environ.get("TELEGRAM_ALERT_CHAT_ID"),
            "TELEGRAM_TRADES_CHAT_ID": os.environ.get("TELEGRAM_TRADES_CHAT_ID"),
        }
        os.environ.pop("TELEGRAM_ALERT_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)
        os.environ.pop("TELEGRAM_TRADES_CHAT_ID", None)
        self.when = datetime(2026, 10, 23, 17, 31, tzinfo=timezone.utc)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        alerts.reset_hold_notice()

    def _arm(self):
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:secret-token"
        os.environ["TELEGRAM_TRADES_CHAT_ID"] = "-1003898687684"

    def _capture(self):
        calls = []

        def post(url, body):
            calls.append(json.loads(body.decode()))

        return calls, post

    def _features(self):
        return [
            SimpleNamespace(symbol="BTCTMN", blockers=["trend structure off", "24h return 0.20% below 1.00%"]),
            SimpleNamespace(symbol="ETHTMN", blockers=["24h return -1.00% below 1.00%", "spread 40.0 bps too wide"]),
            SimpleNamespace(symbol="SOLTMN", blockers=["24h return 0.10% below 1.00%"]),
        ]

    def test_filter_hold_names_the_main_gate_and_the_book(self):
        reason, names = classify_hold(
            features=self._features(),
            candidates=[],
            zscore_n=25,
            zscore_min=18,
            entries_allowed=True,
            entries_blocked=False,
            block_reason="",
            cutoff_reason="",
        )
        self.assertEqual(names, [])
        self.assertIn("no candidates passed the filters (3 symbols, main filter return_24h 3)", reason)
        self.assertIn("ema 1", reason)
        self.assertIn("spread 1", reason)
        text = format_hold(
            when=self.when, ops_state="LIVE_ARMED", reason=reason, cash=4_900_000, positions=[],
        )
        self.assertIn("2026-10-23 21:01 +0330", text)
        self.assertIn("LIVE_ARMED", text)
        self.assertIn("\nHOLD\n", text)
        self.assertIn("cash 4,900,000.00 TMN · flat", text)

    def test_ev_warmup_cutoff_and_llm_rationale(self):
        ev, _names = classify_hold(
            features=[SimpleNamespace(blockers=["edge 0.45% does not clear cost 0.72%"])],
            candidates=[],
            zscore_n=25,
            zscore_min=18,
            entries_allowed=True,
            entries_blocked=False,
            block_reason="",
            cutoff_reason="",
        )
        self.assertTrue(ev.startswith("EV below the fee hurdle (1 symbols, ev 1)"))
        warm, _names = classify_hold(
            features=[],
            candidates=[],
            zscore_n=6,
            zscore_min=18,
            entries_allowed=False,
            entries_blocked=True,
            block_reason="zscore_warmup",
            cutoff_reason="zscore_warmup",
        )
        self.assertEqual(warm, "warm-up not complete (6/18)")
        blocked, names = classify_hold(
            features=[],
            candidates=[SimpleNamespace(symbol="BTCTMN")],
            zscore_n=25,
            zscore_min=18,
            entries_allowed=False,
            entries_blocked=True,
            block_reason="entry_cutoff",
            cutoff_reason="entry_cutoff",
        )
        self.assertEqual(blocked, "risk/cutoff blocking entry: entry cutoff")
        self.assertEqual(names, ["BTCTMN"])
        long = "x" * 400
        decision = SimpleNamespace(
            comment="ignored when a hold rationale exists",
            decisions=[SimpleNamespace(action="HOLD", rationale=long)],
        )
        text, names = classify_hold(
            features=[],
            candidates=[SimpleNamespace(symbol="ETH TMN")],
            zscore_n=25,
            zscore_min=18,
            entries_allowed=True,
            entries_blocked=False,
            block_reason="shadow",
            cutoff_reason="shadow",
            decision=decision,
        )
        self.assertEqual(len(text), 300)
        self.assertEqual(text, long[:300])
        self.assertEqual(names, ["ETH TMN"])

    def test_identical_holds_wait_fifteen_minutes_and_a_new_reason_posts(self):
        calls, post = self._capture()
        kwargs = dict(
            mode="live", ops_state="LIVE_ARMED", when=self.when,
            reason="no candidates passed the filters (6 symbols, main filter ema 6)",
            candidates=[], cash=1_000_000, positions=[("BTCTMN", 0.01)], post=post,
        )
        self.assertFalse(send_hold(**kwargs, now=1))
        self._arm()
        self.assertTrue(send_hold(**kwargs, now=1))
        self.assertFalse(send_hold(**kwargs, now=2))
        self.assertFalse(send_hold(**kwargs, now=1 + 899))
        self.assertEqual(len(calls), 1)
        self.assertIn("HOLD", calls[0]["text"])
        self.assertIn("main filter ema 6", calls[0]["text"])
        self.assertIn("cash 1,000,000.00 TMN · BTCTMN 0.01", calls[0]["text"])
        self.assertNotIn("[SHADOW]", calls[0]["text"])
        self.assertEqual(calls[0]["chat_id"], "-1003898687684")
        self.assertTrue(send_hold(**kwargs, now=1 + 900))
        self.assertIn("still holding (4 cycles), same reason", calls[1]["text"])
        self.assertNotIn("\nHOLD\n", calls[1]["text"])
        changed = dict(kwargs, reason="EV below the fee hurdle (6 symbols, ev 6)")
        self.assertTrue(send_hold(**changed, now=1 + 901))
        self.assertIn("EV below the fee hurdle", calls[2]["text"])
        self.assertIn("\nHOLD\n", calls[2]["text"])
        other = dict(kwargs, candidates=["XRPTMN"])
        self.assertTrue(send_hold(**other, now=1 + 902))
        self.assertEqual(len(calls), 4)

    def test_shadow_prefix_and_a_failed_hold_stay_quiet(self):
        calls, post = self._capture()
        self._arm()

        def explode(url, body):
            raise RuntimeError(url)

        with self.assertLogs("trader.alerts", level="WARNING") as captured:
            sent = send_hold(
                mode="shadow", ops_state="SHADOW", when=self.when, reason="warm-up not complete (4/18)",
                candidates=[], cash=500, positions=[], post=explode, now=5,
            )
        self.assertFalse(sent)
        blob = "\n".join(captured.output)
        self.assertNotIn("secret-token", blob)
        self.assertIn("RuntimeError", blob)
        self.assertTrue(send_hold(
            mode="shadow", ops_state="SHADOW", when=self.when, reason="warm-up not complete (4/18)",
            candidates=[], cash=500, positions=[], post=post, now=5 + 900,
        ))
        self.assertTrue(calls[0]["text"].startswith("[SHADOW] "))
        self.assertIn("SHADOW", calls[0]["text"])
        self.assertIn("still holding (2 cycles), same reason", calls[0]["text"])

    def test_a_trade_cycle_does_not_also_post_hold(self):
        calls, post = self._capture()
        self._arm()
        original = runtime.send_hold
        runtime.send_hold = lambda **kwargs: original(**kwargs, post=post)
        try:
            runtime._relay_hold(
                "live", self.when, {"state": "LIVE_ARMED"},
                SimpleNamespace(entries_blocked=False, block_reason=""),
                SimpleNamespace(zscore_n=25),
                {"ops": {"zscore_min_n": 18}},
                [], [], None,
                SimpleNamespace(actions=[SimpleNamespace(side="SELL")], rejected=[]),
                True, "", SimpleNamespace(cash=10, positions={}),
            )
            runtime._relay_hold(
                "live", self.when, {"state": "LIVE_ARMED"},
                SimpleNamespace(entries_blocked=False, block_reason=""),
                SimpleNamespace(zscore_n=25),
                {"ops": {"zscore_min_n": 18}},
                self._features(), [], None,
                SimpleNamespace(actions=[], rejected=[]),
                True, "", SimpleNamespace(cash=10, positions={}),
            )
        finally:
            runtime.send_hold = original
        self.assertEqual(len(calls), 1)
        self.assertIn("main filter return_24h 3", calls[0]["text"])


if __name__ == "__main__":
    unittest.main()
