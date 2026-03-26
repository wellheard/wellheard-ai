"""
Test Configuration & Fixtures
Shared mocks and helpers for all test suites.
"""
import asyncio
import time
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import AsyncIterator

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.providers.base import STTProvider, LLMProvider, TTSProvider, ProviderHealth
from src.call_manager import CallGuard, CallGuardConfig, CallState


# ── Mock Providers ────────────────────────────────────────────────────────

class MockSTTProvider(STTProvider):
    """Mock STT that simulates Deepgram Nova-3 latency characteristics."""

    name = "mock_deepgram_nova3"
    cost_per_minute = 0.0077

    def __init__(self, latency_ms: float = 250, error_rate: float = 0.0):
        self.latency_ms = latency_ms
        self.error_rate = error_rate
        self._health = ProviderHealth(provider_name=self.name)
        self._connected = False
        self.connect_count = 0
        self.disconnect_count = 0

    async def connect(self):
        self._connected = True
        self.connect_count += 1

    async def transcribe_stream(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[dict]:
        import random
        chunk_count = 0
        async for chunk in audio_chunks:
            chunk_count += 1
            if random.random() < self.error_rate:
                self._health.record_error()
                continue

            await asyncio.sleep(self.latency_ms / 1000)

            # Yield partial first
            yield {
                "text": "Hello, I would like to",
                "is_final": False,
                "confidence": 0.85,
                "latency_ms": self.latency_ms * 0.6,
            }

            # Then final
            self._health.record_success(self.latency_ms)
            yield {
                "text": "Hello, I would like to schedule an appointment",
                "is_final": True,
                "confidence": 0.97,
                "latency_ms": self.latency_ms,
            }
            return

    async def disconnect(self):
        self._connected = False
        self.disconnect_count += 1

    def get_health(self) -> ProviderHealth:
        return self._health


class MockLLMProvider(LLMProvider):
    """Mock LLM that simulates Groq Llama latency characteristics."""

    name = "mock_groq_llama"
    cost_per_1k_input_tokens = 0.00011
    cost_per_1k_output_tokens = 0.00030

    def __init__(self, ttft_ms: float = 400, tokens_per_sec: float = 800, error_rate: float = 0.0):
        self.ttft_ms = ttft_ms
        self.tokens_per_sec = tokens_per_sec
        self.error_rate = error_rate
        self._health = ProviderHealth(provider_name=self.name)

    async def generate_stream(self, messages, system_prompt="", temperature=0.7, max_tokens=256, tools=None):
        import random
        if random.random() < self.error_rate:
            self._health.record_error()
            raise Exception("Mock LLM error")

        # Simulate TTFT
        await asyncio.sleep(self.ttft_ms / 1000)

        response_tokens = [
            "Sure, ", "I can ", "help ", "you ", "schedule ", "an ", "appointment. ",
            "What ", "date ", "works ", "best ", "for ", "you?"
        ]

        accumulated = ""
        for i, token in enumerate(response_tokens):
            delay = 1.0 / self.tokens_per_sec
            await asyncio.sleep(delay)
            accumulated += token
            self._health.record_success(self.ttft_ms)

            yield {
                "text": token,
                "accumulated": accumulated,
                "is_complete": i == len(response_tokens) - 1,
                "ttft_ms": self.ttft_ms,
                "tool_call": None,
            }

    def get_health(self) -> ProviderHealth:
        return self._health


class MockTTSProvider(TTSProvider):
    """Mock TTS that simulates Cartesia Sonic latency characteristics."""

    name = "mock_cartesia_sonic"
    cost_per_minute = 0.010

    def __init__(self, ttfb_ms: float = 40, error_rate: float = 0.0):
        self.ttfb_ms = ttfb_ms
        self.error_rate = error_rate
        self._health = ProviderHealth(provider_name=self.name)
        self._connected = False
        self._cancelled = False

    async def connect(self):
        self._connected = True

    async def synthesize_stream(self, text_chunks, voice_id=""):
        import random
        self._cancelled = False

        # Simulate TTFB
        await asyncio.sleep(self.ttfb_ms / 1000)

        if random.random() < self.error_rate:
            self._health.record_error()
            raise Exception("Mock TTS error")

        async for text_chunk in text_chunks:
            if self._cancelled:
                return

            # Generate fake audio (silence PCM)
            audio_data = b"\x00\x00" * 1600  # 100ms of 16kHz 16-bit audio
            self._health.record_success(self.ttfb_ms)

            yield {
                "audio": audio_data,
                "sample_rate": 16000,
                "is_complete": False,
                "ttfb_ms": self.ttfb_ms,
            }

        yield {"audio": b"", "sample_rate": 16000, "is_complete": True, "ttfb_ms": self.ttfb_ms}

    async def cancel(self):
        self._cancelled = True

    async def disconnect(self):
        self._connected = False

    def get_health(self) -> ProviderHealth:
        return self._health


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_stt():
    return MockSTTProvider(latency_ms=250)

@pytest.fixture
def mock_llm():
    return MockLLMProvider(ttft_ms=400)

@pytest.fixture
def mock_tts():
    return MockTTSProvider(ttfb_ms=40)

@pytest.fixture
def mock_stt_slow():
    return MockSTTProvider(latency_ms=800)

@pytest.fixture
def mock_llm_slow():
    return MockLLMProvider(ttft_ms=1500)

@pytest.fixture
def mock_llm_failing():
    return MockLLMProvider(error_rate=1.0)

@pytest.fixture
def mock_tts_failing():
    return MockTTSProvider(error_rate=1.0)


@pytest.fixture
def mock_call_guard():
    """Mock CallGuard with default configuration."""
    return CallGuard(config=CallGuardConfig())


@pytest.fixture
def mock_call_guard_strict():
    """CallGuard with strict limits (short timeouts for testing)."""
    config = CallGuardConfig(
        silence_prompt_timeout=1.0,
        silence_hangup_timeout=2.0,
        max_call_duration=10,
        max_cost_usd=0.10,
    )
    return CallGuard(config=config)


async def audio_chunk_generator(num_chunks: int = 5, chunk_size: int = 3200) -> AsyncIterator[bytes]:
    """Generate fake audio chunks (simulates microphone input)."""
    for _ in range(num_chunks):
        yield b"\x00\x01" * (chunk_size // 2)
        await asyncio.sleep(0.1)
