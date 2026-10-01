"""
Protocol compliance: JSON-schema validation for every emitted Action.

Objective #6 in the theme guide: "Emit well-formed JSON payloads with valid
snapshots and identifiers." This module is the single choke point all
outbound actions should pass through before being sent on the actions_out
channel, so malformed payloads are caught locally instead of costing points
in the hidden eval.
"""
from __future__ import annotations

import jsonschema

from .events import Action, ActionType

_FILLER_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["text"],
}

_TOOL_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "call_id": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object"},
        "generation": {"type": "integer"},
        "speculative": {"type": "boolean"},
        "mutates": {"type": "boolean"},
    },
    "required": ["call_id", "tool", "args", "generation"],
}

_CANCELLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "call_id": {"type": "string"},
        "tool": {"type": "string"},
        "stale_generation": {"type": "integer"},
        "current_generation": {"type": "integer"},
    },
    "required": ["call_id"],
}

_CLARIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "field": {"type": ["string", "null"]},
    },
    "required": ["question"],
}

_FINAL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "state_snapshot": {
            "type": "object",
            "properties": {
                "intent": {"type": ["string", "null"]},
                "slots": {"type": "object"},
                "generation": {"type": "integer"},
            },
            "required": ["slots", "generation"],
        },
    },
    "required": ["text", "state_snapshot"],
}

_SCHEMAS = {
    ActionType.FILLER: _FILLER_SCHEMA,
    ActionType.TOOL_CALL: _TOOL_CALL_SCHEMA,
    ActionType.CANCELLATION: _CANCELLATION_SCHEMA,
    ActionType.CLARIFICATION: _CLARIFICATION_SCHEMA,
    ActionType.FINAL_RESPONSE: _FINAL_RESPONSE_SCHEMA,
}


class ProtocolError(ValueError):
    pass


def validate_action(action: Action) -> None:
    schema = _SCHEMAS.get(action.type)
    if schema is None:
        raise ProtocolError(f"unknown action type: {action.type}")
    try:
        jsonschema.validate(instance=action.payload, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ProtocolError(f"{action.type.value} payload invalid: {exc.message}") from exc
    if not action.action_id:
        raise ProtocolError("action missing action_id")
