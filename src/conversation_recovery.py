"""
WellHeard AI — Conversation Recovery System

Handles edge cases that cause calls to go silent or get stuck:
1. STT drops transcript (network glitch, audio quality issue)
2. LLM timeout (Groq occasionally errors or takes >5s)
3. TTS failure (WebSocket drop, empty audio)
4. Double-speak (AI interrupted, both responses try to play)
5. Transfer state stuck (agent hangs up during conference setup)

Architecture:
- Watchdog Timer: Detects prolonged silence and injects recovery prompts
- LLM Timeout Fallback: Use cached responses when LLM doesn't respond in time
- TTS Failure Fallback: Retry simplified text, use cache, or generic fallback
- Anti-Double-Speak Guard: Cancel older audio before playing new audio
- Transfer State Recovery: Timeout and graceful recovery from stuck transfer

Pre-synthesized recovery audio is generated during dial setup.
"""

import asyncio
import time
import structlog
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Dict
from enum import Enum

logger = structlog.get_logger()


class CallState(str, Enum):
    """Call state for selecting appropriate fallback responses."""
    CONFIRM_INTEREST = "confirm_interest"
    BANK_ACCOUNT = "bank_account"
    TRANSFER = "transfer"
    UNKNOWN = "unknown"


@dataclass
class RecoveryConfig:
    """Configuration for conversation recovery behavior."""
    # Watchdog timing
    watchdog_ai_timeout_s: float = 8.0      # If AI hasn't spoken for 8s
    watchdog_prospect_timeout_s: float = 5.0  # And prospect hasn't spoken for 5s
    second_recovery_delay_s: float = 10.0     # Time before second recovery attempt

    # LLM timeout fallback
    llm_timeout_s: float = 4.0  # LLM response timeout

    # TTS fallback
    tts_retry_count: int = 1  # Try once more with simplified text

    # Anti-double-speak
    audio_cancel_gap_ms: float = 100.0  # Gap between cancelling old and playing new

    # Transfer state
    transfer_timeout_s: float = 30.0  # Max time stuck in DIALING_AGENT

    # Pre-synthesized recovery phrases
    recovery_phrases: Dict[str, str] = field(default_factory=lambda: {
        "first_recovery": "Hey, are you still there?",
        "second_recovery": "I think we may have lost the connection. I'll try calling back. Take care!",
        "confirm_interest_fallback": "I'd love to help you with that — can you tell me a bit more?",
        "bank_account_fallback": "Quick question — do you have a checking or savings account?",
        "transfer_fallback": "Let me get Sarah on the line for you.",
        "tts_fallback": "Can you repeat that? I want to make sure I heard you correctly.",
    })


@dataclass
class RecoveryMetrics:
    """Track recovery events for monitoring."""
    watchdog_triggers: int = 0
    first_recovery_sent: int = 0
    second_recovery_sent: int = 0
    llm_timeouts: int = 0
    llm_fallbacks_used: int = 0
    tts_failures: int = 0
    tts_retries_succeeded: int = 0
    tts_cache_fallbacks: int = 0
    tts_generic_fallbacks: int = 0
    double_speak_prevented: int = 0
    transfer_timeouts: int = 0


class ConversationRecovery:
    """
    Production-grade conversation recovery system.

    Monitors for failure modes and injects pre-synthesized recovery prompts
    or uses intelligent fallbacks to keep calls alive and natural.

    Call these from the bridge:
      .start()                    — Begin monitoring
      .on_ai_response_start()     — AI started speaking
      .on_ai_response_end()       — AI finished speaking
      .on_prospect_speech()       — Prospect spoke
      .on_llm_timeout()           — LLM didn't respond in time
      .on_tts_failure()           — TTS synthesis failed
      .on_about_to_play_audio()   — Before playing audio (anti-double-speak)
      .on_transfer_state_change() — Transfer state changed
      .stop()                     — Stop monitoring

    Pre-synthesized audio must be loaded:
      .set_recovery_audio(phrase_key, audio_bytes)
    """

    def __init__(
        self,
        config: RecoveryConfig,
        on_recovery_speak: Callable[[str, Optional[bytes]], Awaitable[None]],
        call_id: str = "",
    ):
        self.config = config
        self._on_recovery_speak = on_recovery_speak
        self.call_id = call_id
        self.metrics = RecoveryMetrics()

        # State tracking
        self._active = False
        self._last_ai_response_time = 0.0
        self._last_prospect_speech_time = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._recovery_count = 0  # Number of recovery prompts sent this call
        self._current_call_state = CallState.UNKNOWN

        # Pre-synthesized recovery audio
        self._recovery_audio: Dict[str, Optional[bytes]] = {
            key: None for key in config.recovery_phrases.keys()
        }

        # Audio playback tracking (anti-double-speak)
        self._current_playing_audio_id: Optional[str] = None
        self._current_playing_start_time = 0.0
        self._audio_play_count = 0

        # Transfer state tracking
        self._transfer_state = "idle"  # idle, dialing_agent, connected
        self._transfer_state_start_time = 0.0
        self._transfer_watchdog_task: Optional[asyncio.Task] = None

    def start(self):
        """Start monitoring for failure modes."""
        self._active = True
        self._recovery_count = 0
        self._last_ai_response_time = time.time()
        self._last_prospect_speech_time = time.time()

        # Start watchdog
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        logger.info("conversation_recovery_started", call_id=self.call_id)

    def stop(self):
        """Stop monitoring."""
        self._active = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        if self._transfer_watchdog_task and not self._transfer_watchdog_task.done():
            self._transfer_watchdog_task.cancel()

        logger.info("conversation_recovery_stopped",
            call_id=self.call_id,
            metrics=self.metrics.__dict__)

    def set_recovery_audio(self, phrase_key: str, audio_bytes: Optional[bytes]):
        """Set pre-synthesized audio for a recovery phrase.

        Args:
            phrase_key: Key from config.recovery_phrases (e.g., 'first_recovery')
            audio_bytes: Raw PCM 16-bit 16kHz mono audio (or None if not available)
        """
        if phrase_key in self._recovery_audio:
            self._recovery_audio[phrase_key] = audio_bytes
            logger.debug("recovery_audio_loaded",
                call_id=self.call_id,
                phrase=phrase_key,
                bytes=len(audio_bytes) if audio_bytes else 0)

    def on_ai_response_start(self):
        """AI started generating/speaking a response."""
        self._last_ai_response_time = time.time()

    def on_ai_response_end(self):
        """AI finished speaking."""
        self._last_ai_response_time = time.time()

    def on_prospect_speech(self):
        """Prospect spoke (final transcript)."""
        self._last_prospect_speech_time = time.time()
        # Reset recovery count on substantive conversation
        self._recovery_count = 0

    def on_llm_timeout(self, current_call_state: CallState):
        """LLM didn't respond within timeout window.

        Should use fallback response appropriate to call state.
        """
        self.metrics.llm_timeouts += 1
        self._current_call_state = current_call_state

        logger.warning("llm_timeout_detected",
            call_id=self.call_id,
            state=current_call_state.value)

    def on_tts_failure(self, error: str = ""):
        """TTS synthesis failed."""
        self.metrics.tts_failures += 1
        logger.warning("tts_failure_detected",
            call_id=self.call_id,
            error=error)

    def on_about_to_play_audio(self, audio_id: Optional[str] = None) -> bool:
        """
        Check if we should play audio or if it would cause double-speak.

        Returns True if safe to play. If False, you should cancel the older
        audio (if tracking it) before playing the new audio.

        Args:
            audio_id: Unique ID for this audio (for tracking)

        Returns:
            True if safe to play, False if we're already playing (double-speak risk)
        """
        if not self._current_playing_audio_id:
            # No audio currently playing
            self._current_playing_audio_id = audio_id
            self._current_playing_start_time = time.time()
            return True

        # Audio is already playing — detect double-speak
        self.metrics.double_speak_prevented += 1
        logger.warning("double_speak_detected",
            call_id=self.call_id,
            current_audio=self._current_playing_audio_id,
            new_audio=audio_id)

        # Caller should cancel old audio and wait gap before playing new
        return False

    def on_audio_finished(self):
        """Audio finished playing."""
        self._current_playing_audio_id = None
        self._current_playing_start_time = 0.0

    def on_transfer_state_change(self, new_state: str):
        """Transfer state changed (idle → dialing_agent → connected).

        Args:
            new_state: One of 'idle', 'dialing_agent', 'connected'
        """
        old_state = self._transfer_state
        self._transfer_state = new_state
        self._transfer_state_start_time = time.time()

        if new_state == "dialing_agent":
            # Start transfer watchdog
            if self._transfer_watchdog_task and not self._transfer_watchdog_task.done():
                self._transfer_watchdog_task.cancel()
            self._transfer_watchdog_task = asyncio.create_task(
                self._transfer_watchdog_loop())

            logger.info("transfer_dialing_started", call_id=self.call_id)
        elif new_state == "connected":
            # Transfer succeeded — cancel watchdog
            if self._transfer_watchdog_task and not self._transfer_watchdog_task.done():
                self._transfer_watchdog_task.cancel()
            logger.info("transfer_connected", call_id=self.call_id)
        else:
            # Back to idle — cancel watchdog
            if self._transfer_watchdog_task and not self._transfer_watchdog_task.done():
                self._transfer_watchdog_task.cancel()

    def get_llm_fallback_text(self, call_state: CallState) -> str:
        """Get appropriate fallback response for LLM timeout.

        Args:
            call_state: Current call state (CONFIRM_INTEREST, BANK_ACCOUNT, TRANSFER)

        Returns:
            Pre-written fallback text appropriate to the state
        """
        self.metrics.llm_fallbacks_used += 1

        if call_state == CallState.CONFIRM_INTEREST:
            return self.config.recovery_phrases["confirm_interest_fallback"]
        elif call_state == CallState.BANK_ACCOUNT:
            return self.config.recovery_phrases["bank_account_fallback"]
        elif call_state == CallState.TRANSFER:
            return self.config.recovery_phrases["transfer_fallback"]
        else:
            return self.config.recovery_phrases["confirm_interest_fallback"]

    def get_tts_fallback_text(self) -> str:
        """Get generic fallback when TTS fails completely.

        Returns:
            Generic recovery text to synthesize
        """
        self.metrics.tts_generic_fallbacks += 1
        return self.config.recovery_phrases["tts_fallback"]

    def simplify_text_for_tts_retry(self, text: str, max_length: int = 60) -> str:
        """Simplify text for TTS retry (remove special chars, shorten).

        Args:
            text: Original text to simplify
            max_length: Maximum characters

        Returns:
            Simplified text
        """
        # Remove special characters except basic punctuation
        simplified = "".join(
            c for c in text
            if c.isalnum() or c in " .,!?-"
        )
        # Truncate to max length
        if len(simplified) > max_length:
            simplified = simplified[:max_length].rsplit(' ', 1)[0] + "."
        return simplified.strip()

    async def _watchdog_loop(self):
        """Monitor for silence timeout and inject recovery prompts.

        Triggers when:
        - AI hasn't spoken for >8 seconds AND
        - Prospect hasn't spoken for >5 seconds

        First recovery: "Hey, are you still there?"
        Second recovery (after 10 more sec): "I think we may have lost the connection..."
        Then end call gracefully.
        """
        try:
            while self._active:
                await asyncio.sleep(0.5)
                if not self._active:
                    break

                now = time.time()
                ai_silent = now - self._last_ai_response_time
                prospect_silent = now - self._last_prospect_speech_time

                # Trigger watchdog: both AI and prospect silent
                if (ai_silent >= self.config.watchdog_ai_timeout_s and
                    prospect_silent >= self.config.watchdog_prospect_timeout_s):

                    if self._recovery_count == 0:
                        # First recovery
                        self.metrics.watchdog_triggers += 1
                        self.metrics.first_recovery_sent += 1
                        await self._send_recovery_phrase(
                            "first_recovery",
                            delay_before_s=0.0)
                        self._recovery_count = 1
                        self._last_ai_response_time = time.time()

                    elif self._recovery_count == 1:
                        # Check if second recovery should fire (10s after first)
                        time_since_recovery = now - self._last_ai_response_time
                        if time_since_recovery >= self.config.second_recovery_delay_s:
                            self.metrics.second_recovery_sent += 1
                            await self._send_recovery_phrase(
                                "second_recovery",
                                delay_before_s=0.0)
                            self._recovery_count = 2
                            # After second recovery, don't try again —
                            # let silence manager or call end naturally
                            logger.info("conversation_recovery_max_attempts",
                                call_id=self.call_id)
                            break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("watchdog_loop_error",
                call_id=self.call_id, error=str(e))

    async def _transfer_watchdog_loop(self):
        """Monitor transfer state and timeout if stuck >30 seconds.

        If DIALING_AGENT for >30s without connecting, offer callback and exit.
        """
        try:
            while self._active and self._transfer_state == "dialing_agent":
                await asyncio.sleep(1.0)

                if self._transfer_state != "dialing_agent":
                    break

                elapsed = time.time() - self._transfer_state_start_time
                if elapsed >= self.config.transfer_timeout_s:
                    self.metrics.transfer_timeouts += 1
                    logger.warning("transfer_timeout",
                        call_id=self.call_id,
                        elapsed_s=round(elapsed, 1))

                    # Signal transfer timeout to caller
                    # Caller should cancel transfer and offer callback
                    await self._on_recovery_speak(
                        "transfer_timeout",
                        None)
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("transfer_watchdog_error",
                call_id=self.call_id, error=str(e))

    async def _send_recovery_phrase(self, phrase_key: str, delay_before_s: float = 0.0):
        """Send a pre-synthesized recovery phrase.

        Args:
            phrase_key: Key to recovery phrase (from config)
            delay_before_s: Optional delay before sending
        """
        if delay_before_s > 0:
            try:
                await asyncio.sleep(delay_before_s)
            except asyncio.CancelledError:
                return

        # Get pre-synthesized audio
        audio = self._recovery_audio.get(phrase_key)
        text = self.config.recovery_phrases.get(phrase_key, "")

        if not text:
            logger.warning("recovery_phrase_not_configured",
                call_id=self.call_id, phrase=phrase_key)
            return

        try:
            logger.info("recovery_phrase_sending",
                call_id=self.call_id, phrase=phrase_key,
                text=text, has_audio=bool(audio))
            await self._on_recovery_speak(text, audio)
        except Exception as e:
            logger.warning("recovery_phrase_failed",
                call_id=self.call_id, phrase=phrase_key, error=str(e))

    def get_metrics(self) -> dict:
        """Return recovery metrics for logging."""
        return self.metrics.__dict__
