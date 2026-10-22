"""Read-only desk for the Liara health port.

One HTML page. It tails the bot's append-only journals and never calls
Wallex, the LLM, or anything that can place, cancel, or reconfigure.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from trader.engine.ops import redact

_TAIL_BYTES = 1_500_000
_LLM_CAP_USD = 2.50
_JOURNALS = ("cycles.jsonl", "decisions.jsonl", "adjustments.jsonl", "reviews.jsonl")
_DROP_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "secret",
    "prompt",
    "response",
    "wallex_api_key",
    "llm_api_key",
    "dashboard_password",
    "dashboard_user",
    "bearer",
    "x_api_key",
}
_SECRET_ENV = (
    "WALLEX_API_KEY",
    "WALLEX_SUBACCOUNT_ID",
    "LLM_API_KEY",
    "LIARA_API_TOKEN",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_USER",
)

_basic = HTTPBasic(auto_error=False)


def create_app(data_dir: Path | None = None) -> FastAPI:
    root = Path(data_dir) if data_dir is not None else Path(os.getenv("TRADER_DATA_DIR", "data"))
    app = FastAPI(title="Wallex desk", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return Response(status_code=405, content="method not allowed", media_type="text/plain")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def desk(_user: str = Depends(require_dashboard)) -> HTMLResponse:
        return HTMLResponse(render_page(load_desk(root)))

    @app.get("/status")
    def status(_user: str = Depends(require_dashboard)) -> dict:
        from trader.health import snapshot

        payload = scrub(snapshot())
        payload["ok"] = True
        return payload

    return app


def require_dashboard(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """Fail closed. Missing env, a missing header, or a mismatch are all 401."""
    user = os.getenv("DASHBOARD_USER", "")
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if not user or not password or credentials is None:
        _unauthorized()
    user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), user.encode("utf-8"))
    pass_ok = secrets.compare_digest(credentials.password.encode("utf-8"), password.encode("utf-8"))
    if not (user_ok and pass_ok):
        _unauthorized()
    return credentials.username


def _unauthorized() -> None:
    raise HTTPException(
        status_code=401,
        detail="unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="desk"'},
    )


def load_desk(directory: Path) -> dict:
    directory = Path(directory)
    cycles = [
        row for row in _tail(directory / "cycles.jsonl", limit=240)
        if row.get("record", "cycle") == "cycle"
    ]
    decisions = _tail(directory / "decisions.jsonl", limit=500)
    adjustments = _tail(directory / "adjustments.jsonl", limit=40)
    reviews = _tail(directory / "reviews.jsonl", limit=20)
    symbols = [row for row in decisions if row.get("record") == "symbol"]
    legacy = [row for row in decisions if row.get("record") != "symbol"]
    latest = cycles[-1] if cycles else {}
    return {
        "latest": latest,
        "cycles": cycles[-240:],
        "symbols": _latest_symbols(symbols, latest.get("cycle_id")),
        "block_counts": _block_counts(symbols[-300:]),
        "positions": _positions(latest, legacy),
        "fills": _fills(symbols[-80:], legacy),
        "rationale": _rationale(legacy),
        "adjustments": adjustments[-12:],
        "reviews": reviews[-8:],
        "errors": _errors(cycles[-40:], legacy[-40:]),
        "latency": [row.get("loop_latency_ms") for row in cycles[-12:] if row.get("loop_latency_ms") is not None],
    }


def render_page(desk: dict) -> str:
    latest = desk["latest"]
    pnl = latest.get("pnl_from_golive_pct")
    spent = _num(latest.get("llm_usd_total"))
    go_live = latest.get("go_live_sha") or ""
    running = latest.get("git_sha") or ""
    mismatch = bool(go_live and running and go_live != running)
    parts = [
        _document_start(),
        "<header><p class='kicker'>Wallex · read only</p>",
        f"<h1>Desk</h1><p class='sub'>{_esc(latest.get('ts_tehran') or 'waiting for the first cycle')}</p></header>",
        "<main>",
        _state_panel(latest, mismatch, go_live, running),
        _book_panel(latest, desk, pnl),
        _curve_panel(desk["cycles"]),
        _feed_panel(desk),
        _gates_panel(desk["block_counts"]),
        _fee_panel(latest, desk["symbols"]),
        _wallex_panel(latest, desk),
        _llm_panel(latest, spent),
        _table_panel("Adjustments", _adjustment_rows(desk["adjustments"]), "No adjustments yet."),
        _table_panel("Checkpoint reviews", _review_rows(desk["reviews"]), "No checkpoint reviews yet."),
        _errors_panel(desk["errors"]),
        "</main>",
        "<footer><span id='refresh'>Reload to update.</span> "
        "This page cannot place or cancel orders, change config, or start or stop the loop.</footer>",
        _refresh_script(),
        "</body></html>",
    ]
    return "".join(parts)


def scrub(value):
    return _scrub(value, _secret_values())


def _scrub(value, secret_values: list[str]):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalised = str(key).lower().replace("-", "_")
            if normalised in _DROP_KEYS:
                cleaned[str(key)] = "present" if _present(item) else "absent"
                continue
            cleaned[str(key)] = _scrub(item, secret_values)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item, secret_values) for item in value]
    if isinstance(value, str):
        return redact(value, secret_values)
    return value


def _present(item) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    return item is not None and item is not False


def _secret_values() -> list[str]:
    found = []
    for name in _SECRET_ENV:
        value = os.getenv(name, "")
        if len(value) >= 6:
            found.append(value)
    return found


def _tail(path: Path, limit: int = 80) -> list[dict]:
    if not path.exists():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > _TAIL_BYTES:
            handle.seek(size - _TAIL_BYTES)
            handle.readline()
        raw = handle.read().decode("utf-8", errors="replace")
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(scrub(parsed))
    return rows[-limit:] if len(rows) > limit else rows


def _latest_symbols(symbols: list[dict], cycle_id: str | None) -> list[dict]:
    if cycle_id:
        matched = [row for row in symbols if row.get("cycle_id") == cycle_id]
        if matched:
            return matched
    return symbols[-12:]


def _block_counts(symbols: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in symbols:
        gate = row.get("first_block")
        if gate:
            counts[str(gate)] = counts.get(str(gate), 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]


def _positions(latest: dict, legacy: list[dict]) -> list:
    for row in reversed(legacy):
        positions = row.get("positions")
        if isinstance(positions, list) and positions:
            return positions
    named = latest.get("open_positions") or []
    return [{"symbol": symbol} for symbol in named]


def _fills(symbols: list[dict], legacy: list[dict]) -> list[dict]:
    found = []
    for row in symbols:
        order = row.get("order")
        if isinstance(order, dict) and order.get("status") == "filled":
            found.append({"symbol": row.get("symbol"), **order})
    if found:
        return found[-12:]
    for row in reversed(legacy):
        fills = row.get("fills")
        if isinstance(fills, list) and fills:
            return [item for item in fills if isinstance(item, dict)][-12:]
    return []


def _rationale(legacy: list[dict]) -> dict:
    found: dict[str, str] = {}
    comment = ""
    for row in legacy:
        agent = row.get("agent")
        if not isinstance(agent, dict):
            continue
        comment = str(agent.get("comment") or comment)
        for item in agent.get("decisions") or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            text = str(item.get("rationale") or comment or "").strip()
            if text:
                found[str(item["symbol"])] = text[:240]
    found[""] = comment[:240]
    return found


def _errors(cycles: list[dict], legacy: list[dict]) -> list[dict]:
    rows = []
    for cycle in cycles:
        for item in cycle.get("errors") or []:
            if isinstance(item, dict):
                rows.append({
                    "ts": cycle.get("ts_tehran") or cycle.get("ts_utc") or "",
                    "where": item.get("endpoint") or "wallex",
                    "status": item.get("http_status"),
                    "message": item.get("message") or "",
                })
        if cycle.get("error"):
            rows.append({
                "ts": cycle.get("ts_tehran") or "",
                "where": "cycle",
                "status": None,
                "message": cycle.get("error"),
            })
    for row in legacy:
        agent = row.get("agent") if isinstance(row.get("agent"), dict) else {}
        message = row.get("error") or agent.get("error")
        if message:
            rows.append({
                "ts": row.get("ts_tehran") or row.get("timestamp") or "",
                "where": "decision",
                "status": None,
                "message": message,
            })
    return rows[-20:]


def _document_start() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallex desk</title>
<style>
:root {
  --bg: #010814;
  --panel: #031027;
  --rule: #083177;
  --accent: #508cf3;
  --text: #fafafb;
  --muted: #9b9fa6;
  --highlight: #ff8000;
  --profit: #81d004;
  --loss: #ea5555;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body {
  font-family: "Iowan Old Style", Palatino, Georgia, serif;
  line-height: 1.45;
  padding: 28px 32px 64px;
}
h1, h2 { font-weight: 500; letter-spacing: -0.02em; margin: 0; }
h1 { font-size: 40px; }
h2 { font-size: 18px; color: var(--text); }
.kicker, .sub, .label, th, .meta { color: var(--muted); }
.kicker { letter-spacing: 0.14em; text-transform: uppercase; font-size: 12px; margin: 0 0 8px; }
.sub { margin: 6px 0 0; }
.num, .mono, .pill { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
header { border-bottom: 1px solid var(--rule); padding-bottom: 18px; margin-bottom: 22px; }
main { display: grid; gap: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.panel { background: var(--panel); border: 1px solid var(--rule); padding: 16px 18px; overflow-x: auto; }
.stat { font-size: 28px; margin-top: 6px; }
.label { font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; }
.profit { color: var(--profit); }
.loss { color: var(--loss); }
.highlight, .mismatch { color: var(--highlight); }
.accent { color: var(--accent); }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 6px 8px 6px 0; border-bottom: 1px solid var(--rule); vertical-align: top; font-size: 13px; }
th { font-family: "Iowan Old Style", Palatino, Georgia, serif; font-weight: 500; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; }
.pill { display: inline-block; border: 1px solid var(--rule); color: var(--muted); padding: 1px 6px; margin: 0 4px 4px 0; }
.pill.fail { color: var(--highlight); border-color: var(--highlight); }
svg { width: 100%; height: 168px; display: block; }
.bar { height: 8px; background: #083177; margin-top: 10px; }
.bar > span { display: block; height: 100%; background: var(--accent); }
footer { margin-top: 22px; color: var(--muted); font-size: 13px; }
@media (max-width: 720px) {
  body { padding: 16px 12px 48px; }
  h1 { font-size: 32px; }
  .stat { font-size: 22px; }
}
.empty { color: var(--muted); margin: 8px 0 0; }
a { color: var(--accent); }
.panel, .pill, .bar > span, td, .stat {
  transition: color 160ms ease, background-color 160ms ease, border-color 160ms ease;
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
</style>
</head>
<body>
"""


def _refresh_script() -> str:
    return """<script>
(function () {
  var note = document.getElementById("refresh");
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    if (note) note.textContent = "Motion reduced. Reload to update.";
    return;
  }
  if (note) note.textContent = "Refreshes in 15s.";
  setTimeout(function () { location.reload(); }, 15000);
})();
</script>
"""


def _state_panel(latest: dict, mismatch: bool, go_live: str, running: str) -> str:
    sha_class = "mismatch" if mismatch else "num"
    age = _age(latest.get("ts_utc"))
    cards = [
        ("State", _esc(latest.get("state") or "—"), "stat"),
        ("Mode", _esc(latest.get("mode") or "—"), "stat"),
        ("Last cycle", _esc(age), "stat num"),
        ("Loop latency", _ms(latest.get("loop_latency_ms")), "stat num"),
        ("Go-live SHA", _sha(go_live), "num"),
        ("Running SHA", _sha(running), sha_class),
    ]
    body = "".join(
        f"<div class='panel'><div class='label'>{_esc(label)}</div><div class='{klass}'>{value}</div></div>"
        for label, value, klass in cards
    )
    keys = (
        ("Wallex key", latest.get("wallex_key_present")),
        ("Sub-account", latest.get("subaccount_present")),
        ("LLM key", latest.get("llm_key_present")),
    )
    presence = " · ".join(
        f"{_esc(label)} {'present' if flag else 'absent'}" for label, flag in keys
    )
    meta = (
        f"Entries {'open' if latest.get('entries_allowed') else 'closed'}"
        f" · size ×{_esc(latest.get('size_multiplier') if latest.get('size_multiplier') is not None else '—')}"
        f" · ev override {'on' if latest.get('ev_override') else 'off'}"
        f" · kill {'on' if latest.get('kill_switch') else 'off'}"
    )
    if latest.get("cutoff_reason"):
        meta += f" · cutoff {_esc(latest.get('cutoff_reason'))}"
    return f"<section class='grid'>{body}</section><p class='meta'>{presence}. {meta}.</p>"


def _book_panel(latest: dict, desk: dict, pnl) -> str:
    dust = latest.get("dust") or []
    dust_text = ", ".join(
        f"{_esc(item.get('symbol'))} {_num_text(item.get('notional_tmn'))}"
        for item in dust if isinstance(item, dict)
    ) or "none"
    orders = latest.get("open_orders") or []
    order_text = ", ".join(_esc(item) for item in orders) or "none"
    position_rows = "".join(_position_row(item) for item in desk["positions"]) or "<tr><td colspan='3'>none</td></tr>"
    fill_rows = "".join(_fill_row(item) for item in desk["fills"]) or "<tr><td colspan='4'>none</td></tr>"
    return (
        "<section class='grid'>"
        f"<div class='panel'><div class='label'>Equity TMN</div><div class='stat num'>{_num_text(latest.get('equity_tmn'), 0)}</div></div>"
        f"<div class='panel'><div class='label'>Free TMN</div><div class='stat num'>{_num_text(latest.get('free_tmn'), 0)}</div></div>"
        f"<div class='panel'><div class='label'>PnL from go-live</div><div class='stat num {_pnl_class(pnl)}'>{_pct(pnl)}</div>"
        f"<div class='meta'>{_num_text(latest.get('pnl_from_golive_tmn'))} TMN</div></div>"
        f"<div class='panel'><div class='label'>Dust</div><div class='mono'>{dust_text}</div>"
        f"<div class='label' style='margin-top:12px'>Open orders</div><div class='mono'>{order_text}</div></div>"
        "</section>"
        "<section class='panel'><h2>Positions</h2><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th></tr></thead>"
        f"<tbody>{position_rows}</tbody></table>"
        "<h2 style='margin-top:16px'>Fills</h2><table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Notional</th></tr></thead>"
        f"<tbody>{fill_rows}</tbody></table></section>"
    )


def _curve_panel(cycles: list[dict]) -> str:
    points = []
    for row in cycles:
        value = _num(row.get("equity_tmn"))
        if value is None:
            continue
        points.append(value)
    return f"<section class='panel'><h2>Equity</h2>{_svg(points)}</section>"


def _feed_panel(desk: dict) -> str:
    rationale = desk["rationale"]
    rows = []
    for row in desk["symbols"]:
        symbol = str(row.get("symbol") or "")
        llm = row.get("llm") if isinstance(row.get("llm"), dict) else {}
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        text = rationale.get(symbol) or rationale.get("") or ""
        failed = [
            str(item.get("gate"))
            for item in (row.get("gate_trace") or [])
            if isinstance(item, dict) and not item.get("pass")
        ]
        pills = "".join(f"<span class='pill fail'>{_esc(gate)}</span>" for gate in failed[:6])
        if not pills and row.get("first_block"):
            pills = f"<span class='pill fail'>{_esc(row.get('first_block'))}</span>"
        if not failed:
            pills = "<span class='pill'>clear</span>"
        veto = llm.get("veto")
        rows.append(
            "<tr>"
            f"<td>{_esc(symbol)}</td>"
            f"<td>{pills}</td>"
            f"<td class='mono'>{_esc(llm.get('tier') or '—')} · {_esc(llm.get('model') or '—')}</td>"
            f"<td class='mono'>{_num_text(llm.get('confidence'), 2)}</td>"
            f"<td class='mono'>{'veto' if veto else '—'}</td>"
            f"<td>{_esc(text or '—')}</td>"
            f"<td class='mono'>{_esc(risk.get('verdict') or '—')}</td>"
            "</tr>"
        )
    body = "".join(rows) or "<tr><td colspan='7'>No symbol decisions yet.</td></tr>"
    return (
        "<section class='panel'><h2>Decisions</h2>"
        "<table><thead><tr><th>Symbol</th><th>Gates</th><th>LLM</th><th>Conf</th>"
        f"<th>Veto</th><th>Rationale</th><th>Risk</th></tr></thead><tbody>{body}</tbody></table></section>"
    )


def _gates_panel(counts: list[tuple[str, int]]) -> str:
    if not counts:
        inner = "<p class='empty'>No blocking gates in the recent tail.</p>"
    else:
        top = counts[0][1] or 1
        inner = "".join(
            f"<div><span class='mono highlight'>{count}</span> {_esc(name)}"
            f"<div class='bar'><span style='width:{(count / top) * 100:.1f}%'></span></div></div>"
            for name, count in counts
        )
    return f"<section class='panel'><h2>Top blocking gates</h2>{inner}</section>"


def _fee_panel(latest: dict, symbols: list[dict]) -> str:
    prior = None
    hurdle = latest.get("fee_hurdle")
    passed = None
    for row in reversed(symbols):
        gate = row.get("cost_gate")
        if isinstance(gate, dict):
            prior = gate.get("prior")
            hurdle = gate.get("hurdle", hurdle)
            passed = gate.get("pass")
            break
    relation = "—"
    if prior is not None and hurdle is not None:
        relation = "prior above hurdle" if _num(prior) is not None and _num(hurdle) is not None and float(prior) > float(hurdle) else "prior at or under hurdle"
    flag = "profit" if passed else "highlight" if passed is False else ""
    return (
        "<section class='panel'><h2>Fees and cost gate</h2>"
        f"<p class='num'>maker {_bps(latest.get('fee_maker_bps'))} · taker {_bps(latest.get('fee_taker_bps'))}</p>"
        f"<p>Prior {_pct(prior)} · hurdle {_pct(hurdle)} · <span class='{flag}'>{_esc(relation)}</span></p>"
        f"<p class='meta'>Fetched {_esc(latest.get('fee_fetched_at') or '—')}</p></section>"
    )


def _wallex_panel(latest: dict, desk: dict) -> str:
    samples = [float(item) for item in desk["latency"]]
    median = "—"
    if samples:
        ordered = sorted(samples)
        median = f"{ordered[len(ordered) // 2]:,.0f} ms"
    errors = desk["errors"]
    wallex = [row for row in errors if row.get("where") not in {"decision", "cycle"}]
    lines = "".join(
        f"<tr><td class='mono'>{_esc(row.get('status'))}</td><td>{_esc(row.get('where'))}</td><td>{_esc(row.get('message'))}</td></tr>"
        for row in wallex[-8:]
    ) or "<tr><td colspan='3'>No Wallex errors in the tail.</td></tr>"
    return (
        "<section class='panel'><h2>Wallex</h2>"
        f"<p class='num'>loop {_ms(latest.get('loop_latency_ms'))} · median {median}</p>"
        f"<p class='meta'>Loop time wraps the Wallex reads and the order path. Rejects this cycle: {_esc(latest.get('n_orders_rejected') or 0)}.</p>"
        f"<table><thead><tr><th>HTTP</th><th>Endpoint</th><th>Message</th></tr></thead><tbody>{lines}</tbody></table></section>"
    )


def _llm_panel(latest: dict, spent: float | None) -> str:
    amount = spent or 0.0
    width = min(100.0, (amount / _LLM_CAP_USD) * 100.0)
    klass = "loss" if amount > _LLM_CAP_USD else "accent"
    return (
        "<section class='panel'><h2>LLM spend</h2>"
        f"<p class='stat num {klass}'>${amount:,.4f} <span class='meta'>of ${_LLM_CAP_USD:.2f}</span></p>"
        f"<div class='bar'><span style='width:{width:.1f}%'></span></div>"
        f"<p class='meta'>Today ${(_num(latest.get('llm_usd_today')) or 0):,.4f}"
        f" · tier {_esc(latest.get('llm_tier') or '—')} · {_esc(latest.get('llm_model') or '—')}"
        f" · {_esc(latest.get('llm_source') or '—')}</p></section>"
    )


def _adjustment_rows(rows: list[dict]) -> str:
    body = []
    for row in reversed(rows):
        body.append(
            "<tr>"
            f"<td class='mono'>{_esc(row.get('ts_tehran') or '')}</td>"
            f"<td class='mono'>{_esc(row.get('action') or '')}</td>"
            f"<td>{_esc(row.get('state_before') or '')} → {_esc(row.get('state_after') or '')}</td>"
            f"<td>{_esc(row.get('actor') or '')}</td>"
            f"<td class='mono'>{'yes' if row.get('applied') else 'no'}</td>"
            f"<td>{_esc(row.get('rejected_action') or row.get('note') or '')}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Time</th><th>Action</th><th>State</th><th>Actor</th><th>Applied</th><th>Note</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _review_rows(rows: list[dict]) -> str:
    body = []
    for row in reversed(rows):
        body.append(
            "<tr>"
            f"<td class='mono'>{_esc(row.get('checkpoint') or '')}</td>"
            f"<td class='mono'>{_esc(row.get('suggested_action') or '')}</td>"
            f"<td class='mono'>{_esc(row.get('operator_action') or '—')}</td>"
            f"<td>{'draft' if row.get('draft') else 'filed'}</td>"
            f"<td class='num'>{_num_text(row.get('equity_end_tmn'), 0)}</td>"
            f"<td>{_esc(row.get('note') or '')}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Checkpoint</th><th>Suggested</th><th>Operator</th><th></th><th>Equity</th><th>Note</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _table_panel(title: str, table: str, empty: str) -> str:
    if "<td" not in table:
        table = f"<p class='empty'>{_esc(empty)}</p>"
    return f"<section class='panel'><h2>{_esc(title)}</h2>{table}</section>"


def _errors_panel(rows: list[dict]) -> str:
    if not rows:
        inner = "<p class='empty'>No recent errors.</p>"
    else:
        body = "".join(
            f"<tr><td class='mono'>{_esc(row.get('ts'))}</td><td>{_esc(row.get('where'))}</td><td>{_esc(row.get('message'))}</td></tr>"
            for row in reversed(rows)
        )
        inner = f"<table><thead><tr><th>Time</th><th>Where</th><th>Message</th></tr></thead><tbody>{body}</tbody></table>"
    return f"<section class='panel'><h2>Error tail</h2>{inner}</section>"


def _svg(points: list[float]) -> str:
    width, height, pad = 720, 168, 12
    if not points:
        return "<p class='empty'>No equity points yet.</p>"
    lo, hi = min(points), max(points)
    span = hi - lo or 1.0
    coords = []
    if len(points) == 1:
        y = height / 2
        coords = [f"{pad:.1f},{y:.1f}", f"{width - pad:.1f},{y:.1f}"]
    else:
        last_index = len(points) - 1
        for index, value in enumerate(points):
            x = pad + (width - 2 * pad) * (index / last_index)
            y = pad + (height - 2 * pad) * (1 - (value - lo) / span)
            coords.append(f"{x:.1f},{y:.1f}")
    colour = "#81d004" if points[-1] >= points[0] else "#ea5555"
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Equity curve'>"
        f"<polyline fill='none' stroke='{colour}' stroke-width='1.6' points='{' '.join(coords)}'/>"
        "</svg>"
        f"<p class='meta num'>{_num_text(points[0], 0)} → {_num_text(points[-1], 0)} TMN</p>"
    )


def _position_row(item) -> str:
    if isinstance(item, str):
        return f"<tr><td>{_esc(item)}</td><td></td><td></td></tr>"
    if not isinstance(item, dict):
        return ""
    qty = item.get("qty", item.get("quantity"))
    avg = item.get("avg_px", item.get("entry_price"))
    return f"<tr><td>{_esc(item.get('symbol'))}</td><td class='num'>{_num_text(qty, 6)}</td><td class='num'>{_num_text(avg)}</td></tr>"


def _fill_row(item: dict) -> str:
    notional = item.get("notional_tmn")
    return (
        f"<tr><td>{_esc(item.get('symbol'))}</td><td>{_esc(item.get('side'))}</td>"
        f"<td class='num'>{_num_text(item.get('qty') or item.get('filled_qty'), 6)}</td>"
        f"<td class='num'>{_num_text(notional)}</td></tr>"
    )


def _esc(value) -> str:
    from html import escape
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_text(value, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return "—"
    text = f"{abs(number):,.{digits}f}"
    return f"-{text}" if number < 0 else text


def _pct(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number * 100:+.3f}%"


def _bps(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:.2f} bps"


def _ms(value) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:,.0f} ms"


def _sha(value: str) -> str:
    text = str(value or "—")
    if len(text) > 12:
        text = text[:12]
    return _esc(text)


def _pnl_class(value) -> str:
    number = _num(value)
    if number is None or number == 0:
        return ""
    return "profit" if number > 0 else "loss"


def _age(ts_utc: str | None) -> str:
    if not ts_utc:
        return "—"
    try:
        stamp = datetime.strptime(str(ts_utc), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(ts_utc)
    seconds = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"
