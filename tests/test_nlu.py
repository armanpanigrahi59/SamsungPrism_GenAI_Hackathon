"""
NLU module: regex provider behavior, and FallbackNLUProvider's degrade-
gracefully contract (can't test AnthropicNLUProvider's real API call here
since this environment has no ANTHROPIC_API_KEY -- see README -- but the
fallback wiring itself is fully testable with a fake failing provider).
"""
import pytest

from agent.nlu import (
    FallbackNLUProvider,
    NLUProviderError,
    NLUResult,
    RegexNLUProvider,
    SlotObservation,
    default_provider,
)


async def test_regex_provider_extracts_flight_slots():
    provider = RegexNLUProvider()
    result = await provider.understand("book a flight from Delhi to Paris on the 5th")
    assert result.intent == "book_flight"
    assert result.slots["destination"].value == "Paris"
    assert result.slots["origin"].value == "Delhi"
    assert result.source == "regex"


async def test_regex_provider_falls_back_to_current_intent_on_correction():
    provider = RegexNLUProvider()
    result = await provider.understand("actually to Tokyo", current_intent="book_flight")
    assert result.intent == "book_flight"
    assert result.slots["destination"].value == "Tokyo"


async def test_regex_provider_support_ticket():
    provider = RegexNLUProvider()
    result = await provider.understand("my Galaxy Prism screen is broken, please help with my device")
    assert result.intent == "support_ticket"
    assert "device_model" in result.slots


class _AlwaysFailsProvider:
    async def understand(self, text, **kwargs):
        raise NLUProviderError("simulated failure (e.g. no API key / network error)")


class _CrashesProvider:
    async def understand(self, text, **kwargs):
        raise RuntimeError("unexpected SDK-internal error")


async def test_fallback_provider_uses_fallback_on_nlu_error():
    provider = FallbackNLUProvider(_AlwaysFailsProvider(), RegexNLUProvider())
    result = await provider.understand("book a flight from Delhi to Paris on the 5th")
    assert result.intent == "book_flight"
    assert result.source == "llm_fallback"


async def test_fallback_provider_uses_fallback_on_unexpected_exception():
    """Defensive: even a non-NLUProviderError exception from the primary
    (e.g. an SDK internals change we didn't anticipate) must not take the
    whole agent down -- it should still degrade to the regex fallback."""
    provider = FallbackNLUProvider(_CrashesProvider(), RegexNLUProvider())
    result = await provider.understand("book a flight from Delhi to Paris on the 5th")
    assert result.intent == "book_flight"
    assert result.source == "llm_fallback"


def test_default_provider_is_regex_only_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = default_provider()
    assert isinstance(provider, RegexNLUProvider)


def test_default_provider_wraps_with_fallback_when_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-for-test")
    provider = default_provider()
    assert isinstance(provider, FallbackNLUProvider)
    assert isinstance(provider.fallback, RegexNLUProvider)
