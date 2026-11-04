# Bugs hit while building

## 1. A successful booking was reported to the user as an error

**What broke:** In the happy path, the user said "yes", the booking went through and was verified, but the
reply was the generic "I hit an unexpected problem" message.

**Why:** The outcome was traced with `tracer.event("outcome", kind=outcome.kind, ...)`. `Tracer.event` already
takes the event kind as its first parameter named `kind`, so the call raised `TypeError` after the side effect
had committed. The top-level handler caught it and answered with the error message. The booking was real; the
user was told otherwise.

**Fix:** The outcome field is now `result=` (`errand_agent/agent.py`, `_apply_outcome`). That was a local
fix, and the same clash came back when proactive notifications were traced with `kind=`. The root fix is in
`Tracer.event`: `kind` is now positional-only and a data field that collides with an envelope key
(`kind`, `seq`, `run_id`, ...) is stored with a trailing underscore instead of raising. The error message was
also reworded so it never implies nothing happened: it says no change was *confirmed* in this turn and that
in-flight work will be verified.

**Regression test:** `tests/test_agent.py::test_happy_path_books_once_after_explicit_yes` asserts the outcome is
`booked` and the reply starts with "Booked and verified"; `tests/test_trace.py::test_field_named_like_envelope_never_raises`
guards the tracer itself.

## 2. "Yes" after a failed booking was ignored

**What broke:** After `slot_taken`, the agent says "Want me to pick another slot?". Replying "yes" returned
"There's nothing waiting for your confirmation" instead of continuing.

**Why:** The repeat-confirmation guard (which stops a second "yes" from rebooking) fired whenever the last
assistant message was any outcome. Failure outcomes end with a question, so "yes" there is an answer, not a
repeated approval.

**Fix:** The guard now applies only when the last outcome was a success (`SUCCESS_KINDS`). Other "yes" replies go
to the model, which cannot book without a fresh proposal and approval anyway.

**Regression test:** `tests/test_agent.py::test_slot_taken_returns_to_options`, and
`test_repeated_confirmation_does_not_rebook` still guards the original behaviour.

## 3. Restart before the provider saw `book()` abandoned an approved booking

**What broke:** If the process died after writing the outbox row but before `book()` reached the provider, the
restarted agent told the user the booking "was interrupted and did not go through", even though the hold was
still live and the user had approved it.

**Why:** Reconciliation asked `get_booking_status(idempotency_key=...)` only. The provider had never seen that
key, so it answered `not_booked` without `hold_status`, and the resume logic needs `hold_status == "active"`
to safely retry.

**Fix:** Every status check now sends both the idempotency key and the hold id (`Executor._book_status`). The
provider answers from the key when it knows it, otherwise from the hold, so reconciliation can see the hold is
still active and resume with the same key.

**Regression test:** `tests/test_agent.py::test_restart_after_unbooked_attempt_resumes_with_same_key`.

## 4. Storing one note silently wiped out every other note

**What broke:** In the seeded demo, the user had a dozen global notes ("has a dog", "pays by UPI", ...). After
seeding, only the last one was active; the rest, including an inferred fact the user had not yet reviewed,
were marked superseded and disappeared from `show_memories`.

**Why:** `MemoryStore.remember` supersedes the current fact with the same key and scope, which is right for
single-valued keys like `area` or `earliest_time` but wrong for `note`, which is a list.

**Fix:** `MULTI_VALUED = ("note",)`. Notes are never superseded by `remember` or `confirm`; they are only
removed by forgetting (`errand_agent/memory.py`).

**Regression test:** `tests/test_memory.py::test_seed_has_50_memories_in_every_state_and_long_history` (checks an
inferred-and-confirmed note survives) and `test_show_memories_is_rendered_by_code_and_hidden_from_model`.

## 5. A provider-cancellation replacement ignored what the user had asked for

**What broke:** In the live demo the user booked a plumber "mornings only, under ₹1,500", then the provider
cancelled. The replacement search ran with only the stored memories ("Indiranagar"), so it could have proposed
an afternoon slot or a more expensive provider. It also overwrote the session request, erasing `latest_time`.

**Why:** `resolve_preferences` read long-term memory only. Constraints the user gave for this booking lived in
`session.request` and were never consulted.

**Fix:** `resolve_preferences(memories, service, request)` fills any gap left by memory from the session's
request for the same service, and records which ones it used (`request.latest_time`, ...) in the trace.

**Regression test:** `tests/test_proactive.py::test_replacement_respects_the_original_request_constraints`.

## 6. The model invented a slot that did not exist

**What broke:** In two live runs Claude told the user QuickFix was "free at 11am Sunday", then corrected itself
a turn later. QuickFix has no Sunday slot in the window.

**Why:** To stay inside the context budget, search results showed each provider's first 16 slots and the state
block showed the first 12. Those were mostly 07:00 and afternoon slots outside the user's "mornings only"
window, so the cut fell before the later days. The model filled the gap by pattern.

**Fix:** Both views now drop slots outside the request's dates and time window before trimming, and report
how many were dropped (`slots_outside_request`), so whatever is cut is genuinely out of scope
(`errand_agent/untrusted.py::_trim_slots`, `prompts.state_view`).

**Regression test:** `tests/test_agent.py::test_search_view_shows_every_day_that_fits_the_window`.
