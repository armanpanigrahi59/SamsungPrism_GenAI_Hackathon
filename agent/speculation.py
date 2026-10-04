"""
Layer 1 -- speculative execution engine.

The core architectural thesis: instead of treating interruption recovery as
a rare edge case bolted onto a reactive agent, this engine makes
speculate-then-cancel-if-wrong the *normal* operating mode. As partial
slot-filling text streams in (before end-of-turn), we rank candidate
intents/slot-fills by a lightweight confidence heuristic and speculatively
dispatch the top candidate's read-only tool calls immediately.

Two outcomes when the turn finalizes or a correction lands:
  - Speculation matches the finalized slots -> the result is often already
    available, so "time to first substantive action" can predate
    end-of-turn entirely (this is what the response-latency score rewards).
  - Speculation misses -> Coordinator.reconcile() cancels it via the
    generation mechanism, exactly like a real user-initiated interruption.
    Because misses are common and cheap to recover from, the whole system
    gets extensive exercise of the cancellation path on almost every
    scenario, not just ones explicitly tagged "interruption".

Only read-only tools are ever spectulated on. Mutating tools always wait
for a finalized, confirmed slot state (never speculatively book/create --
that would violate Safety objective #4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .coordinator import Coordinator
from .state import SlotState
from .tools import ToolRegistry


@dataclass
class IntentCandidate:
    intent: str
    slots: dict[str, Any]
    confidence: float  # 0..1, heuristic
    ready_tool: Optional[str] = None  # tool that can fire once required slots present


# A tiny, dependency-free confidence heuristic: proportion of a tool's
# required args already present in the candidate's slot fill, weighted by
# how much text we've seen. Deliberately simple -- the architecture is the
# differentiator, not the NLU. Swap in a real classifier without touching
# the engine below.
def score_candidate(
    slots: dict[str, Any],
    required_fields: list[str],
    *,
    turn_progress: float,
) -> float:
    if not required_fields:
        return 0.0
    filled = sum(1 for f in required_fields if slots.get(f) not in (None, ""))
    completeness = filled / len(required_fields)
    # Blend completeness with how far into the turn we are: early partial
    # fills are lower-confidence even if the fields happen to be present,
    # since they're more likely to be corrected before end-of-turn.
    return round(0.4 * turn_progress + 0.6 * completeness, 4)


SPECULATION_THRESHOLD = 0.55  # min confidence before we'll speculatively dispatch


class SpeculativeEngine:
    def __init__(
        self,
        coordinator: Coordinator,
        slot_state: SlotState,
        registry: ToolRegistry,
        *,
        speculation_stable_keys: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.slot_state = slot_state
        self.registry = registry
        # per-tool: which slot fields form the "stable key" for salvage
        # cache reuse (see salvage.py). Defaults to all required fields.
        self.speculation_stable_keys = speculation_stable_keys or {}
        self._already_speculated: set[tuple] = set()

    def _stable_key_for(self, tool_name: str, args: dict) -> tuple:
        spec = self.registry.specs.get(tool_name)
        keys = self.speculation_stable_keys.get(tool_name)
        if keys is None and spec is not None:
            # required params plus any optional ones actually supplied, so a
            # business-class or round-trip search never reuses the cached
            # economy / one-way result for the same route and date
            keys = tuple(sorted(set(spec.parameters.get("required", [])) | set(args.keys())))
        keys = keys or tuple(sorted(args.keys()))
        return tuple((k, args.get(k)) for k in sorted(keys))

    async def maybe_speculate(
        self,
        candidate: IntentCandidate,
        *,
        turn_progress: float,
    ) -> Optional[str]:
        """Given a ranked candidate, speculatively dispatch its read-only
        tool if confidence clears the threshold and we haven't already
        speculated on this exact (tool, args) this turn. Returns the
        call_id if dispatched, else None."""
        if candidate.confidence < SPECULATION_THRESHOLD:
            return None
        if candidate.ready_tool is None:
            return None
        tool_name = candidate.ready_tool
        if self.registry.mutates(tool_name):
            return None  # never speculate on state-changing tools

        extraction = self.registry.extract_args(tool_name, candidate.slots)
        args = extraction["args"] if isinstance(extraction, dict) and "args" in extraction else extraction
        missing = extraction.get("missing_required", []) if isinstance(extraction, dict) else []
        if missing:
            return None  # not enough to actually call yet

        dedup_key = (tool_name, tuple(sorted(args.items())))
        if dedup_key in self._already_speculated:
            return None
        self._already_speculated.add(dedup_key)

        backend = self.registry.get_backend(tool_name)
        if backend is None:
            return None

        stable_key = self._stable_key_for(tool_name, args)
        record = await self.coordinator.dispatch(
            tool_name,
            args,
            backend,
            mutates=False,
            speculative=True,
            stable_key=stable_key,
        )
        return record.call_id if record else None

    def reset_turn(self) -> None:
        """Call at end-of-turn / after finalization so the next turn's
        speculation isn't deduped against this turn's attempts."""
        self._already_speculated.clear()
