"""Parse the model's JSON decision. Garbage never raises into the loop."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


ACTIONS = {"BUY", "SELL", "CLOSE", "HOLD"}


@dataclass
class ParsedDecision:
    action: str
    symbol: str
    size_fraction: float | None
    confidence: float
    veto: bool
    rationale: str


@dataclass
class AgentDecision:
    source: str
    veto_all: bool
    regime: str
    comment: str
    decisions: list[ParsedDecision] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)

    def as_log(self) -> dict:
        return {
            "source": self.source,
            "veto_all": self.veto_all,
            "regime": self.regime,
            "comment": self.comment,
            "error": self.error,
            "usage": self.usage,
            "decisions": [vars(item) for item in self.decisions],
        }


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("model JSON was not an object")
    return payload


def parse_agent_output(text: str) -> AgentDecision:
    payload = extract_json(text)
    decisions: list[ParsedDecision] = []
    rows = payload.get("decisions") or []
    if not isinstance(rows, list):
        raise ValueError("decisions must be a list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "HOLD").upper()
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action}")
        symbol = str(row.get("symbol") or "").upper()
        size = row.get("size_fraction")
        size_value = float(size) if size is not None and size != "" else None
        confidence = float(row.get("confidence") if row.get("confidence") is not None else 0)
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        if size_value is not None and (size_value < 0 or size_value > 1):
            raise ValueError("size_fraction must be between 0 and 1")
        decisions.append(
            ParsedDecision(
                action=action,
                symbol=symbol,
                size_fraction=size_value,
                confidence=confidence,
                veto=bool(row.get("veto")),
                rationale=str(row.get("rationale") or "").strip(),
            )
        )
    return AgentDecision(
        source="llm",
        veto_all=bool(payload.get("veto_all")),
        regime=str(payload.get("regime") or ""),
        comment=str(payload.get("comment") or ""),
        decisions=decisions,
        raw=text,
    )
