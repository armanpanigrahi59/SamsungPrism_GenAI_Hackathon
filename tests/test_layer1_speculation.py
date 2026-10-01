"""
Layer 1: speculative execution should fire before end-of-turn on a
confident partial fill, and never speculate on a mutating tool.
"""
import trio

from agent.events import Action, ActionType, EventType, InputEvent
from tests.conftest import manifest_event, text_event


async def test_speculation_fires_before_end_of_turn(build_agent, manifest, autojump_clock):
    agent, registry, env = build_agent()
    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)

        await events_in.send(manifest_event(manifest))
        # Enough text to identify intent + fill both required slots for
        # search_flights (origin, destination, date) but NOT end-of-turn yet.
        await events_in.send(text_event("book a flight from Delhi to Paris on the 5th"))
        await trio.sleep(0.5)

        speculative_calls = [e for e in agent.trace.entries if e["kind"] == "action" and e["data"]["type"] == "tool_call" and e["data"]["payload"].get("speculative")]
        speculative_tools = [c["data"]["payload"]["tool"] for c in speculative_calls]
        assert speculative_calls, "expected at least one speculative dispatch before end-of-turn"
        # "nlu_extract" (the language-understanding step itself, dispatched
        # through the same cancellable mechanism -- see main.py) is expected
        # to appear too; what matters is that the actual read-only tool call
        # it triggers also fires speculatively, before end-of-turn.
        assert "search_flights" in speculative_tools

        await events_in.aclose()
        await trio.sleep(0.2)

    await actions_out.aclose()


async def test_never_speculates_on_mutating_tool(build_agent, manifest, autojump_clock):
    agent, registry, env = build_agent()
    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)

        await events_in.send(manifest_event(manifest))
        await events_in.send(text_event("my Galaxy Prism screen is broken please help with my device"))
        await trio.sleep(0.5)

        spec_tool_calls = [
            e["data"]["payload"]["tool"]
            for e in agent.trace.entries
            if e["kind"] == "action" and e["data"]["type"] == "tool_call" and e["data"]["payload"].get("speculative")
        ]
        assert "create_support_ticket" not in spec_tool_calls

        await events_in.aclose()
        await trio.sleep(0.2)

    await actions_out.aclose()
