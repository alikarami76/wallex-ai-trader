"""Optional client for the official Wallex MCP server.

The trading loop does not need this. When it is reachable, the agent calls
one read-only tool (`wallex_basic_candle_history`) and puts the summary in
the prompt so the model is using a Wallex tool, not only REST. Failures are
logged and ignored.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("trader.mcp")


class WallexMcp:
    def __init__(self, url: str = "https://api.wallex.ir/mcp", api_key: str = "", timeout: float = 8.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._ready = False
        self._failures = 0

    @property
    def disabled(self) -> bool:
        return self._failures >= 2

    def call_tool(self, name: str, arguments: dict) -> str | None:
        if self.disabled:
            return None
        try:
            if not self._ready:
                self._rpc("initialize", {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "starcoach-wallex-trader", "version": "1.0"},
                })
                self._ready = True
            result = self._rpc("tools/call", {"name": name, "arguments": arguments})
            content = (result or {}).get("content") or []
            if content and isinstance(content, list):
                text = content[0].get("text") if isinstance(content[0], dict) else None
                return text
            return json.dumps(result)[:2000]
        except Exception as exc:
            self._failures += 1
            log.warning("MCP tool %s failed (%s). failures=%s", name, exc, self._failures)
            return None

    def _rpc(self, method: str, params: dict) -> dict:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "starcoach-wallex-trader/1.0",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"MCP HTTP {exc.code}: {detail}") from exc
        if raw.startswith("event:") or raw.startswith("data:"):
            raw = _sse_data(raw)
        parsed = json.loads(raw)
        if "error" in parsed:
            raise RuntimeError(str(parsed["error"]))
        return parsed.get("result") or {}


def _sse_data(raw: str) -> str:
    chunks = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunks.append(line[5:].strip())
    return "\n".join(chunks) or raw
