import json
import sys
from pathlib import Path

import pytest
import trio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.events import EventType, InputEvent
from agent.main import Agent
from agent.mock_env import MockConfig, MockToolEnvironment
from agent.tools import ToolRegistry

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifests" / "travel_manifest.json"

# Backend selection is read from the environment (agent/nlu.py's
# default_provider, agent/asr.py, agent/flights_provider.py). A developer
# shell that has loaded .env -- PRISM_NLU_BACKEND=groq+ollama plus a real
# GROQ_API_KEY, as the README's setup steps do -- would otherwise make the
# suite call Groq/Ollama for real: slow, non-deterministic, and racing the
# virtual clock (autojump_clock) used by the timing tests. Tests that
# exercise a specific backend set the variables they need themselves via
# monkeypatch, after this runs.
_BACKEND_ENV_VARS = (
    "PRISM_NLU_BACKEND", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "PRISM_GROQ_MODEL",
    "PRISM_NLU_MODEL", "OLLAMA_HOST", "PRISM_OLLAMA_MODEL", "PRISM_ASR_BACKEND",
    "PRISM_WHISPER_MODEL", "PRISM_WHISPER_DEVICE", "PRISM_FLIGHTS_BACKEND",
)


@pytest.fixture(autouse=True)
def _hermetic_backend_env(monkeypatch):
    for name in _BACKEND_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture
def build_agent():
    def _build(*, fault_rate: float = 0.0, seed: int = 42, latency_ms=(20.0, 60.0)):
        registry = ToolRegistry()
        env = MockToolEnvironment(MockConfig(latency_ms=latency_ms, fault_rate=fault_rate, seed=seed))
        agent = Agent(registry, env)
        return agent, registry, env
    return _build


def manifest_event(manifest: dict) -> InputEvent:
    return InputEvent(type=EventType.MANIFEST, payload=manifest)


def text_event(text: str, *, end_of_turn: bool = False) -> InputEvent:
    return InputEvent(type=EventType.TEXT_CHUNK, payload={"text": text}, end_of_turn=end_of_turn)


def interruption_event() -> InputEvent:
    return InputEvent(type=EventType.INTERRUPTION, payload={})
