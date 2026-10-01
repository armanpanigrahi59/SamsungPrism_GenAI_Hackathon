"""
Layer 4 -- schema-driven tool registry + unseen-tool argument extraction.

Parses a scenario manifest (list of tool schemas, read-only vs
state-modifying) and, for tools it has no hardcoded backend for, extracts
arguments generically by matching the current slot state against the JSON
schema's declared properties -- so "unseen tools" in the public/hidden test
suite don't require hardcoding.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Callable, Optional

import jsonschema


@dataclass
class ToolSpec:
    name: str
    mutates: bool
    description: str
    parameters: dict  # JSON schema


class ToolRegistry:
    def __init__(self) -> None:
        self.specs: dict[str, ToolSpec] = {}
        self.backends: dict[str, Callable[[dict], Any]] = {}

    def load_manifest(self, manifest: dict) -> None:
        for entry in manifest.get("tools", []):
            spec = ToolSpec(
                name=entry["name"],
                mutates=bool(entry.get("mutates", False)),
                description=entry.get("description", ""),
                parameters=entry.get("parameters", {"type": "object", "properties": {}}),
            )
            self.specs[spec.name] = spec

    def register_backend(self, name: str, fn: Callable[[dict], Any]) -> None:
        self.backends[name] = fn

    def has_backend(self, name: str) -> bool:
        return name in self.backends

    def get_backend(self, name: str) -> Optional[Callable[[dict], Any]]:
        return self.backends.get(name)

    def mutates(self, name: str) -> bool:
        spec = self.specs.get(name)
        return bool(spec and spec.mutates)

    def known_tool_names(self) -> list[str]:
        return list(self.specs.keys())

    # -- Layer 4: unseen-tool argument extraction -------------------------

    def extract_args(self, tool_name: str, slots: dict[str, Any]) -> dict[str, Any]:
        """Map current slot-state fields onto a tool's declared JSON-schema
        parameters by name similarity (exact match first, then fuzzy), for
        tools whose args aren't already a 1:1 match with slot keys. This is
        what lets a never-before-seen tool from a manifest still get
        reasonable arguments instead of failing outright."""
        spec = self.specs.get(tool_name)
        if spec is None:
            return dict(slots)  # nothing known about this tool; best effort

        props: dict = spec.parameters.get("properties", {})
        required: list[str] = spec.parameters.get("required", [])
        slot_keys = list(slots.keys())
        args: dict[str, Any] = {}

        for field_name, field_schema in props.items():
            if field_name in slots:
                args[field_name] = slots[field_name]
                continue
            # fuzzy match against slot keys (e.g. "destination" vs "dest_city")
            match = difflib.get_close_matches(field_name, slot_keys, n=1, cutoff=0.55)
            if match:
                args[field_name] = slots[match[0]]

        missing = [r for r in required if r not in args]
        return {"args": args, "missing_required": missing}

    def validate_args(self, tool_name: str, args: dict[str, Any]) -> list[str]:
        """Returns a list of validation error strings (empty = valid)."""
        spec = self.specs.get(tool_name)
        if spec is None:
            return []
        try:
            jsonschema.validate(instance=args, schema=spec.parameters)
            return []
        except jsonschema.ValidationError as exc:
            return [exc.message]
