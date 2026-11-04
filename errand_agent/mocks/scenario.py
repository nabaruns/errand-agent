"""Scenario knobs for the mock provider environment.

Defaults match the assignment's required behaviour. Tests override fields to force
specific outcomes; everything else stays reproducible through `seed`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

BOOK_OUTCOMES = ("success", "slot_taken", "timeout_booked", "timeout_not_booked")


@dataclass
class Scenario:
    seed: int = 42
    # Share of generated slots a provider already has filled.
    busy_rate: float = 0.3
    hold_ttl_seconds: int = 600
    # Quoted price at hold time: per-provider absolute override, else search price + delta.
    hold_price_overrides: dict[str, int] = field(default_factory=dict)
    hold_price_delta: int = 0
    # Forced book() outcomes, consumed in order across all book() draws. After the list
    # runs out, outcomes are drawn from the seeded rates below.
    book_outcomes: tuple[str, ...] = ()
    book_success_rate: float = 0.70
    book_slot_taken_rate: float = 0.15
    timeout_booked_share: float = 0.5
    cancel_first_call_fails: bool = True
    # Slots that reschedule() always rejects with slot_conflict. None means every
    # Saturday at 10:00, so "move it to Saturday" can hit a conflict by default.
    conflict_slots: frozenset[str] | None = None

    def __post_init__(self) -> None:
        unknown = set(self.book_outcomes) - set(BOOK_OUTCOMES)
        if unknown:
            raise ValueError(f"unknown book outcomes: {sorted(unknown)}")


def draw(seed: int, *parts: object) -> float:
    """Deterministic uniform [0, 1) keyed by seed and call identity.

    Keyed draws (rather than one shared RNG stream) keep outcomes stable across
    process restarts and independent of call ordering.
    """
    digest = hashlib.sha256(":".join(map(str, (seed, *parts))).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64
