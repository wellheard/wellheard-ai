"""
Deepgram Aura-2 TTS Provider (Budget Mode)
- 90ms time-to-first-byte
- 57.78% rated high naturalness
- ~$0.010/min (estimated from character pricing)
- Best value TTS in market
"""
import asyncio
import time
import structlog
from typing import AsyncIterator, Optional
import httpx

from .base import TTSProvider, ProviderHealth, LatencyTrace

logger = structlog.get_logger()


class DeepgramTTSProvider(TTSProvider):
    """Deepgram Aura-2 text-to-speech for budget mode."""

    name = "deepgram_aura2"
    cost_per_minute = 0.010  # Estimated

    def __init__(self, api_key: str, model: str = "aura-2-en"):
        self.api_key = api_key
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None
        self._health = ProviderHealth(provider_name=self.name)
        self._cancel_event = asyncio.Event()

    async def connect(self) -> None:
        """Create persistent HTTP client with connection pooling."""
        self._client = httpx.AsyncClient(
            base_url="https://api.deepgram.com/v1",
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._cancel_event.clear()
        logger.info("deepgram_tts_connected", model=self.model)

    async def synthesize_stream(
        self,
        text_chunks: AsyncIterator[str],
        voice_id: str = "",
    ) -> AsyncIterator[dict]:
        """
        Stream text to Deepgram Aura, yield audio chunks.
        Accumulates text chunks and sends in batches for efficiency.
        """
        if not self._client:
            await self.connect()

        self._cancel_event.clear()
        trace = LatencyTrace(provider=self.name, operation="synthesize")

        # Accumulate text from LLM token stream into sentence-sized chunks
        text_buffer = ""
        sentence_delimiters = {'.', '!', '?', ';', ':'}

        async for text_chunk in text_chunks:
            if self._cancel_event.is_set():
                return

            text_buffer += text_chunk

            # Check if we have a complete sentence or enough text
            should_flush = (
                any(text_buffer.rstrip().endswith(d) for d in sentence_delimiters)
                or len(text_buffer) > 200
            )

            if should_flush and text_buffer.strip():
                async for audio_result in self._synthesize_chunk(text_buffer.strip(), voice_id, trace):
                    if self._cancel_event.is_set():
                        return
                    yield audio_result
                text_buffer = ""

        # Flush remaining text
        if text_buffer.strip() and not self._cancel_event.is_set():
            async for audio_result in self._synthesize_chunk(text_buffer.strip(), voice_id, trace):
                yield audio_result

        yield {"audio": b"", "sample_rate": 16000, "is_complete": True, "ttfb_ms": trace.time_to_first_result_ms}

    async def _synthesize_chunk(
        self, text: str, voice_id: str, trace: LatencyTrace
    ) -> AsyncIterator[dict]:
        """Synthesize a single text chunk via Deepgram Aura streaming."""
        model = voice_id or self.model

        try:
            async with self._client.stream(
                "POST",
                f"/speak?model={model}&encoding=linear16&sample_rate=16000",
                json={"text": text},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if self._cancel_event.is_set():
                        return

                    if not trace.first_result_time:
                        trace.mark_first_result()

                    self._health.record_success(trace.time_to_first_result_ms)

                    yield {
                        "audio": chunk,
                        "sample_rate": 16000,
                        "is_complete": False,
                        "ttfb_ms": trace.time_to_first_result_ms,
                    }

        except Exception as e:
            self._health.record_error()
            logger.error("deepgram_tts_error", error=str(e))
            raise

    async def synthesize_single(self, text: str, voice_id: str = "") -> Optional[bytes]:
        """Synthesize a complete text string into a single PCM16 audio blob."""
        if not self._client:
            await self.connect()

        model = voice_id or self.model
        audio_chunks = []

        try:
            async with self._client.stream(
                "POST",
                f"/speak?model={model}&encoding=linear16&sample_rate=16000",
                json={"text": text},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    audio_chunks.append(chunk)

            if audio_chunks:
                return b"".join(audio_chunks)
            return None

        except Exception as e:
            logger.error("deepgram_tts_synthesize_single_error", error=str(e), text=text[:50])
            return None

    async def synthesize_single_streamed(
        self, text: str, voice_id: str = ""
    ) -> AsyncIterator[dict]:
        """Stream synthesis of a single text string, yielding audio chunks."""
        if not self._client:
            await self.connect()

        model = voice_id or self.model
        trace = LatencyTrace(provider=self.name, operation="synthesize_single_streamed")

        try:
            async with self._client.stream(
                "POST",
                f"/speak?model={model}&encoding=linear16&sample_rate=16000",
                json={"text": text},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if self._cancel_event.is_set():
                        return

                    if not trace.first_result_time:
                        trace.mark_first_result()

                    self._health.record_success(trace.time_to_first_result_ms)

                    yield {
                        "audio": chunk,
                        "sample_rate": 16000,
                        "is_complete": False,
                        "ttfb_ms": trace.time_to_first_result_ms,
                    }

            yield {"audio": b"", "sample_rate": 16000, "is_complete": True, "ttfb_ms": trace.time_to_first_result_ms}

        except Exception as e:
            self._health.record_error()
            logger.error("deepgram_tts_synthesize_single_streamed_error", error=str(e), text=text[:50])
            raise

    def update_voice_params(self, speed: float = None, emotion: str = None) -> None:
        """Update voice parameters (no-op for Deepgram Aura — speed/emotion not supported)."""
        pass

    async def cancel(self) -> None:
        """Cancel current synthesis for barge-in."""
        self._cancel_event.set()
        logger.debug("deepgram_tts_cancelled")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("deepgram_tts_disconnected")

    def get_health(self) -> ProviderHealth:
        return self._health
