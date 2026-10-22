"""Tehran time helpers. Iran is UTC+03:30 and does not observe DST."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover - tzdata is present on the target image
    TEHRAN = timezone(timedelta(hours=3, minutes=30))


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TEHRAN)
    return dt


def exchange_epoch(value) -> float | None:
    """UTC epoch seconds for an exchange clock.

    ISO strings that end in Z are UTC. UDF history uses unix seconds. A
    websocket timestamp above 1e10 is unix milliseconds.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("+00:00") or text.endswith("-00:00"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        try:
            value = float(text)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000.0
    return number


def to_tehran(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN)


def now_tehran() -> datetime:
    return datetime.now(TEHRAN)


def day_key(dt: datetime) -> str:
    return to_tehran(dt).strftime("%Y-%m-%d")


def iso(dt: datetime | None = None) -> str:
    current = dt or now_tehran()
    return to_tehran(current).isoformat(timespec="seconds")
