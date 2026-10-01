"""
Deterministic mock tool environment with injectable latency and faults.

Mirrors the theme guide's "Mock Environment: Deterministic latency and
fault injection for flight search, booking, ticket creation, and
frame-grounded manual lookups." Everything here is seeded/deterministic so
runs are reproducible for scoring/debugging.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

import trio


@dataclass
class MockConfig:
    latency_ms: tuple[float, float] = (50.0, 400.0)  # (min, max) simulated latency
    fault_rate: float = 0.0                            # probability of a raised fault
    seed: int = 42


class MockToolEnvironment:
    """Deterministic async backends for the canonical travel/support tools.

    Each backend is `async def fn(args: dict) -> dict`, suitable for passing
    straight into Coordinator.dispatch as `tool_fn`.
    """

    def __init__(self, config: MockConfig | None = None) -> None:
        self.config = config or MockConfig()
        self._rng = random.Random(self.config.seed)
        self.call_log: list[dict] = []

    async def _simulate(self, name: str, args: dict) -> None:
        lo, hi = self.config.latency_ms
        delay = self._rng.uniform(lo, hi)
        self.call_log.append({"tool": name, "args": args, "delay_ms": delay})
        await trio.sleep(delay / 1000.0)
        if self._rng.random() < self.config.fault_rate:
            raise RuntimeError(f"mock fault injected in {name}")

    async def search_flights(self, args: dict) -> dict:
        await self._simulate("search_flights", args)
        dest = args.get("destination", "UNKNOWN")
        origin = args.get("origin", "UNKNOWN")
        date = args.get("date", "UNKNOWN")
        offers = [
            {"offer_id": f"FL-{origin}-{dest}-{i}", "price": 200 + i * 47, "date": date}
            for i in range(1, 4)
        ]
        return {"origin": origin, "destination": dest, "date": date, "offers": offers}

    async def book_flight(self, args: dict) -> dict:
        await self._simulate("book_flight", args)
        return {
            "confirmation_id": f"CONF-{args.get('offer_id', 'X')}",
            "passenger_name": args.get("passenger_name"),
            "status": "booked",
        }

    async def create_support_ticket(self, args: dict) -> dict:
        await self._simulate("create_support_ticket", args)
        return {
            "ticket_id": f"TCK-{abs(hash(args.get('issue_summary', ''))) % 10000}",
            "device_model": args.get("device_model"),
            "status": "open",
        }

    async def lookup_manual(self, args: dict) -> dict:
        await self._simulate("lookup_manual", args)
        topic = args.get("topic", "general")
        device = args.get("device_model", "device")
        return {
            "device_model": device,
            "topic": topic,
            "section": f"See '{topic}' section for {device}, page {1 + len(topic) % 20}.",
        }

    def as_registry(self) -> dict[str, Callable[[dict], Any]]:
        return {
            "search_flights": self.search_flights,
            "book_flight": self.book_flight,
            "create_support_ticket": self.create_support_ticket,
            "lookup_manual": self.lookup_manual,
        }
