from datetime import datetime

from errand_agent.clock import IST, FakeClock
from errand_agent.trace import Tracer, sanitize


def test_field_named_like_envelope_never_raises(tmp_path):
    tracer = Tracer("run_1", "s1", FakeClock(datetime(2026, 11, 4, tzinfo=IST)), tmp_path / "t.jsonl")
    record = tracer.event("outcome", kind="booked", seq=99, run_id="spoofed")
    assert record["kind"] == "outcome" and record["kind_"] == "booked"
    assert record["seq"] == 0 and record["seq_"] == 99
    assert record["run_id"] == "run_1" and record["run_id_"] == "spoofed"


def test_sanitize_bounds_long_strings():
    assert sanitize("x" * 1000).endswith("[+400 chars]")
    assert sanitize({"a": ["y" * 700]})["a"][0].startswith("y" * 600)
