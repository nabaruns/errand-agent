"""Transport-level failures.

Business errors (slot_taken, hold_expired, slot_conflict, ...) come back as
`{"ok": False, "error": <code>}` results. These exceptions model the cases where
the call itself does not return a usable answer, so the caller cannot know what happened.
"""


class ToolTransportError(Exception):
    code = "transport_error"


class ToolTimeout(ToolTransportError):
    code = "timeout"


class ServiceUnavailable(ToolTransportError):
    code = "service_unavailable"
    status = 503
