"""Public sentiment inputs.

Fear & Greed comes from alternative.me, which publishes a daily history.
Headlines come from CoinDesk and Cointelegraph RSS, with CryptoPanic and
Reddit tried as extras. Any source that fails is skipped. Scores are a
fixed lexicon, so this layer does not spend the BoxAPI budget.

The backtest can see Fear & Greed. It cannot see headlines, because those
feeds are not a point-in-time archive. A value is used only after a one-day
publication lag, so a bar does not read the index stamped on its own UTC day.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("trader.sentiment")

USER_AGENT = "starcoach-wallex-trader/1.0 (Starcoach hackathon; public research)"
PUBLICATION_LAG_SECONDS = 86400

# Small fixed lists. Not fitted to the backtest.
POSITIVE = {
    "surge", "rally", "gain", "gains", "bull", "bullish", "breakout", "approval",
    "approve", "adopt", "adoption", "inflow", "inflows", "soar", "soars", "record",
    "upgrade", "partnership", "recover", "recovery", "optimism", "optimistic", "support",
}
NEGATIVE = {
    "crash", "hack", "hacked", "exploit", "ban", "banned", "lawsuit", "selloff",
    "plunge", "plunges", "bear", "bearish", "fraud", "outflow", "outflows",
    "liquidation", "liquidations", "dump", "collapse", "halt", "delist", "scam",
    "attack", "stolen", "bankrupt", "bankruptcy",
}

ALIASES = {
    "BTCTMN": ("bitcoin", "btc"),
    "ETHTMN": ("ethereum", "ether"),
    "USDTTMN": ("tether", "usdt"),
    "XRPTMN": ("ripple", "xrp"),
    "DOGETMN": ("dogecoin", "doge"),
    "SOLTMN": ("solana",),
    "BNBTMN": ("binance coin", "bnb"),
    "XAUTTMN": ("tether gold", "xaut"),
}

SOURCES = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "cryptopanic": "https://cryptopanic.com/news/rss/",
    "reddit": "https://www.reddit.com/r/cryptocurrency/new.json?limit=25",
}


@dataclass
class SentimentSnapshot:
    fear_greed: float | None = None
    fear_greed_change: float | None = None
    fear_greed_label: str = ""
    news_score: dict[str, float | None] = field(default_factory=dict)
    news_change: dict[str, float | None] = field(default_factory=dict)
    headlines: dict[str, list[str]] = field(default_factory=dict)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "fear_greed": self.fear_greed,
            "fear_greed_change": self.fear_greed_change,
            "fear_greed_label": self.fear_greed_label,
            "news_score": self.news_score,
            "news_change": self.news_change,
            "headlines": self.headlines,
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
        }


def lexicon_score(text: str) -> tuple[float, int]:
    """Return (score in [-1, 1], number of lexicon hits)."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    pos = sum(1 for word in words if word in POSITIVE)
    neg = sum(1 for word in words if word in NEGATIVE)
    hits = pos + neg
    if hits == 0:
        return 0.0, 0
    return (pos - neg) / hits, hits


def fear_greed_at(series: list[tuple[int, float, str]] | None, stamp: int) -> tuple[float | None, float | None, str]:
    """Latest index whose timestamp is at least one day before `stamp`."""
    if not series:
        return None, None, ""
    known = [row for row in series if row[0] + PUBLICATION_LAG_SECONDS <= stamp]
    if not known:
        return None, None, ""
    value = known[-1][1]
    change = None if len(known) < 2 else value - known[-2][1]
    return value, change, known[-1][2]


def _settings(cfg: dict) -> dict:
    raw = dict(cfg.get("sentiment") or {})
    raw.setdefault("enabled", True)
    raw.setdefault("fear_greed_url", "https://api.alternative.me/fng/")
    raw.setdefault("block_extreme_greed", False)
    raw.setdefault("extreme_greed", 80)
    raw.setdefault("cache_seconds", 900)
    raw.setdefault("history_cache_seconds", 6 * 3600)
    raw.setdefault("cache_path", "data/sentiment_cache.json")
    raw.setdefault("timeout_seconds", 8)
    return raw


class SentimentFeed:
    def __init__(self, cfg: dict, opener=None):
        self.settings = _settings(cfg)
        self.path = Path(self.settings["cache_path"])
        self._opener = opener or urllib.request.urlopen
        self.series: list[tuple[int, float, str]] = []
        self._cache = self._load()
        self._disabled_sources: set[str] = set()
        self._logged_at: dict[str, float] = {}

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._cache), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("sentiment cache write failed: %s", exc)

    def load_history(self) -> list[tuple[int, float, str]]:
        """Daily Fear & Greed. Uses the cache when it is fresh. Never raises."""
        if not self.settings.get("enabled", True):
            return []
        cached_at = float(self._cache.get("fear_greed_fetched_at") or 0)
        cached = self._cache.get("fear_greed") or []
        if cached and time.time() - cached_at < float(self.settings["history_cache_seconds"]):
            self.series = [(int(row["t"]), float(row["v"]), str(row.get("label") or "")) for row in cached]
            return self.series
        try:
            payload = self._get_json(self.settings["fear_greed_url"], {"limit": 0, "format": "json"})
            points = []
            for row in payload.get("data") or []:
                points.append((int(row["timestamp"]), float(row["value"]), str(row.get("value_classification") or "")))
            points.sort()
            self.series = points
            self._cache["fear_greed_fetched_at"] = time.time()
            self._cache["fear_greed"] = [{"t": t, "v": v, "label": label} for t, v, label in points]
            self._save()
            log.info("fear & greed history: %s days", len(points))
        except Exception as exc:
            log.warning("fear & greed history failed: %s", exc)
            self.series = [(int(row["t"]), float(row["v"]), str(row.get("label") or "")) for row in cached]
        return self.series

    def snapshot(self, symbols: list[str]) -> SentimentSnapshot:
        """Market Fear & Greed plus a lexicon score per symbol. Never raises."""
        snap = SentimentSnapshot()
        if not self.settings.get("enabled", True):
            return snap
        try:
            if not self.series:
                self.load_history()
            # Live uses the newest published point. Backtests apply the one-day lag.
            if self.series:
                value = self.series[-1][1]
                change = None if len(self.series) < 2 else value - self.series[-2][1]
                label = self.series[-1][2]
                snap.sources_ok.append("fear_greed")
            else:
                value, change, label = None, None, ""
                snap.sources_failed.append("fear_greed")
            snap.fear_greed = value
            snap.fear_greed_change = change
            snap.fear_greed_label = label
            self._news(symbols, snap)
        except Exception as exc:
            log.warning("sentiment snapshot failed: %s", exc)
            snap.sources_failed.append(f"snapshot:{exc}")
        return snap

    def _news(self, symbols: list[str], snap: SentimentSnapshot) -> None:
        cached_at = float(self._cache.get("news_fetched_at") or 0)
        cached_news = self._cache.get("news") or {}
        if cached_news and time.time() - cached_at < float(self.settings["cache_seconds"]):
            self._apply_cached_news(symbols, snap, cached_news)
            snap.sources_ok.append("news_cache")
            return
        titles: list[str] = []
        for name, url in SOURCES.items():
            if name in self._disabled_sources:
                snap.sources_failed.append(name)
                continue
            try:
                if name == "reddit":
                    titles.extend(self._reddit_titles(url))
                else:
                    titles.extend(self._rss_titles(url))
                snap.sources_ok.append(name)
            except Exception as exc:
                snap.sources_failed.append(name)
                code = _status_code(exc)
                if code in {403, 410} or name in {"cryptopanic", "reddit"}:
                    # Dead or blocked feeds stay off for the process. Fear & greed still runs.
                    self._disabled_sources.add(name)
                self._log_source_failure(name, exc)
        if not titles:
            cached_news = self._cache.get("news") or {}
            if cached_news:
                self._apply_cached_news(symbols, snap, cached_news)
                snap.sources_ok.append("news_cache")
                return
        previous = self._cache.get("news_score") or {}
        scored: dict[str, dict] = {}
        for symbol in symbols:
            matched = [title for title in titles if _mentions(symbol, title)]
            score = _mean_score(matched)
            old = previous.get(symbol)
            change = None if score is None or old is None else round(score - float(old), 4)
            scored[symbol] = {
                "score": score,
                "change": change,
                "headlines": [title[:120] for title in matched[:2]],
            }
            snap.news_score[symbol] = score
            snap.news_change[symbol] = change
            snap.headlines[symbol] = scored[symbol]["headlines"]
        self._cache["news_fetched_at"] = time.time()
        self._cache["news"] = scored
        self._cache["news_score"] = {symbol: row["score"] for symbol, row in scored.items()}
        self._save()

    def _log_source_failure(self, name: str, exc: BaseException) -> None:
        """One line per source per hour. A dead feed must not fill the cycle log."""
        now = time.time()
        if now - self._logged_at.get(name, 0.0) < 3600:
            return
        self._logged_at[name] = now
        code = _status_code(exc)
        # Cryptopanic 410 and Reddit blocks are expected; keep them off the cycle log.
        if name in {"cryptopanic", "reddit"} or code in {403, 410, 429}:
            log.debug("sentiment source %s failed: %s", name, type(exc).__name__)
            return
        log.info("sentiment source %s failed: %s", name, exc)

    def _apply_cached_news(self, symbols, snap, cached_news) -> None:
        for symbol in symbols:
            row = cached_news.get(symbol) or {}
            snap.news_score[symbol] = row.get("score")
            snap.news_change[symbol] = row.get("change")
            snap.headlines[symbol] = list(row.get("headlines") or [])

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        raw = self._read(url)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("sentiment payload was not an object")
        return payload

    def _read(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, text/xml, */*"})
        with self._opener(request, timeout=float(self.settings["timeout_seconds"])) as response:
            return response.read().decode("utf-8", errors="replace")

    def _rss_titles(self, url: str) -> list[str]:
        root = ET.fromstring(self._read(url))
        titles = []
        for item in root.iter():
            tag = item.tag.lower()
            if tag.endswith("item") or tag.endswith("entry"):
                title = ""
                for child in list(item):
                    if child.tag.lower().endswith("title") and child.text:
                        title = child.text.strip()
                        break
                if title:
                    titles.append(re.sub(r"\s+", " ", title))
        return titles[:40]

    def _reddit_titles(self, url: str) -> list[str]:
        payload = self._get_json(url)
        children = ((payload.get("data") or {}).get("children") or [])
        titles = []
        for child in children:
            title = ((child.get("data") or {}).get("title") or "").strip()
            if title:
                titles.append(title)
        return titles[:25]


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if code is None and exc.__cause__ is not None:
        code = getattr(exc.__cause__, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _mentions(symbol: str, title: str) -> bool:
    haystack = title.lower()
    for alias in ALIASES.get(symbol.upper(), ()):
        if re.search(rf"\b{re.escape(alias)}\b", haystack):
            return True
    return False


def _mean_score(titles: list[str]) -> float | None:
    if not titles:
        return None
    total = 0.0
    for title in titles:
        score, _hits = lexicon_score(title)
        total += score
    return round(total / len(titles), 4)
