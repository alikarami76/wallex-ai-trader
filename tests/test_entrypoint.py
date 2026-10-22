"""Container entrypoint: no Wallex key, health stays the trader's job, watchdog is optional."""

import os
import signal
import threading
import unittest
from pathlib import Path

from trader.config import credential_status
from trader.entrypoint import child_commands, journal_dir, supervise


class _Proc:
    def __init__(self, code: int = 0, hold: threading.Event | None = None, started: threading.Event | None = None):
        self.code = code
        self.hold = hold
        self.started = started
        self.signals: list[int] = []
        self.returncode: int | None = None
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        if self.hold is not None:
            self.hold.set()

    def wait(self, timeout: float | None = None):
        if self.started is not None:
            self.started.set()
        if self.hold is not None:
            self.hold.wait(timeout or 2)
        self.returncode = self.code
        return self.code

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.keys = (
            "WALLEX_API_KEY",
            "WALLEX_SUBACCOUNT_ID",
            "WATCHDOG_ENABLED",
            "TRADER_MODE",
            "TRADER_DRY_RUN",
            "TRADER_DATA_DIR",
            "TRADER_LOG_DIR",
            "TRADER_STATE_PATH",
        )
        self.saved = {key: os.environ.get(key) for key in self.keys}
        for key in self.keys:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_absent_keys_are_named_and_not_copied(self):
        os.environ["WALLEX_API_KEY"] = "wallex-secret-value"
        os.environ["WALLEX_SUBACCOUNT_ID"] = "sub-secret-value"
        present = credential_status()
        self.assertIn("WALLEX_API_KEY present", present)
        self.assertIn("WALLEX_SUBACCOUNT_ID present", present)
        self.assertNotIn("wallex-secret-value", present)
        self.assertNotIn("sub-secret-value", present)
        os.environ.pop("WALLEX_API_KEY")
        os.environ.pop("WALLEX_SUBACCOUNT_ID")
        absent = credential_status()
        self.assertIn("WALLEX_API_KEY absent", absent)
        self.assertIn("WALLEX_SUBACCOUNT_ID absent", absent)
        self.assertIn("shadow and dry-run keep serving without orders", absent)

    def test_watchdog_is_off_unless_enabled(self):
        os.environ["TRADER_DATA_DIR"] = "/data"
        commands = child_commands(["--config", "config/default.yaml"])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1:4], ["-m", "trader", "--config"])
        self.assertNotIn("watchdog", commands[0])
        os.environ["WATCHDOG_ENABLED"] = "1"
        os.environ["TRADER_LOG_DIR"] = "/data/journal"
        both = child_commands([])
        self.assertEqual(both[0][1:4], ["-m", "trader", "watchdog"])
        self.assertEqual(both[0][-1], "/data/journal")
        self.assertEqual(both[1][-2:], ["--config", "config/default.yaml"])
        self.assertEqual(journal_dir(), Path("/data/journal"))

    def test_supervise_forwards_stop_and_returns_the_trader_code(self):
        os.environ["WATCHDOG_ENABLED"] = "1"
        os.environ["TRADER_DATA_DIR"] = "/data"
        hold = threading.Event()
        started = threading.Event()
        made: list[_Proc] = []

        def popen(command):
            trader = "watchdog" not in command
            proc = _Proc(code=0, hold=hold if trader else None, started=started if trader else None)
            made.append(proc)
            return proc

        installed = {}

        def install_signal(signum, handler):
            installed[signum] = handler
            return None

        box: dict = {}

        def run():
            box["code"] = supervise(["--config", "config/default.yaml"], popen=popen, install_signal=install_signal)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        installed[signal.SIGTERM](signal.SIGTERM, None)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["code"], 0)
        self.assertEqual(len(made), 2)
        self.assertIn(signal.SIGTERM, made[0].signals)
        self.assertIn(signal.SIGTERM, made[1].signals)

    def test_trader_failure_stops_the_watchdog(self):
        os.environ["WATCHDOG_ENABLED"] = "1"
        watchdog = _Proc(code=0)
        trader = _Proc(code=2)

        def popen(command):
            return watchdog if "watchdog" in command else trader

        code = supervise(["--config", "config/default.yaml"], popen=popen, install_signal=lambda *_args: None)
        self.assertEqual(code, 2)
        self.assertIn(signal.SIGTERM, watchdog.signals)
        self.assertEqual(trader.signals, [])


if __name__ == "__main__":
    unittest.main()
