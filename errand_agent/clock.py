"""Injectable clocks so hold expiry and "this week" are testable without sleeping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30), "IST")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)
