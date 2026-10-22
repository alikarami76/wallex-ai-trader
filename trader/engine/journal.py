"""Decision JSONL, trades CSV, and equity snapshots. Every write is flushed."""

from __future__ import annotations

import csv
import json
from pathlib import Path


TRADE_FIELDS = [
    "timestamp",
    "mode",
    "symbol",
    "side",
    "price",
    "quantity",
    "notional_tmn",
    "fee_tmn",
    "fee_rate",
    "pnl_tmn",
    "reason",
    "order_type",
    "client_id",
]

EQUITY_FIELDS = [
    "timestamp",
    "equity_tmn",
    "cash_tmn",
    "exposure",
    "drawdown",
    "peak_tmn",
]


class Journal:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.decision_path = directory / "decisions.jsonl"
        self.trade_path = directory / "trades.csv"
        self.equity_path = directory / "equity.csv"
        self._decisions = self.decision_path.open("a", encoding="utf-8")
        self._ensure_csv(self.trade_path, TRADE_FIELDS)
        self._ensure_csv(self.equity_path, EQUITY_FIELDS)

    def decision(self, payload: dict) -> None:
        self._decisions.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._decisions.flush()

    def trade(self, row: dict) -> None:
        self._append(self.trade_path, TRADE_FIELDS, row)

    def equity(self, row: dict) -> None:
        self._append(self.equity_path, EQUITY_FIELDS, row)

    def close(self) -> None:
        self._decisions.close()

    @staticmethod
    def _ensure_csv(path: Path, fields: list[str]) -> None:
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()

    @staticmethod
    def _append(path: Path, fields: list[str], row: dict) -> None:
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writerow({key: row.get(key, "") for key in fields})
