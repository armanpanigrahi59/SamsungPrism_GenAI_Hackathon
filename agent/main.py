"""
Agent: wires Layers 0-4 into a runnable full-duplex loop.

Consumes InputEvents from events_in, produces Actions on actions_out.
This is intentionally a thin orchestrator -- all the interesting logic
lives in coordinator.py (Layer 0), speculation.py (Layer 1), salvage.py
(Layer 2), belief.py (Layer 3), tools.py (Layer 4), and nlu.py (language
understanding: real LLM-backed extraction when ANTHROPIC_API_KEY is set,
deterministic regex fallback otherwise -- see nlu.py's module docstring).

Language understanding as slow-path work: per the theme guide's own
architecture ("Slow Path: Executes asynchronous tools, multimodal
processing, and complex reasoning"), NLU extraction on partial/in-progress
text is dispatched through the SAME generation-tagged cancellable mechanism
as tool calls (Coordinator.dispatch). A stale LLM call for text that's been
superseded by a correction or interruption gets cancelled exactly like a
stale tool call -- no separate mechanism needed. The end-of-turn chunk is
the one exception: that extraction is awaited synchronously, because a
final response has to be grounded in a settled result, not a speculative
one still in flight.
"""
from __future__ import annotations

from typing import Any, Optional

import trio

from .asr import ASRProviderError, default_asr_provider
from .belief import BeliefFusion, ModalityObservation
from .coordinator import Coordinator
from .events import Action, EventType, InputEvent
from .mock_env import MockToolEnvironment
from .nlu import NLUResult, default_provider
from .salvage import SalvageCache
from .speculation import IntentCandidate, SpeculativeEngine, score_candidate
from .state import SlotState
from .tools import ToolRegistry
from .trace import TraceLogger

# Legacy intent-label -> tool mapping, kept only for the regex fallback
# provider (whose intent labels are the abstract "book_flight" /
# "support_ticket" rather than an actual manifest tool name). The LLM
# provider is instructed to return the tool's own name as the intent, so
# for it `intent in registry.specs` is already true and this mapping is
# never consulted -- that's what lets the LLM path generalize to tools
# this mapping has never heard of.
_LEGACY_INTENT_TOOL = {
    "book_flight": "search_flights",
    "support_ticket": "lookup_manual",
}


def _describe_call(tool_name: str, args: dict) -> str:
    """'search flights: origin Delhi, destination Paris, date 5th' -- a
    tool-agnostic sentence for the final response."""
    parts = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in args.items() if v not in ("", None))
    return f"{tool_name.replace('_', ' ')}: {parts}" if parts else tool_name.replace("_", " ")


class Agent:
    def __init__(
        self,
        registry: ToolRegistry,
        tool_env: MockToolEnvironment,
        *,
        trace: Optional[TraceLogger] = None,
        nlu_provider: Optional[Any] = None,
        asr_provider: Optional[Any] = None,
    ) -> None:
        self.trace = trace or TraceLogger()
        self.slot_state = SlotState()
        self.registry = registry
        for name, fn in tool_env.as_registry().items():
            self.registry.register_backend(name, fn)
        self.salvage = SalvageCache()
        self._char_budget = 120  # heuristic turn-progress denominator
        # Backend selectable via PRISM_NLU_BACKEND:
        #   groq+ollama (recommended), groq, ollama, anthropic, regex.
        # Always degrades to regex on any failure -- see nlu.py.
        self.nlu_provider = nlu_provider or default_provider()
        # Local Whisper ASR, opt-in via PRISM_ASR_BACKEND=whisper; None
        # means AUDIO_CLIP events must carry a pre-supplied `transcript`
        # (the original behavior) -- see asr.py.
        self.asr_provider = asr_provider or default_asr_provider()
        # Optional async callback(DispatchedCall) for finished tool calls --
        # see Coordinator.result_listener. server/app.py uses it to stream
        # results (flight offers) to the browser.
        self.tool_result_listener = None

    def _tool_specs_for_nlu(self) -> list[dict]:
        return [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in self.registry.specs.values()
        ]

    def _resolve_ready_tool(self, intent: Optional[str]) -> Optional[str]:
        if intent is None:
            return None
        # Legacy mapping takes priority: the regex provider's intent labels
        # ("book_flight", "support_ticket") are conceptual intents, not
        # literal tool names, and deliberately point at the read-only tool
        # for the "search first, confirm later" flow -- even though
        # "book_flight" also happens to be the name of the real mutating
        # tool in this manifest. Checking registry.specs first would let
        # that collision silently resolve to the mutating tool, which
        # speculation then correctly (but silently) refuses to touch,
        # killing the whole speculative search. Only fall through to
        # registry.specs for genuinely novel tool names the LLM path
        # returns that this mapping has never heard of (the "unseen tools"
        # case Layer 4 targets).
        if intent in _LEGACY_INTENT_TOOL:
            return _LEGACY_INTENT_TOOL[intent]
        if intent in self.registry.specs:
            return intent  # LLM path: intent IS the tool name
        return None

    async def run(
        self,
        events_in: "trio.MemoryReceiveChannel[InputEvent]",
        actions_out: "trio.MemorySendChannel[Action]",
    ) -> None:
        async with trio.open_nursery() as nursery:
            coordinator = Coordinator(self.slot_state, actions_out, trace_logger=self.trace)
            coordinator.attach_nursery(nursery)
            coordinator.attach_salvage_cache(self.salvage)
            coordinator.result_listener = self.tool_result_listener
            speculative = SpeculativeEngine(coordinator, self.slot_state, self.registry)
            belief = BeliefFusion(self.slot_state, coordinator)

            accumulated_text = ""

            async for event in events_in:
                self.trace.log("event", {"type": event.type.value, "payload": event.payload})

                if event.type == EventType.MANIFEST:
                    self.registry.load_manifest(event.payload)

                elif event.type == EventType.INTERRUPTION:
                    new_gen = await self.slot_state.bump_generation("interruption")
                    await coordinator.reconcile(new_gen)
                    await coordinator.emit_filler("Go ahead, I'm listening.", reason="interruption_ack")
                    accumulated_text = ""
                    speculative.reset_turn()

                elif event.type == EventType.TEXT_CHUNK:
                    chunk = event.payload.get("text", "")
                    accumulated_text = (accumulated_text + " " + chunk).strip()
                    turn_progress = min(1.0, len(accumulated_text) / self._char_budget)

                    # Fast-path ack: instant, heuristic, never waits on NLU
                    # (objective #1 -- floor management must not be gated on
                    # slow-path reasoning latency).
                    if 0 < len(accumulated_text) < 20:
                        await coordinator.emit_filler("Mm-hm...", reason="progress")

                    if event.end_of_turn:
                        # Authoritative: awaited synchronously, because the
                        # final response has to be grounded in a settled
                        # result, not one still racing in the background.
                        result = await self._run_nlu(accumulated_text)
                        await self._apply_nlu_result(result, coordinator, belief)
                        await self._finalize_turn(coordinator, speculative, accumulated_text)
                        accumulated_text = ""
                    else:
                        await self._dispatch_speculative_nlu(
                            coordinator, belief, speculative, accumulated_text, turn_progress,
                        )

                elif event.type == EventType.AUDIO_CLIP:
                    # Two paths: a pre-supplied `transcript` is used as-is
                    # (back-compat with tests/harness scenarios that already
                    # provide one); otherwise, if a local ASR provider is
                    # configured (PRISM_ASR_BACKEND=whisper -- see asr.py)
                    # and the event carries `audio_path`, transcribe it here.
                    # Either way, the resulting transcript is routed through
                    # the SAME NLU provider as text, so a configured LLM/local
                    # model genuinely understands audio-turn corrections too.
                    transcript = event.payload.get("transcript")
                    payload_conf = event.payload.get("confidence")
                    if transcript is None and self.asr_provider is not None:
                        audio_path = event.payload.get("audio_path")
                        if audio_path:
                            try:
                                transcript, asr_conf = await self.asr_provider.transcribe(audio_path)
                                if payload_conf is None:
                                    payload_conf = asr_conf
                                self.trace.log("asr_result", {
                                    "audio_path": audio_path, "transcript": transcript, "confidence": asr_conf,
                                })
                            except ASRProviderError as exc:
                                self.trace.log("asr_error", {"audio_path": audio_path, "error": str(exc)})
                                transcript = ""
                    transcript = transcript or ""
                    result = await self._run_nlu(transcript)
                    await self._apply_nlu_result(
                        result, coordinator, belief, modality="audio", confidence_override=payload_conf,
                    )

                elif event.type == EventType.VIDEO_FRAME:
                    # Vision grounding stays a clean seam (payload carries
                    # pre-extracted `grounded_field` / `grounded_value`) --
                    # out of scope for this pass, which targeted text/audio
                    # intent understanding. See README "known simplifications".
                    field = event.payload.get("grounded_field")
                    value = event.payload.get("grounded_value")
                    conf = event.payload.get("confidence")
                    if field:
                        disputed = await belief.observe(
                            ModalityObservation(field=field, value=value, modality="video", raw_confidence=conf)
                        )
                        if disputed:
                            new_gen = await self.slot_state.current_generation()
                            await coordinator.reconcile(new_gen)

                elif event.type in (EventType.TOOL_RESULT, EventType.TOOL_ERROR):
                    self.trace.log("external_tool_event", event.payload)

            await actions_out.aclose()

    # -- NLU plumbing --------------------------------------------------

    async def _run_nlu(self, text: str) -> NLUResult:
        if not text:
            return NLUResult(intent=None, slots={})
        snapshot = await self.slot_state.snapshot()
        return await self.nlu_provider.understand(
            text,
            tool_specs=self._tool_specs_for_nlu(),
            current_slots=snapshot.slots,
            current_intent=snapshot.intent,
        )

    async def _apply_nlu_result(
        self,
        result: NLUResult,
        coordinator: Coordinator,
        belief: BeliefFusion,
        *,
        modality: str = "text",
        confidence_override: Optional[float] = None,
    ) -> None:
        self.trace.log("nlu_result", {
            "intent": result.intent, "source": result.source,
            "slots": {k: v.value for k, v in result.slots.items()},
        })
        if result.intent:
            await self.slot_state.set_intent(result.intent, bump=False)
        for field_name, obs in result.slots.items():
            conf = confidence_override if confidence_override is not None else obs.confidence
            disputed = await belief.observe(
                ModalityObservation(field=field_name, value=obs.value, modality=modality, raw_confidence=conf)
            )
            if disputed:
                new_gen = await self.slot_state.current_generation()
                await coordinator.reconcile(new_gen)

    async def _dispatch_speculative_nlu(
        self,
        coordinator: Coordinator,
        belief: BeliefFusion,
        speculative: SpeculativeEngine,
        text: str,
        turn_progress: float,
    ) -> None:
        """Fire-and-forget NLU extraction for a non-final chunk, dispatched
        through Layer 0's cancellable mechanism (see module docstring)."""
        current_gen = await self.slot_state.current_generation()
        if coordinator.has_pending("nlu_extract", current_gen):
            return  # an extraction for this generation is already in flight

        async def _extract_and_react(args: dict) -> dict:
            result = await self._run_nlu(args["text"])
            await self._apply_nlu_result(result, coordinator, belief)
            if result.intent:
                snapshot = await self.slot_state.snapshot()
                ready_tool = self._resolve_ready_tool(result.intent)
                required = []
                if ready_tool and ready_tool in self.registry.specs:
                    required = self.registry.specs[ready_tool].parameters.get("required", [])
                candidate = IntentCandidate(
                    intent=result.intent,
                    slots=snapshot.slots,
                    confidence=score_candidate(snapshot.slots, required, turn_progress=turn_progress),
                    ready_tool=ready_tool,
                )
                await speculative.maybe_speculate(candidate, turn_progress=turn_progress)
            return {"intent": result.intent, "source": result.source}

        await coordinator.dispatch(
            "nlu_extract", {"text": text}, _extract_and_react,
            mutates=False, speculative=True,
        )

    async def _current_intent(self) -> Optional[str]:
        snap = await self.slot_state.snapshot()
        return snap.intent

    async def _finalize_turn(
        self,
        coordinator: Coordinator,
        speculative: SpeculativeEngine,
        text: str,
    ) -> None:
        snapshot = await self.slot_state.snapshot()
        tool_name = self._resolve_ready_tool(snapshot.intent)
        if tool_name and self.registry.has_backend(tool_name):
            extraction = self.registry.extract_args(tool_name, snapshot.slots)
            args = extraction["args"]
            missing = extraction["missing_required"]
            if missing:
                await coordinator.emit_clarification(
                    f"I still need: {', '.join(missing)}.", field=missing[0]
                )
            else:
                stable_key = speculative._stable_key_for(tool_name, args)
                cached = self.salvage.get(tool_name, stable_key)
                in_flight = coordinator.find_in_flight(tool_name, stable_key)
                summary = _describe_call(tool_name, args)
                if cached is not None:
                    await coordinator.publish_cached_result(tool_name, args, cached)
                    await coordinator.emit_final_response(
                        f"(from cache) Here's what I found -- {summary}."
                    )
                elif in_flight is not None:
                    # A speculative call already dispatched with the exact
                    # same tool + stable key is still running -- firing a
                    # second, identical call would be pure waste.
                    await coordinator.emit_final_response(f"Already on it -- {summary}.")
                else:
                    backend = self.registry.get_backend(tool_name)
                    await coordinator.dispatch(
                        tool_name, args, backend, mutates=self.registry.mutates(tool_name),
                        stable_key=stable_key,
                    )
                    await coordinator.emit_final_response(f"On it -- {summary}.")
        elif snapshot.intent:
            await coordinator.emit_clarification(
                f"I understood you want to '{snapshot.intent}', but I don't have a tool for that.",
                field=None,
            )
        else:
            await coordinator.emit_final_response(f"Got it: {text}")
        speculative.reset_turn()
