"""
WellHeard AI — Fast Call Filter
Detects voicemail, dead air, and no-value calls within seconds
to minimize wasted spend. Target: <2s detection for obvious cases,
<4s for all cases. Beat Dasha.ai's 3-second average.

Detection Strategy (3 layers, runs in parallel):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Layer 1 — Twilio AMD (hardware-level, ~2-4s)
  Use async AMD with aggressive thresholds.
  MachineDetectionSpeechThreshold=1500ms (vs 2400 default)
  MachineDetectionSpeechEndThreshold=800ms (vs 1200 default)
  MachineDetectionSilenceTimeout=3000ms (vs 5000 default)

Layer 2 — Transcript analysis (STT-level, ~1-3s after first words)
  Deepgram Nova-3 streams partials in <300ms.
  First partial transcript → check against voicemail phrase patterns.
  This catches cases AMD misses (e.g. custom voicemail greetings).

Layer 3 — Audio signal analysis (raw audio, continuous)
  Monitor for: sustained silence (dead air), beep tones (voicemail),
  no speech energy at all. Runs on raw PCM from the start.

Decision logic:
  - ANY layer returns "voicemail" → hang up immediately
  - Dead air >3s after connect → single "Hello?" → 2s more silence → hang up
  - No speech energy detected within 5s of connect → hang up
  - Beep tone detected → hang up (do not leave message)

Cost savings model:
  Average call cost: ~$0.03/min
  Average voicemail/dead-air call length if undetected: 15-30s
  With fast filter: <3s → saves ~$0.01-0.015 per no-value call
  At 40% voicemail rate on aged leads: significant savings at scale
"""
import asyncio
import re
import time
import struct
import math
import structlog
from typing import Optional, Callable, Awaitable
from enum import Enum
from dataclasses import dataclass, field

logger = structlog.get_logger()


class CallFilterResult(str, Enum):
    """Result of fast call filtering."""
    HUMAN = "human"                     # Proceed with call
    VOICEMAIL_AMD = "voicemail_amd"     # Twilio AMD detected machine
    VOICEMAIL_TRANSCRIPT = "voicemail_transcript"  # STT caught VM phrases
    VOICEMAIL_BEEP = "voicemail_beep"   # Beep tone detected
    DEAD_AIR = "dead_air"              # No audio/speech at all
    SILENT_AFTER_HELLO = "silent_after_hello"  # Said hello, no response
    FAX = "fax"                        # Fax machine detected
    UNKNOWN = "unknown"                # Could not determine


@dataclass
class FilterMetrics:
    """Metrics for the fast filter on a single call."""
    call_id: str = ""
    result: CallFilterResult = CallFilterResult.UNKNOWN
    detection_time_ms: float = 0       # Time from connect to decision
    detection_layer: str = ""          # Which layer caught it
    amd_result: str = ""               # Raw Twilio AMD result
    first_transcript: str = ""         # First STT partial
    speech_detected: bool = False
    beep_detected: bool = False
    silence_duration_ms: float = 0
    cost_saved_estimate: float = 0     # Estimated cost saved by early hangup


# ── Voicemail Phrase Patterns ────────────────────────────────────────────
# Ordered by frequency in real aged-lead call data.
# These fire on the FIRST partial transcript from STT.

VM_PATTERNS_FAST = [
    # Highest confidence — these are almost always voicemail
    re.compile(r"leave\s+(a\s+)?message", re.I),
    re.compile(r"after\s+the\s+(tone|beep)", re.I),
    re.compile(r"not\s+available\s+(right\s+now|at\s+this\s+time|to\s+take)", re.I),
    re.compile(r"(please\s+)?record\s+(your|a)\s+message", re.I),
    re.compile(r"reached\s+the\s+(voicemail|voice\s*mail|mailbox)", re.I),
    re.compile(r"at\s+the\s+(tone|beep)", re.I),
    re.compile(r"press\s+\d+\s+(to\s+leave|for)", re.I),
]

VM_PATTERNS_MEDIUM = [
    # Medium confidence — need more context
    re.compile(r"(voice\s*mail|mail\s*box)", re.I),
    re.compile(r"currently\s+unable", re.I),
    re.compile(r"can'?t\s+(come|get)\s+to\s+the\s+phone", re.I),
    re.compile(r"(hi|hello|hey)[\s,]+you'?ve?\s+reached", re.I),
    re.compile(r"(i'?m\s+)?(not\s+here|away|out)", re.I),
    re.compile(r"call\s+(you\s+)?back", re.I),
    re.compile(r"your\s+call\s+(is\s+)?(important|being)", re.I),
]

# Carrier-generated voicemail (not personal greetings)
VM_PATTERNS_CARRIER = [
    re.compile(r"the\s+(person|number|party)\s+(you\s+)?(have\s+)?(called|dialed|reached)", re.I),
    re.compile(r"(subscriber|customer)\s+(you\s+)?(have\s+)?(called|are\s+calling|trying)", re.I),
    re.compile(r"(this\s+is\s+)?(the\s+)?google\s+(voice|fi)\s+subscriber", re.I),
    re.compile(r"(wireless\s+customer|temporary\s+number)", re.I),
]


class FastCallFilter:
    """
    Multi-layer call filter for instant voicemail/dead-air detection.

    Usage:
        filter = FastCallFilter()
        filter.start(call_id="abc123")

        # Feed it data as it arrives:
        filter.on_amd_result("machine_start")        # From Twilio webhook
        filter.on_transcript("Hi you've reached...")  # From STT partial
        filter.on_audio_chunk(pcm_bytes)              # From raw audio

        # Check result at any time:
        result = filter.get_result()
        if result != CallFilterResult.UNKNOWN:
            # Act on it (hang up, proceed, etc.)
    """

    def __init__(
        self,
        dead_air_threshold_ms: float = 3000,
        hello_wait_ms: float = 2000,
        max_detection_time_ms: float = 5000,
        beep_min_frequency: int = 400,
        beep_max_frequency: int = 2000,
        beep_energy_ratio: float = 8.0,
    ):
        self.dead_air_threshold_ms = dead_air_threshold_ms
        self.hello_wait_ms = hello_wait_ms
        self.max_detection_time_ms = max_detection_time_ms
        self.beep_min_freq = beep_min_frequency
        self.beep_max_freq = beep_max_frequency
        self.beep_energy_ratio = beep_energy_ratio

        self._metrics = FilterMetrics()
        self._result: CallFilterResult = CallFilterResult.UNKNOWN
        self._start_time: float = 0
        self._speech_detected: bool = False
        self._hello_sent: bool = False
        self._hello_sent_time: float = 0
        self._audio_energy_samples: list = []
        self._decided: bool = False

    def start(self, call_id: str = "") -> None:
        """Start the filter clock when call connects."""
        self._start_time = time.time()
        self._metrics.call_id = call_id
        self._decided = False
        logger.debug("fast_filter_started", call_id=call_id)

    @property
    def elapsed_ms(self) -> float:
        if not self._start_time:
            return 0
        return (time.time() - self._start_time) * 1000

    @property
    def is_decided(self) -> bool:
        return self._decided

    # ── Layer 1: Twilio AMD Result ──────────────────────────────────────

    def on_amd_result(self, answered_by: str) -> CallFilterResult:
        """
        Process Twilio's async AMD webhook result.

        answered_by values: "human", "machine_start", "machine_end_beep",
        "machine_end_silence", "machine_end_other", "fax", "unknown"
        """
        self._metrics.amd_result = answered_by

        if answered_by == "human":
            self._speech_detected = True
            # Don't decide yet — let other layers confirm
            return CallFilterResult.UNKNOWN

        if answered_by in ("machine_start", "machine_end_beep",
                           "machine_end_silence", "machine_end_other"):
            return self._decide(CallFilterResult.VOICEMAIL_AMD, "twilio_amd")

        if answered_by == "fax":
            return self._decide(CallFilterResult.FAX, "twilio_amd")

        return CallFilterResult.UNKNOWN

    # ── Layer 2: Transcript Analysis ────────────────────────────────────

    def on_transcript(self, text: str, is_final: bool = False) -> CallFilterResult:
        """
        Process STT transcript (partial or final).
        Called on every partial result from Deepgram.
        Returns result if voicemail detected, UNKNOWN otherwise.
        """
        if self._decided:
            return self._result

        if not text or len(text.strip()) < 3:
            return CallFilterResult.UNKNOWN

        self._speech_detected = True
        self._metrics.speech_detected = True
        if not self._metrics.first_transcript:
            self._metrics.first_transcript = text[:100]

        # Check fast patterns first (highest confidence)
        for pattern in VM_PATTERNS_FAST:
            if pattern.search(text):
                return self._decide(
                    CallFilterResult.VOICEMAIL_TRANSCRIPT,
                    "transcript_fast",
                )

        # Check carrier patterns
        for pattern in VM_PATTERNS_CARRIER:
            if pattern.search(text):
                return self._decide(
                    CallFilterResult.VOICEMAIL_TRANSCRIPT,
                    "transcript_carrier",
                )

        # Medium patterns only on longer text (need more context)
        if len(text) > 30:
            for pattern in VM_PATTERNS_MEDIUM:
                if pattern.search(text):
                    return self._decide(
                        CallFilterResult.VOICEMAIL_TRANSCRIPT,
                        "transcript_medium",
                    )

        # If we got real speech that doesn't match VM patterns → likely human
        if is_final and len(text.split()) <= 5:
            # Short human greeting like "Hello?" or "Yeah?"
            return self._decide(CallFilterResult.HUMAN, "transcript_human")

        return CallFilterResult.UNKNOWN

    # ── Layer 3: Audio Signal Analysis ──────────────────────────────────

    def on_audio_chunk(self, pcm_bytes: bytes, sample_rate: int = 16000) -> CallFilterResult:
        """
        Process raw PCM audio chunk for energy/beep analysis.
        Called continuously on incoming audio frames.
        """
        if self._decided:
            return self._result

        # Calculate RMS energy
        energy = self._calculate_rms(pcm_bytes)
        self._audio_energy_samples.append(energy)

        # Check for beep tone
        if len(pcm_bytes) >= sample_rate * 2 * 0.2:  # At least 200ms of audio
            if self._detect_beep_tone(pcm_bytes, sample_rate):
                self._metrics.beep_detected = True
                return self._decide(CallFilterResult.VOICEMAIL_BEEP, "audio_beep")

        return CallFilterResult.UNKNOWN

    def check_silence(self) -> CallFilterResult:
        """
        Check silence/dead-air status. Call this periodically (~every 500ms).
        Implements the silence → hello → silence → hangup flow.
        """
        if self._decided:
            return self._result

        elapsed = self.elapsed_ms

        # If no speech at all after max detection time → dead air
        if elapsed > self.max_detection_time_ms and not self._speech_detected:
            if self._hello_sent:
                # Already said hello, still nothing
                time_since_hello = (time.time() - self._hello_sent_time) * 1000
                if time_since_hello > self.hello_wait_ms:
                    self._metrics.silence_duration_ms = elapsed
                    return self._decide(
                        CallFilterResult.SILENT_AFTER_HELLO,
                        "silence_after_hello",
                    )
            else:
                # Haven't said hello yet — signal that we should
                if elapsed > self.dead_air_threshold_ms:
                    self._metrics.silence_duration_ms = elapsed
                    return self._decide(CallFilterResult.DEAD_AIR, "silence_dead_air")

        return CallFilterResult.UNKNOWN

    def mark_hello_sent(self) -> None:
        """Mark that we've sent a 'Hello?' prompt after silence."""
        self._hello_sent = True
        self._hello_sent_time = time.time()

    # ── Results ─────────────────────────────────────────────────────────

    def get_result(self) -> CallFilterResult:
        return self._result

    def get_metrics(self) -> FilterMetrics:
        return self._metrics

    # ── Internal Helpers ────────────────────────────────────────────────

    def _decide(self, result: CallFilterResult, layer: str) -> CallFilterResult:
        """Lock in a decision."""
        if self._decided:
            return self._result

        self._decided = True
        self._result = result
        self._metrics.result = result
        self._metrics.detection_layer = layer
        self._metrics.detection_time_ms = self.elapsed_ms

        # Estimate cost saved (assuming ~$0.03/min, avg VM call 20s if undetected)
        if result != CallFilterResult.HUMAN:
            saved_seconds = max(20 - (self.elapsed_ms / 1000), 0)
            self._metrics.cost_saved_estimate = round((saved_seconds / 60) * 0.03, 4)

        logger.info("fast_filter_decided",
            result=result.value,
            layer=layer,
            detection_ms=round(self.elapsed_ms, 0),
            call_id=self._metrics.call_id,
        )
        return result

    @staticmethod
    def _calculate_rms(pcm_bytes: bytes) -> float:
        """Calculate RMS energy of PCM16 audio."""
        if len(pcm_bytes) < 4:
            return 0.0
        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f"<{n_samples}h", pcm_bytes[:n_samples * 2])
        if not samples:
            return 0.0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        return rms / 32768.0  # Normalize to 0-1

    @staticmethod
    def _detect_beep_tone(pcm_bytes: bytes, sample_rate: int = 16000,
                           min_freq: int = 400, max_freq: int = 2000,
                           energy_ratio: float = 8.0) -> bool:
        """
        Detect sustained tone (beep) in PCM audio using simple FFT.
        Looks for dominant frequency in the beep range with high energy ratio.
        """
        try:
            import numpy as np
        except ImportError:
            return False

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < sample_rate * 0.15:  # Need at least 150ms
            return False

        # Compute FFT
        fft_vals = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)

        # Filter to beep frequency range
        mask = (freqs >= min_freq) & (freqs <= max_freq)
        if not np.any(mask):
            return False

        peak_energy = np.max(fft_vals[mask])
        mean_energy = np.mean(fft_vals) + 1e-10

        return (peak_energy / mean_energy) > energy_ratio


# ── Twilio AMD Configuration ────────────────────────────────────────────

def get_twilio_amd_params(mode: str = "aggressive") -> dict:
    """
    Get Twilio AMD parameters optimized for our use case.

    Modes:
    - "aggressive": Fastest detection, higher false-positive risk.
      Target: <2s for obvious VM, <4s for all.
    - "balanced": Good speed with reasonable accuracy.
    - "accurate": Prioritize accuracy over speed.

    Since our STT layer catches most voicemails anyway,
    we use aggressive Twilio AMD as a fast first-pass.
    """
    configs = {
        "aggressive": {
            "MachineDetection": "Enable",
            "AsyncAmd": "true",
            "MachineDetectionTimeout": 8,           # 8s max (vs 30s default)
            "MachineDetectionSpeechThreshold": 1500,  # 1.5s (vs 2.4s default)
            "MachineDetectionSpeechEndThreshold": 800, # 0.8s (vs 1.2s default)
            "MachineDetectionSilenceTimeout": 3000,   # 3s (vs 5s default)
        },
        "balanced": {
            "MachineDetection": "Enable",
            "AsyncAmd": "true",
            "MachineDetectionTimeout": 15,
            "MachineDetectionSpeechThreshold": 1800,
            "MachineDetectionSpeechEndThreshold": 1000,
            "MachineDetectionSilenceTimeout": 4000,
        },
        "accurate": {
            "MachineDetection": "DetectMessageEnd",
            "AsyncAmd": "true",
            "MachineDetectionTimeout": 30,
            "MachineDetectionSpeechThreshold": 2400,
            "MachineDetectionSpeechEndThreshold": 1200,
            "MachineDetectionSilenceTimeout": 5000,
        },
    }
    return configs.get(mode, configs["aggressive"])


# ── Silent Call Handler ──────────────────────────────────────────────────

class SilentCallHandler:
    """
    Handles silent/dead-air calls with FCC-compliant behavior.

    FCC Rules:
    - Must connect to agent within 2 seconds of consumer greeting
    - Max 3% abandoned call rate
    - Must ring at least 15 seconds / 4 rings before disconnect

    Our approach:
    - Start greeting immediately on connect (don't wait for AMD)
    - If no response to greeting after 3s → say "Hello?" once
    - If no response to "Hello?" after 2s → hang up
    - Total: 5s max on a dead-air call
    - Log as "no_answer" for disposition tracking
    """

    def __init__(self, greeting_wait_ms: float = 3000, hello_wait_ms: float = 2000):
        self.greeting_wait_ms = greeting_wait_ms
        self.hello_wait_ms = hello_wait_ms
        self._greeting_sent_time: float = 0
        self._hello_sent_time: float = 0
        self._state: str = "waiting_for_connect"  # → greeting_sent → hello_sent → decided

    def on_greeting_sent(self) -> None:
        """Called after the SDR greeting is played."""
        self._greeting_sent_time = time.time()
        self._state = "greeting_sent"

    def on_hello_sent(self) -> None:
        """Called after the follow-up 'Hello?' is played."""
        self._hello_sent_time = time.time()
        self._state = "hello_sent"

    def on_speech_detected(self) -> None:
        """Called when any speech is detected from the prospect."""
        self._state = "human_detected"

    def check(self) -> Optional[str]:
        """
        Check if we should act. Returns:
        - "send_hello": Say "Hello?" prompt
        - "hang_up": End the call (dead air)
        - None: Keep waiting
        """
        if self._state == "human_detected":
            return None

        now = time.time()

        if self._state == "greeting_sent":
            elapsed = (now - self._greeting_sent_time) * 1000
            if elapsed > self.greeting_wait_ms:
                return "send_hello"

        elif self._state == "hello_sent":
            elapsed = (now - self._hello_sent_time) * 1000
            if elapsed > self.hello_wait_ms:
                return "hang_up"

        return None

    @property
    def is_dead(self) -> bool:
        return self._state == "hello_sent" and self.check() == "hang_up"
