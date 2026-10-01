"""
Layer 0 -- shared event/action types.

These mirror the interface contract in the theme guide section 3.1:
inputs are timestamped events, outputs are timestamped actions, and both
flow through async queues.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


_id_counter = itertools.count(1)


def next_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter)}"


def now_ms() -> float:
    return time.monotonic() * 1000.0


# --------------------------------------------------------------------------
# Input events
# --------------------------------------------------------------------------

class EventType(str, Enum):
    TEXT_CHUNK = "text_chunk"          # partial or final transcribed text
    AUDIO_CLIP = "audio_clip"          # raw audio (WAV) reference
    VIDEO_FRAME = "video_frame"        # raw frame (PNG) reference
    INTERRUPTION = "interruption"      # explicit barge-in signal
    TOOL_RESULT = "tool_result"        # async result of a previously dispatched call
    TOOL_ERROR = "tool_error"          # async fault from a previously dispatched call
    MANIFEST = "manifest"              # scenario tool manifest (schema-driven tools)


@dataclass
class InputEvent:
    type: EventType
    payload: dict[str, Any]
    ts_ms: float = field(default_factory=now_ms)
    event_id: str = field(default_factory=lambda: next_id("evt"))
    end_of_turn: bool = False  # set on TEXT_CHUNK when this closes the turn


# --------------------------------------------------------------------------
# Output actions
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    FILLER = "filler"                      # spoken filler / ack / progress narration
    TOOL_CALL = "tool_call"                # non-blocking tool dispatch
    CANCELLATION = "cancellation"          # cancel a previously dispatched call_id
    CLARIFICATION = "clarification"        # ask the user to resolve ambiguity
    FINAL_RESPONSE = "final_response"      # carries the structured state snapshot


@dataclass
class Action:
    type: ActionType
    payload: dict[str, Any]
    ts_ms: float = field(default_factory=now_ms)
    action_id: str = field(default_factory=lambda: next_id("act"))


# --------------------------------------------------------------------------
# Tool call bookkeeping
# --------------------------------------------------------------------------

@dataclass
class DispatchedCall:
    """Bookkeeping record for a tool call in flight or completed."""
    call_id: str
    tool_name: str
    args: dict[str, Any]
    generation: int
    mutates: bool
    idempotency_key: Optional[str] = None
    speculative: bool = False
    stable_key: Optional[tuple] = None  # for Layer 2 salvage cache lookups
    cancel_scope: Any = None            # trio.CancelScope, set once the task starts
    status: str = "pending"             # pending | done | error | cancelled
    result: Any = None
