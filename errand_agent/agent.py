"""The orchestrator: routes each user message, runs the bounded model loop, and applies outcomes."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from .approval import SERVICE_LABELS, ApprovalGate, classify_reply, fingerprint, format_slot, render_prompt, rupees
from .clock import Clock, SystemClock
from .context import ContextBudget, history_window, request_tokens, select_memories, services_mentioned
from .executor import Executor, Outcome
from .limits import LIMIT_MESSAGES, Budget, LimitExceeded, Limits
from .memory import Memory, MemoryStore, evidence_in, render_memories, validate_memory
from .mocks import MockServices, ToolTransportError
from .mocks.service import parse_slot
from .model import Model, ToolCall, estimate_cost
from .proactive import MAX_HOLD_ATTEMPTS, REPLACEMENT_WINDOW_DAYS, rank_replacements, resolve_preferences
from .prompts import SYSTEM_PROMPT, state_block, state_view
from .state import SEARCH_FIELDS, Phase, Session, is_valid_slot, validate_request_changes
from .store import AgentStore
from .tool_specs import MEMORY_TOOLS, MODEL_TOOLS, PROPOSAL_TOOLS, READ_TOOLS
from .trace import Tracer
from .untrusted import to_model_view

MEMORY_COMMAND = re.compile(r"^\s*(confirm|forget)\s+(m\d+)\s*[.!]?\s*$", re.IGNORECASE)
SUCCESS_KINDS = ("booked", "cancelled", "rescheduled")

ERROR_MESSAGE = (
    "Sorry, I hit an unexpected problem and stopped. I haven't confirmed any booking change in this turn; "
    "if one was in progress I'll verify it before doing anything else."
)
EMPTY_REPLY = "Sorry, I lost my train of thought there. Could you say that again?"
UNVERIFIED_CLAIM = re.compile(
    r"\b(i('ve| have) (booked|cancelled|canceled|rescheduled|moved)|"
    r"(booking|appointment) (is|has been) (confirmed|booked|cancelled|canceled|rescheduled)|"
    r"you'?re (all )?(booked|set))\b",
    re.IGNORECASE,
)
CLAIM_REPLACEMENT = "Just to be clear: nothing has been booked or changed yet."
REFUSAL_MESSAGE = "Sorry, I can't help with that. I can book, reschedule, or cancel home-service visits."


@dataclass
class Notification:
    """A message the agent sends without being asked."""

    session_id: str
    text: str
    kind: str  # replacement_proposed | notify_only | no_replacement
    run_id: str


@dataclass
class AgentReply:
    text: str
    outcome: str  # replied | awaiting_approval | booked | cancelled | rescheduled | failed | unknown | noop | refused | limit_exceeded | error
    run_id: str
    trace_path: str | None


class Agent:
    def __init__(
        self,
        *,
        model: Model,
        services: MockServices,
        store: AgentStore,
        clock: Clock | None = None,
        limits: Limits | None = None,
        trace_dir: str | Path | None = None,
        context_budget: ContextBudget | None = None,
        user_id: str = "u_demo",
        default_session: str = "demo",
        trace_sink: Callable[[dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.services = services
        self.store = store
        self.clock = clock or SystemClock()
        self.limits = limits or Limits()
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.user_id = user_id
        self.default_session = default_session
        self.trace_sink = trace_sink
        self.monotonic = monotonic
        # handle() and poll_events() may run on different threads (chat input vs event poller).
        self._lock = threading.RLock()
        self.context_budget = context_budget or ContextBudget()
        self.memory = MemoryStore(store)
        self.gate = ApprovalGate(store)
        # Per-turn scratch: the user's words (for memory evidence) and code-rendered text to show.
        self._turn_text = ""
        self._turn_display: list[str] = []
        self.executor = Executor(services, store, self.gate, self.limits, user_id, sleep)
        self.reconcile()

    # ---------------------------------------------------------------- public

    def handle(self, session_id: str, text: str) -> AgentReply:
        with self._lock:
            return self._handle(session_id, text)

    def _handle(self, session_id: str, text: str) -> AgentReply:
        self.reconcile()
        session = self._load(session_id)
        tracer = self._tracer(session_id)
        budget = Budget(self.limits, self.monotonic)
        tracer.event("run_start", user_message=text, phase=session.phase)
        notices, session.notices = session.notices, []
        try:
            body, outcome, kind = self._route(session, text, tracer, budget)
        except LimitExceeded as exc:
            tracer.event("limit_exceeded", limit=exc.limit, model_calls=budget.model_calls,
                         tool_calls=budget.tool_calls, elapsed_s=round(budget.elapsed(), 3))
            body, outcome, kind = LIMIT_MESSAGES[exc.limit], "limit_exceeded", "text"
        except Exception as exc:  # every exit path still answers the user
            tracer.event("error", error=repr(exc))
            body, outcome, kind = ERROR_MESSAGE, "error", "text"

        reply = "\n\n".join([*notices, body])
        session.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        session.last_assistant_kind = kind
        calls = tracer.of_kind("model_call")
        tracer.event("run_end", outcome=outcome, phase=session.phase, reply=reply, model_calls=budget.model_calls,
                     tool_calls=len(tracer.of_kind("tool_call")), model_tool_calls=budget.tool_calls,
                     elapsed_s=round(budget.elapsed(), 3),
                     input_tokens=sum(c["input_tokens"] for c in calls),
                     output_tokens=sum(c["output_tokens"] for c in calls),
                     cache_read_tokens=sum(c["cache_read_tokens"] for c in calls),
                     cost_usd=round(sum(c["cost_usd"] for c in calls), 6))
        self.store.save_session(session)
        return AgentReply(reply, outcome, tracer.run_id, str(tracer.path) if tracer.path else None)

    def reconcile(self) -> None:
        """Resolve side effects left in flight by a crash or an unverified outcome."""
        for row in self.store.outbox_open():
            session = self._load(row["session_id"])
            tracer = self._tracer(row["session_id"], prefix="reconcile")
            outcome = self.executor.resume(row, tracer)
            if outcome is None:
                continue
            message, _, _ = self._apply_outcome(session, outcome, tracer)
            session.notices.append(message)
            self.store.save_session(session)

    # ---------------------------------------------------------------- proactive follow-up

    def poll_events(self) -> list[Notification]:
        """Check the provider's event feed and act on anything new. Needs no user message."""
        with self._lock:
            cursor_key = f"event_cursor:{self.user_id}"
            cursor = int(self.store.get_kv(cursor_key, "0"))
            notifications = []
            for event in self.services.fetch_events(self.user_id, after_id=cursor):
                note = self._on_event(event)
                self.store.set_kv(cursor_key, str(event["event_id"]))
                if note:
                    notifications.append(note)
            return notifications

    def _on_event(self, event: dict) -> Notification | None:
        if event["type"] != "provider_cancelled":
            return None
        booking = event["booking"]
        session = self._session_for_booking(booking["booking_id"])
        tracer = self._tracer(session.session_id, prefix="proactive")
        tracer.event("event_received", event_id=event["event_id"], type=event["type"],
                     booking=booking, reason=event.get("reason"))
        now = self.clock.now()
        when = parse_slot(booking["slot"])
        if when <= now:
            tracer.event("proactive_decision", notify=False, reason="booking time has already passed")
            return None
        urgent = when - now < timedelta(hours=24)
        session.known_bookings[booking["booking_id"]] = booking

        service = SERVICE_LABELS.get(booking["service"], booking["service"])
        head = (f"Heads up: {booking['provider_name']} cancelled your {service} visit on "
                f"{format_slot(booking['slot'])} ({event.get('reason', 'no reason given')}). "
                "Nothing is booked in its place yet.")
        if session.pending_approval_id:
            # Don't silently replace a confirmation the user is still looking at.
            tracer.event("proactive_decision", notify=True, propose=False, urgent=urgent, channel="chat",
                         reason="another confirmation is pending")
            text = head + ' You have another confirmation waiting, so I haven\'t picked a replacement yet. ' \
                          'Say "find a replacement" when you\'re ready.'
            return self._notify(session, tracer, text, "notify_only", "text")

        tracer.event("proactive_decision", notify=True, propose=True, urgent=urgent, channel="chat",
                     reason="upcoming booking lost; look for a replacement within saved preferences")
        found = self._find_replacement(session, booking, tracer)
        if isinstance(found, str):
            text = f"{head} I couldn't find a replacement that fits your saved preferences ({found}). " \
                   "Want me to look more widely, for example other days or a higher budget?"
            return self._notify(session, tracer, text, "no_replacement", "text")
        action, prefs_text = found
        prompt = self._open_approval(session, action, tracer)
        text = f"{head} I found a replacement that fits your saved preferences ({prefs_text})."
        if action["price"] != booking["price"]:
            more_or_less = "more" if action["price"] > booking["price"] else "less"
            text += (f" It costs {rupees(abs(action['price'] - booking['price']))} {more_or_less} than the "
                     f"original ({rupees(booking['price'])}).")
        text += f"\n\n{prompt}"
        return self._notify(session, tracer, text, "replacement_proposed", "approval_prompt")

    def _find_replacement(self, session: Session, booking: dict, tracer: Tracer) -> tuple[dict, str] | str:
        """Returns (action, preferences text) for the best held replacement, or why there is none."""
        service = booking["service"]
        prefs = resolve_preferences(self.memory.all(self.user_id), service, session.request)
        if not prefs.area:
            tracer.event("replacement_search", skipped="no saved area")
            return "I don't know which area to search"
        today = self.clock.now().date()
        day = parse_slot(booking["slot"]).date()
        start = max(day, today)
        end = start + timedelta(days=REPLACEMENT_WINDOW_DAYS)
        try:
            res = self.executor.call_tool(tracer, "search_providers", service=service, area=prefs.area,
                                          date_range={"start": start.isoformat(), "end": end.isoformat()})
        except ToolTransportError:
            return "the provider search is unavailable right now"
        if not res["ok"]:
            return res.get("message", "search failed")
        self._absorb(session, "search_providers", res, tracer)
        session.request.update({
            "task": "book", "service": service, "area": prefs.area, "date_start": start.isoformat(),
            "date_end": end.isoformat(), "earliest_time": prefs.earliest_time, "latest_time": prefs.latest_time,
            "budget_max": prefs.budget_max,
        })
        ranked = rank_replacements(res["providers"], booking, prefs, self.clock.now())
        tracer.event("replacement_search", preferences=prefs.describe(), memories_used=prefs.used,
                     options=len(ranked), top=[(p["provider_id"], slot) for p, slot in ranked[:3]])
        for provider, slot in ranked[:MAX_HOLD_ATTEMPTS]:
            try:
                hold = self.executor.call_tool(tracer, "hold_slot", provider_id=provider["provider_id"], slot=slot)
            except ToolTransportError:
                continue
            if not hold["ok"] or (prefs.budget_max and hold["price"] > prefs.budget_max):
                continue
            action = self._book_action(session.candidates[provider["provider_id"]], provider["provider_id"],
                                       slot, hold)
            action["replaces_booking_id"] = booking["booking_id"]
            return action, prefs.describe()
        return prefs.describe()

    def _notify(self, session: Session, tracer: Tracer, text: str, kind: str, assistant_kind: str) -> Notification:
        session.history.append({"role": "assistant", "content": text})
        session.last_assistant_kind = assistant_kind
        tracer.event("notification", notification=kind, text=text)
        self.store.save_session(session)
        return Notification(session.session_id, text, kind, tracer.run_id)

    def _session_for_booking(self, booking_id: str) -> Session:
        """The conversation that knows this booking, else the user's most recent one."""
        sessions = self.store.sessions_for_user(self.user_id)
        for session in sessions:
            if booking_id in session.known_bookings:
                return session
        return sessions[0] if sessions else self._load(self.default_session)

    # ---------------------------------------------------------------- routing

    def _route(self, session: Session, text: str, tracer: Tracer, budget: Budget) -> tuple[str, str, str]:
        intent = classify_reply(text)
        pending = session.pending_approval_id
        tracer.event("route", intent=intent, pending_approval=pending, last_assistant=session.last_assistant_kind)

        if pending and intent == "affirm" and session.last_assistant_kind == "approval_prompt":
            return self._confirm(session, tracer)
        command = MEMORY_COMMAND.match(text)
        if command:
            return self._memory_command(command.group(1).lower(), command.group(2), tracer)
        # A repeated "yes" after a completed action must not redo it. After a failure the outcome
        # message asks a question ("pick another slot?"), so a yes there goes to the model instead.
        last = session.outcomes[-1] if session.outcomes else None
        if (not pending and intent == "affirm" and session.last_assistant_kind == "outcome"
                and last and last["kind"] in SUCCESS_KINDS):
            tracer.event("repeat_confirmation_ignored")
            return f"There's nothing waiting for your confirmation, so I haven't done anything new. {last['message']}", \
                "noop", "outcome"
        if pending and intent == "decline":
            self._drop_pending(session, tracer, "user_declined")
        return self._model_loop(session, text, tracer, budget)

    def _confirm(self, session: Session, tracer: Tracer) -> tuple[str, str, str]:
        approval = self.gate.approve(session.pending_approval_id)
        tracer.event("approval_granted", approval_id=approval["approval_id"], action=approval["action"],
                     fingerprint=approval["fingerprint"])
        session.pending_approval_id = None
        self._phase(session, Phase.EXECUTING, tracer, "user confirmed")
        outcome = self.executor.execute(approval, tracer)
        return self._apply_outcome(session, outcome, tracer)

    def _apply_outcome(self, session: Session, outcome: Outcome, tracer: Tracer) -> tuple[str, str, str]:
        tracer.event("outcome", result=outcome.kind, message=outcome.message, booking=outcome.booking)
        if outcome.kind == "reapprove":
            prompt = self._open_approval(session, outcome.new_action, tracer)
            return f"{outcome.message}\n\n{prompt}", "awaiting_approval", "approval_prompt"
        session.outcomes.append({"kind": outcome.kind, "message": outcome.message, "booking": outcome.booking})
        if outcome.booking:
            session.known_bookings[outcome.booking["booking_id"]] = outcome.booking
        if outcome.kind in SUCCESS_KINDS:
            self._phase(session, Phase.DONE, tracer, outcome.kind)
        elif outcome.kind == "unknown":
            self._phase(session, Phase.UNVERIFIED, tracer, "outcome not verified")
        else:
            self._phase(session, Phase.PRESENTING if session.candidates else Phase.GATHERING, tracer, outcome.kind)
        return outcome.message, outcome.kind, "outcome"

    # ---------------------------------------------------------------- model loop

    def _model_loop(self, session: Session, text: str, tracer: Tracer, budget: Budget) -> tuple[str, str, str]:
        # Append-only within a turn: the system prompt and tools never change, assistant content is
        # replayed verbatim (thinking blocks included), and state updates are appended, never edited in.
        self._turn_text, self._turn_display = text, []
        cb = self.context_budget
        history, omitted = history_window(session.history, cb.history_tokens, cb.history_chunk)
        memories, excluded = self._relevant_memories(session, text)
        state = state_block(session, self.clock.now(), memories, omitted)
        messages = history + [
            {"role": "user", "content": [{"type": "text", "text": state}, {"type": "text", "text": text}]}
        ]
        tracer.event(
            "context_manifest",
            budget={"max_request_tokens": cb.max_request_tokens, "memory_tokens": cb.memory_tokens,
                    "history_tokens": cb.history_tokens},
            tokens={"system": request_tokens(SYSTEM_PROMPT, [], []), "tools": request_tokens("", MODEL_TOOLS, []),
                    "history": request_tokens("", [], history), "state": request_tokens(state, [], []),
                    "user": request_tokens(text, [], [])},
            memories_included=[m.view() for m in memories],
            memories_excluded=excluded,
            history_window={"start": omitted, "messages": len(history), "omitted": omitted},
            state=state_view(session, memories, omitted),
        )
        prompt: str | None = None
        while True:
            budget.charge_model()
            est = request_tokens(SYSTEM_PROMPT, MODEL_TOOLS, messages)
            if est > cb.max_request_tokens:
                tracer.event("context_over_budget", request_tokens_est=est, max=cb.max_request_tokens)
                raise LimitExceeded("context_budget")
            start = time.perf_counter()
            resp = self.model.complete(system=SYSTEM_PROMPT, messages=messages, tools=MODEL_TOOLS)
            u = resp.usage
            tracer.event("model_call", model=resp.model, latency_ms=round((time.perf_counter() - start) * 1000, 2),
                         request_tokens_est=est, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                         cache_read_tokens=u.cache_read_input_tokens, cache_write_tokens=u.cache_creation_input_tokens,
                         cost_usd=round(estimate_cost(resp.model, u), 6), stop_reason=resp.stop_reason,
                         text=resp.text, tool_calls=[{"name": c.name, "args": c.args} for c in resp.tool_calls])
            if resp.stop_reason == "refusal":
                tracer.event("model_refused")
                return REFUSAL_MESSAGE, "refused", "text"
            if not resp.tool_calls or resp.stop_reason not in (None, "tool_use"):
                break
            messages.append({"role": "assistant",
                             "content": resp.content or self._assistant_blocks(resp.text, resp.tool_calls)})
            results: list[dict] = []
            for call in resp.tool_calls:
                if prompt is not None:
                    result = {"ok": False, "error": "skipped", "message": "A confirmation is pending; end your turn."}
                else:
                    budget.charge_tool()
                    result, prompt = self._dispatch(session, call, tracer)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(result, ensure_ascii=False)})
            memories, _ = self._relevant_memories(session, text)
            new_state = state_block(session, self.clock.now(), memories, omitted)
            if new_state != state:
                results.append({"type": "text", "text": new_state})
                state = new_state
            messages.append({"role": "user", "content": results})
            if prompt is not None:
                break

        text_out = resp.text.strip()
        if prompt is not None:
            return "\n\n".join(p for p in (text_out, *self._turn_display, prompt) if p), \
                "awaiting_approval", "approval_prompt"
        if UNVERIFIED_CLAIM.search(text_out):
            tracer.event("unverified_claim_blocked", text=text_out)
            text_out = CLAIM_REPLACEMENT
        parts = [p for p in (text_out, *self._turn_display) if p]
        return "\n\n".join(parts) or EMPTY_REPLY, "replied", "text"

    def _dispatch(self, session: Session, call: ToolCall, tracer: Tracer) -> tuple[dict, str | None]:
        name, args = call.name, dict(call.args)
        if name in READ_TOOLS:
            return self._read_tool(session, name, args, tracer), None
        if name == "update_request":
            return self._update_request(session, args, tracer), None
        if name in PROPOSAL_TOOLS:
            return getattr(self, f"_{name}")(session, args, tracer)
        if name in MEMORY_TOOLS:
            return getattr(self, f"_{name}")(session, args, tracer), None
        tracer.event("blocked_tool_call", tool=name, args=args)
        return {
            "ok": False,
            "error": "tool_not_available",
            "message": "Side effects run only after the user confirms. Use propose_booking, propose_reschedule, "
                       "or propose_cancel.",
        }, None

    def _read_tool(self, session: Session, name: str, args: dict, tracer: Tracer) -> dict:
        if name == "list_bookings":
            args = {"user_id": self.user_id}  # never trust a model-chosen account
        start = time.perf_counter()
        try:
            result = self.services.call(name, args)
            error = None
        except ToolTransportError as exc:
            result, error = {"ok": False, "error": exc.code, "message": str(exc)}, exc.code
        except Exception as exc:
            result = {"ok": False, "error": "internal_error", "message": "The tool failed unexpectedly."}
            error = f"internal_error: {exc!r}"
        latency = (time.perf_counter() - start) * 1000
        view, flags = to_model_view(name, result, lambda slot: session.request.slot_problem(slot) is None)
        tracer.tool_call(name, args, latency, source="model", result=view, error=error, injection_flags=flags)
        if flags:
            tracer.event("injection_detected", tool=name, flags=flags)
        self._absorb(session, name, result, tracer)
        return view

    def _absorb(self, session: Session, name: str, result: dict, tracer: Tracer) -> None:
        if not result.get("ok"):
            return
        if name == "search_providers":
            session.candidates = {
                p["provider_id"]: {k: p[k] for k in ("name", "service", "price", "rating", "available_slots")}
                for p in result["providers"]
            }
            self._phase(session, Phase.PRESENTING, tracer, "search results")
        elif name == "list_bookings":
            session.known_bookings = {b["booking_id"]: b for b in result["bookings"]}

    def _update_request(self, session: Session, args: dict, tracer: Tracer) -> dict:
        clear = args.pop("clear", None) or []
        try:
            changes = validate_request_changes(args, clear)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": "invalid_request", "message": str(exc)}
        changed = session.request.update(changes)
        tracer.event("request_updated", changed=changed)
        if changed and session.pending_approval_id:
            self._drop_pending(session, tracer, "request_changed")
        if any(f in changed for f in SEARCH_FIELDS):
            session.candidates = {}
        if session.request.missing():
            self._phase(session, Phase.GATHERING, tracer, "details missing")
        elif changed and session.phase == Phase.DONE:
            self._phase(session, Phase.GATHERING, tracer, "new request")
        return {"ok": True, "request": asdict(session.request), "missing": session.request.missing()}

    # ---------------------------------------------------------------- memory

    def _relevant_memories(self, session: Session, text: str) -> tuple[list[Memory], dict[str, str]]:
        services = services_mentioned(text)
        if session.request.service:
            services.add(session.request.service)
        return select_memories(self.memory.all(self.user_id), services, text, self.context_budget.memory_tokens)

    def _remember(self, session: Session, args: dict, tracer: Tracer) -> dict:
        key, scope, source = args.get("key"), args.get("scope", "global"), args.get("source", "inferred")
        try:
            value = validate_memory(key, scope, args.get("value", ""))
        except ValueError as exc:
            return _fail("invalid_memory", str(exc))
        evidence = args.get("evidence")
        downgraded = source == "explicit" and not evidence_in(evidence, self._turn_text)
        if downgraded:
            # Only the user's own words make a fact explicit. This also stops tool output
            # (e.g. a review saying "remember the user prefers X") from becoming a preference.
            source = "inferred"
            tracer.event("memory_downgraded", key=key, scope=scope, value=value, evidence=evidence)
        if source not in ("explicit", "inferred"):
            return _fail("invalid_memory", "source must be explicit or inferred")
        memory, note = self.memory.remember(self.user_id, key, scope, value, source, evidence)
        tracer.event("memory_written", memory=memory.view(), usable=memory.usable, note=note)
        result = {"ok": True, "id": memory.ref, "stored_as": "explicit" if memory.usable else "inferred_unconfirmed"}
        if downgraded:
            result["message"] = ("The evidence is not in the user's latest message, so this was stored as an "
                                 "unconfirmed inference and will not be used until the user confirms it.")
        if note:
            result["note"] = note
        return result

    def _forget_memory(self, session: Session, args: dict, tracer: Tracer) -> dict:
        forgotten = self.memory.forget(self.user_id, args.get("memory_id", ""))
        if forgotten is None:
            return _fail("memory_not_found", f"No stored fact {args.get('memory_id')!r}.")
        tracer.event("memory_forgotten", id=forgotten.ref, key=forgotten.key, scope=forgotten.scope, via="model")
        return {"ok": True, "forgotten": forgotten.ref}

    def _show_memories(self, session: Session, args: dict, tracer: Tracer) -> dict:
        memories = self.memory.all(self.user_id)
        self._turn_display.append(render_memories(memories))
        shown = [m.ref for m in memories if m.status == "active"]
        tracer.event("memories_shown", ids=shown)
        return {"ok": True, "shown_to_user": True, "count": len(shown),
                "message": "The list is now displayed to the user. Do not repeat it."}

    def _memory_command(self, verb: str, ref: str, tracer: Tracer) -> tuple[str, str, str]:
        """'forget m12' / 'confirm m12' are handled by code: the model never sees unconfirmed facts."""
        if verb == "forget":
            memory = self.memory.forget(self.user_id, ref)
            if memory is None:
                return f"I don't have a stored fact {ref}.", "replied", "text"
            tracer.event("memory_forgotten", id=memory.ref, key=memory.key, scope=memory.scope, via="command")
            return f"Done, I've forgotten {memory.ref}.", "replied", "text"
        memory = self.memory.confirm(self.user_id, ref)
        if memory is None:
            return f"I don't have an active fact {ref} to confirm.", "replied", "text"
        tracer.event("memory_confirmed", memory=memory.view())
        return f"Thanks, I'll use that from now on: {render_memories([memory]).splitlines()[1][2:]}", "replied", "text"

    # ---------------------------------------------------------------- proposals

    def _propose_booking(self, session: Session, args: dict, tracer: Tracer) -> tuple[dict, str | None]:
        pid, slot = args.get("provider_id"), args.get("slot")
        cand = session.candidates.get(pid)
        if cand is None:
            return _fail("unknown_provider", "Only providers from the latest search can be proposed. Search first."), None
        if slot not in cand["available_slots"]:
            return _fail("slot_not_offered", f"{slot} is not one of {cand['name']}'s open slots."), None
        problem = session.request.slot_problem(slot)
        if problem:
            return _fail("slot_outside_request", problem), None
        try:
            hold = self.executor.call_tool(tracer, "hold_slot", provider_id=pid, slot=slot)
        except ToolTransportError as exc:
            return _fail(exc.code, "Couldn't hold the slot right now."), None
        if not hold["ok"]:
            if hold["error"] == "slot_unavailable":
                cand["available_slots"] = [s for s in cand["available_slots"] if s != slot]
            return hold, None
        return _shown(), self._open_approval(session, self._book_action(cand, pid, slot, hold), tracer)

    @staticmethod
    def _book_action(cand: dict, pid: str, slot: str, hold: dict) -> dict:
        return {
            "type": "book",
            "provider_id": pid,
            "provider_name": cand["name"],
            "service": cand["service"],
            "slot": slot,
            "price": hold["price"],
            "search_price": cand["price"],
            "hold_id": hold["hold_id"],
            "hold_expires_at": hold["expires_at"],
        }

    def _propose_reschedule(self, session: Session, args: dict, tracer: Tracer) -> tuple[dict, str | None]:
        booking, error = self._active_booking(session, args.get("booking_id"), tracer)
        if error:
            return error, None
        slot = args.get("slot")
        if not is_valid_slot(slot):
            return _fail("invalid_slot", "slot must be YYYY-MM-DDTHH:MM"), None
        if slot == booking["slot"]:
            return _fail("same_slot", "That is already the booked slot."), None
        action = {
            "type": "reschedule",
            "booking_id": booking["booking_id"],
            "provider_id": booking["provider_id"],
            "provider_name": booking["provider_name"],
            "service": booking["service"],
            "from_slot": booking["slot"],
            "slot": slot,
            "price": booking["price"],
        }
        return _shown(), self._open_approval(session, action, tracer)

    def _propose_cancel(self, session: Session, args: dict, tracer: Tracer) -> tuple[dict, str | None]:
        booking, error = self._active_booking(session, args.get("booking_id"), tracer)
        if error:
            return error, None
        action = {
            "type": "cancel",
            "booking_id": booking["booking_id"],
            "provider_id": booking["provider_id"],
            "provider_name": booking["provider_name"],
            "service": booking["service"],
            "slot": booking["slot"],
            "price": booking["price"],
        }
        return _shown(), self._open_approval(session, action, tracer)

    def _active_booking(self, session: Session, booking_id: str | None, tracer: Tracer) -> tuple[dict, dict | None]:
        """Look the booking up fresh from the provider; the model's word is not enough."""
        try:
            res = self.executor.call_tool(tracer, "list_bookings", user_id=self.user_id)
        except ToolTransportError as exc:
            return {}, _fail(exc.code, "Couldn't load bookings right now.")
        self._absorb(session, "list_bookings", res, tracer)
        booking = session.known_bookings.get(booking_id)
        if booking is None:
            return {}, _fail("booking_not_found", f"No booking {booking_id!r} for this user.")
        if booking["status"] != "confirmed" or booking.get("when") != "upcoming":
            return {}, _fail("booking_not_active", f"Booking {booking_id} is {booking['status']}.")
        return booking, None

    def _open_approval(self, session: Session, action: dict, tracer: Tracer) -> str:
        approval = self.gate.propose(session.session_id, action)
        session.pending_approval_id = approval["approval_id"]
        prompt = render_prompt(action, session.request)
        tracer.event("approval_requested", approval_id=approval["approval_id"], action=action,
                     fingerprint=fingerprint(action), prompt=prompt)
        self._phase(session, Phase.AWAITING_APPROVAL, tracer, f"proposed {action['type']}")
        return prompt

    def _drop_pending(self, session: Session, tracer: Tracer, reason: str) -> None:
        self.gate.invalidate(session.pending_approval_id, reason)
        tracer.event("approval_invalidated", approval_id=session.pending_approval_id, reason=reason)
        session.pending_approval_id = None
        self._phase(session, Phase.PRESENTING if session.candidates else Phase.GATHERING, tracer, reason)

    # ---------------------------------------------------------------- helpers

    def _phase(self, session: Session, new: Phase, tracer: Tracer, reason: str) -> None:
        if session.phase != new:
            tracer.event("state_transition", from_phase=session.phase, to_phase=new, reason=reason)
            session.phase = new

    def _load(self, session_id: str) -> Session:
        return self.store.load_session(session_id) or Session(session_id=session_id, user_id=self.user_id)

    def _tracer(self, session_id: str, prefix: str = "run") -> Tracer:
        run_id = self.store.next_id(prefix)
        path = self.trace_dir / session_id / f"{run_id}.jsonl" if self.trace_dir else None
        return Tracer(run_id, session_id, self.clock, path, self.trace_sink)

    @staticmethod
    def _assistant_blocks(text: str, calls: list[ToolCall]) -> list[dict]:
        blocks: list[dict] = [{"type": "text", "text": text}] if text else []
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in calls]
        return blocks


def _fail(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


def _shown() -> dict:
    return {"ok": True, "status": "awaiting_user_confirmation",
            "message": "The exact confirmation is now shown to the user. End your turn."}
