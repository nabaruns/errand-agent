"""Explicit workflow state for one chat session, persisted as JSON after every run."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from enum import StrEnum

from .mocks.catalog import SERVICES
from .mocks.service import parse_slot


class Phase(StrEnum):
    GATHERING = "gathering"  # collecting request details
    PRESENTING = "presenting"  # options found, waiting for the user to pick
    AWAITING_APPROVAL = "awaiting_approval"  # exact action shown, waiting for an explicit yes
    EXECUTING = "executing"  # approved side effect in progress
    DONE = "done"  # verified outcome reported
    UNVERIFIED = "unverified"  # side effect outcome unknown; must be reconciled before claiming anything


TASKS = ("book", "reschedule", "cancel")
BOOKING_REQUIRED = ("service", "area", "date_start", "date_end", "budget_max")
# Fields whose change makes earlier search results stale.
SEARCH_FIELDS = ("service", "area", "date_start", "date_end")

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class Request:
    task: str | None = None
    service: str | None = None
    area: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    budget_max: int | None = None
    preferred_provider_id: str | None = None
    notes: str | None = None

    def missing(self) -> list[str]:
        if self.task not in (None, "book"):
            return []
        return [f for f in BOOKING_REQUIRED if getattr(self, f) in (None, "")]

    def update(self, changes: dict) -> dict:
        """Apply only the given fields. Returns {field: [old, new]} for fields that changed."""
        changed = {}
        for key, value in changes.items():
            old = getattr(self, key)
            if old != value:
                setattr(self, key, value)
                changed[key] = [old, value]
        return changed

    def slot_problem(self, slot: str) -> str | None:
        """Why a slot falls outside the stated request, or None if it fits."""
        day, hhmm = slot[:10], slot[11:16]
        if self.date_start and day < self.date_start:
            return f"{slot} is before the requested start date {self.date_start}"
        if self.date_end and day > self.date_end:
            return f"{slot} is after the requested end date {self.date_end}"
        if self.earliest_time and hhmm < self.earliest_time:
            return f"{slot} is earlier than the user's earliest time {self.earliest_time}"
        if self.latest_time and hhmm > self.latest_time:
            return f"{slot} is later than the user's latest time {self.latest_time}"
        return None


def validate_request_changes(args: dict, clear: list[str]) -> dict:
    """Normalize model-supplied request fields. Raises ValueError with a model-readable reason."""
    allowed = {f.name for f in fields(Request)}
    unknown = (set(args) | set(clear)) - allowed
    if unknown:
        raise ValueError(f"unknown request fields: {sorted(unknown)}")
    changes: dict = {name: None for name in clear}
    for key, value in args.items():
        if value is None:
            continue
        if key == "task" and value not in TASKS:
            raise ValueError(f"task must be one of {TASKS}")
        if key == "service" and value not in SERVICES:
            raise ValueError(f"service must be one of {SERVICES}")
        if key in ("date_start", "date_end"):
            value = date.fromisoformat(str(value)[:10]).isoformat()
        if key in ("earliest_time", "latest_time") and not _TIME.match(str(value)):
            raise ValueError(f"{key} must be HH:MM (24h)")
        if key == "budget_max":
            value = int(value)
            if value <= 0:
                raise ValueError("budget_max must be a positive number of rupees")
        changes[key] = value
    start = changes.get("date_start")
    end = changes.get("date_end")
    if start and end and end < start:
        raise ValueError("date_end is before date_start")
    return changes


@dataclass
class Session:
    session_id: str
    user_id: str
    phase: Phase = Phase.GATHERING
    request: Request = field(default_factory=Request)
    # provider_id -> summary from the latest search; proposals must come from here.
    candidates: dict = field(default_factory=dict)
    # booking_id -> booking as last seen from list_bookings or a verified outcome.
    known_bookings: dict = field(default_factory=dict)
    pending_approval_id: str | None = None
    # What the last assistant message was: "none" | "text" | "approval_prompt" | "outcome".
    # A bare "yes" only counts as approval right after an approval prompt.
    last_assistant_kind: str = "none"
    history: list = field(default_factory=list)  # [{"role", "content"}] text only
    outcomes: list = field(default_factory=list)  # verified results reported to the user
    notices: list = field(default_factory=list)  # messages to prepend to the next reply

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        data = json.loads(raw)
        data["phase"] = Phase(data["phase"])
        data["request"] = Request(**data["request"])
        return cls(**data)


def is_valid_slot(slot: str) -> bool:
    try:
        parse_slot(slot)
    except (TypeError, ValueError):
        return False
    return True
