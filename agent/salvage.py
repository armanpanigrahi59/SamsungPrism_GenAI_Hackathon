"""
Layer 2 -- partial-result salvage cache.

When a call gets cancelled or superseded (stale generation), its result
(if it completed) or its intermediate work is not necessarily garbage: if
the *stable* part of the slot state that the call depended on hasn't
changed, a fresh dispatch can reuse it instead of re-hitting the tool.

Example: "flight to Paris on the 5th" -> speculatively search -> user
corrects "actually the 6th" -> origin/destination stable, date changed ->
a search keyed on (origin, destination) alone can still be partially
reused (e.g. carrier metadata), while a search keyed on (origin,
destination, date) correctly misses and re-dispatches.

Callers choose the stable_key granularity per tool (see coordinator.dispatch
`stable_key` argument and speculation.py for how it's derived).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SalvageCache:
    _store: dict[tuple[str, tuple], Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def put(self, tool_name: str, stable_key: tuple, result: Any) -> None:
        self._store[(tool_name, stable_key)] = result

    def get(self, tool_name: str, stable_key: tuple) -> Optional[Any]:
        key = (tool_name, stable_key)
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def has(self, tool_name: str, stable_key: tuple) -> bool:
        return (tool_name, stable_key) in self._store

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": (self.hits / total) if total else 0.0,
            "entries": len(self._store),
        }
