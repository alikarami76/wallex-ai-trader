"""LLM decision layer.

BoxAPI is OpenAI-compatible. One chat completion per decision, routed by job:

- routine (tier0): headline sentiment on a flat cycle, openai/gpt-6-luna at low effort
- signal (tier1): review an open position when nothing new passed the cost gate
- trade (tier2): the default decision, openai/gpt-6-luna, when a buy is legal
- stakes (tier3): openai/gpt-6-sol, and only when that buy is high-stakes

Temperature is omitted unless the config asks for it. A completion cap is sent
as max_completion_tokens, never max_tokens. reasoning_effort is sent and
stripped once if the model returns HTTP 400. Spend uses usage.cost. A missing
cost is estimated from the 2026-10-22 list price plus 15%, with reasoning
tokens billed as output. A disk ledger caps calls per hour, calls for the run,
spend per Tehran day, and total spend. Near the cap the router steps down to
a cheaper tier, and at the cap it stops calling. The running total is logged
against the $3 competition budget. /key and /credits are a shared upstream
account, so the ledger is the balance. Cache hits, cap hits, timeouts, and
transport failures use the deterministic fallback. Once the ledger reaches
$2.25 the model is not called. Invalid JSON is a veto and does not buy.

Shadow and live also pass a menu of actions the risk layer already accepted.
The model then returns one id. Invalid menu output is HOLD and does not fall
through to the quant candidate. A missing key or a failed request still uses
the deterministic fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from trader.strategy.menu import MenuAction, hold_action, parse_menu_choice
from trader.strategy.parser import AgentDecision, ParsedDecision, extract_json, parse_agent_output
from trader.strategy.router import canonical_model, choose_tier, high_stakes, list_prices, quoted_call_usd
from trader.strategy.signals import Candidate, SymbolFeatures
from trader.timeutil import TEHRAN
from trader.wallex.mcp import WallexMcp

log = logging.getLogger("trader.agent")

# Below the $2.50 hard cap. At this ledger total the model is not called.
SPEND_GUARD_USD = 2.25

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
- Cite the numbers you used (slow return, 24h return, 4h pullback, half-spread, imbalance, cost, fear_greed, news_score, depth_100_tmn, liquidity_scale) in rationale.
- liquidity_scale below 1 means the book or the volume is deteriorating. That is a reason to veto or leave size alone. Do not raise size to offset it.
- fear_greed is 0-100. A missing sentiment field is not a reason to veto. Extreme greed (80 or above) or a sharply negative news_score is a reason to veto or cut size.
- Never tell the engine to ignore a stop, the daily max-loss, or the liquidation window.

Schema:
{"veto_all": false, "regime": "trend|chop|hostile", "comment": "short", "decisions": [{"action": "BUY|SELL|CLOSE|HOLD", "symbol": "BTCTMN", "size_fraction": null, "confidence": 0.7, "veto": false, "rationale": "..."}]}
"""

MENU_PROMPT = """You choose one action from a menu the risk layer already validated.
Reply with one JSON object and nothing else:
{"action_id": "hold", "rationale": "one short sentence citing a number in the state"}
Use an id from the menu. Leave size, price, and symbol as written. If unsure, pick hold.
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
    raw.setdefault("assumed_input_usd_per_million", 0.10)
    raw.setdefault("assumed_output_usd_per_million", 0.50)
    raw.setdefault("price_markup", 1.15)
    raw.setdefault("assumed_usd_per_call_if_usage_missing", 0.0006325)
    raw.setdefault("competition_budget_usd", 3.0)
    raw.setdefault("high_stakes_exposure", 0.0)
    raw.setdefault("high_stakes_daily_drawdown", 0.0075)
    raw.setdefault("typical_prompt_tokens", 3000)
    raw.setdefault("typical_completion_tokens", 500)
    raw.setdefault("cache_seconds", 3600)
    raw.setdefault("sentiment_cache_seconds", 900)
    raw.setdefault("usage_path", "data/llm_usage.json")
    raw.setdefault("min_interval_seconds", 1.0)
    raw.setdefault("max_retries", 3)
    raw.setdefault("retry_backoff_seconds", 0.5)
    raw.setdefault("routine_timeout_seconds", 8)
    raw.setdefault("enabled", True)
    raw.setdefault("model", "openai/gpt-6-luna")
    return raw


def request_body(model: str, messages: list[dict], settings: dict, reasoning_effort: str | None = None) -> dict:
    """Body for POST /chat/completions. Temperature is off unless configured.

    gpt-6-luna rejects max_tokens. A cap, including one configured under the
    old name, is sent as max_completion_tokens.
    """
    body: dict = {"model": model, "messages": messages}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    limit = settings.get("max_completion_tokens")
    if not limit and settings.get("max_tokens"):
        limit = settings.get("max_tokens")
    if limit:
        body["max_completion_tokens"] = int(limit)
    if settings.get("send_temperature"):
        body["temperature"] = float(settings.get("temperature", 0.1))
    if settings.get("send_response_format"):
        body["response_format"] = {"type": "json_object"}
    return body


def adapt_rejected_body(body: dict, detail: str) -> str:
    """Rewrite one field a model rejected. Empty string means nothing to change."""
    text = (detail or "").lower()
    if "reasoning_effort" in body and "reasoning_effort" in text:
        body.pop("reasoning_effort", None)
        return "reasoning_effort"
    if "max_tokens" in body and "max_tokens" in text:
        body["max_completion_tokens"] = body.pop("max_tokens")
        return "max_completion_tokens"
    return ""


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
        spent = round(self.spent_usd(), 8)
        budget = float(self.settings.get("competition_budget_usd", 3.0))
        cap = float(self.settings.get("max_spend_usd", 2.5))
        usage["spent_usd"] = spent
        usage["budget_usd"] = budget
        usage["cap_usd"] = cap
        self.calls[-1]["spent_usd"] = spent
        log.info(
            "llm call model=%s tier=%s effort=%s prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s usd=%.8f spent=%.8f/%.2f cap=%.2f source=%s",
            usage.get("model"),
            usage.get("tier") or "-",
            usage.get("reasoning_effort") or "-",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("reasoning_tokens") or 0,
            float(usage.get("usd") or 0),
            spent,
            budget,
            cap,
            usage.get("cost_source") or "-",
        )
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
        """Fallback model when a call has no tier spec. Not a pin for every tier."""
        cheap = (os.getenv("LLM_MODEL_CHEAP") or "").strip()
        return canonical_model(cheap or str(self.settings.get("model") or "openai/gpt-6-luna"))

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
        content, usage, error = self._complete_within(url, headers, body, timeout)
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
        return content, usage

    def _complete_within(self, url: str, headers: dict, body: dict, timeout: float) -> tuple[str | None, dict, str]:
        """Run one send on a daemon thread and stop waiting when the timeout fires.

        The socket timeout inside _send is not enough: a mock or a stalled
        peer can ignore it, and the retry loop would keep the stop path waiting.
        """
        holder: dict = {}

        def _call() -> None:
            try:
                holder["result"] = self._send(url, headers, body, timeout)
            except Exception as exc:
                holder["result"] = (None, {}, str(exc))

        worker = threading.Thread(target=_call, name="llm-complete", daemon=True)
        worker.start()
        worker.join(timeout if timeout > 0 else 0)
        if worker.is_alive() or "result" not in holder:
            return None, {}, "hard timeout"
        return holder["result"]

    def _send(self, url: str, headers: dict, body: dict, timeout: float) -> tuple[str | None, dict, str]:
        """Pace, then retry 429/5xx. A 400 that names reasoning_effort is retried once without it."""
        retries = max(1, int(self.settings.get("max_retries", 3)))
        backoff = float(self.settings.get("retry_backoff_seconds", 0.5))
        adapted = 0
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
                if exc.code == 400 and adapted < 2:
                    note = adapt_rejected_body(body, exc.detail)
                    if note:
                        adapted += 1
                        attempt -= 1
                        log.info("LLM rejected %s; retrying with the compatible body", note)
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
            "reasoning_tokens": _reasoning_tokens(raw_usage),
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


def _menu_key(features, positions, now: float, menu: list[MenuAction]) -> str:
    """Cache a menu pick for the hour. A new id on the menu is a new call."""
    payload = {
        "hour": int(now // 3600),
        "menu": [item.id for item in menu],
        "bars": [(row.symbol, row.bar_time, row.signal) for row in features],
        "positions": [row.get("symbol") for row in positions],
    }
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:24]
    return "menu:" + digest


def _reasoning_tokens(raw_usage: dict) -> int:
    if raw_usage.get("reasoning_tokens") is not None:
        try:
            return int(raw_usage.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            return 0
    details = raw_usage.get("completion_tokens_details") or raw_usage.get("output_tokens_details") or {}
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        try:
            return int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _billed_output_tokens(completion: int, reasoning: int) -> int:
    """Reasoning bills as output. A nested count already inside completion is not added twice."""
    if reasoning > completion:
        return completion + reasoning
    return max(completion, reasoning)


def _price(usage: dict, settings: dict, model: str) -> dict:
    """Prefer BoxAPI usage.cost. Estimate from the list price only when it is absent."""
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    output = _billed_output_tokens(completion, reasoning)
    cost = usage.get("cost")
    name = usage.get("model") or model
    if cost is not None and cost != "":
        usd = float(cost)
        source = "usage.cost"
    elif prompt or output:
        incoming, outgoing = list_prices(name, settings)
        markup = float(settings.get("price_markup", 1.15))
        usd = (prompt / 1_000_000 * incoming + output / 1_000_000 * outgoing) * markup
        source = "estimated_tokens"
    else:
        usd = quoted_call_usd(name, settings)
        source = "flat_fallback"
    return {
        "model": name,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "usd": float(usd),
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

    def _budget_note(self) -> dict:
        return {
            "spent_usd": round(self.budget.spent_usd(), 8),
            "budget_usd": float(self.settings.get("competition_budget_usd", 3.0)),
            "cap_usd": float(self.settings.get("max_spend_usd", 2.5)),
        }

    def _spend_guard_decision(self, candidates: list[Candidate]) -> AgentDecision | None:
        """Deterministic fallback once the ledger reaches $2.25. No model call."""
        spent = self.budget.spent_usd()
        if spent < SPEND_GUARD_USD:
            return None
        log.info("LLM fallback: llm_usd_total %.4f >= 2.25", spent)
        decision = fallback_decision(candidates)
        decision.comment = f"LLM fallback: llm_usd_total {spent:.4f} >= 2.25. " + decision.comment
        decision.usage = {"skipped": "spend guard 2.25", **self._budget_note()}
        return decision

    def decide(
        self,
        features: list[SymbolFeatures],
        candidates: list[Candidate],
        positions: list[dict],
        risk_state: dict,
        now: float,
        use_llm: bool = True,
        menu: list[MenuAction] | None = None,
    ) -> AgentDecision:
        if menu is not None and use_llm and self.settings.get("enabled", True):
            return self._decide_menu(features, candidates, positions, risk_state, now, menu)
        if not use_llm or not self.settings.get("enabled", True):
            decision = fallback_decision(candidates)
            decision.comment = "LLM disabled for this mode. " + decision.comment
            return decision
        guarded = self._spend_guard_decision(candidates)
        if guarded is not None:
            return guarded
        if not candidates and not positions:
            decision = fallback_decision(candidates)
            decision.usage = {"skipped": "nothing to rank", "tier": "routine"}
            return decision
        task = "stakes" if candidates and high_stakes(risk_state, self.settings) else ("trade" if candidates else "signal")
        spec = choose_tier(task, self.settings, self.budget.spent_usd(), self.budget.spent_today(now))
        self._maybe_refresh_mcp(candidates, now)
        user = self._prompt(features, candidates, positions, risk_state)
        key = _cycle_key(features, candidates, positions, now)
        hit = self.budget.peek(key, now)
        if hit and hit.get("content"):
            decision = self._parse_or_fallback(hit["content"], candidates)
            decision.source = "llm_cache"
            decision.replay = {"prompt": user, "response": hit.get("content") or ""}
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
            note = self._budget_note()
            log.info(
                "LLM skipped: %s spent=%.8f/%.2f cap=%.2f",
                reason, note["spent_usd"], note["budget_usd"], note["cap_usd"],
            )
            decision = fallback_decision(candidates)
            decision.comment = f"LLM cap: {reason}. " + decision.comment
            decision.usage = {
                "skipped": reason,
                "tier": spec["tier"],
                "model": spec["model"],
                "downgraded": spec["downgraded"],
                **note,
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
        decision.replay = {"prompt": user, "response": raw}
        return decision

    def _decide_menu(
        self,
        features: list[SymbolFeatures],
        candidates: list[Candidate],
        positions: list[dict],
        risk_state: dict,
        now: float,
        menu: list[MenuAction],
    ) -> AgentDecision:
        if not menu:
            menu = [hold_action()]
        rows = [item.as_log() for item in menu]
        kinds = {item.kind for item in menu}
        if kinds <= {"HOLD"}:
            decision = self._menu_decision(menu, menu[0] if menu else None, "menu is hold only", "")
            decision.error = ""
            decision.usage = {"skipped": "menu is hold only", "tier": "routine"}
            decision.menu = rows
            return decision
        guarded = self._spend_guard_decision(candidates)
        if guarded is not None:
            guarded.menu = rows
            return guarded
        task = "stakes" if "BUY" in kinds and high_stakes(risk_state, self.settings) else ("trade" if "BUY" in kinds else "signal")
        spec = choose_tier(task, self.settings, self.budget.spent_usd(), self.budget.spent_today(now))
        user = self._menu_prompt(features, positions, risk_state, menu)
        key = _menu_key(features, positions, now, menu)
        hit = self.budget.peek(key, now)
        if hit and hit.get("content"):
            decision = self._choice_or_hold(hit["content"], menu)
            decision.source = "llm_cache" if decision.menu_bound else decision.source
            decision.replay = {"prompt": user, "response": hit.get("content") or ""}
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
            decision.menu = rows
            return decision
        allowed, reason = self.budget.allow(now)
        if not allowed:
            note = self._budget_note()
            log.info(
                "LLM skipped: %s spent=%.8f/%.2f cap=%.2f",
                reason, note["spent_usd"], note["budget_usd"], note["cap_usd"],
            )
            decision = fallback_decision(candidates)
            decision.comment = f"LLM cap: {reason}. " + decision.comment
            decision.usage = {
                "skipped": reason,
                "tier": spec["tier"],
                "model": spec["model"],
                "downgraded": spec["downgraded"],
                **note,
            }
            decision.menu = rows
            return decision
        raw, usage = self.llm.complete(MENU_PROMPT, user, spec)
        if not raw:
            decision = fallback_decision(candidates)
            decision.usage = usage
            decision.error = str(usage.get("error") or usage.get("skipped") or "")
            decision.menu = rows
            return decision
        self.budget.remember(key, raw, usage, now)
        decision = self._choice_or_hold(raw, menu)
        decision.usage = usage
        decision.menu = rows
        decision.replay = {"prompt": user, "response": raw}
        return decision

    def _choice_or_hold(self, raw: str, menu: list[MenuAction]) -> AgentDecision:
        chosen, note = parse_menu_choice(raw, menu)
        if note:
            decision = self._menu_decision(menu, next((item for item in menu if item.kind == "HOLD"), None), note, raw)
            decision.error = note
            decision.veto_all = True
            return decision
        return self._menu_decision(menu, chosen, "", raw)

    def _menu_decision(self, menu: list[MenuAction], chosen: MenuAction | None, note: str, raw: str) -> AgentDecision:
        action = chosen if chosen is not None else menu[0]
        rationale = note or ""
        if raw and not note:
            try:
                rationale = str(extract_json(raw).get("rationale") or "").strip()
            except (ValueError, json.JSONDecodeError, TypeError):
                rationale = ""
        if action.kind == "HOLD":
            text = rationale or note or "hold"
            decisions = [ParsedDecision("HOLD", "", None, 1.0, False, text)]
        elif action.kind == "BUY":
            decisions = [ParsedDecision("BUY", action.symbol, action.size_fraction, 0.7, False, rationale)]
        elif action.kind == "SELL":
            decisions = [ParsedDecision("SELL", action.symbol, 1.0, 0.7, False, rationale)]
        elif action.kind == "REDUCE":
            decisions = [ParsedDecision("SELL", action.symbol, action.size_fraction if action.size_fraction is not None else 0.5, 0.7, False, rationale)]
        elif action.kind == "CANCEL":
            decisions = [ParsedDecision(
                "CANCEL", action.symbol, None, 0.7, False, rationale, client_order_id=action.client_order_id,
            )]
        else:
            decisions = [ParsedDecision("HOLD", "", None, 1.0, False, "invalid output")]
            action = next((item for item in menu if item.kind == "HOLD"), action)
            note = "invalid output"
        return AgentDecision(
            source="llm",
            veto_all=False,
            regime="menu",
            comment=rationale or note,
            decisions=decisions,
            raw=(raw or "")[:2000],
            error=note,
            menu_bound=True,
            picked_id=action.id if action.kind != "HOLD" or not note else "hold",
            menu=[item.as_log() for item in menu],
        )

    def _menu_prompt(self, features, positions, risk_state, menu: list[MenuAction]) -> str:
        payload = {
            "risk": {
                "equity": risk_state.get("equity"),
                "drawdown": risk_state.get("drawdown"),
                "daily_drawdown": risk_state.get("daily_drawdown"),
                "gross_exposure": risk_state.get("gross_exposure"),
                "phase": risk_state.get("phase"),
            },
            "positions": [
                {
                    "symbol": row.get("symbol"),
                    "qty": row.get("qty"),
                    "entry_price": row.get("entry_price"),
                }
                for row in positions
            ],
            "state": [
                {
                    "symbol": row.symbol,
                    "close": row.close,
                    "ema_slow": row.ema_slow,
                    "roc_24h": row.roc_trend,
                    "roc_4h": row.roc_pullback,
                    "spread_bps": row.spread_bps,
                    "imbalance": row.imbalance,
                    "liquidity_scale": None if row.liquidity is None else row.liquidity.scale,
                    "depth_100_tmn": None if row.liquidity is None else row.liquidity.depth_100_tmn,
                }
                for row in features
            ],
            "menu": [item.as_log() for item in menu],
        }
        return json.dumps(payload, ensure_ascii=False)

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
        if self.budget.spent_usd() >= SPEND_GUARD_USD:
            spent = self.budget.spent_usd()
            log.info("LLM fallback: llm_usd_total %.4f >= 2.25", spent)
            return [{"skipped": "spend guard 2.25", "tier": "routine", "spent_usd": round(spent, 8)}]
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
            log.warning("LLM output rejected (%s); veto", exc)
            return AgentDecision(
                source="fallback",
                veto_all=True,
                regime="invalid",
                comment="invalid model output",
                decisions=[],
                raw=raw[:2000],
                error=str(exc),
            )
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
