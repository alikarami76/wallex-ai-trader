"""A virtual clock so several hours of market time run in one short test."""

from __future__ import annotations

from datetime import datetime, timezone

from trader.timeutil import TEHRAN


class Clock:
    def __init__(self, unix: float):
        self.unix = float(unix)

    def time(self) -> float:
        return self.unix

    def advance(self, seconds: float) -> None:
        self.unix += float(seconds)

    def now(self, tz=None):
        current = datetime.fromtimestamp(self.unix, timezone.utc)
        if tz is None:
            return current
        return current.astimezone(tz)

    def tehran(self) -> datetime:
        return self.now(TEHRAN)


class _DateTime:
    """Stand-in for the datetime class. Only `now` follows the virtual clock."""

    def __init__(self, clock: Clock, original):
        self._clock = clock
        self._original = original

    def now(self, tz=None):
        return self._clock.now(tz)

    def __getattr__(self, name):
        return getattr(self._original, name)


def install(clock: Clock) -> list[tuple[object, str, object]]:
    """Point the loop and the journals at `clock`. Returns the previous bindings."""
    import trader.engine.ops as ops
    import trader.engine.runtime as runtime
    import trader.strategy.agent as agent
    import trader.timeutil as timeutil
    import trader.wallex.client as client

    saved: list[tuple[object, str, object]] = []

    def patch(obj: object, name: str, value: object) -> None:
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    patch(runtime.time, "time", clock.time)
    patch(client.time, "time", clock.time)
    patch(agent.time, "time", clock.time)
    patch(runtime.time, "sleep", lambda seconds: clock.advance(seconds))
    patch(runtime, "datetime", _DateTime(clock, runtime.datetime))
    patch(ops, "datetime", _DateTime(clock, ops.datetime))
    patch(timeutil, "now_tehran", clock.tehran)
    # The loop imported the function directly, so the module global has to move too.
    patch(runtime, "now_tehran", clock.tehran)
    return saved


def restore(saved: list[tuple[object, str, object]]) -> None:
    for obj, name, value in reversed(saved):
        setattr(obj, name, value)
