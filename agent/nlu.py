"""
Pluggable NLU: intent classification + slot extraction.

Four providers, same interface:

  RegexNLUProvider     -- the original deterministic keyword/regex rules.
                          Zero dependencies, zero latency, always available.
                          Used automatically when no LLM provider is
                          configured and as the fallback whenever any LLM
                          call fails.

  AnthropicNLUProvider -- real language understanding via the Claude API.
                          Generalizes to ANY tool in the loaded manifest
                          (not just the two hardcoded travel/support
                          intents), which is what genuinely closes the
                          "unseen tools" gap the regex version couldn't.
                          Requires ANTHROPIC_API_KEY (paid).

  OllamaNLUProvider    -- the same generalization, but via a locally
                          running Ollama server instead of a cloud API:
                          free, no API key, no network egress beyond
                          localhost. Lower quality than Claude (small local
                          models), but zero cost and works offline. See
                          its class docstring for setup.

  GroqNLUProvider      -- free-tier cloud LLM inference via the Groq API
                          (https://console.groq.com). Uses Groq's
                          OpenAI-compatible REST endpoint with fast models
                          like llama-3.1-8b-instant. No local install
                          needed; free API key, no credit card required.
                          Requires GROQ_API_KEY. Uses stdlib urllib only
                          (zero extra pip deps beyond the base stack).

Select the backend with the PRISM_NLU_BACKEND env var (see
default_provider() below); unset falls back to the original
Anthropic-if-key-present-else-regex behavior.

IMPORTANT trio/asyncio note: the official `anthropic` SDK's async client
(`AsyncAnthropic`) is built on asyncio, not trio -- awaiting it directly
inside a trio nursery does not work (they use incompatible event loop
primitives). Rather than pull in a trio-asyncio bridge dependency, this
module uses the *synchronous* `anthropic.Anthropic` client and runs it in
a worker thread via `trio.to_thread.run_sync`, which is the standard,
dependency-free way to call blocking/foreign-async code from trio. See
`_call_anthropic_sync` below. `OllamaNLUProvider` and `GroqNLUProvider`
use the same thread-offload pattern even though their HTTP calls are plain
stdlib `urllib` (not SDKs) -- keeps trio's event loop unblocked even on
slow/hung remote endpoints.

This module never imports `anthropic` (or requires Ollama to be running)
at module load time in a way that breaks the rest of the system if the
package/server/API key is missing -- all LLM providers degrade to
raising a clear, catchable `NLUProviderError`, and `Agent` (see main.py)
always wraps calls to them with a regex fallback via `FallbackNLUProvider`.
"""
from __future__ import annotations

import inspect
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import trio

# --------------------------------------------------------------------------
# Shared result type
# --------------------------------------------------------------------------


@dataclass
class SlotObservation:
    value: Any
    confidence: float = 0.9


@dataclass
class NLUResult:
    intent: Optional[str]
    slots: dict[str, SlotObservation] = field(default_factory=dict)
    source: str = "unknown"  # "regex" | "llm" | "llm_fallback"


# --------------------------------------------------------------------------
# Regex provider (original behavior, extracted verbatim from main.py so
# nothing about the tested Layer 0-4 behavior changes)
# --------------------------------------------------------------------------

_INTENT_KEYWORDS = {
    "book_flight": [
        "book", "flight", "fly", "flying", "plane", "airfare", "fares",
        "ticket to", "tickets to", "trip to", "travel to", "travelling to",
        "traveling to", "outta", "headed to", "heading to",
    ],
    "support_ticket": ["broken", "issue", "not working", "support", "help with my"],
}

# Origin / destination / date extraction lives in agent/travel_parse.py and
# is backed by the world airport gazetteer (agent/airports.py, compiled
# from OurAirports + OpenFlights): any of ~3,200 airports, their cities and
# historic names ("Bombay", "Peking"), IATA codes, in lowercase or with
# informal phrasing ("outta chicago headed to miami next friday",
# "Delhi to Paris tomorrow"). Places the gazetteer doesn't know are still
# captured from phrasing, at lower confidence. Earlier versions used two
# regexes here (`from X to Y on Z`) that only worked for that exact shape.
_DEVICE_RE = re.compile(r"\b(Galaxy [A-Za-z0-9]+|Prism ?\w*)\b", re.IGNORECASE)


class RegexNLUProvider:
    """Deterministic, offline provider -- no model, no network. Knows
    book_flight (via the airport gazetteer) and support_ticket; see the
    LLM providers below for generalising to arbitrary manifest tools."""

    async def understand(
        self,
        text: str,
        *,
        tool_specs: list[dict] | None = None,
        current_slots: dict[str, Any] | None = None,
        current_intent: Optional[str] = None,
    ) -> NLUResult:
        from .travel_parse import extract_route, extract_trip_options

        lowered = text.lower()
        intent = None
        for candidate, keywords in _INTENT_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in keywords):
                intent = candidate
                break

        origin = destination = None
        route_info: dict = {}
        if intent == "book_flight" or (intent is None and current_intent in (None, "book_flight")):
            origin, destination, route_info = extract_route(text)
            # "chicago to miami tomorrow" names no flight keyword, but two
            # recognised airports/cities is an unambiguous travel request.
            if intent is None and route_info.get("origin") == "known" and route_info.get("destination") == "known":
                intent = "book_flight"
        intent = intent or current_intent

        slots: dict[str, SlotObservation] = {}
        if intent == "book_flight":
            conf = {"known": 0.95, "guessed": 0.6}
            if destination:
                slots["destination"] = SlotObservation(destination, conf[route_info["destination"]])
            if origin:
                slots["origin"] = SlotObservation(origin, conf[route_info["origin"]])
            # date, and -- only when mentioned -- return_date, passengers,
            # cabin (optional search_flights parameters in the manifest)
            for key, value in extract_trip_options(text).items():
                slots[key] = SlotObservation(value)
        elif intent == "support_ticket":
            if m := _DEVICE_RE.search(text):
                slots["device_model"] = SlotObservation(m.group(1))
            slots["issue_summary"] = SlotObservation(text.strip())

        return NLUResult(intent=intent, slots=slots, source="regex")


# --------------------------------------------------------------------------
# Anthropic-backed provider
# --------------------------------------------------------------------------

_EXTRACTION_TOOL_SCHEMA = {
    "name": "extract_intent_and_slots",
    "description": (
        "Report the caller's intent and any slot values you can confidently "
        "extract from the utterance, grounded ONLY in what was actually said."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": ["string", "null"],
                "description": "One of the provided candidate tool/intent names, or null if unclear.",
            },
            "slots": {
                "type": "object",
                "description": (
                    "Map of field_name -> {value, confidence}. Only include "
                    "fields you can actually ground in the text; omit anything "
                    "you're guessing at."
                ),
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "value": {},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["value"],
                },
            },
        },
        "required": ["intent", "slots"],
    },
}


def _run_sync_cancel_kwarg() -> dict:
    """trio renamed to_thread.run_sync's cancel-on-cancellation parameter
    from `cancellable` (older) to `abandon_on_cancel` (newer, >=0.20ish).
    Detect which this installation has once, rather than hardcoding either
    name and breaking on the other."""
    params = inspect.signature(trio.to_thread.run_sync).parameters
    if "abandon_on_cancel" in params:
        return {"abandon_on_cancel": True}
    if "cancellable" in params:
        return {"cancellable": True}
    return {}


class NLUProviderError(RuntimeError):
    """Raised when the LLM provider can't produce a result (missing key,
    missing package, network/API error, malformed response). Callers
    should catch this and fall back to RegexNLUProvider."""


def _build_system_prompt(tool_specs: list[dict]) -> str:
    tools_desc = "\n".join(
        f"- {t['name']}: {t.get('description', '')} "
        f"(fields: {', '.join(t.get('parameters', {}).get('properties', {}).keys())})"
        for t in tool_specs
    ) or "(no tools loaded yet)"
    return (
        "You are the language-understanding component of a real-time voice "
        "assistant. Given a user utterance (which may be a PARTIAL, "
        "still-in-progress turn, or a correction after an interruption), "
        "identify which of the following intents/tools it best matches, and "
        "extract any slot values explicitly stated or clearly implied.\n\n"
        f"Candidate intents/tools:\n{tools_desc}\n\n"
        "Rules:\n"
        "- Never invent a value that wasn't stated or clearly implied.\n"
        "- If the utterance is a correction (e.g. 'actually...', 'no wait...'), "
        "extract only the corrected field(s); do not restate unrelated fields.\n"
        "- If intent is genuinely unclear, return intent: null.\n"
        "- Always call the extract_intent_and_slots tool exactly once."
    )


def _call_anthropic_sync(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    text: str,
    current_slots: dict[str, Any],
) -> dict:
    """Blocking call, executed in a worker thread via trio.to_thread.run_sync
    (see module docstring for why: the SDK's async client is asyncio-based,
    incompatible with trio's event loop)."""
    import anthropic  # local import: keep it optional at module load time

    client = anthropic.Anthropic(api_key=api_key)
    user_content = (
        f"Utterance: {text!r}\n"
        f"Slots already known this turn: {json.dumps(current_slots, default=str)}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=system_prompt,
        tools=[_EXTRACTION_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "extract_intent_and_slots"},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_intent_and_slots":
            return block.input
    raise NLUProviderError("model did not return the expected tool_use block")


class AnthropicNLUProvider:
    """Real LLM-backed intent/slot extraction.

    Usage:
        provider = AnthropicNLUProvider()  # reads ANTHROPIC_API_KEY from env
        # or: AnthropicNLUProvider(api_key="sk-...", model="...")

    Raises NLUProviderError (never a raw SDK exception) so callers can
    reliably catch-and-fallback without needing to know anthropic's
    exception hierarchy.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # Overridable via env so this doesn't go stale as model names
        # change; confirm the exact ID your API key has access to.
        self.model = model or os.environ.get("PRISM_NLU_MODEL", "claude-3-5-sonnet-latest")

    async def understand(
        self,
        text: str,
        *,
        tool_specs: list[dict] | None = None,
        current_slots: dict[str, Any] | None = None,
        current_intent: Optional[str] = None,
    ) -> NLUResult:
        if not self.api_key:
            raise NLUProviderError(
                "ANTHROPIC_API_KEY not set -- pass api_key= explicitly or export it. "
                "Falling back to the regex NLU provider."
            )

        system_prompt = _build_system_prompt(tool_specs or [])
        try:
            raw = await trio.to_thread.run_sync(
                lambda: _call_anthropic_sync(
                    api_key=self.api_key,
                    model=self.model,
                    system_prompt=system_prompt,
                    text=text,
                    current_slots=current_slots or {},
                ),
                # let trio's cancellation actually unblock the awaiting task
                # even though the underlying thread (and its outbound HTTP
                # request) can't be forcibly killed -- the parameter was
                # named `cancellable` in older trio, `abandon_on_cancel` in
                # newer trio; try both so this doesn't silently break across
                # trio versions.
                **_run_sync_cancel_kwarg(),
            )
        except NLUProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 -- normalize any SDK/network error
            raise NLUProviderError(f"Anthropic API call failed: {exc}") from exc

        intent = raw.get("intent")
        slots_raw = raw.get("slots", {}) or {}
        slots: dict[str, SlotObservation] = {}
        for field_name, obs in slots_raw.items():
            if not isinstance(obs, dict) or "value" not in obs:
                continue
            slots[field_name] = SlotObservation(
                value=obs["value"],
                confidence=float(obs.get("confidence", 0.85)),
            )
        return NLUResult(intent=intent or current_intent, slots=slots, source="llm")


# --------------------------------------------------------------------------
# Ollama-backed provider (free, fully local -- no API key, no cloud call)
# --------------------------------------------------------------------------


def _call_ollama_sync(
    *,
    host: str,
    model: str,
    system_prompt: str,
    text: str,
    current_slots: dict[str, Any],
) -> dict:
    """Blocking call to a locally running `ollama serve`, executed in a
    worker thread via trio.to_thread.run_sync (same reasoning as
    _call_anthropic_sync above -- keep trio's event loop unblocked).

    Uses only the standard library (urllib) rather than the `ollama` pip
    package or `requests`, so this provider adds zero new dependencies --
    Ollama itself is a separately-installed local server, not a Python
    package.

    Ollama's small local models are generally less reliable at native
    tool-calling than Claude, so this uses the more broadly-compatible
    `format: "json"` mode (forces valid JSON output) plus an explicit
    schema instruction in the prompt, rather than relying on tool-use
    support that not every small local model implements well.
    """
    import json as _json
    import urllib.error
    import urllib.request

    schema_instruction = (
        '\n\nRespond with ONLY a single JSON object, no other text, no markdown '
        'fences, in exactly this shape:\n'
        '{"intent": "<one of the candidate names above, or null>", '
        '"slots": {"<field_name>": {"value": <the value>, "confidence": <0.0-1.0>}}}'
    )
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt + schema_instruction},
            {
                "role": "user",
                "content": (
                    f"Utterance: {text!r}\n"
                    f"Slots already known this turn: {_json.dumps(current_slots, default=str)}"
                ),
            },
        ],
    }
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise NLUProviderError(
            f"couldn't reach Ollama at {host} -- is `ollama serve` running? "
            f"({exc})"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise NLUProviderError(f"Ollama request failed: {exc}") from exc

    content = body.get("message", {}).get("content", "")
    try:
        return _json.loads(content)
    except _json.JSONDecodeError as exc:
        raise NLUProviderError(
            f"Ollama returned non-JSON content (model may not support "
            f"format=json well): {content[:200]!r}"
        ) from exc


class OllamaNLUProvider:
    """Free, fully local intent/slot extraction via a locally running
    Ollama server (https://ollama.com). No API key, no network egress
    beyond localhost.

    Setup (on a machine that can actually reach Ollama's model registry --
    this was built and unit-tested with the network call mocked, since the
    sandbox it was written in blocks that registry; see README):
        1. Install Ollama and run `ollama serve` (often started automatically).
        2. `ollama pull llama3.2:3b` (or another small instruct model).
        3. export PRISM_NLU_BACKEND=ollama
        4. Optionally: export PRISM_OLLAMA_MODEL=llama3.2:3b (defaults shown)
           and/or OLLAMA_HOST=http://localhost:11434 (Ollama's own default).

    Quality note: small local models (1B-3B params) are noticeably less
    reliable at structured extraction than Claude -- expect more missed
    slots and more fallback-to-regex events in the trace. This is a real
    trade for zero cost / zero network dependency, worth validating against
    your actual scenarios before relying on it for a live demo.
    """

    def __init__(self, host: Optional[str] = None, model: Optional[str] = None) -> None:
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.model = model or os.environ.get("PRISM_OLLAMA_MODEL", "llama3.2:3b")

    async def understand(
        self,
        text: str,
        *,
        tool_specs: list[dict] | None = None,
        current_slots: dict[str, Any] | None = None,
        current_intent: Optional[str] = None,
    ) -> NLUResult:
        system_prompt = _build_system_prompt(tool_specs or [])
        try:
            raw = await trio.to_thread.run_sync(
                lambda: _call_ollama_sync(
                    host=self.host,
                    model=self.model,
                    system_prompt=system_prompt,
                    text=text,
                    current_slots=current_slots or {},
                ),
                **_run_sync_cancel_kwarg(),
            )
        except NLUProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NLUProviderError(f"Ollama call failed: {exc}") from exc

        intent = raw.get("intent")
        slots_raw = raw.get("slots", {}) or {}
        slots: dict[str, SlotObservation] = {}
        for field_name, obs in slots_raw.items():
            if not isinstance(obs, dict) or "value" not in obs:
                continue
            slots[field_name] = SlotObservation(
                value=obs["value"],
                confidence=float(obs.get("confidence", 0.75)),
            )
        return NLUResult(intent=intent or current_intent, slots=slots, source="ollama")


# --------------------------------------------------------------------------
# Groq-backed provider (free-tier cloud, OpenAI-compatible REST, no extra deps)
# --------------------------------------------------------------------------


def _call_groq_sync(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    text: str,
    current_slots: dict[str, Any],
) -> dict:
    """Blocking POST to Groq's OpenAI-compatible chat endpoint, executed in
    a worker thread via trio.to_thread.run_sync (same pattern as
    _call_ollama_sync -- keeps trio's event loop unblocked).

    Uses only the standard library (urllib + json) -- no `groq` or `openai`
    pip package needed, keeping this provider dependency-free beyond the
    base stack.

    Groq serves fast open models (Llama 3, Mixtral, Gemma) on generous
    free-tier rate limits. Get a key at https://console.groq.com (no credit
    card required). Default model is llama-3.1-8b-instant -- fast, free,
    and reliable enough at structured JSON extraction for this use-case.
    """
    import json as _json
    import urllib.error
    import urllib.request

    schema_instruction = (
        "\n\nRespond with ONLY a single JSON object, no other text, no markdown "
        "fences, in exactly this shape:\n"
        '{"intent": "<one of the candidate names above, or null>", '
        '"slots": {"<field_name>": {"value": <the value>, "confidence": <0.0-1.0>}}}'
    )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt + schema_instruction},
            {
                "role": "user",
                "content": (
                    f"Utterance: {text!r}\n"
                    f"Slots already known this turn: {_json.dumps(current_slots, default=str)}"
                ),
            },
        ],
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "prism-agent/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:300]
        raise NLUProviderError(
            f"Groq API returned HTTP {exc.code}: {body_text}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NLUProviderError(
            f"Groq API call failed (network/DNS error): {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise NLUProviderError(f"Groq request failed: {exc}") from exc

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        return _json.loads(content)
    except _json.JSONDecodeError as exc:
        raise NLUProviderError(
            f"Groq returned non-JSON content: {content[:200]!r}"
        ) from exc


class GroqNLUProvider:
    """Free-tier cloud intent/slot extraction via the Groq API.

    Uses fast open-weight models (Llama 3, Mixtral, Gemma) hosted on
    Groq's inference hardware. Free API key at https://console.groq.com
    -- no credit card required, generous rate limits.

    No local install required; calls Groq's OpenAI-compatible REST API
    via the standard library, so this adds zero pip dependencies.

    Setup:
        export GROQ_API_KEY=gsk_...
        export PRISM_NLU_BACKEND=groq
        python demo.py

    Optionally override the model:
        export PRISM_GROQ_MODEL=llama-3.1-70b-versatile   # slower, smarter
        export PRISM_GROQ_MODEL=llama3-8b-8192            # alternative fast model
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = model or os.environ.get("PRISM_GROQ_MODEL", "llama-3.1-8b-instant")

    async def understand(
        self,
        text: str,
        *,
        tool_specs: list[dict] | None = None,
        current_slots: dict[str, Any] | None = None,
        current_intent: Optional[str] = None,
    ) -> NLUResult:
        if not self.api_key:
            raise NLUProviderError(
                "GROQ_API_KEY not set -- pass api_key= explicitly or "
                "export GROQ_API_KEY. Get a free key at https://console.groq.com"
            )

        system_prompt = _build_system_prompt(tool_specs or [])
        try:
            raw = await trio.to_thread.run_sync(
                lambda: _call_groq_sync(
                    api_key=self.api_key,
                    model=self.model,
                    system_prompt=system_prompt,
                    text=text,
                    current_slots=current_slots or {},
                ),
                **_run_sync_cancel_kwarg(),
            )
        except NLUProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NLUProviderError(f"Groq API call failed: {exc}") from exc

        intent = raw.get("intent")
        slots_raw = raw.get("slots", {}) or {}
        slots: dict[str, SlotObservation] = {}
        for field_name, obs in slots_raw.items():
            if not isinstance(obs, dict) or "value" not in obs:
                continue
            slots[field_name] = SlotObservation(
                value=obs["value"],
                confidence=float(obs.get("confidence", 0.85)),
            )
        return NLUResult(intent=intent or current_intent, slots=slots, source="groq")


# --------------------------------------------------------------------------
# Fallback-wrapping composite provider
# --------------------------------------------------------------------------


class FallbackNLUProvider:
    """Tries `primary` first; on any NLUProviderError (or any exception,
    to be defensive against SDK-internal errors we didn't anticipate),
    falls back to `fallback`. This is what Agent uses by default so the
    system is never dead in the water without an API key, but gets
    smarter the moment one is configured."""

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback

    async def understand(self, text: str, **kwargs) -> NLUResult:
        try:
            return await self.primary.understand(text, **kwargs)
        except NLUProviderError:
            result = await self.fallback.understand(text, **kwargs)
            result.source = "llm_fallback"
            return result
        except Exception:  # noqa: BLE001 -- never let NLU take the whole agent down
            result = await self.fallback.understand(text, **kwargs)
            result.source = "llm_fallback"
            return result


def default_provider() -> Any:
    """Picks an NLU backend, always wrapped so it degrades to regex on any
    failure:

      PRISM_NLU_BACKEND=groq+ollama  -> Groq -> Ollama -> regex (three-tier chain)
      PRISM_NLU_BACKEND=ollama+groq  -> Ollama -> Groq -> regex (local-first)
      PRISM_NLU_BACKEND=groq         -> GroqNLUProvider -> regex
      PRISM_NLU_BACKEND=ollama       -> OllamaNLUProvider -> regex
      PRISM_NLU_BACKEND=anthropic    -> AnthropicNLUProvider -> regex
      PRISM_NLU_BACKEND=regex        -> RegexNLUProvider only, no fallback
      (unset) + ANTHROPIC_API_KEY    -> AnthropicNLUProvider -> regex
      (unset) + GROQ_API_KEY         -> GroqNLUProvider -> regex
      (unset), nothing else set      -> RegexNLUProvider only

    The groq+ollama / ollama+groq modes use nested FallbackNLUProviders to
    build a three-tier chain without any new machinery:
        FallbackNLUProvider(
            primary_llm,
            FallbackNLUProvider(secondary_llm, regex)
        )
    On each utterance the chain tries primary first; if that raises
    NLUProviderError (key missing, rate-limited, network down) it falls
    through to secondary; if that also fails it falls through to regex.
    Only the *first* LLM that succeeds is used -- there is no consensus or
    blending step.

    Ollama is opt-in via the env var (not auto-detected by probing
    localhost:11434) so that a plain regex-only run never pays the latency
    of an unnecessary connection attempt to a server that isn't running.
    """
    regex = RegexNLUProvider()
    backend = os.environ.get("PRISM_NLU_BACKEND", "").strip().lower()

    if backend == "groq+ollama":
        # Groq (cloud) first, Ollama (local) as offline fallback, regex last.
        ollama_then_regex = FallbackNLUProvider(OllamaNLUProvider(), regex)
        return FallbackNLUProvider(GroqNLUProvider(), ollama_then_regex)
    if backend == "ollama+groq":
        # Ollama (local, zero egress) first, Groq (cloud) when offline, regex last.
        groq_then_regex = FallbackNLUProvider(GroqNLUProvider(), regex)
        return FallbackNLUProvider(OllamaNLUProvider(), groq_then_regex)
    if backend == "groq":
        return FallbackNLUProvider(GroqNLUProvider(), regex)
    if backend == "ollama":
        return FallbackNLUProvider(OllamaNLUProvider(), regex)
    if backend == "regex":
        return regex
    if backend == "anthropic" or (not backend and os.environ.get("ANTHROPIC_API_KEY")):
        return FallbackNLUProvider(AnthropicNLUProvider(), regex)
    if not backend and os.environ.get("GROQ_API_KEY"):
        return FallbackNLUProvider(GroqNLUProvider(), regex)
    return regex
