"""Contract tests for the mock provider environment."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import pytest

from errand_agent.clock import IST, FakeClock
from errand_agent.mocks import (
    AREAS,
    INJECTION_REVIEWS,
    PROVIDERS,
    SERVICES,
    MockServices,
    Scenario,
    ServiceUnavailable,
    ToolTimeout,
)

# Wednesday morning. "This week" runs Thu 5th to Sun 8th; Saturday is the 7th.
START = datetime(2026, 11, 4, 9, 0, tzinfo=IST)
THIS_WEEK = {"start": "2026-11-05", "end": "2026-11-08"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def make(tmp_path, clock):
    created: list[MockServices] = []

    def _make(**scenario_kwargs) -> MockServices:
        svc = MockServices(tmp_path / "provider.db", Scenario(**scenario_kwargs), clock)
        created.append(svc)
        return svc

    yield _make
    for svc in created:
        svc.close()


def _hold_first_slot(svc: MockServices, provider_id: str = "plm_01") -> dict:
    service = PROVIDERS[provider_id].service
    found = svc.search_providers(service, "Indiranagar", THIS_WEEK)
    slot = next(p for p in found["providers"] if p["provider_id"] == provider_id)["available_slots"][0]
    hold = svc.hold_slot(provider_id, slot)
    assert hold["ok"], hold
    return hold


# ---------------------------------------------------------------- search / get_provider


def test_search_returns_3_to_8_providers_for_every_service_and_area(make):
    svc = make()
    for service in SERVICES:
        for area in AREAS:
            res = svc.search_providers(service, area, THIS_WEEK)
            assert res["ok"]
            assert 3 <= len(res["providers"]) <= 8, (service, area)
            for p in res["providers"]:
                assert {"provider_id", "price", "rating", "available_slots", "attributes"} <= p.keys()


def test_search_normalizes_aliases_and_rejects_unknowns(make):
    svc = make()
    assert svc.search_providers("AC servicing", "indiranagar", THIS_WEEK)["service"] == "ac_servicing"
    assert svc.search_providers("roofing", "Indiranagar", THIS_WEEK)["error"] == "unknown_service"
    assert svc.search_providers("plumber", "Mars", THIS_WEEK)["error"] == "unknown_area"
    bad_range = {"start": "2026-11-08", "end": "2026-11-05"}
    assert svc.search_providers("plumber", "Indiranagar", bad_range)["error"] == "invalid_date_range"


def test_search_excludes_past_slots(make):
    svc = make(busy_rate=0)
    res = svc.search_providers("plumber", "Indiranagar", {"start": "2026-11-04", "end": "2026-11-04"})
    slots = [s for p in res["providers"] for s in p["available_slots"]]
    assert slots and all(s > "2026-11-04T09:00" for s in slots)


def test_search_is_deterministic_per_seed(tmp_path, clock):
    def slots(seed: int, name: str) -> list:
        svc = MockServices(tmp_path / name, Scenario(seed=seed), clock)
        res = svc.search_providers("plumber", "Indiranagar", THIS_WEEK)
        svc.close()
        return [p["available_slots"] for p in res["providers"]]

    assert slots(1, "a.db") == slots(1, "b.db")
    assert slots(1, "c.db") != slots(2, "d.db")


def test_reviews_include_prompt_injection(make):
    svc = make()
    assert INJECTION_REVIEWS
    for pid, injected in INJECTION_REVIEWS.items():
        reviews = svc.get_provider(pid)["provider"]["reviews"]
        assert injected["text"] in [r["text"] for r in reviews]
    assert svc.get_provider("nope")["error"] == "provider_not_found"


# ---------------------------------------------------------------- hold_slot


def test_hold_lasts_ten_minutes_and_blocks_slot(make):
    svc = make()
    hold = _hold_first_slot(svc)
    assert (datetime.fromisoformat(hold["expires_at"]) - START).total_seconds() == 600
    plm = next(
        p for p in svc.search_providers("plumber", "Indiranagar", THIS_WEEK)["providers"] if p["provider_id"] == "plm_01"
    )
    assert hold["slot"] not in plm["available_slots"]


def test_hold_same_slot_twice_reuses_hold(make):
    svc = make()
    hold = _hold_first_slot(svc)
    again = svc.hold_slot("plm_01", hold["slot"])
    assert again["hold_id"] == hold["hold_id"] and again["reused"]


def test_hold_price_can_differ_from_search(make):
    svc = make(hold_price_overrides={"plm_01": 1300})
    hold = _hold_first_slot(svc)
    assert hold["search_price"] == 900 and hold["price"] == 1300


def test_hold_unavailable_and_invalid_slot(make):
    svc = make()
    assert svc.hold_slot("plm_01", "2026-11-05T03:00")["error"] == "slot_unavailable"
    assert svc.hold_slot("plm_01", "tomorrow 9am")["error"] == "invalid_slot"


# ---------------------------------------------------------------- book


def test_book_success_is_idempotent(make):
    svc = make(book_outcomes=("success",))
    hold = _hold_first_slot(svc)
    first = svc.book(hold["hold_id"], "key-1")
    second = svc.book(hold["hold_id"], "key-1")
    assert first["ok"] and first == second
    upcoming = [b for b in svc.list_bookings("u_demo")["bookings"] if b["slot"] == hold["slot"]]
    assert len(upcoming) == 1 and upcoming[0]["price"] == hold["price"]


def test_book_slot_taken_releases_slot_for_good(make):
    svc = make(book_outcomes=("slot_taken",))
    hold = _hold_first_slot(svc)
    assert svc.book(hold["hold_id"], "key-1")["error"] == "slot_taken"
    assert svc.book(hold["hold_id"], "key-1")["error"] == "slot_taken"
    assert svc.hold_slot("plm_01", hold["slot"])["error"] == "slot_unavailable"
    assert svc.get_booking_status(idempotency_key="key-1")["status"] == "failed"


def test_timeout_after_success_is_visible_in_status_and_not_duplicated(make):
    svc = make(book_outcomes=("timeout_booked",))
    hold = _hold_first_slot(svc)
    with pytest.raises(ToolTimeout):
        svc.book(hold["hold_id"], "key-1")
    status = svc.get_booking_status(idempotency_key="key-1")
    assert status["status"] == "confirmed"
    assert svc.get_booking_status(hold_id=hold["hold_id"])["booking"] == status["booking"]
    retry = svc.book(hold["hold_id"], "key-1")
    assert retry["booking"]["booking_id"] == status["booking"]["booking_id"]
    assert sum(b["slot"] == hold["slot"] for b in svc.list_bookings("u_demo")["bookings"]) == 1


def test_timeout_without_booking_then_retry_succeeds(make):
    svc = make(book_outcomes=("timeout_not_booked", "success"))
    hold = _hold_first_slot(svc)
    with pytest.raises(ToolTimeout):
        svc.book(hold["hold_id"], "key-1")
    assert svc.get_booking_status(idempotency_key="key-1")["status"] == "not_booked"
    assert svc.get_booking_status(hold_id=hold["hold_id"])["hold_status"] == "active"
    assert svc.book(hold["hold_id"], "key-1")["ok"]


def test_book_after_expiry_returns_hold_expired(make, clock):
    svc = make(book_outcomes=("success",))
    hold = _hold_first_slot(svc)
    clock.advance(minutes=10)
    assert svc.book(hold["hold_id"], "key-1")["error"] == "hold_expired"
    assert svc.get_booking_status(hold_id=hold["hold_id"])["hold_status"] == "expired"


def test_book_idempotent_result_survives_expiry(make, clock):
    svc = make(book_outcomes=("success",))
    hold = _hold_first_slot(svc)
    booked = svc.book(hold["hold_id"], "key-1")
    clock.advance(hours=1)
    assert svc.book(hold["hold_id"], "key-1") == booked


def test_book_rejects_key_reuse_and_second_key_on_same_hold(make):
    svc = make(book_outcomes=("success",))
    hold = _hold_first_slot(svc)
    svc.book(hold["hold_id"], "key-1")
    assert svc.book("hold_other", "key-1")["error"] == "idempotency_key_mismatch"
    assert svc.book(hold["hold_id"], "key-2")["error"] == "hold_already_used"


def test_book_state_survives_restart(tmp_path, clock):
    path = tmp_path / "provider.db"
    svc = MockServices(path, Scenario(book_outcomes=("timeout_booked",)), clock)
    hold = _hold_first_slot(svc)
    with pytest.raises(ToolTimeout):
        svc.book(hold["hold_id"], "key-1")
    svc.close()

    restarted = MockServices(path, Scenario(), clock)
    assert restarted.get_booking_status(idempotency_key="key-1")["status"] == "confirmed"
    assert restarted.book(hold["hold_id"], "key-1")["ok"]
    assert len(restarted.list_bookings("u_demo")["bookings"]) == 4  # 3 seeded + 1
    restarted.close()


def test_book_outcome_rates_match_contract(tmp_path, clock):
    svc = MockServices(tmp_path / "rates.db", Scenario(seed=7), clock)
    outcomes = Counter(svc._draw_book_outcome(svc._db, f"k{i}") for i in range(4000))
    share = {k: v / 4000 for k, v in outcomes.items()}
    assert share["success"] == pytest.approx(0.70, abs=0.03)
    assert share["slot_taken"] == pytest.approx(0.15, abs=0.03)
    assert share["timeout_booked"] == pytest.approx(0.075, abs=0.02)
    assert share["timeout_not_booked"] == pytest.approx(0.075, abs=0.02)
    svc.close()


# ---------------------------------------------------------------- cancel / reschedule / list


def test_cancel_fails_once_with_503_then_succeeds_idempotently(make):
    svc = make()
    with pytest.raises(ServiceUnavailable):
        svc.cancel("bk_seed_03", "c-1")
    first = svc.cancel("bk_seed_03", "c-1")
    assert first["ok"] and first["booking"]["status"] == "cancelled" and not first["already_cancelled"]
    assert svc.cancel("bk_seed_03", "c-1") == first


def test_cancel_errors(make):
    svc = make(cancel_first_call_fails=False)
    assert svc.cancel("bk_nope", "c-1")["error"] == "booking_not_found"
    assert svc.cancel("bk_seed_01", "c-2")["error"] == "booking_not_cancellable"


def test_reschedule_conflicts_on_saturday_10am_by_default(make):
    svc = make(busy_rate=0)
    res = svc.reschedule("bk_seed_03", "2026-11-07T10:00", "r-1")
    assert res["error"] == "slot_conflict"


def test_reschedule_succeeds_on_free_slot_and_is_idempotent(make):
    svc = make(busy_rate=0)
    res = svc.reschedule("bk_seed_03", "2026-11-07T12:00", "r-1")
    assert res["ok"] and res["booking"]["slot"] == "2026-11-07T12:00"
    assert res["previous_slot"] == "2026-11-05T14:00"
    assert svc.reschedule("bk_seed_03", "2026-11-07T12:00", "r-1") == res
    assert svc.reschedule("bk_seed_03", "2026-11-07T14:00", "r-1")["error"] == "idempotency_key_mismatch"


def test_reschedule_configured_conflict_slots(make):
    svc = make(busy_rate=0, conflict_slots=frozenset({"2026-11-07T12:00"}))
    assert svc.reschedule("bk_seed_03", "2026-11-07T12:00", "r-1")["error"] == "slot_conflict"
    assert svc.reschedule("bk_seed_03", "2026-11-07T10:00", "r-2")["ok"]


def test_list_bookings_has_seeded_history(make):
    bookings = make().list_bookings("u_demo")["bookings"]
    by_id = {b["booking_id"]: b for b in bookings}
    assert by_id["bk_seed_02"]["provider_id"] == "ele_02" and by_id["bk_seed_02"]["when"] == "past"
    assert by_id["bk_seed_03"]["service"] == "ac_servicing" and by_id["bk_seed_03"]["when"] == "upcoming"
    assert make().list_bookings("someone_else")["bookings"] == []


# ---------------------------------------------------------------- events and dispatch


def test_provider_cancellation_emits_event(make):
    svc = make()
    svc.simulate_provider_cancellation("bk_seed_03")
    events = svc.fetch_events("u_demo")
    assert [e["type"] for e in events] == ["provider_cancelled"]
    assert events[0]["booking"]["status"] == "cancelled_by_provider"
    assert svc.fetch_events("u_demo", after_id=events[0]["event_id"]) == []


def test_call_dispatch_validates_names_and_args(make):
    svc = make()
    assert svc.call("launch_rocket", {})["error"] == "unknown_tool"
    assert svc.call("get_provider", {"id": "plm_01"})["error"] == "invalid_arguments"
    assert svc.call("get_provider", {"provider_id": "plm_01"})["ok"]


def test_force_book_outcomes_applies_to_the_next_draws(make):
    svc = make(book_outcomes=("success",))
    svc.book(_hold_first_slot(svc)["hold_id"], "k1")
    svc.force_book_outcomes(["slot_taken"])
    hold = _hold_first_slot(svc, "plm_03")
    assert svc.book(hold["hold_id"], "k2")["error"] == "slot_taken"
    with pytest.raises(ValueError):
        svc.force_book_outcomes(["explode"])
