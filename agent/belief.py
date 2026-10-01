"""
Layer 3 -- multimodal belief fusion.

Instead of a single bolted-on vision call, text/audio/video all write into
the *same* belief-update path (SlotState.update_slot with a confidence +
source tag -- see state.py). This module adds the piece SlotState alone
doesn't provide: detecting when two modalities disagree about the same
field, and routing that disagreement to a clarification request instead of
silently picking one (objective #5: "clarify ambiguous perceptions").

Each modality produces a ModalityObservation with its own confidence.
Text is generally treated as higher-confidence for explicit slot values;
audio transcription and vision grounding are treated as corroborating or
contradicting evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .coordinator import Coordinator
from .state import SlotState

# Baseline per-modality confidence. Deliberately simple/tunable constants
# rather than a learned model -- the fusion *mechanism* (disagreement ->
# clarify) is the differentiator, not the confidence estimator.
MODALITY_BASE_CONFIDENCE = {
    "text": 0.9,
    "audio": 0.7,
    "video": 0.6,
}

DISAGREEMENT_MARGIN = 0.15  # if confidences are within this margin, treat as ambiguous


@dataclass
class ModalityObservation:
    field: str
    value: Any
    modality: str  # "text" | "audio" | "video"
    raw_confidence: Optional[float] = None  # override the modality baseline if provided

    @property
    def confidence(self) -> float:
        if self.raw_confidence is not None:
            return self.raw_confidence
        return MODALITY_BASE_CONFIDENCE.get(self.modality, 0.5)


class BeliefFusion:
    def __init__(self, slot_state: SlotState, coordinator: Coordinator) -> None:
        self.slot_state = slot_state
        self.coordinator = coordinator
        # last observation per field, to detect cross-modality disagreement
        self._last_observation: dict[str, ModalityObservation] = {}

    async def observe(self, obs: ModalityObservation) -> bool:
        """Fold a new observation into slot state. Returns True if a
        clarification was raised instead of (or in addition to) updating
        state, so callers can branch on it if needed."""
        prior = self._last_observation.get(obs.field)
        self._last_observation[obs.field] = obs

        # Only cross-modality mismatches count as "ambiguous perception" --
        # a same-modality update (e.g. the user restating a value in the
        # same text stream, or a correction after an interruption) is a
        # normal slot correction, not a disagreement to clarify. Routing
        # same-modality updates through the dispute path would make every
        # correction get flagged as ambiguous, which defeats objective #3
        # (session slot tracking + localized corrections).
        if prior is not None and prior.value != obs.value and prior.modality != obs.modality:
            close_confidence = abs(prior.confidence - obs.confidence) <= DISAGREEMENT_MARGIN
            if close_confidence:
                await self.coordinator.emit_clarification(
                    self._disagreement_question(obs.field, prior, obs),
                    field=obs.field,
                )
                # Still record the higher-confidence (or newer, if tied) value
                # provisionally, but at reduced confidence so a subsequent
                # real answer can override it cleanly.
                winner = obs if obs.confidence >= prior.confidence else prior
                await self.slot_state.update_slot(
                    obs.field, winner.value,
                    confidence=min(winner.confidence, 0.5),
                    source=f"disputed:{prior.modality}/{obs.modality}",
                )
                return True

        await self.slot_state.update_slot(
            obs.field, obs.value,
            confidence=obs.confidence,
            source=obs.modality,
        )
        return False

    @staticmethod
    def _disagreement_question(field: str, a: ModalityObservation, b: ModalityObservation) -> str:
        return (
            f"I heard/saw two different values for '{field}' "
            f"({a.modality}: {a.value!r} vs {b.modality}: {b.value!r}) -- "
            f"which one is right?"
        )
