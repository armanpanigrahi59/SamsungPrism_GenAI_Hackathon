"""
Runnable demo: streams a realistic interruption scenario through the agent
and prints a human-readable timeline, then dumps the full trace log --
useful both for local sanity-checking and as a judge-facing walkthrough.

Run:
    python demo.py

NLU backend is selected via PRISM_NLU_BACKEND (see .env / README).
Load the .env first:
    PowerShell: Get-Content .env | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } |
                  ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$($k.Trim())" $v.Trim() }
    bash:       set -a && source .env && set +a

If using PRISM_NLU_BACKEND=ollama or groq+ollama, make sure ollama serve is running:
    $env:OLLAMA_MODELS = 'D:\ollama-models'
    Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList 'serve'
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import trio

from agent.events import EventType, InputEvent
from agent.main import Agent
from agent.mock_env import MockConfig, MockToolEnvironment
from agent.tools import ToolRegistry

MANIFEST_PATH = Path(__file__).parent / "manifests" / "travel_manifest.json"


async def consume_actions(actions_out):
    async for action in actions_out:
        print(f"  [{action.type.value:>14}] {json.dumps(action.payload, default=str)}")


async def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    registry = ToolRegistry()
    # Deliberately slow tool latency so the interruption actually lands
    # mid-flight -- this is what exercises the cancellation path visibly.
    env = MockToolEnvironment(MockConfig(latency_ms=(300.0, 300.0), seed=7))
    agent = Agent(registry, env)

    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    print("=== Scenario: booking a flight, then barging in with a correction ===\n")

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)
        nursery.start_soon(consume_actions, actions_out)

        await events_in.send(InputEvent(type=EventType.MANIFEST, payload=manifest))

        print("> user: \"book a flight from Delhi to Paris on the 5th\"")
        await events_in.send(InputEvent(
            type=EventType.TEXT_CHUNK,
            payload={"text": "book a flight from Delhi to Paris on the 5th"},
        ))
        await trio.sleep(0.08)  # speculative search fires, still in flight (300ms)

        print("> user interrupts: \"actually...\"")
        await events_in.send(InputEvent(type=EventType.INTERRUPTION, payload={}))

        print("> user: \"...from Delhi to Tokyo on the 5th\"\n")
        await events_in.send(InputEvent(
            type=EventType.TEXT_CHUNK,
            payload={"text": "actually from Delhi to Tokyo on the 5th"},
            end_of_turn=True,
        ))

        await trio.sleep(5.0)  # give real LLM NLU calls time to complete (Groq ~1-3s)
        await events_in.aclose()
        await trio.sleep(0.2)

    await actions_out.aclose()

    print("\n=== Final slot state ===")
    snapshot = await agent.slot_state.snapshot()
    print(json.dumps({"intent": snapshot.intent, "slots": snapshot.slots, "generation": snapshot.generation}, indent=2))

    print("\n=== Score-relevant checks ===")
    cancellations = agent.trace.cancellations()
    print(f"Stale calls cancelled: {len(cancellations)}")
    print(f"Duplicate state-changing calls suppressed: {len(agent.trace.duplicate_suppressions())}")
    print(f"Salvage cache stats: {agent.salvage.stats()}")

    agent.trace.save("last_run_trace.json")
    print("\nFull trace written to last_run_trace.json")


if __name__ == "__main__":
    trio.run(main)
