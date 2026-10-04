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

Run directly with `python server/app.py` (serves the frontend/ folder too,
so there's nothing else to stand up -- open http://127.0.0.1:8000).
"""
from __future__ import annotations

import json
import mimetypes
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


@app.websocket("/ws")
async def ws() -> None:
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
                msg_type = msg.get("type")
                if msg_type == "text_chunk":
                    await events_send.send(InputEvent(
                        type=EventType.TEXT_CHUNK,
                        payload={"text": msg.get("text", "")},
                        end_of_turn=bool(msg.get("end_of_turn", False)),
                    ))
                elif msg_type == "interruption":
                    await events_send.send(InputEvent(type=EventType.INTERRUPTION, payload={}))
        finally:
            await events_send.aclose()

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


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    from hypercorn.config import Config
    from hypercorn.trio import serve

    config = Config()
    config.bind = [f"{host}:{port}"]
    print(f"prism-agent web bridge -> http://{host}:{port}  (Ctrl+C to stop)")
    trio.run(serve, app, config)


if __name__ == "__main__":
    run()
