"""HTTP port for Liara and the read-only desk.

Liara's Docker platform publishes one HTTP port and curls `/health` before it
sends traffic to a new release. `/health` stays open. `/` is the desk and
`/status` is the in-memory snapshot. Both require `DASHBOARD_USERS` or the
`DASHBOARD_USER` / `DASHBOARD_PASSWORD` fallback. Nothing on this port can
place or cancel an order.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_status: dict = {"ok": True}
_status_path: Path | None = None


def configure_status(path: Path) -> None:
    global _status_path
    _status_path = path


def update_status(**fields) -> None:
    """Merge fields into the status snapshot and write it next to the journal."""
    with _lock:
        _status.update(fields)
        _status["ok"] = True
        if _status_path is None:
            return
        _status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_status, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(_status_path)


def snapshot() -> dict:
    with _lock:
        return dict(_status)


class _Bound:
    """The slice of a server the trader and the tests need."""

    def __init__(self, server, thread: threading.Thread, address: tuple[str, int]):
        self._server = server
        self._thread = thread
        self.server_address = address

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def server_close(self) -> None:
        self.shutdown()


def serve_in_background(host: str, port: int, directory: Path | None = None) -> _Bound:
    """Serve `/health` and the desk. Uvicorn does not install signal handlers."""
    import uvicorn

    from trader.dashboard import create_app

    app = create_app(directory)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, name="health", daemon=True)
    thread.start()
    address = _wait_until_bound(server, thread)
    return _Bound(server, thread, address)


def _wait_until_bound(server, thread: threading.Thread) -> tuple[str, int]:
    deadline = time.time() + 5
    while time.time() < deadline:
        for item in getattr(server, "servers", None) or []:
            for sock in getattr(item, "sockets", None) or []:
                host, port = sock.getsockname()[:2]
                return str(host), int(port)
        if not thread.is_alive():
            break
        time.sleep(0.02)
    raise RuntimeError("health server failed to bind")
