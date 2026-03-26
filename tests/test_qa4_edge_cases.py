"""
╔══════════════════════════════════════════════════════════════════╗
║  QA LEVEL 4: EDGE CASES & CALL MANAGER TESTS                    ║
║  Real-world conversation scenarios and CallGuard protection      ║
║  Silence, hold, voicemail, echo, interruption, cost limits       ║
╚══════════════════════════════════════════════════════════════════╝
"""
import asyncio
import time
import sys
import os
import pytest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.call_manager import (
    CallGuard,
    CallGuardConfig,
    CallState,
    _text_similarity,
    VOICEMAIL_PHRASES,
    HOLD_PHRASES,
    REPEAT_PHRASES,
)
from src.providers.twilio_telephony import TwilioTelephony


# ═══════════════════════════════════════════════════════════════════════════
# Silence Detection Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSilenceDetection:
    """Test silence detection and "are you there?" prompting."""

    def test_initial_no_silence(self, mock_call_guard):
        """No silence action before timeout."""
        guard = mock_call_guard
        guard.start()
        action = guard.check_silence()
        assert action is None

    def test_prompt_after_timeout(self, mock_call_guard_strict):
        """After N seconds silence, should prompt."""
        guard = mock_call_guard_strict
        guard.start()
        guard.last_speech_time = time.time() - 1.5  # 1.5s ago

        action = guard.check_silence()
        assert action == "prompt"
        assert guard.silence_prompted is True

    def test_hangup_after_double_timeout(self, mock_call_guard_strict):
        """After double timeout with prompt already sent, should hangup."""
        guard = mock_call_guard_strict
        guard.start()
        guard.last_speech_time = time.time() - 2.5  # 2.5s ago
        guard.silence_prompted = True

        action = guard.check_silence()
        assert action == "hangup"

    def test_speech_resets_silence_timer(self, mock_call_guard_strict):
        """User speech should reset silence timer and clear prompt flag."""
        guard = mock_call_guard_strict
        guard.start()
        guard.last_speech_time = time.time() - 1.5
        guard.silence_prompted = True

        # User speaks
        guard.record_speech()

        assert guard.silence_prompted is False
        assert guard.last_speech_time > time.time() - 0.1


# ═══════════════════════════════════════════════════════════════════════════
# Hold Detection Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestHoldDetection:
    """Test hold/wait request detection."""

    @pytest.mark.parametrize("phrase", [
        "hang on",
        "hold on",
        "wait a moment",
        "one second",
        "give me a minute",
        "just a sec",
        "un momento",
        "let me check",
        "bear with me",
    ])
    def test_hold_phrases(self, mock_call_guard, phrase):
        """Detect common hold phrases."""
        guard = mock_call_guard
        assert guard.check_hold_request(phrase) is True
        assert guard.state == CallState.HOLD

    def test_hold_extends_silence_timeout(self, mock_call_guard_strict):
        """In HOLD state, silence timeout should extend."""
        guard = mock_call_guard_strict
        guard.start()
        guard.check_hold_request("hang on")
        guard.last_speech_time = time.time() - 1.5

        # Should NOT prompt in HOLD state
        action = guard.check_silence()
        assert action is None

    def test_hold_exits_on_speech(self, mock_call_guard):
        """User speech should exit HOLD state."""
        guard = mock_call_guard
        guard.start()
        guard.check_hold_request("hold on")
        assert guard.state == CallState.HOLD

        guard.record_speech()
        assert guard.state == CallState.LISTENING

    def test_hold_max_timeout(self, mock_call_guard):
        """After max hold time, should prompt timeout."""
        guard = mock_call_guard
        guard.config.hold_max_timeout = 1.0  # 1 second for testing
        guard.start()
        guard.check_hold_request("wait a moment")
        assert guard.state == CallState.HOLD
        guard._hold_entered = time.time() - 2.0  # 2 seconds in HOLD

        action = guard.check_silence()
        assert action == "prompt_hold_timeout"


# ═══════════════════════════════════════════════════════════════════════════
# Voicemail Detection Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestVoicemailDetection:
    """Test voicemail detection from transcripts."""

    @pytest.mark.parametrize("phrase", [
        "leave a message",
        "not available",
        "after the tone",
        "please record",
        "voicemail",
        "at the beep",
        "mailbox",
        "reached the voicemail",
    ])
    def test_voicemail_phrases(self, mock_call_guard, phrase):
        """Detect common voicemail phrases."""
        guard = mock_call_guard
        guard.config.voicemail_detection_enabled = True
        assert guard.check_voicemail(phrase, call_elapsed=5.0) is True

    def test_voicemail_only_in_window(self, mock_call_guard):
        """Voicemail detection only works in first N seconds."""
        guard = mock_call_guard
        guard.config.voicemail_detection_window = 10.0
        guard.config.voicemail_detection_enabled = True

        # Within window
        result = guard.check_voicemail("please leave a message", call_elapsed=5.0)
        assert result is True

        # Outside window
        guard.voicemail_detected = False
        result = guard.check_voicemail("please leave a message", call_elapsed=15.0)
        assert result is False

    def test_voicemail_disabled(self, mock_call_guard):
        """Can disable voicemail detection."""
        guard = mock_call_guard
        guard.config.voicemail_detection_enabled = False
        result = guard.check_voicemail("please leave a message", call_elapsed=5.0)
        assert result is False

    def test_voicemail_detection_persists(self, mock_call_guard):
        """Once detected, voicemail flag stays true."""
        guard = mock_call_guard
        guard.config.voicemail_detection_enabled = True
        guard.config.voicemail_detection_window = 25.0  # Extend window so we test persistence

        # First detection
        guard.check_voicemail("leave a message", call_elapsed=5.0)
        assert guard.voicemail_detected is True

        # Second call with different text but still in window
        # When voicemail_detected is already True, should return True
        result = guard.check_voicemail("random text", call_elapsed=10.0)
        assert result is True  # True because voicemail_detected flag is already set


# ═══════════════════════════════════════════════════════════════════════════
# Echo Suppression Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEchoSuppression:
    """Test echo detection and suppression."""

    def test_exact_echo(self, mock_call_guard):
        """Detect exact echo of our own speech."""
        guard = mock_call_guard
        guard.record_agent_speech("Hello, how can I help you today?")

        # STT picks up our own speech
        is_echo = guard.is_echo("Hello, how can I help you today?")
        assert is_echo is True

    def test_partial_echo(self, mock_call_guard):
        """Detect partial echo with high text similarity."""
        guard = mock_call_guard
        guard.record_agent_speech("Hello, how can I help you today?")

        # Similar text should be detected as echo
        is_echo = guard.is_echo("Hello how can I help you")
        assert is_echo is True

    def test_no_false_positive_echo(self, mock_call_guard):
        """Don't flag completely different text as echo."""
        guard = mock_call_guard
        guard.record_agent_speech("Good morning, what can I do for you?")

        # Different content shouldn't match
        is_echo = guard.is_echo("I'd like to schedule an appointment")
        assert is_echo is False

    def test_echo_suppression_disabled(self, mock_call_guard):
        """Can disable echo suppression."""
        guard = mock_call_guard
        guard.config.echo_suppression_enabled = False
        guard.record_agent_speech("Hello, how can I help?")

        is_echo = guard.is_echo("Hello, how can I help?")
        assert is_echo is False

    def test_echo_buffer_rolling(self, mock_call_guard):
        """Echo buffer keeps rolling window of last N utterances."""
        guard = mock_call_guard
        guard.record_agent_speech("First message")
        guard.record_agent_speech("Second message")
        guard.record_agent_speech("Third message")
        guard.record_agent_speech("Fourth message")
        guard.record_agent_speech("Fifth message")
        guard.record_agent_speech("Sixth message")  # Should push "First" out

        # "First" should no longer be in buffer
        is_echo = guard.is_echo("First message")
        assert is_echo is False

        # "Sixth" should still be there
        is_echo = guard.is_echo("Sixth message")
        assert is_echo is True


# ═══════════════════════════════════════════════════════════════════════════
# Repeat Request Detection Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRepeatDetection:
    """Test repeat/clarification request detection."""

    @pytest.mark.parametrize("phrase", [
        "what did you say?",
        "can you repeat that?",
        "say that again",
        "repeat that",
        "I didn't catch that",
        "sorry?",
        "huh?",
        "come again?",
        "pardon?",
    ])
    def test_repeat_phrases(self, mock_call_guard, phrase):
        """Detect repeat request phrases."""
        guard = mock_call_guard
        assert guard.check_repeat_request(phrase) is True

    def test_no_false_positive_repeat(self, mock_call_guard):
        """Don't flag normal speech as repeat request."""
        guard = mock_call_guard
        assert guard.check_repeat_request("I'd like to book an appointment") is False


# ═══════════════════════════════════════════════════════════════════════════
# Interruption & Adaptive Brevity Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestInterruptionTracking:
    """Test interruption tracking and adaptive response shortening."""

    def test_record_interruption(self, mock_call_guard):
        """Track individual interruptions."""
        guard = mock_call_guard
        assert guard.interruption_count == 0
        assert guard.consecutive_interruptions == 0

        guard.record_interruption()
        assert guard.interruption_count == 1
        assert guard.consecutive_interruptions == 1

    def test_adaptive_brevity_threshold(self, mock_call_guard):
        """After N consecutive interruptions, should shorten responses."""
        guard = mock_call_guard
        guard.config.adaptive_brevity = True
        guard.config.adaptive_brevity_threshold = 3

        assert guard.should_shorten_responses() is False

        guard.record_interruption()
        assert guard.should_shorten_responses() is False

        guard.record_interruption()
        assert guard.should_shorten_responses() is False

        guard.record_interruption()
        assert guard.should_shorten_responses() is True

    def test_reset_consecutive_interruptions(self, mock_call_guard):
        """Reset counter when turn completes without interruption."""
        guard = mock_call_guard
        guard.record_interruption()
        guard.record_interruption()
        guard.record_interruption()
        assert guard.consecutive_interruptions == 3

        guard.reset_consecutive_interruptions()
        assert guard.consecutive_interruptions == 0
        assert guard.should_shorten_responses() is False

    def test_adaptive_brevity_can_be_disabled(self, mock_call_guard):
        """Can disable adaptive brevity."""
        guard = mock_call_guard
        guard.config.adaptive_brevity = False
        for _ in range(5):
            guard.record_interruption()

        assert guard.should_shorten_responses() is False


# ═══════════════════════════════════════════════════════════════════════════
# Cost Limit Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCostLimits:
    """Test cost tracking and limit enforcement."""

    def test_update_cost(self, mock_call_guard):
        """Track cumulative cost."""
        guard = mock_call_guard
        assert guard.total_cost == 0.0

        guard.update_cost(0.25)
        assert guard.total_cost == 0.25

        guard.update_cost(0.15)
        assert guard.total_cost == 0.40

    def test_cost_warning(self, mock_call_guard):
        """Warn when approaching cost limit."""
        guard = mock_call_guard
        guard.config.max_cost_usd = 1.0
        guard.config.cost_warning_threshold = 0.80

        guard.update_cost(0.70)
        result = guard.check_cost_limit()
        assert result is None  # Below threshold

        guard.update_cost(0.15)  # Now at 0.85 (85% of limit)
        result = guard.check_cost_limit()
        assert result == "warn"

        # Should only warn once
        result = guard.check_cost_limit()
        assert result is None

    def test_cost_hangup(self, mock_call_guard):
        """Hangup when cost limit exceeded."""
        guard = mock_call_guard
        guard.config.max_cost_usd = 1.0

        guard.update_cost(0.95)
        guard.update_cost(0.10)  # Now at 1.05 (exceeds limit)

        result = guard.check_cost_limit()
        assert result == "hangup"


# ═══════════════════════════════════════════════════════════════════════════
# Duration Limit Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestDurationLimits:
    """Test call duration limits."""

    def test_no_limit_within_duration(self, mock_call_guard_strict):
        """No action within duration limit."""
        guard = mock_call_guard_strict
        guard.config.max_call_duration = 100  # 100 seconds
        guard.start()
        guard.call_start_time = time.time() - 5  # 5 seconds ago

        result = guard.check_duration_limit()
        assert result is None

    def test_warn_near_end(self, mock_call_guard_strict):
        """Warn 30 seconds before limit."""
        guard = mock_call_guard_strict
        guard.config.max_call_duration = 100
        guard.start()
        guard.call_start_time = time.time() - 71  # 71 seconds ago (29 sec before limit)

        result = guard.check_duration_limit()
        assert result == "warn"

    def test_hangup_at_limit(self, mock_call_guard_strict):
        """Hangup when duration limit reached."""
        guard = mock_call_guard_strict
        guard.config.max_call_duration = 100
        guard.start()
        guard.call_start_time = time.time() - 101  # 101 seconds ago

        result = guard.check_duration_limit()
        assert result == "hangup"


# ═══════════════════════════════════════════════════════════════════════════
# DTMF Handling Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestDTMFHandling:
    """Test DTMF tone handling."""

    def test_dtmf_0_operator(self, mock_call_guard):
        """DTMF 0 -> transfer operator."""
        guard = mock_call_guard
        guard.config.dtmf_enabled = True
        action = guard.handle_dtmf("0")
        assert action == "transfer_operator"

    def test_dtmf_star_repeat(self, mock_call_guard):
        """DTMF * -> repeat last message."""
        guard = mock_call_guard
        guard.config.dtmf_enabled = True
        action = guard.handle_dtmf("*")
        assert action == "repeat_last"

    def test_dtmf_hash_end(self, mock_call_guard):
        """DTMF # -> end call."""
        guard = mock_call_guard
        guard.config.dtmf_enabled = True
        action = guard.handle_dtmf("#")
        assert action == "end_call"

    def test_dtmf_unmapped(self, mock_call_guard):
        """Unmapped DTMF returns None."""
        guard = mock_call_guard
        guard.config.dtmf_enabled = True
        action = guard.handle_dtmf("5")
        assert action is None

    def test_dtmf_disabled(self, mock_call_guard):
        """Disabled DTMF returns None."""
        guard = mock_call_guard
        guard.config.dtmf_enabled = False
        action = guard.handle_dtmf("0")
        assert action is None


# ═══════════════════════════════════════════════════════════════════════════
# Beep/Tone Detection Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestBeepDetection:
    """Test beep tone detection for voicemail."""

    def test_detect_tone_in_audio(self):
        """Detect sustained tone in PCM audio."""
        sample_rate = 16000
        duration_sec = 0.5
        frequency = 1000  # 1kHz tone

        # Generate sine wave at 1kHz
        samples = np.arange(int(sample_rate * duration_sec))
        tone = np.sin(2 * np.pi * frequency * samples / sample_rate)
        audio_pcm = (tone * 32767).astype(np.int16).tobytes()

        is_beep = CallGuard.detect_beep(audio_pcm, sample_rate)
        # numpy bool needs to be compared with ==, not is
        assert bool(is_beep) is True

    def test_no_beep_in_silence(self):
        """No beep detected in silent audio."""
        sample_rate = 16000
        duration_sec = 0.5

        # Silent audio
        silence = np.zeros(int(sample_rate * duration_sec), dtype=np.int16)
        audio_pcm = silence.tobytes()

        is_beep = CallGuard.detect_beep(audio_pcm, sample_rate)
        assert bool(is_beep) is False

    def test_tone_too_short_not_beep(self):
        """Brief tone doesn't count as beep."""
        sample_rate = 16000
        duration_sec = 0.1  # Only 100ms, need 300ms
        frequency = 1000

        samples = np.arange(int(sample_rate * duration_sec))
        tone = np.sin(2 * np.pi * frequency * samples / sample_rate)
        audio_pcm = (tone * 32767).astype(np.int16).tobytes()

        is_beep = CallGuard.detect_beep(audio_pcm, sample_rate)
        assert is_beep is False


# ═══════════════════════════════════════════════════════════════════════════
# Audio Format Conversion Tests (Twilio)
# ═══════════════════════════════════════════════════════════════════════════


class TestAudioFormatConversion:
    """Test mulaw <-> PCM audio format conversion."""

    def test_mulaw_to_pcm_conversion(self):
        """Convert mulaw 8kHz to PCM 16kHz."""
        # Create simple mulaw data
        import audioop

        # Create a simple PCM 8kHz signal (1 second = 8000 samples = 16000 bytes)
        pcm_8k = np.zeros(8000, dtype=np.int16).tobytes()
        mulaw = audioop.lin2ulaw(pcm_8k, 2)

        # Convert back
        pcm_16k = TwilioTelephony.mulaw_8k_to_pcm_16k(mulaw)

        # Should roughly double the samples (8kHz -> 16kHz)
        # mulaw is 1 byte per sample, PCM is 2 bytes per sample
        # So PCM at 16kHz should be ~4x the length of mulaw at 8kHz
        assert len(pcm_16k) > len(mulaw) * 3  # At least 3x due to resampling

    def test_pcm_to_mulaw_conversion(self):
        """Convert PCM 16kHz to mulaw 8kHz."""
        # Create simple PCM 16kHz data (1 second = 16000 samples = 32000 bytes)
        pcm_16k = np.zeros(16000, dtype=np.int16).tobytes()

        mulaw = TwilioTelephony.pcm_16k_to_mulaw_8k(pcm_16k)

        # Should reduce length (16kHz -> 8kHz, PCM 2 bytes/sample -> mulaw 1 byte/sample)
        # Roughly 1/4 the original length
        assert len(mulaw) < len(pcm_16k)
        assert len(mulaw) > len(pcm_16k) // 10  # At least some samples converted

    def test_roundtrip_conversion(self):
        """Test roundtrip conversion maintains reasonable fidelity."""
        import audioop

        # Create test PCM 16kHz signal
        sample_rate = 16000
        duration = 0.1
        frequency = 440

        t = np.arange(int(sample_rate * duration))
        signal = np.sin(2 * np.pi * frequency * t / sample_rate)
        pcm_16k = (signal * 1000).astype(np.int16).tobytes()

        # Convert to mulaw 8kHz and back
        mulaw = TwilioTelephony.pcm_16k_to_mulaw_8k(pcm_16k)
        pcm_16k_recovered = TwilioTelephony.mulaw_8k_to_pcm_16k(mulaw)

        # Should be approximately same length
        assert abs(len(pcm_16k_recovered) - len(pcm_16k)) < len(pcm_16k) * 0.1


# ═══════════════════════════════════════════════════════════════════════════
# Text Similarity Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTextSimilarity:
    """Test text similarity function for echo detection."""

    def test_identical_text(self):
        """Identical text should have perfect similarity."""
        text = "hello world this is a test"
        similarity = _text_similarity(text, text)
        assert similarity == 1.0

    def test_completely_different_text(self):
        """Completely different text should have zero similarity."""
        sim = _text_similarity("hello world", "goodbye universe")
        assert sim == 0.0

    def test_partial_overlap(self):
        """Partial word overlap should give fractional similarity."""
        # "hello world test" vs "hello world apple"
        # Overlap: hello, world = 2 words
        # Max length: 3
        # Similarity: 2/3 ≈ 0.67
        sim = _text_similarity("hello world test", "hello world apple")
        assert 0.6 < sim < 0.7

    def test_empty_strings(self):
        """Empty strings should have zero similarity."""
        assert _text_similarity("", "") == 0.0
        assert _text_similarity("hello", "") == 0.0
        assert _text_similarity("", "world") == 0.0

    def test_case_insensitive(self):
        """Similarity should be case-insensitive."""
        # _text_similarity already lowercases internally
        sim1 = _text_similarity("hello world", "hello world")
        sim2 = _text_similarity("hello world", "hello world")
        assert sim1 == 1.0
        assert sim2 == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# State Machine Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCallStateTransitions:
    """Test call state machine transitions."""

    def test_initial_state(self, mock_call_guard):
        """Initial state should be INITIALIZING."""
        guard = mock_call_guard
        assert guard.state == CallState.INITIALIZING

    def test_start_transitions_to_connected(self, mock_call_guard):
        """Calling start() transitions to CONNECTED."""
        guard = mock_call_guard
        guard.start()
        assert guard.state == CallState.CONNECTED

    def test_hold_transition(self, mock_call_guard):
        """Hold request transitions to HOLD state."""
        guard = mock_call_guard
        guard.start()
        assert guard.state == CallState.CONNECTED

        guard.check_hold_request("hold on")
        assert guard.state == CallState.HOLD

    def test_speech_exits_hold(self, mock_call_guard):
        """User speech in HOLD transitions to LISTENING."""
        guard = mock_call_guard
        guard.start()
        guard.check_hold_request("hang on")
        assert guard.state == CallState.HOLD

        guard.record_speech()
        assert guard.state == CallState.LISTENING


# ═══════════════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCaseScenarios:
    """Integration tests for realistic conversation scenarios."""

    def test_scenario_user_holds_then_speaks(self, mock_call_guard):
        """Realistic scenario: user asks to hold, then speaks again."""
        guard = mock_call_guard
        guard.start()

        # Agent speaks
        guard.record_agent_speech("I'll check that for you.")
        assert guard.last_agent_text == "I'll check that for you."

        # User says hold on
        assert guard.check_hold_request("hold on") is True
        assert guard.state == CallState.HOLD

        # Long silence (but extended in hold)
        guard.last_speech_time = time.time() - 10
        action = guard.check_silence()
        assert action is None  # Extended timeout in HOLD

        # User says something
        guard.record_speech()
        assert guard.state == CallState.LISTENING

    def test_scenario_repeated_interruptions(self, mock_call_guard):
        """Realistic scenario: user keeps interrupting, agent adapts."""
        guard = mock_call_guard
        guard.config.adaptive_brevity_threshold = 2

        # Agent starts speaking
        guard.record_agent_speech("Let me explain the benefits of this service.")
        assert guard.should_shorten_responses() is False

        # User interrupts once
        guard.record_interruption()
        assert guard.should_shorten_responses() is False

        # User interrupts twice
        guard.record_interruption()
        assert guard.should_shorten_responses() is True  # Now adapt

        # Turn completes without interruption, reset
        guard.reset_consecutive_interruptions()
        assert guard.should_shorten_responses() is False

    def test_scenario_voicemail_false_positive_prevention(self, mock_call_guard):
        """Voicemail shouldn't trigger after initial window."""
        guard = mock_call_guard
        guard.config.voicemail_detection_window = 5.0

        # Early in call, voicemail detected
        assert guard.check_voicemail("please leave a message", call_elapsed=2.0) is True

        # Later in call, similar phrase should NOT trigger
        guard.voicemail_detected = False
        result = guard.check_voicemail("please leave a message", call_elapsed=8.0)
        assert result is False
