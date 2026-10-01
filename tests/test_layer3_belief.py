"""
Layer 3: cross-modality disagreement should trigger a clarification
request instead of silently overwriting the slot value.
"""
import trio

from agent.belief import BeliefFusion, ModalityObservation
from agent.coordinator import Coordinator
from agent.state import SlotState


async def test_disagreeing_modalities_trigger_clarification(autojump_clock):
    slot_state = SlotState()
    send, recv = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        coordinator = Coordinator(slot_state, send)
        coordinator.attach_nursery(nursery)
        belief = BeliefFusion(slot_state, coordinator)

        await belief.observe(ModalityObservation(field="color", value="blue", modality="audio", raw_confidence=0.6))
        disputed = await belief.observe(
            ModalityObservation(field="color", value="grey", modality="video", raw_confidence=0.6)
        )
        assert disputed is True

    await send.aclose()
    clarifications = []
    async for action in recv:
        if action.type.value == "clarification":
            clarifications.append(action)
    assert clarifications, "expected a clarification action for the disagreeing modalities"


async def test_agreeing_modalities_do_not_clarify(autojump_clock):
    slot_state = SlotState()
    send, recv = trio.open_memory_channel(100)

    async with trio.open_nursery() as nursery:
        coordinator = Coordinator(slot_state, send)
        coordinator.attach_nursery(nursery)
        belief = BeliefFusion(slot_state, coordinator)

        await belief.observe(ModalityObservation(field="device_model", value="Galaxy Prism", modality="text"))
        disputed = await belief.observe(
            ModalityObservation(field="device_model", value="Galaxy Prism", modality="video", raw_confidence=0.6)
        )
        assert disputed is False

    await send.aclose()
