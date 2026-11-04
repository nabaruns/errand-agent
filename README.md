# Errand Agent

A chat agent that books, reschedules and cancels home-service visits in Bengaluru: plumbers, electricians,
AC servicing, carpenters and cleaners.

It asks only for the details you haven't given. Nothing is booked until you reply "yes" to an exact
confirmation. If a provider call times out, it checks whether the booking went through before retrying, so
you never get two. It remembers your preferences between sessions, and if a provider cancels, it tells you
and proposes a replacement without being asked.

Providers, bookings and payments are local mocks. The only network call is to Claude, and the tests and evals
replace that with a scripted or recorded model.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
echo "CLAUDE_API_KEY=sk-ant-..." > .env        # or export ANTHROPIC_API_KEY

uv run errand-web --seed-demo                   # chat and live trace side by side: http://127.0.0.1:8765
uv run errand --seed-demo                       # same agent in the terminal
uv run pytest                                   # 94 tests, offline
uv run python -m evals.run                      # 30 eval scenarios, writes docs/eval/eval_table.md
```

`--seed-demo` loads a demo user with 54 stored facts and a 120-message prior conversation. Try:

- "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."
- "Move my AC servicing to Saturday." (Saturday 10:00 is set up to conflict.)
- "Book the same electrician as last time, but not before 10 a.m."
- "What do you remember about me?", then "forget m10" or "confirm m9".

Demo controls in the web page (or the terminal flags and commands) force failures:

| Web button | Terminal | Effect |
|---|---|---|
| Next book: timeout after success | `--book-outcomes timeout_booked` | `book()` times out after it has booked |
| Next book: slot taken | `--book-outcomes slot_taken` | Someone else gets the slot |
| Provider cancels it | `/provider-cancel <booking_id>` | Provider cancels; the agent reacts unprompted |

Other terminal commands: `/bookings`, `/memory`, `/trace`, `/quit`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_API_KEY` or `ANTHROPIC_API_KEY` | | API key, read from `.env` |
| `ERRAND_MODEL` | `claude-opus-5-5` | Any Claude model id |
| `ERRAND_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh`, `max` |

The agent turns on the API's server-side refusal fallback, so a request a safety classifier declines is rerun
on a fallback model.

## How it works

[docs/DESIGN.md](docs/DESIGN.md) is the one-page design note. The short version: the model proposes and code
decides. The model has no `book`, `cancel`, `reschedule` or `hold_slot` tool. Code fingerprints each approval
and checks it before every side effect. Each booking goes into an outbox under an idempotency key before it is
sent, and after a timeout the agent asks the provider for the status before retrying or reporting anything.

```
errand_agent/
  agent.py         orchestrator: routing, bounded model loop, proposals, memory tools, proactive events
  approval.py      approval gate, fingerprints, "is this a bare yes?", confirmation text
  executor.py      book / cancel / reschedule with outbox, retries, verification, reconciliation
  context.py       memory selection and history window under a token budget
  memory.py        explicit vs inferred facts, supersede, forget, confirm
  proactive.py     preference resolution and replacement ranking
  untrusted.py     withholds injected reviews, trims search results to the request window
  mocks/           the eight provider tools, seeded and scenario-configurable, SQLite-backed
  claude_model.py  Claude adapter (prompt caching, refusal fallback)
  web.py, cli.py   demo UI and terminal chat
evals/             simulated users, 30 scenarios, runner, live-run recorder and recording
tests/             unit and integration tests, including negative controls for the evals
docs/              design note, eval table, saved traces
```

## Deliverables

| Item | Where |
|---|---|
| Design note | [docs/DESIGN.md](docs/DESIGN.md) |
| Eval table (generated) | [docs/eval/eval_table.md](docs/eval/eval_table.md), raw: `docs/eval/results.json` |
| Bugs found while building | [BUGS.md](BUGS.md) |
| Timeout-after-success trace (live Claude run) | [docs/traces/timeout_after_success/](docs/traces/timeout_after_success/), annotated in the design note |
| Recorded live conversation replayed offline | `evals/recordings/plumber_timeout_live.json` (scenario S30) |
| Demo video script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |
| Original plan | [PLAN.md](PLAN.md) |

## Re-recording the live scenario

`uv run python -m evals.record` replays the S30 conversation against real Claude and overwrites the
recording. It costs about 10 cents.
