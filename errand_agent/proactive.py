"""Proactive follow-up when a provider cancels: preference resolution and replacement ranking.

Runs without the model. The agent decides in code whether to notify, searches within the
user's saved preferences, holds the best slot, and opens a fresh approval. Nothing is
rebooked until the user says yes to that new confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .approval import rupees
from .memory import Memory
from .mocks.catalog import PROVIDERS
from .mocks.service import parse_slot
from .state import Request

# Look for a replacement on the original day and up to this many days after.
REPLACEMENT_WINDOW_DAYS = 2
MAX_HOLD_ATTEMPTS = 3


@dataclass
class Preferences:
    area: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    budget_max: int | None = None
    preferred_provider: str | None = None
    avoid_providers: set[str] = field(default_factory=set)
    used: list[str] = field(default_factory=list)  # memory refs the search relied on

    def describe(self) -> str:
        parts = []
        if self.area:
            parts.append(self.area)
        if self.earliest_time:
            parts.append(f"not before {self.earliest_time}")
        if self.latest_time:
            parts.append(f"not after {self.latest_time}")
        if self.budget_max:
            parts.append(f"up to {rupees(self.budget_max)}")
        if self.avoid_providers:
            parts.append("avoiding " + ", ".join(PROVIDERS[p].name for p in sorted(self.avoid_providers)))
        return ", ".join(parts) or "no saved preferences"


def resolve_preferences(memories: list[Memory], service: str, request: Request | None = None) -> Preferences:
    """Usable facts only. A service-scoped fact beats a global one for the same key.

    Constraints from the session's own request for this service (e.g. "mornings only, under ₹1,500")
    fill any gap the stored memories leave, so a replacement respects what the user asked for.
    """
    prefs = Preferences()
    usable = sorted((m for m in memories if m.usable and m.scope in ("global", service)),
                    key=lambda m: (m.scope != "global", m.id))  # global first, so scoped overrides it
    for m in usable:
        if m.key == "avoid_provider":
            prefs.avoid_providers.add(m.value)
        elif m.key == "area":
            prefs.area = m.value
        elif m.key in ("earliest_time", "latest_time", "preferred_provider"):
            setattr(prefs, m.key, m.value)
        elif m.key == "budget_max":
            prefs.budget_max = int(m.value)
        else:
            continue
        prefs.used.append(m.ref)
    if request is not None and request.service == service:
        for name in ("area", "earliest_time", "latest_time", "budget_max"):
            if getattr(prefs, name) is None and getattr(request, name) is not None:
                setattr(prefs, name, getattr(request, name))
                prefs.used.append(f"request.{name}")
    return prefs


def rank_replacements(providers: list[dict], original: dict, prefs: Preferences,
                      now: datetime) -> list[tuple[dict, str]]:
    """Every (provider, slot) that fits the preferences, best first.

    Order: preferred provider, then closest to the original time, then rating, then price.
    """
    target = parse_slot(original["slot"])
    options = []
    for p in providers:
        if p["provider_id"] == original["provider_id"] or p["provider_id"] in prefs.avoid_providers:
            continue
        if prefs.budget_max and p["price"] > prefs.budget_max:
            continue
        for slot in p["available_slots"]:
            hhmm = slot[11:16]
            if prefs.earliest_time and hhmm < prefs.earliest_time:
                continue
            if prefs.latest_time and hhmm > prefs.latest_time:
                continue
            when = parse_slot(slot)
            if when <= now:
                continue
            key = (
                p["provider_id"] != prefs.preferred_provider,
                abs((when - target).total_seconds()),
                -p["rating"],
                p["price"],
            )
            options.append((key, p, slot))
    options.sort(key=lambda o: o[0])
    return [(p, slot) for _, p, slot in options]
