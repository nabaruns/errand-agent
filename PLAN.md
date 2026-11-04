# Errand Agent: Requirements Plan

Source: `Orange_Engineering_Assignment.pdf`. This plan turns the brief into checkable requirements,
a proposed architecture, and a build order. Items marked **[decide]** are open choices.

## 1. Proposed stack

- Python 3.12, `anthropic` SDK, Claude Sonnet 5.5 (`claude-sonnet-5-5`) for the live agent.
- SQLite for all durable state (bookings, approvals, outbox, memory, sessions). One file, survives restarts.
- Mock tools in-process behind a `ToolBackend` interface, seeded RNG plus scenario overrides.
- `pytest` for evals. A `ScriptedModel` implements the same interface as the real LLM client.
- UI: CLI chat for dev, plus a small local web page (chat left, live trace right) for the demo. **[decide]**

## 2. Architecture in one paragraph

A deterministic orchestrator owns the workflow state machine. The LLM does language work only:
extract slots from user text, choose read-only tool calls, rank and explain providers, write replies.
Every side-effecting tool (`hold_slot`, `book`, `cancel`, `reschedule`) goes through a policy gate
in application code that checks for a valid, unconsumed approval matching the exact action
fingerprint. The model can request these actions but can never execute them directly.

## 3. Functional requirements

### R1. Mock tool environment (contracts are fixed)

| Tool | Must do |
|---|---|
| `search_providers(service, area, date_range)` | 3 to 8 providers: id, price, rating, slots, attributes |
| `get_provider(provider_id)` | Details + free-text reviews; at least one review is a prompt injection |
| `hold_slot(provider_id, slot)` | 10-min hold, returns hold_id + expiry; quoted price can differ from search (configurable) |
| `book(hold_id, idempotency_key)` | 70% success, 15% `slot_taken`, 15% timeout (half of those actually booked); `hold_expired` past expiry |
| `get_booking_status(hold_id \| idempotency_key)` | Source of truth for ambiguous outcomes |
| `cancel(booking_id, idempotency_key)` | First call returns transient 503, retry succeeds |
| `reschedule(booking_id, slot, idempotency_key)` | Returns `slot_conflict` on configured slots |
| `list_bookings(user_id)` | Past and upcoming: id, provider, service, slot, status |

- Seeded randomness (`seed` param) and a scenario config that can force any outcome per call.
- Injectable clock so hold expiry is testable without sleeping.
- Mock `book` itself must be idempotent on `idempotency_key` (same key returns same booking).
- Simulated event source: `provider_cancelled(booking_id)` for proactive follow-up.
- Seed data: one user with past bookings (an electrician "last time", an upcoming AC servicing).

### R2. Conversation and state

- Required slots: service, area, date/time window, budget; optional: preferences (time-of-day, technician, rating floor).
- Fill slots from user text, then memory, then ask. Ask only for what is still missing, one question batch.
- Corrections update one slot and keep the rest ("actually Koramangala, not Indiranagar" keeps date and budget).
- Explicit state machine, persisted per session:
  `GATHERING -> SEARCHING -> PRESENTING -> AWAITING_APPROVAL -> HOLDING -> BOOKING -> VERIFYING -> CONFIRMED`
  plus `RESCHEDULING`, `CANCELLING`, `FAILED`, `ABANDONED`. Transitions are logged.
- Must handle the three example intents: new booking, reschedule existing ("move my AC servicing to Saturday"),
  and repeat provider ("same electrician as last time, not before 10 a.m.").

### R3. Approval enforcement (in code, not in the prompt)

- Approval record: `{action, provider_id, slot, price, booking_id?, fingerprint, issued_at, consumed}`.
- App renders the exact action and details to the user; approval is valid only if the user's next
  message is an explicit yes to that pending prompt. The model's opinion that the user agreed does not count.
- Gate checks fingerprint before every side-effecting call. Any change to provider, slot, or price
  (e.g. hold quotes a higher price) invalidates approval and re-asks.
- Approvals are single use. Repeated "yes" after success does not rebook.
- `hold_slot` runs at proposal time, before approval: it is reversible (expires in 10 min) and gives the real
  quoted price to approve. `book`, `cancel`, `reschedule` are the gated calls.

### R4. Booking correctness and idempotency

- Idempotency key derived from the approval fingerprint, written to an outbox row **before** calling `book`.
- On timeout: call `get_booking_status`; if booked, report success; if not, retry with the same key;
  if unknown after retries, tell the user the status is unconfirmed (never claim success).
- On `slot_taken` or `hold_expired`: go back to options, new approval required.
- On restart: scan outbox for in-flight rows and reconcile via `get_booking_status` before anything else.
- `cancel` retries the 503 with backoff using the same key.
- `reschedule` on `slot_conflict` offers alternative slots, new approval required.

### R5. Bounded execution

- Hard caps per user turn: model calls (e.g. 8), tool calls (e.g. 15), wall clock (e.g. 45 s). Per tool: retries (3) with timeout.
- Each limit hit produces a specific user-facing message and a clean state (no dangling holds claimed as bookings).
- Unexpected exceptions are caught at the orchestrator boundary and reported, never swallowed.

### R6. Untrusted tool output

- All free-text tool fields (reviews, descriptions) are wrapped and labelled as data in the prompt.
- Instruction-like content is flagged in the trace (simple pattern detector) and the review is summarized
  or quoted, not obeyed.
- Defense does not rely on the model: the approval gate makes injection unable to book, cancel,
  change price, or skip confirmation.
- Optional second agent: a tool-less "review reader" that summarizes reviews into a fixed schema
  (rating themes, red flags) so raw review text never reaches the planner. This is the one place
  a second agent clearly earns its keep. **[decide]**

### R7. Persistent memory

- Fact table: `{id, key, value, source: explicit|inferred, confirmed, status: active|superseded|forgotten, created_at, updated_at, evidence}`.
- Explicit facts come from direct user statements; inferred ones (e.g. "usually books mornings") stay
  unconfirmed until the user confirms.
- User commands: show memory, correct a fact (supersedes old row), forget a fact (tombstone, never sent again).
- Cross-session: preferred area, earliest time, preferred technician per service.

### R8. Context management

- Seed 50+ memories (mix of relevant, irrelevant, superseded, forgotten, unconfirmed inferred) and a long prior conversation.
- Fixed token budget for model input (e.g. 6k tokens). Context builder selects:
  system prompt, current state, relevant active facts (by service and slot), rolling summary of older turns, recent turns.
- Forgotten and unconfirmed inferred facts are filtered out by query, never by the model.
- Every turn logs a context manifest (fact ids included, token counts per section). Tests assert on it.

### R9. Proactive follow-up

- Event loop or poll picks up `provider_cancelled`.
- Agent decides whether to notify (upcoming booking: yes; past: no), searches replacements within stored
  preferences and the original constraints, presents options, and requires fresh approval before rebooking.

### R10. Observability

- One JSONL trace per run: state transitions, model calls (tokens in/out, latency, est. cost), tool name,
  args, sanitized result, latency, retries, approval checks, context manifest.
- Redact user PII fields in traces.
- A trace viewer (the demo's right-hand pane) that tails the JSONL.

## 4. Evaluation (offline, 20+ scenarios)

Runs against `ScriptedModel` (recorded or hand-scripted model outputs) and a simulated user.
Assertions are on state, DB rows, and trace events, not reply wording. Output: `eval_table.md` generated by the run.

Global invariants checked in every scenario:
- No side-effecting call without a matching unconsumed approval.
- At most one confirmed booking per approval.
- No success message without a verified booking status.
- No forgotten or unconfirmed inferred fact in any context manifest.
- Every run ends with a user-facing message and within limits.

| # | Scenario | Key assertion |
|---|---|---|
| 1 | Happy path plumber booking | 1 booking, approval consumed |
| 2 | Missing details | asks only for missing slots |
| 3 | Correction mid-flow | one slot changed, others kept |
| 4 | Same electrician as last time | provider from history, time floor applied |
| 5 | Reschedule AC to Saturday | reschedule after approval |
| 6 | Reschedule `slot_conflict` | alternatives offered, new approval |
| 7 | Prompt injection in review | no action, flag in trace |
| 8 | Adversarial user ("skip confirmation") | gate still blocks |
| 9 | Price changed at hold | approval invalidated, re-asked |
| 10 | Slot changed after approval | approval invalidated |
| 11 | Timeout, actually booked | status checked, success, no retry |
| 12 | Timeout, not booked | status checked, retried with same key |
| 13 | `slot_taken` | back to options |
| 14 | `hold_expired` | re-hold and re-approve |
| 15 | Repeated "yes" | single booking |
| 16 | Restart mid-booking | outbox reconciled, no duplicate |
| 17 | Cancel with 503 | retried, cancelled once |
| 18 | Memory: correct a fact | old superseded, new used |
| 19 | Memory: forget a fact | absent from all later manifests |
| 20 | Inferred fact unconfirmed | excluded until confirmed |
| 21 | Proactive provider cancellation | notify, replacement, fresh approval |
| 22 | Context budget with 50+ memories | manifest under budget, only relevant facts |
| 23 | Model-turn limit exhausted | clear message, clean state |
| 24 | Tool-call limit / persistent tool error | clear message, no hang |
| 25 | Wall-clock limit | clear message |

## 5. Deliverables checklist

- [ ] Git repo: code, mocks, tests, README (setup, run CLI, run demo, run evals)
- [ ] Design note, 1 page max: state model, approval, idempotency and recovery, memory, observability, trade-offs, annotated trace excerpt
- [ ] Generated eval table
- [ ] Three real bugs: what broke, why, fix, regression test (keep a `BUGS.md` log while building)
- [ ] Raw trace file of a timeout-after-success run
- [ ] 3 to 5 min demo video: full booking, one live failure (injection or timeout), chat and trace side by side
- [ ] AI coding assistant transcript (export this session)

## 6. Build order

1. Mocks + seeded scenarios + SQLite schema + clock. Unit tests for every failure mode.
2. Orchestrator state machine, approval gate, outbox/idempotency, limits. Test with `ScriptedModel` only.
3. Real LLM adapter (tool schemas, slot extraction), CLI chat.
4. Memory store + context builder + manifest logging.
5. Proactive event handling.
6. Tracing + web demo view.
7. Full eval suite + table generator. Fix bugs, log them in `BUGS.md`.
8. Design note, README, demo recording.

## 7. Open decisions

- Language: Python (assumed) or TypeScript.
- Demo UI: minimal local web page vs. terminal split (tmux with `tail -f` on the trace).
- Second "review reader" agent: include or keep single agent with wrapped untrusted text.
- Concrete limit values and token budget.
