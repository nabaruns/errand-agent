"""Seed data for the demo user: 50+ memories in every state, and a long prior conversation.

Used by the CLI (`--seed-demo`) and by the context-budget tests. Values marked "sentinel"
are distinctive so tests can prove they never appear in a prompt.
"""

from __future__ import annotations

from .memory import MemoryStore
from .state import Session
from .store import AgentStore

FORGOTTEN_SENTINEL = "Gate code 4471"
INFERRED_SENTINEL = "Seems to prefer technicians who speak Tamil"
SUPERSEDED_AREA = "Koramangala"

# key, scope, value, source, then optional follow-up: "forget" | "confirm"
_FACTS: list[tuple] = [
    ("area", "global", SUPERSEDED_AREA, "explicit"),
    ("area", "global", "Indiranagar", "explicit"),
    ("earliest_time", "global", "08:00", "explicit"),
    ("latest_time", "global", "20:00", "explicit"),
    ("language", "global", "English or Kannada", "explicit"),
    ("preferred_days", "global", "weekdays", "inferred"),
    ("budget_max", "global", "2000", "inferred"),
    ("note", "global", FORGOTTEN_SENTINEL, "explicit", "forget"),
    ("note", "global", INFERRED_SENTINEL, "inferred"),
    ("note", "global", "Has a dog; technicians should ring before entering", "explicit"),
    ("note", "global", "Prefers verified providers", "inferred", "confirm"),
    ("note", "global", "Works from home on Mondays", "explicit"),
    ("note", "global", "Fourth floor flat, lift available", "explicit"),
    ("note", "global", "Pays by UPI", "explicit"),
    ("note", "global", "Building water supply is off 2pm to 4pm", "explicit"),
    ("note", "global", "Neighbour recommended Sharma Plumbing", "explicit"),
    ("note", "global", "Visiting parents in Mysuru over Christmas", "explicit"),
    ("note", "global", "Son's birthday party on the 14th", "explicit", "forget"),
    ("note", "global", "Car parking available in basement for technicians", "explicit"),
    ("note", "global", "Prefers SMS over phone calls", "inferred"),
    ("note", "global", "Allergic to strong cleaning chemicals", "explicit"),
    ("note", "global", "Old phone number ending 2231", "explicit", "forget"),
    # electrician
    ("preferred_provider", "electrician", "ele_02", "explicit"),
    ("avoid_provider", "electrician", "ele_06", "explicit"),
    ("budget_max", "electrician", "1000", "explicit"),
    ("budget_max", "electrician", "1200", "explicit"),
    ("note", "electrician", "Bedroom fan regulator replaced in October", "explicit"),
    ("preferred_days", "electrician", "Saturday", "inferred"),
    # plumber
    ("preferred_provider", "plumber", "plm_01", "explicit"),
    ("budget_max", "plumber", "1200", "explicit"),
    ("budget_max", "plumber", "1500", "explicit"),
    ("note", "plumber", "Kitchen tap cartridge replaced in September", "explicit"),
    ("earliest_time", "plumber", "09:00", "inferred"),
    # ac servicing
    ("preferred_provider", "ac_servicing", "acs_04", "explicit"),
    ("preferred_provider", "ac_servicing", "acs_01", "explicit"),
    ("budget_max", "ac_servicing", "1500", "explicit"),
    ("preferred_days", "ac_servicing", "Saturday", "inferred"),
    ("note", "ac_servicing", "Two split ACs, bedroom and living room", "explicit"),
    # carpenter
    ("preferred_provider", "carpenter", "crp_06", "explicit"),
    ("note", "carpenter", "Wardrobe hinge in the guest room is loose", "explicit"),
    ("budget_max", "carpenter", "1200", "explicit"),
    ("budget_max", "carpenter", "1000", "inferred"),
    # cleaning
    ("preferred_provider", "cleaning", "cln_02", "explicit"),
    ("preferred_days", "cleaning", "Sunday", "explicit"),
    ("budget_max", "cleaning", "2500", "explicit"),
    ("budget_max", "cleaning", "3000", "explicit"),
    ("budget_max", "cleaning", "3500", "explicit"),
    ("note", "cleaning", "Deep clean of kitchen chimney every quarter", "explicit"),
    ("note", "cleaning", "Skip balcony, it is under renovation", "explicit", "forget"),
    ("earliest_time", "cleaning", "10:00", "explicit"),
    ("language", "cleaning", "Kannada", "inferred"),
    ("avoid_provider", "cleaning", "cln_05", "explicit"),
    ("note", "cleaning", "Prefers eco-friendly products", "inferred"),
    ("note", "global", "Usually free after 6pm on weekdays", "inferred"),
    ("note", "global", "Has a home office with sensitive equipment", "explicit"),
]

_TOPICS = [
    ("Can you remind me when my last plumber visit was?", "Your last plumber visit was with Sharma Plumbing Works."),
    ("Do electricians here carry their own ladders?", "Most do; I can mention it when booking."),
    ("What's the usual price for AC servicing?", "In Indiranagar it ranges from about ₹850 to ₹1,800."),
    ("Is Sunday OK for cleaning?", "Yes, several cleaners work Sundays."),
    ("Thanks, that's all for now.", "You're welcome. Message me any time."),
    ("Which carpenter did we use for the shelves?", "That was Sri Lakshmi Woodcraft."),
    ("Can a plumber also fix the geyser?", "Many can; I'll note it in the request."),
    ("How long does a deep clean take?", "Usually four to six hours for a 2BHK."),
]


def seed_demo(store: AgentStore, user_id: str = "u_demo", session_id: str = "demo", turns: int = 60) -> dict:
    """Idempotent: does nothing if the user already has memories."""
    memory = MemoryStore(store)
    if memory.all(user_id):
        return {"seeded": False}
    for fact in _FACTS:
        key, scope, value, source, *follow = fact
        m, _ = memory.remember(user_id, key, scope, value, source, evidence="(seeded)")
        if follow == ["forget"]:
            memory.forget(user_id, m.id)
        elif follow == ["confirm"]:
            memory.confirm(user_id, m.id)

    session = store.load_session(session_id) or Session(session_id=session_id, user_id=user_id)
    if not session.history:
        for i in range(turns):
            user, assistant = _TOPICS[i % len(_TOPICS)]
            session.history += [
                {"role": "user", "content": f"{user} (earlier chat #{i + 1})"},
                {"role": "assistant", "content": f"{assistant} Anything else I can help with today? I can also "
                                                 "check availability for other services in your area."},
            ]
        store.save_session(session)
    return {"seeded": True, "memories": len(memory.all(user_id)), "history_messages": len(session.history)}
