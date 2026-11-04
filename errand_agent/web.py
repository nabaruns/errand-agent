"""Local demo UI: chat on the left, the live trace on the right. Standard library only.

    uv run errand-web --seed-demo            # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import Agent
from .cli import load_dotenv
from .clock import SystemClock
from .mocks import MockServices, Scenario
from .seed import seed_demo
from .store import AgentStore


class Feed:
    """Per-session, append-only list of trace events and unprompted notifications."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, list[dict]] = {}

    def add(self, session_id: str, item: dict) -> None:
        with self._lock:
            items = self._items.setdefault(session_id, [])
            items.append({**item, "n": len(items)})

    def since(self, session_id: str, after: int) -> list[dict]:
        with self._lock:
            return list(self._items.get(session_id, [])[after:])


class Demo:
    def __init__(self, args: argparse.Namespace, model=None, clock=None) -> None:
        if model is None:
            from .claude_model import ClaudeModel

            model = ClaudeModel(model=args.model, effort=args.effort)
        data = Path(args.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        clock = clock or SystemClock()
        self.feed = Feed()
        self.services = MockServices(data / "provider.db", Scenario(seed=args.seed), clock)
        store = AgentStore(data / "agent.db", clock)
        if args.seed_demo:
            seed_demo(store, session_id=args.session)
        self.agent = Agent(
            model=model,
            services=self.services,
            store=store,
            clock=clock,
            trace_dir=args.trace_dir,
            default_session=args.session,
            trace_sink=lambda e: self.feed.add(e["session_id"], {"type": "event", "event": e}),
        )
        self.session = args.session
        threading.Thread(target=self._poll, args=(args.poll_seconds,), daemon=True).start()

    def _poll(self, every: float) -> None:
        stop = threading.Event()
        while not stop.wait(every):
            try:
                for note in self.agent.poll_events():
                    self.feed.add(note.session_id, {"type": "notification", "text": note.text, "kind": note.kind})
            except Exception as exc:
                self.feed.add(self.session, {"type": "notification", "text": f"(event poller error: {exc!r})",
                                             "kind": "error"})

    def bookings(self) -> list[dict]:
        return self.services.list_bookings(self.agent.user_id)["bookings"]


def make_handler(demo: Demo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        def _json(self, body: object, status: int = 200) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            session = q.get("session", demo.session)
            if url.path == "/":
                raw = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif url.path == "/api/state":
                history = demo.agent.store.load_session(session)
                self._json({"session": session, "model": demo.agent.model.name, "bookings": demo.bookings(),
                            "history": history.history[-40:] if history else []})
            elif url.path == "/api/feed":
                self._json({"items": demo.feed.since(session, int(q.get("after", 0)))})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            session = body.get("session", demo.session)
            if self.path == "/api/chat":
                reply = demo.agent.handle(session, str(body.get("text", ""))[:2000])
                self._json({"text": reply.text, "outcome": reply.outcome, "run_id": reply.run_id})
            elif self.path == "/api/provider-cancel":
                try:
                    demo.services.simulate_provider_cancellation(body["booking_id"], "technician unavailable")
                    self._json({"ok": True})
                except (KeyError, ValueError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif self.path == "/api/force-book":
                try:
                    demo.services.force_book_outcomes(list(body.get("outcomes", [])))
                    self._json({"ok": True})
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="errand-web", description="Chat and live trace, side by side.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", default="demo")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--trace-dir", default="traces")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--seed-demo", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(Path(".env"))
    demo = Demo(args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(demo))
    print(f"Errand demo on http://127.0.0.1:{args.port}  (model {demo.agent.model.name}, session {args.session})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Errand Agent</title>
<style>
:root {
  --bg: #f6f5f2; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a66; --line: #e3e1db;
  --accent: #c2410c; --user: #fff1e6; --bot: #f2f1ee; --note: #fef3c7;
  --ok: #15803d; --warn: #b45309; --bad: #b91c1c; --info: #1d4ed8; --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161614; --panel: #1f1f1c; --ink: #ecebe7; --muted: #a09e98; --line: #34332f;
    --accent: #fb923c; --user: #3a2a1d; --bot: #2a2a27; --note: #3d3415;
    --ok: #4ade80; --warn: #fbbf24; --bad: #f87171; --info: #93c5fd;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, sans-serif; }
header { display: flex; gap: 16px; align-items: baseline; padding: 12px 18px; border-bottom: 1px solid var(--line); }
header h1 { font-size: 16px; margin: 0; }
header .meta { color: var(--muted); font-size: 12px; }
main { display: grid; grid-template-columns: minmax(340px, 5fr) 7fr; height: calc(100vh - 47px); }
section { display: flex; flex-direction: column; min-height: 0; }
section + section { border-left: 1px solid var(--line); }
.title { padding: 8px 16px; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
  border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; }
#chat { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: 88%; padding: 9px 12px; border-radius: 10px; white-space: pre-wrap; }
.msg.user { align-self: flex-end; background: var(--user); }
.msg.bot { align-self: flex-start; background: var(--bot); }
.msg.note { align-self: flex-start; background: var(--note); border-left: 3px solid var(--warn); }
.msg .tag { display: block; font-size: 11px; color: var(--muted); margin-top: 6px; }
form { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--line); }
input[type=text] { flex: 1; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); font: inherit; }
button { padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); font: inherit; cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .5; cursor: default; }
.controls { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 12px; }
.controls button, .controls select { font-size: 12px; padding: 5px 8px; }
select { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); }
#trace { flex: 1; overflow-y: auto; padding: 8px 0; font-family: var(--mono); font-size: 12px; }
.run { margin: 10px 12px 4px; padding: 6px 10px; border-radius: 6px; background: var(--bot); font-family: system-ui, sans-serif; font-size: 12px; display: flex; justify-content: space-between; }
.run b { color: var(--accent); }
.ev { display: grid; grid-template-columns: 64px 150px 1fr; gap: 8px; padding: 3px 16px; cursor: pointer; }
.ev:hover { background: var(--bot); }
.ev .t { color: var(--muted); }
.ev .k { font-weight: 600; }
.ev .s { overflow-wrap: anywhere; }
.k.model { color: var(--info); } .k.tool { color: var(--ink); } .k.ok { color: var(--ok); }
.k.warn { color: var(--warn); } .k.bad { color: var(--bad); } .k.dim { color: var(--muted); font-weight: 400; }
pre.full { grid-column: 1 / -1; margin: 4px 0 6px; padding: 8px; background: var(--bot); border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
#totals { font-family: var(--mono); text-transform: none; letter-spacing: 0; }
@media (max-width: 860px) { main { grid-template-columns: 1fr; height: auto; } #chat, #trace { max-height: 60vh; } section + section { border-left: 0; border-top: 1px solid var(--line); } }
</style>
</head>
<body>
<header><h1>Errand Agent</h1><span class="meta" id="meta"></span></header>
<main>
  <section>
    <div class="title"><span>Chat</span><span id="status"></span></div>
    <div id="chat"></div>
    <form id="form"><input type="text" id="text" placeholder="Find a plumber for my kitchen tap this week, mornings only, under ₹1,500" autocomplete="off"><button class="primary" id="send">Send</button></form>
    <div class="controls">
      <button type="button" id="force-timeout" title="Next book() times out after it actually booked">Next book: timeout after success</button>
      <button type="button" id="force-taken">Next book: slot taken</button>
      <select id="booking"></select><button type="button" id="cancel">Provider cancels it</button>
    </div>
  </section>
  <section>
    <div class="title"><span>Trace</span><span id="totals"></span></div>
    <div id="trace"></div>
  </section>
</main>
<script>
const session = new URLSearchParams(location.search).get("session") || "demo";
const $ = (id) => document.getElementById(id);
let after = 0, totals = { calls: 0, tin: 0, tout: 0, cache: 0, cost: 0 }, lastRun = null;

function addMsg(cls, text, tag) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  if (tag) { const t = document.createElement("span"); t.className = "tag"; t.textContent = tag; div.appendChild(t); }
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
}

const short = (o, n = 120) => { const s = typeof o === "string" ? o : JSON.stringify(o); return s.length > n ? s.slice(0, n) + "..." : s; };

function describe(e) {
  switch (e.kind) {
    case "run_start": return ["dim", `user: ${short(e.user_message, 100)}`];
    case "route": return ["dim", `intent=${e.intent} pending=${e.pending_approval || "none"}`];
    case "context_manifest": return ["dim", `memories in ${e.memories_included.length}, out ${Object.keys(e.memories_excluded).length}; history ${e.history_window.messages} msgs (${e.history_window.omitted} omitted); tokens ${JSON.stringify(e.tokens)}`];
    case "model_call": return ["model", `${e.model} ${Math.round(e.latency_ms)}ms in ${e.input_tokens}+cache ${e.cache_read_tokens} out ${e.output_tokens} $${e.cost_usd.toFixed(4)} -> ${e.tool_calls.length ? e.tool_calls.map(c => c.name).join(", ") : short(e.text, 80)}`];
    case "tool_call": {
      const r = e.error ? `ERROR ${e.error}` : (e.result && e.result.ok === false ? `error ${e.result.error}` : "ok");
      return [e.error || (e.result && e.result.ok === false) ? "warn" : "tool", `[${e.source}] ${e.tool}(${short(e.args, 90)}) ${r} ${Math.round(e.latency_ms)}ms`];
    }
    case "state_transition": return ["ok", `${e.from_phase} -> ${e.to_phase} (${e.reason})`];
    case "approval_requested": return ["warn", `${e.approval_id} ${e.action.type} ${e.action.provider_name} ${e.action.slot} ₹${e.action.price} fp=${e.fingerprint}`];
    case "approval_granted": return ["ok", `${e.approval_id} approved by user`];
    case "approval_check": return ["ok", `${e.approval_id} authorizes ${e.tool}`];
    case "approval_invalidated": return ["warn", `${e.approval_id} invalidated: ${e.reason}`];
    case "outbox_write": return ["dim", `outbox ${e.key}`];
    case "book_timeout": return ["bad", `book timed out (attempt ${e.attempt}); checking source of truth`];
    case "verified": return ["ok", `${e.op} verified: ${e.booking_id}`];
    case "outcome": return [e.result === "failed" || e.result === "unknown" ? "bad" : "ok", `${e.result}: ${short(e.message, 100)}`];
    case "injection_detected": return ["bad", `prompt injection withheld from model: ${short(e.flags[0].excerpt, 90)}`];
    case "blocked_tool_call": return ["bad", `model tried ${e.tool}; blocked`];
    case "memory_written": return ["ok", `memory ${e.memory.id} ${e.memory.key}=${e.memory.value} (${e.usable ? "used" : "unconfirmed"})`];
    case "memory_downgraded": return ["warn", `explicit -> inferred: evidence not in user's words`];
    case "event_received": return ["bad", `provider event: ${e.type} ${e.booking.booking_id}`];
    case "proactive_decision": return ["warn", `notify=${e.notify} ${e.reason}`];
    case "replacement_search": return ["dim", `prefs: ${e.preferences}; ${e.options} options`];
    case "notification": return ["warn", `sent to user unprompted (${e.notification})`];
    case "limit_exceeded": return ["bad", `limit hit: ${e.limit}`];
    case "error": return ["bad", e.error];
    case "run_end": return ["dim", `end: ${e.outcome}, ${e.model_calls} model / ${e.tool_calls} tool calls, ${e.elapsed_s}s, $${(e.cost_usd || 0).toFixed(4)}`];
    default: return ["dim", short(Object.fromEntries(Object.entries(e).filter(([k]) => !["seq", "ts", "run_id", "session_id", "kind"].includes(k))), 140)];
  }
}

function addEvent(e) {
  if (e.run_id !== lastRun) {
    lastRun = e.run_id;
    const r = document.createElement("div");
    r.className = "run";
    r.innerHTML = `<span><b></b></span><span></span>`;
    r.querySelector("b").textContent = e.run_id;
    r.lastChild.textContent = new Date(e.ts).toLocaleTimeString();
    $("trace").appendChild(r);
  }
  const [cls, text] = describe(e);
  const row = document.createElement("div");
  row.className = "ev";
  row.innerHTML = `<span class="t"></span><span class="k ${cls}"></span><span class="s"></span>`;
  row.children[0].textContent = new Date(e.ts).toLocaleTimeString([], { hour12: false });
  row.children[1].textContent = e.kind;
  row.children[2].textContent = text;
  row.onclick = () => {
    const open = row.querySelector("pre");
    if (open) { open.remove(); return; }
    const pre = document.createElement("pre"); pre.className = "full"; pre.textContent = JSON.stringify(e, null, 2); row.appendChild(pre);
  };
  $("trace").appendChild(row);
  $("trace").scrollTop = $("trace").scrollHeight;
  if (e.kind === "model_call") {
    totals.calls++; totals.tin += e.input_tokens; totals.tout += e.output_tokens; totals.cache += e.cache_read_tokens; totals.cost += e.cost_usd;
    $("totals").textContent = `${totals.calls} model calls · in ${totals.tin} · cached ${totals.cache} · out ${totals.tout} · $${totals.cost.toFixed(4)}`;
  }
}

async function poll() {
  try {
    const res = await fetch(`/api/feed?session=${encodeURIComponent(session)}&after=${after}`);
    const { items } = await res.json();
    for (const it of items) {
      after = it.n + 1;
      if (it.type === "event") addEvent(it.event);
      else { addMsg("note", it.text, "sent without a user message"); refreshBookings(); }
    }
  } catch (err) { /* server restarting; try again */ }
  setTimeout(poll, 700);
}

async function refreshBookings() {
  const s = await (await fetch(`/api/state?session=${encodeURIComponent(session)}`)).json();
  $("meta").textContent = `model ${s.model} · session ${s.session}`;
  const sel = $("booking"); sel.innerHTML = "";
  for (const b of s.bookings.filter(b => b.status === "confirmed" && b.when === "upcoming")) {
    const o = document.createElement("option"); o.value = b.booking_id;
    o.textContent = `${b.booking_id} ${b.provider_name} ${b.slot.replace("T", " ")}`; sel.appendChild(o);
  }
  return s;
}

$("form").onsubmit = async (ev) => {
  ev.preventDefault();
  const text = $("text").value.trim(); if (!text) return;
  $("text").value = ""; $("send").disabled = true; $("status").textContent = "working";
  addMsg("user", text);
  try {
    const res = await fetch("/api/chat", { method: "POST", body: JSON.stringify({ session, text }) });
    const r = await res.json();
    addMsg("bot", r.text, `${r.outcome} · ${r.run_id}`);
  } catch (err) { addMsg("bot", "The server didn't answer. Is it still running?"); }
  $("send").disabled = false; $("status").textContent = ""; $("text").focus(); refreshBookings();
};

const post = (path, body) => fetch(path, { method: "POST", body: JSON.stringify(body) });
$("force-timeout").onclick = async () => { await post("/api/force-book", { outcomes: ["timeout_booked"] }); addMsg("note", "Demo control: the next book() call will time out after it has actually booked.", "test harness"); };
$("force-taken").onclick = async () => { await post("/api/force-book", { outcomes: ["slot_taken"] }); addMsg("note", "Demo control: the next book() call will find the slot taken.", "test harness"); };
$("cancel").onclick = async () => { const id = $("booking").value; if (!id) return; await post("/api/provider-cancel", { booking_id: id }); addMsg("note", `Demo control: the provider cancelled ${id}. Watch the agent notice it on its own.`, "test harness"); };

(async () => {
  const s = await refreshBookings();
  for (const m of s.history) addMsg(m.role === "user" ? "user" : "bot", m.content, "earlier");
  poll();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
