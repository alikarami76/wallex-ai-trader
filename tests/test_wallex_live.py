"""Live Wallex order handling: adopt, fees, reconcile, and fatal codes."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trader.dashboard import pnl_against_base
from trader.engine.portfolio import Portfolio, Position
from trader.engine.runtime import (
    _apply_fill_delta,
    _execute,
    _halt_fatal,
    _reconcile,
    _sync_pending,
)
from trader.report import pnl_against_base as report_base
from trader.risk.manager import Action
from trader.timeutil import exchange_epoch
from trader.types import Balance, BookLevel, MarketRule, OrderBook, OrderRecord
from trader.wallex.client import WallexClient, WallexError, fill_totals, subaccount_blocked
from trader.wallex.precision import OrderRejected


def _rule() -> MarketRule:
    return MarketRule(
        "BTCTMN", "BTC", "TMN", 0, 2, 0.01, 50_000,
        market_orders_enabled=True, last_price=100_000, bid_price=100_000, ask_price=100_100,
    )


def _cfg() -> dict:
    return {
        "fees": {"maker_rate": 0.0025, "taker_rate": 0.003, "slippage_bps": 3},
        "execution": {
            "entry": "limit_maker",
            "exit": "limit_maker",
            "quote_gamma": 0.5,
            "stop_slippage_bps": 5,
        },
        "universe": {"allowlist": ["BTCTMN"], "denylist": []},
        "risk": {
            "max_fraction_per_trade": 0.12,
            "max_fraction_per_symbol": 0.12,
            "max_gross_exposure": 0.30,
            "stop_loss_pct": 0.045,
            "time_stop_hours": 24,
            "min_notional_tmn": 50_000,
            "min_confidence": 0.55,
        },
    }


class _Script(BaseHTTPRequestHandler):
    script: dict

    def log_message(self, fmt: str, *args) -> None:
        return

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode())

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            return

    def do_GET(self) -> None:
        script = self.script
        path = self.path.split("?", 1)[0]
        script["gets"] = script.get("gets", 0) + 1
        if path == "/v1/depth":
            self._send(200, {"success": True, "result": script["depth"]})
            return
        if path == "/v1/account/balances":
            self._send(200, {"success": True, "result": {"balances": script["balances"]}})
            return
        if path.startswith("/v1/account/orders/"):
            client_id = path.rsplit("/", 1)[-1]
            order = script["orders"].get(client_id)
            if order is None:
                self._send(200, {"success": True, "result": {"clientOrderId": client_id, "status": "NOT_FOUND"}})
                return
            self._send(200, {"success": True, "result": order})
            return
        if path == "/v1/account/orders":
            page = 1
            if "page=" in self.path:
                page = int(self.path.split("page=", 1)[1].split("&", 1)[0])
            pages = script["pages"]
            rows = pages.get(page) or []
            self._send(200, {
                "success": True,
                "result": {"orders": rows},
                "result_info": {"page": page, "has_more_pages": page < max(pages)},
            })
            return
        if path == "/sub-accounts":
            self._send(200, {"success": True, "result": script["accounts"]})
            return
        if path == "/v1/markets":
            if script.get("retry_after"):
                script["retry_after"] = False
                self._send(429, {"success": False, "message": "slow down"}, {"Retry-After": "1.25"})
                return
            self._send(200, {"success": True, "result": {"symbols": {}}})
            return
        self._send(404, {"success": False, "message": "missing"})

    def do_POST(self) -> None:
        script = self.script
        body = self._read()
        script["posts"] = script.get("posts", 0) + 1
        script["bodies"].append(body)
        mode = script["mode"]
        client_id = str(body.get("client_id") or "")
        if mode == "duplicate":
            self._send(422, {
                "success": False,
                "message": "duplicate client_id",
                "code": 422,
                "result": {"error_code": 422},
            })
            return
        if mode == "timeout":
            script["orders"][client_id] = {
                "clientOrderId": client_id,
                "symbol": body.get("symbol"),
                "side": body.get("side"),
                "type": body.get("type"),
                "status": "NEW",
                "active": True,
                "origQty": body.get("quantity"),
                "executedQty": 0,
                "price": body.get("price"),
            }
            time.sleep(script.get("stall") or 1.0)
            self._send(201, {"success": True, "result": script["orders"][client_id]})
            return
        if mode == "fatal":
            self._send(400, {"success": False, "message": "account", "result": {"error_code": 1203}})
            return
        if mode == "balance" and script["posts"] == 1:
            self._send(400, {"success": False, "message": "insufficient", "code": 1006})
            return
        if mode == "pace":
            self._send(
                201,
                {"success": True, "result": {"clientOrderId": client_id, "status": "NEW", "active": True}},
                {"x-ratelimit-remaining": "1", "x-ratelimit-limit": "100"},
            )
            return
        order = {
            "clientOrderId": client_id,
            "symbol": body.get("symbol"),
            "side": body.get("side"),
            "type": body.get("type"),
            "status": "FILLED",
            "active": False,
            "origQty": body.get("quantity"),
            "executedQty": body.get("quantity"),
            "executedPrice": body.get("price") or 100_000,
            "executedSum": float(body.get("quantity") or 0) * float(body.get("price") or 100_000),
            "fills": [{
                "price": body.get("price") or 100_000,
                "quantity": body.get("quantity"),
                "sum": float(body.get("quantity") or 0) * float(body.get("price") or 100_000),
                "fee": 0,
                "feeAsset": "TMN",
                "maker": False,
            }],
        }
        script["orders"][client_id] = order
        self._send(201, {"success": True, "result": order})


def _serve(script: dict) -> tuple[ThreadingHTTPServer, WallexClient]:
    handler = type("Handler", (_Script,), {"script": script})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    client = WallexClient(
        base_url=f"http://{host}:{port}",
        api_key="test-key",
        subaccount_id="sub-1",
        timeout=0.3,
        max_retries=3,
    )
    return server, client


def _depth(bid: float = 99, ask: float = 101) -> dict:
    return {
        "bid": [{"price": bid, "quantity": 2}],
        "ask": [{"price": ask, "quantity": 2}],
    }


class ClientTests(unittest.TestCase):
    def test_duplicate_client_id_is_adopted_without_a_second_post(self):
        script = {
            "mode": "duplicate",
            "depth": _depth(99_000, 100_100),
            "orders": {"SC1": {
                "clientOrderId": "SC1",
                "symbol": "BTCTMN",
                "side": "BUY",
                "type": "LIMIT",
                "status": "NEW",
                "active": True,
                "origQty": "1",
                "executedQty": "0",
                "price": "99",
            }},
            "bodies": [],
            "balances": {},
            "pages": {1: []},
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            response = client.place_order(
                symbol="BTCTMN", side="BUY", order_type="LIMIT", quantity=1, price=100_000,
                client_id="SC1", rule=_rule(), price_direction="down", maker=True,
            )
        finally:
            server.shutdown()
        self.assertTrue(response.get("adopted"))
        self.assertEqual(response["result"]["clientOrderId"], "SC1")
        self.assertEqual(script["posts"], 1)

    def test_a_timed_out_post_adopts_the_order_already_on_the_book(self):
        script = {
            "mode": "timeout",
            "stall": 0.8,
            "depth": _depth(99_000, 100_100),
            "orders": {},
            "bodies": [],
            "balances": {},
            "pages": {1: []},
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            response = client.place_order(
                symbol="BTCTMN", side="BUY", order_type="LIMIT", quantity=1, price=100_000,
                client_id="SC2", rule=_rule(), price_direction="down", maker=True,
            )
        finally:
            server.shutdown()
        self.assertTrue(response.get("adopted"))
        self.assertEqual(script["posts"], 1)
        self.assertEqual(response["result"]["clientOrderId"], "SC2")

    def test_error_code_comes_from_the_body_and_1203_is_not_retried(self):
        parsed = WallexError("account", status=400, payload={"result": {"error_code": 1203}})
        self.assertEqual(parsed.code, 1203)
        top = WallexError("account", status=400, payload={"code": 1201})
        self.assertEqual(top.code, 1201)
        script = {
            "mode": "fatal",
            "depth": _depth(100_000, 100_100),
            "orders": {},
            "bodies": [],
            "balances": {},
            "pages": {1: []},
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            with self.assertRaises(WallexError) as caught:
                client.place_order(
                    symbol="BTCTMN", side="SELL", order_type="LIMIT", quantity=1, price=100_200,
                    client_id="SC3", rule=_rule(), price_direction="up", maker=True,
                )
        finally:
            server.shutdown()
        self.assertEqual(caught.exception.code, 1203)
        self.assertEqual(script["posts"], 1)

    def test_a_sell_rejected_for_balance_retries_once_with_the_available_qty(self):
        script = {
            "mode": "balance",
            "depth": _depth(100_000, 100_100),
            "orders": {},
            "bodies": [],
            "balances": {"BTC": {"asset": "BTC", "value": 1.5, "locked": 0.75}},
            "pages": {1: []},
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            response = client.place_order(
                symbol="BTCTMN", side="SELL", order_type="LIMIT", quantity=1.5, price=100_200,
                client_id="SC4", rule=_rule(), price_direction="up", maker=True,
            )
        finally:
            server.shutdown()
        self.assertEqual(script["posts"], 2)
        self.assertEqual(script["bodies"][1]["quantity"], "0.75")
        self.assertFalse(response.get("adopted"))

    def test_maker_quotes_that_cross_the_touch_are_refused(self):
        script = {
            "mode": "ok",
            "depth": _depth(100_000, 100_100),
            "orders": {},
            "bodies": [],
            "balances": {},
            "pages": {1: []},
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            with self.assertRaises(OrderRejected):
                client.place_order(
                    symbol="BTCTMN", side="BUY", order_type="LIMIT", quantity=1, price=100_100,
                    client_id="SC5", rule=_rule(), price_direction="down", maker=True,
                )
            with self.assertRaises(OrderRejected):
                client.place_order(
                    symbol="BTCTMN", side="SELL", order_type="LIMIT", quantity=1, price=100_000,
                    client_id="SC6", rule=_rule(), price_direction="up", maker=True,
                )
            self.assertEqual(script.get("posts", 0), 0)
        finally:
            server.shutdown()

    def test_low_rate_limit_remaining_backs_off_and_retry_after_is_honoured(self):
        script = {
            "mode": "pace",
            "depth": _depth(99_000, 100_100),
            "orders": {},
            "bodies": [],
            "balances": {},
            "pages": {1: []},
            "accounts": [],
            "retry_after": True,
        }
        server, client = _serve(script)
        slept: list[float] = []
        original = client._wait_pace

        def _record() -> None:
            remaining = client._pace_until - time.monotonic()
            if remaining > 0:
                slept.append(remaining)
            client._pace_until = 0

        client._wait_pace = _record
        try:
            client.place_order(
                symbol="BTCTMN", side="BUY", order_type="LIMIT", quantity=1, price=100_000,
                client_id="SC7", rule=_rule(), price_direction="down", maker=True,
            )
            self.assertGreater(client._pace_until, time.monotonic())
            import trader.wallex.client as client_module
            delays: list[float] = []
            original_sleep = client_module.time.sleep
            client_module.time.sleep = lambda seconds: delays.append(seconds)
            try:
                client.get_markets()
            finally:
                client_module.time.sleep = original_sleep
        finally:
            client._wait_pace = original
            server.shutdown()
        self.assertEqual(delays, [1.25])

    def test_order_history_follows_has_more_pages(self):
        script = {
            "mode": "ok",
            "depth": _depth(),
            "orders": {},
            "bodies": [],
            "balances": {},
            "pages": {
                1: [{"clientOrderId": "A", "symbol": "BTCTMN", "side": "BUY", "status": "FILLED"}],
                2: [{"clientOrderId": "B", "symbol": "BTCTMN", "side": "SELL", "status": "FILLED"}],
            },
            "accounts": [],
        }
        server, client = _serve(script)
        try:
            rows = client.get_order_history("BTCTMN")
        finally:
            server.shutdown()
        self.assertEqual([row.client_order_id for row in rows], ["A", "B"])

    def test_subaccount_mismatch_or_suspension_blocks_a_live_arm(self):
        self.assertEqual(
            subaccount_blocked({"clientId": "other", "matched": True, "isActive": True, "isSuspend": False}, "sub-1"),
            "WALLEX_SUBACCOUNT_ID does not match the sub-account clientId",
        )
        self.assertIn("suspended", subaccount_blocked(
            {"clientId": "sub-1", "matched": True, "isActive": True, "isSuspend": True}, "sub-1",
        ))
        self.assertIn("inactive", subaccount_blocked(
            {"clientId": "sub-1", "matched": True, "isActive": False, "isSuspend": False}, "sub-1",
        ))
        self.assertIsNone(subaccount_blocked(
            {"clientId": "sub-1", "matched": True, "isActive": True, "isSuspend": False}, "sub-1",
        ))

    def test_exchange_timestamps_are_utc(self):
        iso = exchange_epoch("2026-10-22T12:00:00Z")
        self.assertIsNotNone(iso)
        self.assertEqual(exchange_epoch(1_700_000_000), 1_700_000_000)
        self.assertEqual(exchange_epoch(1_700_000_000_000), 1_700_000_000)


class FillTests(unittest.TestCase):
    def test_sync_applies_only_the_new_executed_qty(self):
        portfolio = Portfolio(5_000_000)
        portfolio.positions["BTCTMN"] = Position(
            "BTCTMN", 1.0, 100_000, 0, 100_000, 95_000, 100_000, "seed", "seed",
        )
        order = OrderRecord(
            "SCSELL", "BTCTMN", "SELL", "LIMIT", 100_000, 1.0, 0.4, 40_000, 100_000,
            "PARTIALLY_FILLED", True, raw={"fills": [{
                "quantity": 0.4, "price": 100_000, "sum": 40_000, "fee": 100, "feeAsset": "TMN", "maker": True,
            }]},
        )
        state = {"pending": {"SCSELL": {
            "symbol": "BTCTMN", "side": "SELL", "price": 100_000, "qty": 1.0, "fee": 0.0025,
            "reason": "rest", "order_type": "LIMIT", "created": time.time(), "applied_qty": 0,
        }}}

        class _Client:
            def get_order(self, client_id):
                return order

            def cancel_order(self, client_id):
                raise AssertionError("a resting partial sell stays open")

        _sync_pending(_Client(), portfolio, state)
        self.assertAlmostEqual(portfolio.positions["BTCTMN"].qty, 0.6)
        self.assertAlmostEqual(state["pending"]["SCSELL"]["applied_qty"], 0.4)
        self.assertAlmostEqual(portfolio.cash, 5_000_000 + 40_000 - 100)
        order.executed_qty = 0.9
        order.executed_sum = 90_000
        order.raw["fills"].append({
            "quantity": 0.5, "price": 100_000, "sum": 50_000, "fee": 150, "feeAsset": "TMN", "maker": True,
        })
        _sync_pending(_Client(), portfolio, state)
        self.assertAlmostEqual(portfolio.positions["BTCTMN"].qty, 0.1)
        self.assertAlmostEqual(portfolio.cash, 5_000_000 + 90_000 - 250)
        self.assertAlmostEqual(state["pending"]["SCSELL"]["applied_qty"], 0.9)

    def test_a_partial_buy_books_the_base_fee_and_cancels_the_rest(self):
        portfolio = Portfolio(5_000_000)
        order = OrderRecord(
            "SCBUY", "BTCTMN", "BUY", "LIMIT", 100_000, 1.0, 0.4, 40_000, 100_000,
            "PARTIALLY_FILLED", True, raw={"fills": [{
                "quantity": 0.4, "price": 100_000, "sum": 40_000, "fee": 0.001, "feeAsset": "BTC", "maker": True,
            }]},
        )
        cancelled = []

        class _Client:
            def get_order(self, client_id):
                return order

            def cancel_order(self, client_id):
                cancelled.append(client_id)
                order.active = False
                order.status = "CANCELED"

        state = {"pending": {"SCBUY": {
            "symbol": "BTCTMN", "side": "BUY", "price": 100_000, "qty": 1.0, "fee": 0.0025,
            "reason": "rest", "order_type": "LIMIT", "created": time.time(), "applied_qty": 0,
        }}}
        _sync_pending(_Client(), portfolio, state)
        self.assertEqual(cancelled, ["SCBUY"])
        self.assertNotIn("SCBUY", state["pending"])
        self.assertAlmostEqual(portfolio.positions["BTCTMN"].qty, 0.399)
        self.assertAlmostEqual(portfolio.cash, 5_000_000 - 40_000)

    def test_reconcile_uses_total_for_coins_and_available_for_cash(self):
        portfolio = Portfolio(1)
        state = {"portfolio": portfolio}

        class _Client:
            def get_balances(self):
                return {
                    "TMN": Balance("TMN", 5_000_000, 100_000),
                    "BTC": Balance("BTC", 1.5, 1.0),
                }

            def get_open_orders(self):
                return [OrderRecord(
                    "SCOPEN", "BTCTMN", "BUY", "LIMIT", 99_000, 0.2, 0, 0, None, "NEW", True,
                    created_at=exchange_epoch("2026-10-22T12:00:00Z"),
                    raw={"created_at": "2026-10-22T12:00:00Z"},
                )]

            def get_fees(self):
                return {"BTCTMN": (0.0025, 0.003)}

        rules = {"BTCTMN": _rule()}
        _reconcile(_Client(), state, rules)
        self.assertEqual(portfolio.cash, 4_900_000)
        self.assertAlmostEqual(portfolio.positions["BTCTMN"].qty, 1.5)
        pending = state["pending"]["SCOPEN"]
        self.assertEqual(pending["price"], 99_000)
        self.assertEqual(pending["origQty"], 0.2)
        self.assertEqual(pending["fee"], 0.0025)
        self.assertEqual(pending["created"], exchange_epoch("2026-10-22T12:00:00Z"))

    def test_a_cancelled_market_exit_falls_back_to_an_aggressive_limit(self):
        portfolio = Portfolio(1_000_000)
        portfolio.positions["BTCTMN"] = Position(
            "BTCTMN", 1.0, 100_000, 0, 100_000, 95_000, 100_000, "seed", "stop",
        )
        book = OrderBook("BTCTMN", [BookLevel(100_000, 2)], [BookLevel(100_100, 2)])

        class _Client(WallexClient):
            def __init__(self):
                super().__init__(api_key="k", dry_run=False)
                self.placed = []
                self.orders = {}

            def place_order(self, **kwargs):
                self.placed.append(dict(kwargs))
                client_id = kwargs["client_id"]
                if kwargs["order_type"] == "MARKET":
                    self.orders[client_id] = OrderRecord(
                        client_id, "BTCTMN", "SELL", "MARKET", None, kwargs["quantity"], 0, 0, None,
                        "CANCELED", False,
                    )
                else:
                    price = float(kwargs["price"])
                    qty = float(kwargs["quantity"])
                    self.orders[client_id] = OrderRecord(
                        client_id, "BTCTMN", "SELL", "LIMIT", price, qty, qty, qty * price, price,
                        "FILLED", False,
                    )
                return {"liquidity": "taker"}

            def get_order(self, client_id):
                return self.orders[client_id]

        client = _Client()
        action = Action("BTCTMN", "SELL", "MARKET", 1.0, "stop", "stop", "down", aggressive=True)
        state = {"client_seq": 0}
        fill, _request = _execute(
            _cfg(), client, portfolio, action, {"BTCTMN": book}, {"BTCTMN": 100_000},
            {"BTCTMN": _rule()}, state, True, {}, unavailable=set(), ops_state="FLATTEN",
            guard_blocks=[],
        )
        self.assertEqual([row["order_type"] for row in client.placed], ["MARKET", "LIMIT"])
        self.assertFalse(client.placed[1]["maker"])
        self.assertLess(client.placed[1]["price"], 100_000)
        self.assertIsNotNone(fill)
        self.assertNotIn("BTCTMN", portfolio.positions)

    def test_fatal_1203_halts_and_alerts(self):
        sent = []
        import trader.engine.runtime as runtime
        original = runtime.notify
        runtime.notify = lambda key, text, **kwargs: sent.append((key, text)) or True
        try:
            state = {}
            _halt_fatal(state, WallexError("disabled", status=400, payload={"result": {"error_code": 1203}}))
        finally:
            runtime.notify = original
        self.assertEqual(state["ops"]["state"], "HALT")
        self.assertEqual(state["ops"]["fatal_exchange_code"], 1203)
        self.assertEqual(sent[0][0], "fatal:1203")

    def test_buy_fees_in_the_base_asset_reduce_the_coin_balance(self):
        order = OrderRecord(
            "SC", "BTCTMN", "BUY", "LIMIT", 100, 2, 2, 200, 100, "FILLED", False,
            raw={"fills": [{
                "quantity": 2, "price": 100, "sum": 200, "fee": 0.005, "feeAsset": "BTC", "maker": True,
            }]},
        )
        totals = fill_totals(order)
        portfolio = Portfolio(1_000)
        meta = {"applied_qty": 0, "applied_quote": 0, "applied_base_fee": 0, "applied_quote_fee": 0, "price": 100}
        fill = _apply_fill_delta(
            portfolio, side="BUY", symbol="BTCTMN", order=order, meta=meta, totals=totals,
            stop_price=90, setup="uptrend_pullback", reason="fill", client_id="SC",
            order_type="LIMIT", fee_rate=0.0025, when=0,
        )
        self.assertAlmostEqual(portfolio.positions["BTCTMN"].qty, 1.995)
        self.assertAlmostEqual(portfolio.cash, 800)
        self.assertAlmostEqual(fill.fee_tmn, 0.5)


class SurfaceTests(unittest.TestCase):
    def test_dashboard_and_report_use_the_five_million_base_and_skip_prop_packages(self):
        fraction, tmn = pnl_against_base(5_010_000)
        self.assertAlmostEqual(fraction, 0.002)
        self.assertAlmostEqual(tmn, 10_000)
        self.assertAlmostEqual(report_base(4_990_000), -10_000)
        from pathlib import Path
        for path in (Path("trader/dashboard.py"), Path("trader/report.py")):
            self.assertNotIn("prop/user/packages", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
