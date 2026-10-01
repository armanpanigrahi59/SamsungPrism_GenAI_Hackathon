"""
Layer 2: a completed speculative call's result should be reusable from the
salvage cache when a later dispatch shares the same stable key, instead of
re-hitting the tool.
"""
import trio

from agent.salvage import SalvageCache


def test_cache_put_get_roundtrip():
    cache = SalvageCache()
    key = (("destination", "Paris"), ("origin", "Delhi"))
    assert cache.get("search_flights", key) is None
    cache.put("search_flights", key, {"offers": ["a", "b"]})
    assert cache.get("search_flights", key) == {"offers": ["a", "b"]}
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


async def test_end_to_end_speculation_populates_salvage_cache(build_agent, manifest, autojump_clock):
    agent, registry, env = build_agent(latency_ms=(20.0, 20.0))
    events_in, events_agent = trio.open_memory_channel(100)
    actions_agent, actions_out = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(agent.run, events_agent, actions_agent)
        from tests.conftest import manifest_event, text_event

        await events_in.send(manifest_event(manifest))
        await events_in.send(text_event("book a flight from Delhi to Paris on the 5th", end_of_turn=True))
        await trio.sleep(1.0)
        await events_in.aclose()
        await trio.sleep(0.3)

    await actions_out.aclose()
    assert agent.salvage.stats()["entries"] >= 1, "expected the speculative search to have populated the salvage cache"
