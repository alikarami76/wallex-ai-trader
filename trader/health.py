"""Tiny HTTP server so a long-running trader can satisfy a platform port check.

Liara's Docker platform publishes one HTTP port and can curl it before it
sends traffic to a new release. The trading loop is not a web app, so this
server runs beside it. `/health` is only "the process is up". `/status` is
the latest cycle, with no API keys.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/"}:
            self._json({"ok": True})
            return
        if path == "/status":
            self._json(snapshot())
            return
        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_in_background(host: str, port: int) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    return server
