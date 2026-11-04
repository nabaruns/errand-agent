"""Proactive follow-up: provider cancellations are detected, handled within preferences, and re-approved."""

from __future__ import annotations

from datetime import timedelta

import pytest

from errand_agent.memory import MemoryStore
from errand_agent.mocks import Scenario
from errand_agent.model import say, use
from errand_agent.seed import seed_demo

from .conftest import SESSION, full_request, propose, search


@pytest.fixture
def seeded(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",) * 5))
    seed_demo(h.store, session_id=SESSION)
    h.mem = MemoryStore(h.store)
    return h


def test_cancellation_is_detected_and_replacement_needs_fresh_approval(seeded):
    h = seeded
    h.services.simulate_provider_cancellation("bk_seed_03", reason="technician unavailable")
    notes = h.agent.poll_events()

    assert len(notes) == 1 and notes[0].kind == "replacement_proposed"
    assert not h.model.requests  # no user message and no model call needed
    text = notes[0].text
    assert "CoolBreeze AC Care cancelled your AC servicing visit" in text
    assert "Please confirm this booking" in text
    assert "It costs ₹300 more than the original (₹1,200)" in text

    session = h.store.load_session(notes[0].session_id)
    approval = h.store.get_approval(session.pending_approval_id)
    action = approval["action"]
    assert approval["status"] == "pending" and action["replaces_booking_id"] == "bk_seed_03"
    # Within saved preferences: not the cancelling provider, AC budget ₹1,500, not before 08:00.
    assert action["provider_id"] != "acs_01"
    assert action["price"] <= 1500 and action["slot"][11:16] >= "08:00"
    assert not h.tool_calls("book")

    reply = h.agent.handle(notes[0].session_id, "yes")
    assert reply.outcome == "booked"
    assert h.bookings(booking_id="bk_seed_03")[0]["status"] == "cancelled_by_provider"
    assert len(h.bookings(service="ac_servicing", status="confirmed")) == 1
    h.assert_invariants()


def test_events_are_handled_once_even_across_restart(seeded):
    h = seeded
    h.services.simulate_provider_cancellation("bk_seed_03")
    assert len(h.agent.poll_events()) == 1
    assert h.agent.poll_events() == []
    h.restart()
    assert h.agent.poll_events() == []


def test_closest_slot_to_the_original_time_wins(seeded):
    h = seeded
    h.services.simulate_provider_cancellation("bk_seed_03")  # Thu 5 Nov 14:00
    note = h.agent.poll_events()[0]
    action = h.store.get_approval(h.store.load_session(note.session_id).pending_approval_id)["action"]
    assert action["slot"] == "2026-11-05T14:00"


def test_avoided_providers_and_time_preferences_are_respected(seeded):
    h = seeded
    # Avoid every AC provider except one, and only after 15:00.
    for pid in ("acs_02", "acs_03", "acs_04", "acs_05"):
        h.mem.remember("u_demo", "avoid_provider", "ac_servicing", pid, "explicit", "test")
    h.mem.remember("u_demo", "earliest_time", "ac_servicing", "15:00", "explicit", "test")
    h.services.simulate_provider_cancellation("bk_seed_03")
    note = h.agent.poll_events()[0]
    action = h.store.get_approval(h.store.load_session(note.session_id).pending_approval_id)["action"]
    assert action["provider_id"] == "acs_06" and action["slot"][11:16] >= "15:00"


def test_no_replacement_within_preferences_asks_instead_of_proposing(seeded):
    h = seeded
    h.mem.remember("u_demo", "budget_max", "ac_servicing", "300", "explicit", "test")
    h.services.simulate_provider_cancellation("bk_seed_03")
    note = h.agent.poll_events()[0]
    assert note.kind == "no_replacement" and "up to ₹300" in note.text
    assert h.store.load_session(note.session_id).pending_approval_id is None
    assert not h.tool_calls("hold_slot")


def test_pending_confirmation_is_not_clobbered(seeded):
    h = seeded
    h.say("plumber", full_request(), search(), propose())
    pending = h.session().pending_approval_id
    h.services.simulate_provider_cancellation("bk_seed_03")
    note = h.agent.poll_events()[0]
    assert note.kind == "notify_only" and note.session_id == SESSION
    assert h.session().pending_approval_id == pending
    assert h.store.get_approval(pending)["status"] == "pending"
    # The notice came after the prompt, so a bare "yes" no longer confirms the older proposal.
    assert h.say("yes", say("Which one do you mean?")).outcome == "replied"
    assert not h.tool_calls("book")


def test_past_bookings_are_not_followed_up(seeded):
    h = seeded
    h.say("plumber", full_request(), search(), propose())
    h.say("yes")
    booking = h.bookings(slot="2026-11-05T08:00")[0]
    h.clock.advance(days=2)
    h.services.simulate_provider_cancellation(booking["booking_id"])
    assert h.agent.poll_events() == []
    decision = h.events("proactive_decision")[-1]
    assert decision["notify"] is False


def test_notification_lands_in_the_session_that_made_the_booking(seeded):
    h = seeded
    h.say("plumber", full_request(), search(), propose())
    h.say("yes")
    booking = h.bookings(slot="2026-11-05T08:00")[0]
    h.model.add(say("hi"))
    h.agent.handle("other-session", "hello")  # a more recent, unrelated conversation
    h.services.simulate_provider_cancellation(booking["booking_id"])
    note = h.agent.poll_events()[0]
    assert note.session_id == SESSION
    assert h.session().history[-1]["content"] == note.text


def test_declining_the_replacement_hands_back_to_the_model_with_context(seeded):
    h = seeded
    h.services.simulate_provider_cancellation("bk_seed_03")
    note = h.agent.poll_events()[0]
    h.model.add(use("update_request", date_start="2026-11-07", date_end="2026-11-07"), say("Looking at Saturday."))
    h.agent.handle(note.session_id, "no, Saturday instead")
    session = h.store.load_session(note.session_id)
    assert session.pending_approval_id is None
    assert session.request.service == "ac_servicing" and session.request.area == "Indiranagar"
    assert not h.tool_calls("book")


def test_replacement_respects_the_original_request_constraints(harness):
    """Regression: only stored memories were used, so 'mornings only, under ₹1,500' was ignored."""
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    h.say("plumber, mornings, under 1500", full_request(), search(), propose("plm_01", "2026-11-05T08:00"))
    h.say("yes")
    booking = h.bookings(slot="2026-11-05T08:00")[0]
    h.services.simulate_provider_cancellation(booking["booking_id"])
    note = h.agent.poll_events()[0]
    action = h.store.get_approval(h.session().pending_approval_id)["action"]
    assert note.kind == "replacement_proposed"
    assert action["slot"][11:16] <= "12:00" and action["price"] <= 1500
    assert h.session().request.latest_time == "12:00"
    search_event = h.events("replacement_search")[-1]
    assert "request.latest_time" in search_event["memories_used"]
