"""LLM decision layer.

BoxAPI is OpenAI-compatible. One chat completion per decision, routed by job:

- routine (tier0): headline sentiment on a flat cycle, gpt-6-luna at low effort
- signal (tier1): review an open position when nothing new passed the cost gate
- trade (tier2): open, close, or size, and only when a quant candidate exists

Temperature is omitted unless the config asks for it. reasoning_effort is sent
and stripped once if the model returns HTTP 400. Spend uses usage.cost.
A disk ledger caps calls per hour, calls for the run, spend per Tehran day,
and total spend. Near the cap the router steps down to a cheaper tier.
Cache hits, cap hits, timeouts, and bad JSON use the deterministic fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from trader.strategy.parser import AgentDecision, ParsedDecision, extract_json, parse_agent_output
from trader.strategy.router import choose_tier
from trader.strategy.signals import Candidate, SymbolFeatures
from trader.timeutil import TEHRAN
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
    raw.setdefault("max_llm_calls_per_hour", 8)
    raw.setdefault("max_llm_calls_total", 160)
    raw.setdefault("max_spend_usd", 2.5)
    raw.setdefault("max_spend_usd_per_day", 1.25)
    raw.setdefault("downgrade_spend_usd", 2.0)
    raw.setdefault("downgrade_daily_usd", 1.0)
    raw.setdefault("assumed_input_usd_per_million", 0.50)
    raw.setdefault("assumed_output_usd_per_million", 2.00)
    raw.setdefault("assumed_usd_per_call_if_usage_missing", 0.08)
    raw.setdefault("cache_seconds", 3600)
    raw.setdefault("sentiment_cache_seconds", 900)
    raw.setdefault("usage_path", "data/llm_usage.json")
    raw.setdefault("min_interval_seconds", 1.0)
    raw.setdefault("max_retries", 3)
    raw.setdefault("retry_backoff_seconds", 0.5)
    raw.setdefault("routine_timeout_seconds", 8)
    raw.setdefault("enabled", True)
    raw.setdefault("model", "gpt-6-luna")
    return raw


def request_body(model: str, messages: list[dict], settings: dict, reasoning_effort: str | None = None) -> dict:
    """Body for POST /chat/completions. Temperature is off unless configured."""
    body: dict = {"model": model, "messages": messages}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if settings.get("send_temperature"):
        body["temperature"] = float(settings.get("temperature", 0.1))
    if settings.get("send_response_format"):
        body["response_format"] = {"type": "json_object"}
    return body


class LlmHTTPError(Exception):
    def __init__(self, code: int, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail


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
        spent = self.spent_usd()
        if spent >= float(self.settings["max_spend_usd"]):
            return False, f"estimated spend ${spent:.2f} reached the cap"
        today = self.spent_today(now)
        if today >= float(self.settings["max_spend_usd_per_day"]):
            return False, f"daily spend ${today:.2f} reached the cap"
        return True, ""

    def peek(self, key: str, now: float, ttl: float | None = None) -> dict | None:
        row = self.cache.get(key)
        if not row:
            return None
        limit = float(self.settings["cache_seconds"] if ttl is None else ttl)
        if now - float(row.get("ts") or 0) > limit:
            return None
        return row

    def cached(self, key: str, now: float, ttl: float | None = None) -> str | None:
        row = self.peek(key, now, ttl)
        if not row:
            return None
        return row.get("content")

    def remember(self, key: str, content: str, usage: dict, now: float) -> None:
        self.calls.append({"ts": now, **usage})
        self.cache[key] = {
            "ts": now,
            "content": content,
            "model": usage.get("model"),
            "tier": usage.get("tier"),
            "reasoning_effort": usage.get("reasoning_effort"),
        }
        if len(self.cache) > 48:
            oldest = sorted(self.cache, key=lambda item: self.cache[item].get("ts") or 0)[: len(self.cache) - 48]
            for item in oldest:
                self.cache.pop(item, None)
        self.save()

    def spent_usd(self) -> float:
        return sum(float(row.get("usd") or 0) for row in self.calls)

    def spent_today(self, now: float) -> float:
        day = _tehran_day(now)
        return sum(float(row.get("usd") or 0) for row in self.calls if _tehran_day(row.get("ts") or 0) == day)


class LlmClient:
    def __init__(self, settings: dict, post=None, sleep=None):
        self.settings = settings
        self.timeout = float(settings["timeout_seconds"])
        self._post = post or self._http
        self._sleep = sleep or time.sleep
        self._next_at = 0.0

    def model_name(self) -> str:
        chosen = (os.getenv("LLM_MODEL") or "").strip()
        return chosen or str(self.settings.get("model") or "gpt-6-luna")

    def configured(self) -> bool:
        return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_API_KEY") and self.model_name())

    def complete(self, system: str, user: str, spec: dict | None = None) -> tuple[str | None, dict]:
        """One routed request. Returns (content, usage). Never raises."""
        spec = dict(spec or {})
        model = str(spec.get("model") or self.model_name())
        tier = str(spec.get("tier") or "")
        effort = spec.get("reasoning_effort") or None
        if not self.configured():
            return None, {"skipped": "llm env not set", "model": model, "tier": tier}
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body = request_body(model, messages, self.settings, effort if isinstance(effort, str) else None)
        base = os.environ["LLM_BASE_URL"].rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        }
        timeout = float(spec.get("timeout") or self.timeout)
        content, usage, error = self._send(url, headers, body, timeout)
        if content is None:
            log.warning("LLM request failed: %s", error)
            return None, {
                "error": error,
                "model": model,
                "tier": tier,
                "requested_tier": spec.get("requested_tier") or tier,
                "downgraded": bool(spec.get("downgraded")),
                "reasoning_effort": body.get("reasoning_effort", "omitted"),
            }
        usage = _price(usage, self.settings, model)
        usage["tier"] = tier
        usage["requested_tier"] = spec.get("requested_tier") or tier
        usage["downgraded"] = bool(spec.get("downgraded"))
        usage["reasoning_effort"] = body.get("reasoning_effort", "omitted")
        log.info(
            "llm call tier=%s model=%s effort=%s prompt_tokens=%s completion_tokens=%s usd=%.8f source=%s",
            tier or "-",
            usage.get("model"),
            usage.get("reasoning_effort"),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("usd") or 0,
            usage.get("cost_source"),
        )
        return content, usage

    def _send(self, url: str, headers: dict, body: dict, timeout: float) -> tuple[str | None, dict, str]:
        """Pace, then retry 429/5xx. A 400 that names reasoning_effort is retried once without it."""
        retries = max(1, int(self.settings.get("max_retries", 3)))
        backoff = float(self.settings.get("retry_backoff_seconds", 0.5))
        stripped = False
        last = "llm request failed"
        attempt = 0
        while attempt < retries:
            attempt += 1
            self._pace()
            try:
                try:
                    content, usage = self._post(url, headers, body, timeout)
                except TypeError:
                    content, usage = self._post(url, headers, body)
                return content, usage, ""
            except LlmHTTPError as exc:
                last = str(exc)
                if exc.code == 400 and not stripped and "reasoning_effort" in body:
                    body.pop("reasoning_effort", None)
                    stripped = True
                    attempt -= 1
                    log.info("LLM rejected reasoning_effort; retrying without it")
                    continue
                if (exc.code == 429 or exc.code >= 500) and attempt < retries:
                    self._sleep(backoff * (2 ** (attempt - 1)))
                    continue
                break
            except Exception as exc:
                last = str(exc)
                if attempt < retries:
                    self._sleep(backoff * (2 ** (attempt - 1)))
                    continue
                break
        return None, {}, last

    def _pace(self) -> None:
        gap = float(self.settings.get("min_interval_seconds") or 0)
        if gap <= 0:
            return
        wait = self._next_at - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._next_at = time.monotonic() + gap

    def _http(self, url: str, headers: dict, body: dict, timeout: float | None = None) -> tuple[str, dict]:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise LlmHTTPError(exc.code, detail) from exc
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


def _tehran_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts or 0), tz=TEHRAN).strftime("%Y-%m-%d")


SENTIMENT_PROMPT = """You score crypto headlines for a TMN spot bot.
Reply with one JSON object and nothing else:
{"scores": {"BTCTMN": 0.0}}
Each score is from -1 (bearish) to 1 (bullish). Use only the symbols in the payload. No headline evidence is 0.
"""


def parse_sentiment_scores(text: str) -> dict[str, float]:
    try:
        payload = extract_json(text)
    except (ValueError, json.JSONDecodeError, TypeError):
        return {}
    rows = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    scores: dict[str, float] = {}
    for key, value in rows.items():
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        scores[str(key).upper()] = round(max(-1.0, min(1.0, score)), 4)
    return scores


def _apply_news(mood, scores: dict[str, float]) -> int:
    headlines = getattr(mood, "headlines", None) or {}
    applied = 0
    for symbol in headlines:
        if symbol not in scores:
            continue
        previous = mood.news_score.get(symbol)
        score = scores[symbol]
        mood.news_score[symbol] = score
        if previous is not None:
            mood.news_change[symbol] = round(float(score) - float(previous), 4)
        applied += 1
    if applied and "llm_tier0" not in mood.sources_ok:
        mood.sources_ok.append("llm_tier0")
    return applied


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
    def __init__(self, cfg: dict, mcp: WallexMcp | None = None, post=None, sleep=None):
        self.cfg = cfg
        self.settings = llm_settings(cfg)
        self.llm = LlmClient(self.settings, post=post, sleep=sleep)
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
            decision.usage = {"skipped": "nothing to rank", "tier": "routine"}
            return decision
        task = "trade" if candidates else "signal"
        spec = choose_tier(task, self.settings, self.budget.spent_usd(), self.budget.spent_today(now))
        self._maybe_refresh_mcp(candidates, now)
        user = self._prompt(features, candidates, positions, risk_state)
        key = _cycle_key(features, candidates, positions, now)
        hit = self.budget.peek(key, now)
        if hit and hit.get("content"):
            decision = self._parse_or_fallback(hit["content"], candidates)
            decision.source = "llm_cache"
            decision.usage = {
                "cached": True,
                "model": hit.get("model") or spec["model"],
                "tier": hit.get("tier") or spec["tier"],
                "reasoning_effort": hit.get("reasoning_effort"),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "usd": 0.0,
                "cost_source": "cache",
                "spent_usd": round(self.budget.spent_usd(), 6),
            }
            return decision
        allowed, reason = self.budget.allow(now)
        if not allowed:
            log.info("LLM skipped: %s", reason)
            decision = fallback_decision(candidates)
            decision.comment = f"LLM cap: {reason}. " + decision.comment
            decision.usage = {
                "skipped": reason,
                "tier": spec["tier"],
                "model": spec["model"],
                "downgraded": spec["downgraded"],
                "spent_usd": round(self.budget.spent_usd(), 6),
            }
            return decision
        raw, usage = self.llm.complete(SYSTEM_PROMPT, user, spec)
        if not raw:
            decision = fallback_decision(candidates)
            decision.usage = usage
            decision.error = str(usage.get("error") or usage.get("skipped") or "")
            return decision
        self.budget.remember(key, raw, usage, now)
        decision = self._parse_or_fallback(raw, candidates)
        decision.usage = usage
        return decision

    def apply_sentiment(self, mood, now: float, allow_network: bool) -> list[dict]:
        """Tier0 headline score. A cache hit is free. Network runs only when allowed.

        The caller allows the network on flat cycles so a trade decision is not
        delayed by a second model call. Lexicon scores stay in place on failure.
        """
        headlines = getattr(mood, "headlines", None) or {}
        packed = {
            symbol: [str(title)[:160] for title in titles[:3]]
            for symbol, titles in headlines.items()
            if titles
        }
        if not packed or not self.settings.get("enabled", True):
            return []
        key = "sent:" + hashlib.sha256(
            json.dumps(packed, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        ttl = float(self.settings["sentiment_cache_seconds"])
        hit = self.budget.peek(key, now, ttl)
        if hit and hit.get("content"):
            _apply_news(mood, parse_sentiment_scores(hit["content"]))
            return [{
                "cached": True,
                "tier": hit.get("tier") or "routine",
                "model": hit.get("model"),
                "reasoning_effort": hit.get("reasoning_effort") or "low",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "usd": 0.0,
                "cost_source": "cache",
                "spent_usd": round(self.budget.spent_usd(), 6),
            }]
        if not allow_network or not self.llm.configured():
            return []
        allowed, reason = self.budget.allow(now)
        if not allowed:
            return [{"skipped": reason, "tier": "routine", "spent_usd": round(self.budget.spent_usd(), 6)}]
        spec = choose_tier("routine", self.settings, self.budget.spent_usd(), self.budget.spent_today(now))
        spec["timeout"] = float(self.settings["routine_timeout_seconds"])
        raw, usage = self.llm.complete(
            SENTIMENT_PROMPT,
            json.dumps({"headlines": packed}, ensure_ascii=False),
            spec,
        )
        if not raw:
            return [usage]
        self.budget.remember(key, raw, usage, now)
        _apply_news(mood, parse_sentiment_scores(raw))
        return [usage]

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
