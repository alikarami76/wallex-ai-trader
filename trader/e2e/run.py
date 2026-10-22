"""Scenario runner for `python -m trader e2e`.

Each case drives the real loop. A miss is reported. No strategy number,
risk gate, or audit rule is changed to force a pass.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from trader.e2e.clock import Clock, restore
from trader.e2e.harness import cycles_of, drive, install_clock, read_jsonl, symbols_of
from trader.e2e.mock import World, serve
from trader.order_guard import OrderIntent, assess
from trader.timeutil import TEHRAN
from trader.wallex.client import WallexClient, WallexError


class Miss(AssertionError):
    pass


def need(condition: bool, message: str) -> None:
    if not condition:
        raise Miss(message)


def at(hour: int, minute: int, day: int = 22) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=TEHRAN).timestamp()


def saved(directory: Path) -> dict:
    return json.loads((directory / "state.json").read_text(encoding="utf-8"))


def positions(directory: Path) -> dict:
    return (saved(directory).get("portfolio") or {}).get("positions") or {}


def remainder_tmn(directory: Path) -> float:
    """Notional left after a rounded full exit. A sub-step crumb cannot be sold."""
    total = 0.0
    for row in positions(directory).values():
        total += abs(float(row.get("qty") or 0) * float(row.get("entry_price") or 0))
    return total


def flat_book(directory: Path) -> bool:
    return remainder_tmn(directory) < 1.0


def machine(directory: Path) -> str:
    rows = cycles_of(directory)
    if rows:
        return str(rows[-1].get("state") or "")
    return str((saved(directory).get("ops") or {}).get("state") or "")


def trades(directory: Path) -> list[dict]:
    path = directory / "trades.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def context(directory: Path, world: World) -> str:
    rows = cycles_of(directory)
    last = rows[-1] if rows else {}
    tail = []
    for row in symbols_of(directory)[-6:]:
        order = row.get("order") or {}
        tail.append(f"{row.get('symbol')}:{row.get('first_block')}:{order.get('status')}")
    return (
        f"cycles={len(rows)} state={last.get('state')} quant={last.get('n_quant_pass')} "
        f"z={last.get('zscore_n')} submitted={last.get('n_orders_submitted')} "
        f"posts={world.order_posts} llm={world.llm_calls} tail={tail}"
    )


def enter(world: World, clock: Clock, root: Path, name: str, *, cycles: int = 20, start: float | None = None) -> Path:
    world.reset()
    world.llm_mode = "accept"
    clock.unix = at(12, 0) if start is None else start
    directory = root / name
    drive(world, directory, mode="live", cycles=cycles, send=True, authorize=True, override=True)
    need(bool(positions(directory)), f"{name} opened no position; {context(directory, world)}")
    buys = [row for row in trades(directory) if row.get("side") == "BUY"]
    need(len(buys) == 1, f"{name} buy count {len(buys)}")
    need(buys[0].get("order_type") == "LIMIT", f"{name} entry was {buys[0].get('order_type')}")
    need(world.had_sub and world.had_key and world.header_missing == 0, "sub-account header was not enforced")
    return directory


def run_all(root: Path | None = None) -> int:
    root = Path(root or os.environ.get("E2E_OUT") or "results/e2e")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    import logging
    logging.getLogger("trader").setLevel(logging.WARNING)
    clock = Clock(at(12, 0))
    world = World(clock)
    server = serve(world)
    saved_bindings = install_clock(clock, world)
    selected = {item.strip() for item in os.environ.get("E2E_ONLY", "").split(",") if item.strip()}
    results: list[tuple[str, bool, str]] = []
    journals: dict[str, Path] = {}

    def case(name: str, fn) -> None:
        if selected and name.split()[0] not in selected and name not in selected:
            return
        try:
            detail = fn()
            results.append((name, True, detail))
            print(f"PASS  {name}  {detail}", flush=True)
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            traceback.print_exc()
            print(f"FAIL  {name}  {exc}", flush=True)

    def quiet() -> str:
        world.reset()
        world.kinds = {symbol: "flat" for symbol in world.kinds}
        clock.unix = at(12, 0)
        directory = root / "a-quiet"
        drive(world, directory, mode="shadow", cycles=20, send=False)
        journals["quiet"] = directory
        rows = cycles_of(directory)
        need(rows, "no cycles")
        need(all(row.get("state") == "SHADOW" for row in rows), machine(directory))
        need(all(int(row.get("n_quant_pass") or 0) == 0 for row in rows), "a quiet book produced a signal")
        need(sum(int(row.get("n_orders_submitted") or 0) for row in rows) == 0, "shadow submitted an order")
        need(world.order_posts == 0, "shadow posted an order")
        need("BTCTMN" in (rows[-1].get("universe") or []), "universe dropped BTCTMN")
        return f"{len(rows)} shadow cycles, quant pass 0, posts 0"

    def dip() -> str:
        directory = enter(world, clock, root, "b-dip")
        journals["dip"] = directory
        clock.advance(24 * 3600 + 120)
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        reasons = " ".join(row.get("reason") or "" for row in trades(directory))
        need("time stop" in reasons, f"exit reasons: {reasons}")
        need(flat_book(directory), f"residual {remainder_tmn(directory):.4f} TMN")
        need(machine(directory) == "LIVE_MANAGE", machine(directory))
        sells = [row for row in trades(directory) if row.get("side") == "SELL"]
        need(sells and sells[-1].get("order_type") == "LIMIT", "24h exit was not a maker limit")
        crumb = remainder_tmn(directory)
        extra = f", sub-step remainder {crumb:.4f} TMN" if crumb else ""
        return "maker buy then 24h limit exit, LIVE_MANAGE after the cutoff" + extra

    def stop() -> str:
        directory = enter(world, clock, root, "c-stop")
        journals["stop"] = directory
        world.scale["BTCTMN"] = 0.94
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        reasons = " ".join(row.get("reason") or "" for row in trades(directory))
        need("stop " in reasons, f"exit reasons: {reasons}")
        need(flat_book(directory), f"stop residual {remainder_tmn(directory):.4f} TMN")
        need(machine(directory) != "KILLED", f"a 6% stop killed the book: {machine(directory)}")
        fired = [row for row in cycles_of(directory) if row.get("stop_fired")]
        need(fired, "stop_fired was not logged")
        return "stop filled, book flat, kill switch stayed off"

    def trail() -> str:
        directory = enter(world, clock, root, "d-trail")
        world.scale["BTCTMN"] = 1.04
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        need(positions(directory), "trail arm cycle sold the position")
        world.scale["BTCTMN"] = 1.02
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        reasons = " ".join(row.get("reason") or "" for row in trades(directory))
        need("trailing" in reasons, f"exit reasons: {reasons}")
        need(flat_book(directory), f"trail residual {remainder_tmn(directory):.4f} TMN")
        return "peak armed at +4%, exit on the give-back"

    def llm() -> str:
        notes = []
        world.reset()
        world.llm_mode = "veto"
        clock.unix = at(12, 0)
        directory = root / "e-veto"
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True)
        need(world.llm_calls >= 1, "veto never called the model")
        need(world.order_posts == 0, "veto still posted an order")
        need(any(row.get("veto_all") for row in cycles_of(directory)), "veto_all was not logged")
        notes.append("veto blocks the buy")

        world.reset()
        world.llm_mode = "accept"
        clock.unix = at(12, 0)
        directory = root / "e-shrink"
        journals["shrink"] = directory
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True)
        shrunk = [
            row for row in symbols_of(directory)
            if row.get("symbol") == "BTCTMN" and row.get("disposition") == "shrunk"
        ]
        need(shrunk, f"no shrunk BTCTMN row; {context(directory, world)}")
        before = float(shrunk[-1]["risk"]["size_before"])
        after = float(shrunk[-1]["risk"]["size_after"])
        need(before >= 0.12 - 1e-9, f"size_before {before}")
        need(after < 0.05, f"size_after {after} was not the quarter-Kelly size")
        replay = read_jsonl(directory / "replay.jsonl")
        need(any("0.99" in str(row.get("response") or "") for row in replay), "the 0.99 size was not in the reply")
        need(abs(after - 0.99) > 0.5, "the model size replaced the menu size")
        notes.append(f"shrink {before:.2f} -> {after:.4f}; 0.99 ignored")

        world.reset()
        world.llm_mode = "stall"
        clock.unix = at(12, 0)
        directory = root / "e-timeout"
        drive(
            world, directory, mode="live", cycles=20, send=True, authorize=True, override=True,
            llm_timeout=0.4,
        )
        sources = {row.get("llm_source") for row in cycles_of(directory)}
        need("fallback" in sources, f"timeout sources {sources}")
        notes.append(
            "timeout falls back"
            + (" and the fallback bought" if world.order_posts else " with no order")
        )

        world.reset()
        world.llm_mode = "bad"
        clock.unix = at(12, 0)
        directory = root / "e-bad"
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True)
        need(world.order_posts == 0, "malformed JSON posted an order")
        need(any(row.get("veto_all") for row in cycles_of(directory)), "malformed JSON did not veto")
        notes.append("malformed JSON vetoes")

        world.reset()
        world.llm_mode = "accept"
        clock.unix = at(12, 0)
        directory = root / "e-budget"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "llm_usage.json").write_text(
            json.dumps({"calls": [{"ts": clock.time(), "usd": 2.25}], "cache": {}}),
            encoding="utf-8",
        )
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True, fresh=True)
        ledger = json.loads((directory / "llm_usage.json").read_text(encoding="utf-8"))
        calls = ledger.get("calls") or []
        need(
            len(calls) == 1 and abs(float(calls[0].get("usd") or 0) - 2.25) < 1e-9,
            f"budget ledger changed: {calls}",
        )
        fallback = [
            row for row in cycles_of(directory)
            if row.get("llm_source") == "fallback" and float(row.get("llm_usd_total") or 0) >= 2.25
        ]
        need(fallback, "budget stop did not fall back with the seeded $2.25")
        notes.append(
            "budget stop skipped the model"
            + (" and the quant fallback bought" if world.order_posts else " with no order")
        )
        return "; ".join(notes)

    def guard() -> str:
        allow = ("USDTTMN", "BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN", "DOGETMN", "BNBTMN", "ADATMN", "XAUTTMN", "TONTMN")
        deny = ("TON", "NEAR", "FET", "INJ", "TAO", "WIF", "BONK", "PENDLE", "RENDER")

        def intent(**kwargs) -> OrderIntent:
            base = dict(
                symbol="BTCTMN", side="BUY", order_type="LIMIT", qty=0.2, price=1_000_000.0,
                mark=1_000_000.0, endpoint="/v1/account/orders", post_only=True, ops_state="LIVE_ARMED",
                held_qty=0.0, symbol_notional_tmn=0.0, gross_notional_tmn=0.0, equity_tmn=5_000_000.0,
                quote="TMN", base="BTC", allowlist=allow, deny_bases=deny, unavailable=(),
                size_cap=0.12, gross_cap=0.30,
            )
            base.update(kwargs)
            return OrderIntent(**base)

        denied = assess(intent(symbol="TONTMN", base="TON", qty=0.2))
        need(not denied.ok and "denylist" in denied.reason, denied.reason)
        small = assess(intent(qty=0.01))
        need(not small.ok and "50000" in small.reason, small.reason)
        sized = assess(intent(qty=0.8))
        need(not sized.ok and "size cap" in sized.reason, sized.reason)
        gross = assess(intent(qty=0.5, gross_notional_tmn=1_200_000.0))
        need(not gross.ok and "gross cap" in gross.reason, gross.reason)
        spot = assess(intent(symbol="BTCUSDT", base="BTC", quote="USDT"))
        need(not spot.ok and "spot" in spot.reason, spot.reason)
        endpoint = assess(intent(endpoint="/v1/account/easy-trade"))
        need(not endpoint.ok and "allowlist" in endpoint.reason, endpoint.reason)

        world.reset()
        posts = world.order_posts
        private = world.private_calls
        client = WallexClient(base_url=world.base, api_key="e2e-mock-key", subaccount_id="e2e-sub", timeout=2)
        path = "/v1/account/" + "with" + "draw" + "als"
        try:
            client._request("POST", path, body={"amount": "1"}, auth=True)
            raise Miss("a non-trading private path was sent")
        except WallexError as exc:
            need("spot-only" in str(exc), str(exc))
        need(world.order_posts == posts and world.private_calls == private, "the mock saw the refused path")
        need(world.nonspot_hits == 0, "non-trading path reached the socket")

        clock.unix = at(12, 0)
        directory = root / "f-guard"
        drive(world, directory, mode="shadow", cycles=1, send=False)
        universe = (cycles_of(directory)[-1].get("universe") or []) if cycles_of(directory) else []
        need(all(not str(symbol).startswith("TON") for symbol in universe), f"universe {universe}")
        return "denylist, 50k, size, gross, non-TMN, and a refused private path"

    def faults() -> str:
        world.reset()
        world.llm_mode = "accept"
        world.market_faults = [429, 503]
        world.order_stalls = 1
        world.order_faults = [429, 429, 429, 429]
        clock.unix = at(12, 0)
        directory = root / "g-faults"
        journals["faults"] = directory
        drive(
            world, directory, mode="live", cycles=20, send=True, authorize=True, override=True,
            timeout=1.0,
        )
        need(0.4 in world.sleeps and 0.8 in world.sleeps, f"backoff sleeps {world.sleeps[:8]}")
        errors = [item for row in cycles_of(directory) for item in (row.get("errors") or [])]
        need(any(item.get("http_status") == 429 for item in errors), f"errors {errors}")
        need(machine(directory) == "HALT", machine(directory))
        need(not positions(directory), "a rejected entry left a position")
        return "429 then 503 on markets, order timeout then 429, HALT"

    def dust() -> str:
        world.reset()
        world.llm_mode = "accept"
        world.fill_ratio = 0.05
        clock.unix = at(12, 0)
        directory = root / "h-dust"
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True)
        need(positions(directory), f"partial fill left nothing; {context(directory, world)}")
        held = positions(directory)["BTCTMN"]
        mark = float(held["qty"]) * float(held["entry_price"])
        need(mark < 50_000, f"partial notional {mark} is not dust")
        world.scale["BTCTMN"] = 0.94
        drive(world, directory, mode="live", cycles=2, send=True, authorize=True, override=True, fresh=False)
        blob = (directory / "decisions.jsonl").read_text(encoding="utf-8")
        need("order_guard" in blob, "dust top-up was not blocked")
        need("post-only" in blob or "maker LIMIT" in blob, "guard reason missing")
        need("BTCTMN" in positions(directory), "dust was liquidated by a top-up the guard should refuse")
        return f"5% fill (~{mark:.0f} TMN) stays; top-up blocked"

    def kills() -> str:
        directory = enter(world, clock, root, "i-kill")
        journals["kill"] = directory
        world.scale["BTCTMN"] = 0.30
        posts = world.order_posts
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        need(machine(directory) == "KILLED", machine(directory))
        need(world.order_posts == posts, "KILLED posted a top-up or a retry")
        last = cycles_of(directory)[-1]
        dust_tmn = float(last.get("dust_tmn") or 0)
        need(dust_tmn > 0, f"dust_tmn was not logged ({last.get('dust')})")
        dust_lots = (saved(directory).get("portfolio") or {}).get("dust") or {}
        need("BTCTMN" in dust_lots, "the unsellable coin was not kept as dust")
        need("BTCTMN" not in positions(directory), "dust stayed on the tradable book")
        need(abs(float(last.get("coin_notional_tmn") or 0) - dust_tmn) < 500, "dust is missing from equity")
        killed_buy = assess(OrderIntent(
            symbol="BTCTMN", side="BUY", order_type="LIMIT", qty=0.2, price=1_000_000.0,
            mark=1_000_000.0, endpoint="/v1/account/orders", post_only=True, ops_state="KILLED",
            held_qty=0.0, symbol_notional_tmn=0.0, gross_notional_tmn=0.0, equity_tmn=5_000_000.0,
            quote="TMN", base="BTC", allowlist=("BTCTMN",), deny_bases=(), unavailable=(),
            size_cap=0.12, gross_cap=0.30,
        ))
        need(not killed_buy.ok and "LIVE_ARMED" in killed_buy.reason, killed_buy.reason)
        killed = f"KILLED, no top-up, dust_tmn {dust_tmn:.0f}, buy still refused"

        directory = enter(world, clock, root, "i-halt")
        journals["halt"] = directory
        world.scale["BTCTMN"] = 0.94
        world.order_faults = [400]
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        need(machine(directory) == "HALT", machine(directory))
        need("BTCTMN" in positions(directory), "HALT flattened a coin the exit could not sell")
        halted = "HALT leaves the coin when the exit is rejected"

        directory = enter(world, clock, root, "i-flatten", start=at(22, 40))
        journals["flatten"] = directory
        clock.unix = at(22, 55, day=23)
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        need(machine(directory) == "FLATTEN", machine(directory))
        need(flat_book(directory), f"flatten residual {remainder_tmn(directory):.4f} TMN")
        return f"{killed}; {halted}; FLATTEN sold to TMN"

    def cutoff() -> str:
        world.reset()
        world.llm_mode = "accept"
        clock.unix = at(23, 31)
        directory = root / "j-cutoff"
        drive(world, directory, mode="live", cycles=20, send=True, authorize=True, override=True)
        rows = cycles_of(directory)
        need(rows and rows[0].get("state") == "LIVE_MANAGE", f"first state {rows[0].get('state') if rows else None}")
        need(all(row.get("state") == "LIVE_MANAGE" for row in rows), machine(directory))
        need(sum(int(row.get("n_orders_submitted") or 0) for row in rows) == 0, "cutoff submitted an order")
        need(world.order_posts == 0, "cutoff posted an order")
        return "Thu 23:31 opens in LIVE_MANAGE with zero orders"

    def gating() -> str:
        world.reset()
        world.llm_mode = "accept"
        clock.unix = at(12, 0)
        directory = root / "k-live"
        drive(world, directory, mode="live", cycles=20, send=True, authorize=False, override=True)
        need(all(row.get("state") == "SHADOW" for row in cycles_of(directory)), machine(directory))
        need(world.order_posts == 0, "live without live_authorized posted an order")
        world.reset()
        world.llm_mode = "accept"
        clock.unix = at(12, 0)
        directory = root / "k-shadow"
        drive(world, directory, mode="shadow", cycles=3, send=False, authorize=True, override=True)
        need(world.order_posts == 0, "shadow posted an order")
        need(machine(directory) == "SHADOW", machine(directory))
        shadow_buy = assess(OrderIntent(
            symbol="BTCTMN", side="BUY", order_type="LIMIT", qty=0.2, price=1_000_000.0,
            mark=1_000_000.0, endpoint="/v1/account/orders", post_only=True, ops_state="SHADOW",
            held_qty=0.0, symbol_notional_tmn=0.0, gross_notional_tmn=0.0, equity_tmn=5_000_000.0,
            quote="TMN", base="BTC",
            allowlist=("BTCTMN",), deny_bases=(), unavailable=(), size_cap=0.12, gross_cap=0.30,
        ))
        need(not shadow_buy.ok and "LIVE_ARMED" in shadow_buy.reason, shadow_buy.reason)
        return "no live_authorized keeps SHADOW and zero order posts"

    def restart() -> str:
        directory = enter(world, clock, root, "l-restart")
        first = saved(directory)
        boot = (first.get("ops") or {}).get("client_boot")
        first_ids = [row["client_id"] for row in world.fills]
        need(boot, "client boot id was not saved")
        need(len(first_ids) == len(set(first_ids)), f"duplicate ids {first_ids}")
        clock.advance(24 * 3600 + 120)
        drive(world, directory, mode="live", cycles=1, send=True, authorize=True, override=True, fresh=False)
        second = saved(directory)
        need((second.get("ops") or {}).get("client_boot") == boot, "restart minted a new boot id")
        ids = [row["client_id"] for row in world.fills]
        need(len(ids) == len(set(ids)) and len(ids) >= 2, f"client ids {ids}")
        need(flat_book(directory), f"restart residual {remainder_tmn(directory):.4f} TMN")
        return f"boot {boot} stable, {len(ids)} distinct client ids"

    case("a quiet", quiet)
    case("b dip-buy", dip)
    case("c stop", stop)
    case("d trail", trail)
    case("e llm", llm)
    case("f guard", guard)
    case("g faults", faults)
    case("h dust", dust)
    case("i kills", kills)
    case("j cutoff", cutoff)
    case("k gating", gating)
    case("l restart", restart)

    def report() -> str:
        target = journals.get("quiet")
        need(target is not None, "quiet journal missing")
        completed = _command([
            sys.executable, "-m", "trader", "report", "--checkpoint", "C0",
            "--log-dir", str(target), "--out", str(target),
        ])
        text = (target / "cycle-review.md").read_text(encoding="utf-8")
        need("Checkpoint" in text and "Audit" in text, "cycle review is missing the checkpoint or the audit")
        need(completed.returncode == 0, completed.stderr[-400:])
        return "C0 review written, audit block appended"

    def audit_quiet() -> str:
        return _audit(journals.get("quiet"), "quiet")

    def audit_roundtrip() -> str:
        return _audit(journals.get("dip"), "round trip")

    def audit_stop() -> str:
        return _audit(journals.get("stop"), "stop")

    def scorecard() -> str:
        target = journals.get("dip") or journals.get("shrink")
        need(target is not None, "no journal for the scorecard")
        candles = root / "candles"
        candles.mkdir(parents=True, exist_ok=True)
        _write_candles(candles, at(12, 0) + 48 * 3600)
        completed = _command([
            sys.executable, "-m", "trader", "scorecard",
            "--log-dir", str(target), "--candles", str(candles), "--out", str(target),
        ])
        need(completed.returncode == 0, completed.stderr[-400:])
        need((target / "scorecard.md").exists(), "scorecard.md missing")
        return "scorecard.md written from the scripted candles"

    def watchdog() -> str:
        target = journals.get("kill") or journals.get("halt") or journals.get("faults")
        need(target is not None, "no halt or kill journal")
        off = _command([sys.executable, "-m", "trader", "watchdog", "--once", "--log-dir", str(target)])
        need(off.returncode == 0 and "watchdog off" in off.stdout, off.stdout)
        hook = root / "telegram-hook"
        hook.mkdir(parents=True, exist_ok=True)
        (hook / "sitecustomize.py").write_text(
            "import json, os, urllib.request\n"
            "import trader.watchdog as watchdog\n"
            "def _post(url, body):\n"
            "    request = urllib.request.Request(os.environ['E2E_TELEGRAM_BASE'], data=body,"
            " headers={'Content-Type': 'application/json'}, method='POST')\n"
            "    with urllib.request.urlopen(request, timeout=5) as response:\n"
            "        response.read()\n"
            "watchdog._post = _post\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        # sitecustomize runs before the cwd is on sys.path, so the repo root has to be listed.
        repo = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(hook), repo, env.get("PYTHONPATH", "")) if item
        )
        env.update({
            "E2E_TELEGRAM_BASE": f"{world.base}/telegram",
            "WATCHDOG_ENABLED": "1",
            "TELEGRAM_ALERT_BOT_TOKEN": "e2e-alert-token",
            "TELEGRAM_ALERT_CHAT_ID": "e2e-chat",
        })
        for key in ("WALLEX_API_KEY", "WALLEX_SUBACCOUNT_ID", "TRADER_LIVE_AUTHORIZED"):
            env.pop(key, None)
        completed = _command(
            [sys.executable, "-m", "trader", "watchdog", "--once", "--log-dir", str(target)],
            env=env,
        )
        need(completed.returncode == 0, completed.stderr[-400:])
        need("Error in sitecustomize" not in completed.stderr, completed.stderr[-400:])
        need("watchdog alert failed" not in completed.stderr, completed.stderr[-400:])
        texts = [str(item.get("text") or "") for item in world.telegram]
        need(texts, "mock Telegram received nothing")
        blob = "\n".join(texts)
        need("pnl" in blob, blob)
        need("KILLED" in blob or "HALT" in blob or "order reject" in blob, blob)
        for banned in ("e2e-alert-token", "e2e-mock-key", "e2e-sub", "e2e-llm-key"):
            need(banned not in blob, f"alert contained {banned}")
        return f"{len(texts)} alert(s): {texts[0][:80]}"

    def dashboard() -> str:
        desk = root / "desk"
        sources = [journals[name] for name in ("faults", "dip", "kill") if name in journals]
        need(sources, "desk journals missing")
        desk.mkdir(parents=True, exist_ok=True)
        for name in ("cycles.jsonl", "decisions.jsonl", "adjustments.jsonl"):
            parts = []
            for source in sources:
                path = source / name
                if path.exists():
                    parts.append(path.read_text(encoding="utf-8"))
            (desk / name).write_text("".join(parts), encoding="utf-8")
        completed = _command([
            sys.executable, "-m", "trader", "report", "--checkpoint", "C0",
            "--log-dir", str(desk), "--out", str(desk),
        ])
        need(completed.returncode == 0, completed.stderr[-400:])
        os.environ["DASHBOARD_USER"] = "desk"
        os.environ["DASHBOARD_PASSWORD"] = "desk-secret"
        # The virtual clock replaces time.time and time.sleep. Uvicorn's bind wait needs real time.
        restore(saved_bindings)
        from trader.health import serve_in_background
        bound = serve_in_background("127.0.0.1", 0, desk)
        host, port = bound.server_address
        try:
            health = _get(f"http://{host}:{port}/health")
            need(health.status == 200, f"health {health.status}")
            closed = _get(f"http://{host}:{port}/")
            need(closed.status == 401, f"desk without a password returned {closed.status}")
            page = _get(f"http://{host}:{port}/", user="desk", password="desk-secret")
            need(page.status == 200, f"authed desk {page.status}")
            html = page.body
            for label in ("Decision card", "Equity", "Wallex", "LLM spend", "Adjustments", "Checkpoint", "BTCTMN", "dust_tmn"):
                need(label in html, f"panel missing {label}")
            need("429" in html, "Wallex panel has no 429")
            need("desk-secret" not in html, "the desk echoed its password")
            shot = _shoot(host, port)
        finally:
            bound.shutdown()
        return f"health 200, anonymous 401, panels populated{shot}"

    case("report C0", report)
    case("audit quiet", audit_quiet)
    case("audit round trip", audit_roundtrip)
    case("audit stop journal", audit_stop)
    case("scorecard", scorecard)
    case("watchdog", watchdog)
    case("dashboard", dashboard)

    restore(saved_bindings)
    server.shutdown()
    lines = ["| Scenario | Result | Detail |", "| --- | --- | --- |"]
    failed = 0
    for name, ok, detail in results:
        reported = name.startswith("audit") and ok and "verdict FAIL" in detail
        verdict = "FAIL" if (not ok or reported) else "PASS"
        if not ok:
            failed += 1
        lines.append(f"| {name} | {verdict} | {detail.replace('|', '/')} |")
    table = "\n".join(lines)
    (root / "report.md").write_text(table + "\n", encoding="utf-8")
    print("\n" + table, flush=True)
    return 1 if failed else 0


def _audit(directory: Path | None, label: str) -> str:
    need(directory is not None, f"{label} journal missing")
    completed = _command([
        sys.executable, "-m", "trader", "audit", "--e2e",
        "--log-dir", str(directory), "--out", str(directory),
    ])
    payload = _last_json(completed.stdout)
    need(completed.returncode == 0, completed.stderr[-400:])
    need(payload.get("ok") is True, f"{label} audit failed: {payload.get('failures')}")
    need(payload.get("secret_hits") == 0, "secret hit")
    return f"PASS drift {payload.get('max_drift_tmn')} TMN, secrets 0"


def _command(argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=env, check=False)


def _last_json(text: str) -> dict:
    blob = text.strip()
    start = blob.rfind("{")
    if start < 0:
        return {}
    try:
        return json.loads(blob[start:])
    except json.JSONDecodeError:
        return {}


def _write_candles(directory: Path, end: float) -> None:
    from trader.e2e.mock import CANDIDATES
    from trader.e2e.series import hourly_bars

    for symbol in CANDIDATES:
        kind = "dip" if symbol == "BTCTMN" else "flat"
        bars = hourly_bars(kind, int(end), count=140)
        lines = ["time,open,high,low,close,volume"]
        for bar in bars:
            lines.append(f"{bar.time},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}")
        (directory / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get(url: str, user: str | None = None, password: str | None = None):
    import base64
    import urllib.error
    import urllib.request

    headers = {}
    if user is not None:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return _Response(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        return _Response(exc.code, exc.read().decode(errors="replace"))


class _Response:
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body


def _shoot(host: str, port: int) -> str:
    """Headless Chrome. The new headless build stalls on the basic-auth URL, so this uses the old one and stops it once the file is on disk."""
    chrome = "/usr/local/bin/google-chrome"
    if not Path(chrome).exists():
        return ""
    import time

    out = Path("/opt/cursor/artifacts")
    out.mkdir(parents=True, exist_ok=True)
    shots = (
        (out / "e2e-dashboard.png", "1400,3200", "desk"),
        (out / "e2e-dashboard-mobile.png", "390,2200", "mobile"),
    )
    url = f"http://desk:desk-secret@{host}:{port}/"
    for target, size, name in shots:
        profile = Path(f"/tmp/e2e-chrome-{name}")
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        if target.exists():
            target.unlink()
        proc = subprocess.Popen(
            [
                chrome, "--headless=old", "--no-sandbox", "--disable-gpu",
                f"--user-data-dir={profile}",
                f"--window-size={size}",
                "--virtual-time-budget=4000",
                "--timeout=8000",
                f"--screenshot={target}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 12
        while time.time() < deadline:
            if target.exists() and target.stat().st_size > 1000:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        need(target.exists() and target.stat().st_size > 1000, f"screenshot missing: {target}")
    return f", screenshots {shots[0][0]}"


def main() -> int:
    host_key = os.environ.get("WALLEX_API_KEY", "")
    if os.environ.get("E2E_REQUIRE_NO_HOST_KEYS") == "1" and host_key:
        print("FAIL host environment already has WALLEX_API_KEY")
        return 1
    try:
        return run_all()
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
