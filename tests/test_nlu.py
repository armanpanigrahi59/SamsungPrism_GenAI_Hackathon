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


async def test_regex_provider_extracts_lowercase_slots():
    """Typing in lowercase is the common case (nobody reliably capitalizes
    while typing fast) -- an earlier version of the regex required an
    uppercase first letter and silently extracted nothing for lowercase
    input, which made the live demo look hardcoded to its one exact
    example phrase. Extraction now lives in agent/travel_parse.py."""
    provider = RegexNLUProvider()
    result = await provider.understand("book a flight from delhi to paris on the 5th")
    assert result.slots["origin"].value == "delhi"
    assert result.slots["destination"].value == "paris"


async def test_regex_provider_extracts_iso_date_from_airport_picker():
    """The /flights From/To airport-picker (flights.html) sends an
    unambiguous ISO date ("on 2026-11-05") rather than spoken-style "on
    the 5th", because a real search_flights call (flights_provider.py's
    engine) needs an exact YYYY-MM-DD -- a plain ordinal pattern would
    silently truncate this to "20"."""
    provider = RegexNLUProvider()
    result = await provider.understand("book a flight from DEL to CDG on 2026-11-05")
    assert result.slots["origin"].value == "DEL"
    assert result.slots["destination"].value == "CDG"
    assert result.slots["date"].value == "2026-11-05"


async def test_regex_provider_extracts_multiword_city_names():
    provider = RegexNLUProvider()
    result = await provider.understand(
        "book a flight from New York to Los Angeles on the 5th"
    )
    assert result.slots["origin"].value == "New York"
    assert result.slots["destination"].value == "Los Angeles"


async def test_regex_provider_understands_informal_phrasing():
    """Backed by the world airport gazetteer (agent/airports.py): no
    'from X to Y on Z' template needed."""
    provider = RegexNLUProvider()
    result = await provider.understand("yo i need a flight outta chicago headed to miami next friday")
    assert result.intent == "book_flight"
    assert result.slots["origin"].value == "chicago"
    assert result.slots["destination"].value == "miami"
    assert result.slots["date"].value == "next friday"


async def test_regex_provider_infers_travel_intent_from_two_known_places():
    provider = RegexNLUProvider()
    result = await provider.understand("Mumbai to Frankfurt on the 12th")
    assert result.intent == "book_flight"
    assert result.slots["origin"].value == "Mumbai"
    assert result.slots["destination"].value == "Frankfurt"


async def test_regex_provider_lowers_confidence_for_unknown_places():
    provider = RegexNLUProvider()
    result = await provider.understand("book a flight from Gotham to Paris on the 5th")
    assert result.slots["origin"].value == "Gotham"
    assert result.slots["origin"].confidence < result.slots["destination"].confidence


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
