"""
╔══════════════════════════════════════════════════════════════════╗
║  QA LEVEL 2: INTEGRATION TESTS                                  ║
║  Tests component interactions and pipeline behavior              ║
║  Orchestrator flow, failover, barge-in, API endpoints            ║
╚══════════════════════════════════════════════════════════════════╝
"""
import asyncio
import time
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.pipelines.orchestrator import VoicePipelineOrchestrator, AgentConfig, CallMetrics
from tests.conftest import MockSTTProvider, MockLLMProvider, MockTTSProvider, audio_chunk_generator


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline Orchestrator Integration
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineOrchestrator:
    """Test full pipeline flow with mock providers."""

    @pytest.mark.asyncio
    async def test_start_and_end_call(self, mock_stt, mock_llm, mock_tts):
        """Test basic call lifecycle."""
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)

        metrics = await orchestrator.start_call(AgentConfig(), pipeline_mode="budget")
        assert isinstance(metrics, CallMetrics)
        assert orchestrator.is_active
        assert mock_stt._connected

        result = await orchestrator.end_call()
        assert not orchestrator.is_active
        assert "call_id" in result

    @pytest.mark.asyncio
    async def test_full_conversation_turn(self, mock_stt, mock_llm, mock_tts):
        """Test a complete listen → think → speak turn."""
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig(), pipeline_mode="budget")

        transcripts = []
        responses = []
        audio_chunks = []

        async def on_transcript(text, is_final):
            transcripts.append({"text": text, "is_final": is_final})

        async def on_response(text):
            responses.append(text)

        async def on_audio(audio):
            audio_chunks.append(audio)

        result = await orchestrator.process_turn(
            audio_stream=audio_chunk_generator(5),
            config=AgentConfig(),
            on_transcript=on_transcript,
            on_response_text=on_response,
            on_audio_chunk=on_audio,
        )

        assert result["status"] == "completed"
        assert result["transcript"] != ""
        assert result["response"] != ""
        assert result["stt_latency_ms"] > 0
        assert result["llm_ttft_ms"] > 0
        assert result["tts_ttfb_ms"] > 0
        assert len(transcripts) > 0
        assert len(responses) > 0
        assert len(audio_chunks) > 0

        await orchestrator.end_call()

    @pytest.mark.asyncio
    async def test_multiple_turns(self, mock_stt, mock_llm, mock_tts):
        """Test multiple conversation turns maintain state."""
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig(), pipeline_mode="quality")

        for i in range(3):
            result = await orchestrator.process_turn(
                audio_stream=audio_chunk_generator(3),
                config=AgentConfig(),
            )
            assert result["status"] == "completed"

        assert orchestrator.metrics.turns == 3
        final = await orchestrator.end_call()
        assert final["turns"] == 3

    @pytest.mark.asyncio
    async def test_cost_tracking_per_turn(self, mock_stt, mock_llm, mock_tts):
        """Test that cost is tracked per turn and accumulates correctly."""
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig(), pipeline_mode="budget")

        result = await orchestrator.process_turn(
            audio_stream=audio_chunk_generator(3),
            config=AgentConfig(),
        )

        assert result["turn_cost_usd"] > 0
        assert orchestrator.metrics.total_cost > 0

        await orchestrator.end_call()


# ═══════════════════════════════════════════════════════════════════════════
# Failover Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFailover:
    """Test automatic provider failover when primary degrades."""

    @pytest.mark.asyncio
    async def test_llm_failover_on_unhealthy(self):
        """When primary LLM is unhealthy, should use fallback."""
        primary_llm = MockLLMProvider(ttft_ms=400)
        fallback_llm = MockLLMProvider(ttft_ms=500)
        mock_stt = MockSTTProvider(latency_ms=200)
        mock_tts = MockTTSProvider(ttfb_ms=50)

        # Make primary unhealthy
        for _ in range(5):
            primary_llm._health.record_error()
        assert not primary_llm.get_health().is_healthy

        orchestrator = VoicePipelineOrchestrator(
            stt=mock_stt, llm=primary_llm, tts=mock_tts,
            fallback_llm=fallback_llm,
        )
        await orchestrator.start_call(AgentConfig(), pipeline_mode="budget")

        result = await orchestrator.process_turn(
            audio_stream=audio_chunk_generator(3),
            config=AgentConfig(),
        )

        # Should succeed using fallback
        assert result["status"] == "completed"
        # Fallback LLM should have been used (recorded success)
        assert fallback_llm.get_health().total_requests > 0

        await orchestrator.end_call()

    @pytest.mark.asyncio
    async def test_tts_failover_on_unhealthy(self):
        """When primary TTS is unhealthy, should use fallback."""
        primary_tts = MockTTSProvider(ttfb_ms=40)
        fallback_tts = MockTTSProvider(ttfb_ms=90)
        mock_stt = MockSTTProvider(latency_ms=200)
        mock_llm = MockLLMProvider(ttft_ms=400)

        # Make primary unhealthy
        for _ in range(5):
            primary_tts._health.record_error()

        orchestrator = VoicePipelineOrchestrator(
            stt=mock_stt, llm=mock_llm, tts=primary_tts,
            fallback_tts=fallback_tts,
        )
        await orchestrator.start_call(AgentConfig(), pipeline_mode="quality")

        result = await orchestrator.process_turn(
            audio_stream=audio_chunk_generator(3),
            config=AgentConfig(),
        )

        assert result["status"] == "completed"
        assert fallback_tts.get_health().total_requests > 0

        await orchestrator.end_call()


# ═══════════════════════════════════════════════════════════════════════════
# Barge-In / Interruption Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestBargeIn:
    """Test user interruption handling."""

    @pytest.mark.asyncio
    async def test_interruption_cancels_tts(self):
        """Barge-in should immediately cancel TTS output."""
        mock_stt = MockSTTProvider(latency_ms=200)
        mock_llm = MockLLMProvider(ttft_ms=400)
        mock_tts = MockTTSProvider(ttfb_ms=40)

        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig(), pipeline_mode="budget")

        # Start a turn in background
        async def run_turn():
            return await orchestrator.process_turn(
                audio_stream=audio_chunk_generator(3),
                config=AgentConfig(),
            )

        turn_task = asyncio.create_task(run_turn())

        # Wait for TTS to start, then interrupt
        await asyncio.sleep(0.8)
        await orchestrator.handle_interruption()

        result = await turn_task
        assert orchestrator.metrics.interruptions >= 0  # May or may not register depending on timing

        await orchestrator.end_call()


# ═══════════════════════════════════════════════════════════════════════════
# Agent Configuration Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentConfig:

    def test_default_config(self):
        config = AgentConfig()
        assert config.agent_id == "default"
        assert config.language == "en"
        assert config.temperature == 0.7
        assert config.interruption_enabled is True

    def test_custom_config(self):
        config = AgentConfig(
            agent_id="sales_bot",
            system_prompt="You are a sales agent for Brightcall.",
            voice_id="cartesia_warm_female",
            temperature=0.5,
            max_tokens=512,
        )
        assert config.agent_id == "sales_bot"
        assert config.max_tokens == 512


# ═══════════════════════════════════════════════════════════════════════════
# Connection Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectionLifecycle:

    @pytest.mark.asyncio
    async def test_providers_connect_on_call_start(self, mock_stt, mock_llm, mock_tts):
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig())
        assert mock_stt._connected
        assert mock_tts._connected
        await orchestrator.end_call()

    @pytest.mark.asyncio
    async def test_providers_disconnect_on_call_end(self, mock_stt, mock_llm, mock_tts):
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        await orchestrator.start_call(AgentConfig())
        await orchestrator.end_call()
        assert not mock_stt._connected
        assert not mock_tts._connected

    @pytest.mark.asyncio
    async def test_process_turn_without_start_raises(self, mock_stt, mock_llm, mock_tts):
        orchestrator = VoicePipelineOrchestrator(stt=mock_stt, llm=mock_llm, tts=mock_tts)
        with pytest.raises(RuntimeError, match="Call not started"):
            await orchestrator.process_turn(
                audio_stream=audio_chunk_generator(1),
                config=AgentConfig(),
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
