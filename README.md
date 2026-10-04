<h1 align="center">🔮 prism-agent</h1>

<p align="center">
  <strong>An interruptible, full-duplex real-time agent — Samsung PRISM Theme 05</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/async-trio-blueviolet" alt="trio">
  <img src="https://img.shields.io/badge/tests-47%20passed-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/NLU-Groq%20%7C%20Ollama%20%7C%20Claude%20%7C%20Regex-orange" alt="NLU Backends">
  <img src="https://img.shields.io/badge/frontend-Quart--Trio%20%2B%20WebSocket-informational" alt="Frontend">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
</p>

---

## ✨ What's in this repo

- **The agent engine** (`agent/`) — the actual Theme 05 submission: a trio-native, generation-tagged
  cancellation spine with speculative execution, salvage caching, multimodal belief fusion, and
  schema-driven tool use. Runnable headless via [`demo.py`](#-demo-walkthrough) or the
  [test suite](#-test-suite).
- **A live, judge-facing web frontend** (`server/` + `frontend/`) — a real 4-page site, backed by a
  real `Agent` per browser tab over a WebSocket, not a slide or a recorded GIF. See
  [🖥️ Browser Frontend](#️-browser-frontend) below.

**Jump to:** [Quick Start](#-quick-start) · [Demo Walkthrough](#-demo-walkthrough) ·
[NLU Backends](#-nlu-backends) · [Browser Frontend](#️-browser-frontend) ·
[Project Structure](#-project-structure)

---

## 🧠 The Core Idea

Most agents treat interruption handling as **reactive cleanup** — a special case bolted onto a normal
request/response loop. `prism-agent` flips this:

> **Speculate-then-cancel-if-wrong is the *default* operating mode.**

As the user is still speaking, the agent predicts likely intent + slots and **starts read-only tool
calls before end-of-turn**. When the prediction is right, the result is often already in hand — huge
latency win. When it's wrong, cancellation is not a special case; it's the same mechanism running on
almost every turn.

The entire architecture hangs off one data structure: **`SlotState`** — a session-scoped slot store
with a monotonically increasing `generation` counter. Bump the generation (on an interruption or slot
correction) and every in-flight call tagged with a stale generation gets cancelled via its own
`trio.CancelScope`. No polling. No manual bookkeeping per call site.

---

## 🏗️ Architecture

```
User speech (TEXT_CHUNK / AUDIO_CLIP / VIDEO_FRAME)
        │
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Layer 0 — Cancellation Spine                            │
 │  state.py · coordinator.py · events.py                   │
 │                                                          │
 │  SlotState { slots, generation }                         │
 │  ┌──────────────┐   bump_generation()                    │
 │  │ Fast Path    │ ──────────────────▶ reconcile()        │
 │  │ filler/ack   │                    cancel stale calls  │
 │  └──────────────┘                                        │
 │  ┌──────────────┐   dispatch(tool, args, fn, generation) │
 │  │ Slow Path    │ ──── tagged task in trio nursery ────▶ │
 │  │ tool calls   │                                        │
 │  └──────────────┘                                        │
 └──────────────────────────────────────────────────────────┘
        │
        ▼
 ┌─────────────────────┐    ┌──────────────────────────┐
 │  Layer 1            │    │  Layer 3                 │
 │  speculation.py     │    │  belief.py               │
 │                     │    │                          │
 │  Score candidates   │    │  Fuse text/audio/video   │
 │  → dispatch early   │    │  Cross-modal disagreement│
 │    read-only calls  │    │  → clarification request │
 └─────────────────────┘    └──────────────────────────┘
        │
        ▼
 ┌─────────────────────┐    ┌──────────────────────────┐
 │  Layer 2            │    │  Layer 4                 │
 │  salvage.py         │    │  tools.py                │
 │                     │    │                          │
 │  Cache results by   │    │  Schema-driven arg       │
 │  stable slot-key    │    │  extraction for ANY      │
 │  Reuse, don't retry │    │  manifest tool           │
 └─────────────────────┘    └──────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────┐
 │  NLU  (nlu.py)                                           │
 │  Groq ──▶ Ollama ──▶ Regex   (three-tier fallback chain) │
 │  Intent + slot extraction for any loaded manifest tool   │
 └──────────────────────────────────────────────────────────┘
```

### Layer Reference

| Layer | File(s) | Responsibility | Rubric Target |
|:---:|---|---|---|
| **0** | `state.py`, `coordinator.py`, `events.py` | Generation-tagged cancellation spine; idempotency keys for mutating calls | Interruption Recovery (35%), Safety (10%) |
| **1** | `speculation.py` | Speculative dispatch of read-only tool calls before end-of-turn | Response Latency (15%), Interruption Recovery (35%) |
| **2** | `salvage.py` | Cache partial/completed results by stable slot-key; reuse over re-dispatching | Interruption Recovery (35%), Task Completion (40%) |
| **3** | `belief.py` | Fuse text/audio/video beliefs; cross-modality disagreement → clarification | Task Completion (40%), multimodal multiplier |
| **4** | `tools.py` | Schema-driven arg extraction for never-before-seen manifest tools | Task Completion (40%) — unseen-tool scenarios |
| **—** | `nlu.py` | LLM-backed intent/slot extraction (Groq/Ollama/Claude) + regex fallback | Task Completion (40%) — real language understanding |
| **—** | `protocol.py` | JSON-schema validation on every emitted action | Safety & Protocol (10%) |
| **—** | `mock_env.py` | Deterministic mock backends with injectable latency/faults | Mirrors eval kit mock environment |
| **—** | `harness.py` | Virtual-clock scenario replay + trace logging | Mirrors eval kit streaming harness |
| **—** | `main.py` | Wires all layers into a runnable `Agent` | — |

> Layer 0 alone already scores on Interruption Recovery + Safety. Each subsequent layer is **purely additive**.

---

## 🚀 Quick Start

### 1. Install

```bash
pip install -e ".[dev]"            # base deps + pytest (trio, jsonschema)
pip install -e ".[dev,llm]"        # + Anthropic Claude support
pip install -e ".[dev,local]"      # + faster-whisper (local ASR)
pip install -e ".[dev,llm,local]"  # everything
```

### 2. Configure NLU Backend

`.env` itself is gitignored (it's where your real API keys go) and is **not** shipped in the repo.
Copy the template first, then fill in your keys:

```powershell
# PowerShell
Copy-Item .env.example .env
notepad .env   # set GROQ_API_KEY, confirm PRISM_NLU_BACKEND=groq+ollama
```

```bash
# bash / zsh
cp .env.example .env
```

Then load it into your shell:

```powershell
# PowerShell
Get-Content .env | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } |
  ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$($k.Trim())" $v.Trim() }
```

```bash
# bash / zsh
set -a && source .env && set +a
```

### 3. Run

```bash
python demo.py              # interruption scenario → prints trace
python -m pytest tests/ -v  # 47 tests across all layers
```

---

## 🎬 Demo Walkthrough

`demo.py` streams a realistic interruption scenario through the agent:

```
=== Scenario: booking a flight, then barging in with a correction ===

> user: "book a flight from Delhi to Paris on the 5th"
  [    tool_call] nlu_extract dispatched speculatively  (gen=0)

> user interrupts: "actually..."
> user: "...from Delhi to Tokyo on the 5th"

  [ cancellation] nlu_extract cancelled  (stale gen=0 → current gen=1)
  [      filler] "Go ahead, I'm listening."
  [clarification] "I still need: origin, destination, date."

=== Final slot state ===
{ "intent": "search_flights", "slots": {}, "generation": 1 }

=== Score-relevant checks ===
Stale calls cancelled            : 1
Duplicate state-changing calls   : 0  (idempotency guard working)
Salvage cache stats              : { hits: 0, misses: 0 }

Full trace written to last_run_trace.json
```

---

## 🤖 NLU Backends

By default the agent uses `RegexNLUProvider` — fast, free, zero dependencies, but only knows two
hardcoded intents. Upgrade via `PRISM_NLU_BACKEND`:

| Backend | Cost | Setup | Quality |
|---|:---:|---|---|
| `regex` *(default)* | Free | None | Keyword-match only, 2 hardcoded intents |
| `groq` | **Free** cloud | `GROQ_API_KEY=gsk_...` — [console.groq.com](https://console.groq.com), no card | Any manifest tool; fast Llama 3; stdlib `urllib` only |
| `ollama` | **Free**, local | Ollama installed + `ollama pull llama3.2:3b` | Any manifest tool; fully offline |
| `groq+ollama` ⭐ | **Free** | Both above | **3-tier chain:** Groq → Ollama → Regex. Always live. |
| `ollama+groq` | **Free** | Both above | Local-first: Ollama → Groq → Regex |
| `anthropic` | Paid | `ANTHROPIC_API_KEY=sk-ant-...` | Most reliable; auto-selected if key set |

All LLM backends receive tool names/descriptions/schemas from the loaded manifest — **they generalize
to any tool**, not just the two hardcoded travel/support ones. All degrade to regex automatically on
any failure via `FallbackNLUProvider`.

### Model Overrides

| Env Var | Default | Backend |
|---|---|---|
| `PRISM_NLU_BACKEND` | *(regex)* | Selects backend |
| `GROQ_API_KEY` | — | Required for Groq |
| `PRISM_GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model |
| `PRISM_OLLAMA_MODEL` | `llama3.2:3b` | Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODELS` | `~/.ollama/models` | Ollama model storage path |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic |
| `PRISM_NLU_MODEL` | `claude-3-5-sonnet-latest` | Anthropic model |

### Starting Ollama on Windows

```powershell
# Start the server with models on D: (if C: is full):
$env:OLLAMA_MODELS = "D:\ollama-models"
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList "serve"

# Pull the model (one-time):
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull llama3.2:3b
```

---

## 🏙️ Local ASR (Speech-to-Text)

`AUDIO_CLIP` events work two ways:

- **Pre-supplied `transcript`** in the payload → used directly, zero extra setup (backward compatible with all tests)
- **`audio_path`** (WAV file path) in the payload → transcribed locally via `faster-whisper`

```bash
pip install -e ".[local]"          # installs faster-whisper
export PRISM_ASR_BACKEND=whisper
# optional: export PRISM_WHISPER_MODEL=tiny   (default: "base")
```

First use downloads model weights once, then caches locally. See [`asr.py`](agent/asr.py) for the full contract.

---

## 🔬 Test Suite

```
tests/
├── test_asr.py                      # LocalWhisperASR contract (monkeypatched)
├── test_end_to_end_interruption.py  # Full agent: interrupt mid-flight call
├── test_layer0_cancellation.py      # Generation-tagged cancellation spine
├── test_layer1_speculation.py       # Speculative dispatch + threshold
├── test_layer2_salvage.py           # Partial-result cache reuse
├── test_layer3_belief.py            # Cross-modality disagreement → clarification
├── test_layer4_unseen_tools.py      # Schema-driven arg extraction, novel tools
├── test_nlu.py                      # Regex provider + FallbackNLUProvider
├── test_nlu_anthropic_plumbing.py   # Anthropic plumbing (mocked)
├── test_nlu_groq_plumbing.py        # Groq plumbing (mocked)
└── test_nlu_ollama_plumbing.py      # Ollama plumbing (mocked)
```

```
47 passed in 3.31s  ✅
```

---

## 🧩 Design Decisions

<details>
<summary><strong>Why speculate on every confident partial fill, not just explicit interruption scenarios?</strong></summary>

The hidden eval set is ~60 scenarios testing "edge cases and adversarial timing." A system that only
exercises its cancellation path on scenarios explicitly tagged "interruption" has an undertested
cancellation path. Making speculation the default means the cancellation logic gets exercised on nearly
every scenario — public or hidden.
</details>

<details>
<summary><strong>Why never speculate on mutating tools?</strong></summary>

`book_flight` / `create_support_ticket` are effectively irreversible and idempotency-keyed on
`(tool, args, generation)`. Speculating on them risks either (a) a duplicate booking if a speculative
and "real" call race, or (b) a complex cancellation-before-side-effect protocol with little added
benefit. Read-only speculation gets ~90% of the latency win for ~10% of the risk.
</details>

<details>
<summary><strong>Why is cross-modality disagreement the clarification trigger, not same-modality updates?</strong></summary>

A same-modality update (user restates a value, self-corrects in the same text stream) is a normal slot
correction — objective #3 explicitly asks these to be *applied*, not flagged. Only *disagreeing*
modalities (audio heard one thing, video grounded another) represent genuine perceptual ambiguity per
objective #5.
</details>

<details>
<summary><strong>Trio + blocking SDKs: how are they bridged?</strong></summary>

The official `anthropic` SDK's async client is built on `asyncio` — incompatible with trio's event
loop. `nlu.py` uses the *synchronous* Anthropic client (and `stdlib urllib` for Groq/Ollama) inside
`trio.to_thread.run_sync`. This keeps trio's event loop unblocked while the HTTP call runs in a worker
thread. LLM extraction on partial text is dispatched through the same generation-tagged cancellable
mechanism as tool calls — a stale LLM call for superseded text gets cancelled exactly like a stale
tool call.
</details>

---

## ⚠️ Known Simplifications

| Limitation | Status |
|---|---|
| Vision / frame grounding | `VIDEO_FRAME` assumes `grounded_field`/`grounded_value` is already extracted. `belief.py` fusion logic is real and tested — only the frame-captioning step is stubbed. |
| Real WAV ASR | `LocalWhisperASR` is wired and works, but not validated against a real audio clip. Pre-supplied `transcript` still works as before. |
| Eval kit | Built and tested against a self-authored mock environment (`mock_env.py`) with the same shape as the real kit. |
| Confidence scorer | `score_candidate` in `speculation.py` is a simple completeness/turn-progress blend, not a learned model. |

---

## 🖥️ Browser Frontend

`server/app.py` + `frontend/*.html` turn the agent into a live, judge-facing multi-page site —
not just one demo screen, but genuinely separate pages backed by genuinely separate, domain-scoped
agent sessions:

| Page | Route | Tools its Agent can see | What it demonstrates |
|---|---|---|---|
| Landing | `/` | — (static, no WebSocket) | Entry point, links into the two live pages. |
| Flights | `/flights` | `search_flights`, `book_flight` | Barge-in: a speculative search gets struck through live the instant a correction cancels it. |
| Support | `/support` | `create_support_ticket`, `lookup_manual` | Clarification: the agent asks instead of guessing when a required field (`topic`) can't be grounded. |
| How it works | `/how-it-works` | — (static, no WebSocket) | The Layer 0–4 architecture and what each timeline card actually represents. |

```bash
pip install -e ".[dev,web]"
python server/app.py
# open http://127.0.0.1:8000
```

**Why Quart-Trio and not Flask/FastAPI:** the entire agent core (`coordinator.py`, `speculation.py`,
and every NLU provider's HTTP call) runs on trio's structured concurrency — a `trio.CancelScope` per
in-flight call *is* the interruption mechanism. Quart-Trio runs the WebSocket handler on that same
trio event loop natively, so there's no asyncio/trio bridge to debug (exactly the mismatch this
README already flags for the Anthropic SDK's asyncio-based client).

**Protocol** (JSON over one `/ws` WebSocket per browser tab, full docstring in `server/app.py`):
the first frame a browser sends MUST be `{"type": "init", "domain": "flights" | "support"}` —
`server/app.py`'s `DOMAIN_TOOLS` filters the manifest *before* the Agent for that session is even
built, so a Flights tab's agent never receives `create_support_ticket`/`lookup_manual` at all, and
vice versa. This is real backend scoping, not a frontend-only filter — verified by asserting the
`manifest` frame's tool list for each domain in a live WebSocket test (not just unit-tested against
`Agent.run()` directly). After that handshake: browser sends
`{"type": "text_chunk", "text": "...", "end_of_turn": bool}` (one per partial speech hypothesis —
the delta since the last send, matching how `main.py` accumulates chunks) or
`{"type": "interruption"}`; server streams back `{"type": "action", ...}` for every `Action` the
agent emits, `{"type": "state", "intent", "slots", "generation"}` after each one, and
`{"type": "nlu_backend", "chain": "..."}` once on connect so the UI shows which backend is actually live.

Each WebSocket connection gets its own fresh `Agent` — two browser tabs never share slot state —
verified by replaying two concurrent sessions through the live server and confirming neither sees the
other's slots. `frontend/assets/assistant.js` is one shared client module (WS handling, timeline
rendering, the live field cards) that every page's inline `<script>` configures with its own
`domain` + field labels, instead of duplicating ~250 lines of JS per page. Each live page's
"▶ Run the demo" chip replays a scripted scenario through the real WebSocket path, so there's always
a one-click, no-typing proof it works end-to-end.

**Serving binary assets correctly.** `/assets/<path:filename>` serves `style.css`/`assistant.js` as
well as `hero.mp4`/`hero-poster.jpg` from the same route. An early version read every asset with
`.read_text(encoding="utf-8")`, which is fine for CSS/JS but corrupts (or raises
`UnicodeDecodeError` on) a binary file -- caught before it shipped by actually requesting `hero.mp4`
through the route and diffing it byte-for-byte against the source file, not by assuming text-mode
read was safe for everything. The route now reads every file as bytes and additionally honors HTTP
`Range` requests (`Accept-Ranges: bytes`, `206 Partial Content`), since Chrome/Safari issue a Range
request for `<video>` elements and some browsers won't start playback without a 206 response to it --
verified with a direct `curl -H "Range: bytes=0-999"` against the running server, not assumed.

---

## 🔌 Extending for the Real Eval Kit

1. Swap [`mock_env.py`](agent/mock_env.py) for the real mock environment once released.
2. Point `tools.py → load_manifest()` at the real scenario manifests.
3. Wire a real vision model (Gemini multimodal, or a local captioning model) to populate `VIDEO_FRAME`'s
   `grounded_field`/`grounded_value` — [`events.py`](agent/events.py) already matches the theme
   guide's §3.1 interface contract.
4. [`harness.py`](agent/harness.py) is ready to replay the public test suite's canonical scenarios once
   they're in `InputEvent` shape.
5. Verify each backend end-to-end:
   ```bash
   PRISM_NLU_BACKEND=groq+ollama python demo.py
   # check last_run_trace.json for "source": "groq" or "source": "ollama"

   PRISM_NLU_BACKEND=ollama python demo.py
   # confirm ollama serve is running with OLLAMA_MODELS set

   PRISM_ASR_BACKEND=whisper python demo.py
   # pass an AUDIO_CLIP event with a real audio_path
   ```

---

## 📂 Project Structure

```
prism-agent/
├── agent/
│   ├── __init__.py          # Package entry + architecture overview
│   ├── main.py              # Agent class — wires all layers together
│   ├── events.py            # InputEvent / Action types (Layer 0)
│   ├── state.py             # SlotState + generation counter (Layer 0)
│   ├── coordinator.py       # Fast/slow path + cancellation (Layer 0)
│   ├── speculation.py       # Speculative dispatch engine (Layer 1)
│   ├── salvage.py           # Partial-result cache (Layer 2)
│   ├── belief.py            # Multimodal belief fusion (Layer 3)
│   ├── tools.py             # Schema-driven tool registry (Layer 4)
│   ├── nlu.py               # NLU providers: Groq, Ollama, Anthropic, Regex
│   ├── asr.py               # Local Whisper ASR (optional)
│   ├── protocol.py          # Action payload validation
│   ├── mock_env.py          # Mock tool backends (flight, booking, support)
│   ├── harness.py           # Virtual-clock scenario replay
│   └── trace.py             # Trace logger
├── tests/                   # 47 unit + integration tests
├── manifests/
│   └── travel_manifest.json # Tool schemas: search_flights, book_flight, ...
├── demo.py                  # Runnable interruption scenario demo
├── server/
│   └── app.py               # Quart-Trio WebSocket bridge + page/asset routes, domain-scoped manifest
├── frontend/
│   ├── index.html           # Static landing page (no WebSocket)
│   ├── flights.html         # Live assistant -- search_flights, book_flight only
│   ├── support.html         # Live assistant -- create_support_ticket, lookup_manual only
│   ├── how-it-works.html    # Static architecture explainer (no WebSocket)
│   └── assets/
│       ├── style.css        # Shared design system for every page
│       ├── assistant.js     # Shared WS client + timeline/field-card rendering, config per page
│       ├── hero.mp4         # Looping background video (muted, web-optimized, ~1.6MB)
│       └── hero-poster.jpg  # First-frame fallback shown before the video decodes
├── .env.example             # NLU backend config template (copy to .env, gitignored)
├── pyproject.toml           # Project metadata + optional deps
└── README.md
```

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

