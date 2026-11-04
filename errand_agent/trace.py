"""Per-run JSONL traces: state transitions, model calls, tool calls, approvals, outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .clock import Clock

MAX_STRING = 600


def sanitize(value: Any, max_len: int = MAX_STRING) -> Any:
    """Bound the size of anything written to a trace."""
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len] + f"...[+{len(value) - max_len} chars]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, max_len) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize(str(value), max_len)


class Tracer:
    def __init__(self, run_id: str, session_id: str, clock: Clock, path: Path | None = None,
                 sink: Callable[[dict], None] | None = None) -> None:
        self.run_id = run_id
        self.sink = sink  # live consumer, e.g. the demo UI's trace pane
        self.session_id = session_id
        self.clock = clock
        self.path = path
        self.events: list[dict] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, /, **data: Any) -> dict:
        # Tracing must never take a run down: `kind` is positional-only so a data field with the
        # same name cannot clash with it, and data fields can never overwrite the envelope.
        record = {
            "seq": len(self.events),
            "ts": self.clock.now().isoformat(),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "kind": kind,
        }
        for key, value in sanitize(data).items():
            record[f"{key}_" if key in record else key] = value
        self.events.append(record)
        if self.sink is not None:
            try:
                self.sink(record)
            except Exception:  # a broken viewer must not break the run
                pass
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def tool_call(self, name: str, args: dict, latency_ms: float, *, source: str, result: Any = None,
                  error: str | None = None, **extra: Any) -> None:
        self.event("tool_call", tool=name, args=args, source=source, latency_ms=round(latency_ms, 2),
                   result=result, error=error, **extra)

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]
