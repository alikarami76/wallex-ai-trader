import json
import os
import unittest
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from trader.dashboard import create_app
from trader.report import decision_cards


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data/test_dashboard")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("cycles.jsonl", "decisions.jsonl", "adjustments.jsonl", "reviews.jsonl"):
            path = self.root / name
            if path.exists():
                path.unlink()
        self.saved = {
            key: os.environ.get(key)
            for key in ("DASHBOARD_USER", "DASHBOARD_PASSWORD", "DASHBOARD_USERS", "WALLEX_API_KEY")
        }
        os.environ["DASHBOARD_USER"] = "desk-operator"
        os.environ["DASHBOARD_PASSWORD"] = "desk-password-not-for-html"
        os.environ.pop("DASHBOARD_USERS", None)
        os.environ["WALLEX_API_KEY"] = "wallex-secret-value"
        self.client = TestClient(create_app(self.root))

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_no_mutating_routes(self):
        paths = set()
        methods = set()
        for route in self.client.app.routes:
            if not isinstance(route, APIRoute):
                continue
            paths.add(route.path)
            methods |= set(route.methods or ())
        self.assertEqual(paths, {"/", "/health", "/status"})
        self.assertTrue(methods <= {"GET", "HEAD"})
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.client.request(method, "/")
            self.assertEqual(response.status_code, 405, method)
            response = self.client.request(method, "/health")
            self.assertEqual(response.status_code, 405, method)
            response = self.client.request(method, "/orders")
            self.assertEqual(response.status_code, 405, method)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/redoc").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_health_is_open_and_desk_requires_auth(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"ok": True})
        empty = self.client.get("/", auth=("desk-operator", "desk-password-not-for-html"))
        self.assertEqual(empty.status_code, 200)
        self.assertIn("waiting for the first cycle", empty.text)
        self.assertIn("logged in as desk-operator", empty.text)
        self.assertNotIn("desk-password-not-for-html", empty.text)
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(self.client.get("/status").status_code, 401)
        wrong = self.client.get("/", auth=("desk-operator", "nope"))
        self.assertEqual(wrong.status_code, 401)
        os.environ.pop("DASHBOARD_PASSWORD")
        closed = TestClient(create_app(self.root))
        self.assertEqual(closed.get("/health").status_code, 200)
        self.assertEqual(closed.get("/", auth=("desk-operator", "desk-password-not-for-html")).status_code, 401)

    def test_page_reads_journals_and_hides_secrets(self):
        secret = "wallex-secret-value"
        password = "desk-password-not-for-html"
        self._write("cycles.jsonl", [{
            "record": "cycle",
            "cycle_id": "c1",
            "ts_utc": "2026-10-22T18:00:00Z",
            "ts_tehran": "2026-10-22T21:30:00+03:30",
            "state": "LIVE_ARMED",
            "mode": "live",
            "git_sha": "abc123running",
            "go_live_sha": "def456golive",
            "loop_latency_ms": 840,
            "equity_tmn": 5_010_000,
            "free_tmn": 4_800_000,
            "pnl_from_golive_tmn": 10000,
            "pnl_from_golive_pct": 0.002,
            "dust": [{"symbol": "DOGETMN", "notional_tmn": 1200}],
            "open_orders": ["SCabc1"],
            "open_positions": ["BTCTMN"],
            "fee_maker_bps": 25,
            "fee_taker_bps": 30,
            "fee_hurdle": 0.0072,
            "fee_fetched_at": "2026-10-22T18:00:00Z",
            "llm_usd_total": 0.42,
            "llm_usd_today": 0.11,
            "llm_tier": "routine",
            "llm_model": "openai/gpt-6-luna",
            "llm_source": "llm",
            "wallex_key_present": True,
            "subaccount_present": False,
            "llm_key_present": True,
            "n_orders_rejected": 1,
            "errors": [{
                "endpoint": "/v1/account/orders",
                "http_status": 400,
                "message": f"rejected {secret}",
                "api_key": secret,
            }],
            "api_key": secret,
        }])
        self._write("decisions.jsonl", [
            {
                "record": "symbol",
                "cycle_id": "c1",
                "symbol": "BTCTMN",
                "first_block": "cost",
                "gate_trace": [
                    {"gate": "trend", "pass": True},
                    {"gate": "cost", "pass": False, "value": 0.003, "threshold": 0.0045},
                ],
                "llm": {
                    "tier": "routine", "model": "openai/gpt-6-luna", "source": "fallback",
                    "confidence": 0.62, "veto": False, "rationale": "4h pullback is inside the band",
                },
                "risk": {
                    "verdict": "pass",
                    "size_before": 0.12,
                    "size_after": 0.06,
                    "reason_codes": ["liquidity scale"],
                },
                "signal": {"close": 100, "roc_24h": 0.02, "roc_4h": -0.008, "spread_bps": 10, "signal": True},
                "cost_gate": {
                    "prior": 0.0045, "hurdle": 0.0072, "pass": False,
                    "fee_in_bps": 25, "fee_out_bps": 30, "half_spread_bps": 5, "buffer_bps": 3,
                },
                "order": {"status": "filled", "side": "BUY", "qty": 0.001, "notional_tmn": 100000},
            },
            {
                "timestamp": "2026-10-22T21:30:00+03:30",
                "positions": [{"symbol": "BTCTMN", "qty": 0.001, "entry_price": 100000000}],
                "agent": {
                    "comment": "held the pullback",
                    "decisions": [{"symbol": "BTCTMN", "rationale": "4h pullback is inside the band", "confidence": 0.62}],
                },
                "fills": [{"symbol": "BTCTMN", "side": "BUY", "qty": 0.001, "notional_tmn": 100000}],
            },
        ])
        self._write("adjustments.jsonl", [{
            "ts_tehran": "2026-10-22T21:31:00+03:30",
            "action": "A0_NO_CHANGE",
            "state_before": "LIVE_ARMED",
            "state_after": "LIVE_ARMED",
            "actor": "bot",
            "applied": True,
            "note": f"note leaked {password}",
        }])
        self._write("reviews.jsonl", [{
            "checkpoint": "C1",
            "suggested_action": "A0_NO_CHANGE",
            "operator_action": None,
            "draft": True,
            "equity_end_tmn": 5010000,
            "note": "checkpoint draft",
            "prompt": "do not render this prompt",
        }])
        page = self.client.get("/", auth=("desk-operator", password))
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("LIVE_ARMED", html)
        self.assertIn("BTCTMN", html)
        self.assertIn("4h pullback is inside the band", html)
        self.assertIn("cost", html)
        self.assertIn("gpt-6-luna", html)
        self.assertIn("A0_NO_CHANGE", html)
        self.assertIn("C1", html)
        self.assertIn("Wallex key present", html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("#010814", html)
        self.assertIn("#508cf3", html)
        self.assertIn("$2.50", html)
        self.assertNotIn(secret, html)
        self.assertNotIn(password, html)
        self.assertIn("logged in as desk-operator", html)
        self.assertNotIn("do not render this prompt", html)
        self.assertIn("present", html)
        self.assertIn("Equity", html)
        self.assertIn("dust_tmn", html)
        self.assertIn("1,200.00", html)
        self.assertIn("Decision card", html)
        self.assertIn("First block", html)
        self.assertIn("half-spread", html)
        self.assertIn("buffer", html)
        self.assertIn("fallback", html)
        self.assertIn("0.1200 → 0.0600", html)
        self.assertIn("liquidity scale", html)
        self.assertIn("roc_24h", html)
        card = decision_cards([{
            "record": "symbol",
            "cycle_id": "c1",
            "symbol": "BTCTMN",
            "first_block": "cost",
            "signal": {"close": 100, "roc_24h": 0.02, "signal": True},
            "cost_gate": {
                "prior": 0.0045, "fee_in_bps": 25, "fee_out_bps": 30,
                "half_spread_bps": 5, "buffer_bps": 3, "hurdle": 0.0072,
            },
            "llm": {"source": "fallback", "confidence": 0.62, "rationale": "4h pullback is inside the band"},
            "risk": {"size_before": 0.12, "size_after": 0.06, "reason_codes": ["liquidity scale"]},
            "prompt": secret,
        }])
        self.assertIn("First block: cost", card)
        self.assertIn("prior 0.0045", card)
        self.assertIn("half-spread 5", card)
        self.assertIn("0.12 → 0.06", card)
        self.assertNotIn(secret, card)

    def test_hashed_users_and_the_plaintext_pair(self):
        import bcrypt
        from trader.dashboard import _new_sha256

        sha_password = "sha-secret-value"
        bcrypt_password = "bcrypt-secret-value"
        sha_hash = _new_sha256(sha_password)
        bcrypt_hash = bcrypt.hashpw(bcrypt_password.encode(), bcrypt.gensalt(rounds=4)).decode()
        os.environ["DASHBOARD_USERS"] = json.dumps({
            "sam": sha_hash,
            "bob": bcrypt_hash,
            "op<erator>": _new_sha256("angle-secret"),
        })
        client = TestClient(create_app(self.root))
        with self.assertLogs("trader.dashboard", level="INFO") as captured:
            page = client.get("/", auth=("sam", sha_password))
        self.assertEqual(page.status_code, 200)
        self.assertIn("logged in as sam", page.text)
        self.assertNotIn(sha_password, page.text)
        self.assertNotIn(sha_hash, page.text)
        blob = "\n".join(captured.output)
        self.assertIn("user=sam", blob)
        self.assertNotIn(sha_password, blob)
        bob = client.get("/status", auth=("bob", bcrypt_password))
        self.assertEqual(bob.status_code, 200)
        self.assertNotIn(bcrypt_password, json.dumps(bob.json()))
        plain = client.get("/", auth=("desk-operator", "desk-password-not-for-html"))
        self.assertEqual(plain.status_code, 200)
        self.assertIn("logged in as desk-operator", plain.text)
        self.assertEqual(client.get("/", auth=("sam", "nope")).status_code, 401)
        self.assertEqual(client.get("/", auth=("nobody", sha_password)).status_code, 401)
        with self.assertNoLogs("trader.dashboard", level="INFO"):
            self.assertEqual(client.get("/", auth=("sam", "still-wrong")).status_code, 401)
        escaped = client.get("/", auth=("op<erator>", "angle-secret"))
        self.assertEqual(escaped.status_code, 200)
        self.assertIn("logged in as op&lt;erator&gt;", escaped.text)
        self.assertNotIn("op<erator>", escaped.text)

    def test_closed_when_no_user_is_configured(self):
        os.environ.pop("DASHBOARD_USER", None)
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ["DASHBOARD_USERS"] = "{not-json plaintext-secret-not-logged"
        client = TestClient(create_app(self.root))
        with self.assertLogs("trader.dashboard", level="WARNING") as captured:
            denied = client.get("/", auth=("ada", "plaintext-secret-not-logged"))
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(client.get("/status").status_code, 401)
        self.assertEqual(client.get("/health").status_code, 200)
        blob = "\n".join(captured.output)
        self.assertNotIn("plaintext-secret-not-logged", blob)
        os.environ["DASHBOARD_USERS"] = "{}"
        empty = TestClient(create_app(self.root))
        self.assertEqual(empty.get("/", auth=("desk-operator", "desk-password-not-for-html")).status_code, 401)

    def test_dashboard_hash_reads_stdin(self):
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, "-m", "trader", "dashboard-hash", "ada"],
            input="cli-password-value\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("cli-password-value", completed.stdout)
        self.assertNotIn("cli-password-value", completed.stderr)
        entry = json.loads(completed.stdout)
        self.assertEqual(set(entry), {"ada"})
        self.assertTrue(entry["ada"].startswith("$2"))
        os.environ.pop("DASHBOARD_USER", None)
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ["DASHBOARD_USERS"] = completed.stdout.strip()
        client = TestClient(create_app(self.root))
        with self.assertLogs("trader.dashboard", level="INFO") as captured:
            page = client.get("/", auth=("ada", "cli-password-value"))
        self.assertEqual(page.status_code, 200)
        self.assertIn("logged in as ada", page.text)
        self.assertNotIn("cli-password-value", page.text)
        self.assertNotIn("cli-password-value", "\n".join(captured.output))
        self.assertEqual(client.get("/", auth=("ada", "other")).status_code, 401)
        empty = subprocess.run(
            [sys.executable, "-m", "trader", "dashboard-hash", "ada"],
            input="\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(empty.returncode, 2)
        self.assertEqual(empty.stdout, "")
        missing = subprocess.run(
            [sys.executable, "-m", "trader", "dashboard-hash"],
            input="secret\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("secret", missing.stdout)
        self.assertNotIn("secret", missing.stderr)

    def _write(self, name: str, rows: list[dict]) -> None:
        text = "".join(json.dumps(row) + "\n" for row in rows)
        (self.root / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
