"""Mock provider marketplace implementing the eight fixed tool contracts.

State lives in its own SQLite file, separate from the agent's database, so it
behaves like an external service: it survives agent restarts and is the source
of truth that `get_booking_status` reports from.

Results are plain JSON-able dicts: `{"ok": True, ...}` or
`{"ok": False, "error": <code>, "message": ...}`. Timeouts and 503s are raised
as `ToolTransportError` subclasses because the caller gets no answer at all.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..clock import IST, Clock, SystemClock
from .catalog import PROVIDERS, Provider, candidate_slots, normalize_area, normalize_service, reviews_for, AREAS, SERVICES
from .errors import ServiceUnavailable, ToolTimeout
from .scenario import BOOK_OUTCOMES, Scenario, draw

MAX_RANGE_DAYS = 14
MAX_SEARCH_RESULTS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    price INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL  -- active | consumed | lost
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    service TEXT NOT NULL,
    slot TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,  -- confirmed | cancelled | cancelled_by_provider | completed
    hold_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    op TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS taken_slots (
    provider_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    PRIMARY KEY (provider_id, slot)
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _ok(**fields: Any) -> dict:
    return {"ok": True, **fields}


def _err(code: str, message: str, **fields: Any) -> dict:
    return {"ok": False, "error": code, "message": message, **fields}


def parse_slot(slot: str) -> datetime:
    """Slots are "YYYY-MM-DDTHH:MM" in local IST time."""
    parsed = datetime.strptime(slot, "%Y-%m-%dT%H:%M")
    return parsed.replace(tzinfo=IST)


def _parse_date_range(date_range: Any) -> tuple[date, date]:
    if isinstance(date_range, dict):
        start, end = date_range.get("start"), date_range.get("end")
    elif isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start, end = date_range
    else:
        raise ValueError("date_range must be {start, end} or [start, end]")
    start_d = date.fromisoformat(str(start)[:10])
    end_d = date.fromisoformat(str(end)[:10])
    if end_d < start_d:
        raise ValueError("date_range end is before start")
    if (end_d - start_d).days >= MAX_RANGE_DAYS:
        raise ValueError(f"date_range may span at most {MAX_RANGE_DAYS} days")
    return start_d, end_d


class MockServices:
    TOOL_NAMES = (
        "search_providers",
        "get_provider",
        "hold_slot",
        "book",
        "get_booking_status",
        "cancel",
        "reschedule",
        "list_bookings",
    )

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        scenario: Scenario | None = None,
        clock: Clock | None = None,
        user_id: str = "u_demo",
        seed_history: bool = True,
    ) -> None:
        self.scenario = scenario or Scenario()
        self.clock = clock or SystemClock()
        self.user_id = user_id  # the account the agent acts for; holds and bookings belong to it
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        if seed_history:
            self._seed_history()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ dispatch

    def call(self, name: str, args: dict) -> dict:
        """Invoke a tool by name with keyword args. Transport errors propagate."""
        if name not in self.TOOL_NAMES:
            return _err("unknown_tool", f"no tool named {name!r}")
        fn = getattr(self, name)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as exc:
            return _err("invalid_arguments", str(exc))
        return fn(**args)

    # ------------------------------------------------------------------ tools

    def search_providers(self, service: str, area: str, date_range: Any) -> dict:
        svc = normalize_service(service)
        if svc is None:
            return _err("unknown_service", f"unsupported service {service!r}", supported=list(SERVICES))
        area_n = normalize_area(area)
        if area_n is None:
            return _err("unknown_area", f"unsupported area {area!r}", supported=list(AREAS))
        try:
            start, end = _parse_date_range(date_range)
        except ValueError as exc:
            return _err("invalid_date_range", str(exc))

        with self._lock:
            matches = [p for p in PROVIDERS.values() if p.service == svc and area_n in p.areas]
            providers = [
                {
                    "provider_id": p.provider_id,
                    "name": p.name,
                    "service": p.service,
                    "price": p.price,
                    "currency": "INR",
                    "rating": p.rating,
                    "review_count": p.review_count,
                    "available_slots": self._available_slots(p, start, end),
                    "attributes": self._attributes(p),
                }
                for p in matches[:MAX_SEARCH_RESULTS]
            ]
        return _ok(
            service=svc,
            area=area_n,
            date_range={"start": start.isoformat(), "end": end.isoformat()},
            providers=providers,
        )

    def get_provider(self, provider_id: str) -> dict:
        p = PROVIDERS.get(provider_id)
        if p is None:
            return _err("provider_not_found", f"no provider {provider_id!r}")
        return _ok(
            provider={
                "provider_id": p.provider_id,
                "name": p.name,
                "service": p.service,
                "price": p.price,
                "currency": "INR",
                "rating": p.rating,
                "review_count": p.review_count,
                "areas": list(p.areas),
                "attributes": self._attributes(p),
                "reviews": reviews_for(p.provider_id),
            }
        )

    def hold_slot(self, provider_id: str, slot: str) -> dict:
        p = PROVIDERS.get(provider_id)
        if p is None:
            return _err("provider_not_found", f"no provider {provider_id!r}")
        try:
            slot_dt = parse_slot(slot)
        except ValueError:
            return _err("invalid_slot", f"slot {slot!r} is not YYYY-MM-DDTHH:MM")

        with self._tx() as db:
            now = self.clock.now()
            existing = db.execute(
                "SELECT * FROM holds WHERE provider_id = ? AND slot = ? AND user_id = ? AND status = 'active'"
                " AND expires_at > ?",
                (provider_id, slot, self.user_id, now.isoformat()),
            ).fetchone()
            if existing:
                return _ok(**self._hold_view(existing, p), reused=True)

            if slot not in self._available_slots(p, slot_dt.date(), slot_dt.date()):
                return _err("slot_unavailable", f"{p.name} is not available at {slot}")

            price = self.scenario.hold_price_overrides.get(provider_id, p.price + self.scenario.hold_price_delta)
            hold_id = f"hold_{self._bump(db, 'hold_seq'):05d}"
            expires = now + timedelta(seconds=self.scenario.hold_ttl_seconds)
            db.execute(
                "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (hold_id, self.user_id, provider_id, slot, price, now.isoformat(), expires.isoformat()),
            )
            row = db.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
            return _ok(**self._hold_view(row, p), reused=False)

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        timeout = False
        with self._tx() as db:
            prior = self._idem_get(db, idempotency_key)
            if prior is not None:
                if prior["op"] != "book" or prior["fingerprint"] != hold_id:
                    return _err("idempotency_key_mismatch", "key was already used for a different request")
                return json.loads(prior["response"])

            hold = db.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
            if hold is None:
                return _err("hold_not_found", f"no hold {hold_id!r}")
            if hold["status"] == "consumed":
                return _err("hold_already_used", "this hold was already booked under another key")
            if hold["status"] == "lost":
                return _err("slot_taken", "the slot was taken by another customer")
            if self.clock.now() >= datetime.fromisoformat(hold["expires_at"]):
                return _err("hold_expired", "the hold expired; hold the slot again", expired_at=hold["expires_at"])

            outcome = self._draw_book_outcome(db, idempotency_key)
            if outcome == "slot_taken":
                db.execute("UPDATE holds SET status = 'lost' WHERE hold_id = ?", (hold_id,))
                db.execute("INSERT OR IGNORE INTO taken_slots VALUES (?, ?)", (hold["provider_id"], hold["slot"]))
                result = _err("slot_taken", "the slot was taken by another customer")
                self._idem_put(db, idempotency_key, "book", hold_id, result)
                return result
            if outcome == "timeout_not_booked":
                timeout = True
            else:
                booking = self._create_booking(db, hold)
                result = _ok(booking=booking)
                self._idem_put(db, idempotency_key, "book", hold_id, result)
                timeout = outcome == "timeout_booked"
        # Raised after the transaction commits, so a timed-out-but-booked call really booked.
        if timeout:
            raise ToolTimeout("book() timed out; outcome unknown")
        return result

    def get_booking_status(self, hold_id: str | None = None, idempotency_key: str | None = None) -> dict:
        if not hold_id and not idempotency_key:
            return _err("invalid_arguments", "pass hold_id or idempotency_key")
        with self._tx() as db:
            if idempotency_key:
                prior = self._idem_get(db, idempotency_key)
                if prior is not None and prior["op"] == "book":
                    response = json.loads(prior["response"])
                    if response["ok"]:
                        row = self._booking_row(db, response["booking"]["booking_id"])
                        return _ok(status=row["status"], booking=self._booking_view(row))
                    return _ok(status="failed", reason=response["error"], booking=None)
            if hold_id:
                row = db.execute("SELECT * FROM bookings WHERE hold_id = ?", (hold_id,)).fetchone()
                if row is not None:
                    return _ok(status=row["status"], booking=self._booking_view(row))
                hold = db.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
                if hold is not None:
                    if hold["status"] == "lost":
                        return _ok(status="failed", reason="slot_taken", booking=None)
                    expired = self.clock.now() >= datetime.fromisoformat(hold["expires_at"])
                    return _ok(status="not_booked", hold_status="expired" if expired else "active", booking=None)
                return _err("not_found", f"no hold {hold_id!r}")
            return _ok(status="not_booked", booking=None)

    def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        with self._tx() as db:
            prior = self._idem_get(db, idempotency_key)
            if prior is not None:
                if prior["op"] != "cancel" or prior["fingerprint"] != booking_id:
                    return _err("idempotency_key_mismatch", "key was already used for a different request")
                return json.loads(prior["response"])

            row = self._booking_row(db, booking_id)
            if row is None or row["user_id"] != self.user_id:
                return _err("booking_not_found", f"no booking {booking_id!r}")

            attempt = self._bump(db, f"cancel_attempt:{booking_id}")
            if not (self.scenario.cancel_first_call_fails and attempt == 1):
                if row["status"] == "completed":
                    return _err("booking_not_cancellable", "completed bookings cannot be cancelled")
                already = row["status"] in ("cancelled", "cancelled_by_provider")
                if not already:
                    db.execute(
                        "UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE booking_id = ?",
                        (self.clock.now().isoformat(), booking_id),
                    )
                result = _ok(booking=self._booking_view(self._booking_row(db, booking_id)), already_cancelled=already)
                self._idem_put(db, idempotency_key, "cancel", booking_id, result)
                return result
        # First attempt: the attempt counter commits, then the call fails as a transient 503.
        raise ServiceUnavailable("503 Service Unavailable: please retry")

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> dict:
        fingerprint = f"{booking_id}|{slot}"
        with self._tx() as db:
            prior = self._idem_get(db, idempotency_key)
            if prior is not None:
                if prior["op"] != "reschedule" or prior["fingerprint"] != fingerprint:
                    return _err("idempotency_key_mismatch", "key was already used for a different request")
                return json.loads(prior["response"])

            row = self._booking_row(db, booking_id)
            if row is None or row["user_id"] != self.user_id:
                return _err("booking_not_found", f"no booking {booking_id!r}")
            if row["status"] != "confirmed":
                return _err("booking_not_active", f"booking is {row['status']}")
            try:
                slot_dt = parse_slot(slot)
            except ValueError:
                return _err("invalid_slot", f"slot {slot!r} is not YYYY-MM-DDTHH:MM")

            previous = row["slot"]
            if slot != previous:
                provider = PROVIDERS[row["provider_id"]]
                free = self._available_slots(provider, slot_dt.date(), slot_dt.date())
                if self._is_conflict_slot(slot) or slot not in free:
                    return _err("slot_conflict", f"{provider.name} cannot take {slot}", requested_slot=slot)
                db.execute(
                    "UPDATE bookings SET slot = ?, updated_at = ? WHERE booking_id = ?",
                    (slot, self.clock.now().isoformat(), booking_id),
                )
            result = _ok(booking=self._booking_view(self._booking_row(db, booking_id)), previous_slot=previous)
            self._idem_put(db, idempotency_key, "reschedule", fingerprint, result)
            return result

    def list_bookings(self, user_id: str) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT * FROM bookings WHERE user_id = ? ORDER BY slot", (user_id,)).fetchall()
            now = self.clock.now()
            bookings = [
                {**self._booking_view(r), "when": "upcoming" if parse_slot(r["slot"]) > now else "past"} for r in rows
            ]
        return _ok(user_id=user_id, bookings=bookings)

    # ------------------------------------------------------------------ simulation hooks (not agent tools)

    def simulate_provider_cancellation(self, booking_id: str, reason: str = "technician unavailable") -> dict:
        """The provider cancels a confirmed booking and an event is queued for the agent to notice."""
        with self._tx() as db:
            row = self._booking_row(db, booking_id)
            if row is None or row["status"] != "confirmed":
                raise ValueError(f"booking {booking_id!r} is not confirmed")
            now = self.clock.now().isoformat()
            db.execute(
                "UPDATE bookings SET status = 'cancelled_by_provider', updated_at = ? WHERE booking_id = ?",
                (now, booking_id),
            )
            payload = {"reason": reason, "booking": self._booking_view(self._booking_row(db, booking_id))}
            cur = db.execute(
                "INSERT INTO events (user_id, type, booking_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (row["user_id"], "provider_cancelled", booking_id, json.dumps(payload), now),
            )
            return {"event_id": cur.lastrowid, "type": "provider_cancelled", "booking_id": booking_id, **payload}

    def simulate_slot_taken(self, provider_id: str, slot: str) -> None:
        """Another customer takes a slot (e.g. after our hold lapsed)."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO taken_slots VALUES (?, ?)", (provider_id, slot))
            db.execute("UPDATE holds SET status = 'lost' WHERE provider_id = ? AND slot = ? AND status = 'active'",
                       (provider_id, slot))

    def force_book_outcomes(self, outcomes: list[str]) -> None:
        """Demo hook: the next book() draws return these outcomes, in order."""
        unknown = set(outcomes) - set(BOOK_OUTCOMES)
        if unknown:
            raise ValueError(f"unknown book outcomes: {sorted(unknown)}")
        with self._lock:
            row = self._db.execute("SELECT value FROM counters WHERE name = 'book_draws'").fetchone()
            draws = row[0] if row else 0
        # Entries before `draws` are never read again; only the tail matters.
        self.scenario.book_outcomes = ("success",) * draws + tuple(outcomes)

    def fetch_events(self, user_id: str, after_id: int = 0) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE user_id = ? AND event_id > ? ORDER BY event_id", (user_id, after_id)
            ).fetchall()
        return [
            {"event_id": r["event_id"], "type": r["type"], "booking_id": r["booking_id"], **json.loads(r["payload"])}
            for r in rows
        ]

    # ------------------------------------------------------------------ internals

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:
            yield self._db

    def _bump(self, db: sqlite3.Connection, name: str) -> int:
        db.execute(
            "INSERT INTO counters VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )
        return db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]

    def _idem_get(self, db: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()

    def _idem_put(self, db: sqlite3.Connection, key: str, op: str, fingerprint: str, response: dict) -> None:
        db.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
            (key, op, fingerprint, json.dumps(response), self.clock.now().isoformat()),
        )

    def _draw_book_outcome(self, db: sqlite3.Connection, key: str) -> str:
        sc = self.scenario
        n = self._bump(db, "book_draws") - 1
        if n < len(sc.book_outcomes):
            return sc.book_outcomes[n]
        attempt = self._bump(db, f"book_attempt:{key}")
        r = draw(sc.seed, "book", key, attempt)
        if r < sc.book_success_rate:
            return "success"
        if r < sc.book_success_rate + sc.book_slot_taken_rate:
            return "slot_taken"
        booked = draw(sc.seed, "timeout", key, attempt) < sc.timeout_booked_share
        return "timeout_booked" if booked else "timeout_not_booked"

    def _create_booking(self, db: sqlite3.Connection, hold: sqlite3.Row) -> dict:
        p = PROVIDERS[hold["provider_id"]]
        booking_id = f"bk_{self._bump(db, 'booking_seq'):05d}"
        now = self.clock.now().isoformat()
        db.execute(
            "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)",
            (booking_id, hold["user_id"], p.provider_id, p.name, p.service, hold["slot"], hold["price"],
             hold["hold_id"], now, now),
        )
        db.execute("UPDATE holds SET status = 'consumed' WHERE hold_id = ?", (hold["hold_id"],))
        return self._booking_view(self._booking_row(db, booking_id))

    def _booking_row(self, db: sqlite3.Connection, booking_id: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()

    @staticmethod
    def _booking_view(row: sqlite3.Row) -> dict:
        return {
            "booking_id": row["booking_id"],
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"],
            "service": row["service"],
            "slot": row["slot"],
            "price": row["price"],
            "currency": "INR",
            "status": row["status"],
        }

    @staticmethod
    def _hold_view(row: sqlite3.Row, p: Provider) -> dict:
        return {
            "hold_id": row["hold_id"],
            "provider_id": row["provider_id"],
            "slot": row["slot"],
            "price": row["price"],
            "search_price": p.price,
            "currency": "INR",
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _attributes(p: Provider) -> dict:
        return {
            "verified": p.verified,
            "years_experience": p.years_experience,
            "languages": list(p.languages),
            "works_weekends": p.works_weekends,
            "hours": f"{p.start_hour:02d}:00-{p.end_hour:02d}:00",
        }

    def _available_slots(self, p: Provider, start: date, end: date) -> list[str]:
        now = self.clock.now()
        blocked = {
            r[0] for r in self._db.execute("SELECT slot FROM taken_slots WHERE provider_id = ?", (p.provider_id,))
        }
        blocked |= {
            r[0]
            for r in self._db.execute(
                "SELECT slot FROM bookings WHERE provider_id = ? AND status = 'confirmed'", (p.provider_id,)
            )
        }
        blocked |= {
            r[0]
            for r in self._db.execute(
                "SELECT slot FROM holds WHERE provider_id = ? AND status = 'active' AND expires_at > ?",
                (p.provider_id, now.isoformat()),
            )
        }
        return [
            s
            for s in candidate_slots(p, start, end)
            if s not in blocked
            and parse_slot(s) > now
            and draw(self.scenario.seed, "busy", p.provider_id, s) >= self.scenario.busy_rate
        ]

    def _is_conflict_slot(self, slot: str) -> bool:
        if self.scenario.conflict_slots is not None:
            return slot in self.scenario.conflict_slots
        dt = parse_slot(slot)
        return dt.weekday() == 5 and dt.hour == 10

    def _seed_history(self) -> None:
        """Past and upcoming bookings for the demo user, placed relative to today."""
        with self._tx() as db:
            if db.execute("SELECT 1 FROM bookings WHERE user_id = ? LIMIT 1", (self.user_id,)).fetchone():
                return
            today = self.clock.now().date()
            created = self.clock.now().isoformat()
            seed = [
                ("bk_seed_01", "plm_01", today - timedelta(days=40), 8, "completed"),
                ("bk_seed_02", "ele_02", today - timedelta(days=16), 10, "completed"),
                ("bk_seed_03", "acs_01", today + timedelta(days=1), 14, "confirmed"),
            ]
            for booking_id, pid, day, hour, status in seed:
                p = PROVIDERS[pid]
                db.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (booking_id, self.user_id, pid, p.name, p.service, f"{day.isoformat()}T{hour:02d}:00",
                     p.price, status, created, created),
                )
