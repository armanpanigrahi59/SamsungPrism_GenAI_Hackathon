"""
Quart-Trio WebSocket bridge: wires the browser frontend to a real Agent
instance over a WebSocket, one fresh Agent per connected client (fully
session-scoped -- two browser tabs never share slot state).

Why Quart-Trio instead of FastAPI/Flask/asyncio: the whole agent core
(coordinator.py, speculation.py, and nlu.py's Groq/Ollama HTTP calls) is
built on trio's structured concurrency -- a trio.CancelScope per in-flight
call is the ENTIRE interruption-handling mechanism (see coordinator.py).
Bridging that to an asyncio web framework would mean running trio inside
asyncio via trio_asyncio (or the reverse), which is exactly the kind of
event-loop impedance mismatch the README already flags for the Anthropic
SDK's asyncio-based client. Quart-Trio is Quart (a Flask-shaped ASGI
framework) running natively on trio, so the WebSocket handler, the Agent,
and every NLU provider's `trio.to_thread.run_sync` call all share one
event loop -- no bridge, no adapter, nothing new to debug.

Pages (frontend/*.html, served by the routes below -- plain files, no
templating engine, no build step):
  /               Static landing page, no WebSocket. Links into the two
                   live pages below.
  /flights         Live assistant scoped to the travel domain.
  /support         Live assistant scoped to the device-support domain.
  /how-it-works    Static architecture explainer, no WebSocket.

Wire protocol (JSON text frames over the WebSocket):

  browser -> server
    {"type": "init", "domain": "flights" | "support" | "all"}
        -- MUST be the first frame sent, right after the socket opens
           (frontend/assets/assistant.js does this automatically). Picks
           which slice of the tool manifest this session's Agent gets --
           see DOMAIN_TOOLS below. A Flights-page agent never even sees
           create_support_ticket/lookup_manual, and vice versa: this is
           real backend scoping, not a frontend-only filter.
    {"type": "text_chunk", "text": "...", "end_of_turn": false}
        -- one per partial "speech" hypothesis; set end_of_turn: true on
           the chunk that closes the turn (mirrors demo.py's scenario).
    {"type": "interruption"}
        -- barge-in: bumps the generation, cancels every in-flight call
           tagged with a stale generation (Layer 0).

  server -> browser
    {"type": "nlu_backend", "chain": "GroqNLUProvider -> OllamaNLUProvider -> RegexNLUProvider"}
        -- sent once on connect, so the UI shows which backend chain is
           actually live instead of guessing from env vars.
    {"type": "manifest", "tools": [...]}
        -- the (domain-filtered) tool manifest actually loaded into this
           session's registry.
    {"type": "action", "action": "filler"|"tool_call"|"cancellation"|
                                  "clarification"|"final_response",
     "payload": {...}, "ts_ms": <float>, "action_id": "..."}
        -- every Action the agent emits, forwarded verbatim and in order.
    {"type": "state", "intent": ..., "slots": {...}, "generation": <int>}
        -- a slot-state snapshot sent after every action, so the UI's
           live trip-summary card never has to poll or guess.
    {"type": "error", "message": "..."}
        -- sent, best-effort, if this session's turn hit an unhandled
           exception, right before the connection is dropped. assistant.js
           surfaces it as an inline hint instead of just going silent.

Run directly with `python server/app.py` (serves the frontend/ folder too,
so there's nothing else to stand up -- open http://127.0.0.1:8000).

Deployment knobs (all optional -- sane defaults for local dev):
  PORT / PRISM_PORT   Port to bind. Most PaaS providers set PORT for you.
  PRISM_HOST          Override the bind address (default: 0.0.0.0 if PORT
                       is set, else 127.0.0.1 -- see run() below).
  PRISM_MAX_TEXT_LENGTH
                       Cap, in characters, on a single text_chunk's text
                       (default 2000). Enforced server-side regardless of
                       what the frontend's own textarea cap allows.
  PRISM_LOG_LEVEL     Python logging level (default INFO).
See README.md's "Deployment" section for a walkthrough and the current
scaling caveats (state is per-WebSocket-connection, in-process memory).

Each connection is individually try/excepted (see ws() below): one
session's bug drops that session with a client-visible error frame, not
the whole process.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from pathlib import Path

import trio
from quart import Response, request, websocket
from quart_trio import QuartTrio

from agent.events import EventType, InputEvent
from agent.main import Agent
from agent.mock_env import MockConfig, MockToolEnvironment
from agent.tools import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "manifests" / "travel_manifest.json"
FRONTEND_DIR = ROOT / "frontend"

# Which manifest tools each page's Agent is allowed to know about. "all"
# (or any unrecognized domain) gets the full manifest -- used as a safe
# fallback, not currently wired to a page.
DOMAIN_TOOLS = {
    "flights": {"search_flights", "book_flight"},
    "support": {"create_support_ticket", "lookup_manual"},
}

# Hard cap on a single text_chunk's text, so one misbehaving or hostile
# client can't balloon memory / NLU request size. Keep in sync with the
# frontend's own MAX_INPUT_LENGTH in assistant.js -- that one exists so the
# textarea gives honest feedback before typing past the limit; this one is
# the actual enforcement, since the frontend's cap is not trustworthy on
# its own (nothing stops a client from skipping the browser entirely).
MAX_TEXT_LENGTH = int(os.environ.get("PRISM_MAX_TEXT_LENGTH", "2000"))

logging.basicConfig(
    level=os.environ.get("PRISM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("prism.server")

app = QuartTrio(__name__, static_folder=None)


def _page(name: str) -> str:
    return (FRONTEND_DIR / name).read_text(encoding="utf-8")


def _filtered_manifest(domain: str | None) -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text())
    allowed = DOMAIN_TOOLS.get(domain or "")
    if allowed is None:
        return manifest
    return {**manifest, "tools": [t for t in manifest.get("tools", []) if t.get("name") in allowed]}


def _describe_nlu_chain(provider: object) -> str:
    """Walks FallbackNLUProvider.primary/.fallback links (see agent/nlu.py)
    into a human-readable string like
    'GroqNLUProvider -> OllamaNLUProvider -> RegexNLUProvider', so the
    frontend can show the judge which backend is actually configured
    instead of re-deriving it from PRISM_NLU_BACKEND itself."""
    names: list[str] = []
    node = provider
    for _ in range(6):  # hard cap: never trust external state to terminate a loop
        if node is None:
            break
        names.append(type(node).__name__)
        primary = getattr(node, "primary", None)
        if primary is not None:
            names.append(type(primary).__name__)
        node = getattr(node, "fallback", None)
    # de-dup consecutive repeats from the walk above without losing order
    deduped: list[str] = []
    for n in names:
        if not deduped or deduped[-1] != n:
            deduped.append(n)
    return " -> ".join(deduped) if deduped else type(provider).__name__


@app.route("/")
async def index():
    return _page("index.html")


@app.route("/flights")
async def flights_page():
    return _page("flights.html")


@app.route("/support")
async def support_page():
    return _page("support.html")


@app.route("/how-it-works")
async def how_it_works_page():
    return _page("how-it-works.html")


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.route("/healthz")
async def healthz():
    # Plain liveness probe for whatever platform this ends up deployed on
    # (Render/Railway/Fly/etc. all expect a cheap 200 to decide a instance
    # is up) -- deliberately does no work beyond confirming the process is
    # responsive, so it stays cheap even under load.
    return {"status": "ok"}


@app.route("/favicon.ico")
async def favicon():
    # No favicon asset -- a bare 204 keeps the browser's automatic request
    # from showing up as a spurious 404 in devtools during a live demo.
    return Response(b"", status=204)


@app.route("/assets/<path:filename>")
async def assets(filename: str):
    # Deliberately narrow: only serves frontend/assets/, and only files
    # that are actually there -- not a general static-file passthrough
    # (no path traversal surface, nothing outside this one folder).
    safe_path = (FRONTEND_DIR / "assets" / filename).resolve()
    assets_root = (FRONTEND_DIR / "assets").resolve()
    if assets_root not in safe_path.parents or not safe_path.is_file():
        return Response("not found", status=404)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # Read as BYTES, always -- this folder holds CSS/JS (text) as well as
    # hero.mp4/hero-poster.jpg (binary). An earlier version of this route
    # called .read_text(encoding="utf-8"), which works for CSS/JS but
    # corrupts (or raises UnicodeDecodeError on) any binary file -- caught
    # before it shipped, by actually requesting hero.mp4 through this route
    # rather than assuming text-mode read_text was fine for everything.
    data = safe_path.read_bytes()
    total = len(data)

    # Range support: Chrome/Safari issue a Range request for <video> even
    # for a same-origin same-size file, and some browsers simply won't
    # start playback without a 206 response to that first request.
    range_header = request.headers.get("Range")
    if range_header:
        match = _RANGE_RE.match(range_header)
        if match:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else total - 1
            end = min(end, total - 1)
            if start <= end < total:
                chunk = data[start : end + 1]
                resp = Response(chunk, status=206, content_type=content_type)
                resp.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
                resp.headers["Accept-Ranges"] = "bytes"
                resp.headers["Content-Length"] = str(len(chunk))
                return resp

    resp = Response(data, content_type=content_type)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(total)
    return resp


_connection_counter = 0


@app.websocket("/ws")
async def ws() -> None:
    # Each connection gets a short id purely for log correlation -- with
    # more than a handful of concurrent users, "the agent errored" in the
    # logs is useless without knowing which of N open sockets it was.
    global _connection_counter
    _connection_counter += 1
    conn_id = _connection_counter
    client = (websocket.scope.get("client") or ("?",))[0]
    log.info("ws[%s] connecting from %s", conn_id, client)

    # First frame picks the domain (see DOMAIN_TOOLS / assistant.js) --
    # everything else about this session is built only after we know it.
    domain = "all"
    try:
        first = json.loads(await websocket.receive())
        if isinstance(first, dict) and first.get("type") == "init":
            domain = first.get("domain", "all")
    except (TypeError, ValueError):
        pass  # fall through with domain="all" rather than drop the connection

    manifest = _filtered_manifest(domain)
    registry = ToolRegistry()
    # Latency wide enough that a real interruption typed mid-search still
    # lands before the mock tool resolves -- same reasoning as demo.py's
    # fixed (300, 300) window, widened here since real typing speed varies.
    env = MockToolEnvironment(MockConfig(latency_ms=(400.0, 900.0)))
    agent = Agent(registry, env)
    log.info("ws[%s] domain=%s nlu=%s", conn_id, domain, _describe_nlu_chain(agent.nlu_provider))

    await websocket.send(json.dumps({
        "type": "nlu_backend",
        "chain": _describe_nlu_chain(agent.nlu_provider),
    }))
    await websocket.send(json.dumps({"type": "manifest", "tools": manifest.get("tools", [])}))

    events_send, events_recv = trio.open_memory_channel(100)
    actions_send, actions_recv = trio.open_memory_channel(100)

    async def pump_actions_to_client() -> None:
        async for action in actions_recv:
            await websocket.send(json.dumps({
                "type": "action",
                "action": action.type.value,
                "payload": action.payload,
                "ts_ms": action.ts_ms,
                "action_id": action.action_id,
            }))
            snap = await agent.slot_state.snapshot()
            await websocket.send(json.dumps({
                "type": "state",
                "intent": snap.intent,
                "slots": snap.slots,
                "generation": snap.generation,
            }))

    async def pump_client_to_events() -> None:
        try:
            while True:
                raw = await websocket.receive()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue  # ignore malformed frames rather than killing the session
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type")
                if msg_type == "text_chunk":
                    text = str(msg.get("text", ""))[:MAX_TEXT_LENGTH]
                    await events_send.send(InputEvent(
                        type=EventType.TEXT_CHUNK,
                        payload={"text": text},
                        end_of_turn=bool(msg.get("end_of_turn", False)),
                    ))
                elif msg_type == "interruption":
                    await events_send.send(InputEvent(type=EventType.INTERRUPTION, payload={}))
        finally:
            await events_send.aclose()

    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(agent.run, events_recv, actions_send)
            nursery.start_soon(pump_actions_to_client)
            await events_send.send(InputEvent(type=EventType.MANIFEST, payload=manifest))
            try:
                await pump_client_to_events()
            finally:
                # Browser tab closed / WS dropped: tear down this session's
                # Agent and both pumps rather than leaking the nursery.
                nursery.cancel_scope.cancel()
    except Exception:
        # A bug in one session's turn (bad NLU response shape, a tool
        # backend raising, etc.) should drop THAT connection cleanly, not
        # take the whole process down or leave the browser hanging with no
        # explanation. Best-effort: tell the client why before it closes --
        # assistant.js surfaces msg.message as an inline hint.
        log.exception("ws[%s] unhandled error", conn_id)
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "The agent hit an unexpected error. Try 'New session'.",
            }))
        except Exception:
            pass
    finally:
        log.info("ws[%s] disconnected", conn_id)


def run() -> None:
    from hypercorn.config import Config
    from hypercorn.trio import serve

    port = int(os.environ.get("PORT", os.environ.get("PRISM_PORT", "8000")))
    # Most PaaS providers (Render, Railway, Fly, Heroku-style buildpacks)
    # set PORT and expect the process to bind every interface, not just
    # loopback. Treat PORT being set as the signal to switch the default
    # bind address -- running locally with no PORT set keeps the original
    # 127.0.0.1-only behavior, so `python server/app.py` on a laptop
    # doesn't silently become reachable from the rest of the LAN. Set
    # PRISM_HOST explicitly to override either way.
    default_host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    host = os.environ.get("PRISM_HOST", default_host)

    config = Config()
    config.bind = [f"{host}:{port}"]
    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"prism-agent web bridge -> http://{display_host}:{port}  (Ctrl+C to stop)")
    log.info(
        "starting on %s:%s (PRISM_NLU_BACKEND=%s)",
        host, port, os.environ.get("PRISM_NLU_BACKEND", "regex (default)"),
    )
    trio.run(serve, app, config)


if __name__ == "__main__":
    run()
