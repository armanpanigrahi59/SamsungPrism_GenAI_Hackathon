"""
Layer 4: schema-driven argument extraction should work for a tool the
registry has never hardcoded logic for, purely from the manifest schema +
fuzzy field-name matching against current slot state.
"""
from agent.tools import ToolRegistry

UNSEEN_TOOL_MANIFEST = {
    "manifest_version": "1.0",
    "tools": [
        {
            "name": "reschedule_appointment",
            "mutates": True,
            "description": "An unseen tool not hardcoded anywhere in the agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "new_date": {"type": "string"},
                    "customer_email": {"type": "string"},
                },
                "required": ["appointment_id", "new_date"],
            },
        }
    ],
}


def test_extract_args_exact_match():
    registry = ToolRegistry()
    registry.load_manifest(UNSEEN_TOOL_MANIFEST)
    slots = {"appointment_id": "APT-1", "new_date": "2026-10-01"}
    result = registry.extract_args("reschedule_appointment", slots)
    assert result["args"]["appointment_id"] == "APT-1"
    assert result["args"]["new_date"] == "2026-10-01"
    assert result["missing_required"] == []


def test_extract_args_fuzzy_match():
    registry = ToolRegistry()
    registry.load_manifest(UNSEEN_TOOL_MANIFEST)
    # slot keys don't exactly match schema field names
    slots = {"appointment_id": "APT-2", "date": "2026-11-05"}
    result = registry.extract_args("reschedule_appointment", slots)
    assert result["args"]["appointment_id"] == "APT-2"
    # "new_date" should fuzzy-match "date"
    assert result["args"].get("new_date") == "2026-11-05"


def test_extract_args_reports_missing_required():
    registry = ToolRegistry()
    registry.load_manifest(UNSEEN_TOOL_MANIFEST)
    slots = {"appointment_id": "APT-3"}
    result = registry.extract_args("reschedule_appointment", slots)
    assert "new_date" in result["missing_required"]


def test_validate_args_against_schema():
    registry = ToolRegistry()
    registry.load_manifest(UNSEEN_TOOL_MANIFEST)
    errors = registry.validate_args("reschedule_appointment", {"appointment_id": "X", "new_date": "Y"})
    assert errors == []
    errors = registry.validate_args("reschedule_appointment", {"appointment_id": 123, "new_date": "Y"})
    assert errors  # wrong type should fail schema validation
