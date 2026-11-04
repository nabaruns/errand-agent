"""Terminal chat with the live agent.

    uv run errand                       # chat; state in ./data, traces in ./traces
    uv run errand --session demo2       # separate conversation
    uv run errand --book-outcomes timeout_booked   # force the next book() outcomes

Commands inside the chat:
    /bookings                 list the user's bookings straight from the provider
    /memory                   every stored fact, including forgotten and superseded ones
    /provider-cancel <id>     simulate the provider cancelling a booking (the agent notices on its own)
    /trace                    path of the last run's trace
    /quit
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from .agent import Agent
from .clock import SystemClock
from .mocks import MockServices, Scenario
from .seed import seed_demo
from .store import AgentStore

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader so the key never has to be exported by hand. Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_agent(args: argparse.Namespace) -> tuple[Agent, MockServices]:
    from .claude_model import ClaudeModel

    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    scenario = Scenario(seed=args.seed, book_outcomes=tuple(args.book_outcomes or ()),
                        hold_price_delta=args.hold_price_delta)
    services = MockServices(data / "provider.db", scenario, clock)
    store = AgentStore(data / "agent.db", clock)
    model = ClaudeModel(model=args.model, effort=args.effort)
    if args.seed_demo:
        seed_demo(store, session_id=args.session)
    agent = Agent(model=model, services=services, store=store, clock=clock, trace_dir=args.trace_dir)
    return agent, services


def start_event_poller(agent: Agent, session: str, every: float) -> None:
    """Background check of the provider's event feed, so cancellations surface without a user message."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                notes = agent.poll_events()
            except Exception as exc:  # keep polling; the chat must not die with the poller
                print(f"\n{DIM}[event poller error: {exc!r}]{RESET}")
                continue
            for note in notes:
                where = "" if note.session_id == session else f" (session {note.session_id})"
                print(f"\n\n{BOLD}errand (unprompted){where}>{RESET} {note.text}\n{DIM}[{note.kind}]{RESET}")
                print(f"\n{BOLD}you>{RESET} ", end="", flush=True)

    threading.Thread(target=loop, daemon=True, name="event-poller").start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="errand", description="Chat with the errand agent.")
    parser.add_argument("--session", default="demo")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--trace-dir", default="traces")
    parser.add_argument("--model", default=None, help="defaults to $ERRAND_MODEL or claude-opus-5-5")
    parser.add_argument("--effort", default=None, help="low | medium | high | xhigh | max")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--book-outcomes", nargs="*",
                        help="forced book() outcomes: success slot_taken timeout_booked timeout_not_booked")
    parser.add_argument("--hold-price-delta", type=int, default=0, help="add this to every quoted hold price")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="how often to check provider events")
    parser.add_argument("--seed-demo", action="store_true",
                        help="seed 50+ memories and a long prior conversation for the demo user")
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"))
    agent, services = build_agent(args)
    print(f"{BOLD}Errand agent{RESET} {DIM}({agent.model.name}, session {args.session}). /quit to exit.{RESET}")
    last_trace = None
    start_event_poller(agent, args.session, args.poll_seconds)
    while True:
        try:
            text = input(f"\n{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/trace":
            print(last_trace or "no runs yet")
            continue
        if text == "/memory":
            for m in agent.memory.all(agent.user_id):
                print(f"  {m.ref:<5} {m.status:<10} {m.source:<8} {'used' if m.usable else 'unused':<6} "
                      f"{m.scope:<13} {m.key:<18} {m.value}")
            continue
        if text == "/bookings":
            for b in services.list_bookings(agent.user_id)["bookings"]:
                print(f"  {b['booking_id']}  {b['service']:<13} {b['provider_name']:<26} {b['slot']}  "
                      f"₹{b['price']:<6} {b['status']} ({b['when']})")
            continue
        if text.startswith("/provider-cancel"):
            parts = text.split()
            if len(parts) != 2:
                print("usage: /provider-cancel <booking_id>")
                continue
            try:
                services.simulate_provider_cancellation(parts[1])
                print(f"{DIM}provider cancelled {parts[1]}{RESET}")
            except ValueError as exc:
                print(exc)
            continue

        reply = agent.handle(args.session, text)
        last_trace = reply.trace_path
        print(f"\n{BOLD}errand>{RESET} {reply.text}")
        print(f"{DIM}[{reply.outcome}] trace: {reply.trace_path}{RESET}")


if __name__ == "__main__":
    sys.exit(main())
