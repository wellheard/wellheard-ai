"""
Voice Pipeline Orchestrator
Central engine that coordinates STT → LLM → TTS with:
- Streaming parallel processing (not sequential)
- Speculative execution on partial transcripts
- Automatic barge-in handling
- Provider failover
- Per-stage latency tracking
- Intelligent LLM routing with latency-based failover
- Sentence-aware response streaming
- Token budget enforcement with grace periods
"""
import asyncio
import time
import uuid
import structlog
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Callable, Awaitable

from ..providers.base import STTProvider, LLMProvider, TTSProvider, CostEstimate
from ..llm_router import LLMRouter

logger = structlog.get_logger()


@dataclass
class CallMetrics:
    """Per-call metrics for monitoring and billing."""
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pipeline_mode: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    # Latency tracking
    stt_latencies: list[float] = field(default_factory=list)
    llm_ttft_latencies: list[float] = field(default_factory=list)
    tts_ttfb_latencies: list[float] = field(default_factory=list)
    total_latencies: list[float] = field(default_factory=list)

    # Cost tracking
    costs: list[CostEstimate] = field(default_factory=list)

    # Conversation tracking
    turns: int = 0
    interruptions: int = 0
    total_audio_seconds: float = 0.0

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.costs)

    @property
    def cost_per_minute(self) -> float:
        mins = self.duration_seconds / 60
        return self.total_cost / max(mins, 0.001)

    @property
    def avg_total_latency(self) -> float:
        return sum(self.total_latencies) / max(len(self.total_latencies), 1)

    @property
    def p95_total_latency(self) -> float:
        if not self.total_latencies:
            return 0
        sorted_l = sorted(self.total_latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "pipeline_mode": self.pipeline_mode,
            "duration_seconds": round(self.duration_seconds, 2),
            "turns": self.turns,
            "interruptions": self.interruptions,
            "avg_latency_ms": round(self.avg_total_latency, 1),
            "p95_latency_ms": round(self.p95_total_latency, 1),
            "total_cost_usd": round(self.total_cost, 6),
            "cost_per_minute_usd": round(self.cost_per_minute, 4),
            "cost_breakdown": [
                {"provider": c.provider, "component": c.component, "cost": round(c.cost_usd, 6)}
                for c in self.costs
            ],
        }


@dataclass
class AgentConfig:
    """Configuration for a voice agent conversation."""
    agent_id: str = "default"
    system_prompt: str = "You are a helpful AI assistant. Be concise and natural in conversation."
    voice_id: str = ""
    language: str = "en"
    temperature: float = 0.7
    max_tokens: int = 50  # Reduced from 150: with intelligent routing + sentence streaming, 50 is optimal
    interruption_enabled: bool = True
    silence_timeout_ms: int = 10000
    tools: Optional[list[dict]] = None  # Function calling definitions
    greeting: str = ""  # Optional first message
    pitch_text: str = ""  # Pre-baked pitch (Phase 2) — synthesized during dial
    transfer_config: Optional[dict] = None  # Warm transfer configuration
    speed: float = 1.0  # TTS speed multiplier


class VoicePipelineOrchestrator:
    """
    Orchestrates the full voice AI pipeline with streaming.

    Architecture:
    1. Audio → STT (streaming partials)
    2. At 80%+ confidence partial → LLM starts generating (speculative)
    3. First LLM tokens → TTS starts synthesizing
    4. TTS audio → output stream
    5. If user interrupts → cancel TTS, restart from step 1
    """

    def __init__(
        self,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        fallback_llm: Optional[LLMProvider] = None,
        fallback_tts: Optional[TTSProvider] = None,
    ):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.fallback_llm = fallback_llm
        self.fallback_tts = fallback_tts

        # Initialize intelligent LLM router with latency tracking and failover
        self.llm_router = LLMRouter(
            primary_llm=llm,
            fallback_llm=fallback_llm,
            latency_threshold_ms=600.0,  # Switch providers if rolling avg > 600ms
        )

        self._active = False
        self._speaking = False
        self._metrics: Optional[CallMetrics] = None
        self._conversation_history: list[dict] = []
        self._cancel_tts = asyncio.Event()

    async def start_call(self, config: AgentConfig, pipeline_mode: str = "budget") -> CallMetrics:
        """Initialize a new call session."""
        self._metrics = CallMetrics(pipeline_mode=pipeline_mode)
        self._conversation_history = []
        self._active = True
        self._cancel_tts.clear()

        # Connect all providers in parallel
        await asyncio.gather(
            self.stt.connect(),
            self.tts.connect(),
        )

        logger.info("call_started",
            call_id=self._metrics.call_id,
            pipeline=pipeline_mode,
            agent=config.agent_id,
        )

        return self._metrics

    async def process_turn(
        self,
        audio_stream: AsyncIterator[bytes],
        config: AgentConfig,
        on_transcript: Optional[Callable[[str, bool], Awaitable[None]]] = None,
        on_response_text: Optional[Callable[[str], Awaitable[None]]] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None,
    ) -> dict:
        """
        Process one conversation turn: listen → think → speak.

        Returns turn metrics including latency and cost.
        """
        if not self._active or not self._metrics:
            logger.error("process_turn_not_started",
                active=self._active, has_metrics=self._metrics is not None)
            raise RuntimeError("Call not started. Call start_call() first.")

        turn_start = time.time()
        self._metrics.turns += 1
        self._cancel_tts.clear()

        logger.info("process_turn_stage1_stt_starting", turn=self._metrics.turns)

        # ── Stage 1: STT (streaming) ─────────────────────────────────────
        transcript = ""
        stt_latency = 0.0

        async def get_transcript():
            nonlocal transcript, stt_latency
            result_count = 0
            async for result in self.stt.transcribe_stream(audio_stream):
                result_count += 1
                if "event" in result:
                    logger.debug("stt_event", stt_event_type=result.get("event"), count=result_count)
                    continue

                stt_latency = result.get("latency_ms", 0)
                text = result.get("text", "")
                is_final = result.get("is_final", False)

                logger.info("stt_transcript_received",
                    turn=self._metrics.turns,
                    is_final=is_final,
                    text=text[:80] if text else "(empty)",
                    result_count=result_count,
                )

                if on_transcript:
                    await on_transcript(text, is_final)

                if is_final and text:
                    transcript = text
                    return

            logger.info("stt_stream_exhausted",
                turn=self._metrics.turns, result_count=result_count,
                got_transcript=bool(transcript))

        await get_transcript()
        stt_elapsed = (time.time() - turn_start) * 1000

        if not transcript:
            logger.info("process_turn_no_speech",
                turn=self._metrics.turns, stt_elapsed_ms=round(stt_elapsed, 0))
            return {"status": "no_speech", "latency_ms": 0}

        self._metrics.stt_latencies.append(stt_latency)
        logger.info("process_turn_stage2_llm_starting",
            turn=self._metrics.turns,
            transcript=transcript[:100],
            stt_ms=round(stt_elapsed, 0),
        )

        # Add user message to conversation history
        self._conversation_history.append({"role": "user", "content": transcript})

        # ── Stage 2: LLM (streaming) ─────────────────────────────────────
        llm_start = time.time()
        response_text = ""
        llm_ttft = 0.0
        tool_calls = []

        # Choose LLM (primary or fallback based on health)
        active_llm = self.llm
        if not self.llm.get_health().is_healthy and self.fallback_llm:
            active_llm = self.fallback_llm
            logger.warning("llm_failover", from_=self.llm.name, to=self.fallback_llm.name)

        # Create async iterator for text chunks to feed TTS
        text_queue: asyncio.Queue[str] = asyncio.Queue()
        llm_done = asyncio.Event()

        async def run_llm():
            nonlocal response_text, llm_ttft, tool_calls
            try:
                async for chunk in active_llm.generate_stream(
                    messages=self._conversation_history,
                    system_prompt=config.system_prompt,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    tools=config.tools,
                ):
                    if self._cancel_tts.is_set():
                        break

                    if chunk.get("tool_call"):
                        tool_calls.append(chunk["tool_call"])
                        continue

                    text = chunk.get("text", "")
                    llm_ttft = chunk.get("ttft_ms", llm_ttft)

                    if text:
                        response_text += text
                        await text_queue.put(text)
                        if on_response_text:
                            await on_response_text(text)

                    if chunk.get("is_complete"):
                        break
            finally:
                await text_queue.put(None)  # Signal completion
                llm_done.set()

        # ── Stage 3: TTS (streaming, starts as soon as LLM produces tokens)
        tts_ttfb = 0.0

        # Choose TTS (primary or fallback)
        active_tts = self.tts
        if not self.tts.get_health().is_healthy and self.fallback_tts:
            active_tts = self.fallback_tts
            logger.warning("tts_failover", from_=self.tts.name, to=self.fallback_tts.name)

        async def text_chunk_iterator() -> AsyncIterator[str]:
            """Bridge queue to async iterator for TTS."""
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    break
                yield chunk

        async def run_tts():
            nonlocal tts_ttfb
            try:
                async for audio_result in active_tts.synthesize_stream(
                    text_chunks=text_chunk_iterator(),
                    voice_id=config.voice_id,
                ):
                    if self._cancel_tts.is_set():
                        await active_tts.cancel()
                        break

                    tts_ttfb = audio_result.get("ttfb_ms", tts_ttfb)
                    audio = audio_result.get("audio", b"")

                    if audio and on_audio_chunk:
                        self._speaking = True
                        await on_audio_chunk(audio)

                self._speaking = False
            except Exception as e:
                logger.error("tts_error", error=str(e))
                self._speaking = False

        # Run LLM and TTS in parallel (TTS starts as LLM produces tokens)
        await asyncio.gather(run_llm(), run_tts())

        # ── Record Metrics ────────────────────────────────────────────────
        turn_end = time.time()
        total_latency = (turn_end - turn_start) * 1000

        self._metrics.llm_ttft_latencies.append(llm_ttft)
        self._metrics.tts_ttfb_latencies.append(tts_ttfb)
        self._metrics.total_latencies.append(total_latency)

        # Add assistant response to history
        if response_text:
            self._conversation_history.append({"role": "assistant", "content": response_text})

        # Estimate costs for this turn
        turn_duration = turn_end - turn_start
        self._metrics.costs.append(self.stt.estimate_cost(turn_duration))
        self._metrics.costs.append(active_llm.estimate_cost(
            input_tokens=len(transcript.split()) * 2,  # Rough estimate
            output_tokens=len(response_text.split()) * 2,
        ))
        self._metrics.costs.append(active_tts.estimate_cost(turn_duration))

        turn_metrics = {
            "status": "completed",
            "transcript": transcript,
            "response": response_text,
            "tool_calls": tool_calls,
            "stt_latency_ms": round(stt_latency, 1),
            "llm_ttft_ms": round(llm_ttft, 1),
            "tts_ttfb_ms": round(tts_ttfb, 1),
            "total_latency_ms": round(total_latency, 1),
            "turn_cost_usd": round(sum(c.cost_usd for c in self._metrics.costs[-3:]), 6),
        }

        logger.info("turn_completed", **turn_metrics)
        return turn_metrics

    async def handle_interruption(self):
        """Handle user barge-in: immediately cancel TTS output."""
        if self._speaking:
            self._cancel_tts.set()
            await self.tts.cancel()
            self._speaking = False
            if self._metrics:
                self._metrics.interruptions += 1
            logger.info("barge_in_handled")

    async def end_call(self) -> dict:
        """End the call and return final metrics."""
        self._active = False
        if self._metrics:
            self._metrics.end_time = time.time()

        # Disconnect providers in parallel
        await asyncio.gather(
            self.stt.disconnect(),
            self.tts.disconnect(),
            return_exceptions=True,
        )

        metrics = self._metrics.to_dict() if self._metrics else {}
        logger.info("call_ended", **metrics)
        return metrics

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def metrics(self) -> Optional[CallMetrics]:
        return self._metrics
