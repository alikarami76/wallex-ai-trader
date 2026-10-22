"""Wallex REST client.

Public market data needs no key. Private calls send `X-API-Key` and, when
`WALLEX_SUBACCOUNT_ID` is set, `sub-account-client-id`. There is no HMAC
signature. Order placement is capped well under the documented 20 requests
per 10 seconds. `dry_run` builds the private write request and returns it
without sending.

The challenge is spot only. The only order write is POST or DELETE
`/v1/account/orders`. Margin, leverage, futures, OTC, and credit-margin
paths are refused before any socket is opened.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from trader.timeutil import exchange_epoch
from trader.types import Balance, Bar, BookLevel, MarketRule, OrderBook, OrderRecord, PreparedOrder
from trader.wallex.precision import OrderRejected, build_order_body, round_qty

log = logging.getLogger("trader.wallex")


_SPOT_GET_EXACT = {
    "/v1/markets",
    "/v1/depth",
    "/v2/depth/all",
    "/v1/trades",
    "/v1/udf/history",
    "/v1/account/balances",
    "/v1/account/balances-detail",
    "/v1/account/fee",
    "/v1/account/openOrders",
    "/v1/account/orders",
    "/sub-accounts",
    "/sub-accounts/transfers",
}
_DENIED_SEGMENTS = ("margin", "leverage", "futures", "otc", "easy-trade", "easytrade", "credit-margin")
# Account-level failures. The loop halts and alerts; it does not send the order again.
FATAL_WALLEX_CODES = frozenset({1201, 1203})


SPOT_ORDER_PATH = "/v1/account/orders"


def assert_spot_order(method: str, path: str) -> None:
    """Order placement is only POST /v1/account/orders. Cancels stay on DELETE of that path."""
    method = (method or "").upper()
    clean = (path or "").split("?", 1)[0].rstrip("/") or "/"
    if method != "POST" or clean != SPOT_ORDER_PATH:
        raise WallexError(
            f"spot-only: order requests are POST {SPOT_ORDER_PATH}, refused {method} {clean}"
        )
    assert_spot_request(method, clean)


def assert_spot_request(method: str, path: str) -> None:
    """Refuse anything except spot reads and `/v1/account/orders` writes."""
    method = (method or "").upper()
    path = (path or "").split("?", 1)[0]
    if not path.startswith("/"):
        raise WallexError(f"spot-only: refused {method} {path}")
    parts = [part for part in path.split("/") if part]
    scan = parts
    if len(parts) >= 4 and parts[:3] == ["v1", "account", "orders"]:
        scan = parts[:3]
    for part in scan:
        lowered = part.lower()
        for marker in _DENIED_SEGMENTS:
            if marker in lowered:
                raise WallexError(f"spot-only: refused {method} {path}")
    if method == "POST":
        if path != "/v1/account/orders":
            raise WallexError(f"spot-only: orders are POST /v1/account/orders, refused {path}")
        return
    if method == "DELETE":
        rest = path[len("/v1/account/orders/"):] if path.startswith("/v1/account/orders/") else ""
        if not rest or "/" in rest:
            raise WallexError(f"spot-only: cancel is DELETE /v1/account/orders/{{id}}, refused {path}")
        return
    if method == "GET":
        if path in _SPOT_GET_EXACT:
            return
        if path.startswith("/v1/prop/user/packages"):
            raise WallexError(f"spot-only: refused GET {path}")
        if path.startswith("/v1/prop/packages/") and path.count("/") == 4:
            return
        if path.startswith("/v1/account/orders/") and "/" not in path[len("/v1/account/orders/"):]:
            return
        raise WallexError(f"spot-only: refused GET {path}")
    raise WallexError(f"spot-only: refused {method} {path}")


class WallexError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        payload: dict | None = None,
        code: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}
        self.code = _error_code(self.payload) if code is None else _coerce_code(code)


@dataclass
class RateLimiter:
    max_calls: int
    period: float
    _times: list[float] = field(default_factory=list)

    def acquire(self) -> None:
        now = time.monotonic()
        self._times = [t for t in self._times if now - t < self.period]
        if len(self._times) >= self.max_calls:
            wait = self.period - (now - self._times[0]) + 0.02
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < self.period]
        self._times.append(time.monotonic())


def aggregate_bars(bars: list[Bar], bucket_seconds: int) -> list[Bar]:
    """Fold base candles into larger buckets. The bucket timestamp is the open."""
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    ordered = sorted(bars, key=lambda b: b.time)
    out: list[Bar] = []
    current: Bar | None = None
    for bar in ordered:
        start = bar.time - (bar.time % bucket_seconds)
        if current is None or current.time != start:
            if current is not None:
                out.append(current)
            current = Bar(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
            continue
        current.high = max(current.high, bar.high)
        current.low = min(current.low, bar.low)
        current.close = bar.close
        current.volume += bar.volume
    if current is not None:
        out.append(current)
    return out


def drop_incomplete(bars: list[Bar], bucket_seconds: int, now: int | None = None) -> list[Bar]:
    if not bars:
        return []
    current = int(now if now is not None else time.time())
    return [b for b in bars if b.time + bucket_seconds <= current]


def median_spacing(bars: list[Bar]) -> float | None:
    if len(bars) < 3:
        return None
    deltas = sorted(bars[i].time - bars[i - 1].time for i in range(1, len(bars)))
    return float(deltas[len(deltas) // 2])


class WallexClient:
    def __init__(
        self,
        base_url: str = "https://api.wallex.ir",
        api_key: str | None = None,
        subaccount_id: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 4,
        dry_run: bool = False,
        user_agent: str = "starcoach-wallex-trader/1.0",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.subaccount_id = subaccount_id or ""
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.user_agent = user_agent
        self._public_limit = RateLimiter(5, 1.0)
        self._order_limit = RateLimiter(15, 10.0)
        self._pace_until = 0.0

    def _headers(self, auth: bool, body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if body:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.api_key:
                raise WallexError("WALLEX_API_KEY is not set")
            headers["X-API-Key"] = self.api_key
            if self.subaccount_id:
                headers["sub-account-client-id"] = self.subaccount_id
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        auth: bool = False,
        is_order: bool = False,
    ) -> dict:
        assert_spot_request(method, path)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        payload = json.dumps(body).encode() if body is not None else None
        headers = self._headers(auth, body is not None)

        if self.dry_run and auth and method in {"POST", "DELETE", "PUT"}:
            prepared = PreparedOrder(method, url, headers, body or {})
            log.info("dry-run %s %s body=%s", method, path, body)
            return {"success": True, "dry_run": True, "result": {}, "request": prepared.redacted()}

        limiter = self._order_limit if is_order else self._public_limit
        # A lost order POST is adopted by client id. Sending it again can double-fill.
        order_post = method == "POST" and path.split("?", 1)[0].rstrip("/") == SPOT_ORDER_PATH
        attempts = 1 if order_post else self.max_retries
        client_id = str((body or {}).get("client_id") or "")
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._wait_pace()
            limiter.acquire()
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode()
                    status = response.status
                    self._note_rate(response.headers)
                parsed = json.loads(raw) if raw else {}
                if status >= 400 or (isinstance(parsed, dict) and parsed.get("success") is False):
                    error = WallexError(
                        f"{method} {path} failed: {parsed.get('message', status) if isinstance(parsed, dict) else status}",
                        status=status,
                        payload=parsed if isinstance(parsed, dict) else {},
                    )
                    adopted = self._maybe_adopt(order_post, client_id, error)
                    if adopted is not None:
                        return adopted
                    raise error
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except WallexError:
                raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                try:
                    parsed = json.loads(detail) if detail else {}
                except json.JSONDecodeError:
                    parsed = {"message": detail[:400]}
                self._note_rate(exc.headers)
                last_error = WallexError(
                    f"{method} {path} HTTP {exc.code}: {parsed.get('message', detail[:180]) if isinstance(parsed, dict) else detail[:180]}",
                    status=exc.code,
                    payload=parsed if isinstance(parsed, dict) else {},
                )
                adopted = self._maybe_adopt(order_post, client_id, last_error)
                if adopted is not None:
                    return adopted
                retryable = exc.code in {429, 500, 502, 503, 504}
                if order_post or not retryable or attempt + 1 == attempts:
                    raise last_error from exc
                delay = _retry_after_seconds(exc.headers)
                if delay is None:
                    delay = min(8.0, 0.4 * (2**attempt))
                log.warning("retrying %s %s after HTTP %s (attempt %s)", method, path, exc.code, attempt + 1)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = WallexError(f"{method} {path} network error: {exc}")
                adopted = self._maybe_adopt(order_post, client_id, last_error)
                if adopted is not None:
                    return adopted
                if order_post or attempt + 1 == attempts:
                    raise last_error from exc
                delay = min(8.0, 0.4 * (2**attempt))
                log.warning("retrying %s %s after %s", method, path, exc)
                time.sleep(delay)
        raise last_error or WallexError(f"{method} {path} failed")

    def _wait_pace(self) -> None:
        remaining = self._pace_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _note_rate(self, headers) -> None:
        """Back off when the remaining budget is under about 10% of the limit."""
        if headers is None:
            return
        remaining = _header_float(headers, "x-ratelimit-remaining")
        limit = _header_float(headers, "x-ratelimit-limit")
        if remaining is None or limit is None or limit <= 0:
            return
        if remaining / limit < 0.10:
            self._pace_until = max(self._pace_until, time.monotonic() + 1.0)

    def _maybe_adopt(self, order_post: bool, client_id: str, error: WallexError) -> dict | None:
        if not order_post or not client_id or not _adopt_status(error):
            return None
        log.warning("order POST for %s failed (%s); reading the order instead of sending it again", client_id, error.status or type(error).__name__)
        return self._adopt_order(client_id)

    def _adopt_order(self, client_id: str) -> dict | None:
        try:
            payload = self._request("GET", f"/v1/account/orders/{client_id}", auth=True)
        except WallexError as exc:
            log.warning("could not adopt order %s: %s", client_id, exc)
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return None
        status = str(result.get("status") or "").upper()
        if status in {"", "NOT_FOUND"} and not result.get("symbol"):
            return None
        if not result.get("clientOrderId") and not result.get("symbol"):
            return None
        return {"success": True, "adopted": True, "result": result}

    def get_markets(self) -> dict[str, MarketRule]:
        payload = self._request("GET", "/v1/markets")
        symbols = (payload.get("result") or {}).get("symbols") or {}
        rules: dict[str, MarketRule] = {}
        for symbol, row in symbols.items():
            stats = row.get("stats") or {}
            rules[symbol] = MarketRule(
                symbol=row.get("symbol") or symbol,
                base_asset=row.get("baseAsset") or "",
                quote_asset=row.get("quoteAsset") or "",
                price_decimals=int(row.get("tickSize") or 0),
                qty_decimals=int(row.get("stepSize") or 0),
                min_qty=float(row.get("minQty") or 0),
                min_notional=float(row.get("minNotional") or 0),
                is_zero_fee=bool(row.get("isZeroFee")),
                market_orders_enabled=bool(row.get("isMarketTypeEnable")),
                quote_volume_24h=_num(stats.get("24h_quoteVolume")) or 0.0,
                change_24h=_num(stats.get("24h_ch")) or 0.0,
                last_price=_num(stats.get("lastPrice")) or 0.0,
                bid_price=_num(stats.get("bidPrice")),
                ask_price=_num(stats.get("askPrice")),
            )
        return rules

    def get_unavailable_markets(self, package_id: int = 183) -> set[str]:
        """Symbols the hackathon credit plan refuses. Fail closed if this breaks."""
        payload = self._request("GET", f"/v1/prop/packages/{int(package_id)}")
        result = payload.get("result") or {}
        raw = result.get("unavailableMarkets")
        if raw is None and isinstance(result.get("detail"), dict):
            raw = result["detail"].get("unavailableMarkets")
        if not isinstance(raw, list) or not raw:
            raise WallexError(
                f"package {package_id} did not return unavailableMarkets; refusing to trade"
            )
        banned = {str(item).upper() for item in raw}
        log.info("package %s bans %s markets", package_id, len(banned))
        return banned

    def get_depth(self, symbol: str) -> OrderBook:
        payload = self._request("GET", "/v1/depth", params={"symbol": symbol})
        return _book(symbol, payload.get("result") or {})

    def get_depth_all(self) -> dict[str, OrderBook]:
        """Every market's book. Prefer this over one /v1/depth call per symbol."""
        payload = self._request("GET", "/v2/depth/all")
        result = payload.get("result") or {}
        books: dict[str, OrderBook] = {}
        if not isinstance(result, dict):
            return books
        for symbol, row in result.items():
            if isinstance(row, dict):
                books[str(symbol).upper()] = _book(str(symbol).upper(), row)
        return books

    def get_trades(self, symbol: str) -> list[dict]:
        payload = self._request("GET", "/v1/trades", params={"symbol": symbol})
        return list((payload.get("result") or {}).get("latestTrades") or [])

    def get_udf(self, symbol: str, resolution: str, start: int, end: int) -> list[Bar]:
        payload = self._request(
            "GET",
            "/v1/udf/history",
            params={"symbol": symbol, "resolution": resolution, "from": int(start), "to": int(end)},
        )
        if payload.get("s") != "ok":
            log.info("udf %s resolution=%s status=%s", symbol, resolution, payload.get("s"))
            return []
        times = payload.get("t") or []
        bars: list[Bar] = []
        for i, ts in enumerate(times):
            stamp = exchange_epoch(ts)
            if stamp is None:
                continue
            bars.append(
                Bar(
                    time=int(stamp),
                    open=float(payload["o"][i]),
                    high=float(payload["h"][i]),
                    low=float(payload["l"][i]),
                    close=float(payload["c"][i]),
                    volume=float(payload["v"][i]),
                )
            )
        return bars

    def get_ohlcv(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """Return closed candles. 15m and 4h are aggregated here.

        The history endpoint only emits base bars: resolution 1 and 15 are
        1-minute, 60/240/720 are 1-hour, and D is daily. Higher timeframes
        are built with `aggregate_bars`.
        """
        timeframe = timeframe.lower()
        now = int(time.time())
        if timeframe == "1m":
            bars = self._paged_minutes(symbol, start, end)
            return drop_incomplete(bars, 60, now)
        if timeframe == "15m":
            bars = aggregate_bars(self._paged_minutes(symbol, start, end), 900)
            return drop_incomplete(bars, 900, now)
        if timeframe == "1h":
            native = self._paged_hours(symbol, start, end)
            spacing = median_spacing(native)
            if native and spacing is not None and spacing < 1800:
                log.info("%s hourly endpoint returned %ss bars; aggregating", symbol, int(spacing))
                native = aggregate_bars(native, 3600)
            if len(native) < 30:
                minutes = self._paged_minutes(symbol, start, end)
                if minutes:
                    native = aggregate_bars(minutes, 3600)
            return drop_incomplete(native, 3600, now)
        if timeframe == "4h":
            hourly = self.get_ohlcv(symbol, "1h", start, end)
            return drop_incomplete(aggregate_bars(hourly, 4 * 3600), 4 * 3600, now)
        raise WallexError(f"unsupported timeframe {timeframe}")

    def _paged_minutes(self, symbol: str, start: int, end: int) -> list[Bar]:
        """resolution=15 returns about 20 days of 1-minute bars per call."""
        bars: list[Bar] = []
        cursor = int(start)
        end = int(end)
        while cursor < end:
            chunk_end = min(end, cursor + 15 * 86400)
            part = self.get_udf(symbol, "15", cursor, chunk_end)
            if not part and chunk_end - cursor <= 86400:
                part = self.get_udf(symbol, "1", cursor, chunk_end)
            bars.extend(part)
            if chunk_end >= end:
                break
            cursor = chunk_end
        return _dedupe(bars)

    def _paged_hours(self, symbol: str, start: int, end: int) -> list[Bar]:
        """resolution=60 is capped at 720 hourly bars (30 days)."""
        bars: list[Bar] = []
        cursor = int(start)
        end = int(end)
        while cursor < end:
            chunk_end = min(end, cursor + 29 * 86400)
            bars.extend(self.get_udf(symbol, "60", cursor, chunk_end))
            if chunk_end >= end:
                break
            cursor = chunk_end
        return _dedupe(bars)

    def get_balances(self) -> dict[str, Balance]:
        if self.subaccount_id:
            try:
                return self._parse_balances_detail(
                    self._request("GET", "/v1/account/balances-detail", auth=True)
                )
            except WallexError as exc:
                log.warning("balances-detail failed (%s); falling back to /balances", exc)
        payload = self._request("GET", "/v1/account/balances", auth=True)
        result = payload.get("result") or {}
        raw = result.get("balances") if isinstance(result, dict) else None
        balances: dict[str, Balance] = {}
        if isinstance(raw, dict):
            for asset, row in raw.items():
                if not isinstance(row, dict):
                    continue
                total = _num(row.get("value")) or 0.0
                locked = _num(row.get("locked")) or 0.0
                balances[row.get("asset") or asset] = Balance(row.get("asset") or asset, total, locked)
        return balances

    def _parse_balances_detail(self, payload: dict) -> dict[str, Balance]:
        result = payload.get("result") or {}
        balances: dict[str, Balance] = {}
        rows = result.items() if isinstance(result, dict) else []
        for asset, row in rows:
            if not isinstance(row, dict):
                continue
            if "available" in row or "total" in row:
                total = _num(row.get("total")) or 0.0
                available = _num(row.get("available"))
                locked = _num(row.get("freeze"))
                if locked is None and available is not None:
                    locked = max(0.0, total - available)
                balances[row.get("symbol") or asset] = Balance(row.get("symbol") or asset, total, locked or 0.0)
        if not balances:
            raise WallexError("balances-detail response had no assets")
        return balances

    def get_fees(self) -> dict[str, tuple[float, float]]:
        payload = self._request("GET", "/v1/account/fee", auth=True)
        result = payload.get("result") or {}
        fees: dict[str, tuple[float, float]] = {}
        if isinstance(result, dict):
            for symbol, row in result.items():
                if not isinstance(row, dict):
                    continue
                maker = _num(row.get("makerFeeRate"))
                taker = _num(row.get("takerFeeRate"))
                if maker is None or taker is None:
                    continue
                fees[symbol] = (maker, taker)
        return fees

    def get_open_orders(self, symbol: str | None = None) -> list[OrderRecord]:
        payload = self._request("GET", "/v1/account/openOrders", params={"symbol": symbol}, auth=True)
        return _parse_order_list(payload.get("result"))

    def get_order(self, client_order_id: str) -> OrderRecord:
        payload = self._request("GET", f"/v1/account/orders/{client_order_id}", auth=True)
        return _parse_order((payload.get("result") or {}))

    def get_order_history(self, symbol: str | None = None, page: int = 1) -> list[OrderRecord]:
        orders: list[OrderRecord] = []
        current = max(1, int(page))
        for _ in range(50):
            payload = self._request(
                "GET",
                "/v1/account/orders",
                params={"market": symbol, "page": current, "per_page": 50},
                auth=True,
            )
            orders.extend(_parse_order_list(payload.get("result")))
            info = payload.get("result_info") if isinstance(payload, dict) else None
            if not isinstance(info, dict) or not info.get("has_more_pages"):
                break
            current += 1
        return orders

    def get_subaccounts(self) -> list[dict]:
        payload = self._request("GET", "/sub-accounts", auth=True)
        return _dict_rows(payload, ("subAccounts", "sub_accounts", "data"))

    def get_subaccount_transfers(self) -> list[dict]:
        payload = self._request("GET", "/sub-accounts/transfers", auth=True)
        return _dict_rows(payload, ("transfers", "data"))

    def account_status(self) -> dict | None:
        """The sub-account this key can see, plus the free TMN balance."""
        rows = self.get_subaccounts()
        chosen = match_subaccount(rows, self.subaccount_id)
        if chosen is None and len(rows) == 1 and not self.subaccount_id:
            chosen = rows[0]
        balances = self.get_balances()
        tmn = balances.get("TMN")
        if chosen is None and tmn is None:
            return None
        row = chosen or {}
        return {
            "title": row.get("title") or row.get("name") or "",
            "clientId": str(row.get("clientId") or row.get("client_id") or ""),
            "isActive": row.get("isActive") if chosen else None,
            "isSuspend": row.get("isSuspend") if chosen else None,
            "tmn_available": None if tmn is None else tmn.available,
            "rows": len(rows),
            "matched": chosen is not None,
        }

    def prepare_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        client_id: str,
        rule: MarketRule,
        price_direction: str,
    ) -> PreparedOrder:
        body = build_order_body(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            client_id=client_id,
            price_decimals=rule.price_decimals,
            qty_decimals=rule.qty_decimals,
            min_qty=rule.min_qty,
            min_notional=rule.min_notional,
            price_direction=price_direction,
        )
        headers = self._headers(auth=True, body=True) if self.api_key else {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-API-Key": "",
        }
        if self.subaccount_id:
            headers["sub-account-client-id"] = self.subaccount_id
        prepared = PreparedOrder("POST", f"{self.base_url}{SPOT_ORDER_PATH}", headers, body)
        assert_spot_order(prepared.method, SPOT_ORDER_PATH)
        return prepared

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None,
        client_id: str,
        rule: MarketRule,
        price_direction: str,
        maker: bool = False,
        _balance_retry: bool = False,
    ) -> dict:
        assert_spot_order("POST", SPOT_ORDER_PATH)
        prepared = self.prepare_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            client_id=client_id,
            rule=rule,
            price_direction=price_direction,
        )
        if prepared.method != "POST" or not prepared.url.split("?", 1)[0].rstrip("/").endswith(SPOT_ORDER_PATH):
            raise WallexError(f"spot-only: prepared order left {SPOT_ORDER_PATH}")
        if self.dry_run or not self.api_key:
            log.info("order not sent: %s", prepared.redacted())
            return {
                "success": True,
                "dry_run": True,
                "result": {"clientOrderId": client_id, "status": "DRY_RUN", "active": False},
                "request": prepared.redacted(),
            }
        book = self.get_depth(symbol)
        if maker:
            _refuse_crossing_maker(side, price, book)
        try:
            response = self._request("POST", SPOT_ORDER_PATH, body=prepared.body, auth=True, is_order=True)
        except WallexError as exc:
            if exc.code == 1006 and str(side).upper() == "SELL" and not _balance_retry:
                available = self._available_base(rule)
                retry_qty = float(round_qty(available, rule.qty_decimals))
                if 0 < retry_qty < float(quantity) - 1e-12:
                    log.warning("sell %s rejected for balance; retrying once with %s", symbol, retry_qty)
                    return self.place_order(
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        quantity=retry_qty,
                        price=price,
                        client_id=client_id,
                        rule=rule,
                        price_direction=price_direction,
                        maker=maker,
                        _balance_retry=True,
                    )
            raise
        response["request"] = prepared.redacted()
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        response["liquidity"] = _liquidity(result, side, book)
        return response

    def _available_base(self, rule: MarketRule) -> float:
        balances = self.get_balances()
        row = balances.get(rule.base_asset)
        if row is None:
            return 0.0
        return float(row.available)

    def cancel_order(self, client_order_id: str) -> dict:
        return self._request("DELETE", f"/v1/account/orders/{client_order_id}", auth=True, is_order=True)


def _error_code(payload: dict) -> int | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("code")
    if raw is None and isinstance(payload.get("result"), dict):
        raw = payload["result"].get("error_code")
    return _coerce_code(raw)


def _coerce_code(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _adopt_status(error: WallexError) -> bool:
    """Timeouts, 5xx, 429, and duplicate-client 422 are read back. Other codes are not."""
    if error.code in FATAL_WALLEX_CODES or error.code == 1006:
        return False
    status = error.status
    if status is None:
        return True
    if status == 422 or status == 429 or status >= 500:
        return True
    return False


def _header_float(headers, name: str) -> float | None:
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        raw = headers.get(name.title())
    if raw is None:
        return None
    try:
        return float(str(raw).split(",", 1)[0].strip())
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(headers) -> float | None:
    raw = None if headers is None else headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 1.0


def _refuse_crossing_maker(side: str, price: float | None, book: OrderBook) -> None:
    """There is no post-only flag. A maker quote that crosses the touch is not sent."""
    if price is None or book is None:
        raise OrderRejected("maker order has no book to check")
    side = (side or "").upper()
    if side == "BUY":
        if book.best_ask is None or float(price) >= float(book.best_ask):
            raise OrderRejected(f"maker BUY price {price} is at or above the best ask")
    elif side == "SELL":
        if book.best_bid is None or float(price) <= float(book.best_bid):
            raise OrderRejected(f"maker SELL price {price} is at or below the best bid")


def _liquidity(result: dict, side: str, book: OrderBook | None) -> str | None:
    fills = result.get("fills") if isinstance(result, dict) else None
    flags: list[bool] = []
    if isinstance(fills, list):
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            if "maker" in fill:
                flags.append(bool(fill.get("maker")))
            elif "isMaker" in fill:
                flags.append(bool(fill.get("isMaker")))
            else:
                role = str(fill.get("liquidity") or "").lower()
                if role in {"maker", "taker"}:
                    flags.append(role == "maker")
    if flags:
        return "maker" if all(flags) else "taker"
    if book is None:
        return None
    price = _num(result.get("executedPrice") if isinstance(result, dict) else None)
    if price is None:
        price = _num(result.get("price") if isinstance(result, dict) else None)
    if price is None:
        return None
    side = (side or "").upper()
    if side == "BUY" and book.best_ask is not None:
        return "maker" if price < book.best_ask else "taker"
    if side == "SELL" and book.best_bid is not None:
        return "maker" if price > book.best_bid else "taker"
    return None


def match_subaccount(rows: list[dict], subaccount_id: str) -> dict | None:
    wanted = str(subaccount_id or "")
    if not wanted:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("clientId") or row.get("client_id") or "")
        if cid == wanted:
            return row
    return None


def subaccount_blocked(status: dict | None, subaccount_id: str) -> str | None:
    """Why a live arm must stop. None when the account can trade."""
    if not status:
        if subaccount_id:
            return "WALLEX_SUBACCOUNT_ID is set and the sub-account list did not match"
        return None
    if subaccount_id and not status.get("matched"):
        return "WALLEX_SUBACCOUNT_ID does not match a sub-account"
    cid = str(status.get("clientId") or "")
    if subaccount_id and cid and cid != str(subaccount_id):
        return "WALLEX_SUBACCOUNT_ID does not match the sub-account clientId"
    if status.get("isSuspend") in {True, 1, "1", "true", "True"}:
        return "sub-account is suspended"
    if "isActive" in status and status.get("isActive") in {False, 0, "0", "false", "False"}:
        return "sub-account is inactive"
    return None


def _dict_rows(payload: dict, keys: tuple[str, ...]) -> list[dict]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in keys:
            rows = result.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        if result.get("clientId") or result.get("client_id") or result.get("title"):
            return [result]
    return []


def fill_totals(order: OrderRecord) -> dict:
    """Cumulative qty, quote, and fees. BUY fees charged in the base asset stay in base_fee."""
    fills = order.raw.get("fills") if isinstance(order.raw, dict) else None
    qty = 0.0
    quote = 0.0
    base_fee = 0.0
    quote_fee = 0.0
    flags: list[bool] = []
    found = False
    if isinstance(fills, list):
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            found = True
            row_qty = _num(fill.get("quantity") if fill.get("quantity") is not None else fill.get("qty")) or 0.0
            row_price = _num(fill.get("price")) or 0.0
            row_sum = _num(fill.get("sum"))
            if row_sum is None:
                row_sum = _num(fill.get("executedSum"))
            fee = _num(fill.get("fee")) or 0.0
            asset = str(fill.get("feeAsset") or fill.get("fee_asset") or "").upper()
            qty += row_qty
            quote += row_sum if row_sum is not None else row_qty * row_price
            if asset in {"TMN", "IRT"} or asset.endswith("TMN"):
                quote_fee += fee
            elif asset:
                base_fee += fee
            if "maker" in fill:
                flags.append(bool(fill.get("maker")))
            elif "isMaker" in fill:
                flags.append(bool(fill.get("isMaker")))
    if not found:
        qty = float(order.executed_qty or 0)
        quote = float(order.executed_sum or 0)
        if quote <= 0 and order.executed_price and qty:
            quote = float(order.executed_price) * qty
    liquidity = order.liquidity
    if flags:
        liquidity = "maker" if all(flags) else "taker"
    return {
        "qty": qty,
        "quote": quote,
        "base_fee": base_fee,
        "quote_fee": quote_fee,
        "liquidity": liquidity,
        "from_fills": found,
    }


def _num(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _level(row: dict) -> BookLevel:
    return BookLevel(price=float(row.get("price") or 0), quantity=float(row.get("quantity") or 0))


def _book(symbol: str, result: dict) -> OrderBook:
    bids = sorted((_level(row) for row in result.get("bid") or []), key=lambda level: level.price, reverse=True)
    asks = sorted((_level(row) for row in result.get("ask") or []), key=lambda level: level.price)
    return OrderBook(symbol=symbol, bids=bids, asks=asks)


def _dedupe(bars: list[Bar]) -> list[Bar]:
    by_time: dict[int, Bar] = {}
    for bar in bars:
        by_time[bar.time] = bar
    return [by_time[k] for k in sorted(by_time)]


def _parse_order(row: dict) -> OrderRecord:
    return OrderRecord(
        client_order_id=str(row.get("clientOrderId") or row.get("client_id") or ""),
        symbol=str(row.get("symbol") or ""),
        side=str(row.get("side") or "").upper(),
        order_type=str(row.get("type") or "").upper(),
        price=_num(row.get("price")),
        orig_qty=_num(row.get("origQty")) or 0.0,
        executed_qty=_num(row.get("executedQty")) or 0.0,
        executed_sum=_num(row.get("executedSum")) or 0.0,
        executed_price=_num(row.get("executedPrice")),
        status=str(row.get("status") or ""),
        active=bool(row.get("active")),
        fee=_num(row.get("fee")) or 0.0,
        created_at=exchange_epoch(row.get("created_at") if row.get("created_at") is not None else row.get("createdAt")),
        liquidity=_row_liquidity(row),
        raw=row,
    )


def _row_liquidity(row: dict) -> str | None:
    role = str(row.get("liquidity") or "").lower()
    if role in {"maker", "taker"}:
        return role
    if "maker" in row and not isinstance(row.get("maker"), dict):
        return "maker" if row.get("maker") else "taker"
    return None


def _parse_order_list(result) -> list[OrderRecord]:
    orders: list[OrderRecord] = []
    if not isinstance(result, dict):
        return orders
    if isinstance(result.get("orders"), list):
        return [_parse_order(row) for row in result["orders"] if isinstance(row, dict)]
    for value in result.values():
        if isinstance(value, list):
            orders.extend(_parse_order(row) for row in value if isinstance(row, dict))
        elif isinstance(value, dict) and ("symbol" in value or "clientOrderId" in value):
            orders.append(_parse_order(value))
    return orders
