"""Agent-side durable state: sessions, approvals, and the side-effect outbox."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .clock import Clock
from .state import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending | approved | consumed | invalidated
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Written before every side-effecting call, keyed by its idempotency key, so a
-- restart can find in-flight work and reconcile it against the provider.
CREATE TABLE IF NOT EXISTS outbox (
    key TEXT PRIMARY KEY,
    op TEXT NOT NULL,
    session_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending | done | failed | unknown
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Long-lived user facts. Forgotten rows keep their key for audit but lose their value.
CREATE TABLE IF NOT EXISTS memories (
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    scope TEXT NOT NULL,  -- global | <service>
    value TEXT,
    source TEXT NOT NULL,  -- explicit | inferred
    confirmed INTEGER NOT NULL,
    status TEXT NOT NULL,  -- active | superseded | forgotten
    evidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

OPEN_OUTBOX = ("pending", "unknown")


class AgentStore:
    def __init__(self, path: str | Path, clock: Clock) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _now(self) -> str:
        return self.clock.now().isoformat()

    def next_id(self, prefix: str) -> str:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO counters VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1", (prefix,)
            )
            n = self._db.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()[0]
        return f"{prefix}_{n:04d}"

    # ---------------------------------------------------------------- sessions

    def load_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return Session.from_json(row["state"]) if row else None

    def save_session(self, session: Session) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
                " state = excluded.state, updated_at = excluded.updated_at",
                (session.session_id, session.user_id, session.to_json(), self._now()),
            )

    def sessions_for_user(self, user_id: str) -> list[Session]:
        """Most recently updated first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT state FROM sessions WHERE user_id = ? ORDER BY updated_at DESC, rowid DESC", (user_id,)
            ).fetchall()
        return [Session.from_json(r["state"]) for r in rows]

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value)
            )

    # ---------------------------------------------------------------- approvals

    def insert_approval(self, approval_id: str, session_id: str, action: dict, fingerprint: str) -> dict:
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
                (approval_id, session_id, json.dumps(action), fingerprint, now, now),
            )
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "action": json.loads(row["action"])}

    def approvals_for(self, session_id: str, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._db.execute(
                f"SELECT approval_id FROM approvals WHERE session_id = ? AND status IN ({marks})",
                (session_id, *statuses),
            ).fetchall()
        return [self.get_approval(r["approval_id"]) for r in rows]

    def update_approval(self, approval_id: str, **fields: object) -> None:
        if "action" in fields:
            fields["action"] = json.dumps(fields["action"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE approvals SET {sets}, updated_at = ? WHERE approval_id = ?",
                (*fields.values(), self._now(), approval_id),
            )

    # ---------------------------------------------------------------- outbox

    def outbox_insert(self, key: str, op: str, session_id: str, approval_id: str, args: dict) -> None:
        """Record intent before the side effect. A no-op if the key is already recorded."""
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
                (key, op, session_id, approval_id, json.dumps(args), now, now),
            )

    def outbox_set(self, key: str, status: str, result: dict | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE outbox SET status = ?, result = ?, updated_at = ? WHERE key = ?",
                (status, json.dumps(result) if result is not None else None, self._now(), key),
            )

    def outbox_get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM outbox WHERE key = ?", (key,)).fetchone()
        return self._outbox_row(row) if row else None

    def outbox_open(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE status IN ('pending', 'unknown') ORDER BY created_at"
            ).fetchall()
        return [self._outbox_row(r) for r in rows]

    def outbox_for_approval(self, approval_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE approval_id = ? ORDER BY created_at", (approval_id,)
            ).fetchall()
        return [self._outbox_row(r) for r in rows]

    @staticmethod
    def _outbox_row(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["args"] = json.loads(out["args"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out
