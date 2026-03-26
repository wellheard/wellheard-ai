"""
LLM Router Integration Guide - How to Use in call_bridge.py

This module shows EXACTLY how to integrate the new optimizations into the existing call_bridge.py.

INTEGRATION POINTS:
===================

1. In VoicePipelineOrchestrator.__init__ (pipelines/orchestrator.py):
   - Initialize LLMRouter instead of using raw llm provider
   - Pass both primary and fallback LLMs

2. In CallBridge.text_turn() method (call_bridge.py around line 2600):
   - Replace current LLM calls with router.generate_stream_with_routing()
   - For sentence-streaming: use generate_stream_with_sentence_streaming()
   - For token budgeting: use generate_stream_with_token_budget()

3. Config updates (config/settings.py):
   - Keep max_tokens at 40-50 (was 150, can now be lower due to optimizations)
   - Add: llm_router_enabled = True
   - Add: llm_parallel_execution_on_high_stakes = True

IMPLEMENTATION CHECKLIST:
========================

# Step 1: Update pipelines/orchestrator.py
OLD:
    def __init__(self, stt, llm, tts, fallback_llm=None, fallback_tts=None):
        self.llm = llm
        self.fallback_llm = fallback_llm

NEW:
    def __init__(self, stt, llm, tts, fallback_llm=None, fallback_tts=None):
        from ..llm_router import LLMRouter
        self.llm = llm
        self.fallback_llm = fallback_llm
        self.llm_router = LLMRouter(
            primary_llm=llm,
            fallback_llm=fallback_llm,
            latency_threshold_ms=600.0,
        )

# Step 2: Update call_bridge.py text_turn() method
Replace line 2575 (async for chunk in active_llm.generate_stream...):

OLD:
    response_text = ""
    try:
        async for chunk in active_llm.generate_stream(
            messages=compressed_messages,
            system_prompt=effective_system_prompt,
            temperature=self.agent_config.temperature,
            max_tokens=self.agent_config.max_tokens,
        ):
            text = chunk.get("text", "")
            llm_ttft = chunk.get("ttft_ms", llm_ttft)
            if text:
                response_text += text
            if chunk.get("is_complete"):
                break

NEW - Using intelligent routing with sentence streaming:
    response_text = ""
    first_sentence_queued = False
    try:
        async for sentence_text, is_final in self.orchestrator.llm_router.generate_stream_with_sentence_streaming(
            messages=compressed_messages,
            system_prompt=effective_system_prompt,
            temperature=self.agent_config.temperature,
            max_tokens=50,  # Reduced from 150
            turn_number=turn_number,
        ):
            response_text += sentence_text

            # Queue first sentence to TTS IMMEDIATELY (don't wait for full response)
            if not first_sentence_queued and sentence_text.strip():
                logger.info("first_sentence_ready", call_id=self.call_id,
                    turn=turn_number, sentence=sentence_text[:100])
                await self._synthesize_and_queue(sentence_text)
                first_sentence_queued = True

            if is_final:
                # Remaining sentences (if any) synthesized after LLM finishes
                break

ALTERNATIVELY - Using token-budgeted streaming:
    response_text = ""
    try:
        async for text_chunk in self.orchestrator.llm_router.generate_stream_with_token_budget(
            messages=compressed_messages,
            system_prompt=effective_system_prompt,
            temperature=self.agent_config.temperature,
            max_tokens=40,
            turn_number=turn_number,
        ):
            response_text += text_chunk

# Step 3: Update config/settings.py

Add to Settings class:
    # ── LLM Router Settings ─────────────────────────────────────────────
    llm_router_enabled: bool = True
    llm_parallel_execution_enabled: bool = True
    llm_routing_latency_threshold_ms: int = 600
    llm_enable_prompt_caching: bool = True
    llm_sentence_streaming_enabled: bool = True
    llm_token_budget_hard_limit: int = 40
    llm_token_budget_grace_limit: int = 50


PERFORMANCE EXPECTATIONS:
========================

Current (no optimizations):
- Turn 1: 2000-3000ms (LLM 490ms + TTS 800ms + overhead)
- Turn 2+: 1500-2500ms
- TTFT: 424ms (Groq)
- Perception: Sluggish, noticeable lag

Optimized (with all features):
- Turn 1-2: 800-1200ms (parallel execution + sentence streaming)
  * First sentence to TTS in ~300ms
  * User hears response start while LLM finishing
  * Second sentence cached/synthesized in parallel
- Turn 3+: 600-1000ms (routing + semantic cache)
- TTFT: <300ms rolling average (with auto-failover)
- Provider failover: <100ms (parallel execution ensures fastest response)
- Cost: -15% (fewer tokens, cached prompts)


METRICS TO TRACK:
================

After deploying optimizations, monitor:

1. llm_router.primary_stats.rolling_avg_ttft
   - Should drop from 424ms to <300ms within first 10 calls
   - Watch for degradation (> 600ms threshold triggers failover)

2. llm_parallel_execution metrics
   - llm_parallel_primary_wins: count of times Groq responds first
   - llm_parallel_fallback_wins: count of times OpenAI responds first
   - Goal: ~80% primary wins for Groq (faster)

3. Sentence streaming latency
   - Time from text_turn_start to first_sentence_queued
   - Should be 150-250ms (LLM TTFT + detection)
   - Compare with previous full-response latency

4. Token budget enforcement
   - Percent of responses hitting hard limit (40 tokens)
   - Percent using grace period (40-50 tokens)
   - Should be well under 100 tokens total

5. End-to-end perceived latency (call_id="X")
   - text_turn_complete["total_ms"]
   - Should decrease 30-40% with all optimizations enabled

Example monitoring query:
    SELECT
        turn,
        AVG(llm_ttft_ms) as avg_ttft,
        COUNT(*) as call_count,
        SUM(CASE WHEN llm_ttft_ms > 600 THEN 1 ELSE 0 END) as degraded_count
    FROM call_metrics
    WHERE timestamp > NOW() - INTERVAL '24 hours'
    GROUP BY turn
    ORDER BY turn;


ROLLBACK / FEATURE FLAGS:
=========================

If needed, disable individual features:

1. Disable routing (use primary only):
   router.choose_primary_provider = lambda: (primary_llm, "disabled")

2. Disable parallel execution (use single provider):
   Set is_high_stakes = False in generate_stream_with_routing

3. Disable sentence streaming (wait for full response):
   Use generate_stream_with_routing() instead of _with_sentence_streaming

4. Disable token budgeting:
   Use generate_stream_with_routing() instead of _with_token_budget


TESTING:
========

Unit tests to add (src/tests/test_llm_router.py):

    async def test_sentence_boundary_detection():
        detector = SentenceBoundaryDetector()
        assert detector.is_complete_sentence("Hello. World")
        assert detector.extract_first_sentence("Hello. World") == "Hello."

    async def test_token_budget_enforcement():
        enforcer = TokenBudgetEnforcer(hard_limit=40, grace_limit=50)
        text = "a" * 200  # 50 tokens
        text_out, should_stop = enforcer.add_token(text)
        assert should_stop

    async def test_parallel_execution():
        primary = MockLLMProvider(delay_ms=100)
        fallback = MockLLMProvider(delay_ms=500)
        router = LLMRouter(primary, fallback)
        # Verify primary responds first

    async def test_latency_degradation_failover():
        router = LLMRouter(primary, fallback)
        # Simulate 3 slow primary calls
        for _ in range(3):
            router.primary_stats.add_ttft(700)
        # Next call should use fallback
        provider, reason = router.choose_primary_provider()
        assert provider == fallback
        assert reason == "primary_degraded_latency"
"""

# ============================================================================
# EXAMPLE: How to modify CallBridge.text_turn() method
# ============================================================================

# BEFORE (current implementation):
BEFORE_EXAMPLE = """
        try:
            async for chunk in active_llm.generate_stream(
                messages=compressed_messages,
                system_prompt=effective_system_prompt,
                temperature=self.agent_config.temperature,
                max_tokens=self.agent_config.max_tokens,
            ):
                text = chunk.get("text", "")
                llm_ttft = chunk.get("ttft_ms", llm_ttft)
                if text:
                    response_text += text
                if chunk.get("is_complete"):
                    break
"""

# AFTER (optimized implementation):
AFTER_EXAMPLE = """
        try:
            # Stream sentences as they complete, start TTS on first sentence
            async for sentence_text, is_final in self.orchestrator.llm_router.generate_stream_with_sentence_streaming(
                messages=compressed_messages,
                system_prompt=effective_system_prompt,
                temperature=self.agent_config.temperature,
                max_tokens=50,  # Reduced from 150
                turn_number=turn_number,
            ):
                response_text += sentence_text

                # Queue first sentence immediately (don't wait for full LLM response)
                if not first_sentence_queued and sentence_text.strip():
                    logger.info("first_sentence_ready_for_tts",
                        call_id=self.call_id,
                        turn=turn_number,
                        sentence=sentence_text[:80],
                        elapsed_ms=round((time.time() - t0) * 1000, 1))
                    # Async start TTS on background task so LLM can keep generating
                    tts_task = asyncio.create_task(
                        self._synthesize_and_queue(sentence_text)
                    )
                    first_sentence_queued = True

                if is_final:
                    break
"""

# ============================================================================
# ALTERNATIVE: Use token budgeting if you prefer explicit token limits
# ============================================================================

ALTERNATIVE_EXAMPLE = """
        response_text = ""
        token_count = 0
        try:
            async for text_chunk in self.orchestrator.llm_router.generate_stream_with_token_budget(
                messages=compressed_messages,
                system_prompt=effective_system_prompt,
                temperature=self.agent_config.temperature,
                max_tokens=40,  # Hard limit at 40 tokens
                turn_number=turn_number,
            ):
                response_text += text_chunk
                # Token budget enforcer stops automatically at 50 tokens max
"""

print(__doc__)
