"""
Agent.tool_result_listener: finished tool calls are reported to the host
(server/app.py streams them to the browser as "tool_result" frames), while
cancelled calls never are -- and the agent's own Action protocol is
unchanged (no new action types).
"""
import datetime as dt

import trio

from agent.events import ActionType
from agent.flights_provider import OfflineFlightProvider
from agent.main import Agent
from agent.tools import ToolRegistry
from tests.conftest import interruption_event, manifest_event, text_event


async def _run(agent, manifest, steps):
    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)
    actions = []

    async def drain():
        async for action in actions_out:
            actions.append(action)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)
        nursery.start_soon(drain)
        await events_in.send(manifest_event(manifest))
        for step in steps:
            if step == "interrupt":
                await events_in.send(interruption_event())
            elif isinstance(step, float):
                await trio.sleep(step)
            else:
                await events_in.send(step)
        await trio.sleep(2.0)
        await events_in.aclose()
    return actions


async def test_search_results_reach_the_listener(manifest, autojump_clock):
    provider = OfflineFlightProvider(latency_ms=(100.0, 100.0), today_fn=lambda: dt.date(2026, 10, 4))
    agent = Agent(ToolRegistry(), provider)
    seen = []

    async def listener(record):
        seen.append(record)

    agent.tool_result_listener = listener
    actions = await _run(agent, manifest, [
        text_event("yo i need a flight outta chicago headed to miami next friday", end_of_turn=True),
    ])
    searches = [r for r in seen if r.tool_name == "search_flights"]
    assert searches and searches[-1].status == "done"
    result = searches[-1].result
    assert result["status"] == "ok" and result["date"] == "2026-10-09"
    assert result["origin"]["city"] == "Chicago" and result["destination"]["city"] == "Miami"
    assert {a.type for a in actions} <= set(ActionType)


async def test_cancelled_calls_are_never_reported(manifest, autojump_clock):
    provider = OfflineFlightProvider(latency_ms=(300.0, 300.0), today_fn=lambda: dt.date(2026, 10, 4))
    agent = Agent(ToolRegistry(), provider)
    seen = []

    async def listener(record):
        seen.append(record)

    agent.tool_result_listener = listener
    actions = await _run(agent, manifest, [
        text_event("book a flight from Delhi to Paris next friday"),
        0.05,
        "interrupt",
        text_event("actually from Delhi to Tokyo next friday", end_of_turn=True),
    ])
    cancelled = {a.payload["call_id"] for a in actions if a.type == ActionType.CANCELLATION}
    assert cancelled, "the Paris speculative search should have been cancelled"
    assert not cancelled & {r.call_id for r in seen}
    final = [r for r in seen if r.tool_name == "search_flights"][-1]
    assert final.result["destination"]["city"] == "Tokyo"
