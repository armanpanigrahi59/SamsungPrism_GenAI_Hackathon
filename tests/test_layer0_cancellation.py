"""
Layer 0: verify the generation-tagged cancellation spine directly, without
going through the full Agent -- the mechanism should hold on its own.
"""
import trio

from agent.coordinator import Coordinator
from agent.state import SlotState


async def slow_tool(args: dict) -> dict:
    await trio.sleep(1.0)  # would exceed any reasonable grace period
    return {"ok": True, "args": args}


async def fast_tool(args: dict) -> dict:
    await trio.sleep(0.01)
    return {"ok": True, "args": args}


async def test_stale_call_is_cancelled_on_reconcile(autojump_clock):
    slot_state = SlotState()
    send, recv = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        coordinator = Coordinator(slot_state, send)
        coordinator.attach_nursery(nursery)

        record = await coordinator.dispatch(
            "search_flights", {"destination": "Paris"}, slow_tool, mutates=False,
        )
        assert record is not None
        assert record.status == "pending"
        await trio.sleep(0)  # let the spawned task actually start (sets cancel_scope)

        # bump generation (simulates an interruption / correction) and reconcile
        new_gen = await slot_state.bump_generation("test")
        cancelled = await coordinator.reconcile(new_gen)

        assert record.call_id in cancelled
        await trio.sleep(0)  # let cancellation propagate
        assert record.status == "cancelled"

    await send.aclose()


async def test_current_generation_call_is_not_cancelled(autojump_clock):
    slot_state = SlotState()
    send, recv = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        coordinator = Coordinator(slot_state, send)
        coordinator.attach_nursery(nursery)

        record = await coordinator.dispatch(
            "search_flights", {"destination": "Paris"}, fast_tool, mutates=False,
        )
        current_gen = await slot_state.current_generation()
        cancelled = await coordinator.reconcile(current_gen)  # no bump happened
        assert record.call_id not in cancelled

        await trio.sleep(0.05)
        assert record.status == "done"

    await send.aclose()


async def test_duplicate_state_changing_call_suppressed(autojump_clock):
    slot_state = SlotState()
    send, recv = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        coordinator = Coordinator(slot_state, send)
        coordinator.attach_nursery(nursery)

        args = {"offer_id": "FL-1", "passenger_name": "Kushagra"}
        r1 = await coordinator.dispatch("book_flight", args, fast_tool, mutates=True)
        r2 = await coordinator.dispatch("book_flight", args, fast_tool, mutates=True)

        assert r1 is not None
        assert r2 is None  # duplicate suppressed -- Safety objective

        await trio.sleep(0.05)

    await send.aclose()
