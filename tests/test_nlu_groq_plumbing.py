"""
Exercises GroqNLUProvider's integration code (thread offloading, JSON
response parsing, HTTP/network error handling) by monkeypatching the one
function that makes the real REST call (`_call_groq_sync`).  Same rigor
and structure as test_nlu_anthropic_plumbing.py and
test_nlu_ollama_plumbing.py -- no live API call, validates everything up
to the network boundary.
"""
import agent.nlu as nlu_module
from agent.nlu import GroqNLUProvider, NLUProviderError


async def test_groq_provider_parses_json_response(monkeypatch):
    def fake_call(**kwargs):
        assert kwargs["api_key"] == "gsk-fake-test-key"
        assert kwargs["model"] == "llama-3.1-8b-instant"
        return {
            "intent": "search_flights",
            "slots": {
                "destination": {"value": "Tokyo", "confidence": 0.95},
                "origin": {"value": "Delhi", "confidence": 0.9},
            },
        }

    monkeypatch.setattr(nlu_module, "_call_groq_sync", fake_call)
    provider = GroqNLUProvider(api_key="gsk-fake-test-key")
    result = await provider.understand(
        "from Delhi to Tokyo",
        tool_specs=[{"name": "search_flights", "description": "...", "parameters": {}}],
        current_slots={},
        current_intent=None,
    )
    assert result.intent == "search_flights"
    assert result.slots["destination"].value == "Tokyo"
    assert result.slots["destination"].confidence == 0.95
    assert result.slots["origin"].value == "Delhi"
    assert result.source == "groq"


async def test_groq_provider_raises_clean_error_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = GroqNLUProvider(api_key=None)
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "GROQ_API_KEY" in str(exc)


async def test_groq_provider_wraps_http_errors(monkeypatch):
    def fake_call(**kwargs):
        raise NLUProviderError("Groq API returned HTTP 401: Invalid API key")

    monkeypatch.setattr(nlu_module, "_call_groq_sync", fake_call)
    provider = GroqNLUProvider(api_key="gsk-fake-test-key")
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "HTTP 401" in str(exc)


async def test_groq_provider_wraps_network_errors(monkeypatch):
    def fake_call(**kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(nlu_module, "_call_groq_sync", fake_call)
    provider = GroqNLUProvider(api_key="gsk-fake-test-key")
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "Groq API call failed" in str(exc)


async def test_groq_provider_falls_back_to_current_intent_when_none_returned(monkeypatch):
    def fake_call(**kwargs):
        return {"intent": None, "slots": {}}

    monkeypatch.setattr(nlu_module, "_call_groq_sync", fake_call)
    provider = GroqNLUProvider(api_key="gsk-fake-test-key")
    result = await provider.understand("um, actually", current_intent="book_flight")
    assert result.intent == "book_flight"
    assert result.source == "groq"


async def test_groq_provider_respects_env_overrides(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env-key")
    monkeypatch.setenv("PRISM_GROQ_MODEL", "llama-3.1-70b-versatile")
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {"intent": None, "slots": {}}

    monkeypatch.setattr(nlu_module, "_call_groq_sync", fake_call)
    provider = GroqNLUProvider()
    await provider.understand("hello")
    assert captured["api_key"] == "gsk-env-key"
    assert captured["model"] == "llama-3.1-70b-versatile"


def test_default_provider_selects_groq_via_env(monkeypatch):
    from agent.nlu import FallbackNLUProvider, RegexNLUProvider, default_provider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    monkeypatch.setenv("PRISM_NLU_BACKEND", "groq")
    provider = default_provider()
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.primary, GroqNLUProvider)
    assert isinstance(provider.fallback, RegexNLUProvider)


def test_default_provider_auto_selects_groq_when_key_present_and_no_anthropic(monkeypatch):
    from agent.nlu import FallbackNLUProvider, RegexNLUProvider, default_provider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PRISM_NLU_BACKEND", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    provider = default_provider()
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.primary, GroqNLUProvider)
    assert isinstance(provider.fallback, RegexNLUProvider)


def test_default_provider_groq_plus_ollama_builds_three_tier_chain(monkeypatch):
    """groq+ollama -> FallbackNLUProvider(Groq, FallbackNLUProvider(Ollama, regex))"""
    from agent.nlu import (
        FallbackNLUProvider,
        OllamaNLUProvider,
        RegexNLUProvider,
        default_provider,
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    monkeypatch.setenv("PRISM_NLU_BACKEND", "groq+ollama")
    provider = default_provider()

    # Outer: Groq -> (Ollama -> regex)
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.primary, GroqNLUProvider)
    # Inner fallback: Ollama -> regex
    assert isinstance(provider.fallback, FallbackNLUProvider)
    assert isinstance(provider.fallback.primary, OllamaNLUProvider)
    assert isinstance(provider.fallback.fallback, RegexNLUProvider)


def test_default_provider_ollama_plus_groq_builds_three_tier_chain(monkeypatch):
    """ollama+groq -> FallbackNLUProvider(Ollama, FallbackNLUProvider(Groq, regex))"""
    from agent.nlu import (
        FallbackNLUProvider,
        OllamaNLUProvider,
        RegexNLUProvider,
        default_provider,
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    monkeypatch.setenv("PRISM_NLU_BACKEND", "ollama+groq")
    provider = default_provider()

    # Outer: Ollama -> (Groq -> regex)
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.primary, OllamaNLUProvider)
    # Inner fallback: Groq -> regex
    assert isinstance(provider.fallback, FallbackNLUProvider)
    assert isinstance(provider.fallback.primary, GroqNLUProvider)
    assert isinstance(provider.fallback.fallback, RegexNLUProvider)


async def test_groq_plus_ollama_falls_through_to_ollama_on_groq_failure(monkeypatch):
    """Integration: if Groq raises NLUProviderError, Ollama result is used."""

    def groq_fail(**kwargs):
        raise NLUProviderError("simulated Groq rate-limit")

    def ollama_ok(**kwargs):
        return {
            "intent": "book_flight",
            "slots": {"destination": {"value": "Paris", "confidence": 0.8}},
        }

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    monkeypatch.setenv("PRISM_NLU_BACKEND", "groq+ollama")
    monkeypatch.setattr(nlu_module, "_call_groq_sync", groq_fail)
    monkeypatch.setattr(nlu_module, "_call_ollama_sync", ollama_ok)

    from agent.nlu import default_provider

    provider = default_provider()
    result = await provider.understand("fly to Paris")

    assert result.intent == "book_flight"
    assert result.slots["destination"].value == "Paris"
    # FallbackNLUProvider stamps source="llm_fallback" when the primary fails
    assert result.source == "llm_fallback"
