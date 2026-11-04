"""Persistent user memory with explicit vs inferred facts, correction, and forgetting.

Rules enforced here, not by the model:
- A fact is stored as explicit only if its evidence quote appears in the user's own message
  this turn. Anything else (including text lifted from tool output) is stored as inferred.
- Inferred facts are not usable until the user confirms them.
- Storing a fact supersedes the previous active fact for the same key and scope.
- Forgetting blanks the value, so a forgotten fact cannot reach any later prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .mocks.catalog import AREAS, PROVIDERS, SERVICES, normalize_area
from .state import _TIME
from .store import AgentStore

MEMORY_KEYS = (
    "area",
    "earliest_time",
    "latest_time",
    "budget_max",
    "preferred_provider",
    "avoid_provider",
    "preferred_days",
    "language",
    "note",
)
SCOPES = ("global", *SERVICES)
MULTI_VALUED = ("note",)

KEY_LABELS = {
    "area": "Area",
    "earliest_time": "Earliest time",
    "latest_time": "Latest time",
    "budget_max": "Budget",
    "preferred_provider": "Preferred provider",
    "avoid_provider": "Avoid provider",
    "preferred_days": "Preferred days",
    "language": "Language",
    "note": "Note",
}

_REF = re.compile(r"^m?(\d+)$", re.IGNORECASE)


@dataclass
class Memory:
    id: int
    user_id: str
    key: str
    scope: str
    value: str | None
    source: str
    confirmed: bool
    status: str
    evidence: str | None
    created_at: str
    updated_at: str

    @property
    def ref(self) -> str:
        return f"m{self.id}"

    @property
    def usable(self) -> bool:
        """May this fact reach the model? Active, and either stated by the user or confirmed."""
        return self.status == "active" and (self.source == "explicit" or self.confirmed)

    def view(self) -> dict:
        return {"id": self.ref, "key": self.key, "scope": self.scope, "value": self.value, "source": self.source}


def validate_memory(key: str, scope: str, value: object) -> str:
    """Normalize a fact. Raises ValueError with a model-readable reason."""
    if key not in MEMORY_KEYS:
        raise ValueError(f"key must be one of {MEMORY_KEYS}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    text = str(value).strip()
    if not text:
        raise ValueError("value is empty")
    if key == "area":
        area = normalize_area(text)
        if area is None:
            raise ValueError(f"area must be one of {AREAS}")
        return area
    if key in ("earliest_time", "latest_time") and not _TIME.match(text):
        raise ValueError(f"{key} must be HH:MM (24h)")
    if key == "budget_max" and not text.isdigit():
        raise ValueError("budget_max must be a whole number of rupees")
    if key in ("preferred_provider", "avoid_provider"):
        provider = PROVIDERS.get(text)
        if provider is None:
            raise ValueError("value must be a provider_id, e.g. ele_02")
        if scope != provider.service:
            raise ValueError(f"{text} is a {provider.service}; use scope {provider.service!r}")
    return text[:200]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9₹]+", " ", text.lower()).strip()


def evidence_in(evidence: str | None, user_text: str) -> bool:
    """True if the quoted evidence really is in what the user said this turn."""
    quote = _norm(evidence or "")
    return len(quote) >= 3 and quote in _norm(user_text)


class MemoryStore:
    def __init__(self, store: AgentStore) -> None:
        self.store = store

    def all(self, user_id: str) -> list[Memory]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY memory_id", (user_id,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, user_id: str, ref: str | int) -> Memory | None:
        match = _REF.match(str(ref).strip())
        if not match:
            return None
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT * FROM memories WHERE user_id = ? AND memory_id = ?", (user_id, int(match.group(1)))
            ).fetchone()
        return self._row(row) if row else None

    def remember(self, user_id: str, key: str, scope: str, value: str, source: str,
                 evidence: str | None = None, confirmed: bool = False) -> tuple[Memory, str | None]:
        """Store a validated fact. Returns (memory, note) where note explains anything unexpected."""
        # Notes are multi-valued; every other key holds one current value per scope.
        current = [m for m in self.all(user_id) if m.status == "active" and m.key == key and m.scope == scope
                   and key not in MULTI_VALUED]
        if source == "inferred" and any(m.source == "explicit" for m in current):
            existing = next(m for m in current if m.source == "explicit")
            return existing, "an explicit fact already exists for this key; inferred value not stored"
        now = self.store.clock.now().isoformat()
        with self.store._lock, self.store._db:
            for m in current:
                self.store._db.execute(
                    "UPDATE memories SET status = 'superseded', updated_at = ? WHERE memory_id = ?", (now, m.id)
                )
            cur = self.store._db.execute(
                "INSERT INTO memories (user_id, key, scope, value, source, confirmed, status, evidence, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (user_id, key, scope, value, source, int(confirmed or source == "explicit"), evidence, now, now),
            )
        memory = self.get(user_id, cur.lastrowid)
        note = f"superseded {', '.join(m.ref for m in current)}" if current else None
        return memory, note

    def forget(self, user_id: str, ref: str | int) -> Memory | None:
        memory = self.get(user_id, ref)
        if memory is None or memory.status == "forgotten":
            return None
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "UPDATE memories SET status = 'forgotten', value = NULL, evidence = NULL, updated_at = ?"
                " WHERE memory_id = ?",
                (self.store.clock.now().isoformat(), memory.id),
            )
        return self.get(user_id, memory.id)

    def confirm(self, user_id: str, ref: str | int) -> Memory | None:
        memory = self.get(user_id, ref)
        if memory is None or memory.status != "active":
            return None
        # Confirming supersedes any other active fact for the same single-valued key and scope.
        with self.store._lock, self.store._db:
            now = self.store.clock.now().isoformat()
            if memory.key not in MULTI_VALUED:
                self.store._db.execute(
                    "UPDATE memories SET status = 'superseded', updated_at = ? WHERE user_id = ? AND key = ?"
                    " AND scope = ? AND status = 'active' AND memory_id != ?",
                    (now, user_id, memory.key, memory.scope, memory.id),
                )
            self.store._db.execute(
                "UPDATE memories SET confirmed = 1, updated_at = ? WHERE memory_id = ?", (now, memory.id)
            )
        return self.get(user_id, memory.id)

    @staticmethod
    def _row(row) -> Memory:
        return Memory(
            id=row["memory_id"], user_id=row["user_id"], key=row["key"], scope=row["scope"], value=row["value"],
            source=row["source"], confirmed=bool(row["confirmed"]), status=row["status"], evidence=row["evidence"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


def describe(memory: Memory) -> str:
    label = KEY_LABELS.get(memory.key, memory.key)
    scope = "" if memory.scope == "global" else f" ({memory.scope.replace('_', ' ')})"
    value = memory.value or ""
    if memory.key in ("preferred_provider", "avoid_provider") and value in PROVIDERS:
        value = f"{PROVIDERS[value].name} ({value})"
    return f"{memory.ref} {label}{scope}: {value}"


def render_memories(memories: list[Memory]) -> str:
    """User-facing list, written by code. Includes inferred facts the model never sees."""
    active = [m for m in memories if m.status == "active"]
    used = [m for m in active if m.usable]
    pending = [m for m in active if not m.usable]
    if not active:
        return "I don't have anything stored about you."
    lines = ["Here's what I remember:"] if used else []
    lines += [f"- {describe(m)}" + (" (confirmed)" if m.source == "inferred" else "") for m in used]
    if pending:
        lines.append("Inferred, not used until you confirm:")
        lines += [f"- {describe(m)}" for m in pending]
    lines.append('Say "forget m<number>" to remove one, or "confirm m<number>" to start using an inferred one.')
    return "\n".join(lines)
