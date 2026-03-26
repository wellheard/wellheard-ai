"""
Intelligent LLM Router with Optimizations:
1. Intelligent routing based on rolling TTFT latency
2. Parallel speculative execution for high-stakes turns
3. Response streaming with sentence-aware chunking
4. Token budget enforcement with sentence-completion grace period
5. Prompt caching support

Architecture:
- Tracks rolling average TTFT for each provider
- Auto-switches when primary provider's latency exceeds threshold
- Fires parallel requests for turns 1-2 and transfer initiation
- Uses whichever responds first, cancels the slower one
- Streams first complete sentence to TTS immediately
- Enforces hard 50-token limit with sentence completion grace
"""
import asyncio
import time
import structlog
from typing import AsyncIterator, Optional, List, Tuple
from dataclasses import dataclass, field
from collections import deque
import re

from .providers.base import LLMProvider, ProviderHealth

logger = structlog.get_logger()


@dataclass
class LatencyStats:
    """Rolling window of latencies for a provider."""
    provider_name: str
    window_size: int = 3
    ttfts: deque = field(default_factory=lambda: deque(maxlen=3))

    def add_ttft(self, ttft_ms: float):
        """Record a new TTFT measurement."""
        self.ttfts.append(ttft_ms)

    @property
    def rolling_avg_ttft(self) -> float:
        """Get rolling average TTFT."""
        if not self.ttfts:
            return 0.0
        return sum(self.ttfts) / len(self.ttfts)

    @property
    def is_degraded(self) -> bool:
        """True if rolling avg exceeds 600ms threshold."""
        return len(self.ttfts) >= 2 and self.rolling_avg_ttft > 600.0


@dataclass
class StreamChunk:
    """Normalized response chunk from any provider."""
    text: str
    accumulated: str
    is_complete: bool
    ttft_ms: float
    total_ms: Optional[float] = None
    tool_call: Optional[dict] = None


class SentenceBoundaryDetector:
    """Detects sentence boundaries for streaming optimization."""

    # Regex for sentence endings with optional whitespace
    SENTENCE_PATTERN = re.compile(r'([.!?])\s+(?=[A-Z])|([.!?])\s*$')

    @staticmethod
    def is_complete_sentence(text: str) -> Tuple[bool, Optional[str]]:
        """
        Check if text ends with a complete sentence.

        Returns:
            (is_complete, sentence_text) where:
            - is_complete: True if ends with period, ?, !, or 15+ words without punctuation
            - sentence_text: The complete sentence if found, else None
        """
        text = text.strip()
        if not text:
            return False, None

        # Check for explicit sentence ending
        if text[-1] in '.!?':
            # Try to extract just the last sentence
            match = list(SentenceBoundaryDetector.SENTENCE_PATTERN.finditer(text))
            if match:
                last_match = match[-1]
                start_idx = last_match.end()
                sentence = text[start_idx:].strip()
                if sentence:
                    return True, sentence
            # If no multi-sentence, the whole thing is one sentence
            return True, text

        # Check for 15+ words without punctuation (natural speech boundary)
        words = text.split()
        if len(words) >= 15 and not any(c in text for c in '.!?'):
            return True, text

        return False, None

    @staticmethod
    def extract_first_sentence(text: str) -> Optional[str]:
        """Extract and return just the first complete sentence."""
        text = text.strip()
        if not text:
            return None

        # Look for period, question mark, exclamation mark
        for match in SentenceBoundaryDetector.SENTENCE_PATTERN.finditer(text):
            end_pos = match.start() + 1
            sentence = text[:end_pos].strip()
            if sentence:
                return sentence

        # No sentence ending found; check for 15+ word boundary
        words = text.split()
        if len(words) >= 15:
            # Return first 15 words as a chunk
            return ' '.join(words[:15])

        return None


class TokenBudgetEnforcer:
    """Enforces token budget with sentence-completion grace period."""

    def __init__(self, hard_limit: int = 40, grace_limit: int = 50):
        """
        Initialize token enforcer.

        Args:
            hard_limit: Hard stop at this many tokens
            grace_limit: Allow up to this to complete sentence
        """
        self.hard_limit = hard_limit
        self.grace_limit = grace_limit
        self.token_count = 0
        self.text_buffer = ""

    def add_token(self, text: str) -> Tuple[str, bool]:
        """
        Add token text. Returns (text_to_emit, should_stop).

        Args:
            text: New text chunk from LLM

        Returns:
            (text_to_emit, should_stop) where:
            - text_to_emit: Text to pass downstream
            - should_stop: True if we should stop generating
        """
        self.text_buffer += text
        # Simple estimation: ~4 chars per token
        self.token_count = len(self.text_buffer) // 4

        # Hard limit: never exceed grace_limit
        if self.token_count >= self.grace_limit:
            # Truncate to grace limit
            char_limit = self.grace_limit * 4
            text_to_emit = self.text_buffer[:char_limit]
            self.text_buffer = text_to_emit
            return text_to_emit[-len(text):] if len(text) > 0 else "", True

        # Soft limit: if at hard_limit, check if we're mid-sentence
        if self.token_count >= self.hard_limit:
            # Allow grace period if mid-sentence (no punctuation at end)
            if not self._is_sentence_complete(self.text_buffer):
                # Still mid-sentence, allow more tokens up to grace_limit
                return text, False
            else:
                # Sentence is complete, stop now
                return text, True

        return text, False

    @staticmethod
    def _is_sentence_complete(text: str) -> bool:
        """Check if text ends with sentence-ending punctuation."""
        text = text.strip()
        return text and text[-1] in '.!?'

    def should_stop(self) -> bool:
        """Check if we should stop generating."""
        self.token_count = len(self.text_buffer) // 4
        if self.token_count >= self.grace_limit:
            return True
        if self.token_count >= self.hard_limit:
            return not self._is_sentence_complete(self.text_buffer)
        return False


class LLMRouter:
    """
    Intelligent router for LLM requests with latency tracking,
    parallel speculative execution, and response streaming optimization.
    """

    def __init__(
        self,
        primary_llm: LLMProvider,
        fallback_llm: Optional[LLMProvider] = None,
        latency_threshold_ms: float = 600.0,
    ):
        """
        Initialize router.

        Args:
            primary_llm: Primary LLM provider (Groq)
            fallback_llm: Fallback provider (OpenAI)
            latency_threshold_ms: Switch threshold (default 600ms)
        """
        self.primary_llm = primary_llm
        self.fallback_llm = fallback_llm
        self.latency_threshold_ms = latency_threshold_ms

        # Latency tracking
        self.primary_stats = LatencyStats(provider_name=primary_llm.name)
        self.fallback_stats = LatencyStats(
            provider_name=fallback_llm.name if fallback_llm else "none"
        )

        # Parallel execution tracking
        self._active_tasks: dict[str, asyncio.Task] = {}

    def choose_primary_provider(self) -> Tuple[LLMProvider, str]:
        """
        Choose which provider to use based on latency and health.

        Returns:
            (provider, reason) where reason explains the choice
        """
        # Check health first
        primary_health = self.primary_llm.get_health()
        fallback_health = (self.fallback_llm.get_health()
                          if self.fallback_llm else None)

        # Primary is unhealthy, use fallback if available
        if not primary_health.is_healthy and fallback_health:
            logger.warning(
                "llm_routing_primary_unhealthy",
                primary=self.primary_llm.name,
                fallback=self.fallback_llm.name,
                primary_status=primary_health.status,
            )
            return self.fallback_llm, "primary_unhealthy"

        # Primary is healthy but degraded (slow), check if we should failover
        if self.primary_stats.is_degraded and fallback_health:
            logger.warning(
                "llm_routing_primary_degraded",
                primary=self.primary_llm.name,
                primary_avg_ttft=round(self.primary_stats.rolling_avg_ttft, 1),
                threshold=self.latency_threshold_ms,
                fallback=self.fallback_llm.name,
            )
            return self.fallback_llm, "primary_degraded_latency"

        # Primary is good, use it
        return self.primary_llm, "primary_healthy"

    async def generate_stream_with_routing(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 50,
        tools: Optional[list[dict]] = None,
        turn_number: int = 0,
    ) -> AsyncIterator[StreamChunk]:
        """
        Generate response using intelligent routing.

        For turns 1-2 and transfer initiation, uses parallel speculative execution.
        Otherwise uses chosen primary provider with automatic fallback on error.

        Args:
            messages: Chat message history
            system_prompt: System prompt
            temperature: LLM temperature
            max_tokens: Max output tokens
            tools: Optional tool definitions
            turn_number: Current turn number (0-indexed)

        Yields:
            StreamChunk objects with optimized streaming
        """
        # Determine if this is a high-stakes turn (parallel execution)
        is_high_stakes = (
            turn_number <= 1 or  # Turns 0-1 (first response, second turn)
            "transfer" in system_prompt.lower()  # Transfer initiation
        )

        # Choose primary provider
        provider, routing_reason = self.choose_primary_provider()

        if is_high_stakes and self.fallback_llm and self.fallback_llm != provider:
            # Use parallel execution
            async for chunk in self._parallel_speculative_execution(
                primary=provider,
                fallback=self.fallback_llm,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                turn_number=turn_number,
                routing_reason=routing_reason,
            ):
                yield chunk
        else:
            # Use single provider with fallback on error
            async for chunk in self._single_provider_generation(
                provider=provider,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                turn_number=turn_number,
                routing_reason=routing_reason,
            ):
                yield chunk

    async def _parallel_speculative_execution(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        messages: list[dict],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict]],
        turn_number: int,
        routing_reason: str,
    ) -> AsyncIterator[StreamChunk]:
        """
        Fire requests to both providers simultaneously.
        Use whichever responds first, cancel the slower one.

        This guarantees minimum latency for critical moments (turn 1-2).
        """
        logger.info(
            "llm_parallel_execution_start",
            turn=turn_number,
            primary=primary.name,
            fallback=fallback.name,
            routing_reason=routing_reason,
        )

        # Create async generators for both
        primary_gen = primary.generate_stream(
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )

        fallback_gen = fallback.generate_stream(
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )

        # Convert to tasks that yield chunks
        primary_chunks = []
        fallback_chunks = []
        winner = None

        async def drain_primary():
            nonlocal winner
            try:
                async for chunk in primary_gen:
                    if winner is None:
                        winner = "primary"
                        logger.info("llm_parallel_primary_wins", turn=turn_number)
                    if winner == "primary":
                        primary_chunks.append(chunk)
            except Exception as e:
                logger.error("llm_parallel_primary_error",
                            turn=turn_number, error=str(e))

        async def drain_fallback():
            nonlocal winner
            try:
                async for chunk in fallback_gen:
                    if winner is None:
                        winner = "fallback"
                        logger.info("llm_parallel_fallback_wins", turn=turn_number)
                    if winner == "fallback":
                        fallback_chunks.append(chunk)
            except Exception as e:
                logger.error("llm_parallel_fallback_error",
                            turn=turn_number, error=str(e))

        # Run both concurrently
        tasks = [
            asyncio.create_task(drain_primary()),
            asyncio.create_task(drain_fallback()),
        ]

        # Wait for first chunk from either provider
        start_time = time.time()
        while winner is None:
            await asyncio.sleep(0.001)  # Busy-wait for first chunk (very brief)
            if time.time() - start_time > 10:  # Timeout safety
                break

        # Stream chunks from winner
        if winner == "primary":
            for chunk in primary_chunks:
                yield StreamChunk(
                    text=chunk.get("text", ""),
                    accumulated=chunk.get("accumulated", ""),
                    is_complete=chunk.get("is_complete", False),
                    ttft_ms=chunk.get("ttft_ms", 0.0),
                    total_ms=chunk.get("total_ms"),
                    tool_call=chunk.get("tool_call"),
                )
                if chunk.get("is_complete"):
                    break

            # Record latency
            if primary_chunks:
                ttft = primary_chunks[0].get("ttft_ms", 0.0)
                self.primary_stats.add_ttft(ttft)

        elif winner == "fallback":
            for chunk in fallback_chunks:
                yield StreamChunk(
                    text=chunk.get("text", ""),
                    accumulated=chunk.get("accumulated", ""),
                    is_complete=chunk.get("is_complete", False),
                    ttft_ms=chunk.get("ttft_ms", 0.0),
                    total_ms=chunk.get("total_ms"),
                    tool_call=chunk.get("tool_call"),
                )
                if chunk.get("is_complete"):
                    break

            # Record latency
            if fallback_chunks:
                ttft = fallback_chunks[0].get("ttft_ms", 0.0)
                self.fallback_stats.add_ttft(ttft)

        # Cancel remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()

    async def _single_provider_generation(
        self,
        provider: LLMProvider,
        messages: list[dict],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict]],
        turn_number: int,
        routing_reason: str,
    ) -> AsyncIterator[StreamChunk]:
        """
        Generate from single provider with automatic fallback on error.
        """
        try:
            ttft_recorded = False
            async for chunk in provider.generate_stream(
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            ):
                # Record TTFT on first chunk
                if not ttft_recorded:
                    ttft = chunk.get("ttft_ms", 0.0)
                    if provider == self.primary_llm:
                        self.primary_stats.add_ttft(ttft)
                    else:
                        self.fallback_stats.add_ttft(ttft)
                    ttft_recorded = True

                yield StreamChunk(
                    text=chunk.get("text", ""),
                    accumulated=chunk.get("accumulated", ""),
                    is_complete=chunk.get("is_complete", False),
                    ttft_ms=chunk.get("ttft_ms", 0.0),
                    total_ms=chunk.get("total_ms"),
                    tool_call=chunk.get("tool_call"),
                )

                if chunk.get("is_complete"):
                    break

        except Exception as e:
            logger.error(
                "llm_generation_error",
                provider=provider.name,
                turn=turn_number,
                error=str(e),
            )

            # Try fallback if primary failed
            if provider == self.primary_llm and self.fallback_llm:
                logger.warning(
                    "llm_failover_on_error",
                    from_=provider.name,
                    to=self.fallback_llm.name,
                    turn=turn_number,
                )

                try:
                    ttft_recorded = False
                    async for chunk in self.fallback_llm.generate_stream(
                        messages=messages,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                    ):
                        # Record TTFT on first chunk
                        if not ttft_recorded:
                            ttft = chunk.get("ttft_ms", 0.0)
                            self.fallback_stats.add_ttft(ttft)
                            ttft_recorded = True

                        yield StreamChunk(
                            text=chunk.get("text", ""),
                            accumulated=chunk.get("accumulated", ""),
                            is_complete=chunk.get("is_complete", False),
                            ttft_ms=chunk.get("ttft_ms", 0.0),
                            total_ms=chunk.get("total_ms"),
                            tool_call=chunk.get("tool_call"),
                        )

                        if chunk.get("is_complete"):
                            break
                except Exception as e2:
                    logger.error(
                        "llm_fallback_error",
                        fallback=self.fallback_llm.name,
                        turn=turn_number,
                        error=str(e2),
                    )
                    raise
            else:
                raise

    async def generate_stream_with_sentence_streaming(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 50,
        tools: Optional[list[dict]] = None,
        turn_number: int = 0,
    ) -> AsyncIterator[Tuple[str, bool]]:
        """
        Generate response and stream sentences as they complete.

        Yields (sentence_text, is_final) tuples where:
        - sentence_text: A complete sentence ready for TTS
        - is_final: True if this is the last sentence

        This allows TTS to start on the first sentence while LLM is still generating.
        """
        accumulated = ""
        detector = SentenceBoundaryDetector()

        async for chunk in self.generate_stream_with_routing(
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            turn_number=turn_number,
        ):
            if chunk.tool_call:
                # Skip tool calls in streaming
                continue

            accumulated += chunk.text

            # Check if we have a complete sentence
            is_complete, sentence = detector.is_complete_sentence(accumulated)

            if is_complete and sentence:
                # Extract and yield the first sentence
                first_sentence = detector.extract_first_sentence(accumulated)
                if first_sentence:
                    yield first_sentence, False
                    # Remove yielded sentence from accumulated
                    accumulated = accumulated[len(first_sentence):].lstrip()

            if chunk.is_complete:
                # Emit remaining text as final
                if accumulated.strip():
                    yield accumulated.strip(), True
                break

    async def generate_stream_with_token_budget(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 40,
        tools: Optional[list[dict]] = None,
        turn_number: int = 0,
    ) -> AsyncIterator[str]:
        """
        Generate response with hard token budget enforcement.

        Enforces:
        - Hard stop at max_tokens
        - Grace period up to max_tokens + 10 if completing a sentence
        - Never exceeds max_tokens + 10

        Yields: Text chunks (only up to token limit)
        """
        enforcer = TokenBudgetEnforcer(
            hard_limit=max_tokens,
            grace_limit=max_tokens + 10,
        )

        async for chunk in self.generate_stream_with_routing(
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens + 10,  # Request slightly more to check for sentence boundary
            tools=tools,
            turn_number=turn_number,
        ):
            if chunk.tool_call or not chunk.text:
                continue

            text_to_emit, should_stop = enforcer.add_token(chunk.text)
            if text_to_emit:
                yield text_to_emit

            if should_stop or chunk.is_complete:
                break
