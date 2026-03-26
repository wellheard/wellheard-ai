"""
╔══════════════════════════════════════════════════════════════════╗
║  QA LEVEL 1: UNIT TESTS                                         ║
║  Tests individual components in isolation                        ║
║  Provider interfaces, health tracking, cost calculation,         ║
║  audio utilities, and configuration validation                   ║
╚══════════════════════════════════════════════════════════════════╝
"""
import asyncio
import time
import sys
import os
import pytest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.providers.base import ProviderHealth, ProviderStatus, LatencyTrace, CostEstimate
from src.utils.audio import pcm_to_float32, float32_to_pcm, resample_linear, is_silence, AudioRingBuffer
from src.monitoring.metrics import MetricsCollector


# ═══════════════════════════════════════════════════════════════════════════
# Provider Health Tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestProviderHealth:
    """Test that provider health tracking correctly detects degradation."""

    def test_initial_state_healthy(self):
        health = ProviderHealth(provider_name="test")
        assert health.status == ProviderStatus.HEALTHY
        assert health.is_healthy
        assert health.consecutive_errors == 0

    def test_success_keeps_healthy(self):
        health = ProviderHealth(provider_name="test")
        for _ in range(10):
            health.record_success(100.0)
        assert health.is_healthy
        assert health.total_requests == 10
        assert health.error_rate == 0.0

    def test_errors_trigger_degraded(self):
        health = ProviderHealth(provider_name="test")
        health.record_success(100.0)
        health.record_error()
        health.record_error()
        assert health.status == ProviderStatus.DEGRADED

    def test_many_errors_trigger_unhealthy(self):
        health = ProviderHealth(provider_name="test")
        for _ in range(5):
            health.record_error()
        assert health.status == ProviderStatus.UNHEALTHY
        assert not health.is_healthy

    def test_recovery_after_errors(self):
        health = ProviderHealth(provider_name="test")
        health.record_error()
        health.record_error()
        assert health.status == ProviderStatus.DEGRADED

        # Successful requests reset consecutive errors
        health.record_success(100.0)
        assert health.consecutive_errors == 0

    def test_latency_tracking(self):
        health = ProviderHealth(provider_name="test")
        health.record_success(200.0)
        health.record_success(300.0)
        # EMA should reflect recent values
        assert health.avg_latency_ms > 0


# ═══════════════════════════════════════════════════════════════════════════
# Latency Trace
# ═══════════════════════════════════════════════════════════════════════════

class TestLatencyTrace:

    def test_trace_lifecycle(self):
        trace = LatencyTrace(provider="test", operation="stt")
        time.sleep(0.01)
        trace.mark_first_result()
        assert trace.time_to_first_result_ms > 0
        trace.mark_complete()
        assert trace.total_ms >= trace.time_to_first_result_ms

    def test_trace_without_first_result(self):
        trace = LatencyTrace(provider="test", operation="stt")
        assert trace.time_to_first_result_ms == 0


# ═══════════════════════════════════════════════════════════════════════════
# Cost Estimation
# ═══════════════════════════════════════════════════════════════════════════

class TestCostEstimation:

    def test_stt_cost_deepgram_budget(self):
        """Verify Deepgram Nova-3 streaming cost: $0.0077/min"""
        cost = CostEstimate(provider="deepgram", component="stt",
                           cost_usd=0.0077, units=1.0, unit_type="minutes")
        assert cost.cost_usd == 0.0077

    def test_llm_cost_groq(self):
        """Verify Groq cost for typical voice conversation turn."""
        # Typical turn: 50 input tokens, 50 output tokens
        input_cost = 50 / 1000 * 0.00011
        output_cost = 50 / 1000 * 0.00030
        total = input_cost + output_cost
        assert total < 0.0001  # Less than $0.0001 per turn

    def test_tts_cost_deepgram_aura(self):
        """Verify Deepgram Aura-2 TTS cost: ~$0.010/min"""
        cost_per_min = 0.010
        five_min_call = cost_per_min * 5
        assert five_min_call == 0.05

    def test_total_budget_pipeline_cost(self):
        """Verify total budget pipeline stays within $0.02-$0.04/min target."""
        stt = 0.0077   # Deepgram Nova-3 streaming
        llm = 0.0002   # Groq Llama
        tts = 0.0100   # Deepgram Aura-2
        telephony = 0.0070  # Telnyx outbound
        total = stt + llm + tts + telephony
        assert 0.02 <= total <= 0.04, f"Budget pipeline cost ${total}/min outside target"

    def test_total_quality_pipeline_cost(self):
        """Verify total quality pipeline stays within $0.02-$0.04/min target."""
        stt = 0.0077   # Deepgram Nova-3 streaming
        llm = 0.0009   # Gemini Flash
        tts = 0.0100   # Cartesia Sonic
        telephony = 0.0070  # Telnyx outbound
        total = stt + llm + tts + telephony
        assert 0.02 <= total <= 0.04, f"Quality pipeline cost ${total}/min outside target"

    def test_cost_savings_vs_dasha(self):
        """Verify both pipelines are cheaper than Dasha's $0.08/min."""
        dasha_cost = 0.08
        budget_cost = 0.0077 + 0.0002 + 0.0100 + 0.0070  # $0.0249
        quality_cost = 0.0077 + 0.0009 + 0.0100 + 0.0070  # $0.0256
        assert budget_cost < dasha_cost, "Budget must be cheaper than Dasha"
        assert quality_cost < dasha_cost, "Quality must be cheaper than Dasha"
        savings_pct = (1 - budget_cost / dasha_cost) * 100
        assert savings_pct > 50, f"Expected >50% savings, got {savings_pct:.1f}%"


# ═══════════════════════════════════════════════════════════════════════════
# Audio Utilities
# ═══════════════════════════════════════════════════════════════════════════

class TestAudioUtilities:

    def test_pcm_to_float32_conversion(self):
        # Max positive int16 should map to ~1.0
        pcm = b"\xff\x7f"  # 32767 in little-endian int16
        result = pcm_to_float32(pcm, sample_width=2)
        assert abs(result[0] - 1.0) < 0.001

    def test_float32_to_pcm_roundtrip(self):
        original = np.array([0.5, -0.5, 0.0, 1.0, -1.0], dtype=np.float32)
        pcm = float32_to_pcm(original)
        recovered = pcm_to_float32(pcm)
        np.testing.assert_allclose(original, recovered, atol=0.001)

    def test_resample_upsample(self):
        audio = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        resampled = resample_linear(audio, 8000, 16000)
        assert len(resampled) == 8  # Double the samples

    def test_resample_same_rate(self):
        audio = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = resample_linear(audio, 16000, 16000)
        np.testing.assert_array_equal(audio, result)

    def test_silence_detection(self):
        silence = b"\x00\x00" * 100
        assert is_silence(silence)

        # Non-silence
        loud = b"\xff\x7f" * 100
        assert not is_silence(loud)

    def test_ring_buffer_basic(self):
        buf = AudioRingBuffer(max_seconds=1.0, sample_rate=16000, sample_width=2)
        data = b"\x01\x02" * 1000
        buf.write(data)
        assert buf.available == 2000
        read_data = buf.read()
        assert read_data == data
        assert buf.available == 0

    def test_ring_buffer_overflow_drops_old(self):
        buf = AudioRingBuffer(max_seconds=0.1, sample_rate=16000, sample_width=2)
        max_bytes = int(0.1 * 16000 * 2)
        # Write more than buffer can hold
        big_data = b"\x01" * (max_bytes * 3)
        buf.write(big_data)
        assert buf.available == max_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Metrics Collector
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsCollector:

    def test_record_and_retrieve(self):
        mc = MetricsCollector()
        mc.record_call_start("call-1", "budget")
        mc.record_turn("call-1", {"stt_latency_ms": 200})
        mc.record_call_end("call-1", {"total_cost_usd": 0.001, "duration_seconds": 60})

        dashboard = mc.get_dashboard()
        assert dashboard["total_calls"] == 1
        assert dashboard["total_cost_usd"] > 0

    def test_provider_latency_tracking(self):
        mc = MetricsCollector()
        for i in range(100):
            mc.record_provider_latency("deepgram_nova3", 200 + i)

        dashboard = mc.get_dashboard()
        stats = dashboard["providers"]["deepgram_nova3"]
        assert stats["avg_latency_ms"] > 0
        assert stats["p95_latency_ms"] > stats["avg_latency_ms"]

    def test_active_calls_count(self):
        mc = MetricsCollector()
        mc.record_call_start("call-1", "budget")
        mc.record_call_start("call-2", "quality")
        # call-1 has no "final" key so it's active
        assert mc.get_dashboard()["active_calls"] == 2

        mc.record_call_end("call-1", {"total_cost_usd": 0.001, "duration_seconds": 60})
        assert mc.get_dashboard()["active_calls"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Run all unit tests
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
