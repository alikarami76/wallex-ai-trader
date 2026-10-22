"""Local Wallex, BoxAPI, and Telegram stand-ins.

The handler speaks the JSON the real client already parses. It checks the
sub-account header on private routes and never stores the header values.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from trader.e2e.series import book, hourly_bars, minute_bars


# Captured before the harness replaces time.sleep with the virtual clock.
_REAL_SLEEP = time.sleep

CANDIDATES = (
    "USDTTMN",
    "BTCTMN",
    "ETHTMN",
    "XAUTTMN",
    "XRPTMN",
    "DOGETMN",
    "SOLTMN",
    "BNBTMN",
    "ADATMN",
)
WIDE = {"SOLTMN", "BNBTMN", "ADATMN"}
MAKER_FEE = 0.0025
TAKER_FEE = 0.0030


class World:
    """Scripted book, balances, and fault queues shared with the test thread."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.base = ""
        self.llm_calls = 0
        self.telegram: list[dict] = []
        self.sleeps: list[float] = []
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.kinds = {symbol: "flat" for symbol in CANDIDATES}
            self.kinds["BTCTMN"] = "dip"
            self.scale = {symbol: 1.0 for symbol in CANDIDATES}
            self.cash = 5_000_000.0
            self.balances = {
                "TMN": {"total": self.cash, "available": self.cash, "freeze": 0.0},
            }
            self.orders: dict[str, dict] = {}
            self.fills: list[dict] = []
            self.fill_ratio = 1.0
            self.market_faults: list[int] = []
            self.order_faults: list[int] = []
            self.order_stalls = 0
            self.llm_mode = "accept"
            self.llm_calls = 0
            self.order_posts = 0
            self.market_auto_cancel = False
            self.history_pages = 1
            self.private_calls = 0
            self.header_ok = 0
            self.header_missing = 0
            self.nonspot_hits = 0
            self.had_key = False
            self.had_sub = False
            self.telegram.clear()
            self.sleeps.clear()

    def note_sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(round(float(seconds), 4))

    def mid(self, symbol: str) -> float:
        end = int(self.clock.time())
        kind = self.kinds.get(symbol, "flat")
        close = hourly_bars(kind, end, count=100)[-1].close
        return close * float(self.scale.get(symbol, 1.0))

    def touch(self, symbol: str) -> tuple[float, float, dict]:
        spread = 40.0 if symbol in WIDE else 10.0
        payload = book(symbol, self.mid(symbol), spread_bps=spread)
        bid = float(payload["bid"][0]["price"])
        ask = float(payload["ask"][0]["price"])
        # tickSize 0 means integer prices. Keep the ask strictly above the bid.
        bid_i = int(bid)
        ask_i = max(int(ask) + (0 if ask > int(ask) else 0), bid_i + 1)
        if ask_i <= bid_i:
            ask_i = bid_i + 1
        payload["bid"][0]["price"] = str(bid_i)
        payload["ask"][0]["price"] = str(ask_i)
        return float(bid_i), float(ask_i), payload

    def bars(self, symbol: str, resolution: str, start: int, end: int) -> dict:
        kind = self.kinds.get(symbol, "flat")
        if resolution in {"60", "240", "720", "D"}:
            rows = hourly_bars(kind, int(self.clock.time()), count=120)
        else:
            rows = minute_bars(kind, int(self.clock.time()), hours=40)
        chosen = [bar for bar in rows if start <= bar.time < end]
        if not chosen:
            return {"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        return {
            "s": "ok",
            "t": [bar.time for bar in chosen],
            "o": [bar.open for bar in chosen],
            "h": [bar.high for bar in chosen],
            "l": [bar.low for bar in chosen],
            "c": [bar.close for bar in chosen],
            "v": [bar.volume for bar in chosen],
        }

    def markets(self) -> dict:
        symbols = {}
        for symbol in CANDIDATES:
            bid, ask, _payload = self.touch(symbol)
            mid = (bid + ask) / 2
            base = symbol[:-3]
            symbols[symbol] = {
                "symbol": symbol,
                "baseAsset": base,
                "quoteAsset": "TMN",
                "tickSize": 0,
                "stepSize": 8,
                "minQty": 1e-8,
                "minNotional": 50000,
                "isZeroFee": False,
                "isMarketTypeEnable": True,
                "stats": {
                    "24h_quoteVolume": 9_000_000_000_000,
                    "24h_ch": 1.5,
                    "lastPrice": mid,
                    "bidPrice": bid,
                    "askPrice": ask,
                },
            }
        return {"success": True, "result": {"symbols": symbols}}

    def depth_all(self) -> dict:
        result = {}
        for symbol in CANDIDATES:
            _bid, _ask, payload = self.touch(symbol)
            result[symbol] = payload
        return {"success": True, "result": result}

    def _asset(self, symbol: str) -> str:
        return symbol[:-3] if symbol.endswith("TMN") else symbol

    def _credit(self, asset: str, qty: float) -> None:
        row = self.balances.setdefault(asset, {"total": 0.0, "available": 0.0, "freeze": 0.0})
        row["available"] = float(row["available"]) + qty
        row["total"] = float(row["available"]) + float(row["freeze"])

    def _sync_tmn(self) -> None:
        row = self.balances.setdefault("TMN", {"total": 0.0, "available": 0.0, "freeze": 0.0})
        row["available"] = self.cash
        row["total"] = self.cash + float(row["freeze"])

    def place(self, body: dict) -> tuple[int, dict]:
        """Fill or refuse one order. Caller holds no lock across a stall."""
        symbol = str(body.get("symbol") or "").upper()
        side = str(body.get("side") or "").upper()
        order_type = str(body.get("type") or "").upper()
        client_id = str(body.get("client_id") or body.get("clientOrderId") or "")
        qty = float(body.get("quantity") or 0)
        price = float(body["price"]) if body.get("price") not in (None, "") else None
        with self.lock:
            self.order_posts += 1
            duplicate = client_id in self.orders
            stall = self.order_stalls > 0
            if stall:
                self.order_stalls -= 1
                fault = None
            else:
                fault = self.order_faults.pop(0) if self.order_faults else None
        if duplicate:
            return 422, {
                "success": False,
                "code": 422,
                "message": "duplicate client_id",
                "result": {"error_code": 422},
            }
        if stall:
            # Longer than the fault scenario's 1s client timeout, so the client
            # always sees a timeout and the later close of this socket is abandoned.
            _REAL_SLEEP(1.5)
            return 0, {}
        if fault:
            return int(fault), {"success": False, "message": "rate limited" if int(fault) == 429 else "rejected"}
        bid, ask, _payload = self.touch(symbol)
        fill_price = None
        ratio = 1.0
        fee_rate = MAKER_FEE
        with self.lock:
            ratio = self.fill_ratio if side == "BUY" else 1.0
        if side == "BUY" and order_type == "LIMIT" and price is not None and price < ask:
            fill_price = price
            fee_rate = MAKER_FEE
        elif side == "SELL" and order_type == "MARKET":
            fill_price = bid
            fee_rate = TAKER_FEE
        elif side == "SELL" and order_type == "LIMIT" and price is not None and price <= ask:
            # A maker offer sits on the ask. A crossing limit (at or through the bid) pays the taker fee.
            fill_price = price if price > bid else bid
            fee_rate = MAKER_FEE if price >= ask else TAKER_FEE
        executed = 0.0 if fill_price is None else qty * ratio
        if order_type == "MARKET" and self.market_auto_cancel:
            executed = 0.0
            fill_price = None
        status = "NEW" if executed <= 0 else ("FILLED" if executed + 1e-12 >= qty else "PARTIALLY_FILLED")
        if order_type == "MARKET" and self.market_auto_cancel:
            status = "CANCELED"
        asset = self._asset(symbol)
        fee_amount = 0.0 if fill_price is None else (
            executed * fee_rate if side == "BUY" else executed * fill_price * fee_rate
        )
        fills = []
        if executed > 0 and fill_price is not None:
            fills.append({
                "price": fill_price,
                "quantity": executed,
                "sum": executed * fill_price,
                "fee": fee_amount,
                "feeAsset": asset if side == "BUY" else "TMN",
                "maker": fee_rate == MAKER_FEE,
            })
        order = {
            "clientOrderId": client_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": None if price is None else str(int(price)),
            "origQty": qty,
            "executedQty": executed,
            "executedSum": 0.0 if fill_price is None else executed * fill_price,
            "executedPrice": fill_price,
            "status": status,
            "active": status not in {"FILLED", "CANCELED"} and executed + 1e-12 < qty,
            "fee": fee_amount,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fills": fills,
        }
        with self.lock:
            self.orders[client_id] = order
            if executed > 0 and fill_price is not None:
                self._apply_fill(symbol, side, executed, fill_price, fee_rate)
                self.fills.append({
                    "client_id": client_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": executed,
                    "price": fill_price,
                    "type": order_type,
                })
        return 201, {"success": True, "result": order}

    def _apply_fill(self, symbol: str, side: str, qty: float, price: float, fee_rate: float) -> None:
        asset = self._asset(symbol)
        if side == "BUY":
            self.cash -= price * qty
            self._credit(asset, qty * (1 - fee_rate))
        else:
            self.cash += price * qty * (1 - fee_rate)
            self._credit(asset, -qty)
            row = self.balances.get(asset)
            if row is not None and float(row["available"]) <= 1e-10:
                self.balances.pop(asset, None)
        self._sync_tmn()

    def cancel(self, client_id: str) -> dict:
        with self.lock:
            order = self.orders.get(client_id)
            if order is None:
                return {"success": False, "message": "unknown order"}
            order["active"] = False
            order["status"] = "CANCELED" if float(order.get("executedQty") or 0) <= 0 else order["status"]
            return {"success": True, "result": order}

    def order_view(self, client_id: str) -> dict:
        with self.lock:
            order = self.orders.get(client_id) or {
                "clientOrderId": client_id,
                "status": "NOT_FOUND",
                "active": False,
                "executedQty": 0,
            }
            return {"success": True, "result": dict(order)}

    def open_orders(self) -> dict:
        with self.lock:
            rows = [dict(order) for order in self.orders.values() if order.get("active")]
        return {"success": True, "result": {"orders": rows}}

    def balances_payload(self) -> dict:
        with self.lock:
            result = {}
            for asset, row in self.balances.items():
                result[asset] = {
                    "symbol": asset,
                    "total": row["total"],
                    "available": row["available"],
                    "freeze": row["freeze"],
                }
            return {"success": True, "result": result}

    def fees(self) -> dict:
        result = {}
        for symbol in CANDIDATES:
            result[symbol] = {"makerFeeRate": MAKER_FEE, "takerFeeRate": TAKER_FEE}
        return {"success": True, "result": result}

    def llm(self, raw: bytes) -> tuple[int, dict]:
        with self.lock:
            self.llm_calls += 1
            mode = self.llm_mode
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        prompt = json.dumps(body)
        if mode == "stall":
            _REAL_SLEEP(2.0)
            mode = "accept"
        if mode == "bad":
            content = "this is not json"
        elif mode == "veto":
            content = json.dumps({
                "action_id": "hold",
                "veto_all": True,
                "rationale": "the book is hostile",
            })
        elif mode == "hold":
            content = json.dumps({"action_id": "hold", "rationale": "nothing to do"})
        else:
            action_id = "hold"
            for token in ("buy_BTCTMN", "buy_ETH", "buy_"):
                if token in prompt and token != "buy_":
                    start = prompt.find("buy_")
                    end = start
                    while end < len(prompt) and (prompt[end].isalnum() or prompt[end] == "_"):
                        end += 1
                    action_id = prompt[start:end]
                    break
            content = json.dumps({
                "action_id": action_id,
                "rationale": "24h return is above 1 percent",
                "size_fraction": 0.99,
            })
        return 200, {
            "id": "mock",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "cost": 0.00001},
        }

    def mcp(self, raw: bytes) -> dict:
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        method = body.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        else:
            result = {"content": [{"type": "text", "text": "mock candles"}]}
        return {"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}


class _Handler(BaseHTTPRequestHandler):
    world: World

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        world = self.world
        parsed = urlsplit(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path.startswith("/v1/account") or path.startswith("/sub-accounts"):
            self._private_headers()
        try:
            status, payload = self._route(method, path, query, raw)
        except Exception as exc:
            status, payload = 500, {"success": False, "message": type(exc).__name__}
        if status == 0:
            return
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-ratelimit-limit", "100")
        self.send_header("x-ratelimit-remaining", "80")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            return

    def _private_headers(self) -> None:
        world = self.world
        key = self.headers.get("X-API-Key") or ""
        sub = self.headers.get("sub-account-client-id") or ""
        with world.lock:
            world.private_calls += 1
            world.had_key = world.had_key or bool(key)
            world.had_sub = world.had_sub or bool(sub)
            if key and sub:
                world.header_ok += 1
            else:
                world.header_missing += 1

    def _route(self, method: str, path: str, query: dict, raw: bytes) -> tuple[int, dict]:
        world = self.world
        if path.startswith("/v1/prop/user/packages"):
            return 401, {"success": False, "code": 401, "message": "unauthorized"}
        private = path.startswith("/v1/account") or path.startswith("/sub-accounts")
        if private and (not self.headers.get("X-API-Key") or not self.headers.get("sub-account-client-id")):
            return 401, {"success": False, "message": "unauthorized"}
        if "margin" in path or "futures" in path or "otc" in path or "easy-trade" in path or "easytrade" in path:
            with world.lock:
                world.nonspot_hits += 1
        if path == "/telegram" and method == "POST":
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            with world.lock:
                world.telegram.append(payload if isinstance(payload, dict) else {})
            return 200, {"ok": True}
        if path == "/llm/v1/chat/completions" and method == "POST":
            return world.llm(raw)
        if path == "/mcp" and method == "POST":
            return 200, world.mcp(raw)
        if path == "/v1/markets" and method == "GET":
            with world.lock:
                fault = world.market_faults.pop(0) if world.market_faults else None
            if fault:
                return int(fault), {"success": False, "message": "unavailable"}
            return 200, world.markets()
        if path.startswith("/v1/prop/packages/") and method == "GET":
            return 200, {"success": True, "result": {"unavailableMarkets": ["FAKECOIN"]}}
        if path == "/v1/depth" and method == "GET":
            symbol = str(query.get("symbol") or "").upper()
            _bid, _ask, payload = world.touch(symbol)
            return 200, {"success": True, "result": payload}
        if path == "/v2/depth/all" and method == "GET":
            return 200, world.depth_all()
        if path == "/v1/trades" and method == "GET":
            return 200, {"success": True, "result": {"latestTrades": []}}
        if path == "/v1/udf/history" and method == "GET":
            symbol = str(query.get("symbol") or "").upper()
            payload = world.bars(symbol, str(query.get("resolution") or "60"), int(float(query.get("from") or 0)), int(float(query.get("to") or 0)))
            return 200, payload
        if path == "/v1/account/balances-detail" and method == "GET":
            return 200, world.balances_payload()
        if path == "/v1/account/balances" and method == "GET":
            detail = world.balances_payload()["result"]
            balances = {
                asset: {"asset": asset, "value": row["total"], "locked": row["freeze"]}
                for asset, row in detail.items()
            }
            return 200, {"success": True, "result": {"balances": balances}}
        if path == "/v1/account/fee" and method == "GET":
            return 200, world.fees()
        if path == "/v1/account/openOrders" and method == "GET":
            return 200, world.open_orders()
        if path == "/sub-accounts" and method == "GET":
            return 200, {
                "success": True,
                "result": [{
                    "title": "hackathon",
                    "clientId": "e2e-sub",
                    "isActive": True,
                    "isSuspend": False,
                }],
            }
        if path == "/sub-accounts/transfers" and method == "GET":
            return 200, {"success": True, "result": []}
        if path == "/v1/account/orders" and method == "GET":
            page = int(query.get("page") or 1)
            payload = world.open_orders()
            payload["result_info"] = {"has_more_pages": page < int(world.history_pages), "page": page}
            return 200, payload
        if path == "/v1/account/orders" and method == "POST":
            try:
                body = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                body = {}
            return world.place(body if isinstance(body, dict) else {})
        prefix = "/v1/account/orders/"
        if path.startswith(prefix) and method == "GET":
            return 200, world.order_view(path[len(prefix):])
        if path.startswith(prefix) and method == "DELETE":
            return 200, world.cancel(path[len(prefix):])
        return 404, {"success": False, "message": "not found"}


def serve(world: World) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"world": world})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="e2e-mock", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    world.base = f"http://{host}:{port}"
    return server
