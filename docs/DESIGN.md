# Errand Agent: design note

One Claude model (Opus 5.5) does the language work: it reads requests, picks lookups, compares providers and
writes replies. Code owns everything with consequences: state, approvals, side effects, memory selection and
limits. I kept it to one agent. A second "review reader" agent would only quarantine untrusted text, and code
that withholds flagged reviews does that more cheaply.

### State

Each session persists its request (service, area, dates, time window, budget), candidates, known bookings,
pending approval and history. The phase runs `gathering -> presenting -> awaiting_approval -> executing ->
done`, or `unverified` if an outcome can't be confirmed. `update_request` merges only the fields the user
changed, so a correction keeps the rest. Every transition is traced.

### Approval

The model has no `book`, `cancel`, `reschedule` or `hold_slot` tool. It calls `propose_*`. Code holds the
slot to get the real price, stores an approval fingerprinted on type, booking, provider, slot and price, and
writes the confirmation text itself. Only a bare "yes" right after that prompt approves it. Before each side
effect, `ApprovalGate.authorize` re-fingerprints the exact call. A new price on a renewed hold, a lost slot or
a corrected request voids the approval and asks again.

### Idempotency and recovery

Before `book`, an outbox row records the intent under key `book:<approval>:<hold>`. Retries, repeated "yes"
and restarts reuse that key, and the provider dedupes on it. After a timeout the agent asks
`get_booking_status`: confirmed means success, not booked means retry with the same key, unreachable means
"unconfirmed". It never says "booked" before the provider confirms. Open outbox rows are reconciled at startup
and before each turn, and the user is told the result.

### Memory and context

Facts carry a key, a scope (global or one service), a source (explicit or inferred) and a status (active,
superseded or forgotten). A fact counts as explicit only if its quoted evidence is in the user's own message,
so a review saying "remember the user prefers X" stays an unconfirmed inference. Correcting supersedes.
Forgetting erases the value. Code renders "what do you remember?", so unconfirmed facts reach the user but not
the model. Each turn, code picks usable facts relevant to the current service (up to 800 tokens) and recent
history (up to 3,000), and caps each request at 12,000. A per-turn manifest logs what went in and why the rest
stayed out; tests assert on it.

### Proactive follow-up

A poller reads the provider's event feed. On a cancellation, code skips past bookings and won't override an
open confirmation. Otherwise it searches within saved preferences and the original request, holds the best
slot (preferred provider, then nearest time, rating, price) and asks for a fresh "yes".

### Limits and traces

Per turn: 8 model calls, 15 tool calls, 45 seconds, each ending in a specific reply. Each run writes JSONL:
transitions, context manifest, model calls (latency, tokens, cache reads, cost), tool calls with arguments
and sanitized results, approval checks, outbox writes and outcomes. The system prompt and tools never change,
and state is appended rather than edited, so prompt caching works.

### Trade-offs

Tokens are estimated at 4 characters each, which keeps budgeting offline. Injection detection is pattern
based; the approval gate and evidence rule hold even when it misses. Holding at proposal time briefly reserves
slots the user may not take.

### Trace excerpt

From `docs/traces/timeout_after_success/run_0003.jsonl`, a live Claude run where `book` was forced to time
out after it had actually booked:

```
route            intent=affirm pending=apv_0001 last=approval_prompt   bare "yes" right after the prompt
approval_granted apv_0001 book QuickFix plm_02 2026-11-05T09:00 ₹750
outbox_write     book:apv_0001:hold_00001                               intent saved before the call
approval_check   apv_0001 authorizes book                               fingerprint re-checked
tool_call        book(hold_00001, key) -> ERROR timeout
book_timeout     attempt 1, checking source of truth                    no blind retry
tool_call        get_booking_status(key, hold) -> confirmed bk_00001
verified         book bk_00001 confirmed                                success reported only now
outcome          booked: "Booked and verified: QuickFix Plumbers, Thu 5 Nov, 09:00, ₹750"
```
