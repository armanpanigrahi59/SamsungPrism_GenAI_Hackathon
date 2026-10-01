"""
Exercises LocalWhisperASR's integration code by monkeypatching the one
function that does real transcription (`_transcribe_sync`), plus the
Agent-level wiring that routes AUDIO_CLIP events with an `audio_path`
through a configured ASR provider. This environment can't actually run
faster-whisper end-to-end (its converted models are hosted on Hugging
Face, which this sandbox's network policy blocks), so this validates
everything up to that boundary -- same approach as the NLU plumbing tests.
"""
import trio

import agent.asr as asr_module
from agent.asr import ASRProviderError, LocalWhisperASR, default_asr_provider


async def test_local_whisper_transcribe_returns_text_and_confidence(monkeypatch):
    def fake_transcribe(**kwargs):
        assert kwargs["audio_path"] == "/tmp/clip.wav"
        return "book a flight to Tokyo", 0.92

    monkeypatch.setattr(asr_module, "_transcribe_sync", fake_transcribe)
    asr = LocalWhisperASR(model_size="tiny")
    text, confidence = await asr.transcribe("/tmp/clip.wav")
    assert text == "book a flight to Tokyo"
    assert confidence == 0.92


async def test_local_whisper_raises_clean_error_on_missing_package(monkeypatch):
    def fake_transcribe(**kwargs):
        raise ASRProviderError("the 'faster-whisper' package isn't installed (pip install faster-whisper)")

    monkeypatch.setattr(asr_module, "_transcribe_sync", fake_transcribe)
    asr = LocalWhisperASR()
    try:
        await asr.transcribe("/tmp/clip.wav")
        assert False, "expected ASRProviderError"
    except ASRProviderError as exc:
        assert "faster-whisper" in str(exc)


def test_default_asr_provider_opt_in_via_env(monkeypatch):
    monkeypatch.delenv("PRISM_ASR_BACKEND", raising=False)
    assert default_asr_provider() is None

    monkeypatch.setenv("PRISM_ASR_BACKEND", "whisper")
    provider = default_asr_provider()
    assert isinstance(provider, LocalWhisperASR)


async def test_agent_transcribes_audio_path_via_configured_asr(build_agent, manifest, autojump_clock):
    """End-to-end: an AUDIO_CLIP event with no pre-supplied `transcript`,
    only an `audio_path`, gets transcribed via a fake ASR provider and the
    result flows through belief fusion exactly like a text chunk would."""
    from agent.events import EventType, InputEvent
    from tests.conftest import manifest_event

    class _FakeASR:
        async def transcribe(self, audio_path):
            return "book a flight from Delhi to Paris on the 5th", 0.88

    agent, registry, env = build_agent()
    agent.asr_provider = _FakeASR()

    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)
        await events_in.send(manifest_event(manifest))
        await events_in.send(InputEvent(
            type=EventType.AUDIO_CLIP,
            payload={"audio_path": "/tmp/fake_clip.wav"},
        ))
        await trio.sleep(0.3)
        await events_in.aclose()
        await trio.sleep(0.2)
    await actions_out.aclose()

    snapshot = await agent.slot_state.snapshot()
    assert snapshot.intent == "book_flight"
    assert snapshot.slots.get("destination") == "Paris"

    asr_events = [e for e in agent.trace.entries if e["kind"] == "asr_result"]
    assert asr_events, "expected an asr_result trace entry"
    assert asr_events[0]["data"]["transcript"] == "book a flight from Delhi to Paris on the 5th"


async def test_agent_falls_back_gracefully_on_asr_error(build_agent, manifest, autojump_clock):
    from agent.events import EventType, InputEvent
    from tests.conftest import manifest_event

    class _FailingASR:
        async def transcribe(self, audio_path):
            raise ASRProviderError("simulated transcription failure")

    agent, registry, env = build_agent()
    agent.asr_provider = _FailingASR()

    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)
        await events_in.send(manifest_event(manifest))
        await events_in.send(InputEvent(
            type=EventType.AUDIO_CLIP,
            payload={"audio_path": "/tmp/fake_clip.wav"},
        ))
        await trio.sleep(0.3)
        await events_in.aclose()
        await trio.sleep(0.2)
    await actions_out.aclose()

    # Must not crash the agent -- degrade to an empty transcript.
    error_events = [e for e in agent.trace.entries if e["kind"] == "asr_error"]
    assert error_events, "expected an asr_error trace entry"
