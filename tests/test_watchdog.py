"""The sidecar alerts from journals, rate-limits, and does not write trader files."""

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from trader.watchdog import main, run


def _row(**extra) -> dict:
    base = {
        "record": "cycle",
        "ts_utc": "2026-10-22T12:00:00Z",
        "ts_tehran": "2026-10-22T15:30:00+03:30",
        "state": "LIVE_ARMED",
        "n_orders_rejected": 0,
        "n_entry_taker_fills": 0,
        "pnl_from_golive_pct": -0.004,
        "client_id": "SCSECRET",
        "note": "sk-" + "b" * 20,
    }
    base.update(extra)
    return base


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            key: os.environ.get(key)
            for key in ("WATCHDOG_ENABLED", "TELEGRAM_ALERT_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")
        }
        os.environ["WATCHDOG_ENABLED"] = "1"
        os.environ["TELEGRAM_ALERT_BOT_TOKEN"] = "123456:telegram-secret-token"
        os.environ["TELEGRAM_ALERT_CHAT_ID"] = "999"

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_off_switch_sends_nothing_and_writes_nothing(self):
        root = Path(tempfile.mkdtemp())
        os.environ.pop("WATCHDOG_ENABLED", None)
        before = set(root.iterdir())
        self.assertEqual(main(["--log-dir", str(root), "--once"]), 0)
        self.assertEqual(set(root.iterdir()), before)

    def test_four_events_are_percent_only_and_rate_limited(self):
        root = Path(tempfile.mkdtemp())
        sent = []

        def post(url, body):
            sent.append(json.loads(body.decode())["text"])
            if "fail" in json.loads(body.decode())["text"]:
                raise RuntimeError(url)

        (root / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
            _row(ts_utc="2026-10-22T12:00:00Z", state="HALT", n_orders_rejected=2, n_entry_taker_fills=1),
            _row(ts_utc="2026-10-22T12:06:00Z", state="KILLED", n_orders_rejected=1, n_entry_taker_fills=1),
        )), encoding="utf-8")
        state: dict = {}
        first = run(root, once=True, post=post, now=0, state=state)
        self.assertEqual(first, ["HALT", "order_reject", "taker_entry", "cycle_gap", "KILLED"])
        self.assertTrue(all("pnl -0.40%" in text for text in sent))
        blob = " ".join(sent)
        self.assertNotIn("SCSECRET", blob)
        self.assertNotIn("telegram-secret-token", blob)
        self.assertNotIn("sk-", blob)
        self.assertEqual(set(path.name for path in root.iterdir()), {"cycles.jsonl"})
        again = run(root, once=True, post=post, now=10, state=state)
        self.assertEqual(again, [])
        with (root / "cycles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_row(
                ts_utc="2026-10-22T13:00:00Z", state="HALT", n_orders_rejected=1,
            )) + "\n")
        later = run(root, once=True, post=post, now=1000, state=state)
        self.assertIn("HALT", later)
        self.assertIn("order_reject", later)

    def test_failed_send_logs_the_exception_type_only(self):
        root = Path(tempfile.mkdtemp())
        (root / "cycles.jsonl").write_text(json.dumps(_row(state="HALT")) + "\n", encoding="utf-8")

        def post(url, body):
            raise RuntimeError(url)

        with self.assertLogs("trader.watchdog", level=logging.WARNING) as logs:
            sent = run(root, once=True, post=post, now=0)
        self.assertEqual(sent, [])
        text = " ".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("telegram-secret-token", text)
        self.assertNotIn("123456", text)


if __name__ == "__main__":
    unittest.main()
