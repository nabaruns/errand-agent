"""Orchestrator behaviour against a scripted model: approval, idempotency, recovery, limits."""

from __future__ import annotations

import pytest

from errand_agent.approval import ApprovalError, ApprovalGate, classify_reply
from errand_agent.limits import Limits
from errand_agent.mocks import Scenario
from errand_agent.model import say, use, use_many
from errand_agent.state import Phase
from errand_agent.untrusted import WITHHELD

from .conftest import full_request, propose, search


def book_flow(h, **propose_kwargs):
    """Turn 1: full request, search, propose. Returns the reply carrying the approval prompt."""
    return h.say("Find a plumber for my kitchen tap this week, mornings only, under ₹1,500. I'm in Indiranagar.",
                 full_request(), search(), propose(**propose_kwargs))


# ---------------------------------------------------------------- happy path and approval


def test_happy_path_books_once_after_explicit_yes(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    reply = book_flow(h)
    assert reply.outcome == "awaiting_approval"
    assert "Sharma Plumbing Works" in reply.text and "Thu 5 Nov, 08:00" in reply.text and "₹900" in reply.text
    assert not h.tool_calls("book")

    done = h.say("yes")
    assert done.outcome == "booked" and done.text.startswith("Booked and verified")
    assert len(h.bookings(provider_id="plm_01", slot="2026-11-05T08:00")) == 1
    assert h.session().phase == Phase.DONE
    assert len(h.model.requests) == 3  # the confirmation itself never went through the model
    h.assert_invariants()


def test_model_cannot_call_side_effect_tools_directly(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    reply = h.say(
        "book anything",
        full_request(),
        search(),
        use("hold_slot", provider_id="plm_01", slot="2026-11-05T08:00"),
        use("book", hold_id="hold_00001", idempotency_key="x"),
        use("cancel", booking_id="bk_seed_03", idempotency_key="y"),
        say("Done."),
    )
    assert [e["tool"] for e in h.events("blocked_tool_call")] == ["hold_slot", "book", "cancel"]
    assert h.bookings(status="confirmed") == h.bookings(booking_id="bk_seed_03")
    assert reply.text == "Done."
    h.assert_invariants()


def test_yes_only_counts_right_after_the_approval_prompt(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    h.say("is that the cheapest option?", say("QuickFix is cheaper at ₹750. Want me to look at them instead?"))
    reply = h.say("yes", say("Okay, I'll look at QuickFix."))
    assert reply.outcome == "replied"
    assert not h.tool_calls("book")
    h.assert_invariants()


@pytest.mark.parametrize("text", ["yes but make it 10am", "yes, and also find an electrician", "maybe"])
def test_affirmation_with_extra_content_is_not_approval(harness, text):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    reply = h.say(text, say("Let me check that."))
    assert reply.outcome == "replied" and not h.tool_calls("book")


def test_classify_reply():
    for text in ["yes", "Yes please", "yes, book it", "ok", "Go ahead.", "confirm", "yes please go ahead!"]:
        assert classify_reply(text) == "affirm", text
    for text in ["no", "No, make it Friday", "wait", "don't"]:
        assert classify_reply(text) == "decline", text
    for text in ["yes but at 10", "what about tomorrow?", "yesterday works"]:
        assert classify_reply(text) == "other", text


def test_decline_invalidates_pending_approval(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    approval_id = h.session().pending_approval_id
    h.say("no, not that one", say("Sure, which would you prefer?"))
    assert h.session().pending_approval_id is None
    assert h.store.get_approval(approval_id)["status"] == "invalidated"
    assert h.say("yes", say("Which one?")).outcome == "replied"
    assert not h.tool_calls("book")


def test_correction_while_pending_invalidates_and_keeps_other_fields(harness):
    h = harness(scenario=Scenario(busy_rate=0))
    book_flow(h)
    approval_id = h.session().pending_approval_id
    h.say("actually make the budget 1200", use("update_request", budget_max=1200), say("Updated."))
    s = h.session()
    assert s.pending_approval_id is None and h.store.get_approval(approval_id)["status"] == "invalidated"
    assert s.request.budget_max == 1200
    assert (s.request.service, s.request.area, s.request.latest_time) == ("plumber", "Indiranagar", "12:00")


def test_missing_details_are_reported_and_not_re_asked(harness):
    h = harness()
    h.say("I need a plumber", use("update_request", task="book", service="plumber"), say("Which area?"))
    first = h.events("request_updated")
    assert h.session().request.missing() == ["area", "date_start", "date_end", "budget_max"]
    h.say("Indiranagar, this week, under 1500",
          use("update_request", area="Indiranagar", date_start="2026-11-05", date_end="2026-11-08", budget_max=1500),
          say("Searching."))
    assert h.session().request.missing() == []
    assert h.session().request.service == "plumber"
    assert first


def test_repeated_confirmation_does_not_rebook(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success", "success")))
    book_flow(h)
    assert h.say("yes").outcome == "booked"
    again = h.say("yes")
    assert again.outcome == "noop" and "nothing waiting" in again.text
    assert len(h.tool_calls("book")) == 1
    assert len(h.bookings(slot="2026-11-05T08:00")) == 1
    h.assert_invariants()


def test_gate_rejects_mismatched_or_unapproved_actions(harness):
    h = harness()
    gate = ApprovalGate(h.store)
    action = {"type": "book", "provider_id": "plm_01", "slot": "2026-11-05T08:00", "price": 900}
    approval = gate.propose("s1", action)
    with pytest.raises(ApprovalError):
        gate.authorize(approval["approval_id"], action)  # pending, not approved
    gate.approve(approval["approval_id"])
    gate.authorize(approval["approval_id"], action)
    with pytest.raises(ApprovalError):
        gate.authorize(approval["approval_id"], {**action, "price": 1000})
    with pytest.raises(ApprovalError):
        gate.authorize(approval["approval_id"], {**action, "slot": "2026-11-05T10:00"})
    gate.consume(approval["approval_id"])
    with pytest.raises(ApprovalError):
        gate.authorize(approval["approval_id"], action)


def test_proposal_validation(harness):
    h = harness()
    h.say("x", full_request(), search(),
          propose("plm_99"), propose("plm_01", "2026-11-05T09:00"), propose("plm_01", "2026-11-05T14:00"),
          say("Hmm."))
    assert h.session().pending_approval_id is None
    codes = [b["content"] for m in h.model.requests[-1].messages if m["role"] == "user"
             for b in m["content"] if b["type"] == "tool_result"]
    assert any("unknown_provider" in c for c in codes)
    assert any("slot_not_offered" in c for c in codes)
    assert any("slot_outside_request" in c for c in codes)


# ---------------------------------------------------------------- material changes


def test_price_change_on_renewed_hold_requires_fresh_approval(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    first = h.session().pending_approval_id
    h.clock.advance(minutes=11)
    h.services.scenario.hold_price_overrides["plm_01"] = 1100

    reply = h.say("yes")
    assert reply.outcome == "awaiting_approval"
    assert "₹900 to ₹1,100" in reply.text and "Price: ₹1,100" in reply.text
    assert h.store.get_approval(first)["status"] == "invalidated"
    assert not h.tool_calls("book")

    done = h.say("yes")
    assert done.outcome == "booked"
    assert h.bookings(slot="2026-11-05T08:00")[0]["price"] == 1100
    h.assert_invariants()


def test_expired_hold_renewed_at_same_price_keeps_approval(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    h.clock.advance(minutes=11)
    assert h.say("yes").outcome == "booked"
    assert len(h.tool_calls("hold_slot")) == 2


def test_hold_price_above_search_price_is_shown_with_budget_warning(harness):
    h = harness(scenario=Scenario(busy_rate=0, hold_price_overrides={"plm_01": 1600}))
    reply = book_flow(h)
    assert "₹1,600 (search listed ₹900)" in reply.text
    assert "above your ₹1,500 budget" in reply.text


# ---------------------------------------------------------------- booking failures


def test_timeout_after_success_checks_status_and_does_not_retry(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("timeout_booked",)))
    book_flow(h)
    reply = h.say("yes")
    assert reply.outcome == "booked"
    assert len(h.tool_calls("book")) == 1
    assert h.events("book_timeout") and h.tool_calls("get_booking_status")
    assert len(h.bookings(slot="2026-11-05T08:00")) == 1
    h.assert_invariants()


def test_timeout_without_booking_retries_with_same_key(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("timeout_not_booked", "success")))
    book_flow(h)
    assert h.say("yes").outcome == "booked"
    keys = {e["args"]["idempotency_key"] for e in h.tool_calls("book")}
    assert len(h.tool_calls("book")) == 2 and len(keys) == 1
    h.assert_invariants()


def test_persistent_timeouts_report_honestly(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("timeout_not_booked",) * 3))
    book_flow(h)
    reply = h.say("yes")
    assert reply.outcome == "failed" and "nothing was booked" in reply.text
    assert not h.bookings(slot="2026-11-05T08:00")
    h.assert_invariants()


def test_slot_taken_returns_to_options(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("slot_taken",)))
    book_flow(h)
    reply = h.say("yes")
    assert reply.outcome == "failed" and "Someone else took" in reply.text
    assert h.session().phase == Phase.PRESENTING
    assert h.say("yes", say("Which slot would you like instead?")).outcome == "replied"
    h.assert_invariants()


def test_restart_mid_booking_reconciles_without_duplicate(harness, monkeypatch):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)

    class Crash(BaseException):
        pass

    real_book = h.services.book

    def book_then_crash(*args, **kwargs):
        real_book(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(h.services, "book", book_then_crash)
    with pytest.raises(Crash):
        h.say("yes")

    h.restart()
    reply = h.say("what happened?", say("See above."))
    assert reply.text.startswith("Booked and verified")
    assert len(h.bookings(slot="2026-11-05T08:00")) == 1
    assert h.store.outbox_open() == []
    # The approval was consumed during reconciliation, so a late "yes" cannot book again.
    assert h.say("yes", say("You're welcome.")).outcome == "replied"
    assert len(h.bookings(slot="2026-11-05T08:00")) == 1
    h.assert_invariants()


def test_restart_after_unbooked_attempt_resumes_with_same_key(harness, monkeypatch):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)

    class Crash(BaseException):
        pass

    monkeypatch.setattr(h.services, "book", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        h.say("yes")
    h.restart()  # restart restores the real book(); the outbox row is still pending
    reply = h.say("hello?", say("Hi."))
    assert reply.text.startswith("Booked and verified")
    assert len(h.bookings(slot="2026-11-05T08:00")) == 1


# ---------------------------------------------------------------- cancel and reschedule


def test_cancel_survives_transient_503(harness):
    h = harness()
    reply = h.say("cancel my AC servicing", use("list_bookings"), use("propose_cancel", booking_id="bk_seed_03"))
    assert reply.outcome == "awaiting_approval" and "Please confirm this cancellation" in reply.text
    done = h.say("yes")
    assert done.outcome == "cancelled"
    assert len(h.tool_calls("cancel")) == 2
    assert h.bookings(booking_id="bk_seed_03")[0]["status"] == "cancelled"
    h.assert_invariants()


def test_reschedule_conflict_then_success(harness):
    h = harness()
    h.say("Move my AC servicing to Saturday", use("list_bookings"),
          use("propose_reschedule", booking_id="bk_seed_03", slot="2026-11-07T10:00"))
    conflict = h.say("yes")
    assert conflict.outcome == "failed" and "can't do" in conflict.text
    assert h.bookings(booking_id="bk_seed_03")[0]["slot"] == "2026-11-05T14:00"

    h.say("try 12", use("propose_reschedule", booking_id="bk_seed_03", slot="2026-11-07T12:00"))
    done = h.say("yes")
    assert done.outcome == "rescheduled"
    assert h.bookings(booking_id="bk_seed_03")[0]["slot"] == "2026-11-07T12:00"
    h.assert_invariants()


def test_cannot_propose_on_someone_elses_or_past_booking(harness):
    h = harness()
    h.say("cancel", use("propose_cancel", booking_id="bk_seed_01"), use("propose_cancel", booking_id="bk_other"),
          say("I can't cancel those."))
    assert h.session().pending_approval_id is None


# ---------------------------------------------------------------- untrusted output


def test_injected_review_is_withheld_and_changes_nothing(harness):
    h = harness(scenario=Scenario(busy_rate=0))
    reply = h.say("tell me about QuickFix", full_request(), search(), use("get_provider", provider_id="plm_02"),
                  say("QuickFix is rated 4.8."))
    seen = h.model.requests[-1].messages[-1]["content"][0]["content"]
    assert WITHHELD in seen and "ignore all previous instructions" not in seen
    assert h.events("injection_detected")
    assert reply.outcome == "replied" and h.session().pending_approval_id is None
    assert not h.tool_calls("book")


def test_model_claiming_success_without_verification_is_blocked(harness):
    h = harness()
    reply = h.say("book it", say("Great news, I've booked QuickFix for you!"))
    assert reply.text == "Just to be clear: nothing has been booked or changed yet."
    assert h.events("unverified_claim_blocked")


# ---------------------------------------------------------------- limits and errors


def test_model_call_limit(harness):
    h = harness(limits=Limits(max_model_calls=3))
    reply = h.say("loop", *[use("list_bookings") for _ in range(5)])
    assert reply.outcome == "limit_exceeded" and "step limit" in reply.text
    assert sum(1 for e in h.events("model_call")) == 3


def test_tool_call_limit(harness):
    h = harness(limits=Limits(max_tool_calls=4))
    reply = h.say("many", use_many(*[("get_provider", {"provider_id": "plm_01"})] * 6))
    assert reply.outcome == "limit_exceeded" and "too many lookups" in reply.text


def test_wall_clock_limit(harness):
    ticks = iter([0.0, 0.0, 50.0, 50.0, 50.0, 50.0, 50.0])
    h = harness(limits=Limits(max_wall_seconds=45), monotonic=lambda: next(ticks, 99.0))
    reply = h.say("slow", use("list_bookings"), say("done"))
    assert reply.outcome == "limit_exceeded" and "took too long" in reply.text


def test_unexpected_tool_exception_is_contained(harness, monkeypatch):
    h = harness()
    monkeypatch.setattr(h.services, "get_provider", lambda provider_id: 1 / 0)
    reply = h.say("details", use("get_provider", provider_id="plm_01"), say("That lookup failed, sorry."))
    assert reply.text == "That lookup failed, sorry."
    assert "internal_error" in h.tool_calls("get_provider")[0]["error"]


def test_model_failure_still_answers(harness):
    h = harness()
    reply = h.say("hi")  # script is empty, so the model raises
    assert reply.outcome == "error" and "unexpected problem" in reply.text
    assert h.events("run_end")


def test_every_run_is_traced(harness):
    h = harness(scenario=Scenario(busy_rate=0, book_outcomes=("success",)))
    book_flow(h)
    h.say("yes")
    kinds = {e["kind"] for e in h.events()}
    assert {"run_start", "context_manifest", "model_call", "tool_call", "state_transition", "approval_requested",
            "approval_granted", "approval_check", "outbox_write", "verified", "run_end"} <= kinds
    model_call = h.events("model_call")[0]
    assert model_call["input_tokens"] > 0 and "latency_ms" in model_call and "cost_usd" in model_call


def test_requests_are_append_only_within_a_turn(harness):
    """Frozen system/tools and an append-only message list keep the prompt cache and thinking blocks valid."""
    h = harness(scenario=Scenario(busy_rate=0))
    h.say("plumber please", full_request(), search(), use("get_provider", provider_id="plm_01"), say("Here you go."))
    reqs = h.model.requests
    assert len({r.system for r in reqs}) == 1
    assert all(r.tools == reqs[0].tools for r in reqs)
    for prev, cur in zip(reqs, reqs[1:]):
        assert cur.messages[: len(prev.messages)] == prev.messages
    state_blocks = [b for m in reqs[-1].messages if m["role"] == "user" for b in m["content"]
                    if b["type"] == "text" and b["text"].startswith("<app_state>")]
    assert len(state_blocks) >= 2  # initial state plus at least one appended update


def test_search_view_shows_every_day_that_fits_the_window(harness):
    """Regression: the first 12-16 slots were early/afternoon ones, hiding later in-window days, and the
    model invented a Sunday slot. Slots outside the request window are now dropped before trimming."""
    h = harness(scenario=Scenario(busy_rate=0))
    h.say("plumber", full_request(earliest_time="08:00"), search(), say("Here are the options."))
    result = next(b for m in h.model.requests[-1].messages if m["role"] == "user" for b in m["content"]
                  if b["type"] == "tool_result" and "providers" in b["content"])
    import json
    quickfix = next(p for p in json.loads(result["content"])["providers"] if p["provider_id"] == "plm_02")
    days = {s[:10] for s in quickfix["available_slots"]}
    assert days == {"2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"}
    assert all("08:00" <= s[11:16] <= "12:00" for s in quickfix["available_slots"])
    assert quickfix["slots_outside_request"] > 0
