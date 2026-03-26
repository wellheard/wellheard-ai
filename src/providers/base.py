"""
Abstract base classes for all providers.
Every STT, LLM, and TTS provider must implement these interfaces.
This enables hot-swapping and automatic failover.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from enum import Enum
import time


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ProviderHealth:
    """Real-time health status of a provider."""
    provider_name: str
    status: ProviderStatus = ProviderStatus.HEALTHY
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    last_check: float = field(default_factory=time.time)
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0

    @property
    def is_healthy(self) -> bool:
        return self.status == ProviderStatus.HEALTHY

    def record_success(self, latency_ms: float):
        self.total_requests += 1
        self.consecutive_errors = 0
        # Exponential moving average
        alpha = 0.1
        self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms
        self.p95_latency_ms = max(self.p95_latency_ms * 0.95, latency_ms)
        self.error_rate = self.total_errors / max(self.total_requests, 1)
        self._update_status()

    def record_error(self):
        self.total_requests += 1
        self.total_errors += 1
        self.consecutive_errors += 1
        self.error_rate = self.total_errors / max(self.total_requests, 1)
        self._update_status()

    def _update_status(self):
        if self.consecutive_errors >= 5:
            self.status = ProviderStatus.UNHEALTHY
        elif self.consecutive_errors >= 2 or (self.error_rate > 0.3 and self.total_requests >= 10):
            self.status = ProviderStatus.DEGRADED
        else:
            self.status = ProviderStatus.HEALTHY
        self.last_check = time.time()


@dataclass
class LatencyTrace:
    """Tracks latency for a single operation."""
    provider: str
    operation: str
    start_time: float = field(default_factory=time.time)
    first_result_time: Optional[float] = None
    end_time: Optional[float] = None

    def mark_first_result(self):
        self.first_result_time = time.time()

    def mark_complete(self):
        self.end_time = time.time()

    @property
    def time_to_first_result_ms(self) -> float:
        if self.first_result_time:
            return (self.first_result_time - self.start_time) * 1000
        return 0.0

    @property
    def total_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000


@dataclass
class CostEstimate:
    """Per-call cost tracking."""
    provider: str
    component: str  # "stt", "llm", "tts", "telephony"
    cost_usd: float = 0.0
    units: float = 0.0  # minutes, tokens, characters
    unit_type: str = "minutes"


@dataclass
class CachedPrompt:
    """Cached system prompt with cache control metadata."""
    content: str
    cache_key: Optional[str] = None
    cache_ttl_seconds: int = 3600
    is_cacheable: bool = False

    def to_message(self) -> dict:
        """Convert to OpenAI message format."""
        return {
            "role": "system",
            "content": self.content,
        }

    def to_groq_cached_message(self) -> dict:
        """Convert to Groq cached format."""
        return {
            "role": "system",
            "content": self.content,
            "cache_control": {"type": "ephemeral"},
        }


class STTProvider(ABC):
    """Speech-to-Text provider interface."""

    name: str = "base_stt"
    cost_per_minute: float = 0.0

    @abstractmethod
    async def connect(self) -> None:
        """Establish persistent connection."""
        ...

    @abstractmethod
    async def transcribe_stream(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[dict]:
        """
        Stream audio chunks, yield partial/final transcripts.
        Yields: {"text": str, "is_final": bool, "confidence": float, "latency_ms": float}
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection."""
        ...

    @abstractmethod
    def get_health(self) -> ProviderHealth:
        ...

    def estimate_cost(self, duration_seconds: float) -> CostEstimate:
        minutes = duration_seconds / 60
        return CostEstimate(
            provider=self.name, component="stt",
            cost_usd=minutes * self.cost_per_minute,
            units=minutes, unit_type="minutes"
        )


class LLMProvider(ABC):
    """Large Language Model provider interface."""

    name: str = "base_llm"
    cost_per_1k_input_tokens: float = 0.0
    cost_per_1k_output_tokens: float = 0.0

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 256,
        tools: Optional[list[dict]] = None,
    ) -> AsyncIterator[dict]:
        """
        Stream LLM response tokens.
        Yields: {"text": str, "is_complete": bool, "ttft_ms": float, "tool_call": dict|None}
        """
        ...

    @abstractmethod
    def get_health(self) -> ProviderHealth:
        ...

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> CostEstimate:
        cost = (input_tokens / 1000 * self.cost_per_1k_input_tokens +
                output_tokens / 1000 * self.cost_per_1k_output_tokens)
        return CostEstimate(
            provider=self.name, component="llm",
            cost_usd=cost, units=input_tokens + output_tokens, unit_type="tokens"
        )


class TTSProvider(ABC):
    """Text-to-Speech provider interface."""

    name: str = "base_tts"
    cost_per_minute: float = 0.0

    @abstractmethod
    async def connect(self) -> None:
        """Establish persistent connection."""
        ...

    @abstractmethod
    async def synthesize_stream(
        self,
        text_chunks: AsyncIterator[str],
        voice_id: str = "",
    ) -> AsyncIterator[dict]:
        """
        Stream text chunks, yield audio chunks.
        Yields: {"audio": bytes, "sample_rate": int, "is_complete": bool, "ttfb_ms": float}
        """
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel current synthesis (for barge-in)."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    def get_health(self) -> ProviderHealth:
        ...

    def estimate_cost(self, duration_seconds: float) -> CostEstimate:
        minutes = duration_seconds / 60
        return CostEstimate(
            provider=self.name, component="tts",
            cost_usd=minutes * self.cost_per_minute,
            units=minutes, unit_type="minutes"
        )
