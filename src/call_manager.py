"""
WellHeard AI — Intelligent Call Manager
Handles all real-world conversation edge cases with intelligent state management.
"""
import asyncio
import time
import re
import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

logger = structlog.get_logger()


class CallState(str, Enum):
    """Call state machine."""
    INITIALIZING = "initializing"
    RINGING = "ringing"
    CONNECTED = "connected"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    HOLD = "hold"
    VOICEMAIL = "voicemail"
    ENDING = "ending"
    ENDED = "ended"


@dataclass
class CallGuardConfig:
    """Configuration for call protection features."""

    # Silence handling
    silence_prompt_timeout: float = 5.0  # seconds before "are you there?"
    silence_hangup_timeout: float = 15.0  # total silence before hangup
    hold_max_timeout: float = 120.0  # max hold time

    # Voicemail detection
    voicemail_detection_enabled: bool = True
    voicemail_action: str = "hangup"  # "hangup" or "leave_message"
    voicemail_message: str = ""  # message to leave if action is leave_message
    voicemail_detection_window: float = 10.0  # seconds to check for VM

    # Interruption
    interruption_enabled: bool = True
    interruption_energy_threshold: float = 0.02
    interruption_min_duration_ms: int = 150
    adaptive_brevity: bool = True  # shorten responses after repeated interruptions
    adaptive_brevity_threshold: int = 3  # interruptions before adapting

    # Credit protection
    max_call_duration: int = 1800  # 30 min
    max_cost_usd: float = 1.00  # $1 per call hard cap
    cost_warning_threshold: float = 0.80  # warn at 80% of max

    # Echo suppression
    echo_suppression_enabled: bool = True
    echo_match_threshold: float = 0.7  # 70% text similarity

    # DTMF
    dtmf_enabled: bool = True
    dtmf_actions: dict = field(
        default_factory=lambda: {
            "0": "transfer_operator",
            "*": "repeat_last",
            "#": "end_call",
        }
    )

    # Filler audio
    filler_enabled: bool = True
    filler_threshold_ms: int = 800  # play filler if TTFT > this


# ═══════════════════════════════════════════════════════════════════════════
# Voicemail Detection Patterns
# ═══════════════════════════════════════════════════════════════════════════

VOICEMAIL_PHRASES = [
    r"leave\s+(a\s+)?message",
    r"not\s+available",
    r"after\s+the\s+(tone|beep)",
    r"please\s+record",
    r"voicemail",
    r"at\s+the\s+beep",
    r"mailbox",
    r"press\s+\d+\s+to\s+leave",
    r"record\s+(your|a)\s+message",
    r"currently\s+unable\s+to\s+take",
    r"reached\s+the\s+voicemail",
]

# Hold request phrases
HOLD_PHRASES = [
    r"hang\s+on",
    r"hold\s+on",
    r"wait\s+(a\s+)?(moment|minute|second|sec|bit)",
    r"one\s+(second|moment|minute|sec)",
    r"give\s+me\s+(a\s+)?(second|moment|minute)",
    r"just\s+(a\s+)?(sec|second|moment|minute)",
    r"un\s+momento",
    r"let\s+me\s+(check|think|see)",
    r"bear\s+with\s+me",
]

# Repeat request phrases
REPEAT_PHRASES = [
    r"what\s+did\s+you\s+say",
    r"can\s+you\s+repeat",
    r"say\s+that\s+again",
    r"repeat\s+that",
    r"i\s+didn.?t\s+(catch|hear|get)\s+that",
    r"sorry\s*\??$",
    r"^huh\s*\??$",
    r"come\s+again",
    r"pardon\s*\??",
    r"what\s+was\s+that",
    r"could\s+you\s+say\s+that\s+again",
]


class CallGuard:
    """Monitors a call and handles edge cases in real-time."""

    def __init__(self, config: Optional[CallGuardConfig] = None):
        """Initialize CallGuard.

        Args:
            config: CallGuardConfig instance, or None for defaults
        """
        self.config = config or CallGuardConfig()
        self.state = CallState.INITIALIZING
        self.call_start_time: float = 0
        self.last_speech_time: float = 0
        self.last_agent_speech_time: float = 0
        self.silence_prompted: bool = False
        self.interruption_count: int = 0
        self.consecutive_interruptions: int = 0
        self.last_agent_text: str = ""
        self.recent_tts_text: list[str] = []  # Rolling buffer for echo detection
        self.voicemail_detected: bool = False
        self.first_transcript_time: float = 0
        self.total_cost: float = 0
        self.cost_warned: bool = False
        self._hold_entered: float = 0
        self._dtmf_buffer: str = ""

    def start(self):
        """Mark call as started and initialize timers."""
        self.call_start_time = time.time()
        self.last_speech_time = time.time()
        self.state = CallState.CONNECTED

    # ───────────────────────────────────────────────────────────────────────
    # Silence Detection
    # ───────────────────────────────────────────────────────────────────────

    def check_silence(self) -> Optional[str]:
        """Check silence status. Returns action: None, 'prompt', or 'hangup'.

        Returns:
            None if no action, 'prompt' to ask if user is there,
            'hangup' to end call, 'prompt_hold_timeout' if hold expired
        """
        if self.state == CallState.HOLD:
            elapsed = time.time() - self._hold_entered
            if elapsed > self.config.hold_max_timeout:
                return "prompt_hold_timeout"
            return None

        if self.state not in (CallState.LISTENING, CallState.CONNECTED):
            return None

        silence_duration = time.time() - self.last_speech_time

        if silence_duration > self.config.silence_hangup_timeout and self.silence_prompted:
            return "hangup"
        elif (
            silence_duration > self.config.silence_prompt_timeout
            and not self.silence_prompted
        ):
            self.silence_prompted = True
            return "prompt"
        return None

    def record_speech(self):
        """Called when user speech is detected."""
        self.last_speech_time = time.time()
        self.silence_prompted = False
        if self.state == CallState.HOLD:
            self.state = CallState.LISTENING

    def record_agent_speech(self, text: str):
        """Called when agent finishes speaking.

        Args:
            text: The text the agent just spoke
        """
        self.last_agent_speech_time = time.time()
        self.last_agent_text = text
        self.last_speech_time = time.time()  # Reset silence timer
        # Keep rolling buffer of recent TTS text for echo detection
        self.recent_tts_text.append(text.lower())
        if len(self.recent_tts_text) > 5:
            self.recent_tts_text.pop(0)

    # ───────────────────────────────────────────────────────────────────────
    # Hold Detection
    # ───────────────────────────────────────────────────────────────────────

    def check_hold_request(self, transcript: str) -> bool:
        """Check if user is asking to hold/wait.

        Args:
            transcript: User speech transcript

        Returns:
            True if hold request detected
        """
        text = transcript.lower().strip()
        for pattern in HOLD_PHRASES:
            if re.search(pattern, text):
                self.state = CallState.HOLD
                self._hold_entered = time.time()
                return True
        return False

    # ───────────────────────────────────────────────────────────────────────
    # Voicemail Detection (AI layer)
    # ───────────────────────────────────────────────────────────────────────

    def check_voicemail(self, transcript: str, call_elapsed: float) -> bool:
        """Check if transcript indicates voicemail. Only in first N seconds.

        Args:
            transcript: First transcript from the call
            call_elapsed: Seconds since call started

        Returns:
            True if voicemail detected
        """
        if not self.config.voicemail_detection_enabled:
            return False
        if call_elapsed > self.config.voicemail_detection_window:
            return False
        if self.voicemail_detected:
            return True

        text = transcript.lower()
        for pattern in VOICEMAIL_PHRASES:
            if re.search(pattern, text):
                self.voicemail_detected = True
                return True
        return False

    # ───────────────────────────────────────────────────────────────────────
    # Echo Suppression
    # ───────────────────────────────────────────────────────────────────────

    def is_echo(self, transcript: str) -> bool:
        """Check if STT transcript is echo of our own TTS output.

        Args:
            transcript: Transcript from STT

        Returns:
            True if likely an echo of our own speech
        """
        if (
            not self.config.echo_suppression_enabled
            or not self.recent_tts_text
        ):
            return False

        transcript_lower = transcript.lower().strip()
        if len(transcript_lower) < 5:
            return False

        for tts_text in self.recent_tts_text:
            similarity = _text_similarity(transcript_lower, tts_text)
            if similarity > self.config.echo_match_threshold:
                return True
        return False

    # ───────────────────────────────────────────────────────────────────────
    # Repeat Detection
    # ───────────────────────────────────────────────────────────────────────

    def check_repeat_request(self, transcript: str) -> bool:
        """Check if user is asking to repeat.

        Args:
            transcript: User speech transcript

        Returns:
            True if repeat request detected
        """
        text = transcript.lower().strip()
        for pattern in REPEAT_PHRASES:
            if re.search(pattern, text):
                return True
        return False

    # ───────────────────────────────────────────────────────────────────────
    # Interruption Tracking
    # ───────────────────────────────────────────────────────────────────────

    def record_interruption(self):
        """Track interruption for adaptive behavior."""
        self.interruption_count += 1
        self.consecutive_interruptions += 1

    def reset_consecutive_interruptions(self):
        """Reset when a turn completes without interruption."""
        self.consecutive_interruptions = 0

    def should_shorten_responses(self) -> bool:
        """Check if we should adapt to shorter responses.

        Returns:
            True if consecutive interruptions exceed threshold
        """
        return (
            self.config.adaptive_brevity
            and self.consecutive_interruptions
            >= self.config.adaptive_brevity_threshold
        )

    # ───────────────────────────────────────────────────────────────────────
    # Credit Protection
    # ───────────────────────────────────────────────────────────────────────

    def update_cost(self, cost_usd: float):
        """Update running cost total.

        Args:
            cost_usd: Cost increment in USD
        """
        self.total_cost += cost_usd

    def check_cost_limit(self) -> Optional[str]:
        """Check cost limit enforcement.

        Returns:
            'warn' to alert user, 'hangup' to end call, None for OK
        """
        if self.total_cost >= self.config.max_cost_usd:
            return "hangup"
        if (
            not self.cost_warned
            and self.total_cost
            >= self.config.max_cost_usd * self.config.cost_warning_threshold
        ):
            self.cost_warned = True
            return "warn"
        return None

    def check_duration_limit(self) -> Optional[str]:
        """Check call duration limit.

        Returns:
            'warn' to alert user, 'hangup' to end call, None for OK
        """
        elapsed = time.time() - self.call_start_time
        if elapsed >= self.config.max_call_duration:
            return "hangup"
        if elapsed >= self.config.max_call_duration - 30:  # 30 sec warning
            return "warn"
        return None

    # ───────────────────────────────────────────────────────────────────────
    # DTMF Handling
    # ───────────────────────────────────────────────────────────────────────

    def handle_dtmf(self, digit: str) -> Optional[str]:
        """Handle a DTMF digit, return action or None.

        Args:
            digit: DTMF digit pressed (0-9, *, #)

        Returns:
            Action string or None
        """
        if not self.config.dtmf_enabled:
            return None
        return self.config.dtmf_actions.get(digit)

    # ───────────────────────────────────────────────────────────────────────
    # Tone/Beep Detection for Voicemail
    # ───────────────────────────────────────────────────────────────────────

    @staticmethod
    def detect_beep(audio_pcm: bytes, sample_rate: int = 16000) -> bool:
        """Detect sustained tone (beep) in audio for voicemail detection.

        Looks for dominant frequency 400-2000Hz sustained for >300ms.

        Args:
            audio_pcm: PCM 16-bit audio data
            sample_rate: Sample rate in Hz

        Returns:
            True if beep/tone detected
        """
        try:
            import numpy as np
        except ImportError:
            # Fall back to False if numpy not available
            return False

        samples = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32)
        if len(samples) < sample_rate * 0.3:  # Need at least 300ms
            return False

        # Use FFT to find dominant frequency
        fft = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)

        # Look in 400-2000Hz range
        mask = (freqs >= 400) & (freqs <= 2000)
        if not np.any(mask):
            return False

        tone_energy = np.max(fft[mask])
        total_energy = np.mean(fft) + 1e-10

        # If tone energy is >10x the average, it's likely a beep
        return tone_energy / total_energy > 10.0


def _text_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity ratio.

    Args:
        a: First text
        b: Second text

    Returns:
        Similarity score from 0.0 to 1.0
    """
    if not a or not b:
        return 0.0
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    overlap = len(words_a & words_b)
    return overlap / max(len(words_a), len(words_b))
