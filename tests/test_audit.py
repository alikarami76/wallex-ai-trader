"""Offline audit: balances, clocks, orders, and a secret scan that does not echo the secret."""

import json
import tempfile
import unittest
from pathlib import Path

from trader.audit import audit_summary, build_audit, write_audit
from trader.cli import main
from trader.engine.ops import config_hash
from trader.config import load_config


def _write(root: Path, name: str, rows: list[dict]) -> None:
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (root / name).write_text(text, encoding="utf-8")


class AuditTests(unittest.TestCase):
    def test_balanced_book_passes_and_a_secret_fails_without_being_copied(self):
        root = Path(tempfile.mkdtemp())
        secret = "sk-" + "a" * 20
        _write(root, "decisions.jsonl", [{
            "timestamp": "2026-10-22T16:35:12+03:30",
            "mode": "shadow",
            "equity_tmn": 4_999_750.0,
            "features": [{"symbol": "BTCTMN", "close": 100.0}],
            "fills": [{
                "symbol": "BTCTMN", "side": "BUY", "price": 100.0, "qty": 997.5,
                "notional_tmn": 100_000.0, "fee_tmn": 250.0, "order_type": "LIMIT",
                "client_id": "SC1",
            }],
        }])
        clean = build_audit(root, starting_cash=5_000_000)
        self.assertTrue(clean["ok"], clean["failures"])
        self.assertLessEqual(clean["max_drift_tmn"], 1)
        self.assertEqual(clean["fee_bill_tmn"], 250.0)
        self.assertEqual(clean["fills"][0]["liquidity"], "maker")
        self.assertAlmostEqual(clean["fills"][0]["slippage_bps"], 0.0)
        self.assertIn("Fee bill: 250.0 TMN.", write_audit(root, root) and (root / "audit.md").read_text(encoding="utf-8"))

        _write(root, "replay.jsonl", [{"prompt": secret, "response": "hold"}])
        leaked = build_audit(root, starting_cash=5_000_000)
        self.assertFalse(leaked["ok"])
        self.assertIn("secret-like string", leaked["failures"])
        rendered = write_audit(root, root)
        text = (root / "audit.md").read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn(secret, json.dumps(rendered))
        self.assertEqual(leaked["secret_hits"][0]["pattern"], "openai_key")

    def test_drift_duplicates_gaps_clocks_orders_and_snapshot(self):
        root = Path(tempfile.mkdtemp())
        _write(root, "cycles.jsonl", [
            {
                "record": "cycle", "cycle_id": "c1", "equity_tmn": 5_000_000,
                "ts_utc": "2026-10-22T12:00:00Z", "ts_tehran": "2026-10-22T15:30:00+03:30",
                "git_sha": "abc", "go_live_sha": "abc", "config_hash": "hash",
            },
            {
                "record": "cycle", "cycle_id": "c2", "equity_tmn": 5_000_010,
                "ts_utc": "2026-10-22T12:10:00Z", "ts_tehran": "2026-10-22T16:30:00+03:30",
                "git_sha": "abc", "go_live_sha": "different", "config_hash": "other",
            },
        ])
        _write(root, "decisions.jsonl", [
            {
                "record": "symbol", "cycle_id": "c2", "symbol": "BTCTMN",
                "git_sha": "abc", "go_live_sha": "different", "config_hash": "other",
                "gate_trace": [{"gate": "cost", "pass": False}],
                "order": {
                    "status": "filled", "side": "BUY", "type": "LIMIT", "client_id": "SC1",
                    "limit_price": 100, "qty": 1, "filled_qty": 1, "notional_tmn": 100,
                    "fee_tmn": 1, "liquidity_flag": "maker",
                },
            },
            {
                "record": "symbol", "cycle_id": "c2", "symbol": "ETHTMN",
                "order": {"status": "filled", "side": "BUY", "client_id": "SC1", "limit_price": 10, "qty": 1, "notional_tmn": 10, "fee_tmn": 0},
                "gate_trace": [{"gate": "trend", "pass": True}],
                "git_sha": "abc", "go_live_sha": "abc", "config_hash": "hash",
            },
        ])
        report = build_audit(root, starting_cash=5_000_000, config_hash="hash")
        self.assertFalse(report["ok"])
        self.assertGreater(report["max_drift_tmn"], 1)
        self.assertEqual(report["duplicate_client_ids"][0]["client_id"], "SC1")
        self.assertEqual(len(report["gaps"]), 1)
        self.assertTrue(report["clock_mismatches"])
        self.assertTrue(report["order_failures"])
        snapshot = root / "balances.json"
        snapshot.write_text(json.dumps({"TMN": 1}), encoding="utf-8")
        compared = build_audit(root, starting_cash=5_000_000, config_hash="hash", wallex_snapshot=snapshot)
        self.assertIn("wallex snapshot drift", compared["failures"])

    def test_checkpoint_report_appends_the_audit_summary(self):
        root = Path(tempfile.mkdtemp())
        _write(root, "cycles.jsonl", [{
            "ts_tehran": "2026-10-22T18:00:00+03:30",
            "ts_utc": "2026-10-22T14:30:00Z",
            "equity_tmn": 5_000_000,
            "n_orders_submitted": 0,
            "n_quant_pass": 1,
            "zscore_n": 20,
            "zscore_ready": True,
            "state": "SHADOW",
            "git_sha": "abc",
            "go_live_sha": "abc",
            "config_hash": config_hash(load_config("config/default.yaml")),
        }])
        _write(root, "decisions.jsonl", [])
        main(["report", "--checkpoint", "C0", "--log-dir", str(root), "--out", str(root)])
        text = (root / "cycle-review.md").read_text(encoding="utf-8")
        self.assertIn("Suggested action: A0_NO_CHANGE", text)
        self.assertIn("## Audit", text)
        self.assertIn("secret_hits: 0", text)
        self.assertTrue(text.strip().splitlines()[0].startswith("Checkpoint:"))
        summary = audit_summary({"ok": True, "max_drift_tmn": 0, "duplicate_client_ids": [], "gaps": [], "clock_mismatches": [], "order_failures": [], "secret_hits": [], "fee_bill_tmn": 0})
        self.assertNotIn("sk-", summary)


if __name__ == "__main__":
    unittest.main()
