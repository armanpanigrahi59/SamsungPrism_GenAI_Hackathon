<div align="center">

# 🔮 prism-agent

### **An interruptible, full-duplex real-time agent with speculative dual-process execution**
*Built for the Samsung PRISM Theme 05 Hackathon*

---

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![async trio](https://img.shields.io/badge/Async-Trio-8B5CF6?style=for-the-badge&logo=python&logoColor=white)](https://trio.readthedocs.io/)
[![tests 47 passed](https://img.shields.io/badge/Tests-47%20Passed-10B981?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![NLU Backends](https://img.shields.io/badge/NLU-Groq%20%7C%20Ollama%20%7C%20Claude%20%7C%20Regex-F59E0B?style=for-the-badge&logo=openai&logoColor=white)](#-nlu-backend-configuration)
[![Web Frontend](https://img.shields.io/badge/Frontend-Quart--Trio%20%7C%20WS-EC4899?style=for-the-badge&logo=websocket&logoColor=white)](#-running-the-full-app-frontend--backend)
[![License MIT](https://img.shields.io/badge/License-MIT-6B7280?style=for-the-badge)](LICENSE)

<br/>

[🌟 Overview](#-what-this-project-is) • [🏗️ Architecture](#%EF%B8%8F-architecture) • [🚀 Quick Start](#-setup--quick-start) • [🖥️ Web App](#-running-the-full-app-frontend--backend) • [⚡ E2E Flow](#-how-a-request-actually-flows-end-to-end) • [🤖 NLU Configuration](#-nlu-backend-configuration) • [🧪 Test Suite](#-testing)

<br/>

<img src="Claude outputs/preview_home_v2.png" alt="prism-agent Landing Page" width="92%" style="border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.25);" />

</div>

---

## 📑 Table of Contents

- [🔮 What this project is](#-what-this-project-is)
- [🏗️ Architecture](#%EF%B8%8F-architecture)
  - [Request lifecycle inside the agent](#request-lifecycle-inside-the-agent)
  - [Layer-by-layer breakdown](#layer-by-layer-breakdown)
  - [Frontend ↔ Backend component map](#frontend--backend-component-map)
- [📂 Repository layout](#-repository-layout)
- [⚙️ Setup & Quick Start](#%EF%B8%8F-setup--quick-start)
- [💻 Running the backend only (CLI)](#-running-the-backend-only-cli)
- [🌐 Running the full app: frontend + backend](#-running-the-full-app-frontend--backend)
- [🎬 The four pages, and what each proves](#-the-four-pages-and-what-each-proves)
- [🔄 How a request actually flows, end to end](#-how-a-request-actually-flows-end-to-end)
- [🤖 NLU backend configuration](#-nlu-backend-configuration)
- [🧪 Testing](#-testing)
- [🔍 Known simplifications](#-known-simplifications)
- [📄 License](#-license)

---

## 🔮 What this project is

Most conversational agents treat an interruption as **reactive cleanup**: the user speaks, the agent initiates a tool call, and if the user interrupts mid-call, special-case code attempts to cancel or patch things up.

`prism-agent` flips this premise:

> [!IMPORTANT]
> **Speculating before the user finishes talking, and cancelling the speculation if it turns out wrong, is the default behavior on almost every turn** — not an edge case.

Everything hangs off a single core data structure: **`SlotState`** — a per-session store of extracted slots paired with a monotonically increasing `generation` counter.

```
       User Speech (Streaming Chunks)
                     │
         predict intent & slots early
                     ▼
  ┌─────────────────────────────────────┐
  │  Speculative Tool Call Dispatched   │  (generation = N, tagged in Trio CancelScope)
  └──────────────────┬──────────────────┘
                     │
        User barged in / corrected?
        ┌────────────┴────────────┐
       YES                        NO
        │                          │
        ▼                          ▼
  bump_generation()        Turn completes cleanly
        │                          │
        ▼                          ▼
 CancelScope.cancel()       Result delivered instantly!
 (Zero stale leakage)       (Massive latency win)
```

1. Every tool call is tagged with the `generation` current at dispatch time.
2. When the user interrupts or corrects themselves, `bump_generation()` is invoked.
3. Every in-flight call still tagged with the stale generation is cancelled instantly through its own `trio.CancelScope`.
4. **No polling, no manual tracking, zero call-site bookkeeping.**

The repository is structured into two complementary halves:

- **`agent/`** — **The Submission Core**: cancellation spine, speculative execution, salvage cache for cancelled-but-reusable results, multimodal belief fusion, and schema-driven tool execution for never-before-seen tools. Runs completely headless via `demo.py` or the test suite.
- **`server/` + `frontend/`** — **The Interactive Web Interface**: A native Quart-Trio ASGI server and WebSocket bridge delivering a reactive, zero-build browser UI that displays live cancellation timelines, trip states, and barge-in events in real time.

---

## 🏗️ Architecture

### Request lifecycle inside the agent

```mermaid
flowchart TD
    IN["Input event: TEXT_CHUNK / AUDIO_CLIP / VIDEO_FRAME"] --> L0

    subgraph L0["Layer 0 — Cancellation spine (state.py, coordinator.py)"]
        GEN["SlotState: slots + generation counter"]
        FAST["Fast path: filler / acknowledgement"]
        SLOW["Slow path: dispatch tool call tagged with current generation"]
    end

    L0 --> FAST
    L0 --> SLOW

    SLOW --> L1["Layer 1 — Speculation (speculation.py)\nscores candidate, fires read-only calls\nbefore end-of-turn if confident enough"]
    L1 --> NLU["NLU provider (nlu.py)\nGroq -> Ollama -> Regex fallback chain"]
    NLU --> L2["Layer 2 — Salvage cache (salvage.py)\ncache hit? reuse. cache miss? call the tool."]
    L2 --> ENV["Mock tool environment (mock_env.py)"]

    INT["User interrupts or corrects a slot"] --> BUMP["bump_generation()"]
    BUMP --> CANCEL["cancel every in-flight call\nstill tagged with the old generation"]
    CANCEL -.->|stale call torn down via trio.CancelScope| L1

    ENV --> L3["Layer 3 — Belief fusion (belief.py)\ntext / audio / video observations\nof the same field reconciled"]
    L3 -->|cross-modal disagreement| CLARIFY["Clarification requested"]
    L3 -->|agreement| L4["Layer 4 — Schema-driven tools (tools.py)\nfills arguments for ANY manifest tool\nby matching slot names to its JSON schema"]
    L4 --> OUT["Action emitted: tool_call / cancellation /\nclarification / final_response"]
    CLARIFY --> OUT

    style L0 fill:#1e1e2f,stroke:#6366f1,stroke-width:2px,color:#fff
    style L1 fill:#1e1e2f,stroke:#3b82f6,stroke-width:1px,color:#fff
    style L2 fill:#1e1e2f,stroke:#10b981,stroke-width:1px,color:#fff
    style L3 fill:#1e1e2f,stroke:#f59e0b,stroke-width:1px,color:#fff
    style L4 fill:#1e1e2f,stroke:#ec4899,stroke-width:1px,color:#fff
```

### Layer-by-layer breakdown

| Layer | Component Files | Core Responsibility | Evaluation Target |
|:---:|---|---|---|
| **0** | `state.py`<br>`coordinator.py`<br>`events.py` | Generation-tagged cancellation spine; idempotency keys ensuring retried mutating calls cannot double-book or produce side effects. | **Interruption Recovery (35%)**<br>**Safety (10%)** |
| **1** | `speculation.py` | Completeness-driven candidate scoring; speculatively dispatches read-only calls before turn completion. | **Response Latency (15%)**<br>**Interruption Recovery (35%)** |
| **2** | `salvage.py` | Caches results under a stable slot-key so cancelled-yet-valid computations are immediately reused rather than re-dispatched. | **Interruption Recovery (35%)**<br>**Task Completion (40%)** |
| **3** | `belief.py` | Fuses text, audio, and video beliefs over identical slots; isolates authentic cross-modal disagreements to trigger clarifications. | **Multimodal Disagreement**<br>**Task Completion (40%)** |
| **4** | `tools.py` | Schema-driven argument extraction matching slot values to manifest JSON schemas for novel, unseen tools. | **Unseen Tool Generalization**<br>**Task Completion (40%)** |
| **—** | `nlu.py` | Multi-tier intent & slot extraction: Groq (cloud) → Ollama (local) → Anthropic → Regex deterministic fallback. | **Natural Language Grounding** |
| **—** | `protocol.py` | Strict JSON-schema validation over every outbound `Action` payload prior to emission. | **Protocol Safety (10%)** |
| **—** | `mock_env.py` | Deterministic tool backend mock with injectable latencies and fault controls. | **Reproducible Benchmarking** |
| **—** | `main.py` | `Agent` harness orchestrating channels, nurseries, and cross-layer state transitions. | **End-to-End Coordination** |

### Frontend ↔ Backend component map

```mermaid
flowchart LR
    subgraph Browser["Browser Client (HTML5 / Vanilla CSS / ES6)"]
        P1["index.html\n(Static Landing)"]
        P2["flights.html\n(Flights Assistant)"]
        P3["support.html\n(Device Support)"]
        P4["how-it-works.html\n(Interactive Explainer)"]
        JS["assistant.js\nShared WebSocket Client"]
    end

    subgraph Server["Server Runtime: server/app.py (Quart-Trio)"]
        ROUTES["Page & /assets Routes\n(HTTP Byte-Range Streaming)"]
        WS["/ws WebSocket Gateway\n(Per-Session Event Loop)"]
        FILTER["DOMAIN_TOOLS Filter\n(Scoping Manifest before Agent Init)"]
    end

    subgraph Core["Agent Engine: agent/ (Pure Trio Core)"]
        AGENT["Agent Instance"]
        REG["ToolRegistry"]
        ENV2["MockToolEnvironment"]
    end

    P2 -- "WebSocket: domain='flights'" --> WS
    P3 -- "WebSocket: domain='support'" --> WS
    JS --- P2
    JS --- P3
    ROUTES -. "serves" .-> P1
    ROUTES -. "serves" .-> P4
    WS --> FILTER --> AGENT
    AGENT --> REG
    AGENT --> ENV2

    style Browser fill:#13151f,stroke:#38bdf8,stroke-width:1px,color:#fff
    style Server fill:#18182b,stroke:#818cf8,stroke-width:1px,color:#fff
    style Core fill:#1f1635,stroke:#c084fc,stroke-width:1px,color:#fff
```

> [!NOTE]
> **Strict Sandboxing**: The `FILTER` step executes **before** the session's `Agent` is instantiated. When a user connects to the `/flights` endpoint, the agent's tool registry physically does not contain `create_support_ticket` or `lookup_manual`. Every browser tab receives an isolated `Agent` and a fresh `SlotState`.

---

## 📂 Repository layout

```
prism-agent/
├── agent/                         # Core submission: 5 layers + NLU + Protocol
│   ├── main.py                    # Agent coordinator wiring channels and nurseries
│   ├── state.py                   # SlotState + atomic generation counter   (Layer 0)
│   ├── coordinator.py             # Fast/slow path dispatch & cancellation (Layer 0)
│   ├── speculation.py             # Speculative candidate scoring & dispatch (Layer 1)
│   ├── salvage.py                 # Stable slot-key partial-result cache     (Layer 2)
│   ├── belief.py                  # Multi-source belief fusion engine        (Layer 3)
│   ├── tools.py                   # Zero-shot schema-driven arg mapper       (Layer 4)
│   ├── nlu.py                     # 3-tier fallback chain (Groq/Ollama/Regex)
│   ├── asr.py                     # Local faster-whisper ASR integration
│   ├── protocol.py                # Schema validation for outbound actions
│   ├── mock_env.py                # Deterministic tool environment with latency
│   ├── harness.py                 # Virtual-clock trace replay harness
│   └── events.py, trace.py        # Event primitives and JSON trace serialization
├── server/
│   └── app.py                     # Native Quart-Trio ASGI server & WebSocket bridge
├── frontend/
│   ├── index.html                 # Atmospheric hero landing page
│   ├── flights.html               # Live flight agent (barge-in cancellation demo)
│   ├── support.html               # Live device support agent (clarification demo)
│   ├── how-it-works.html          # Interactive architecture and layer guide
│   └── assets/
│       ├── style.css              # Custom dark-theme glassmorphism design system
│       ├── assistant.js           # Full-duplex WebSocket client & reactive timeline
│       ├── hero.mp4               # High-definition video hero loop
│       └── hero-poster.jpg        # Fast-paint video poster fallback
├── manifests/
│   └── travel_manifest.json       # Tool schemas (search_flights, book_flight, etc.)
├── tests/                         # 47 unit & integration tests across all layers
├── demo.py                        # Standalone terminal walkthrough scenario
├── .env.example                   # NLU backend template (copy to .env)
├── pyproject.toml                 # Package configuration & optional dependency sets
└── LICENSE                        # MIT License
```

---

## ⚙️ Setup & Quick Start

Requires **Python 3.10, 3.11, or 3.12**.

### 1. Installation

```bash
# Core package + test dependencies
pip install -e ".[dev]"

# (Recommended) Install with web extras for the live browser UI
pip install -e ".[dev,web]"

# All optional components (Claude, Whisper local ASR, Web UI)
pip install -e ".[dev,web,llm,local]"
```

### 2. Environment Configuration

The project ships with an `.env.example` template:

<details open>
<summary><b>PowerShell (Windows)</b></summary>

```powershell
Copy-Item .env.example .env
notepad .env   # Insert your GROQ_API_KEY (free from console.groq.com)

# Load into current PowerShell session:
Get-Content .env | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } |
  ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$($k.Trim())" $v.Trim() }
```
</details>

<details>
<summary><b>Bash / Zsh (Linux / macOS)</b></summary>

```bash
cp .env.example .env
nano .env      # Insert your GROQ_API_KEY

# Load into environment:
set -a && source .env && set +a
```
</details>

> [!TIP]
> If `.env` is omitted, `prism-agent` automatically operates in deterministic **Regex mode** — zero network requests, zero cost, completely offline.

---

## 💻 Running the backend only (CLI)

Stream an interruption and slot-correction scenario directly in the terminal without opening a browser:

```bash
python demo.py
```

### CLI Demo Execution Output

```text
=== Scenario: booking a flight, then barging in with a correction ===

> user: "book a flight from Delhi to Paris on the 5th"
  [     tool_call] {"call_id": "call_nlu_extract_0_0", "tool": "nlu_extract", "args": {...}, "generation": 0, "speculative": true}

> user interrupts: "actually..."
> user: "...from Delhi to Tokyo on the 5th"

  [  cancellation] {"call_id": "call_nlu_extract_0_0", "tool": "nlu_extract", "stale_generation": 0, "current_generation": 1}
  [        filler] {"text": "Go ahead, I'm listening.", "reason": "interruption_ack"}
  [     tool_call] {"call_id": "call_search_flights_4_1", "tool": "search_flights", "args": {"origin": "Delhi", "destination": "Tokyo", "date": "5th"}, "generation": 4, "speculative": false}
  [final_response] {"text": "Working on your search_flights request.", ...}

=== Final slot state ===
{
  "intent": "search_flights",
  "slots": { "origin": "Delhi", "destination": "Tokyo", "date": "5th" },
  "generation": 4
}

=== Score-relevant checks ===
Stale calls cancelled: 1
Duplicate state-changing calls suppressed: 0
Salvage cache stats: {'hits': 0, 'misses': 1, 'hit_rate': 0.0, 'entries': 1}
Full trace written to last_run_trace.json
```

---

## 🌐 Running the full app: frontend + backend

Launch the Quart-Trio ASGI server:

```bash
python server/app.py
```

```
prism-agent web bridge -> http://127.0.0.1:8000  (Ctrl+C to stop)
```

Point your browser to **`http://127.0.0.1:8000`**. The single process serves the HTML pages, streaming CSS/JS/video assets, and the high-throughput WebSocket bridge.

---

## 🎬 The four pages, and what each proves

| Page | URL | Visible Manifest Tools | Technical Capability Demonstrated |
|---|---|---|---|
| **Landing** | `/` | *None (Static)* | Atmospheric overview, architecture summary, and quick navigation into live test pages. |
| **Flights** | `/flights` | `search_flights`<br>`book_flight` | **Live Barge-In & Cancellation**: Type a flight query, then barge in with a different destination. Observe the stale speculative search strike through live in the timeline. |
| **Support** | `/support` | `create_support_ticket`<br>`lookup_manual` | **Grounded Clarification**: Solicits device issue descriptions. When required slots are ambiguous, the agent formulates clarifying questions rather than hallucinating. |
| **How It Works** | `/how-it-works` | *None (Static)* | Comprehensive interactive breakdown of the 5 architectural layers and their rubric alignments. |

<div align="center">
  <table width="100%">
    <tr>
      <td width="50%" align="center">
        <b>Flight Assistant Live Barge-In (<code>/flights</code>)</b><br/>
        <img src="Claude outputs/preview_flights_v2.png" width="98%" style="border-radius: 8px; margin-top: 8px;" />
      </td>
      <td width="50%" align="center">
        <b>Device Support Clarification (<code>/support</code>)</b><br/>
        <img src="Claude outputs/preview_support.png" width="98%" style="border-radius: 8px; margin-top: 8px;" />
      </td>
    </tr>
  </table>
</div>

> [!TIP]
> Both live pages include a **"▶ Run the demo"** chip that streams an automated script across the real WebSocket to observe real-time cancellation without manual typing.

---

## 🔄 How a request actually flows, end to end

The diagram below tracks the exact wire protocol frames exchanged between `assistant.js` and `server/app.py` during an interruption:

```mermaid
sequenceDiagram
    autonumber
    participant You as You (User)
    participant Browser as Browser (assistant.js)
    participant Server as server/app.py
    participant Agent as Agent (agent/main.py)
    participant NLU as NLU Provider
    participant Tool as Tool Environment

    Browser->>Server: WebSocket connect: {"type":"init","domain":"flights"}
    Note over Server: Filter manifest to domain tools before Agent construction
    Server->>Browser: {"type":"nlu_backend","chain":"Groq -> Ollama -> Regex"}
    Server->>Browser: {"type":"manifest","tools":[...]}

    You->>Browser: Types: "book a flight from Delhi to Paris on the 5th"
    Browser->>Server: {"type":"text_chunk","text":"...","end_of_turn":false}
    Server->>Agent: InputEvent(TEXT_CHUNK)
    Agent->>NLU: Extract intent & slots (speculative)
    NLU-->>Agent: intent="search_flights", slots={origin:"Delhi", destination:"Paris"}
    Agent->>Tool: Speculative search_flights() [generation=0]
    Agent-->>Server: Action(tool_call, speculative=true)
    Server-->>Browser: {"type":"action","action":"tool_call",...}

    You->>Browser: User barges in / interrupts
    Browser->>Server: {"type":"interruption"}
    Server->>Agent: InputEvent(INTERRUPTION)
    Note over Agent: bump_generation() -> 1
    Agent->>Tool: Cancel in-flight call (generation=0 torn down via CancelScope)
    Agent-->>Server: Action(cancellation, stale_generation=0)
    Server-->>Browser: {"type":"action","action":"cancellation",...}
    Note over Browser: Timeline marks call as CANCELLED (strikethrough)

    You->>Browser: "...actually from Delhi to Tokyo on the 5th" (End of Turn)
    Browser->>Server: {"type":"text_chunk","text":"...","end_of_turn":true}
    Server->>Agent: InputEvent(TEXT_CHUNK, end_of_turn=true)
    Agent->>NLU: Re-extract slots from full utterance
    Agent->>Tool: search_flights(origin="Delhi", destination="Tokyo") [generation=1]
    Tool-->>Agent: Returns flight results
    Agent-->>Server: Action(final_response)
    Server-->>Browser: {"type":"action","action":"final_response",...}
    Browser->>You: Displays final itinerary & confirmed state
```

### Critical WebSocket Bridge Details
1. **Mandatory Handshake**: The `init` frame must be the first message transmitted. It ensures the session's `Agent` is bound strictly to the selected domain.
2. **State Synchronization**: A `{"type":"state", "intent", "slots", "generation"}` packet is dispatched after every action, updating the UI's reactive trip card without client polling.
3. **HTTP Byte-Range Audio/Video**: `server/app.py` implements RFC-compliant byte-range streaming for `hero.mp4`, enabling seeking and instant video playback.

---

## 🤖 NLU backend configuration

`prism-agent` features a pluggable NLU layer where cloud and local LLMs generalize across any tool manifest, with deterministic regex fallback.

| Backend (`PRISM_NLU_BACKEND`) | Cost / Tier | Prerequisites | Capabilities & Behavior |
|:---:|:---:|---|---|
| **`regex`** | Free | None (Default) | Deterministic keyword matching. Zero external calls, zero latency. |
| **`groq`** | Free Cloud | `GROQ_API_KEY` ([console.groq.com](https://console.groq.com)) | High-speed Llama 3 via stdlib `urllib` (no additional pip packages required). |
| **`ollama`** | Free Local | [Ollama](https://ollama.com) running + `llama3.2:3b` | Completely offline zero-egress inference on localhost. |
| **`groq+ollama`** ⭐ | **Free** | Both Groq key & Ollama running | **3-Tier Robust Chain**: Groq $\to$ Ollama $\to$ Regex. Maximum speed with graceful local degradation. |
| **`ollama+groq`** | Free | Both Groq key & Ollama running | **Local-First**: Tries offline Ollama first; calls Groq if local is unavailable. |
| **`anthropic`** | Paid Cloud | `ANTHROPIC_API_KEY` | Claude 3.5 Sonnet / Haiku. Auto-selected if key is detected. |

### Environment Variables Reference

| Variable | Default Value | Description |
|---|---|---|
| `PRISM_NLU_BACKEND` | `regex` | Selects active provider: `groq+ollama`, `groq`, `ollama`, `anthropic`, or `regex`. |
| `GROQ_API_KEY` | — | API key for Groq cloud inference. |
| `PRISM_GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model selection. |
| `PRISM_OLLAMA_MODEL` | `llama3.2:3b` | Target Ollama model name. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama service endpoint. |
| `ANTHROPIC_API_KEY` | — | API key for Anthropic Claude. |
| `PRISM_NLU_MODEL` | `claude-3-5-sonnet-latest`| Claude model override. |

---

## 🧪 Testing

The repository maintains **100% passing test coverage** across all five architectural layers and backend providers:

```
tests/
├── test_layer0_cancellation.py      # Generation-tagged cancellation spine & idempotency
├── test_layer1_speculation.py       # Speculative dispatch thresholds & read-only enforcement
├── test_layer2_salvage.py           # Partial-result cache hits, misses & salvage logic
├── test_layer3_belief.py            # Multimodal cross-modality disagreement & fusion
├── test_layer4_unseen_tools.py      # Schema-driven argument extraction for novel tools
├── test_end_to_end_interruption.py  # Full agent pipeline under mid-call barge-in
├── test_nlu.py                      # Deterministic regex provider & FallbackNLUProvider
├── test_nlu_groq_plumbing.py        # Groq REST API integration (mocked HTTP)
├── test_nlu_ollama_plumbing.py      # Ollama local endpoint integration (mocked HTTP)
├── test_nlu_anthropic_plumbing.py   # Anthropic Claude worker offloading (mocked)
└── test_asr.py                      # Local Whisper ASR transcription contract
```

Execute the test suite:

```bash
python -m pytest tests/ -v
```

```text
============================= 47 passed in 0.55s ==============================
```

---

## 🔍 Known simplifications

To ensure stability and transparent evaluation, certain production elements are stubbed or simplified:

| Domain | Current Implementation | Production Evolution Path |
|---|---|---|
| **Vision Grounding** | `VIDEO_FRAME` events assume `grounded_field` and `grounded_value` are already computed. Layer 3 belief fusion is fully implemented and tested. | Integrate real-time VLM frame captioning (e.g. PaliGemma / Moondream). |
| **Real ASR Stream** | `LocalWhisperASR` is implemented and verified against the contract; integration tests use pre-transcribed payloads. | Stream chunked PCM audio via WebSocket directly into `faster-whisper`. |
| **Environment Sandbox**| Tested against `mock_env.py` providing deterministic delays and fault injection matching the PRISM eval harness. | Swap mock with production microservice REST endpoints. |
| **Confidence Heuristic**| `score_candidate` uses a deterministic slot-completeness metric. | Replace with a calibrated probabilistic intent/slot confidence model. |

---

## 📄 License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for complete terms.

<div align="center">
<sub>Built with 💜 for the Samsung PRISM GenAI Hackathon</sub>
</div>
