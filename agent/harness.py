"""
Virtual-clock streaming harness: deterministic event replay against the
agent, mirroring the eval kit's "Virtual Clock Streaming Harness" section.

A scenario is a list of (offset_ms, InputEvent) pairs. The harness uses
trio's mock clock so replay is deterministic and fast (no real sleeping),
while cancellation grace periods and latency scoring still behave as if
real time were passing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import trio

from .events import InputEvent


@dataclass
class ScenarioStep:
    offset_ms: float
    event: InputEvent


class Scenario:
    def __init__(self, name: str, steps: list[ScenarioStep]) -> None:
        self.name = name
        self.steps = sorted(steps, key=lambda s: s.offset_ms)

    @classmethod
    def from_events(cls, name: str, timed_events: list[tuple[float, InputEvent]]) -> "Scenario":
        return cls(name, [ScenarioStep(off, evt) for off, evt in timed_events])


async def replay_scenario(
    scenario: Scenario,
    events_in: "trio.MemorySendChannel[InputEvent]",
    *,
    speed: float = 1.0,
) -> None:
    """Send each event at its scheduled offset (relative to replay start).
    speed > 1 replays faster than real time (useful for automated test
    runs); speed = 1.0 mirrors real-time pacing for latency measurement."""
    start = trio.current_time()
    for step in scenario.steps:
        target = start + (step.offset_ms / 1000.0) / speed
        now = trio.current_time()
        if target > now:
            await trio.sleep(target - now)
        await events_in.send(step.event)
    await events_in.aclose()
