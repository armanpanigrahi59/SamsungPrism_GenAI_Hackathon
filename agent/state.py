"""
Layer 0 -- SlotState: the single source of truth for session-scoped slots.

Every state-modifying decision (dispatch, cancellation, final response) reads
from or writes to this object. It is guarded by a trio.Lock because fast-path
and slow-path tasks both touch it concurrently.

The `generation` counter is the crux of the whole architecture: it is bumped
on every interruption or slot correction, and every in-flight tool call is
tagged with the generation it was dispatched under. When a call's generation
falls behind the current one, it is stale and gets cancelled -- this is what
gives us "prompt cancellation of invalidated calls" almost for free.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import trio


@dataclass
class SlotSnapshot:
    """Immutable-ish view of slot state at a point in time, for grounding
    final responses and for Layer 2's stable-key cache lookups."""
    intent: Optional[str]
    slots: dict[str, Any]
    generation: int

    def stable_subset(self, keys: tuple[str, ...]) -> tuple:
        """A hashable projection of just the slots that determine whether a
        cached partial result is still reusable (see salvage.py)."""
        return tuple((k, self.slots.get(k)) for k in sorted(keys))


class SlotState:
    """Session-scoped mutable slot state, generation-tracked."""

    def __init__(self) -> None:
        self._lock = trio.Lock()
        self.intent: Optional[str] = None
        self.slots: dict[str, Any] = {}
        self.generation: int = 0
        # per-field confidence + source, used by Layer 3 belief fusion;
        # harmless / unused if Layer 3 isn't wired in.
        self.field_confidence: dict[str, float] = {}
        self.field_source: dict[str, str] = {}

    async def snapshot(self) -> SlotSnapshot:
        async with self._lock:
            return SlotSnapshot(
                intent=self.intent,
                slots=dict(self.slots),
                generation=self.generation,
            )

    async def bump_generation(self, reason: str = "") -> int:
        """Call this on any interruption or correction that invalidates
        in-flight work. Returns the new generation."""
        async with self._lock:
            self.generation += 1
            return self.generation

    async def set_intent(self, intent: str, *, bump: bool = True) -> int:
        async with self._lock:
            changed = intent != self.intent
            self.intent = intent
            if changed and bump:
                self.generation += 1
            return self.generation

    async def update_slot(
        self,
        key: str,
        value: Any,
        *,
        confidence: float = 1.0,
        source: str = "text",
        bump: bool = True,
    ) -> int:
        """Apply a localized slot correction. Bumps generation by default
        since a slot change can invalidate in-flight calls that depended on
        the old value (objective #3: session slot tracking + corrections)."""
        async with self._lock:
            prev = self.slots.get(key)
            changed = prev != value
            # Confidence-gated overwrite: a low-confidence modality shouldn't
            # clobber a high-confidence existing value silently (Layer 3
            # relies on this; harmless default for text-only use).
            prev_conf = self.field_confidence.get(key, 0.0)
            if changed and confidence < prev_conf:
                return self.generation
            self.slots[key] = value
            self.field_confidence[key] = confidence
            self.field_source[key] = source
            if changed and bump:
                self.generation += 1
            return self.generation

    async def current_generation(self) -> int:
        async with self._lock:
            return self.generation


def idempotency_key(tool_name: str, args: dict[str, Any], generation: int) -> str:
    """Hash of (tool, args, generation) so we never double-fire a
    state-changing call for the same logical request (objective #4 / Safety
    category: 'zero duplicate state-changing calls')."""
    payload = json.dumps(
        {"tool": tool_name, "args": args, "generation": generation},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
