"""Smoke test of the demo server with a scripted model: chat, live trace feed, unprompted notifications."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from errand_agent.clock import FakeClock
from errand_agent.model import ScriptedModel, say
from errand_agent.web import Demo, make_handler, parse_args

from .conftest import START


@pytest.fixture
def server(tmp_path):
    args = parse_args(["--data-dir", str(tmp_path / "data"), "--trace-dir", str(tmp_path / "traces"),
                       "--seed-demo", "--poll-seconds", "0.05"])
    model = ScriptedModel([say("Hi! What do you need done?")])
    demo = Demo(args, model=model, clock=FakeClock(START))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(demo))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", demo
    httpd.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, (json.loads(res.read()) if "api" in url else res.read().decode())


def test_page_chat_feed_and_notifications(server):
    base, demo = server
    status, page = call(base + "/")
    assert status == 200 and "<title>Errand Agent</title>" in page

    _, reply = call(base + "/api/chat", {"session": "demo", "text": "hi"})
    assert reply["text"] == "Hi! What do you need done?" and reply["outcome"] == "replied"
    _, feed = call(base + "/api/feed?session=demo&after=0")
    kinds = [i["event"]["kind"] for i in feed["items"] if i["type"] == "event"]
    assert kinds[0] == "run_start" and "context_manifest" in kinds and kinds[-1] == "run_end"

    _, ok = call(base + "/api/provider-cancel", {"booking_id": "bk_seed_03"})
    assert ok["ok"]
    deadline = time.time() + 3
    notes = []
    while time.time() < deadline and not notes:
        _, feed = call(base + "/api/feed?session=demo&after=0")
        notes = [i for i in feed["items"] if i["type"] == "notification"]
        time.sleep(0.05)
    assert notes and notes[0]["kind"] == "replacement_proposed"

    _, ok = call(base + "/api/force-book", {"outcomes": ["timeout_booked"]})
    assert ok["ok"] and demo.services.scenario.book_outcomes[-1] == "timeout_booked"
    _, state = call(base + "/api/state?session=demo")
    assert any(b["booking_id"] == "bk_seed_03" for b in state["bookings"])
