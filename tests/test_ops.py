"""Operational contract: state machine, loosening, Tehran midnight, checkpoint report."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trader.cli import main
from trader.engine.ops import (
    Facts,
    apply_frozen_cost_gate,
    fresh_ops,
    llm_override_warning,
    next_client_id,
    propose,
    replay_offline,
    stamp,
    suggest_action,
    advance,
    entries_allowed,
    write_checkpoint,
)
from trader.risk.manager import roll_day
from trader.strategy.router import choose_tier
from trader.timeutil import TEHRAN, day_key

CFG = {
    "strategy": {
        "edge_multiple": 1.2,
        "expected_gross": 0.009,
        "slow_roc_min": 0.0,
        "trend_roc_min": 0.01,
        "pullback_roc_max": -0.004,
        "max_atr_pct": 0.03,
        "imbalance_block": -0.55,
        "min_micro_return": -0.004,
    },
    "universe": {"max_spread_bps": 22, "min_quote_volume_tmn": 1, "max_symbols": 6},
    "risk": {
        "daily_max_drawdown": 0.015,
        "global_max_drawdown": 0.03,
        "min_confidence": 0.55,
        "min_notional_tmn": 50000,
        "max_fraction_per_trade": 0.12,
        "max_gross_exposure": 0.30,
    },
    "liquidity": {"min_depth_tmn": 1_000_000, "z_block": 2.0, "max_order_fraction_of_depth": 0.10},
    "fees": {"slippage_bps": 3, "maker_rate": 0.0025, "taker_rate": 0.0030},
    "ops": {
        "frozen_gross_prior": 0.0045,
        "entry_cutoff": "2026-10-22T23:30:00+03:30",
        "zscore_min_n": 18,
    },
}


def _facts(now, **kwargs) -> Facts:
    base = dict(
        now=now,
        mode="shadow",
        send_orders=False,
        equity=5_000_000,
        zscore_n=18,
    )
    base.update(kwargs)
    return Facts(**base)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            key: os.environ.get(key)
            for key in ("TRADER_LIVE_AUTHORIZED", "TRADER_EV_OVERRIDE", "LLM_MODEL", "WALLEX_API_KEY")
        }
        for key in self._env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_shadow_is_the_default_and_live_needs_the_flag(self):
        ops = fresh_ops()
        now = datetime(2026, 10, 22, 22, 0, tzinfo=TEHRAN)
        advance(ops, _facts(now, mode="live", send_orders=True), CFG, stage="pre")
        self.assertEqual(ops["state"], "SHADOW")
        self.assertFalse(ops["locked"])
        allowed, reason = entries_allowed(ops, _facts(now), CFG)
        self.assertFalse(allowed)
        self.assertEqual(reason, "shadow")

    def test_authorized_before_cutoff_arms_and_locks_the_override(self):
        os.environ["TRADER_LIVE_AUTHORIZED"] = "1"
        ops = fresh_ops()
        now = datetime(2026, 10, 22, 22, 0, tzinfo=TEHRAN)
        advance(ops, _facts(now, mode="live", send_orders=True, fee_hurdle=0.002), CFG, stage="pre")
        self.assertEqual(ops["state"], "LIVE_ARMED")
        self.assertTrue(ops["locked"])
        self.assertFalse(ops["ev_override"])
        self.assertEqual(ops["go_live_equity"], 5_000_000)
        os.environ["TRADER_EV_OVERRIDE"] = "1"
        rows = advance(ops, _facts(now, mode="live", send_orders=True, fee_hurdle=0.002), CFG, stage="pre")
        self.assertFalse(ops["ev_override"])
        self.assertEqual(rows[-1]["rejected_action"], "ev_override_after_golive")

    def test_go_live_after_cutoff_starts_in_manage(self):
        os.environ["TRADER_LIVE_AUTHORIZED"] = "1"
        os.environ["TRADER_EV_OVERRIDE"] = "1"
        ops = fresh_ops()
        now = datetime(2026, 10, 22, 23, 31, tzinfo=TEHRAN)
        advance(ops, _facts(now, mode="live", send_orders=True), CFG, stage="pre")
        self.assertEqual(ops["state"], "LIVE_MANAGE")
        self.assertTrue(ops["ev_override"])
        self.assertTrue(ops["locked"])

    def test_bot_actions_halt_kill_size_and_cutoff(self):
        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, go_live_equity=5_000_000, ev_override=False)
        now = datetime(2026, 10, 22, 22, 10, tzinfo=TEHRAN)
        advance(ops, _facts(now, mode="live", send_orders=True, order_rejects=1), CFG, stage="post")
        self.assertEqual(ops["state"], "HALT")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, go_live_equity=5_000_000)
        advance(ops, _facts(now, mode="live", send_orders=True, daily_drawdown=0.015), CFG, stage="pre")
        self.assertEqual(ops["state"], "KILLED")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, go_live_equity=5_000_000, size_multiplier=1.0)
        advance(ops, _facts(now, mode="live", send_orders=True, pnl_from_golive_pct=-0.006), CFG, stage="post")
        self.assertEqual(ops["size_multiplier"], 0.5)
        advance(ops, _facts(now, mode="live", send_orders=True, pnl_from_golive_pct=-0.009), CFG, stage="post")
        self.assertEqual(ops["size_multiplier"], 0.5)

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True)
        advance(ops, _facts(datetime(2026, 10, 22, 23, 30, tzinfo=TEHRAN), mode="live", send_orders=True), CFG, stage="post")
        self.assertEqual(ops["state"], "LIVE_MANAGE")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True)
        advance(ops, _facts(now, mode="live", send_orders=True, entry_taker_fills=1), CFG, stage="post")
        self.assertEqual(ops["state"], "HALT")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, ev_override=True)
        advance(ops, _facts(now, mode="live", send_orders=True, zscore_n=3, fee_hurdle=0.002), CFG, stage="pre")
        self.assertEqual(ops["state"], "LIVE_ARMED")
        allowed, reason = entries_allowed(ops, _facts(now, zscore_n=3), CFG)
        self.assertFalse(allowed)
        self.assertEqual(reason, "zscore_warmup")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True)
        advance(ops, _facts(now, mode="live", send_orders=True, dropped=("TONTMN",)), CFG, stage="pre")
        self.assertIn("TONTMN", ops["dropped_symbols"])

    def test_fee_hurdle_above_the_frozen_prior_halts_unless_override_was_locked(self):
        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, ev_override=False)
        now = datetime(2026, 10, 22, 22, 0, tzinfo=TEHRAN)
        advance(ops, _facts(now, mode="live", send_orders=True, fee_hurdle=0.007, fee_maker_bps=25, fee_taker_bps=30), CFG, stage="pre")
        self.assertEqual(ops["state"], "HALT")

        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, ev_override=True)
        advance(ops, _facts(now, mode="live", send_orders=True, fee_hurdle=0.007, fee_maker_bps=25, fee_taker_bps=30), CFG, stage="pre")
        self.assertEqual(ops["state"], "LIVE_ARMED")

    def test_loosening_is_rejected_and_a_tighter_change_is_kept(self):
        ops = fresh_ops()
        ops.update(state="LIVE_ARMED", locked=True, size_multiplier=0.5, dropped_symbols=["TON"])
        now = datetime(2026, 10, 22, 22, 0, tzinfo=TEHRAN)
        raised = propose(ops, {"action": "A2_SIZE_MULT_DOWN", "new": 1.0, "actor": "operator:ali"}, CFG, now)
        self.assertEqual(raised["rejected_action"], "raise_size")
        self.assertEqual(ops["size_multiplier"], 0.5)
        added = propose(ops, {"action": "add_symbol", "symbol": "ETH", "actor": "operator:ali"}, CFG, now)
        self.assertEqual(added["rejected_action"], "add_symbol")
        self.assertEqual(ops["dropped_symbols"], ["TON"])
        lowered = propose(ops, {"action": "A2_SIZE_MULT_DOWN", "new": 0.0, "actor": "operator:ali", "note": "tighter"}, CFG, now)
        self.assertIsNone(lowered["rejected_action"])
        self.assertEqual(ops["size_multiplier"], 0.0)

        halted = fresh_ops()
        halted.update(state="HALT", locked=True)
        missing = propose(halted, {"action": "A5_ARM_ENTRIES", "actor": "operator:ali", "note": ""}, CFG, now)
        self.assertEqual(missing["rejected_action"], "arm_entries")
        self.assertEqual(halted["state"], "HALT")
        armed = propose(
            halted,
            {"action": "A5_ARM_ENTRIES", "actor": "operator:ali", "note": "execution bug, no strategy number changed"},
            CFG,
            now,
        )
        self.assertIsNone(armed["rejected_action"])
        self.assertEqual(halted["state"], "LIVE_ARMED")

    def test_frozen_prior_blocks_until_the_override_is_locked(self):
        class Row:
            def __init__(self):
                self.blockers = []
                self.expected_gross = 0.009
                self.round_trip_cost = 0.006
                self.signal = True
                self.score = 1.0

        blocked = Row()
        apply_frozen_cost_gate([blocked], CFG, locked_override=False)
        self.assertFalse(blocked.signal)
        self.assertEqual(blocked.expected_gross, 0.0045)
        self.assertTrue(any("does not clear" in item for item in blocked.blockers))
        kept = Row()
        apply_frozen_cost_gate([kept], CFG, locked_override=True)
        self.assertTrue(kept.signal)
        self.assertEqual(kept.expected_gross, 0.009)


class TehranMidnightTests(unittest.TestCase):
    def test_daily_boundary_is_tehran_midnight_not_a_whole_hour(self):
        before = datetime(2026, 10, 22, 20, 29, tzinfo=timezone.utc)
        after = datetime(2026, 10, 22, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(day_key(before), "2026-10-22")
        self.assertEqual(day_key(after), "2026-10-23")
        key, baseline = roll_day("2026-10-22", datetime(2026, 10, 22, 23, 59, tzinfo=TEHRAN), 90, 100)
        self.assertEqual((key, baseline), ("2026-10-22", 100))
        key, baseline = roll_day("2026-10-22", datetime(2026, 10, 23, 0, 0, tzinfo=TEHRAN), 90, 100)
        self.assertEqual((key, baseline), ("2026-10-23", 90))


class ReportTests(unittest.TestCase):
    def test_suggestion_is_pure_and_the_writer_rejects_a_looser_action(self):
        action, rule = suggest_action("C1", {"n_orders_rejected": 2, "zscore_ready": True, "n_quant_pass": 1})
        self.assertEqual((action, rule), ("A1_HALT_ENTRIES", "C1-R1"))
        action, rule = suggest_action("C1", {"pnl_from_golive_pct": -0.006, "zscore_ready": True, "n_quant_pass": 1})
        self.assertEqual((action, rule), ("A2_SIZE_MULT_DOWN", "C1-R5"))
        root = Path(tempfile.mkdtemp())
        cycle = {
            "ts_tehran": "2026-10-22T22:00:00+03:30",
            "ts_utc": "2026-10-22T18:30:00Z",
            "equity_tmn": 5_000_000,
            "n_orders_rejected": 1,
            "n_orders_submitted": 1,
            "n_quant_pass": 1,
            "loop_latency_ms": 1200,
            "zscore_n": 20,
            "zscore_ready": True,
            "fee_fetched_at": "2026-10-22T22:00:00+03:30",
            "ban_list_hash": "abc",
            "fee_maker_bps": 25,
            "fee_taker_bps": 30,
            "state": "HALT",
            "git_sha": "abc",
            "go_live_sha": "abc",
            "config_hash": "def",
            "llm_source": "fallback",
        }
        (root / "cycles.jsonl").write_text(json.dumps(cycle) + "\n", encoding="utf-8")
        (root / "decisions.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(SystemExit):
            main([
                "report", "--checkpoint", "C1", "--log-dir", str(root), "--out", str(root),
                "--operator-action", "A0_NO_CHANGE",
            ])
        text = (root / "cycle-review.md").read_text(encoding="utf-8")
        self.assertEqual(len(text.strip().splitlines()), 15)
        self.assertIn("Suggested action: A1_HALT_ENTRIES", text)
        saved = (root / "adjustments.jsonl").read_text(encoding="utf-8")
        self.assertIn("rejected_action", saved)
        self.assertIn("A0_NO_CHANGE", saved)
        draft = write_checkpoint(root, "C1", out_dir=root)
        self.assertTrue(draft.exists())
        review = json.loads((root / "reviews.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(review["suggested_action"], "A1_HALT_ENTRIES")
        self.assertIsNone(review["operator_action"])

    def test_replay_reads_saved_responses_without_a_client(self):
        root = Path(tempfile.mkdtemp())
        response = '{"action_id": "hold", "rationale": "nothing to do"}'
        row = {"prompt": "menu", "response": response, "prompt_hash": "p", "response_hash": ""}
        (root / "replay.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        parsed = replay_offline(root)
        self.assertEqual(parsed[0]["action_id"], "hold")
        self.assertTrue(parsed[0]["parsed"])
        self.assertEqual(parsed[0]["source"], "replay")

    def test_secrets_are_presence_only_and_client_ids_do_not_collide(self):
        os.environ["WALLEX_API_KEY"] = "supersecretvalue"
        try:
            row = stamp({"note": "leaked supersecretvalue"})
        finally:
            os.environ.pop("WALLEX_API_KEY", None)
        self.assertNotIn("supersecretvalue", json.dumps(row))
        self.assertIn("***", row["note"])
        first = {"client_seq": 1}
        left = next_client_id(first)
        right = next_client_id(first)
        self.assertNotEqual(left, right)
        self.assertTrue(left.startswith("SC"))
        restarted = {"client_seq": 1}
        other = next_client_id(restarted)
        self.assertNotEqual(other[:10], left[:10])

    def test_llm_model_is_warned_and_does_not_pin_the_tier_table(self):
        os.environ["LLM_MODEL"] = "openai/gpt-6-luna"
        try:
            warning = llm_override_warning()
            spec = choose_tier("stakes", {}, 0, 0)
        finally:
            os.environ.pop("LLM_MODEL", None)
        self.assertIn("does not override the tier table", warning)
        self.assertIn("gpt-6-sol", spec["model"])
        self.assertIsNone(llm_override_warning())


if __name__ == "__main__":
    unittest.main()
