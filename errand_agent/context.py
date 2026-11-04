"""Builds what the model sees each turn, under a fixed token budget.

Selection is done in code: only usable memories (active, and explicit or confirmed) that are
relevant to the current service or the user's message, packed by relevance until the memory
budget is spent; plus the most recent chat history that fits the history budget. Every turn
logs a manifest of what was included and why everything else was left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .memory import Memory
from .mocks.catalog import SERVICE_ALIASES, SERVICES
from .model import estimate_tokens


@dataclass(frozen=True)
class ContextBudget:
    max_request_tokens: int = 12_000  # hard cap on any single model request
    memory_tokens: int = 800
    history_tokens: int = 3_000
    # The history window's start moves in steps of this many messages, so consecutive
    # turns usually share the same prefix and stay prompt-cache friendly.
    history_chunk: int = 8


SERVICE_HINTS = {
    "plumber": ("plumb", "tap", "leak", "pipe", "drain", "toilet", "geyser"),
    "electrician": ("electric", "wiring", "switch", "fan", "socket", "light", "mcb"),
    "ac_servicing": (" ac ", "a/c", "air condition", "aircon", "cooling"),
    "carpenter": ("carpent", "furniture", "wood", "cupboard", "door hinge", "shelf"),
    "cleaning": ("clean", "maid", "deep clean", "dust"),
}
CORE_GLOBAL_KEYS = ("area", "earliest_time", "latest_time", "preferred_days", "language", "budget_max")
_WORD = re.compile(r"[a-z]{4,}")
_STOP = {
    "with", "that", "this", "have", "from", "your", "want", "need", "please", "book", "some", "next", "week",
    "before", "after", "then", "than", "when", "what", "there", "also", "time", "last", "never", "anyone",
    "always", "should", "would", "could", "about", "them", "they", "just", "like", "same", "sometime",
}


def services_mentioned(text: str) -> set[str]:
    lowered = f" {text.lower()} "
    found = {s for s, hints in SERVICE_HINTS.items() if any(h in lowered for h in hints)}
    found |= {s for s in SERVICES if s.replace("_", " ") in lowered}
    found |= {s for alias, s in SERVICE_ALIASES.items() if f" {alias} " in lowered}
    return found


def _keywords(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP


def relevance(memory: Memory, services: set[str], text: str) -> float:
    """0 means irrelevant now. Higher is packed first."""
    if memory.scope != "global":
        return 4.0 if memory.scope in services else 0.0
    if memory.key in CORE_GLOBAL_KEYS:
        return 3.0
    overlap = _keywords(memory.value or "") & _keywords(text)
    return 1.0 + len(overlap) if overlap else 0.0


def select_memories(memories: list[Memory], services: set[str], text: str,
                    max_tokens: int) -> tuple[list[Memory], dict[str, str]]:
    """Returns (included, {ref: reason}) for every memory left out."""
    excluded: dict[str, str] = {}
    scored: list[tuple[float, Memory]] = []
    for m in memories:
        if m.status != "active":
            excluded[m.ref] = m.status  # forgotten | superseded
        elif not m.usable:
            excluded[m.ref] = "unconfirmed_inferred"
        else:
            score = relevance(m, services, text)
            if score <= 0:
                excluded[m.ref] = "not_relevant"
            else:
                scored.append((score, m))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].id))
    included: list[Memory] = []
    used = 0
    for _, m in scored:
        cost = estimate_tokens(m.view())
        if used + cost > max_tokens:
            excluded[m.ref] = "over_budget"
            continue
        included.append(m)
        used += cost
    return included, excluded


def history_window(history: list[dict], max_tokens: int, chunk: int) -> tuple[list[dict], int]:
    """The newest slice of history that fits, starting on a chunk boundary and a user message."""
    start = 0
    while start < len(history) and estimate_tokens(history[start:]) > max_tokens:
        start += chunk
    while start < len(history) and history[start]["role"] != "user":
        start += 1
    return [dict(m) for m in history[start:]], start


def request_tokens(system: str, tools: list[dict], messages: list[dict]) -> int:
    return estimate_tokens(system) + estimate_tokens(tools) + estimate_tokens(messages)
