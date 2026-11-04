"""Runs approved side effects: book, cancel, reschedule.

Rules this module enforces:
- Every side-effecting call is preceded by `ApprovalGate.authorize` with the exact action.
- Intent is written to the outbox before the call, under a deterministic idempotency key,
  so retries, repeated confirmations, and restarts all reuse the same key.
- Success is reported only after the provider's source of truth confirms it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .approval import ApprovalGate, format_slot, rupees
from .limits import Limits
from .mocks import MockServices, ToolTimeout, ToolTransportError
from .store import AgentStore
from .trace import Tracer

CANCELLED = ("cancelled", "cancelled_by_provider")


@dataclass
class Outcome:
    kind: str  # booked | cancelled | rescheduled | reapprove | failed | unknown
    message: str
    booking: dict | None = None
    new_action: dict | None = None  # for "reapprove": the changed action needing fresh consent
    retry_options: bool = field(default=False)  # user should pick another option


class Executor:
    def __init__(
        self,
        services: MockServices,
        store: AgentStore,
        gate: ApprovalGate,
        limits: Limits,
        user_id: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.services = services
        self.store = store
        self.gate = gate
        self.limits = limits
        self.user_id = user_id
        self.sleep = sleep

    # ---------------------------------------------------------------- entry points

    def execute(self, approval: dict, tr: Tracer) -> Outcome:
        kind = approval["action"]["type"]
        handler = {"book": self._book, "cancel": self._cancel, "reschedule": self._reschedule}[kind]
        return handler(approval, tr)

    def resume(self, row: dict, tr: Tracer) -> Outcome | None:
        """Reconcile an outbox row left pending or unknown (e.g. by a crash). None means still unknown."""
        approval = self.store.get_approval(row["approval_id"])
        tr.event("reconcile_start", key=row["key"], op=row["op"], outbox_status=row["status"],
                 approval_status=approval["status"])
        if row["op"] != "book":
            if approval["status"] != "approved":
                self.store.outbox_set(row["key"], "failed", {"error": "approval_not_active"})
                return None
            return self.execute(approval, tr)  # same deterministic key, so the provider dedupes

        action = approval["action"]
        status = self._book_status(tr, row["key"], row["args"]["hold_id"])
        if status is None:
            return None
        if status["status"] == "confirmed":
            return self._booked(tr, approval["approval_id"], row["key"], status["booking"])
        if status["status"] == "failed":
            return self._slot_taken(tr, approval["approval_id"], row["key"], action)
        if approval["status"] == "approved" and status.get("hold_status") == "active":
            return self._drive_book(tr, approval["approval_id"], action, row["key"])
        self.store.outbox_set(row["key"], "failed", {"error": "interrupted"})
        self.gate.invalidate(approval["approval_id"], "interrupted")
        return Outcome(
            "failed",
            f"Your booking with {action['provider_name']} for {format_slot(action['slot'])} was interrupted "
            "and did not go through. Nothing was booked. Want me to try again?",
            retry_options=True,
        )

    # ---------------------------------------------------------------- booking

    def _book(self, approval: dict, tr: Tracer) -> Outcome:
        aid = approval["approval_id"]
        action = dict(approval["action"])
        if self.services.clock.now() >= datetime.fromisoformat(action["hold_expires_at"]):
            renewed = self._rehold(tr, aid, action)
            if isinstance(renewed, Outcome):
                return renewed
            action = renewed
        key = f"book:{aid}:{action['hold_id']}"
        self.store.outbox_insert(key, "book", approval["session_id"], aid, {"hold_id": action["hold_id"]})
        tr.event("outbox_write", key=key, op="book")
        return self._drive_book(tr, aid, action, key)

    def _drive_book(self, tr: Tracer, aid: str, action: dict, key: str) -> Outcome:
        for attempt in range(1, self.limits.max_attempts + 1):
            try:
                res = self._effect(tr, aid, action, "book", hold_id=action["hold_id"], idempotency_key=key)
            except ToolTimeout:
                tr.event("book_timeout", key=key, attempt=attempt)
                status = self._book_status(tr, key, action["hold_id"])
                if status is None:
                    continue  # unknown; retrying with the same key is safe
                if status["status"] == "confirmed":
                    return self._booked(tr, aid, key, status["booking"])
                if status["status"] == "failed":
                    return self._slot_taken(tr, aid, key, action)
                tr.event("book_retry", key=key, reason="status says not booked")
                continue
            except ToolTransportError as exc:
                tr.event("book_retry", key=key, reason=exc.code)
                self._backoff(attempt)
                continue

            if res["ok"]:
                status = self._book_status(tr, key, action["hold_id"])
                if status and status["status"] == "confirmed":
                    return self._booked(tr, aid, key, status["booking"])
                self.store.outbox_set(key, "unknown", res)
                return self._unverified(action)
            if res["error"] == "slot_taken":
                return self._slot_taken(tr, aid, key, action)
            if res["error"] == "hold_expired":
                self.store.outbox_set(key, "failed", res)
                renewed = self._rehold(tr, aid, action)
                if isinstance(renewed, Outcome):
                    return renewed
                action = renewed
                key = f"book:{aid}:{action['hold_id']}"
                self.store.outbox_insert(key, "book", self.store.get_approval(aid)["session_id"], aid,
                                         {"hold_id": action["hold_id"]})
                continue
            self.store.outbox_set(key, "failed", res)
            self.gate.invalidate(aid, res["error"])
            return Outcome("failed", f"The booking didn't go through ({res['error']}). Nothing was booked.",
                           retry_options=True)

        status = self._book_status(tr, key, action["hold_id"])
        if status and status["status"] == "confirmed":
            return self._booked(tr, aid, key, status["booking"])
        if status and status["status"] == "not_booked":
            self.store.outbox_set(key, "failed", {"error": "attempts_exhausted"})
            self.gate.invalidate(aid, "attempts_exhausted")
            return Outcome(
                "failed",
                f"I tried {self.limits.max_attempts} times and the provider didn't respond. I checked, and nothing "
                "was booked. Want me to try again?",
                retry_options=True,
            )
        self.store.outbox_set(key, "unknown")
        return self._unverified(action)

    def _rehold(self, tr: Tracer, aid: str, action: dict) -> dict | Outcome:
        """The hold lapsed. Hold again; any material change voids the approval."""
        when = format_slot(action["slot"])
        try:
            res = self.call_tool(tr, "hold_slot", provider_id=action["provider_id"], slot=action["slot"])
        except ToolTransportError:
            res = {"ok": False, "error": "unavailable"}
        if not res["ok"]:
            self.gate.invalidate(aid, "slot_lost")
            return Outcome(
                "failed",
                f"The hold on {when} with {action['provider_name']} expired and the slot is no longer available. "
                "Nothing was booked. Want me to look for another time?",
                retry_options=True,
            )
        if res["price"] != action["price"]:
            self.gate.invalidate(aid, "price_changed")
            tr.event("approval_invalidated", approval_id=aid, reason="price_changed",
                     old_price=action["price"], new_price=res["price"])
            new_action = {**action, "price": res["price"], "hold_id": res["hold_id"],
                          "hold_expires_at": res["expires_at"], "previous_price": action["price"]}
            return Outcome(
                "reapprove",
                f"The price changed from {rupees(action['price'])} to {rupees(res['price'])} when I renewed the "
                "hold, so I need you to confirm again.",
                new_action=new_action,
            )
        renewed = {**action, "hold_id": res["hold_id"], "hold_expires_at": res["expires_at"]}
        self.gate.update_nonmaterial(aid, renewed)
        return renewed

    def _booked(self, tr: Tracer, aid: str, key: str, booking: dict) -> Outcome:
        self.store.outbox_set(key, "done", booking)
        self.gate.consume(aid)
        tr.event("verified", op="book", booking_id=booking["booking_id"], status=booking["status"])
        return Outcome(
            "booked",
            f"Booked and verified: {booking['provider_name']}, {format_slot(booking['slot'])}, "
            f"{rupees(booking['price'])}. Booking ID {booking['booking_id']}.",
            booking=booking,
        )

    def _slot_taken(self, tr: Tracer, aid: str, key: str, action: dict) -> Outcome:
        self.store.outbox_set(key, "failed", {"error": "slot_taken"})
        self.gate.invalidate(aid, "slot_taken")
        return Outcome(
            "failed",
            f"Someone else took the {format_slot(action['slot'])} slot with {action['provider_name']} just before "
            "I could book it. Nothing was booked. Want me to pick another slot?",
            retry_options=True,
        )

    @staticmethod
    def _unverified(action: dict) -> Outcome:
        return Outcome(
            "unknown",
            f"I couldn't confirm whether the booking with {action['provider_name']} for "
            f"{format_slot(action['slot'])} went through, so I'm not reporting it as booked. I'll check its status "
            "again before doing anything else.",
        )

    def _book_status(self, tr: Tracer, key: str, hold_id: str) -> dict | None:
        """Ask the source of truth. The hold id covers attempts that never reached the provider."""
        for attempt in range(1, self.limits.max_attempts + 1):
            try:
                res = self.call_tool(tr, "get_booking_status", idempotency_key=key, hold_id=hold_id)
            except ToolTransportError:
                self._backoff(attempt)
                continue
            if res["ok"]:
                return res
        return None

    # ---------------------------------------------------------------- cancel / reschedule

    def _cancel(self, approval: dict, tr: Tracer) -> Outcome:
        aid, action = approval["approval_id"], approval["action"]
        key = f"cancel:{aid}"
        self.store.outbox_insert(key, "cancel", approval["session_id"], aid, {"booking_id": action["booking_id"]})
        tr.event("outbox_write", key=key, op="cancel")
        for attempt in range(1, self.limits.max_attempts + 1):
            try:
                res = self._effect(tr, aid, action, "cancel", booking_id=action["booking_id"], idempotency_key=key)
            except ToolTransportError as exc:
                tr.event("cancel_retry", key=key, attempt=attempt, reason=exc.code)
                self._backoff(attempt)
                continue
            if not res["ok"]:
                self.store.outbox_set(key, "failed", res)
                self.gate.invalidate(aid, res["error"])
                return Outcome("failed", f"I couldn't cancel {action['booking_id']} ({res['error']}). It is unchanged.")
            break
        booking = self._find_booking(tr, action["booking_id"])
        if booking and booking["status"] in CANCELLED:
            self.store.outbox_set(key, "done", booking)
            self.gate.consume(aid)
            tr.event("verified", op="cancel", booking_id=booking["booking_id"], status=booking["status"])
            return Outcome(
                "cancelled",
                f"Cancelled and verified: {booking['provider_name']} on {format_slot(booking['slot'])} "
                f"(booking {booking['booking_id']}).",
                booking=booking,
            )
        self.store.outbox_set(key, "unknown")
        return Outcome(
            "unknown",
            f"I couldn't confirm the cancellation of {action['booking_id']}, so treat it as still active for now. "
            "I'll check again before doing anything else.",
        )

    def _reschedule(self, approval: dict, tr: Tracer) -> Outcome:
        aid, action = approval["approval_id"], approval["action"]
        key = f"reschedule:{aid}"
        self.store.outbox_insert(key, "reschedule", approval["session_id"], aid,
                                 {"booking_id": action["booking_id"], "slot": action["slot"]})
        tr.event("outbox_write", key=key, op="reschedule")
        for attempt in range(1, self.limits.max_attempts + 1):
            try:
                res = self._effect(tr, aid, action, "reschedule", booking_id=action["booking_id"],
                                   slot=action["slot"], idempotency_key=key)
            except ToolTransportError as exc:
                tr.event("reschedule_retry", key=key, attempt=attempt, reason=exc.code)
                self._backoff(attempt)
                continue
            if not res["ok"]:
                self.store.outbox_set(key, "failed", res)
                self.gate.invalidate(aid, res["error"])
                if res["error"] == "slot_conflict":
                    return Outcome(
                        "failed",
                        f"{action['provider_name']} can't do {format_slot(action['slot'])}. Your booking is unchanged "
                        f"at {format_slot(action['from_slot'])}. Want me to find another time?",
                        retry_options=True,
                    )
                return Outcome("failed", f"I couldn't reschedule ({res['error']}). Your booking is unchanged.")
            break
        booking = self._find_booking(tr, action["booking_id"])
        if booking and booking["status"] == "confirmed" and booking["slot"] == action["slot"]:
            self.store.outbox_set(key, "done", booking)
            self.gate.consume(aid)
            tr.event("verified", op="reschedule", booking_id=booking["booking_id"], slot=booking["slot"])
            return Outcome(
                "rescheduled",
                f"Rescheduled and verified: {booking['provider_name']} moved to {format_slot(booking['slot'])} "
                f"(booking {booking['booking_id']}).",
                booking=booking,
            )
        self.store.outbox_set(key, "unknown")
        return Outcome(
            "unknown",
            f"I couldn't confirm the move of {action['booking_id']}, so treat it as unchanged for now. "
            "I'll check again before doing anything else.",
        )

    # ---------------------------------------------------------------- plumbing

    def _effect(self, tr: Tracer, aid: str, action: dict, name: str, **args: object) -> dict:
        """The only path to a side-effecting tool: authorize, then call."""
        self.gate.authorize(aid, action)
        tr.event("approval_check", approval_id=aid, tool=name, ok=True)
        return self.call_tool(tr, name, **args)

    def call_tool(self, tr: Tracer, name: str, **args: object) -> dict:
        start = time.perf_counter()
        try:
            res = self.services.call(name, args)
        except ToolTransportError as exc:
            tr.tool_call(name, args, (time.perf_counter() - start) * 1000, source="app", error=exc.code)
            raise
        tr.tool_call(name, args, (time.perf_counter() - start) * 1000, source="app", result=res)
        return res

    def _find_booking(self, tr: Tracer, booking_id: str) -> dict | None:
        for attempt in range(1, self.limits.max_attempts + 1):
            try:
                res = self.call_tool(tr, "list_bookings", user_id=self.user_id)
            except ToolTransportError:
                self._backoff(attempt)
                continue
            return next((b for b in res.get("bookings", []) if b["booking_id"] == booking_id), None)
        return None

    def _backoff(self, attempt: int) -> None:
        self.sleep(self.limits.backoff_seconds * 2 ** (attempt - 1))
