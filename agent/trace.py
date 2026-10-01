"""
Trace logging -- mirrors the eval kit's "complete event/action trace
logging" so scoring can be verified locally against the same shape of
trace the hidden harness would produce.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .events import now_ms


@dataclass
class TraceLogger:
    entries: list[dict] = field(default_factory=list)

    def log(self, kind: str, data: dict[str, Any]) -> None:
        self.entries.append({"ts_ms": now_ms(), "kind": kind, "data": data})

    def dump(self) -> str:
        return json.dumps(self.entries, indent=2, default=str)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            f.write(self.dump())

    # -- convenience queries used by tests / scoring sanity checks --------

    def calls(self) -> list[dict]:
        return [e for e in self.entries if e["kind"] in ("call_completed", "call_cancelled", "call_error")]

    def cancellations(self) -> list[dict]:
        return [e for e in self.entries if e["kind"] == "call_cancelled"]

    def duplicate_suppressions(self) -> list[dict]:
        return [e for e in self.entries if e["kind"] == "duplicate_call_suppressed"]

    def first_action_latency_ms(self, from_ts_ms: float) -> float | None:
        for e in self.entries:
            if e["kind"] == "action" and e["data"]["type"] in ("filler", "tool_call", "clarification"):
                return e["ts_ms"] - from_ts_ms
        return None
