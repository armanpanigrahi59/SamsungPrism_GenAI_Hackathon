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
  /airports       Airport explorer: every airport in the model, filterable by
                   continent / country / text, with top destinations.
  /how-it-works    Static architecture explainer, no WebSocket.
  /api/airports?q= GET, JSON. World airport search (agent/airports.py)
                   backing the From/To autocomplete on /flights.
  /api/resolve?q=  GET, JSON. What a place string ("bombay", "NYC") resolves
                   to -- used for the trip card's friendly labels.
  /api/routes/popular?n=
                   GET, JSON. A random sample of busy real routes from the
                   flight model, for the /flights quick-pick chips.
  /api/model       GET, JSON. Flight model metadata: counts, sources,
                   licences, calibration.
  /api/version     GET, JSON. API version the pages check on load.
  /api/airports/popular, /api/airports/browse, /api/countries,
  /api/airport/<code>
                   GET, JSON. Explorer data: hubs, filtered pages of
                   airports, countries per continent, one airport's top
                   destinations.
  /api/search?from=&to=&date=&return=&pax=&cabin=
                   GET, JSON. Direct search (shareable links; WebSocket
                   fallback). Same engine as the agent's search_flights.
  /api/fares?from=&to=&date=&pax=&cabin=&days=
                   GET, JSON. Lowest fare per day around a date.
  /api/book        POST, JSON. Simulated booking (WebSocket fallback).

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
    {"type": "book", "offer_id": "...", "passenger_name": "..."}
        -- explicit, user-confirmed booking of an offer this session's
           search returned. Handled outside the speculative pipeline on
           purpose: a state-changing call is never speculated (Layer 1
           only fires read-only tools), so the UI's "Book" button calls the
           session's book_flight backend directly.

  server -> browser
    {"type": "nlu_backend", "chain": "GroqNLUProvider -> OllamaNLUProvider -> RegexNLUProvider"}
        -- sent once on connect, so the UI shows which backend chain is
           actually live instead of guessing from env vars.
    {"type": "flights_backend", "chain": "OfflineFlightModel (3,244 airports · 28,052 routes)"}
        -- same idea as nlu_backend, for search_flights/book_flight's data
           source (agent/flights_provider.py; "MockToolEnvironment" when
           PRISM_FLIGHTS_BACKEND=mock).
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
    {"type": "tool_result", "call_id": "...", "tool": "...", "generation": <int>,
     "speculative": <bool>, "status": "done"|"error", "result": {...}}
        -- a finished (not cancelled) tool call's result, via the agent's
           tool_result_listener hook. Not an agent Action: the agent's
           output protocol is unchanged. The UI uses it to render flight
           offers and to stop the timeline card's spinner.
    {"type": "booking", "ok": <bool>, "booking": {...} | "message": "..."}
        -- reply to a "book" frame.
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
  PRISM_FLIGHTS_BACKEND
                       "model" (default): offline world flight model, no
                       API keys (agent/flights_provider.py). "mock": the
                       original fake generator in agent/mock_env.py.
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
import random
import re
import sys
from pathlib import Path

# Always run against the agent package that sits next to this file. Without
# this, `python server/app.py` imports whatever `agent` an earlier
# `pip install .` copied into site-packages -- an old flight model and old
# APIs behind brand-new pages, which looks like "nothing works".
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if sys.path[:1] != [_REPO_ROOT]:
    sys.path.insert(0, _REPO_ROOT)

import trio
from quart import Response, request, websocket
from quart_trio import QuartTrio

from agent.airports import (
    browse_airports,
    continents,
    countries,
    get_airport,
    model_meta,
    popular_airports,
    primary_airports,
    resolve_place,
    search_airports,
)
from agent.events import EventType, InputEvent
from agent.flights_provider import (
    FlightModelError,
    OfflineFlightProvider,
    default_flight_env,
    describe_flight_backend,
    fare_calendar,
    network,
    search_schedule,
    top_destinations,
)
from agent.main import Agent
from agent.mock_env import MockConfig, MockToolEnvironment
from agent.tools import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent

# Bumped whenever the HTTP/WebSocket API the frontend relies on changes.
# The pages read it from /api/version on load and show an "out of date
# server" banner when an older process is still serving -- the HTML/JS are
# read from disk per request, so a server started before an update serves
# new pages against old endpoints, which otherwise just looks broken.
API_VERSION = 3
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


class _MergedToolEnvironment:
    """Combines two tool environments' backends into one `.as_registry()`
    (the only interface Agent actually needs -- see agent/main.py). Used so
    a single Agent instance can serve the offline flight model's
    search_flights/book_flight (agent/flights_provider.py) alongside the
    still-mock
    create_support_ticket/lookup_manual, without needing a second env
    object threaded through Agent's constructor. `primary`'s tools win on
    any name collision."""

    def __init__(self, primary: object, secondary: object) -> None:
        self._registry = {**secondary.as_registry(), **primary.as_registry()}

    def as_registry(self) -> dict:
        return self._registry


def _build_tool_env() -> tuple[object, object]:
    """Returns (merged_env, flights_env) -- callers pass merged_env into
    Agent() and use flights_env only for the backend-chain log line
    (describe_flight_backend needs the un-merged object to tell the
    offline model apart from the plain mock)."""
    # search_flights/book_flight: the offline world flight model by default
    # (agent/flights_provider.py), or mock_env.py's generator when
    # PRISM_FLIGHTS_BACKEND=mock.
    # create_support_ticket/lookup_manual: always mock -- there's no
    # support-ticketing backend wired up here, only the travel one.
    # Latency on the mock layer is wide enough that a real interruption
    # typed mid-search still lands before the mock tool resolves -- same
    # reasoning as demo.py's fixed (300, 300) window, widened here since
    # real typing speed varies.
    mock = MockToolEnvironment(MockConfig(latency_ms=(400.0, 900.0)))
    flights = default_flight_env(mock)
    return _MergedToolEnvironment(flights, mock), flights


@app.before_serving
async def warm_up() -> None:
    # Parse the flight model and build the airport / route indexes once at
    # startup, so the first user's first keystroke doesn't pay ~0.3 s for it
    # (that delay was enough to make the barge-in demo's first NLU pass land
    # after the interruption).
    search_airports("warm")
    network()
    meta = model_meta().get("counts", {})
    log.info("flight model ready: %s airports, %s routes", meta.get("airports"), meta.get("airport_pairs"))


@app.route("/")
async def index():
    return _page("index.html")


@app.route("/flights")
async def flights_page():
    return _page("flights.html")


@app.route("/support")
async def support_page():
    return _page("support.html")


@app.route("/airports")
async def airports_page():
    return _page("airports.html")


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


@app.route("/api/airports")
async def api_airports():
    # Backs the From/To autocomplete fields on /flights (see
    # frontend/assets/assistant.js's airport-lookup handling and
    # agent/airports.py's module docstring for the dataset this searches).
    # GET, read-only, no auth needed -- this is reference data (airport
    # codes/names/cities), not anything user- or session-specific.
    query = request.args.get("q", "")[:80]
    return {"airports": search_airports(query, limit=8)}


@app.route("/api/resolve")
async def api_resolve():
    query = request.args.get("q", "")[:80]
    found = primary_airports(query) or resolve_place(query)
    return {"query": query, "airports": [a.to_dict() for a in found[:4]]}


_POPULAR_CACHE: list[dict] = []


def _popular_routes() -> list[dict]:
    """Well-known routes between the world's best-connected airports
    (ranked by both ends' connectivity, at least daily service) --
    computed once from the model."""
    if not _POPULAR_CACHE:
        net = network()
        scored = []
        for src, dests in net.out.items():
            a = get_airport(src)
            if a is None or a.kind != "large":
                continue
            for dst, services in dests.items():
                b = get_airport(dst)
                weekly = sum(s.weekly for s in services)
                if b is None or b.kind != "large" or a.city == b.city or src > dst or weekly < 7:
                    continue
                scored.append(((a.weight * b.weight) ** 0.5, weekly, a, b))
        scored.sort(key=lambda t: t[0], reverse=True)
        seen_cities = set()
        for _, weekly, a, b in scored:
            key = tuple(sorted((a.city, b.city)))
            if key in seen_cities:
                continue
            seen_cities.add(key)
            _POPULAR_CACHE.append({
                "from": a.city, "from_iata": a.iata, "from_country": a.country,
                "to": b.city, "to_iata": b.iata, "to_country": b.country, "weekly_flights": weekly,
            })
            if len(_POPULAR_CACHE) >= 300:
                break
    return _POPULAR_CACHE


@app.route("/api/routes/popular")
async def api_popular_routes():
    try:
        n = max(1, min(12, int(request.args.get("n", "4"))))
    except ValueError:
        n = 4
    pool = list(_popular_routes())
    random.shuffle(pool)
    picked, countries = [], set()
    for r in pool:  # spread the sample across countries
        if r["from_country"] in countries or r["to_country"] in countries:
            continue
        countries.update((r["from_country"], r["to_country"]))
        picked.append(r if random.random() < 0.5 else {
            **r, "from": r["to"], "from_iata": r["to_iata"], "from_country": r["to_country"],
            "to": r["from"], "to_iata": r["from_iata"], "to_country": r["from_country"],
        })
        if len(picked) >= n:
            break
    return {"routes": picked}


@app.route("/api/model")
async def api_model():
    return model_meta()


@app.route("/api/version")
async def api_version():
    counts = model_meta().get("counts", {})
    return {"api": API_VERSION, "app": "prism-agent", "pid": os.getpid(), "airports": counts.get("airports"),
            "airports_with_routes": counts.get("airports_with_routes"), "countries": counts.get("countries"),
            "routes": counts.get("airport_pairs"), "airlines": counts.get("carriers")}


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


@app.route("/api/airports/popular")
async def api_popular_airports():
    return {"airports": popular_airports(_int_arg("n", 12, 1, 50), request.args.get("continent", "")[:2].upper())}


@app.route("/api/airports/browse")
async def api_browse_airports():
    return browse_airports(
        continent=request.args.get("continent", "")[:2].upper(),
        country=request.args.get("country", "")[:2].upper(),
        query=request.args.get("q", "")[:80],
        sort=request.args.get("sort", "routes")[:10],
        page=_int_arg("page", 1, 1, 10_000),
        page_size=_int_arg("page_size", 48, 1, 200),
        routes_only=request.args.get("routes_only", "") in ("1", "true", "yes"),
    )


@app.route("/api/countries")
async def api_countries():
    return {"continents": continents(), "countries": countries(request.args.get("continent", "")[:2].upper())}


@app.route("/api/airport/<code>")
async def api_airport(code: str):
    airport = get_airport(code[:3])
    if airport is None:
        return {"error": f"unknown airport '{code[:3]}'"}, 404
    return {**airport.to_dict(), "aliases": list(airport.aliases),
            "destinations": top_destinations(airport.iata, _int_arg("n", 15, 1, 100))}


def _search_args() -> dict:
    a = request.args
    return {
        "origin": a.get("from", "")[:80], "destination": a.get("to", "")[:80], "date": a.get("date", "")[:40],
        "return_date": a.get("return", "")[:40] or None, "passengers": a.get("pax", "1")[:2],
        "cabin": a.get("cabin", "economy")[:20],
    }


@app.route("/api/search")
async def api_search():
    """Direct search (no agent): shareable links, and the page's fallback
    when the WebSocket isn't connected."""
    q = _search_args()
    return search_schedule(q["origin"], q["destination"], q["date"], passengers=q["passengers"],
                           cabin=q["cabin"], return_date=q["return_date"])


@app.route("/api/fares")
async def api_fares():
    """Lowest fare per day around a date -- the results page's date strip."""
    q = _search_args()
    return fare_calendar(q["origin"], q["destination"], q["date"], passengers=q["passengers"],
                         cabin=q["cabin"], days=_int_arg("days", 3, 1, 7))


_REST_BOOKER = OfflineFlightProvider(latency_ms=(0.0, 0.0))


@app.route("/api/book", methods=["POST"])
async def api_book():
    """Booking without a WebSocket (fallback path). Same idempotent,
    simulated booking as the agent's book_flight."""
    body = await request.get_json(silent=True) or {}
    try:
        booking = _REST_BOOKER.book(str(body.get("offer_id", ""))[:200], str(body.get("passenger_name", ""))[:120])
    except FlightModelError as exc:
        return {"ok": False, "message": str(exc)}, 400
    return {"ok": True, "offer_id": booking["offer_id"], "booking": booking}


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
    env, flights_env = _build_tool_env()
    agent = Agent(registry, env)

    # Several tasks write to this socket (action pump, tool-result hook,
    # booking replies); one lock keeps frames from interleaving.
    send_lock = trio.Lock()

    async def ws_send(obj: dict) -> None:
        async with send_lock:
            await websocket.send(json.dumps(obj, default=str))

    async def on_tool_result(record) -> None:
        await ws_send({
            "type": "tool_result",
            "call_id": record.call_id,
            "tool": record.tool_name,
            "args": record.args,
            "generation": record.generation,
            "speculative": record.speculative,
            "status": record.status,
            "result": record.result,
        })

    agent.tool_result_listener = on_tool_result

    async def handle_book(msg: dict) -> None:
        backend = env.as_registry().get("book_flight")
        if backend is None or domain not in ("flights", "all"):
            await ws_send({"type": "booking", "ok": False, "message": "Booking isn't available on this page."})
            return
        offer_id = str(msg.get("offer_id", ""))[:200]
        passenger = str(msg.get("passenger_name", ""))[:120]
        try:
            booking = await backend({"offer_id": offer_id, "passenger_name": passenger})
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not fatal
            await ws_send({"type": "booking", "ok": False, "offer_id": offer_id, "message": str(exc)})
            return
        await ws_send({"type": "booking", "ok": True, "offer_id": offer_id, "booking": booking})

    log.info(
        "ws[%s] domain=%s nlu=%s flights=%s",
        conn_id, domain,
        _describe_nlu_chain(agent.nlu_provider),
        describe_flight_backend(flights_env),
    )

    await ws_send({"type": "nlu_backend", "chain": _describe_nlu_chain(agent.nlu_provider)})
    await ws_send({"type": "flights_backend", "chain": describe_flight_backend(flights_env)})
    await ws_send({"type": "manifest", "tools": manifest.get("tools", [])})

    events_send, events_recv = trio.open_memory_channel(100)
    actions_send, actions_recv = trio.open_memory_channel(100)

    async def pump_actions_to_client() -> None:
        async for action in actions_recv:
            await ws_send({
                "type": "action",
                "action": action.type.value,
                "payload": action.payload,
                "ts_ms": action.ts_ms,
                "action_id": action.action_id,
            })
            snap = await agent.slot_state.snapshot()
            await ws_send({
                "type": "state",
                "intent": snap.intent,
                "slots": snap.slots,
                "generation": snap.generation,
            })

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
                elif msg_type == "book":
                    # run beside the receive loop, so a booking in flight
                    # never delays the next text chunk / interruption
                    if nursery_holder:
                        nursery_holder[0].start_soon(handle_book, msg)
                    else:
                        await handle_book(msg)
        finally:
            await events_send.aclose()

    nursery_holder: list = []
    try:
        async with trio.open_nursery() as nursery:
            nursery_holder.append(nursery)
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
            await ws_send({
                "type": "error",
                "message": "The agent hit an unexpected error. Try 'New session'.",
            })
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

    from server.port_guard import claim_port, exclusive_bind_config

    if "PORT" not in os.environ:
        # Local run: an older server still holding the port (Windows lets
        # it share the port silently) would keep answering the browser with
        # the old API. Stop it, or move to a free port and say so.
        port = claim_port(host, port, api_version=API_VERSION)
    config = exclusive_bind_config(Config)()
    config.bind = [f"{host}:{port}"]
    display_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1", "localhost") else host
    print(f"prism-agent web bridge -> http://{display_host}:{port}  (Ctrl+C to stop)")
    log.info(
        "starting on %s:%s (PRISM_NLU_BACKEND=%s, api v%s)",
        host, port, os.environ.get("PRISM_NLU_BACKEND", "regex (default)"), API_VERSION,
    )
    try:
        trio.run(serve, app, config)
    except OSError as exc:
        raise SystemExit(f"Could not listen on {host}:{port} ({exc}). Is another server still running? "
                         f"Set PRISM_PORT to use a different port.") from exc
    except KeyboardInterrupt:
        print("prism-agent stopped.")


if __name__ == "__main__":
    run()
