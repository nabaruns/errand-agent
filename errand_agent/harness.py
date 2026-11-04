"""Offline test harness shared by the pytest suite and the eval runner.

Wires an agent to the mocks, a fake clock, and a scripted (or replayed) model, with all state in
files under one directory so a restart can be simulated by rebuilding the process-local objects.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agent import Agent
from .clock import IST, FakeClock
from .context import ContextBudget
from .limits import Limits
from .mocks import INJECTION_REVIEWS, MockServices, Scenario
from .model import Model, ScriptedModel, use
from .store import AgentStore

# Wednesday morning. "This week" runs Thu 5th to Sun 8th; Saturday is the 7th.
START = datetime(2026, 11, 4, 9, 0, tzinfo=IST)
THIS_WEEK = {"start": "2026-11-05", "end": "2026-11-08"}
SESSION = "s1"
SIDE_EFFECTS = ("book", "cancel", "reschedule")


class Harness:
    def __init__(self, root: Path, scenario: Scenario | None = None, limits: Limits | None = None,
                 monotonic=None, model: Model | None = None, context_budget: ContextBudget | None = None) -> None:
        self.root = Path(root)
        self.tmp_path = self.root  # name kept for older tests
        self.clock = FakeClock(START)
        self.scenario = scenario or Scenario(busy_rate=0)
        self.limits = limits or Limits()
        self.context_budget = context_budget
        self.monotonic = monotonic or (lambda: 0.0)
        self.model = model if model is not None else ScriptedModel()
        self.trace: list[dict] = []  # every trace event, in emission order, across restarts
        self.start()

    def start(self) -> None:
        """(Re)create every process-local object over the same databases, as a restart would."""
        self.services = MockServices(self.root / "provider.db", self.scenario, self.clock)
        self.store = AgentStore(self.root / "agent.db", self.clock)
        self.agent = Agent(model=self.model, services=self.services, store=self.store, clock=self.clock,
                           limits=self.limits, trace_dir=self.root / "traces", sleep=lambda s: None,
                           monotonic=self.monotonic, context_budget=self.context_budget,
                           default_session=SESSION, trace_sink=self.trace.append)

    def restart(self) -> None:
        self.close()
        self.start()

    def close(self) -> None:
        self.services.close()
        self.store.close()

    def say(self, text: str, *steps, session: str = SESSION):
        if steps:
            self.model.add(*steps)
        return self.agent.handle(session, text)

    def session(self, session_id: str = SESSION):
        return self.store.load_session(session_id)

    def events(self, kind: str | None = None) -> list[dict]:
        return [e for e in self.trace if kind is None or e["kind"] == kind]

    def tool_calls(self, tool: str) -> list[dict]:
        return [e for e in self.events("tool_call") if e["tool"] == tool]

    def bookings(self, **filters) -> list[dict]:
        rows = self.services.list_bookings("u_demo")["bookings"]
        return [b for b in rows if all(b.get(k) == v for k, v in filters.items())]

    def sent_to_model(self) -> str:
        requests = getattr(self.model, "requests", [])
        return json.dumps([{"system": r.system, "messages": r.messages} for r in requests], ensure_ascii=False)

    # ---------------------------------------------------------------- invariants

    def invariant_violations(self, never_sent: tuple[str, ...] = ()) -> list[str]:
        """Properties that must hold in every run, whatever the scenario."""
        problems: list[str] = []
        events = self.trace
        for i, e in enumerate(events):
            if e["kind"] != "tool_call":
                continue
            if e["tool"] in (*SIDE_EFFECTS, "hold_slot") and e["source"] != "app":
                problems.append(f"model reached side-effecting tool {e['tool']}")
            if e["tool"] in SIDE_EFFECTS:
                prev = events[i - 1] if i else {}
                if not (prev.get("kind") == "approval_check" and prev.get("tool") == e["tool"]):
                    problems.append(f"{e['tool']} called without an approval check")

        new = [b for b in self.bookings() if not b["booking_id"].startswith("bk_seed")]
        if len(new) != len({(b["provider_id"], b["slot"]) for b in new}):
            problems.append("duplicate booking for the same provider and slot")

        verified = {e["booking_id"] for e in events if e["kind"] == "verified"}
        for e in events:
            if e["kind"] == "outcome" and e.get("result") in ("booked", "cancelled", "rescheduled"):
                if not e.get("booking") or e["booking"]["booking_id"] not in verified:
                    problems.append(f"{e['result']} reported without verification")
        for e in events:
            if e["kind"] == "run_end":
                if not str(e.get("reply", "")).strip():
                    problems.append(f"{e['run_id']} ended without a user-facing reply")
                if e["model_calls"] > self.limits.max_model_calls:
                    problems.append(f"{e['run_id']} exceeded the model call limit")

        sent = self.sent_to_model()
        for review in INJECTION_REVIEWS.values():
            if review["text"] in sent:
                problems.append("an injected review reached the model verbatim")
        for value in never_sent:
            if value in sent:
                problems.append(f"{value!r} reached the model")
        return problems

    def assert_invariants(self, never_sent: tuple[str, ...] = ()) -> None:
        problems = self.invariant_violations(never_sent)
        assert not problems, problems


def full_request(**overrides):
    args = dict(task="book", service="plumber", area="Indiranagar", date_start="2026-11-05",
                date_end="2026-11-08", latest_time="12:00", budget_max=1500, notes="kitchen tap leaking")
    args.update(overrides)
    return use("update_request", **args)


def search(service="plumber", area="Indiranagar", date_range=None):
    return use("search_providers", service=service, area=area, date_range=date_range or THIS_WEEK)


def propose(provider_id="plm_01", slot="2026-11-05T08:00"):
    return use("propose_booking", provider_id=provider_id, slot=slot)
