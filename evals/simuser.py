"""A simulated user: a profile of facts to answer with and a policy for confirmations.

The user reacts to what the agent actually did (the reply's outcome and the session's missing
fields), not to the wording of the reply, so scenarios stay stable when phrasing changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Request fields the user answers from their profile, and how they phrase them.
FIELD_KEYS = {
    "area": "area",
    "date_start": "dates",
    "date_end": "dates",
    "budget_max": "budget",
    "service": "service",
}


@dataclass
class SimUser:
    profile: dict = field(default_factory=lambda: {
        "area": "I'm in Indiranagar",
        "dates": "any day this week",
        "budget": "up to ₹1,500",
        "service": "a plumber",
    })
    confirm: bool = True
    on_failure: str = "Okay, pick another slot for me."
    said: list[str] = field(default_factory=list)

    def reply_to(self, reply, session) -> str | None:
        if reply.outcome == "awaiting_approval":
            text = "yes" if self.confirm else "no, not that one"
        elif session is not None and session.request.missing():
            keys = dict.fromkeys(FIELD_KEYS[f] for f in session.request.missing() if f in FIELD_KEYS)
            text = ", ".join(self.profile[k] for k in keys if k in self.profile) or None
        elif reply.outcome in ("failed",):
            text = self.on_failure
        else:
            text = None
        if text:
            self.said.append(text)
        return text
