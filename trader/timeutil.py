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
