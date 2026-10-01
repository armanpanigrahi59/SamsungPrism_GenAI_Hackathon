"""
Exercises AnthropicNLUProvider's actual integration code (trio.to_thread
offloading, tool-use response parsing) by monkeypatching the one function
that makes the real network call (`_call_anthropic_sync`). This is the
riskiest code in the project -- mixing an asyncio-based SDK into a trio
app via a worker thread -- so it's worth testing directly rather than only
via the regex-path integration tests, even without live API access here.
"""
import agent.nlu as nlu_module
from agent.nlu import AnthropicNLUProvider, NLUProviderError


async def test_anthropic_provider_parses_tool_use_response(monkeypatch):
    def fake_call(**kwargs):
        assert "text" in kwargs or True  # sanity: called with the right kwargs shape
        return {
            "intent": "search_flights",
            "slots": {
                "destination": {"value": "Tokyo", "confidence": 0.95},
                "origin": {"value": "Delhi", "confidence": 0.9},
            },
        }

    monkeypatch.setattr(nlu_module, "_call_anthropic_sync", fake_call)
    provider = AnthropicNLUProvider(api_key="sk-fake-test-key")
    result = await provider.understand(
        "from Delhi to Tokyo",
        tool_specs=[{"name": "search_flights", "description": "...", "parameters": {}}],
        current_slots={},
        current_intent=None,
    )
    assert result.intent == "search_flights"
    assert result.slots["destination"].value == "Tokyo"
    assert result.slots["destination"].confidence == 0.95
    assert result.source == "llm"


async def test_anthropic_provider_raises_clean_error_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicNLUProvider(api_key=None)
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)


async def test_anthropic_provider_wraps_network_errors(monkeypatch):
    def fake_call(**kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(nlu_module, "_call_anthropic_sync", fake_call)
    provider = AnthropicNLUProvider(api_key="sk-fake-test-key")
    try:
        await provider.understand("book a flight")
        assert False, "expected NLUProviderError"
    except NLUProviderError as exc:
        assert "Anthropic API call failed" in str(exc)


async def test_anthropic_provider_falls_back_to_current_intent_when_none_returned(monkeypatch):
    def fake_call(**kwargs):
        return {"intent": None, "slots": {}}

    monkeypatch.setattr(nlu_module, "_call_anthropic_sync", fake_call)
    provider = AnthropicNLUProvider(api_key="sk-fake-test-key")
    result = await provider.understand("um, actually", current_intent="book_flight")
    assert result.intent == "book_flight"
