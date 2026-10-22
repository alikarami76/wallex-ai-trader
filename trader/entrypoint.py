"""Container entrypoint.

Starts the trader, and the journal watchdog beside it when WATCHDOG_ENABLED
is set. Shadow and dry-run do not need a Wallex key. The log names the
variables and never their values. This process does not place orders.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from trader.config import credential_status, runtime_dirs
from trader.watchdog import enabled


log = logging.getLogger("trader.entrypoint")


def journal_dir() -> Path:
    mode = os.getenv("TRADER_MODE", "shadow").strip() or "shadow"
    if mode not in {"shadow", "live"}:
        mode = "shadow"
    dry_run = mode == "shadow" or os.getenv("TRADER_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
    _out, _state, logs = runtime_dirs(None, mode, dry_run)
    return logs


def child_commands(argv: list[str] | None = None) -> list[list[str]]:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["--config", "config/default.yaml"]
    commands: list[list[str]] = []
    if enabled():
        commands.append([sys.executable, "-m", "trader", "watchdog", "--log-dir", str(journal_dir())])
    commands.append([sys.executable, "-m", "trader", *args])
    return commands


def supervise(argv: list[str] | None = None, *, popen=subprocess.Popen, install_signal=signal.signal) -> int:
    """Run the children. SIGTERM and SIGINT are forwarded. The trader's code is the result."""
    commands = child_commands(argv)
    processes = []
    for command in commands:
        if command[2:4] == ["trader", "watchdog"]:
            log.info("watchdog started")
        processes.append(popen(command))
    trader = processes[-1]

    def _stop(signum, _frame) -> None:
        for proc in processes:
            if proc.poll() is None:
                proc.send_signal(signum)

    install_signal(signal.SIGTERM, _stop)
    install_signal(signal.SIGINT, _stop)
    code = trader.wait()
    for proc in processes:
        if proc is trader or proc.poll() is not None:
            continue
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if code is None:
        return 1
    return code if code >= 0 else 128 + abs(code)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("%s", credential_status())
    if enabled():
        log.info("watchdog enabled")
    else:
        log.info("watchdog off")
    return supervise(argv)


if __name__ == "__main__":
    raise SystemExit(main())
