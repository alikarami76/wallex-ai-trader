"""LLM decision layer.

One BoxAPI chat completion per cycle, with every symbol in that prompt.
Temperature is omitted unless the config asks for it (some models reject it).
The default model is gpt-6-luna. Spend uses usage.cost when BoxAPI sends it.
A disk ledger caps calls per hour, calls for the whole run, and estimated
spend. Cache hits, cap hits, timeouts, and bad JSON all use the deterministic
fallback so the bot keeps trading inside the $3 budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from trader.strategy.parser import AgentDecision, ParsedDecision, parse_agent_output
from trader.strategy.signals import Candidate, SymbolFeatures
from trader.wallex.mcp import WallexMcp

log = logging.getLogger("trader.agent")

SYSTEM_PROMPT = """You are the regime filter of a Wallex TMN spot trading system, not the trade trigger.
The quantitative layer already proposed candidates from a slow uptrend plus a 4-hour pullback.
If you abstain (no BUY, veto_all false), the engine may take the top candidate. You cannot invent a symbol.

Rules:
- Reply with one JSON object and nothing else.
- Set regime to hostile and veto_all to true when the book imbalance, the spread, or the slow trend conflicts with the setup. That blocks every new entry.
- A BUY only confirms a candidate and may cut size_fraction. It never raises size. Use null to leave the quant size.
- HOLD means abstain, not a veto. Use veto true on a symbol to reject that name only.
- confidence is from 0 to 1. Below 0.55 a BUY confirmation is dropped.
- You may CLOSE an existing position only when close is below ema_slow and roc_24h has turned negative.
- Cite the numbers you used (slow return, 24h return, 4h pullback, half-spread, imbalance, cost, fear_greed, news_score) in rationale.
- fear_greed is 0-100. A missing sentiment field is not a reason to veto. Extreme greed (80 or above) or a sharply negative news_score is a reason to veto or cut size.
- Never tell the engine to ignore a stop, the daily max-loss, or the liquidation window.

Schema:
{"veto_all": false, "regime": "trend|chop|hostile", "comment": "short", "decisions": [{"action": "BUY|SELL|CLOSE|HOLD", "symbol": "BTCTMN", "size_fraction": null, "confidence": 0.7, "veto": false, "rationale": "..."}]}
"""


def llm_settings(cfg: dict) -> dict:
    raw = dict(cfg.get("llm") or {})
    raw.setdefault("timeout_seconds", 20)
    raw.setdefault("send_temperature", False)
    raw.setdefault("temperature", 0.1)
    raw.setdefault("send_response_format", False)
    raw.setdefault("max_llm_calls_per_hour", 2)
    raw.setdefault("max_llm_calls_total", 24)
    raw.setdefault("max_spend_usd", 2.5)
    raw.setdefault("assumed_input_usd_per_million", 0.50)
    raw.setdefault("assumed_output_usd_per_million", 2.00)
    raw.setdefault("assumed_usd_per_call_if_usage_missing", 0.08)
    raw.setdefault("cache_seconds", 3600)
    raw.setdefault("usage_path", "data/llm_usage.json")
    raw.setdefault("enabled", True)
    raw.setdefault("model", "gpt-6-luna")
    return raw


def request_body(model: str, messages: list[dict], settings: dict) -> dict:
    """Body for POST /chat/completions. Temperature is off unless configured."""
    body: dict = {"model": model, "messages": messages}
    if settings.get("send_temperature"):
        body["temperature"] = float(settings.get("temperature", 0.1))
    if settings.get("send_response_format"):
        body["response_format"] = {"type": "json_object"}
    return body


class LlmBudget:
    """Persisted call counter and response cache. Survives a process restart."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.path = Path(settings["usage_path"])
        self.calls: list[dict] = []
        self.cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.calls = list(payload.get("calls") or [])
        self.cache = dict(payload.get("cache") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"calls": self.calls[-200:], "cache": self.cache}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def allow(self, now: float) -> tuple[bool, str]:
        recent = [row for row in self.calls if now - float(row.get("ts") or 0) <= 3600]
        if len(recent) >= int(self.settings["max_llm_calls_per_hour"]):
            return False, "hourly LLM call cap"
        if len(self.calls) >= int(self.settings["max_llm_calls_total"]):
            return False, "total LLM call cap"
        spent = sum(float(row.get("usd") or 0) for row in self.calls)
        if spent >= float(self.settings["max_spend_usd"]):
            return False, f"estimated spend ${spent:.2f} reached the cap"
        return True, ""

    def cached(self, key: str, now: float) -> str | None:
        row = self.cache.get(key)
        if not row:
            return None
        if now - float(row.get("ts") or 0) > float(self.settings["cache_seconds"]):
            return None
        return row.get("content")

    def remember(self, key: str, content: str, usage: dict, now: float) -> None:
        self.calls.append({"ts": now, **usage})
        self.cache[key] = {"ts": now, "content": content}
        if len(self.cache) > 48:
            oldest = sorted(self.cache, key=lambda item: self.cache[item].get("ts") or 0)[: len(self.cache) - 48]
            for item in oldest:
                self.cache.pop(item, None)
        self.save()

    def spent_usd(self) -> float:
        return sum(float(row.get("usd") or 0) for row in self.calls)


class LlmClient:
    def __init__(self, settings: dict, post=None):
        self.settings = settings
        self.timeout = float(settings["timeout_seconds"])
        self._post = post or self._http

    def model_name(self) -> str:
        chosen = (os.getenv("LLM_MODEL") or "").strip()
        return chosen or str(self.settings.get("model") or "gpt-6-luna")

    def configured(self) -> bool:
        return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_API_KEY") and self.model_name())

    def complete(self, system: str, user: str) -> tuple[str | None, dict]:
        """One request. Returns (content, usage). Never raises."""
        if not self.configured():
            return None, {"skipped": "llm env not set"}
        model = self.model_name()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body = request_body(model, messages, self.settings)
        base = os.environ["LLM_BASE_URL"].rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        }
        try:
            content, usage = self._post(url, headers, body)
        except Exception as exc:
            log.warning("LLM request failed: %s", exc)
            return None, {"error": str(exc), "model": model}
        usage = _price(usage, self.settings, model)
        log.info(
            "llm call model=%s prompt_tokens=%s completion_tokens=%s usd=%.8f source=%s",
            model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("usd") or 0,
            usage.get("cost_source"),
        )
        return content, usage

    def _http(self, url: str, headers: dict, body: dict) -> tuple[str, dict]:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        choice = payload["choices"][0]["message"]["content"]
        raw_usage = payload.get("usage") or {}
        return choice, {
            "prompt_tokens": int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0),
            "completion_tokens": int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0),
            "cost": raw_usage.get("cost"),
            "model": body.get("model"),
        }


def _cycle_key(features, candidates, positions, now: float) -> str:
    """One cache entry per hour and setup, so book jitter does not buy another call."""
    payload = {
        "hour": int(now // 3600),
        "candidates": [item.symbol for item in candidates],
        "bars": [(row.symbol, row.bar_time, row.signal) for row in features],
        "positions": [row.get("symbol") for row in positions],
    }
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:24]


def _price(usage: dict, settings: dict, model: str) -> dict:
    """Prefer BoxAPI usage.cost. Fall back to the token estimate only when it is absent."""
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    if cost is not None and cost != "":
        usd = float(cost)
        source = "usage.cost"
    elif prompt or completion:
        usd = (
            prompt / 1_000_000 * float(settings["assumed_input_usd_per_million"])
            + completion / 1_000_000 * float(settings["assumed_output_usd_per_million"])
        )
        source = "estimated_tokens"
    else:
        usd = float(settings["assumed_usd_per_call_if_usage_missing"])
        source = "flat_fallback"
    return {
        "model": usage.get("model") or model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "usd": round(usd, 8),
        "cost_source": source,
    }


def fallback_decision(candidates: list[Candidate]) -> AgentDecision:
    if not candidates:
        return AgentDecision(
            source="fallback",
            veto_all=False,
            regime="no_setup",
            comment="No symbol cleared the trend, pullback, and fee gates.",
            decisions=[],
        )
    best = candidates[0]
    confidence = round(min(0.85, 0.55 + 0.30 * best.score), 2)
    rationale = (
        "DETERMINISTIC_FALLBACK: "
        f"{best.symbol} is the top uptrend-pullback candidate. "
        f"24h return {best.roc_trend:.2%}, 4h pullback {best.roc_pullback:.2%}, "
        f"expected gross {best.expected_gross:.2%} versus round-trip cost {best.round_trip_cost:.2%}, "
        f"score {best.score:.2f}. One entry only."
    )
    return AgentDecision(
        source="fallback",
        veto_all=False,
        regime="trend",
        comment=rationale,
        decisions=[
            ParsedDecision(
                action="BUY",
                symbol=best.symbol,
                size_fraction=best.size_fraction,
                confidence=confidence,
                veto=False,
                rationale=rationale,
            )
        ],
    )


class TradingAgent:
    def __init__(self, cfg: dict, mcp: WallexMcp | None = None, post=None):
        self.cfg = cfg
        self.settings = llm_settings(cfg)
        self.llm = LlmClient(self.settings, post=post)
        self.budget = LlmBudget(self.settings)
        self.mcp = mcp
        self._mcp_note = ""
        self._mcp_note_at = 0.0

    def decide(
        self,
        features: list[SymbolFeatures],
        candidates: list[Candidate],
        positions: list[dict],
        risk_state: dict,
        now: float,
        use_llm: bool = True,
    ) -> AgentDecision:
        if not use_llm or not self.settings.get("enabled", True):
            decision = fallback_decision(candidates)
            decision.comment = "LLM disabled for this mode. " + decision.comment
            return decision
        if not candidates and not positions:
            decision = fallback_decision(candidates)
            decision.usage = {"skipped": "nothing to rank"}
            return decision
        self._maybe_refresh_mcp(candidates, now)
        user = self._prompt(features, candidates, positions, risk_state)
        key = _cycle_key(features, candidates, positions, now)
        cached = self.budget.cached(key, now)
        if cached:
            decision = self._parse_or_fallback(cached, candidates)
            decision.source = "llm_cache"
            decision.usage = {"cached": True, "spent_usd": round(self.budget.spent_usd(), 6)}
            return decision
        allowed, reason = self.budget.allow(now)
        if not allowed:
            log.info("LLM skipped: %s", reason)
            decision = fallback_decision(candidates)
            decision.comment = f"LLM cap: {reason}. " + decision.comment
            decision.usage = {"skipped": reason, "spent_usd": round(self.budget.spent_usd(), 6)}
            return decision
        raw, usage = self.llm.complete(SYSTEM_PROMPT, user)
        if not raw:
            decision = fallback_decision(candidates)
            decision.usage = usage
            decision.error = str(usage.get("error") or usage.get("skipped") or "")
            return decision
        self.budget.remember(key, raw, {"ts": now, **usage}, now)
        decision = self._parse_or_fallback(raw, candidates)
        decision.usage = usage
        return decision

    def _parse_or_fallback(self, raw: str, candidates: list[Candidate]) -> AgentDecision:
        try:
            parsed = parse_agent_output(raw)
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            log.warning("LLM output rejected (%s); using fallback", exc)
            decision = fallback_decision(candidates)
            decision.error = str(exc)
            decision.raw = raw[:2000]
            return decision
        parsed.raw = raw[:2000]
        if parsed.veto_all:
            parsed.decisions = [item for item in parsed.decisions if item.action in {"SELL", "CLOSE", "HOLD"}]
        return parsed

    def _maybe_refresh_mcp(self, candidates: list[Candidate], now: float) -> None:
        mcp_cfg = self.cfg.get("mcp") or {}
        if not mcp_cfg.get("enabled") or self.mcp is None or not candidates:
            return
        if now - self._mcp_note_at < float(mcp_cfg.get("refresh_seconds", 600)):
            return
        top = candidates[0]
        start = int(now) - 6 * 3600
        text = self.mcp.call_tool(
            "wallex_basic_candle_history",
            {"symbol": top.symbol, "resolution": "60", "from": start, "to": int(now)},
        )
        self._mcp_note_at = now
        if not text:
            self._mcp_note = ""
            return
        self._mcp_note = f"Wallex MCP wallex_basic_candle_history({top.symbol}) returned: {text[:500]}"

    def _prompt(self, features, candidates, positions, risk_state) -> str:
        payload = {
            "risk": risk_state,
            "positions": positions,
            "candidates": [c.compact() for c in candidates],
            "features": [row.compact() for row in features],
            "mcp_context": self._mcp_note,
            "instructions": (
                "Pick at most one BUY from candidates, or veto. "
                "Do not select a symbol that is absent from candidates."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)
