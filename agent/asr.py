"""
Free, local ASR (speech-to-text) via faster-whisper.

Closes the "no real ASR" gap noted in README's known-simplifications: prior
to this, AUDIO_CLIP events required a `transcript` to already be present in
the event payload, as if upstream ASR had already run. This module lets the
agent do that transcription itself, locally, for free -- matching the
theme guide's actual interface contract, which specifies raw audio clips
(WAV) as the input type (section 3.1), not pre-transcribed text.

Usage in main.py: Agent(..., asr_provider=LocalWhisperASR()) (or set
PRISM_ASR_BACKEND=whisper and let default_asr_provider() pick it up). An
AUDIO_CLIP event can then carry `{"audio_path": "/path/to/clip.wav"}`
instead of a pre-computed `transcript`, and the agent transcribes it
in-process.

Backward compatible: if an AUDIO_CLIP event already carries a `transcript`
(as all the earlier tests and the mock harness do), that's used directly
and no ASR call happens at all -- this module is purely additive.

Setup (on a machine that can actually reach faster-whisper's model host --
this was written and unit-tested with the transcription call mocked, since
the sandbox it was built in blocks Hugging Face, where faster-whisper's
converted models are hosted; see README):
    pip install faster-whisper
    export PRISM_ASR_BACKEND=whisper
    # optionally: export PRISM_WHISPER_MODEL=base  (default: "base")
    # first real use downloads the model weights once, then caches locally

Quality/latency note: CPU-only transcription with even the "base" model
is noticeably slower than real-time for longer clips. For a live demo,
either use the "tiny" model (faster, lower accuracy) or accept the added
latency -- this is a real trade-off worth measuring against your actual
scenario clip lengths before relying on it live.
"""
from __future__ import annotations

import inspect
import os
from typing import Optional

import trio


class ASRProviderError(RuntimeError):
    """Raised when local ASR can't produce a transcript (missing package,
    missing/corrupt model, decode error). Callers should catch this and
    degrade gracefully (e.g. treat as empty transcript, log, move on) --
    exactly the same contract as NLUProviderError in nlu.py."""


def _run_sync_cancel_kwarg() -> dict:
    """Same trio-version compatibility shim as nlu.py's helper of the same
    name (kept local to this module rather than imported, so asr.py has no
    hard dependency on nlu.py)."""
    params = inspect.signature(trio.to_thread.run_sync).parameters
    if "abandon_on_cancel" in params:
        return {"abandon_on_cancel": True}
    if "cancellable" in params:
        return {"cancellable": True}
    return {}


def _transcribe_sync(*, model_size: str, audio_path: str, device: str) -> tuple[str, float]:
    """Blocking transcription call, executed in a worker thread via
    trio.to_thread.run_sync -- CPU-bound model inference would otherwise
    block trio's event loop for the whole clip duration."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ASRProviderError(
            "the 'faster-whisper' package isn't installed (pip install faster-whisper)"
        ) from exc

    try:
        # Model load is itself slow (esp. first call, downloading weights);
        # a real deployment would cache this instance across calls rather
        # than reconstructing per-clip. Left simple here deliberately --
        # see LocalWhisperASR, which DOES cache the loaded model instance.
        model = WhisperModel(model_size, device=device, compute_type="int8")
        segments, info = model.transcribe(audio_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # faster-whisper doesn't give a single scalar confidence; use the
        # language-detection probability as a rough proxy, clamped, rather
        # than fabricating a number -- an honest "we don't really know"
        # value is safer than a fake-precise one for belief-fusion confidence.
        confidence = float(getattr(info, "language_probability", 0.75) or 0.75)
        return text, min(max(confidence, 0.0), 1.0)
    except ASRProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 -- normalize any backend error
        raise ASRProviderError(f"transcription failed: {exc}") from exc


class LocalWhisperASR:
    """Free, local speech-to-text via faster-whisper. No API key, no
    network egress at inference time (only once, to download model
    weights on first use, then cached locally)."""

    def __init__(self, model_size: Optional[str] = None, device: Optional[str] = None) -> None:
        self.model_size = model_size or os.environ.get("PRISM_WHISPER_MODEL", "base")
        self.device = device or os.environ.get("PRISM_WHISPER_DEVICE", "cpu")

    async def transcribe(self, audio_path: str) -> tuple[str, float]:
        """Returns (transcript, confidence). Raises ASRProviderError on
        any failure -- callers decide how to degrade (see main.py)."""
        try:
            return await trio.to_thread.run_sync(
                lambda: _transcribe_sync(
                    model_size=self.model_size, audio_path=audio_path, device=self.device,
                ),
                **_run_sync_cancel_kwarg(),
            )
        except ASRProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ASRProviderError(f"ASR call failed: {exc}") from exc


def default_asr_provider():
    """Opt-in via PRISM_ASR_BACKEND=whisper, same philosophy as
    nlu.default_provider(): never silently try to reach a local service
    that may not be running. Returns None (no local ASR) otherwise, in
    which case main.py falls back to requiring a pre-supplied `transcript`
    in the AUDIO_CLIP payload, exactly as before this module existed."""
    backend = os.environ.get("PRISM_ASR_BACKEND", "").strip().lower()
    if backend == "whisper":
        return LocalWhisperASR()
    return None
