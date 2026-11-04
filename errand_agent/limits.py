"""Hard per-run limits on model calls, tool calls, and wall-clock time."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 8
    max_tool_calls: int = 15
    max_wall_seconds: float = 45.0
    # Side-effect retries (book, cancel, reschedule, status checks) per action.
    max_attempts: int = 3
    backoff_seconds: float = 0.2


class LimitExceeded(Exception):
    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


LIMIT_MESSAGES = {
    "model_calls": (
        "I couldn't work this out within my step limit, so I stopped. Nothing was booked or changed. "
        "Could you tell me more specifically what you need?"
    ),
    "tool_calls": (
        "I made too many lookups for one request and stopped to avoid looping. Nothing was booked or changed. "
        "Try narrowing it down: one service, one area, a shorter date range."
    ),
    "context_budget": (
        "This request needed more context than I'm allowed to send in one step, so I stopped. Nothing was "
        "booked or changed. Try a narrower request."
    ),
    "wall_clock": "This took too long, so I stopped. Nothing was booked or changed in this turn. Please try again.",
}


class Budget:
    def __init__(self, limits: Limits, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._monotonic = monotonic
        self.start = monotonic()
        self.model_calls = 0
        self.tool_calls = 0

    def elapsed(self) -> float:
        return self._monotonic() - self.start

    def check_time(self) -> None:
        if self.elapsed() > self.limits.max_wall_seconds:
            raise LimitExceeded("wall_clock")

    def charge_model(self) -> None:
        self.check_time()
        if self.model_calls >= self.limits.max_model_calls:
            raise LimitExceeded("model_calls")
        self.model_calls += 1

    def charge_tool(self) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise LimitExceeded("tool_calls")
        self.tool_calls += 1
