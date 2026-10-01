"""
prism-agent: an interruptible, full-duplex real-time agent.

Architecture (see README.md for the full design writeup):

  Layer 0  events.py, state.py, coordinator.py
           The cancellation spine. A monotonically increasing `generation`
           counter tags every in-flight tool call; interruptions/corrections
           bump it and cancel anything tagged with a stale generation.

  Layer 1  speculation.py
           Speculative execution: dispatch likely tool calls *before* the
           user finishes speaking, using partial slot-filling. Misses are
           cancelled via the Layer 0 mechanism -- so cancellation is the
           normal path, not a rare edge case.

  Layer 2  salvage.py
           Partial-result cache keyed on the stable subset of slot state,
           so a cancelled/superseded call's partial work isn't wasted.

  Layer 3  belief.py
           Multimodal belief fusion: text/audio/video all update one
           Belief object with per-field confidence + source; disagreement
           triggers a clarification request instead of a silent guess.

  Layer 4  tools.py (ToolRegistry.extract_args)
           Schema-driven argument extraction for tools never seen before,
           so unseen-tool scenarios don't require hardcoding.

  protocol.py   Output payload schemas + validation (fillers, calls,
                cancellations, clarifications, final responses).
  mock_env.py   Deterministic mock tool backends with injectable latency
                and faults (flight search, booking, ticket creation,
                manual lookup).
  harness.py    Virtual-clock event replay + trace logging, mirroring the
                eval kit's streaming harness.
  main.py       Wires it all together into a runnable Agent.
"""
