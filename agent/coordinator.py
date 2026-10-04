"""
Layer 0 -- Coordinator: fast path / slow path / cancellation.

Two trio nurseries:
  - fast nursery: emits fillers/acks/clarifications within a tight latency
    budget (objective #1: floor management).
  - slow nursery: runs tool calls as cancellable trio tasks, each tagged
    with the generation it was dispatched under.

`reconcile(new_generation)` is called whenever an interruption or slot
correction happens. It cancels every in-flight call whose generation is
stale, using each call's own trio.CancelScope -- this is what gives us
"cancel superseded in-flight tool calls within a few ms grace period"
without polling.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import trio

from .events import Action, ActionType, DispatchedCall, InputEvent, now_ms
from .protocol import validate_action
from .state import SlotState, idempotency_key

ToolFn = Callable[[dict], Awaitable[dict]]

FAST_PATH_BUDGET_MS = 250.0  # objective #1 / scoring: response latency


class Coordinator:
    def __init__(
        self,
        slot_state: SlotState,
        actions_out: "trio.MemorySendChannel[Action]",
        trace_logger=None,
    ) -> None:
        self.slot_state = slot_state
        self.actions_out = actions_out
        self.trace = trace_logger
        self.calls: dict[str, DispatchedCall] = {}
        self._dispatched_keys: set[str] = set()  # Safety: duplicate-call guard
        self._salvage_cache = None  # wired in by Layer 2 if present
        self._nursery: Optional[trio.Nursery] = None
        # Optional observer for finished calls (done or error, never
        # cancelled). Not an Action -- the agent's output protocol stays
        # the five action types -- just a hook a host (server/app.py) can
        # use to show tool results, e.g. flight offers, in its UI.
        self.result_listener: Optional[Callable[[DispatchedCall], Awaitable[None]]] = None

    def attach_nursery(self, nursery: trio.Nursery) -> None:
        self._nursery = nursery

    def attach_salvage_cache(self, cache) -> None:
        self._salvage_cache = cache

    # -- fast path -----------------------------------------------------

    async def emit_filler(self, text: str, *, reason: str = "ack") -> None:
        action = Action(
            type=ActionType.FILLER,
            payload={"text": text, "reason": reason},
        )
        await self._emit(action)

    async def emit_clarification(self, question: str, *, field: str | None = None) -> None:
        action = Action(
            type=ActionType.CLARIFICATION,
            payload={"question": question, "field": field},
        )
        await self._emit(action)

    async def emit_final_response(self, text: str) -> None:
        snapshot = await self.slot_state.snapshot()
        action = Action(
            type=ActionType.FINAL_RESPONSE,
            payload={
                "text": text,
                "state_snapshot": {
                    "intent": snapshot.intent,
                    "slots": snapshot.slots,
                    "generation": snapshot.generation,
                },
            },
        )
        await self._emit(action)

    # -- slow path / dispatch ------------------------------------------

    async def dispatch(
        self,
        tool_name: str,
        args: dict,
        tool_fn: ToolFn,
        *,
        mutates: bool,
        speculative: bool = False,
        stable_key: tuple | None = None,
    ) -> Optional[DispatchedCall]:
        """Dispatch a tool call as a cancellable background task tagged with
        the current generation. Returns the DispatchedCall record, or None
        if a duplicate state-changing call was suppressed."""
        generation = await self.slot_state.current_generation()
        call_id = f"call_{tool_name}_{generation}_{len(self.calls)}"

        idem_key = None
        if mutates:
            idem_key = idempotency_key(tool_name, args, generation)
            if idem_key in self._dispatched_keys:
                if self.trace:
                    self.trace.log("duplicate_call_suppressed", {
                        "tool": tool_name, "args": args, "generation": generation,
                    })
                return None
            self._dispatched_keys.add(idem_key)

        record = DispatchedCall(
            call_id=call_id,
            tool_name=tool_name,
            args=args,
            generation=generation,
            mutates=mutates,
            idempotency_key=idem_key,
            speculative=speculative,
            stable_key=stable_key,
        )
        self.calls[call_id] = record

        await self._emit(Action(
            type=ActionType.TOOL_CALL,
            payload={
                "call_id": call_id,
                "tool": tool_name,
                "args": args,
                "generation": generation,
                "speculative": speculative,
                "mutates": mutates,
            },
        ))

        # Create the cancel scope synchronously, before the task is even
        # scheduled, so a reconcile() that lands before the task gets its
        # first turn can still cancel it -- start_soon() only schedules,
        # it doesn't guarantee the task has run by the time this returns.
        record.cancel_scope = trio.CancelScope()

        assert self._nursery is not None, "attach_nursery() before dispatching"
        self._nursery.start_soon(self._run_call, record, tool_fn)
        return record

    async def _run_call(self, record: DispatchedCall, tool_fn: ToolFn) -> None:
        scope = record.cancel_scope
        with scope:
            try:
                result = await tool_fn(record.args)
                record.result = result
                record.status = "done"
                if self._salvage_cache is not None and record.stable_key is not None:
                    self._salvage_cache.put(record.tool_name, record.stable_key, result)
                if self.trace:
                    self.trace.log("call_completed", {
                        "call_id": record.call_id, "tool": record.tool_name,
                        "generation": record.generation, "speculative": record.speculative,
                    })
                await self._notify_result(record)
            except trio.Cancelled:
                record.status = "cancelled"
                if self.trace:
                    self.trace.log("call_cancelled", {
                        "call_id": record.call_id, "tool": record.tool_name,
                        "generation": record.generation,
                    })
                raise
            except Exception as exc:  # noqa: BLE001 -- surfaced via trace/status
                record.status = "error"
                record.result = {"error": str(exc)}
                if self.trace:
                    self.trace.log("call_error", {
                        "call_id": record.call_id, "tool": record.tool_name, "error": str(exc),
                    })
                await self._notify_result(record)

    async def publish_cached_result(self, tool_name: str, args: dict, result: Any) -> None:
        """Surface a salvage-cache hit to the result listener, so a host UI
        shows the reused result exactly like a fresh one."""
        generation = await self.slot_state.current_generation()
        record = DispatchedCall(
            call_id=f"cache_{tool_name}_{generation}_{len(self.calls)}", tool_name=tool_name,
            args=args, generation=generation, mutates=False, status="done", result=result,
        )
        await self._notify_result(record)

    async def _notify_result(self, record: DispatchedCall) -> None:
        if self.result_listener is None:
            return
        try:
            await self.result_listener(record)
        except trio.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- a UI hook must never break a call
            if self.trace:
                self.trace.log("result_listener_error", {"call_id": record.call_id, "error": str(exc)})

    def has_pending(self, tool_name: str, generation: int) -> bool:
        """True if a call for this tool is already pending at this exact
        generation. Used to debounce fire-and-forget background work (e.g.
        NLU extraction on rapid partial-text chunks) so we don't spam N
        concurrent calls for N chunks of the same turn."""
        return any(
            r.status == "pending" and r.tool_name == tool_name and r.generation == generation
            for r in self.calls.values()
        )

    def find_in_flight(self, tool_name: str, stable_key: tuple) -> Optional[DispatchedCall]:
        """Look for a still-pending call (typically a speculative one)
        already dispatched for this exact tool + stable key, so callers can
        avoid firing a redundant duplicate read-only call."""
        for record in self.calls.values():
            if (
                record.status == "pending"
                and record.tool_name == tool_name
                and record.stable_key == stable_key
            ):
                return record
        return None

    # -- reconciliation / cancellation -----------------------------------

    async def reconcile(self, new_generation: int) -> list[str]:
        """Cancel every in-flight call whose generation is stale relative to
        new_generation. Called right after an interruption or slot
        correction bumps the generation. Returns cancelled call_ids."""
        cancelled: list[str] = []
        for call_id, record in list(self.calls.items()):
            if record.status != "pending":
                continue
            if record.generation < new_generation and record.cancel_scope is not None:
                record.cancel_scope.cancel()
                cancelled.append(call_id)
                await self._emit(Action(
                    type=ActionType.CANCELLATION,
                    payload={
                        "call_id": call_id,
                        "tool": record.tool_name,
                        "stale_generation": record.generation,
                        "current_generation": new_generation,
                    },
                ))
        if self.trace and cancelled:
            self.trace.log("reconcile", {
                "new_generation": new_generation, "cancelled": cancelled,
            })
        return cancelled

    # -- internal ----------------------------------------------------------

    async def _emit(self, action: Action) -> None:
        validate_action(action)  # protocol compliance: fail loud, not silent
        if self.trace:
            self.trace.log("action", {"type": action.type.value, "payload": action.payload})
        await self.actions_out.send(action)
