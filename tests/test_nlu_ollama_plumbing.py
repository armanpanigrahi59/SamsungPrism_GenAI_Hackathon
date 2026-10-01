"""
Exercises OllamaNLUProvider's integration code (thread offloading, JSON
response parsing, connection-error handling) by monkeypatching the one
function that makes the real HTTP call to a local Ollama server
(`_call_ollama_sync`). Same rigor as test_nlu_anthropic_plumbing.py --
this environment can't actually run `ollama serve` (network policy blocks
Ollama's model registry, so no model could be pulled even if the binary
were installed), so this validates everything up to that boundary.
"""
import agent.nlu as nlu_module
from agent.nlu import NLUProviderError, OllamaNLUProvider


async def test_ollama_provider_parses_json_response(monkeypatch):
    def fake_call(**kwargs):
        assert kwargs["host"] == "http://localhost:11434"
        assert kwargs["model"] == "llama3.2:3b"
        return {
            "intent": "search_flights",
            "slots": {
                "destination": {"value": "Berlin", "confidence": 0.7},
            },
        }

    monkeypatch.setattr(nlu_module, "_call_ollama_sync", fake_call)
    provider = OllamaNLUProvider()
    result = await provider.understand(
        "to Berlin",
        tool_specs=[{"name": "search_flights", "description": "...", "parameters": {}}],
        current_slots={},
        current_intent=None,
    )
    assert result.intent == "search_flights"
    assert result.slots["destination"].value == "Berlin"
    assert result.source == "ollama"


async def test_ollama_provider_respects_env_overrides(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:9999")
    monkeypatch.setenv("PRISM_OLLAMA_MODEL", "qwen2.5:1.5b")
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {"intent": None, "slots": {}}

    monkeypatch.setattr(nlu_module, "_call_ollama_sync", fake_call)
    provider = OllamaNLUProvider()
    await provider.understand("hello")
    assert captured["host"] == "http://localhost:9999"
    assert captured["model"] == "qwen2.5:1.5b"


async def test_ollama_provider_raises_clean_error_when_server_unreachable(monkeypatch):
    def fake_call(**kwargs):
        raise NLUProviderError("couldn't reach Ollama at http://localhost:11434 -- is `ollama serve` running?")

    monkeypatch.setattr(nlu_module, "_call_ollama_sync", fake_call)
    provider = OllamaNLUProvider()
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "ollama serve" in str(exc)


def test_default_provider_selects_ollama_via_env(monkeypatch):
    from agent.nlu import FallbackNLUProvider, RegexNLUProvider, default_provider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PRISM_NLU_BACKEND", "ollama")
    provider = default_provider()
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.primary, OllamaNLUProvider)
    assert isinstance(provider.fallback, RegexNLUProvider)


def test_default_provider_backend_regex_forces_regex_even_with_api_key(monkeypatch):
    from agent.nlu import RegexNLUProvider, default_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("PRISM_NLU_BACKEND", "regex")
    provider = default_provider()
    assert isinstance(provider, RegexNLUProvider)
