"""Telegram alerts are optional, rate-limited, and never include the token."""

import json
import os
import unittest

from trader.alerts import _last_sent, notify, report_cycle


class AlertTests(unittest.TestCase):
    def setUp(self):
        _last_sent.clear()
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


if __name__ == "__main__":
    unittest.main()
