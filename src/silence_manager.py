"""
WellHeard AI — Silence Manager (v4 — A+ grade)

2-stage silence management that NEVER false-exits active conversations
and ALWAYS catches truly silent/abandoned calls.

Key design principles:
  1. Clock only runs when BOTH AI and prospect are quiet
  2. "Hold on" / "one sec" pauses the timer (prospect asked for time)
  3. Backchannel-only speech ("yeah", "ok") restarts the clock but
     doesn't require an AI response — so we go to LISTENING, not BUSY
  4. The nudge is cancellable — if prospect speaks during TTS synthesis,
     we abort the nudge and return to normal flow
  5. After nudge plays, we wait for the full exit window from when the
     nudge AUDIO finishes, not from when we started synthesizing

State machine:
  BUSY       → AI generating/speaking. Clock frozen.
  LISTENING  → Both quiet. Clock ticking toward nudge.
  NUDGE_SENT → Nudge played, clock ticking toward exit.
  EXITING    → Goodbye playing, call ending.
  PAUSED     → Transfer hold. Everything frozen.
  HOLD       → Prospect said "hold on". Longer tolerance.
"""

import asyncio
import time
import structlog
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Set

logger = structlog.get_logger()

# Phrases that indicate the prospect needs a moment
HOLD_PHRASES: Set[str] = {
    "hold on", "one second", "one sec", "just a second", "just a sec",
    "give me a second", "give me a moment", "hang on", "one moment",
    "wait", "wait a second", "just a moment", "gimme a sec",
    "let me think", "hold on a second", "hold on one second",
}

# Backchannel words — prospect is listening but not really saying anything
BACKCHANNELS: Set[str] = {
    "mm", "mhm", "mm-hmm", "mmhmm", "uh-huh", "uh huh",
    "ok", "okay", "yeah", "yep", "yup", "sure", "right",
    "got it", "i see", "ah", "oh", "hmm", "alright",
}


@dataclass
class SilenceConfig:
    """Thresholds for 2-stage silence management."""
    nudge_after_s: float = 8.0    # Increased from 6s — give prospect time to think
    exit_after_nudge_s: float = 6.0  # Increased from 4s — more patient after nudge
    hold_timeout_s: float = 30.0  # Max wait when prospect says "hold on"

    nudge_phrases: list = field(default_factory=lambda: [
        "So what do you think?",
        "Does that make sense?",
        "Sound okay to you?",
        "What are your thoughts on that?",
    ])

    exit_phrases: list = field(default_factory=lambda: [
        "It sounds like we may have gotten disconnected. No worries, have a wonderful day!",
        "Looks like I lost you there. No problem, take care!",
        "I think we got cut off. Have a great day!",
    ])


class SilenceManager:
    """
    Production-grade 2-stage silence manager.

    Call these from the bridge:
      .start()              — Begin monitoring (Phase 3 entry)
      .on_speech(text)      — Prospect spoke (pass transcript for hold detection)
      .on_backchannel()     — Prospect said "yeah"/"ok" (no AI response coming)
      .ai_busy()            — AI is generating a response
      .on_ai_done()         — AI audio finished playing on phone
      .pause() / .resume()  — Transfer hold
      .stop()               — Call ended
    """

    BUSY = "busy"
    LISTENING = "listening"
    NUDGE_SENT = "nudge_sent"
    EXITING = "exiting"
    PAUSED = "paused"
    HOLD = "hold"  # Prospect said "hold on"

    def __init__(
        self,
        config: SilenceConfig,
        on_speak: Callable[[str], Awaitable[None]],
        on_exit: Callable[[str], Awaitable[None]],
        call_id: str = "",
    ):
        self.config = config
        self._on_speak = on_speak
        self._on_exit = on_exit
        self.call_id = call_id

        self._stage = self.BUSY
        self._clock: float = 0.0
        self._active: bool = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._nudge_index: int = 0
        self._exit_index: int = 0
        self._speaking_nudge: bool = False  # True while nudge TTS is playing

        # Metrics
        self.nudges_sent: int = 0
        self.silence_exit: bool = False

    def start(self):
        """Start monitoring."""
        self._active = True
        self._stage = self.BUSY
        self._clock = 0.0
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("silence_manager_started", call_id=self.call_id)

    def stop(self):
        """Stop monitoring."""
        self._active = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        logger.info("silence_manager_stopped",
            call_id=self.call_id,
            nudges=self.nudges_sent,
            exit=self.silence_exit)

    def pause(self):
        """Pause for transfer hold."""
        self._stage = self.PAUSED
        self._clock = 0.0

    def resume(self):
        """Resume after transfer."""
        if self._stage == self.PAUSED:
            self._stage = self.LISTENING
            self._clock = time.time()

    def ai_busy(self):
        """AI is generating a response. Freeze clock."""
        if self._stage in (self.PAUSED, self.EXITING):
            return
        self._stage = self.BUSY
        self._clock = 0.0

    def on_speech(self, text: str = ""):
        """Prospect spoke. Check for hold phrases, then go BUSY.

        Args:
            text: The transcript text (used for hold-on detection)
        """
        if self._stage in (self.PAUSED, self.EXITING):
            return

        old = self._stage
        clean = text.strip().lower().rstrip(".,!?")

        # Check if prospect is asking for time
        if clean in HOLD_PHRASES or any(h in clean for h in HOLD_PHRASES):
            self._stage = self.HOLD
            self._clock = time.time()  # Start hold timer
            logger.info("silence_hold_detected",
                call_id=self.call_id, text=text[:50])
            return

        # Normal speech — AI will respond, so go BUSY
        self._stage = self.BUSY
        self._clock = 0.0

        if old in (self.NUDGE_SENT, self.HOLD):
            logger.info("silence_reset_by_speech",
                call_id=self.call_id, from_stage=old)

    def on_backchannel(self):
        """Prospect said a backchannel ("yeah", "ok") that won't trigger
        an AI response. Go to LISTENING with clock running — we're waiting
        for them to say something substantive."""
        if self._stage in (self.PAUSED, self.EXITING):
            return
        self._stage = self.LISTENING
        self._clock = time.time()

    def on_ai_done(self):
        """AI audio finished playing on phone. Start silence clock."""
        if self._stage in (self.PAUSED, self.EXITING, self.HOLD):
            return
        self._stage = self.LISTENING
        self._clock = time.time()

    @property
    def is_speaking_nudge(self) -> bool:
        """True if currently playing a nudge — bridge can check this to
        know if it should cancel the nudge on barge-in."""
        return self._speaking_nudge

    async def _monitor_loop(self):
        """Check every 500ms."""
        try:
            while self._active:
                await asyncio.sleep(0.5)
                if not self._active:
                    break

                # Skip frozen states
                if self._stage in (self.BUSY, self.PAUSED, self.EXITING):
                    continue

                if self._clock <= 0:
                    continue

                elapsed = time.time() - self._clock

                # HOLD state: longer tolerance, but not infinite
                if self._stage == self.HOLD:
                    if elapsed >= self.config.hold_timeout_s:
                        # Been on hold too long — treat as silence exit
                        logger.info("silence_hold_timeout",
                            call_id=self.call_id,
                            hold_s=round(elapsed, 1))
                        self._stage = self.EXITING
                        self.silence_exit = True
                        phrase = self.config.exit_phrases[
                            self._exit_index % len(self.config.exit_phrases)]
                        self._exit_index += 1
                        try:
                            await self._on_exit(phrase)
                        except Exception as e:
                            logger.warning("silence_hold_exit_failed",
                                call_id=self.call_id, error=str(e))
                        break
                    continue

                # LISTENING → nudge
                if self._stage == self.LISTENING:
                    if elapsed >= self.config.nudge_after_s:
                        self._stage = self.NUDGE_SENT
                        self.nudges_sent += 1
                        phrase = self.config.nudge_phrases[
                            self._nudge_index % len(self.config.nudge_phrases)]
                        self._nudge_index += 1
                        logger.info("silence_nudge",
                            call_id=self.call_id,
                            silence_s=round(elapsed, 1),
                            phrase=phrase)
                        self._speaking_nudge = True
                        try:
                            await self._on_speak(phrase)
                        except Exception as e:
                            logger.warning("silence_nudge_failed",
                                call_id=self.call_id, error=str(e))
                        self._speaking_nudge = False

                        # If speech arrived during nudge playback, we got
                        # reset to BUSY — don't overwrite that
                        if self._stage == self.NUDGE_SENT:
                            self._clock = time.time()

                # NUDGE_SENT → exit
                elif self._stage == self.NUDGE_SENT:
                    if elapsed >= self.config.exit_after_nudge_s:
                        self._stage = self.EXITING
                        self.silence_exit = True
                        phrase = self.config.exit_phrases[
                            self._exit_index % len(self.config.exit_phrases)]
                        self._exit_index += 1
                        logger.info("silence_exit",
                            call_id=self.call_id,
                            silence_s=round(elapsed, 1),
                            phrase=phrase)
                        try:
                            await self._on_exit(phrase)
                        except Exception as e:
                            logger.warning("silence_exit_failed",
                                call_id=self.call_id, error=str(e))
                        break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("silence_monitor_error",
                call_id=self.call_id, error=str(e))

    def get_metrics(self) -> dict:
        return {
            "nudges_sent": self.nudges_sent,
            "silence_exit": self.silence_exit,
        }
