"""
WellHeard AI — Call Bridge (v2 — 3-Phase Architecture)

3-PHASE CALL FLOW:
  Phase 1 — DETECT (0-3s): Play greeting, classify answer (human/voicemail/IVR/silence)
  Phase 2 — PITCH (pre-baked): Deliver the sales pitch as one seamless pre-generated audio.
            Zero latency, perfect intonation. No LLM involved.
  Phase 3 — CONVERSE (live): Fast STT→LLM→TTS turns for qualification + transfer.

The pitch is pre-synthesized during dial time along with the greeting, so both
play instantly when the prospect answers. The LLM only kicks in for Phase 3
when the prospect actually responds to the pitch.

Target latency benchmarks (best-in-class):
  - Greeting start: <100ms after answer  (ACHIEVED via pre-synthesis)
  - Pitch start: <200ms after human detected
  - Turn-around (Phase 3): <800ms (STT 3ms + LLM 200ms + TTS 133ms + silence 500ms)
"""

import asyncio
import time
import struct
import structlog
from typing import Optional, AsyncIterator
from .pipelines.orchestrator import VoicePipelineOrchestrator, AgentConfig
from .call_state import CallStateTracker, ScriptStep
from .silence_manager import SilenceManager, SilenceConfig, BACKCHANNELS, HOLD_PHRASES
from .conversation_recovery import ConversationRecovery, RecoveryConfig, CallState
from .response_cache import SemanticResponseCache
from .sentiment_analyzer import SentimentAnalyzer

logger = structlog.get_logger()


class BackchannelManager:
    """
    Manages AI backchanneling — short affirmative sounds during prospect pauses.

    Research shows backchanneling (mm-hmm, right, yeah, okay) during natural pauses
    increases perceived humanness by ~40%. This manager injects brief backchannel
    responses at strategic moments during prospect speech to signal active listening.

    Architecture:
      1. Pre-synthesized backchannel audio (4 types) stored during dial time
      2. Detects mid-speech pauses (200-400ms) via silence detection
      3. Applies probabilistic selection and cooldown logic
      4. Injects audio without triggering turn-end detection or VAD reset

    CRITICAL: Backchannel audio must NOT reset the silence manager or VAD.
    It's treated as a "micro-response" that doesn't count as AI speech.
    """

    def __init__(self, call_id: str = ""):
        self.call_id = call_id

        # Pre-synthesized backchannel audio (populated during dial setup)
        # Maps backchannel type to raw PCM bytes (16kHz, 16-bit mono)
        self._backchannel_audio: dict[str, Optional[bytes]] = {
            "mmhmm": None,   # "Mm-hmm" — 60% (most common)
            "right": None,   # "Right" — 20%
            "yeah": None,    # "Yeah" — 15%
            "okay": None,    # "Okay" — 5%
        }

        # Selection probabilities (must sum to 1.0)
        self._probabilities = {
            "mmhmm": 0.60,
            "right": 0.20,
            "yeah": 0.15,
            "okay": 0.05,
        }

        # Cooldown and timing constraints
        self._last_backchannel_time: float = 0.0  # Timestamp of last backchannel
        self._last_ai_speech_time: float = 0.0    # Timestamp when AI last finished speaking
        self._min_cooldown_s = 8.0                # Min seconds between backchannels (8-15s range)
        self._max_cooldown_s = 15.0
        self._ai_speech_grace_period_s = 3.0     # Don't backchannel within 3s of AI finishing
        self._prospect_speech_grace_period_s = 2.0  # Let prospect speak first 2s before backchanneling
        self._pause_duration_min_ms = 200        # Min pause to trigger (200-400ms range)
        self._pause_duration_max_ms = 400

        # State tracking
        self._prospect_speaking_start: Optional[float] = None
        self._prospect_last_audio_time: Optional[float] = None
        self._current_pause_duration_ms = 0.0
        self._enabled = False

        # Metrics
        self.backchannels_injected = 0
        self.backchannel_suppressed = 0

    def set_audio(self, backchannel_type: str, audio_bytes: Optional[bytes]):
        """Set pre-synthesized audio for a backchannel type.

        Args:
            backchannel_type: One of "mmhmm", "right", "yeah", "okay"
            audio_bytes: Raw PCM 16-bit 16kHz mono audio (or None if not available)
        """
        if backchannel_type in self._backchannel_audio:
            self._backchannel_audio[backchannel_type] = audio_bytes
            logger.debug("backchannel_audio_loaded",
                call_id=self.call_id,
                type=backchannel_type,
                bytes=len(audio_bytes) if audio_bytes else 0)

    def enable(self):
        """Enable backchanneling."""
        self._enabled = True
        logger.info("backchannel_enabled", call_id=self.call_id)

    def disable(self):
        """Disable backchanneling."""
        self._enabled = False
        logger.info("backchannel_disabled", call_id=self.call_id)

    def on_prospect_speech_started(self):
        """Called when prospect starts speaking (speech_started VAD event)."""
        if not self._enabled:
            return
        if self._prospect_speaking_start is None:
            self._prospect_speaking_start = time.time()
            self._prospect_last_audio_time = time.time()
            logger.debug("backchannel_prospect_speech_started",
                call_id=self.call_id)

    def on_prospect_audio_chunk(self):
        """Called when prospect audio is received (has energy above threshold).

        This resets the pause detection timer — we use it to detect natural
        pauses within speech (200-400ms) vs. end-of-turn (700ms+).
        """
        if not self._enabled or self._prospect_speaking_start is None:
            return
        self._prospect_last_audio_time = time.time()
        self._current_pause_duration_ms = 0.0

    def on_prospect_silence_chunk(self):
        """Called when silence chunk received (no energy above threshold).

        This increments pause duration. If pause enters 200-400ms range,
        we may inject a backchannel (subject to cooldown/grace period checks).
        """
        if not self._enabled or self._prospect_speaking_start is None:
            return

        if self._prospect_last_audio_time is None:
            return

        # Calculate pause duration since last audio
        pause_ms = (time.time() - self._prospect_last_audio_time) * 1000
        self._current_pause_duration_ms = pause_ms

        # Only backchannel in the 200-400ms "natural pause" window
        if self._pause_duration_min_ms <= pause_ms <= self._pause_duration_max_ms:
            # Check all constraints before injecting
            if self._should_backchannel():
                selected = self._select_backchannel()
                if selected:
                    logger.info("backchannel_injectable_pause_detected",
                        call_id=self.call_id,
                        pause_ms=round(pause_ms, 1),
                        type=selected)
                    return selected  # Return for caller to inject
        elif pause_ms > self._pause_duration_max_ms:
            # Pause too long — prospect is done speaking
            self.on_prospect_speech_ended()

        return None

    def on_prospect_speech_ended(self):
        """Called when prospect finishes speaking (speech_final or utterance_end).

        Resets the speaking window for next turn.
        """
        if not self._enabled:
            return
        self._prospect_speaking_start = None
        self._prospect_last_audio_time = None
        self._current_pause_duration_ms = 0.0
        logger.debug("backchannel_prospect_speech_ended", call_id=self.call_id)

    def on_ai_speech_started(self):
        """Called when AI starts speaking (begins TTS output)."""
        if not self._enabled:
            return
        # Don't reset timing — AI speaking doesn't affect backchannel logic

    def on_ai_speech_ended(self):
        """Called when AI finishes speaking.

        Records timestamp for grace period — don't backchannel too soon after AI speaks.
        """
        if not self._enabled:
            return
        self._last_ai_speech_time = time.time()
        logger.debug("backchannel_ai_speech_ended", call_id=self.call_id)

    def on_phase_changed(self, phase: str):
        """Called when call phase changes (detect→pitch→converse→ended).

        Backchanneling only makes sense during Phase 3 (converse).
        """
        if phase == "converse":
            self.enable()
        else:
            self.disable()
            self.on_prospect_speech_ended()

    def _should_backchannel(self) -> bool:
        """Check all constraints before approving backchannel injection.

        Returns True only if ALL conditions are met:
          - At least 3s since AI last spoke (grace period)
          - At least 2s since prospect started speaking (let them get rolling)
          - At least 8s since last backchannel (cooldown)
          - Backchannel audio is available
        """
        now = time.time()

        # Check AI grace period: don't backchannel within 3s of AI finishing speech
        if self._last_ai_speech_time > 0:
            time_since_ai = now - self._last_ai_speech_time
            if time_since_ai < self._ai_speech_grace_period_s:
                self.backchannel_suppressed += 1
                logger.debug("backchannel_suppressed_ai_grace_period",
                    call_id=self.call_id,
                    time_since_ai=round(time_since_ai, 2))
                return False

        # Check prospect grace period: don't backchannel in first 2s of speech
        if self._prospect_speaking_start is not None:
            time_since_speech_start = now - self._prospect_speaking_start
            if time_since_speech_start < self._prospect_speech_grace_period_s:
                self.backchannel_suppressed += 1
                logger.debug("backchannel_suppressed_prospect_grace_period",
                    call_id=self.call_id,
                    time_since_start=round(time_since_speech_start, 2))
                return False

        # Check cooldown: at least 8s since last backchannel
        if self._last_backchannel_time > 0:
            time_since_last = now - self._last_backchannel_time
            if time_since_last < self._min_cooldown_s:
                self.backchannel_suppressed += 1
                logger.debug("backchannel_suppressed_cooldown",
                    call_id=self.call_id,
                    time_since_last=round(time_since_last, 2),
                    min_cooldown_s=self._min_cooldown_s)
                return False

        # Check that we have audio available
        if not any(self._backchannel_audio.values()):
            logger.debug("backchannel_suppressed_no_audio",
                call_id=self.call_id)
            return False

        return True

    def _select_backchannel(self) -> Optional[str]:
        """Probabilistically select a backchannel type.

        Uses weighted random selection: mmhmm (60%), right (20%), yeah (15%), okay (5%).

        Returns:
            Backchannel type (mmhmm/right/yeah/okay) or None if no audio available
        """
        import random

        types = list(self._probabilities.keys())
        weights = [self._probabilities[t] for t in types]
        selected = random.choices(types, weights=weights, k=1)[0]

        # Verify audio is available
        if self._backchannel_audio.get(selected) is None:
            logger.debug("backchannel_selected_no_audio",
                call_id=self.call_id, type=selected)
            return None

        self._last_backchannel_time = time.time()
        self.backchannels_injected += 1
        logger.info("backchannel_selected",
            call_id=self.call_id,
            type=selected,
            total_injected=self.backchannels_injected)
        return selected

    def get_audio(self, backchannel_type: str) -> Optional[bytes]:
        """Retrieve the pre-synthesized audio for a backchannel type.

        Args:
            backchannel_type: One of "mmhmm", "right", "yeah", "okay"

        Returns:
            Raw PCM bytes or None if not available
        """
        return self._backchannel_audio.get(backchannel_type)

    def get_metrics(self) -> dict:
        """Return backchannel metrics for logging."""
        return {
            "backchannels_injected": self.backchannels_injected,
            "backchannel_suppressed": self.backchannel_suppressed,
            "last_backchannel_time": self._last_backchannel_time,
            "enabled": self._enabled,
        }


class CallBridge:
    """
    Bidirectional bridge: Twilio Media Stream ↔ Voice Pipeline Orchestrator.

    3-phase call architecture for maximum speed and conversion.
    """

    # Default greeting — short, gets a quick response
    DEFAULT_GREETING = "Hi, can you hear me ok?"

    # The pitch text for Phase 2 — delivered as pre-baked audio
    # This is the identical pitch every call. Pre-generated = perfect quality.
    DEFAULT_PITCH = ""

    # Transfer trigger phrases — if the LLM response contains any of these,
    # initiate the warm transfer to a licensed agent.
    TRANSFER_TRIGGERS = [
        "licensed agent standing by",
        "licensed agent on standby",
        "have them jump on the call",
        "jump on the call",
        "transfer you now",
        "connecting you to",
        "hand you over",
        "agent standing by",
    ]

    def __init__(
        self,
        orchestrator: VoicePipelineOrchestrator,
        agent_config: AgentConfig,
        call_id: str = "",
    ):
        self.orchestrator = orchestrator
        self.agent_config = agent_config
        self.call_id = call_id

        # Twilio references (set by server.py after creation)
        self.twilio_call_sid: str = ""
        self.twilio_client = None  # Twilio REST client
        self.twilio_telephony = None  # TwilioTelephony instance (for clear messages)
        self.webhook_base_url: str = ""
        self.prospect_name: str = ""  # Prospect's first name — used in agent whisper

        # Transfer manager (initialized lazily)
        self._transfer_manager = None
        self._transfer_initiated = False

        # Input: Twilio audio → orchestrator
        self._input_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=200)

        # Output: orchestrator response audio → Twilio
        # Must be large enough for pre-baked pitch (~700 chunks for 14s audio)
        self._output_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=1500)

        # State
        self._active = False
        self._turn_task: Optional[asyncio.Task] = None
        self._call_phase = "init"  # init → detect → pitch → converse → ended

        # Pre-synthesized audio (raw PCM bytes, generated during dial)
        self._greeting_audio: Optional[bytes] = None
        self._pitch_audio: Optional[bytes] = None

        # Structured call state tracker — prevents repetition and tracks progress
        self._call_state = CallStateTracker(call_id=call_id)

        # Multi-stage silence manager v4 — hold-on detection, backchannel handling
        # Escalates: nudge (6s) → exit (+4s). Hold-on pauses timer (20s max)
        self._silence_manager = SilenceManager(
            config=SilenceConfig(),
            on_speak=self._silence_speak,
            on_exit=self._silence_exit,
            call_id=call_id,
        )
        self._silence_clock_task: Optional[asyncio.Task] = None

        # Backchannel manager — injects "mm-hmm", "right", etc. during prospect pauses
        # Research: backchanneling increases perceived humanness by ~40%
        self._backchannel_manager = BackchannelManager(call_id=call_id)

        # Conversation recovery system — handles edge cases (STT drops, LLM timeout, TTS failure)
        # Pre-synthesized recovery audio is generated during dial time
        self._recovery = ConversationRecovery(
            config=RecoveryConfig(),
            on_recovery_speak=self._recovery_speak,
            call_id=call_id,
        )

        # Debug counters
        self._audio_chunks_received = 0

        # VAD parameters — tuned for phone audio
        self._silence_threshold_ms = 500  # 500ms silence = end of turn (was 700)
        self._min_audio_for_turn = 4800   # ~150ms at 16kHz 16-bit mono
        self._speech_energy_threshold = 300  # RMS threshold for speech vs silence

        # Echo suppression
        self._ai_speaking = False
        self._ai_speech_ended_at = 0.0
        self._ai_speaking_started_at = 0.0  # When TTS first started outputting audio
        self._echo_cooldown_ms = 400  # Base cooldown after AI stops (reduced from 800)
        self._last_output_bytes = 0    # Track output audio size for dynamic cooldown
        self._twilio_playback_offset_ms = 150  # Queue-to-playback delay estimate

        # Interruption management
        self._barge_in_energy_frames = 0  # Consecutive frames above threshold during AI speech
        self._barge_in_frame_threshold = 3  # Need 3 consecutive frames (~60ms) to confirm barge-in
        self._barge_in_grace_ms = 500  # Don't allow barge-in for 500ms after TTS starts (was 1200)
        self._pitch_barge_in_grace_ms = 3000  # Allow barge-in after 3s of pitch playing
        self._pitch_min_interrupt_ms = 800  # Prospect must speak 800ms+ to interrupt pitch (ignore "ok", "got it")
        self._pitch_interrupted = False  # Flag to skip to Phase 3 immediately
        self._pending_barge_in = False  # Speech detected, waiting for transcript confirmation

        # Silence compression: reduces pause duration at commas/periods (pre-baked audio)
        self._silence_compression = 0.40  # Compress pauses to 40% of original — natural but crisp
        self._silence_threshold_rms = 50  # RMS below this = silence
        self._min_silence_ms = 120  # Only compress pauses longer than 120ms

        # Turn accumulation: wait for user to finish speaking before triggering LLM
        # 250ms was too short — caused "Well, okay..." + "I guess so" to fire as two turns.
        # 500ms gives natural speakers time to pause mid-thought without feeling sluggish.
        self._turn_accumulation_ms = 500  # Wait 500ms after speech_final for more speech
        self._accumulated_timer_task: Optional[asyncio.Task] = None

        # Interruption context: track what was being said when interrupted
        # so the LLM knows what was already spoken and can continue naturally
        self._current_response_text: str = ""  # Full LLM response being TTS'd
        self._tts_start_time: float = 0.0  # When TTS audio started streaming
        self._tts_words_per_second: float = 2.8  # Estimated speaking rate (words/sec)
        self._interrupted_context: str = ""  # What was spoken before interruption

        # Context compression: keep history lean for faster LLM TTFT
        # After N turns, compress older turns into a summary.
        # Only last K raw turns are sent verbatim; rest is summarized.
        self._context_summary: str = ""  # Running summary of older turns
        self._raw_turn_window: int = 2  # Keep last 2 raw exchange pairs (saves ~100 tokens)
        self._compress_after_turns: int = 3  # Start compressing after 3 turns (earlier = faster)
        self._compression_task: Optional[asyncio.Task] = None

        # Latency-masking fillers: pre-synthesized short sounds ("Okay", "Right", etc.)
        # Played immediately when user finishes speaking, while LLM generates.
        # Masks ~400-600ms of latency, making the call feel responsive.
        self._filler_audio: list[bytes] = []  # Pre-synthesized filler audio clips
        self._filler_index: int = 0  # Round-robin through fillers
        self._filler_texts = [
            "Okay.",      # ~300ms - acknowledgment
            "Right.",     # ~250ms - engaged acknowledgment
            "Sure.",      # ~250ms
        ]

        # Turn 2 bank account question cache (pre-synthesized during dial)
        self._turn2_bank_audio: Optional[bytes] = None

        # Turn 1 response cache: pre-synthesized audio for common first responses
        # to "Does that ring a bell?" — eliminates LLM latency on turn 1.
        # Keys are pattern categories, values are (text, audio_bytes) tuples.
        self._turn1_cache: dict[str, tuple[str, Optional[bytes]]] = {}

        # Semantic response cache for turns 3+: matches LLM responses to pre-synthesized
        # common patterns via word-overlap similarity (Jaccard). Threshold 0.65 balances
        # precision (avoid false positives) with recall (catch paraphrases).
        self._semantic_cache = SemanticResponseCache(similarity_threshold=0.65)
        self._turn1_responses = {
            # Pattern → response text — SHORT (under 25 words each)
            "yes": (
                "Oh nice! So there's a preferred offer set aside for you — "
                "expires tomorrow though. Want me to pull it up?"
            ),
            "confused": (
                "No worries. A coverage offer came through with your name on it, "
                "runs out tomorrow. Want me to take a look?"
            ),
            "no_memory": (
                "That's okay. I've got a burial coverage quote here for you — "
                "want me to go over it real quick?"
            ),
            "not_interested": (
                "I hear you. It's free, no strings. Just a quick peek before "
                "it expires — worth a look?"
            ),
        }
        # Keywords that map to each pattern
        self._turn1_patterns = {
            "yes": {"yeah", "yes", "yep", "sure", "i remember", "i do", "i did",
                    "that's right", "rings a bell", "i think so", "uh huh",
                    "mm hmm", "right", "correct", "absolutely"},
            "not_interested": {"not interested", "no thanks", "no thank you",
                               "don't want", "don't need", "stop calling",
                               "remove me", "take me off", "do not call"},
            "confused": {"what", "huh", "who is this", "what are you",
                        "what's this about", "excuse me", "i'm sorry",
                        "what do you mean", "what company", "who are you"},
            "no_memory": {"no", "nope", "don't remember", "don't recall",
                         "i don't think so", "not sure", "i never",
                         "don't know", "no idea"},
        }

    async def pre_synthesize_backchannel_audio(self):
        """Pre-synthesize backchannel audio during dial time.
        These ultra-short clips (100-300ms) are injected during prospect pauses
        to signal active listening and increase perceived humanness."""
        logger.info("backchannel_synthesis_starting", call_id=self.call_id)
        t0 = time.time()

        backchannel_texts = {
            "mmhmm": "Mm-hmm.",
            "right": "Right.",
            "yeah": "Yeah.",
            "okay": "Okay.",
        }

        for key, text in backchannel_texts.items():
            try:
                audio = await self.orchestrator.tts.synthesize_single(
                    text=text, voice_id=self.agent_config.voice_id)
                if audio:
                    self._backchannel_manager.set_audio(key, audio)
                    logger.debug("backchannel_synthesis_success",
                        key=key, audio_bytes=len(audio))
            except Exception as e:
                logger.warning("backchannel_synthesis_failed",
                    key=key, error=str(e))

        elapsed = (time.time() - t0) * 1000
        logger.info("backchannel_synthesis_done", call_id=self.call_id,
            elapsed_ms=round(elapsed, 1))

    async def pre_synthesize_turn1_cache(self):
        """Pre-synthesize turn 1 response audio during dial time.
        This eliminates LLM latency entirely for the first conversational turn."""
        logger.info("turn1_cache_synthesis_starting", call_id=self.call_id,
            patterns=len(self._turn1_responses))
        t0 = time.time()
        for key, text in self._turn1_responses.items():
            try:
                audio = await self.orchestrator.tts.synthesize_single(
                    text=text, voice_id=self.agent_config.voice_id)
                if audio:
                    self._turn1_cache[key] = (text, audio)
            except Exception as e:
                logger.debug("turn1_cache_synthesis_failed", key=key, error=str(e))
        elapsed = (time.time() - t0) * 1000
        logger.info("turn1_cache_synthesis_done", call_id=self.call_id,
            cached=len(self._turn1_cache), elapsed_ms=round(elapsed, 1))

    def _match_turn1_pattern(self, transcript: str) -> Optional[str]:
        """Match user's first response to a cached pattern.
        Returns cache key or None if no match.

        IMPORTANT: If the prospect asks a question (contains '?', or question
        words like 'how', 'what', 'who', 'why', 'can you', 'could you'),
        do NOT serve a cached response — let the LLM handle it so it can
        actually answer their question.
        """
        clean = transcript.strip().lower()

        # If they're asking a question, skip cache — let LLM answer naturally
        question_indicators = ['?', 'how ', 'what ', 'who ', 'why ', 'when ',
                              'can you', 'could you', 'would you', 'tell me',
                              'remind me', 'explain', 'what\'s']
        for qi in question_indicators:
            if qi in clean:
                logger.debug("turn1_cache_skip_question",
                    call_id=self.call_id, transcript=transcript[:80])
                return None

        clean = clean.rstrip(".,!?")
        # Check exact matches first
        for key, keywords in self._turn1_patterns.items():
            if clean in keywords:
                return key
        # Check substring matches (only for very short responses — longer ones need LLM)
        if len(clean.split()) <= 4:
            for key, keywords in self._turn1_patterns.items():
                for kw in keywords:
                    if kw in clean:
                        return key
        return None

    # ── Turn 2 Cache (Bank Account Question) ────────────────────────────
    # After the prospect confirms interest, we ALWAYS ask about bank account.
    # Pre-synthesize this during dial to eliminate latency on turn 2.
    TURN2_BANK_ACCOUNT_TEXT = (
        "Got it. Quick thing — do you have a checking or savings account?"
    )

    async def pre_synthesize_turn2_cache(self):
        """Pre-synthesize the bank account question audio during dial time."""
        try:
            audio = await self.orchestrator.tts.synthesize_single(
                text=self.TURN2_BANK_ACCOUNT_TEXT,
                voice_id=self.agent_config.voice_id)
            if audio:
                self._turn2_bank_audio = audio
                logger.info("turn2_cache_ready", call_id=self.call_id)
        except Exception as e:
            self._turn2_bank_audio = None
            logger.debug("turn2_cache_failed", error=str(e))

    async def pre_synthesize_semantic_cache(self):
        """Pre-synthesize common response patterns during dial time for turns 3+.

        Semantic cache uses Jaccard similarity to match LLM-generated responses
        against ~15-20 high-frequency patterns. If match confidence > 0.85,
        we skip TTS entirely and use pre-synthesized audio.

        This saves ~200-400ms per cache hit on turns 3+. Pre-synthesis happens
        during dial time (5-10 seconds available before prospect answer).

        Call this alongside pre_synthesize_turn1_cache and pre_synthesize_turn2_cache.
        """
        count = await self._semantic_cache.presynthesize_all(
            synthesize_fn=self.orchestrator.tts.synthesize_single,
            voice_id=self.agent_config.voice_id
        )
        logger.info("semantic_cache_presynthesize_done",
            call_id=self.call_id, cached=count)
        return count

    async def pre_synthesize_recovery_audio(self):
        """Pre-synthesize conversation recovery phrases during dial time.

        Recovery phrases handle edge case failure modes:
        - Watchdog timeouts (no AI or prospect speech for 8s/5s)
        - LLM fallbacks (when Groq times out)
        - TTS generic fallback (when synthesis fails)

        All pre-synthesized during dial time so they're available instantly
        if needed during the call.
        """
        logger.info("recovery_audio_synthesis_starting", call_id=self.call_id)
        t0 = time.time()

        for phrase_key, phrase_text in self._recovery.config.recovery_phrases.items():
            try:
                audio = await self.orchestrator.tts.synthesize_single(
                    text=phrase_text, voice_id=self.agent_config.voice_id)
                if audio:
                    self._recovery.set_recovery_audio(phrase_key, audio)
                    logger.debug("recovery_audio_synthesis_success",
                        call_id=self.call_id, phrase=phrase_key,
                        audio_bytes=len(audio))
            except Exception as e:
                logger.warning("recovery_audio_synthesis_failed",
                    call_id=self.call_id, phrase=phrase_key, error=str(e))

        elapsed = (time.time() - t0) * 1000
        logger.info("recovery_audio_synthesis_done",
            call_id=self.call_id, elapsed_ms=round(elapsed, 1))

    # ── Context Compression ──────────────────────────────────────────────

    def _get_compressed_messages(self) -> list[dict]:
        """Return compressed conversation history for LLM input.

        Strategy:
        - If history is short (< compress_after_turns * 2 messages), return as-is.
        - Otherwise, prepend a summary of older turns, then only the last N raw turns.
        - This keeps the token count low → faster TTFT without losing essential context.
        """
        history = self.orchestrator._conversation_history
        # Each "turn" is a user+assistant pair (2 messages)
        threshold = self._compress_after_turns * 2

        if len(history) <= threshold:
            return list(history)  # Short enough, send everything

        # Split: older turns (to summarize) + recent turns (verbatim)
        raw_window = self._raw_turn_window * 2  # messages to keep raw
        recent = history[-raw_window:] if raw_window < len(history) else history

        # Build compressed messages
        compressed = []

        if self._context_summary:
            compressed.append({
                "role": "system",
                "content": f"[CONVERSATION SUMMARY — earlier in this call:\n{self._context_summary}]"
            })

        compressed.extend(recent)
        return compressed

    def _trigger_background_compression(self):
        """Trigger background compression of older conversation turns.

        Runs asynchronously so it doesn't add latency to the current turn.
        Uses a lightweight inline summarizer (no LLM call) — just extracts
        key facts from older turns to build a running context summary.
        """
        history = self.orchestrator._conversation_history
        threshold = self._compress_after_turns * 2

        if len(history) <= threshold:
            return  # Not enough turns to compress

        # Don't run if already running
        if self._compression_task and not self._compression_task.done():
            return

        self._compression_task = asyncio.create_task(
            self._compress_history_async())

    async def _compress_history_async(self):
        """Compress older conversation turns into a summary.

        LIGHTWEIGHT approach: no LLM call. Just extracts key information
        from older turns into a structured summary. This runs in background.
        """
        try:
            history = self.orchestrator._conversation_history
            raw_window = self._raw_turn_window * 2
            older = history[:-raw_window] if raw_window < len(history) else []

            if not older:
                return

            # Extract key facts from older turns
            summary_parts = []
            step_reached = ""
            objections = []
            prospect_info = []

            for msg in older:
                content = msg.get("content", "")
                role = msg.get("role", "")

                if role == "user":
                    # Track what the prospect said
                    lower = content.lower().strip()
                    if any(w in lower for w in ["yes", "yeah", "sure", "okay", "yep"]):
                        prospect_info.append(f"Prospect agreed: \"{content[:50]}\"")
                    elif any(w in lower for w in ["no", "not interested", "don't"]):
                        prospect_info.append(f"Prospect objected: \"{content[:50]}\"")
                    elif "?" in content:
                        prospect_info.append(f"Prospect asked: \"{content[:60]}\"")
                    elif len(content) > 10:
                        prospect_info.append(f"Prospect said: \"{content[:60]}\"")

                elif role == "assistant":
                    # Track what step the agent reached
                    lower = content.lower()
                    if "bank" in lower or "checking" in lower or "savings" in lower:
                        step_reached = "Asked about bank account"
                    elif "preferred" in lower or "offer" in lower or "discount" in lower:
                        step_reached = "Presented the preferred offer"
                    elif "ring a bell" in lower or "does that" in lower:
                        step_reached = "Initial pitch response"
                    if "interrupted" in lower:
                        # Don't include interrupted context in summary
                        continue

            if step_reached:
                summary_parts.append(f"Current step reached: {step_reached}")
            if prospect_info:
                # Keep last 5 prospect interactions
                summary_parts.extend(prospect_info[-5:])
            if objections:
                summary_parts.append(f"Objections raised: {', '.join(objections)}")

            if summary_parts:
                self._context_summary = "\n".join(summary_parts)
                logger.info("context_compressed",
                    call_id=self.call_id,
                    original_msgs=len(history),
                    summary_lines=len(summary_parts),
                    summary=self._context_summary[:200])

        except Exception as e:
            logger.warning("context_compression_error",
                call_id=self.call_id, error=str(e))

    # ── Interruption Context Tracking ─────────────────────────────────────

    def _record_interrupted_context(self) -> None:
        """When barge-in occurs, estimate what was actually spoken and add to history.

        Uses elapsed TTS playback time + estimated words-per-second to truncate
        the full LLM response to only what the prospect actually heard.
        Adds the truncated text with [INTERRUPTED] marker to conversation history
        so the LLM knows what was already said and can continue naturally.
        """
        if not self._current_response_text or not self._tts_start_time:
            self._interrupted_context = ""
            return

        # Calculate how long audio was playing (accounting for Twilio buffer delay)
        elapsed_s = time.time() - self._tts_start_time
        elapsed_s = max(0, elapsed_s - (self._twilio_playback_offset_ms / 1000))

        # Estimate words spoken based on speaking rate
        words_spoken = int(elapsed_s * self._tts_words_per_second)
        full_words = self._current_response_text.split()

        if words_spoken <= 0:
            self._interrupted_context = ""
            return

        if words_spoken >= len(full_words):
            # Spoke everything (or nearly) — add full text
            spoken_text = self._current_response_text
            unsaid_text = ""
        else:
            spoken_text = " ".join(full_words[:words_spoken])
            unsaid_text = " ".join(full_words[words_spoken:])

        # Add to conversation history with interruption marker
        # This tells the LLM exactly what was heard vs. not heard
        history_entry = f"{spoken_text} [INTERRUPTED — prospect did not hear the rest: \"{unsaid_text}\"]"
        self.orchestrator._conversation_history.append(
            {"role": "assistant", "content": history_entry})

        self._interrupted_context = unsaid_text  # Store unsaid part for system prompt injection

        logger.info("interrupted_context_recorded",
            call_id=self.call_id,
            words_spoken=words_spoken,
            total_words=len(full_words),
            spoken=spoken_text[:80],
            unsaid=unsaid_text[:80])

    # ── Repetition Detection ─────────────────────────────────────────────

    def _is_repetition(self, response_text: str) -> bool:
        """Check if a response is too similar to something already said.

        Two-level check:
        1. Word-overlap ratio: >65% overlap with any previous response = repetition
        2. Phrase-level: any 5+ word phrase reused verbatim = repetition
        """
        if not response_text:
            return False

        response_lower = response_text.lower()
        response_words = set(response_lower.split())
        if len(response_words) < 3:
            return False  # Too short to meaningfully compare

        for msg in self.orchestrator._conversation_history:
            if msg.get("role") != "assistant":
                continue
            prev_text = msg.get("content", "").lower()
            prev_words = set(prev_text.split())
            if len(prev_words) < 3:
                continue

            # Check 1: word-overlap ratio (lowered from 70% to 65%)
            overlap = len(response_words & prev_words)
            max_len = max(len(response_words), len(prev_words))
            if max_len > 0 and overlap / max_len > 0.65:
                logger.warning("repetition_word_overlap",
                    call_id=self.call_id,
                    response=response_text[:80],
                    overlap_ratio=round(overlap / max_len, 2))
                return True

            # Check 2: phrase-level — any 5+ word run reused verbatim
            prev_word_list = prev_text.split()
            for n in (6, 5):
                for i in range(len(prev_word_list) - n + 1):
                    phrase = " ".join(prev_word_list[i:i+n])
                    if phrase in response_lower:
                        logger.warning("repetition_phrase_match",
                            call_id=self.call_id,
                            phrase=phrase,
                            response=response_text[:80])
                        return True

        return False

    def _extract_key_phrases(self, text: str) -> list[str]:
        """Extract distinctive 4+ word phrases from a response."""
        words = text.lower().split()
        phrases = []
        # Extract 4-gram and 5-gram phrases
        for n in (5, 4):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i+n])
                # Skip very generic phrases
                skip = ("i'm here to", "would you like", "do you have", "can i help",
                        "let me know", "is there anything", "i want to make")
                if not any(s in phrase for s in skip):
                    phrases.append(phrase)
        return phrases[:15]  # Cap to keep instruction lean

    def _make_anti_repeat_instruction(self) -> str:
        """Generate aggressive anti-repeat instruction showing ALL previous responses
        and extracted key phrases that must NOT be reused verbatim."""
        prev_responses = [
            msg["content"] for msg in self.orchestrator._conversation_history
            if msg.get("role") == "assistant"
        ]
        if not prev_responses:
            return ""

        # Show ALL previous responses (truncated) so LLM sees its full history
        lines = " | ".join(r[:80] for r in prev_responses[-5:])

        # Extract distinctive phrases from ALL responses — these are BANNED
        all_phrases = set()
        for resp in prev_responses:
            all_phrases.update(self._extract_key_phrases(resp))
        banned = ", ".join(f'"{p}"' for p in list(all_phrases)[:10])

        instruction = (
            f'\n[SYSTEM: Your previous responses this call: "{lines}"\n'
            f'BANNED PHRASES (never reuse these exact words): {banned}\n'
            f'RULES: (1) NEVER repeat the same phrasing — if making the same point, '
            f'use COMPLETELY DIFFERENT words. (2) Keep it to 1-2 SHORT sentences + '
            f'ONE ending question. (3) Each response must say something NEW. '
            f'(4) Your question MUST be the LAST thing you say — nothing after it.]'
        )
        return instruction

    def _trim_prompt_for_step(self, system_prompt: str) -> str:
        """Dynamically trim the system prompt based on current call step.

        Strips verbose instructions for COMPLETED steps to reduce token count.
        This is the single biggest lever for reducing LLM TTFT — cutting 30-40%
        of input tokens on later turns.
        """
        import re
        step = self._call_state.current_step

        if step == ScriptStep.CONFIRM_INTEREST:
            return system_prompt  # Need full prompt on Step 1

        # On Step 2+: replace goal 1 with "DONE", strip Step 1 examples
        if step in (ScriptStep.BANK_ACCOUNT, ScriptStep.TRANSFER, ScriptStep.COMPLETED):
            system_prompt = re.sub(
                r'1\. CONFIRM INTEREST[^\n]*',
                '1. CONFIRM INTEREST — DONE (prospect is interested).',
                system_prompt)
            # Strip Step 1 examples (saves ~50 tokens)
            system_prompt = re.sub(
                r'- Step 1 —[^\n]*\n', '', system_prompt)

        # On Step 3+: also replace goal 2 with "DONE", strip Step 2 examples
        if step in (ScriptStep.TRANSFER, ScriptStep.COMPLETED):
            bank_info = self._call_state.bank_account_type or "confirmed"
            system_prompt = re.sub(
                r'2\. BANK ACCOUNT[^\n]*',
                f'2. BANK ACCOUNT — DONE (account: {bank_info}).',
                system_prompt)
            # Strip Step 2 examples and objection handling (saves ~150 tokens)
            system_prompt = re.sub(
                r'- Step 2 —[^\n]*\n', '', system_prompt)
            system_prompt = re.sub(
                r'OBJECTION HANDLING —[^\n]*\n(?:- [^\n]*\n)*', '', system_prompt)

        return system_prompt

    @staticmethod
    def _enforce_brevity(text: str) -> str:
        """Post-process LLM response to enforce brevity and trailing question.

        Rules:
        1. Strip any asterisk/bracket stage directions
        2. Cap at ~25 words (keep last sentence if it's a question)
        3. Ensure response ends with a question mark or trailing tag
        """
        import re

        # Remove stage directions: *laughs*, [pause], etc.
        text = re.sub(r'\*[^*]+\*', '', text).strip()
        text = re.sub(r'\[[^\]]+\]', '', text).strip()

        if not text:
            return "So what do you think?"

        words = text.split()

        # If over 35 words, truncate smartly (target: 1-2 sentences + question)
        if len(words) > 35:
            # Find the last sentence boundary at or before word 35
            truncated = ' '.join(words[:35])
            # Try to end at a sentence boundary
            for punct in ['?', '.', '!', ',']:
                last_idx = truncated.rfind(punct)
                if last_idx > len(truncated) // 2:  # Don't cut too short
                    truncated = truncated[:last_idx + 1]
                    break

            # If we lost the question, add a trailing tag
            if '?' not in truncated:
                truncated = truncated.rstrip('.!,') + ", sound good?"
            text = truncated

        # Ensure ends with question (the #1 rule for conversion)
        if '?' not in text:
            # Add a natural trailing question
            text = text.rstrip('.!,') + ", okay?"

        return text

    # ── Audio Utilities ──────────────────────────────────────────────────

    @staticmethod
    def _audio_rms(pcm_data: bytes) -> float:
        """Calculate RMS energy of PCM 16-bit audio."""
        if len(pcm_data) < 2:
            return 0.0
        n_samples = len(pcm_data) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_data[:n_samples * 2])
        if not samples:
            return 0.0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    @staticmethod
    def _fade_in(pcm_data: bytes, fade_samples: int = 160) -> bytes:
        """Apply Hann window fade-in to first N samples (10ms at 16kHz).
        Hann window endpoints touch zero, preventing discontinuity clicks.
        Increased from 80 (5ms) to 160 (10ms) for smoother transitions."""
        import math
        if len(pcm_data) < fade_samples * 2:
            return pcm_data
        n = len(pcm_data) // 2
        samples = list(struct.unpack(f'<{n}h', pcm_data[:n * 2]))
        for i in range(min(fade_samples, n)):
            # Hann fade-in: (1 - cos(pi * i / N)) / 2
            w = (1.0 - math.cos(math.pi * i / fade_samples)) / 2.0
            samples[i] = int(samples[i] * w)
        return struct.pack(f'<{n}h', *samples)

    @staticmethod
    def _fade_out(pcm_data: bytes, fade_samples: int = 160) -> bytes:
        """Apply Hann window fade-out to last N samples (10ms at 16kHz).
        Hann window endpoints touch zero, preventing discontinuity clicks.
        Increased from 80 (5ms) to 160 (10ms) for smoother transitions."""
        import math
        if len(pcm_data) < fade_samples * 2:
            return pcm_data
        n = len(pcm_data) // 2
        samples = list(struct.unpack(f'<{n}h', pcm_data[:n * 2]))
        for i in range(min(fade_samples, n)):
            idx = n - 1 - i
            # Hann fade-out: (1 - cos(pi * i / N)) / 2
            w = (1.0 - math.cos(math.pi * i / fade_samples)) / 2.0
            samples[idx] = int(samples[idx] * w)
        return struct.pack(f'<{n}h', *samples)

    @staticmethod
    def _apply_fade(pcm_data: bytes, fade_in: bool = False, fade_out: bool = False,
                    fade_samples: int = 80) -> bytes:
        """Apply fade-in and/or fade-out to a PCM frame (5ms at 16kHz = 80 samples).
        Shorter than the 10ms used for pitch (160 samples) to keep responses snappy
        while still eliminating click artifacts at speech boundaries."""
        import math
        n = len(pcm_data) // 2
        if n < fade_samples * 2:
            return pcm_data
        samples = list(struct.unpack(f'<{n}h', pcm_data[:n * 2]))
        if fade_in:
            for i in range(min(fade_samples, n)):
                w = (1.0 - math.cos(math.pi * i / fade_samples)) / 2.0
                samples[i] = int(samples[i] * w)
        if fade_out:
            for i in range(min(fade_samples, n)):
                idx = n - 1 - i
                w = (1.0 - math.cos(math.pi * i / fade_samples)) / 2.0
                samples[idx] = int(samples[idx] * w)
        return struct.pack(f'<{n}h', *samples)

    def _queue_pcm_with_fades(self, pcm_data: bytes) -> int:
        """Queue a complete PCM audio blob (e.g. from synthesize_single) to _output_queue
        with fade-in on first frame and fade-out on last frame.
        Returns number of frames queued."""
        PCM_FRAME_SIZE = 640
        frames = []
        for i in range(0, len(pcm_data) - (PCM_FRAME_SIZE - 1), PCM_FRAME_SIZE):
            frames.append(pcm_data[i:i + PCM_FRAME_SIZE])
        # Pad last partial frame if any
        remainder = len(pcm_data) % PCM_FRAME_SIZE
        if remainder > 0 and len(pcm_data) > PCM_FRAME_SIZE:
            last = pcm_data[-(remainder):]
            last = last + b'\x00' * (PCM_FRAME_SIZE - len(last))
            frames.append(last)
        if not frames:
            return 0
        # Apply fades
        frames[0] = self._apply_fade(frames[0], fade_in=True)
        frames[-1] = self._apply_fade(frames[-1], fade_out=True)
        queued = 0
        for frame in frames:
            try:
                self._output_queue.put_nowait(frame)
                queued += 1
            except asyncio.QueueFull:
                break
        return queued

    @staticmethod
    def _remove_dc_offset(pcm_data: bytes) -> bytes:
        """Remove DC offset from PCM audio to prevent clicks at start/stop boundaries."""
        if len(pcm_data) < 4:
            return pcm_data
        n = len(pcm_data) // 2
        samples = list(struct.unpack(f'<{n}h', pcm_data[:n * 2]))
        mean = sum(samples) / n
        if abs(mean) > 10:  # Only correct if offset is significant
            samples = [max(-32768, min(32767, int(s - mean))) for s in samples]
            return struct.pack(f'<{n}h', *samples)
        return pcm_data

    @staticmethod
    def _compress_silence(pcm_data: bytes, threshold_rms: int = 50,
                          min_silence_ms: int = 150, compression: float = 0.5) -> bytes:
        """Compress silence/pause segments in PCM audio by a factor.

        This reduces comma pauses and other punctuation-driven pauses
        that make speech sound unnatural.

        CROSSFADING: At silence boundaries (speech→silence and silence→speech),
        applies a short crossfade to prevent clicks from abrupt transitions.

        Args:
            threshold_rms: RMS below this = silence (50 is very quiet)
            min_silence_ms: Only compress silences longer than this (ms)
            compression: Factor to compress by (0.5 = half the silence)
        """
        import math
        if len(pcm_data) < 640 or compression >= 1.0:
            return pcm_data

        FRAME_SIZE = 640  # 20ms at 16kHz 16-bit mono
        CROSSFADE_SAMPLES = 80  # 5ms crossfade at silence boundaries
        min_silence_frames = max(1, int(min_silence_ms / 20))
        frames = []

        for i in range(0, len(pcm_data) - FRAME_SIZE + 1, FRAME_SIZE):
            frames.append(pcm_data[i:i + FRAME_SIZE])

        if not frames:
            return pcm_data

        # Detect silence runs and build output with crossfades
        import audioop
        result = bytearray()
        silence_run = 0
        last_speech_frame = None  # Track for crossfading

        for frame in frames:
            rms = audioop.rms(frame, 2) if len(frame) >= 2 else 0
            if rms < threshold_rms:
                silence_run += 1
            else:
                if silence_run > min_silence_frames:
                    # Compress this silence: keep only compression * frames
                    keep = max(min_silence_frames, int(silence_run * compression))
                    for _ in range(keep):
                        result.extend(b'\x00' * FRAME_SIZE)
                    # Crossfade: fade-in the first speech frame after silence
                    n = len(frame) // 2
                    if n > CROSSFADE_SAMPLES:
                        samples = list(struct.unpack(f'<{n}h', frame[:n * 2]))
                        for i in range(CROSSFADE_SAMPLES):
                            w = (1.0 - math.cos(math.pi * i / CROSSFADE_SAMPLES)) / 2.0
                            samples[i] = int(samples[i] * w)
                        frame = struct.pack(f'<{n}h', *samples)
                elif silence_run > 0:
                    # Short silence — keep as-is
                    for _ in range(silence_run):
                        result.extend(b'\x00' * FRAME_SIZE)
                silence_run = 0

                # Crossfade: fade-out the last speech frame before upcoming silence
                # (applied retroactively when we detect silence starts)
                last_speech_frame = len(result)
                result.extend(frame)

        # Handle trailing silence
        if silence_run > 0:
            # Fade-out the last speech frame before trailing silence
            if last_speech_frame is not None and last_speech_frame + FRAME_SIZE <= len(result):
                last_frame = bytes(result[last_speech_frame:last_speech_frame + FRAME_SIZE])
                n = len(last_frame) // 2
                if n > CROSSFADE_SAMPLES:
                    samples = list(struct.unpack(f'<{n}h', last_frame[:n * 2]))
                    for i in range(CROSSFADE_SAMPLES):
                        idx = n - 1 - i
                        w = (1.0 - math.cos(math.pi * i / CROSSFADE_SAMPLES)) / 2.0
                        samples[idx] = int(samples[idx] * w)
                    faded = struct.pack(f'<{n}h', *samples)
                    result[last_speech_frame:last_speech_frame + FRAME_SIZE] = faded

            keep = max(1, int(silence_run * compression)) if silence_run > min_silence_frames else silence_run
            for _ in range(keep):
                result.extend(b'\x00' * FRAME_SIZE)

        return bytes(result)

    def _in_echo_zone(self) -> bool:
        """Check if we're in the echo suppression zone (AI just spoke).

        Reduced aggressiveness: base 400ms + 50ms/s of audio, capped at 800ms.
        Previously 800ms + 150ms/s capped at 2000ms — way too long, was
        suppressing user speech and causing missed interruptions.
        """
        if self._ai_speaking:
            return True
        if self._ai_speech_ended_at > 0:
            elapsed = (time.time() - self._ai_speech_ended_at) * 1000
            playback_duration_ms = self._last_output_bytes / 32  # PCM 16kHz 16-bit
            dynamic_cooldown = min(
                self._echo_cooldown_ms + (playback_duration_ms * 0.05),
                800,  # Cap at 800ms (was 2000ms)
            )
            return elapsed < dynamic_cooldown
        return False

    async def _try_inject_backchannel(self, pcm_chunk: bytes) -> bool:
        """Check if a backchannel should be injected during this silence.

        Called when a silence chunk is detected during prospect speech.
        Returns True if backchannel was injected (caller should suppress the silence frame).

        CRITICAL: Backchannel injection must NOT reset VAD/turn timers.
        The audio is played but treated as a "micro-response" that doesn't
        count as a full AI turn.
        """
        if not self._backchannel_manager._enabled:
            return False

        # Calculate RMS energy to determine if this is actual silence
        rms = self._audio_rms(pcm_chunk)
        if rms >= self._speech_energy_threshold:
            # Still speech — not silence
            self._backchannel_manager.on_prospect_audio_chunk()
            return False

        # This is silence
        backchannel_type = self._backchannel_manager.on_prospect_silence_chunk()

        if backchannel_type is None:
            return False

        # Get the pre-synthesized audio
        audio = self._backchannel_manager.get_audio(backchannel_type)
        if audio is None:
            return False

        try:
            # Queue backchannel audio directly (minimal processing)
            # Don't use fades or compression — keep it brief and natural
            chunk_size = 640
            for i in range(0, len(audio), chunk_size):
                chunk = audio[i:i + chunk_size]
                try:
                    self._output_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    break

            logger.info("backchannel_injected",
                call_id=self.call_id,
                type=backchannel_type,
                audio_bytes=len(audio))

            # Record that AI "spoke" briefly (for echo suppression grace period)
            self._ai_speaking = True
            self._ai_speaking_started_at = time.time()

            # Schedule when backchannel finishes so echo suppression can apply
            async def _backchannel_finished():
                backchannel_duration_s = len(audio) / 32000
                await asyncio.sleep(backchannel_duration_s)
                if self._ai_speaking:
                    self._ai_speaking = False
                    self._ai_speech_ended_at = time.time()
                    # Record backchannel end for AI grace period calculation
                    self._backchannel_manager.on_ai_speech_ended()

            asyncio.create_task(_backchannel_finished())
            return True

        except Exception as e:
            logger.warning("backchannel_injection_failed",
                call_id=self.call_id,
                type=backchannel_type,
                error=str(e))
            return False

    def _queue_audio(self, audio_bytes: bytes) -> int:
        """Queue PCM audio for output to Twilio. Returns chunk count.
        Pipeline: DC offset removal → silence compression → Hann fade-in/out."""
        # Step 1: Remove DC offset (prevents clicks at start/stop)
        audio_bytes = self._remove_dc_offset(audio_bytes)

        # Step 2: Compress silence/pauses (reduces comma pauses)
        if self._silence_compression < 1.0:
            audio_bytes = self._compress_silence(
                audio_bytes,
                threshold_rms=self._silence_threshold_rms,
                min_silence_ms=self._min_silence_ms,
                compression=self._silence_compression,
            )

        chunk_size = 640  # ~20ms at 16kHz 16-bit mono
        chunk_count = 0
        total_chunks = (len(audio_bytes) + chunk_size - 1) // chunk_size
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            # Hann fade-in on first chunk, Hann fade-out on last chunk
            if chunk_count == 0:
                chunk = self._fade_in(chunk)
            elif chunk_count == total_chunks - 1:
                chunk = self._fade_out(chunk)
            try:
                self._output_queue.put_nowait(chunk)
                chunk_count += 1
            except asyncio.QueueFull:
                break
        self._last_output_bytes = len(audio_bytes)
        return chunk_count

    # ── Pre-synthesis (called during dial, while phone rings) ────────────

    async def pre_synthesize_greeting(self):
        """Pre-synthesize greeting audio BEFORE the call is answered."""
        greeting_text = self.agent_config.greeting or self.DEFAULT_GREETING
        logger.info("greeting_pre_synthesis_starting",
            call_id=self.call_id, greeting=greeting_text[:80])

        t0 = time.time()
        try:
            audio_bytes = await self.orchestrator.tts.synthesize_single(
                text=greeting_text,
                voice_id=self.agent_config.voice_id,
            )
            elapsed = (time.time() - t0) * 1000
            if audio_bytes:
                self._greeting_audio = audio_bytes
                logger.info("greeting_pre_synthesis_done",
                    call_id=self.call_id, bytes=len(audio_bytes),
                    elapsed_ms=round(elapsed, 1))
            else:
                logger.warning("greeting_pre_synthesis_empty", call_id=self.call_id)
        except Exception as e:
            logger.error("greeting_pre_synthesis_failed",
                call_id=self.call_id, error=str(e))

    async def pre_synthesize_pitch(self):
        """
        Pre-synthesize the sales pitch as ONE seamless audio.
        Called during dial time. This is the key to natural-sounding delivery —
        one continuous TTS generation, not chunked sentence-by-sentence.
        """
        pitch_text = getattr(self.agent_config, 'pitch_text', '') or self.DEFAULT_PITCH
        if not pitch_text:
            logger.info("pitch_pre_synthesis_skipped", call_id=self.call_id,
                reason="no_pitch_text")
            return

        logger.info("pitch_pre_synthesis_starting",
            call_id=self.call_id, pitch=pitch_text[:100])

        t0 = time.time()
        try:
            audio_bytes = await self.orchestrator.tts.synthesize_single(
                text=pitch_text,
                voice_id=self.agent_config.voice_id,
            )
            elapsed = (time.time() - t0) * 1000
            if audio_bytes:
                self._pitch_audio = audio_bytes
                duration_ms = len(audio_bytes) / 32  # PCM 16kHz 16-bit
                logger.info("pitch_pre_synthesis_done",
                    call_id=self.call_id,
                    bytes=len(audio_bytes),
                    duration_ms=round(duration_ms, 0),
                    elapsed_ms=round(elapsed, 1))
            else:
                logger.warning("pitch_pre_synthesis_empty", call_id=self.call_id)
        except Exception as e:
            logger.error("pitch_pre_synthesis_failed",
                call_id=self.call_id, error=str(e))

    async def pre_synthesize_fillers(self):
        """Pre-synthesize latency-masking filler words during dial time.
        These short clips (~200-300ms) play instantly when the user stops speaking,
        giving the LLM time to generate while the caller hears an acknowledgment."""
        logger.info("filler_pre_synthesis_starting", call_id=self.call_id,
            count=len(self._filler_texts))
        t0 = time.time()
        for text in self._filler_texts:
            try:
                audio = await self.orchestrator.tts.synthesize_single(
                    text=text, voice_id=self.agent_config.voice_id)
                if audio:
                    self._filler_audio.append(audio)
            except Exception as e:
                logger.debug("filler_synthesis_failed", text=text, error=str(e))
        elapsed = (time.time() - t0) * 1000
        logger.info("filler_pre_synthesis_done", call_id=self.call_id,
            count=len(self._filler_audio), elapsed_ms=round(elapsed, 1))

    def _get_next_filler(self) -> Optional[bytes]:
        """Get next pre-synthesized filler audio (round-robin)."""
        if not self._filler_audio:
            return None
        audio = self._filler_audio[self._filler_index % len(self._filler_audio)]
        self._filler_index += 1
        return audio

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self):
        """Start the bridge and begin the 3-phase call loop."""
        self._active = True
        self._call_phase = "detect"
        logger.info("call_bridge_started", call_id=self.call_id)
        self._turn_task = asyncio.create_task(self._call_loop())

    async def _hangup_twilio(self):
        """Terminate the Twilio call via REST API so the prospect's line is freed."""
        if not self.twilio_client or not self.twilio_call_sid:
            logger.warning("hangup_twilio_skipped",
                call_id=self.call_id,
                has_client=bool(self.twilio_client),
                has_sid=bool(self.twilio_call_sid))
            return
        try:
            self.twilio_client.calls(self.twilio_call_sid).update(status="completed")
            logger.info("hangup_twilio_success",
                call_id=self.call_id, call_sid=self.twilio_call_sid)
        except Exception as e:
            logger.warning("hangup_twilio_failed",
                call_id=self.call_id, error=str(e))

    async def stop(self):
        """Stop the bridge."""
        self._active = False
        self._call_phase = "ended"
        await self._hangup_twilio()
        await self._input_queue.put(None)
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
        logger.info("call_bridge_stopped", call_id=self.call_id)

    # ── Audio I/O (called by media stream handler) ───────────────────────

    async def process_audio(self, pcm_data: bytes):
        """Push incoming audio from Twilio into the bridge."""
        if not self._active:
            return

        self._audio_chunks_received += 1

        if self._audio_chunks_received in (1, 10, 100):
            logger.info("audio_chunk_received",
                call_id=self.call_id, count=self._audio_chunks_received,
                chunk_bytes=len(pcm_data), phase=self._call_phase)

        try:
            self._input_queue.put_nowait(pcm_data)
        except asyncio.QueueFull:
            try:
                self._input_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._input_queue.put_nowait(pcm_data)
            except asyncio.QueueFull:
                pass

    async def get_audio(self) -> Optional[bytes]:
        """Pull response audio to send back to Twilio."""
        if not self._active:
            return None

        try:
            data = await asyncio.wait_for(self._output_queue.get(), timeout=0.05)
            if data and len(data) > 1:
                if not self._ai_speaking:
                    self._ai_speaking = True
                    self._ai_speaking_started_at = time.time()
            return data
        except asyncio.TimeoutError:
            if self._ai_speaking:
                self._ai_speaking = False
                self._ai_speech_ended_at = time.time()
            return b""

    # ── 3-Phase Call Loop ────────────────────────────────────────────────

    async def _call_loop(self):
        """
        Main call loop implementing the 3-phase architecture.

        Phase 1 — DETECT:  Play greeting, wait for human response
        Phase 2 — PITCH:   Play pre-baked sales pitch
        Phase 3 — CONVERSE: Live STT→LLM→TTS conversation
        """
        try:
            # ═══════════════════════════════════════════════════════════════
            # PHASE 1 — DETECT: Greeting + human detection
            # ═══════════════════════════════════════════════════════════════
            self._call_phase = "detect"
            greeting_text = self.agent_config.greeting or self.DEFAULT_GREETING

            # Small delay before greeting — feels more natural, like picking up the phone
            await asyncio.sleep(0.3)

            # Play pre-synthesized greeting
            if self._greeting_audio:
                chunks = self._queue_audio(self._greeting_audio)
                self.orchestrator._conversation_history.append({
                    "role": "assistant", "content": greeting_text,
                })
                logger.info("phase1_greeting_queued",
                    call_id=self.call_id, chunks=chunks,
                    bytes=len(self._greeting_audio),
                    history_len=len(self.orchestrator._conversation_history))
            else:
                # Fallback: synthesize on-the-fly
                try:
                    audio = await self.orchestrator.tts.synthesize_single(
                        text=greeting_text, voice_id=self.agent_config.voice_id)
                    if audio:
                        self._queue_audio(audio)
                        self.orchestrator._conversation_history.append({
                            "role": "assistant", "content": greeting_text,
                        })
                except Exception as e:
                    logger.error("phase1_greeting_failed", error=str(e))

            # Wait for greeting to finish playing (estimate from audio size)
            greeting_duration_ms = len(self._greeting_audio or b'') / 32
            await asyncio.sleep(greeting_duration_ms / 1000 + 0.3)  # +300ms buffer

            # Drain stale audio accumulated during greeting playback
            drained = 0
            while not self._input_queue.empty():
                try:
                    self._input_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                logger.info("phase1_stale_drained", call_id=self.call_id, chunks=drained)

            # Wait for human speech (up to 5 seconds)
            # If no speech → likely voicemail/silence → end call
            # Minimum wait: 500ms to avoid detecting greeting echo as human speech
            human_detected = False
            detect_start = time.time()
            detect_timeout = 5.0  # seconds
            min_detect_delay = 0.5  # Ignore first 500ms (greeting echo)

            # Track speech for voicemail detection
            speech_duration_ms = 0
            speech_start_time = None
            silence_gaps = []
            last_speech_end_time = None

            logger.info("phase1_waiting_for_human", call_id=self.call_id)

            while self._active and (time.time() - detect_start) < detect_timeout:
                try:
                    chunk = await asyncio.wait_for(self._input_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if chunk is None:
                    return

                elapsed = time.time() - detect_start
                rms = self._audio_rms(chunk)
                # Skip echo detection in first 500ms
                if elapsed < min_detect_delay:
                    continue
                
                # Track speech duration for voicemail detection
                is_speech = rms > self._speech_energy_threshold
                if is_speech:
                    if speech_start_time is None:
                        speech_start_time = time.time()
                    speech_duration_ms = (time.time() - speech_start_time) * 1000
                    last_speech_end_time = time.time()
                    
                    if not human_detected:
                        human_detected = True
                        detect_ms = (time.time() - detect_start) * 1000
                        logger.info("phase1_human_detected",
                            call_id=self.call_id, rms=round(rms, 0),
                            detect_ms=round(detect_ms, 0))
                        break
                else:
                    # Silence detected - track gap
                    if last_speech_end_time and (time.time() - last_speech_end_time) > 0.5:
                        gap_ms = (time.time() - last_speech_end_time) * 1000
                        if gap_ms not in silence_gaps:
                            silence_gaps.append(gap_ms)

            if not human_detected:
                logger.info("phase1_no_human_detected",
                    call_id=self.call_id, timeout_s=detect_timeout)
                # Voicemail detection: check if pattern matches voicemail/IVR
                is_voicemail = self._check_voicemail_pattern(speech_duration_ms, silence_gaps)
                if is_voicemail:
                    logger.info("voicemail_detected_ending_call",
                        call_id=self.call_id,
                        speech_duration_ms=round(speech_duration_ms, 0),
                        silence_patterns=silence_gaps)
                    return  # End call gracefully
                else:
                    # No clear voicemail pattern - continue (might be slow responder)
                    logger.info("phase1_no_voicemail_pattern_continuing",
                        call_id=self.call_id, speech_duration_ms=round(speech_duration_ms, 0))

            if not self._active:
                return

            # Let the human finish their greeting response (e.g. "hello?", "yes")
            # before jumping into the pitch. 800ms settling time.
            await asyncio.sleep(0.8)

            # ═══════════════════════════════════════════════════════════════
            # PHASE 2 — PITCH: Deliver pre-baked sales pitch
            # ═══════════════════════════════════════════════════════════════
            self._call_phase = "pitch"
            pitch_text = getattr(self.agent_config, 'pitch_text', '') or self.DEFAULT_PITCH

            # For inbound calls: pitch_text is set but audio wasn't pre-synthesized
            # (no dial phase to pre-generate). Synthesize on-the-fly now.
            if pitch_text and not self._pitch_audio:
                logger.info("phase2_pitch_synthesizing_live",
                    call_id=self.call_id, text=pitch_text[:80])
                try:
                    t_synth = time.time()
                    self._pitch_audio = await self.orchestrator.tts.synthesize_single(
                        text=pitch_text, voice_id=self.agent_config.voice_id)
                    synth_ms = (time.time() - t_synth) * 1000
                    if self._pitch_audio:
                        logger.info("phase2_pitch_synthesized_live",
                            call_id=self.call_id,
                            bytes=len(self._pitch_audio),
                            synth_ms=round(synth_ms, 0))
                    else:
                        logger.warning("phase2_pitch_synthesis_empty",
                            call_id=self.call_id)
                except Exception as e:
                    logger.error("phase2_pitch_synthesis_failed",
                        call_id=self.call_id, error=str(e))

            if self._pitch_audio and pitch_text:
                # Drain any audio that arrived during detection
                while not self._input_queue.empty():
                    try:
                        self._input_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                # Add 400ms breath pause between greeting and pitch
                # (20 frames × 20ms = 400ms of silence — natural breath pause)
                silence_frame = b'\x00' * 640
                for _ in range(20):
                    try:
                        await self._output_queue.put(silence_frame)
                    except asyncio.QueueFull:
                        break

                chunks = self._queue_audio(self._pitch_audio)
                # Add pitch to conversation history so LLM knows it was said
                self.orchestrator._conversation_history.append({
                    "role": "assistant", "content": pitch_text,
                })

                pitch_duration_ms = len(self._pitch_audio) / 32
                logger.info("phase2_pitch_queued",
                    call_id=self.call_id, chunks=chunks,
                    bytes=len(self._pitch_audio),
                    duration_ms=round(pitch_duration_ms, 0),
                    history_len=len(self.orchestrator._conversation_history))

                # Wait for pitch to finish playing.
                # Barge-in ENABLED after 3s grace period using RMS energy detection.
                # This lets the prospect interrupt the pitch naturally.
                pitch_start_time = time.time()
                pitch_end_time = pitch_start_time + (pitch_duration_ms / 1000) + 0.5
                barge_in_energy_count = 0
                barge_in_speech_start = 0.0  # When sustained speech started
                PITCH_BARGE_IN_RMS = 1500  # Minimum RMS for real speech (not echo)
                PITCH_BARGE_IN_FRAMES = 4  # Need 4 consecutive frames (~80ms) to START counting
                # Minimum speech duration to accept as real interruption:
                # Short sounds like "ok", "got it", "mhm" are ~200-400ms
                # Real questions/objections are 800ms+
                MIN_INTERRUPT_DURATION_MS = self._pitch_min_interrupt_ms

                while time.time() < pitch_end_time and self._active:
                    try:
                        chunk = await asyncio.wait_for(
                            self._input_queue.get(), timeout=0.5)
                        if chunk is None:
                            return

                        # Check for barge-in after grace period
                        elapsed_ms = (time.time() - pitch_start_time) * 1000
                        if elapsed_ms > self._pitch_barge_in_grace_ms:
                            import audioop
                            rms = audioop.rms(chunk, 2) if len(chunk) >= 2 else 0
                            if rms > PITCH_BARGE_IN_RMS:
                                barge_in_energy_count += 1
                                if barge_in_energy_count == PITCH_BARGE_IN_FRAMES:
                                    # Speech just started — record the timestamp
                                    barge_in_speech_start = time.time()
                                elif barge_in_energy_count > PITCH_BARGE_IN_FRAMES and barge_in_speech_start > 0:
                                    # Check if speech has been sustained long enough
                                    speech_duration_ms = (time.time() - barge_in_speech_start) * 1000
                                    if speech_duration_ms >= MIN_INTERRUPT_DURATION_MS:
                                        logger.info("phase2_barge_in_detected",
                                            call_id=self.call_id,
                                            elapsed_ms=round(elapsed_ms),
                                            speech_duration_ms=round(speech_duration_ms),
                                            rms=rms)
                                        # Clear queued pitch audio
                                        cleared = 0
                                        while not self._output_queue.empty():
                                            try:
                                                self._output_queue.get_nowait()
                                                cleared += 1
                                            except asyncio.QueueEmpty:
                                                break
                                        # Tell Twilio to stop playing
                                        if self.twilio_telephony:
                                            try:
                                                await self.twilio_telephony.send_clear(self.call_id)
                                            except Exception:
                                                pass
                                        self._pitch_interrupted = True
                                        logger.info("phase2_pitch_interrupted",
                                            call_id=self.call_id,
                                            cleared_chunks=cleared,
                                            speech_duration_ms=round(speech_duration_ms))
                                        break
                            else:
                                # Speech stopped — reset counters
                                if barge_in_energy_count >= PITCH_BARGE_IN_FRAMES and barge_in_speech_start > 0:
                                    short_ms = (time.time() - barge_in_speech_start) * 1000
                                    logger.debug("phase2_short_speech_ignored",
                                        call_id=self.call_id,
                                        duration_ms=round(short_ms))
                                barge_in_energy_count = 0
                                barge_in_speech_start = 0.0
                    except asyncio.TimeoutError:
                        continue

                # Drain echo audio from pitch playback — but preserve recent audio.
                # The queue may contain the user's first words spoken right as the
                # pitch ended. Only drain chunks that are clearly pitch echo (older
                # than 200ms before pitch end). Keep the most recent ~10 chunks
                # (200ms of audio) which may contain the user's first response.
                queue_size = self._input_queue.qsize()
                preserve_count = min(10, queue_size)  # Keep last ~200ms
                drain_count = max(0, queue_size - preserve_count)
                drained = 0
                for _ in range(drain_count):
                    try:
                        self._input_queue.get_nowait()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained or preserve_count:
                    logger.info("phase2_echo_drained",
                        call_id=self.call_id, drained=drained,
                        preserved=preserve_count)
            else:
                logger.info("phase2_no_pitch_audio",
                    call_id=self.call_id,
                    has_pitch_text=bool(pitch_text),
                    has_pitch_audio=bool(self._pitch_audio))

            if not self._active:
                return

            # ═══════════════════════════════════════════════════════════════
            # PHASE 3 — CONVERSE: Continuous STT streaming + LLM→TTS
            # ═══════════════════════════════════════════════════════════════
            # Architecture: Stream ALL audio to Deepgram continuously.
            # Let Deepgram's VAD + endpointing (300ms) handle turn detection.
            # On final transcript → trigger LLM→TTS.
            # During echo zone → send silence to keep Deepgram alive.
            # This eliminates our custom VAD (which was catching noise).
            # ═══════════════════════════════════════════════════════════════
            self._call_phase = "converse"
            self._backchannel_manager.on_phase_changed("converse")

            # KEEP the same TTS WebSocket that produced the great-sounding
            # greeting and pitch. Reconnecting changes Cartesia's internal
            # state and degrades quality. Just verify connection is alive.
            try:
                if not self.orchestrator.tts._ws:
                    await self.orchestrator.tts.connect()
                    logger.info("phase3_tts_connected_fresh", call_id=self.call_id)
                else:
                    logger.info("phase3_tts_reusing_connection", call_id=self.call_id)
            except Exception as e:
                logger.warning("phase3_tts_check_failed", error=str(e))
                try:
                    await self.orchestrator.tts.connect()
                except Exception:
                    pass

            # Reconnect STT — WebSocket went stale during pitch.
            # FAST reconnect: no sleep, parallel with queue buffer preservation.
            # Audio in _input_queue is NOT drained here — it contains the user's
            # first words after the pitch, which we want STT to process.
            try:
                await self.orchestrator.stt.disconnect()
                await self.orchestrator.stt.connect()
                logger.info("phase3_stt_reconnected", call_id=self.call_id,
                    queued_audio_chunks=self._input_queue.qsize())
            except Exception as e:
                logger.error("phase3_stt_reconnect_failed",
                    call_id=self.call_id, error=str(e))

            # Reset echo state — use MINIMAL echo zone.
            # The pitch echo was already drained in Phase 2 (lines above).
            # Setting _last_output_bytes=0 gives base 800ms cooldown only.
            # Previously this was set to len(pitch_audio) which created 2s
            # echo zone, suppressing the prospect's first response and causing
            # a deadlock (both sides waiting for each other).
            self._ai_speech_ended_at = time.time()
            self._last_output_bytes = 0  # Minimal echo zone — pitch echo already drained
            turn_number = 0
            active_turn_task: Optional[asyncio.Task] = None

            phase3_start_time = time.time()
            self._phase3_first_speech = False  # Track if we've heard any speech

            MAX_PHASE3_TURNS = 15  # Safety: end call after 15 turns to prevent loops

            logger.info("phase3_continuous_starting",
                call_id=self.call_id,
                history_len=len(self.orchestrator._conversation_history))

            # Audio feeder: continuously pipes input_queue → Deepgram.
            # During echo zone, sends silence instead of real audio.
            async def audio_feeder() -> AsyncIterator[bytes]:
                silence_frame = b'\x00' * 640  # 20ms silence at 16kHz 16-bit
                while self._active and self._call_phase == "converse":
                    try:
                        chunk = await asyncio.wait_for(
                            self._input_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        yield silence_frame  # Keep Deepgram alive
                        continue

                    if chunk is None:
                        return  # Call ended

                    if self._in_echo_zone():
                        yield silence_frame  # Suppress echo
                        continue

                    # Backchannel injection: try to inject during natural pauses
                    # If injected, don't yield the silence frame to Deepgram
                    # (the backchannel audio itself will be output separately)
                    rms = self._audio_rms(chunk)
                    if rms < self._speech_energy_threshold:
                        # This is a silence frame — check for backchannel opportunity
                        if await self._try_inject_backchannel(chunk):
                            # Backchannel was injected — suppress this silence frame
                            # But still signal prospect audio absence to STT
                            yield silence_frame
                            continue

                    yield chunk  # Real audio → Deepgram

            # Multi-stage silence management — escalates through nudge → check-in → exit
            # Replaces the old single-shot silence_watchdog with proper 3-stage escalation.
            # The SilenceManager fires on_speak/on_exit callbacks at each stage.
            self._silence_manager.start()
            # Pitch already played — start the silence clock immediately.
            # If prospect doesn't speak within 6s, the nudge fires.
            self._silence_manager.on_ai_done()

            # Start conversation recovery monitoring
            self._recovery.start()
            self._recovery.on_ai_response_end()

            # Max call timer: Hard stop at 180 seconds from Phase 3 start
            # Ensures calls don't run indefinitely — sales calls finish within 3 minutes
            async def max_call_timer():
                MAX_CALL_DURATION_S = 180
                await asyncio.sleep(MAX_CALL_DURATION_S)
                if self._active and self._call_phase == "converse":
                    elapsed_s = time.time() - phase3_start_time
                    logger.info("phase3_max_call_timer_triggered",
                        call_id=self.call_id, elapsed_s=round(elapsed_s, 1))
                    try:
                        closing = "I appreciate your time, have a wonderful day!"
                        pcm_data = await self.orchestrator.tts.synthesize_single(closing)
                        if pcm_data:
                            queued = self._queue_pcm_with_fades(pcm_data)
                            self.orchestrator._conversation_history.append(
                                {"role": "assistant", "content": closing})
                            self._ai_speaking = True
                            self._ai_speaking_started_at = time.time()
                            logger.info("phase3_max_call_closing_sent",
                                call_id=self.call_id, text=closing,
                                audio_bytes=len(pcm_data), frames=queued)
                            # Wait for audio to finish playing before ending
                            await asyncio.sleep(min(3.0, len(pcm_data) / 32000))
                    except Exception as e:
                        logger.warning("phase3_max_call_closing_failed",
                            call_id=self.call_id, error=str(e))
                    self._active = False
                    await self._hangup_twilio()

            max_call_timer_task = asyncio.create_task(max_call_timer())

            # Process continuous STT results
            accumulated_transcript = ""

            async for result in self.orchestrator.stt.transcribe_continuous(
                    audio_feeder()):
                if not self._active:
                    break

                event = result.get("event")

                # ── Speech started: check for barge-in ──
                if event == "speech_started":
                    # Reset silence manager — prospect is speaking
                    # No transcript yet at VAD level, pass empty
                    self._silence_manager.on_speech("")
                    # Notify backchannel manager that prospect started speaking
                    self._backchannel_manager.on_prospect_speech_started()
                    # Cancel any pending delayed silence clock start
                    if hasattr(self, '_silence_clock_task') and self._silence_clock_task:
                        if not self._silence_clock_task.done():
                            self._silence_clock_task.cancel()
                    if not self._phase3_first_speech:
                        self._phase3_first_speech = True
                    # Barge-in when TTS is playing audio AND past grace period.
                    # Offset accounts for queue-to-playback lag (~150ms).
                    if self._ai_speaking:
                        tts_playing_ms = (time.time() - self._ai_speaking_started_at) * 1000
                        # Subtract Twilio playback offset: audio starts playing ~150ms
                        # after we queue it, so real playback time is shorter
                        effective_playing_ms = max(0, tts_playing_ms - self._twilio_playback_offset_ms)
                        if effective_playing_ms < self._barge_in_grace_ms:
                            logger.debug("barge_in_suppressed_grace_period",
                                call_id=self.call_id,
                                tts_playing_ms=round(tts_playing_ms),
                                effective_ms=round(effective_playing_ms))
                            continue

                        logger.info("barge_in_detected", call_id=self.call_id,
                            tts_playing_ms=round(tts_playing_ms),
                            effective_ms=round(effective_playing_ms))
                        # 0a. Record what was spoken before interruption (for context)
                        self._record_interrupted_context()
                        # 0b. Cancel any pending accumulation timer
                        if self._accumulated_timer_task and not self._accumulated_timer_task.done():
                            self._accumulated_timer_task.cancel()
                        # 1. Send Twilio "clear" to stop buffered audio IMMEDIATELY
                        # This is critical — without it, Twilio keeps playing 1-3s
                        # of buffered audio even after we clear our output queue.
                        if self.twilio_telephony:
                            try:
                                await self.twilio_telephony.send_clear(self.call_id)
                            except Exception as e:
                                logger.warning("barge_in_clear_failed",
                                    call_id=self.call_id, error=str(e))
                        # 2. Cancel TTS synthesis
                        try:
                            await self.orchestrator.handle_interruption()
                        except Exception:
                            pass
                        # 3. Clear output queue (our side)
                        cleared = 0
                        while not self._output_queue.empty():
                            try:
                                self._output_queue.get_nowait()
                                cleared += 1
                            except asyncio.QueueEmpty:
                                break
                        # 4. Cancel active LLM+TTS turn task
                        if active_turn_task and not active_turn_task.done():
                            active_turn_task.cancel()
                            try:
                                await active_turn_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            active_turn_task = None
                        self._ai_speaking = False
                        self._ai_speech_ended_at = time.time()
                        # Reset audio conversion state to prevent clicks on next audio
                        if self.twilio_telephony:
                            self.twilio_telephony.reset_audio_state()
                        if cleared > 0:
                            logger.info("barge_in_audio_cleared",
                                call_id=self.call_id, cleared_chunks=cleared)
                    continue

                # ── Utterance end: handled by accumulation timer now ──
                # speech_final triggers the accumulation window (600ms).
                # utterance_end fires 1000ms after speech ends — by then the
                # accumulation timer has already fired the turn. If there's
                # leftover transcript, fold it into the pending timer.
                if event == "utterance_end":
                    if accumulated_transcript.strip():
                        # There's leftover text — start/restart accumulation timer
                        self._phase3_first_speech = True
                        if self._accumulated_timer_task and not self._accumulated_timer_task.done():
                            # Timer already running — it will pick up accumulated_transcript
                            pass
                        else:
                            # No timer running — start one now
                            pending_transcript = accumulated_transcript.strip()
                            accumulated_transcript = ""

                            async def _fire_utterance_turn(ts: str):
                                nonlocal turn_number, active_turn_task, accumulated_transcript
                                try:
                                    await asyncio.sleep(0.3)  # Short wait for any trailing speech
                                except asyncio.CancelledError:
                                    return
                                if accumulated_transcript.strip():
                                    ts += " " + accumulated_transcript.strip()
                                    accumulated_transcript = ""
                                turn_number += 1
                                if turn_number > MAX_PHASE3_TURNS:
                                    self._active = False
                                    await self._hangup_twilio()
                                    return
                                logger.info("phase3_utterance_turn",
                                    call_id=self.call_id, turn=turn_number,
                                    transcript=ts[:100])
                                if active_turn_task and not active_turn_task.done():
                                    active_turn_task.cancel()
                                    try:
                                        await active_turn_task
                                    except (asyncio.CancelledError, Exception):
                                        pass
                                active_turn_task = asyncio.create_task(
                                    self._process_text_turn(ts, turn_number))

                            self._accumulated_timer_task = asyncio.create_task(
                                _fire_utterance_turn(pending_transcript))
                    continue

                # ── Transcript result ──
                text = result.get("text", "")
                is_final = result.get("is_final", False)
                speech_final = result.get("speech_final", False)

                if is_final and text:
                    accumulated_transcript += " " + text
                    # Reset silence manager on any transcript — pass text for hold-on detection
                    self._silence_manager.on_speech(text)
                    # Signal prospect speech to recovery system
                    self._recovery.on_prospect_speech()
                    # Cancel any pending delayed silence clock
                    if hasattr(self, '_silence_clock_task') and self._silence_clock_task:
                        if not self._silence_clock_task.done():
                            self._silence_clock_task.cancel()
                    if not self._phase3_first_speech:
                        self._phase3_first_speech = True

                # speech_final = Deepgram's endpointing triggered (user paused)
                # DON'T trigger LLM immediately — wait for accumulation window.
                # This prevents split-utterance responses ("Yeah" + "I remember"
                # becoming two separate turns instead of one).
                if speech_final and accumulated_transcript.strip():
                    self._phase3_first_speech = True
                    # Notify backchannel manager that prospect finished speaking
                    self._backchannel_manager.on_prospect_speech_ended()

                    # Cancel any pending accumulation timer
                    if self._accumulated_timer_task and not self._accumulated_timer_task.done():
                        self._accumulated_timer_task.cancel()

                    # Capture transcript snapshot for the timer closure
                    pending_transcript = accumulated_transcript.strip()
                    accumulated_transcript = ""

                    async def _fire_turn_after_accumulation(transcript_snapshot: str):
                        """Wait for accumulation window, then fire turn.
                        Includes backchannel detection: if the user only said
                        'mm-hmm', 'ok', 'yeah', etc., skip the LLM turn.
                        Uses BACKCHANNELS imported from silence_manager.
                        Smart pause: waits longer if transcript looks incomplete."""
                        nonlocal turn_number, active_turn_task, accumulated_transcript

                        # Smart accumulation: if transcript ends mid-thought, wait longer
                        clean_end = transcript_snapshot.strip().lower()
                        INCOMPLETE_ENDINGS = ("and", "but", "so", "or", "well", "like",
                                              "the", "a", "to", "for", "with", "is", "was",
                                              "i", "my", "that", "what", "how", "who", "if")
                        last_word = clean_end.split()[-1].rstrip(".,!?") if clean_end.split() else ""
                        wait_ms = self._turn_accumulation_ms
                        if last_word in INCOMPLETE_ENDINGS:
                            wait_ms = 800  # Wait longer for incomplete thoughts

                        try:
                            await asyncio.sleep(wait_ms / 1000)
                        except asyncio.CancelledError:
                            # Timer cancelled — more speech arrived, transcript was re-accumulated
                            return

                        # Check if more speech accumulated during the wait
                        if accumulated_transcript.strip():
                            transcript_snapshot += " " + accumulated_transcript.strip()
                            accumulated_transcript = ""

                        # Backchannel detection: skip LLM if just an acknowledgment
                        # UNLESS it's the first turn (needs a response to the pitch)
                        clean = transcript_snapshot.strip().lower().rstrip(".,!?")
                        if turn_number >= 1 and clean in BACKCHANNELS:
                            logger.info("backchannel_skipped",
                                call_id=self.call_id,
                                transcript=transcript_snapshot,
                                turn=turn_number + 1)
                            # Still add to history so context is preserved
                            self.orchestrator._conversation_history.append(
                                {"role": "user", "content": transcript_snapshot})
                            # Tell silence manager — no AI response coming,
                            # so go to LISTENING (clock running) not BUSY
                            self._silence_manager.on_backchannel()
                            return

                        turn_number += 1

                        # Safety: end call if too many turns
                        if turn_number > MAX_PHASE3_TURNS:
                            logger.warning("phase3_max_turns_reached",
                                call_id=self.call_id, turns=turn_number)
                            try:
                                bye_audio = await self.orchestrator.tts.synthesize_single(
                                    "Thanks for your time, have a great day!")
                                if bye_audio:
                                    self._queue_pcm_with_fades(bye_audio)
                                    await asyncio.sleep(2.0)
                            except Exception:
                                pass
                            self._active = False
                            await self._hangup_twilio()
                            return

                        logger.info("phase3_accumulated_turn",
                            call_id=self.call_id, turn=turn_number,
                            transcript=transcript_snapshot[:100],
                            accumulation_ms=self._turn_accumulation_ms)
                        if active_turn_task and not active_turn_task.done():
                            active_turn_task.cancel()
                            try:
                                await active_turn_task
                            except (asyncio.CancelledError, Exception):
                                pass
                        active_turn_task = asyncio.create_task(
                            self._process_text_turn(transcript_snapshot, turn_number))

                    self._accumulated_timer_task = asyncio.create_task(
                        _fire_turn_after_accumulation(pending_transcript))

            # Cleanup
            self._silence_manager.stop()
            max_call_timer_task.cancel()
            if active_turn_task and not active_turn_task.done():
                try:
                    await asyncio.wait_for(active_turn_task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass

        except Exception as e:
            logger.error("call_loop_error", call_id=self.call_id, error=str(e))

        # Log full conversation summary for debugging
        try:
            history = self.orchestrator._conversation_history
            summary_lines = []
            for msg in history:
                role = msg.get("role", "?")
                content = msg.get("content", "")[:120]
                summary_lines.append(f"  [{role}]: {content}")
            logger.info("call_conversation_summary",
                call_id=self.call_id,
                turns=len(history),
                transfer_triggered=self._transfer_initiated,
                conversation="\n".join(summary_lines))
        except Exception:
            pass

    # ── Silence Manager Callbacks ─────────────────────────────────────

    def _schedule_silence_clock(self, playback_s: float):
        """Start the silence clock AFTER estimated audio playback finishes.

        This is critical: TTS chunks are queued instantly, but Twilio plays
        them at real-time speed. A 5-second response means the prospect
        won't hear the end until ~5s after we queue the last chunk.
        Starting the silence timer immediately would penalize the prospect
        for the AI's own speaking time.

        We add a small buffer (+0.5s) for Twilio network latency.
        """
        delay = playback_s + 0.5  # playback + Twilio buffer
        if hasattr(self, '_silence_clock_task') and self._silence_clock_task:
            if not self._silence_clock_task.done():
                self._silence_clock_task.cancel()

        async def _delayed_ai_done():
            try:
                await asyncio.sleep(delay)
                if self._active and self._call_phase == "converse":
                    self._silence_manager.on_ai_done()
                    self._ai_speaking = False
                    self._ai_speech_ended_at = time.time()
                    logger.debug("silence_clock_started_after_playback",
                        call_id=self.call_id,
                        playback_s=round(playback_s, 1),
                        delay_s=round(delay, 1))
            except asyncio.CancelledError:
                pass  # Cancelled by speech/barge-in — that's fine

        self._silence_clock_task = asyncio.create_task(_delayed_ai_done())

    async def _silence_speak(self, text: str):
        """Called by SilenceManager to speak a nudge phrase.
        Synthesizes TTS audio, queues it with fades, and waits for playback.
        Cancellable: if prospect speaks during synthesis, we abort."""
        try:
            # Check if prospect already started speaking (stage changed from NUDGE_SENT)
            if self._silence_manager._stage == self._silence_manager.BUSY:
                logger.info("silence_nudge_cancelled_before_tts",
                    call_id=self.call_id)
                return

            pcm_data = await self.orchestrator.tts.synthesize_single(text)

            # Check again after TTS synthesis — prospect may have spoken during synthesis
            if self._silence_manager._stage == self._silence_manager.BUSY:
                logger.info("silence_nudge_cancelled_after_tts",
                    call_id=self.call_id)
                return

            if pcm_data:
                queued = self._queue_pcm_with_fades(pcm_data)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": text})
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                logger.info("silence_speak_sent",
                    call_id=self.call_id, text=text,
                    audio_bytes=len(pcm_data), frames=queued)
                # Wait for audio to finish playing
                play_time = len(pcm_data) / 32000
                await asyncio.sleep(play_time)
                self._ai_speaking = False
                self._ai_speech_ended_at = time.time()
        except Exception as e:
            logger.warning("silence_speak_failed",
                call_id=self.call_id, text=text, error=str(e))

    async def _silence_exit(self, text: str):
        """Called by SilenceManager to speak goodbye and end the call.
        Synthesizes farewell audio with fades, plays it, then terminates the call."""
        try:
            pcm_data = await self.orchestrator.tts.synthesize_single(text)
            if pcm_data:
                queued = self._queue_pcm_with_fades(pcm_data)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": text})
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                logger.info("silence_exit_sent",
                    call_id=self.call_id, text=text,
                    audio_bytes=len(pcm_data), frames=queued)
                # Wait for goodbye audio to finish before ending
                play_time = len(pcm_data) / 32000
                await asyncio.sleep(min(4.0, play_time + 0.5))
        except Exception as e:
            logger.warning("silence_exit_speak_failed",
                call_id=self.call_id, text=text, error=str(e))
        # End the call — both stop the bridge AND terminate Twilio call
        self._active = False
        await self._hangup_twilio()
        logger.info("silence_exit_call_ended", call_id=self.call_id)

    async def _recovery_speak(self, text: str, audio_bytes: Optional[bytes] = None):
        """Called by ConversationRecovery to speak a recovery phrase.

        Synthesizes (if no pre-synthesized audio) and queues the audio.
        Pre-synthesized recovery audio is preferred (already synthesized during dial).

        Args:
            text: The recovery phrase text (used if audio_bytes is None)
            audio_bytes: Optional pre-synthesized audio (raw PCM bytes)
        """
        try:
            # Use pre-synthesized audio if available, otherwise synthesize
            if audio_bytes:
                pcm_data = audio_bytes
                logger.info("recovery_speak_using_presynthesized",
                    call_id=self.call_id, text=text[:60],
                    audio_bytes=len(pcm_data))
            else:
                pcm_data = await self.orchestrator.tts.synthesize_single(text)
                logger.info("recovery_speak_synthesizing",
                    call_id=self.call_id, text=text[:60],
                    audio_bytes=len(pcm_data) if pcm_data else 0)

            if pcm_data:
                # Check for double-speak before queuing
                if not self._recovery.on_about_to_play_audio("recovery"):
                    logger.warning("recovery_speak_double_speak",
                        call_id=self.call_id)
                    # Cancel older audio and wait gap
                    await asyncio.sleep(0.1)

                queued = self._queue_pcm_with_fades(pcm_data)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": text})
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()

                logger.info("recovery_speak_sent",
                    call_id=self.call_id, text=text[:60],
                    audio_bytes=len(pcm_data), frames=queued)

                # Wait for audio to finish playing
                play_time = len(pcm_data) / 32000
                await asyncio.sleep(play_time)
                self._ai_speaking = False
                self._ai_speech_ended_at = time.time()
                self._recovery.on_audio_finished()
        except Exception as e:
            logger.warning("recovery_speak_failed",
                call_id=self.call_id, text=text[:60], error=str(e))

    # ── Turn Processing ──────────────────────────────────────────────────

    async def _safe_process_turn(self, audio_data: bytes, turn_number: int):
        """Wrapper with error handling and timeout."""
        try:
            await asyncio.wait_for(
                self._process_orchestrator_turn(audio_data, turn_number),
                timeout=15.0,  # Reduced from 30s — faster timeout
            )
        except asyncio.CancelledError:
            logger.info("turn_cancelled", call_id=self.call_id, turn=turn_number)
        except asyncio.TimeoutError:
            logger.error("turn_timeout", call_id=self.call_id, turn=turn_number)
        except Exception as e:
            logger.error("turn_error", call_id=self.call_id, turn=turn_number,
                error=str(e), error_type=type(e).__name__)

    async def _process_orchestrator_turn(self, audio_data: bytes, turn_number: int = 0):
        """Run one conversation turn through STT→LLM→TTS."""
        t0 = time.time()

        async def audio_iterator() -> AsyncIterator[bytes]:
            chunk_size = 640
            for i in range(0, len(audio_data), chunk_size):
                yield audio_data[i:i + chunk_size]
            logger.info("audio_iter_done",
                call_id=self.call_id, turn=turn_number,
                bytes=len(audio_data))

        response_chunks = 0
        response_bytes = 0

        async def on_audio_chunk(audio: bytes):
            nonlocal response_chunks, response_bytes
            response_chunks += 1
            response_bytes += len(audio)
            if response_chunks <= 3:
                logger.info("tts_chunk",
                    call_id=self.call_id, turn=turn_number,
                    chunk=response_chunks, bytes=len(audio))
            chunk_size = 640
            for i in range(0, len(audio), chunk_size):
                try:
                    await self._output_queue.put(audio[i:i + chunk_size])
                except asyncio.QueueFull:
                    pass

        logger.info("turn_starting",
            call_id=self.call_id, turn=turn_number,
            audio_bytes=len(audio_data),
            llm=type(self.orchestrator.llm).__name__,
            phase=self._call_phase)

        try:
            result = await self.orchestrator.process_turn(
                audio_stream=audio_iterator(),
                config=self.agent_config,
                on_audio_chunk=on_audio_chunk,
            )
        except Exception as e:
            logger.error("turn_exception",
                call_id=self.call_id, turn=turn_number,
                error=str(e), error_type=type(e).__name__)
            return

        elapsed = (time.time() - t0) * 1000
        self._last_output_bytes = response_bytes

        logger.info("turn_complete",
            call_id=self.call_id, turn=turn_number,
            status=result.get("status", "?"),
            transcript=result.get("transcript", "")[:100] or "(none)",
            response=result.get("response", "")[:100] or "(none)",
            audio_chunks=response_chunks,
            audio_bytes=response_bytes,
            total_ms=round(elapsed, 0),
            stt_ms=result.get("stt_latency_ms", 0),
            llm_ttft_ms=result.get("llm_ttft_ms", 0),
            tts_ttfb_ms=result.get("tts_ttfb_ms", 0))

    async def _process_text_turn(self, transcript: str, turn_number: int):
        """
        Process a conversation turn from transcript text.
        Uses STREAMING LLM→TTS pipeline for minimum latency.

        Architecture:
        1. Play a pre-synthesized filler ("Okay", "Right") IMMEDIATELY
        2. Stream LLM text deltas directly into Cartesia TTS (sentence-level)
        3. Stream audio to output as it arrives — TTS starts on FIRST SENTENCE
        This overlaps LLM generation with TTS synthesis, saving 300-800ms per turn.
        """
        t0 = time.time()

        # FREEZE silence clock — AI is generating a response.
        # Without this, LLM+TTS processing time (3-6s) counts as "silence"
        # and the nudge fires while the AI's audio is still playing.
        self._silence_manager.ai_busy()
        # Cancel any pending delayed clock start from previous turn
        if hasattr(self, '_silence_clock_task') and self._silence_clock_task:
            if not self._silence_clock_task.done():
                self._silence_clock_task.cancel()

        # ── LATENCY MASK: Play filler immediately while LLM generates ────
        # Skip filler on turn 1 (first response after pitch should be substantive)
        # and when we have interrupted context (continuation should feel seamless)
        filler_played = False
        if turn_number > 1 and not self._interrupted_context:
            filler_audio = self._get_next_filler()
            if filler_audio:
                # Queue filler audio (tiny: ~200-300ms)
                filler_chunks = self._queue_audio(filler_audio)
                filler_played = True
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                logger.debug("filler_played", call_id=self.call_id,
                    turn=turn_number, chunks=filler_chunks)

        # Add user message to conversation history
        self.orchestrator._conversation_history.append(
            {"role": "user", "content": transcript})

        # ── SENTIMENT ANALYSIS: Analyze prospect emotional state ──────────
        # Perform text-based sentiment detection to adapt response style
        sentiment_result = self._call_state.analyze_prospect_sentiment(transcript)
        if sentiment_result:
            logger.info("sentiment_analyzed",
                call_id=self.call_id, turn=turn_number,
                state=sentiment_result["state"],
                confidence=sentiment_result["confidence"],
                shift=sentiment_result["shift_detected"],
                trend=sentiment_result["trend"],
                signals=sentiment_result["signals"])

            # Check for sustained frustration (2+ consecutive frustrated turns)
            # If detected, trigger graceful exit
            if sentiment_result.get("sustained_frustration"):
                logger.warning("sustained_frustration_detected",
                    call_id=self.call_id, turn=turn_number)
                # Queue graceful exit response
                exit_text = (
                    "I hear you, and I respect your time. "
                    "I'll make a note here so we don't bother you again. "
                    "Have a great day!"
                )
                await self._synthesize_and_queue(exit_text)
                # Add to history
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": exit_text})
                self._call_state.update_from_exchange(transcript, exit_text)
                # Schedule hangup after a brief pause
                asyncio.create_task(self._silence_exit(exit_text))
                return

        # Update recovery system with current call state for fallback responses
        try:
            call_step = self._call_state.current_step
            # Map ScriptStep to CallState enum
            if call_step and hasattr(call_step, 'value'):
                recovery_state = CallState(call_step.value)
                self._recovery._current_call_state = recovery_state
        except (AttributeError, ValueError):
            pass  # Can't convert step, leave as is

        logger.info("text_turn_starting",
            call_id=self.call_id, turn=turn_number,
            transcript=transcript[:100],
            history_len=len(self.orchestrator._conversation_history),
            llm=type(self.orchestrator.llm).__name__,
            filler_played=filler_played)

        # ── TURN 1 RESPONSE CACHE: Bypass LLM for first turn after pitch ──
        # Matches common prospect responses to "Does that ring a bell?" and
        # serves pre-synthesized audio. Eliminates 2000-3000ms LLM latency.
        if turn_number == 1 and self._turn1_cache:
            pattern_key = self._match_turn1_pattern(transcript)
            if pattern_key and pattern_key in self._turn1_cache:
                cached_text, cached_audio = self._turn1_cache[pattern_key]
                if cached_audio:
                    logger.info("turn1_cache_hit",
                        call_id=self.call_id, pattern=pattern_key,
                        transcript=transcript[:80])
                    # Queue cached audio
                    queued = self._queue_audio(cached_audio)
                    self._ai_speaking = True
                    self._ai_speaking_started_at = time.time()
                    self._tts_start_time = time.time()
                    self._current_response_text = cached_text
                    # Signal AI response start to recovery system
                    self._recovery.on_ai_response_start()

                    # Add to conversation history
                    self.orchestrator._conversation_history.append(
                        {"role": "assistant", "content": cached_text})

                    # Update call state tracker with turn 1 exchange
                    self._call_state.update_from_exchange(transcript, cached_text)

                    elapsed = (time.time() - t0) * 1000
                    logger.info("text_turn_complete",
                        call_id=self.call_id, turn=turn_number,
                        transcript=transcript[:200],
                        response=cached_text[:500],
                        response_full=cached_text,
                        response_bytes=len(cached_audio),
                        response_words=len(cached_text.split()),
                        llm_ms=0, llm_ttft_ms=0, tts_ms=0,
                        perceived_latency_ms=round(elapsed, 1),
                        total_ms=round(elapsed, 0),
                        frames_queued=queued,
                        turn1_cache_hit=pattern_key)
                    # Delay silence clock until audio finishes PLAYING on phone
                    # Each frame = 640 bytes = 20ms at 16kHz 16-bit
                    playback_s = queued * 0.02
                    self._schedule_silence_clock(playback_s)
                    return

        # ── TURN 2 CACHE: Serve bank account question instantly ─────────
        # After Turn 1 (interest confirmed), Turn 2 ALWAYS asks about bank account.
        # Use pre-synthesized audio to eliminate LLM + TTS latency entirely.
        if (turn_number == 2
            and self._turn2_bank_audio
            and self._call_state.current_step == ScriptStep.BANK_ACCOUNT):
            cached_text = self.TURN2_BANK_ACCOUNT_TEXT
            cached_audio = self._turn2_bank_audio
            logger.info("turn2_cache_hit",
                call_id=self.call_id, transcript=transcript[:80])
            queued = self._queue_audio(cached_audio)
            self._ai_speaking = True
            self._ai_speaking_started_at = time.time()
            self._tts_start_time = time.time()
            self._current_response_text = cached_text
            # Signal AI response start to recovery system
            self._recovery.on_ai_response_start()
            self.orchestrator._conversation_history.append(
                {"role": "assistant", "content": cached_text})
            self._call_state.update_from_exchange(transcript, cached_text)
            elapsed = (time.time() - t0) * 1000
            logger.info("text_turn_complete",
                call_id=self.call_id, turn=turn_number,
                transcript=transcript[:200],
                response=cached_text[:500],
                response_full=cached_text,
                response_bytes=len(cached_audio),
                response_words=len(cached_text.split()),
                llm_ms=0, llm_ttft_ms=0, tts_ms=0,
                perceived_latency_ms=round(elapsed, 1),
                total_ms=round(elapsed, 0),
                frames_queued=queued,
                turn2_cache_hit=True)
            playback_s = queued * 0.02
            self._schedule_silence_clock(playback_s)
            return

        # Choose LLM (primary or fallback)
        active_llm = self.orchestrator.llm
        if not active_llm.get_health().is_healthy and self.orchestrator.fallback_llm:
            active_llm = self.orchestrator.fallback_llm
            logger.warning("llm_failover", call_id=self.call_id,
                to=active_llm.name)

        # Build effective system prompt with call state + anti-repetition + sentiment
        # Start with full prompt, then trim completed steps to reduce tokens
        effective_system_prompt = self._trim_prompt_for_step(self.agent_config.system_prompt)

        # ── SENTIMENT-BASED RESPONSE ADAPTATION ─────────────────────────
        # Inject sentiment context before other directives for high priority
        sentiment_injection = self._call_state.get_sentiment_prompt_injection()
        if sentiment_injection:
            effective_system_prompt += "\n\n" + sentiment_injection

        # Inject structured call state — gives LLM clear picture of progress
        call_state_block = self._call_state.to_prompt_block()
        effective_system_prompt += "\n\n" + call_state_block

        # Add explicit step directive — tells LLM exactly what to do next
        step = self._call_state.current_step
        if step == ScriptStep.CONFIRM_INTEREST:
            effective_system_prompt += (
                "\n[SYSTEM: You are on Step 1 (Confirm Interest). "
                "Your goal is to get them interested in the offer. "
                "End with a question about whether they want to see the offer.]"
            )
        elif step == ScriptStep.BANK_ACCOUNT:
            effective_system_prompt += (
                "\n[SYSTEM: You are on Step 2 (Bank Account). The prospect is interested. "
                "You MUST ask about their bank account NOW. Ask: do they have a checking or savings account? "
                "Do NOT mention transfer or Sarah yet. End with the bank account question.]"
            )
        elif step == ScriptStep.TRANSFER:
            effective_system_prompt += (
                "\n[SYSTEM: You are on Step 3 (Transfer). Interest confirmed, bank account confirmed. "
                "Now connect them to Sarah. Use the transfer trigger phrase.]"
            )

        # Also add anti-repetition context (just last response — lean)
        anti_repeat = self._make_anti_repeat_instruction()
        if anti_repeat:
            effective_system_prompt += anti_repeat

        # Inject interruption context — tell LLM what was already spoken
        if self._interrupted_context:
            effective_system_prompt += (
                f"\n[SYSTEM: You were INTERRUPTED mid-sentence. The prospect did NOT hear: "
                f'"{self._interrupted_context[:200]}". '
                f"Do NOT repeat what you already said word-for-word. Instead: "
                f"(1) Continue from roughly where you left off using DIFFERENT wording, or "
                f"(2) Briefly acknowledge then continue with the key point they missed. "
                f"Keep it natural — like a real person who got cut off.]"
            )
            self._interrupted_context = ""  # Clear after use

        # Use compressed messages for faster TTFT (fewer tokens in context)
        compressed_messages = self._get_compressed_messages()
        logger.info("llm_context_size",
            call_id=self.call_id,
            raw_msgs=len(self.orchestrator._conversation_history),
            compressed_msgs=len(compressed_messages),
            has_summary=bool(self._context_summary))

        # ── COLLECT LLM TEXT → INTELLIGENT TTS ─────────────────────────
        # Collect all LLM text first (fast: ~200-400ms with max_tokens=60),
        # then apply PROSODIC-AWARE CHUNKING:
        #
        # • SHORT responses (1-2 sentences): Single-shot synthesis
        #   - Cartesia sees complete text → perfect prosody across entire response
        #   - Best for brief answers like "Yes, I'm interested"
        #
        # • LONG responses (3+ sentences): Prosodic chunk synthesis
        #   - Split at linguistic boundaries (sentences, clauses, conjunctions)
        #   - Each chunk: 3-12 words (optimal for prosody planning)
        #   - Cartesia plans prosody within each phrase unit
        #   - Result: Natural intonation without mid-sentence pauses (15-20% improvement)
        #
        # Prosodic chunking research: TTS models plan pitch/stress/rhythm for complete
        # phrases. By chunking at semantic boundaries, we give the model shorter,
        # complete thoughts to synthesize, improving naturalness significantly.
        # The filler audio masks any LLM wait, so quality >> latency savings.

        response_text = ""
        llm_ttft = 0.0
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
        except asyncio.TimeoutError:
            logger.error("text_turn_llm_timeout",
                call_id=self.call_id, turn=turn_number,
                timeout_s=self._recovery.config.llm_timeout_s)
            # Signal timeout to recovery system
            self._recovery.on_llm_timeout(self._call_state.current_step)
            # Use recovery fallback response
            fallback_call_state = CallState(self._call_state.current_step.value) \
                if hasattr(self._call_state.current_step, 'value') \
                else CallState.UNKNOWN
            response_text = self._recovery.get_llm_fallback_text(fallback_call_state)
            logger.info("text_turn_llm_timeout_using_fallback",
                call_id=self.call_id, turn=turn_number,
                response=response_text[:80])
        except Exception as e:
            logger.error("text_turn_llm_error",
                call_id=self.call_id, turn=turn_number, error=str(e))
            # Immediate fallback
            if self.orchestrator.fallback_llm and active_llm != self.orchestrator.fallback_llm:
                logger.warning("llm_immediate_failover",
                    call_id=self.call_id, turn=turn_number,
                    to=self.orchestrator.fallback_llm.name)
                try:
                    async for chunk in self.orchestrator.fallback_llm.generate_stream(
                        messages=compressed_messages,
                        system_prompt=self.agent_config.system_prompt,
                        temperature=self.agent_config.temperature,
                        max_tokens=self.agent_config.max_tokens,
                    ):
                        text = chunk.get("text", "")
                        llm_ttft = chunk.get("ttft_ms", llm_ttft)
                        if text:
                            response_text += text
                        if chunk.get("is_complete"):
                            break
                except Exception as e2:
                    logger.error("text_turn_fallback_llm_error",
                        call_id=self.call_id, turn=turn_number, error=str(e2))
                    # Both LLMs failed — use recovery fallback
                    fallback_call_state = CallState(self._call_state.current_step.value) \
                        if hasattr(self._call_state.current_step, 'value') \
                        else CallState.UNKNOWN
                    response_text = self._recovery.get_llm_fallback_text(fallback_call_state)

        llm_done_ms = (time.time() - t0) * 1000

        if not response_text.strip():
            logger.warning("text_turn_empty_response",
                call_id=self.call_id, turn=turn_number)
            return

        # ── POST-PROCESS: Enforce brevity + trailing question ────────
        response_text = self._enforce_brevity(response_text.strip())

        # ── REPETITION CHECK: Regenerate if reusing phrases ────────
        if self._is_repetition(response_text):
            logger.info("regenerating_due_to_repetition",
                call_id=self.call_id, turn=turn_number,
                original=response_text[:100])
            # Add explicit rephrase instruction and retry ONCE
            rephrase_prompt = effective_system_prompt + (
                f'\n[CRITICAL: Your draft response "{response_text[:120]}" '
                f'REPEATS phrases you already used. You MUST say something '
                f'COMPLETELY DIFFERENT using ENTIRELY NEW words. '
                f'Make the SAME point but with FRESH phrasing. Keep it to '
                f'1 short sentence + 1 question. GO.]'
            )
            retry_text = ""
            try:
                async for chunk in active_llm.generate_stream(
                    messages=compressed_messages,
                    system_prompt=rephrase_prompt,
                    temperature=min(self.agent_config.temperature + 0.2, 1.0),
                    max_tokens=self.agent_config.max_tokens,
                ):
                    text = chunk.get("text", "")
                    if text:
                        retry_text += text
                    if chunk.get("is_complete"):
                        break
            except Exception:
                pass  # Keep original if retry fails
            if retry_text.strip():
                retry_text = self._enforce_brevity(retry_text.strip())
                # Only use retry if it's actually different
                if not self._is_repetition(retry_text):
                    response_text = retry_text
                    logger.info("regeneration_success",
                        call_id=self.call_id, response=retry_text[:100])
                else:
                    logger.warning("regeneration_still_repetitive",
                        call_id=self.call_id)

        # Track current response for interruption context
        self._current_response_text = response_text
        self._tts_start_time = 0.0  # Will be set when first audio frame queued

        # ── SEMANTIC CACHE CHECK (Turns 3+) ──────────────────────────────
        # For turns 3+, attempt to match LLM response to pre-synthesized common patterns
        # using word-overlap similarity (Jaccard). If match confidence >= 0.65, skip TTS
        # and use cached audio (saves ~200-400ms per hit). Still respects anti-repetition.
        #
        # This catches paraphrases: e.g., "Got it, let me get Sarah on the line"
        # matches cached "Let me get Sarah on the line" with ~0.90 similarity.
        semantic_cache_key = None
        semantic_cache_audio = None
        semantic_similarity = 0.0

        if turn_number >= 3 and self._semantic_cache:
            semantic_cache_key, semantic_cache_audio, semantic_similarity = (
                self._semantic_cache.find_best_match(response_text)
            )
            if semantic_cache_audio:
                logger.info("semantic_cache_hit",
                    call_id=self.call_id, turn=turn_number,
                    key=semantic_cache_key, similarity=round(semantic_similarity, 3),
                    generated=response_text[:80])
                # Queue cached audio immediately
                queued = self._queue_audio(semantic_cache_audio)
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                self._tts_start_time = time.time()

                # Update conversation history
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": response_text})
                self._call_state.update_from_exchange(transcript, response_text)

                elapsed = (time.time() - t0) * 1000
                logger.info("text_turn_complete",
                    call_id=self.call_id, turn=turn_number,
                    transcript=transcript[:200],
                    response=response_text[:500],
                    response_full=response_text,
                    response_bytes=len(semantic_cache_audio),
                    response_words=len(response_text.split()),
                    llm_ms=round(llm_done_ms, 1), llm_ttft_ms=round(llm_ttft, 1),
                    tts_ms=0,
                    perceived_latency_ms=round(elapsed, 1),
                    total_ms=round(elapsed, 0),
                    frames_queued=queued,
                    semantic_cache_hit=semantic_cache_key,
                    semantic_similarity=round(semantic_similarity, 3))
                # Delay silence clock until audio finishes playing
                playback_s = queued * 0.02
                self._schedule_silence_clock(playback_s)
                return

        # ── INTELLIGENT TTS METHOD SELECTION ──
        # For short responses (1-2 sentences): single-shot synthesis for best prosody
        # For longer responses (3+ sentences): prosodic chunking for natural intonation
        #
        # Prosodic chunking: splits at linguistic boundaries (sentences, clauses, conjunctions)
        # and synthesizes each chunk separately. This improves naturalness by 15-20% for
        # longer responses by allowing Cartesia to plan prosody over complete phrases.
        active_tts = self.orchestrator.tts
        if not active_tts.get_health().is_healthy and self.orchestrator.fallback_tts:
            active_tts = self.orchestrator.fallback_tts

        # ── APPLY SENTIMENT-BASED SPEED ADJUSTMENT ─────────────────────
        # Modify speech speed based on prospect's emotional state:
        # - FRUSTRATED: 5% slower (calming, more empathetic)
        # - POSITIVE: 3% faster (match energy)
        # - HESITANT: Normal speed (patient, deliberate)
        # - DISENGAGED: Normal speed (consistent, clear)
        speed_adjustment = self._call_state.get_speech_speed_adjustment()
        adjusted_speed = self.agent_config.speed * speed_adjustment
        logger.info("speech_speed_adjusted",
            call_id=self.call_id,
            base_speed=self.agent_config.speed,
            adjustment=round(speed_adjustment, 3),
            adjusted_speed=round(adjusted_speed, 3))

        # Update TTS voice params with sentiment-adjusted speed
        if hasattr(active_tts, 'update_voice_params'):
            active_tts.update_voice_params(speed=adjusted_speed)

        # Decide which synthesis method to use
        use_prosodic = (
            hasattr(active_tts, '_should_use_prosodic_chunking') and
            active_tts._should_use_prosodic_chunking(response_text)
        )
        synthesis_method = "prosodic_chunked" if use_prosodic else "single_shot"
        logger.info("tts_method_selected",
            call_id=self.call_id,
            method=synthesis_method,
            response_length=len(response_text),
            response_words=len(response_text.split()))

        tts_ttfb = 0.0
        response_bytes = 0
        response_chunks = 0
        frame_buffer = bytearray()
        frames_queued = 0

        # PCM frame size: 640 bytes = 20ms at 16kHz 16-bit mono
        PCM_FRAME_SIZE = 640

        # Silence detection for PCM: RMS-based
        _silence_run = 0
        _silence_skip_threshold = 5   # Start skipping after 100ms silence
        _silence_keep_ratio = 0.35    # Keep 35% of silence frames

        try:
            # Use prosodic chunking for longer responses, single-shot for short ones
            if use_prosodic and hasattr(active_tts, 'synthesize_prosodic_streamed'):
                tts_generator = active_tts.synthesize_prosodic_streamed(
                    text=response_text.strip(),
                    voice_id=self.agent_config.voice_id,
                )
            else:
                tts_generator = active_tts.synthesize_single_streamed(
                    text=response_text.strip(),
                    voice_id=self.agent_config.voice_id,
                )

            async for audio_result in tts_generator:
                tts_ttfb = audio_result.get("ttfb_ms", tts_ttfb)
                audio = audio_result.get("audio", b"")

                if not audio:
                    continue

                response_chunks += 1
                response_bytes += len(audio)
                frame_buffer.extend(audio)

                # Emit full 640-byte PCM frames (20ms each)
                while len(frame_buffer) >= PCM_FRAME_SIZE:
                    frame = bytes(frame_buffer[:PCM_FRAME_SIZE])
                    del frame_buffer[:PCM_FRAME_SIZE]

                    # PCM silence compression: check RMS energy
                    n_samp = len(frame) // 2
                    if n_samp > 0:
                        samples = struct.unpack(f'<{n_samp}h', frame)
                        rms = (sum(s * s for s in samples) / n_samp) ** 0.5
                        is_silent = rms < self._silence_threshold_rms
                    else:
                        is_silent = True

                    if is_silent:
                        _silence_run += 1
                        if _silence_run > _silence_skip_threshold:
                            period = max(2, int(1.0 / _silence_keep_ratio))
                            if _silence_run % period != 0:
                                continue  # Skip this silent frame
                    else:
                        _silence_run = 0

                    if frames_queued == 0:
                        # Fade-in on the very first frame to prevent click
                        frame = self._apply_fade(frame, fade_in=True)
                        self._ai_speaking = True
                        self._ai_speaking_started_at = time.time()
                        self._tts_start_time = time.time()

                    frames_queued += 1
                    try:
                        await self._output_queue.put(frame)
                    except asyncio.QueueFull:
                        pass

            # Flush remaining partial frame (pad with silence to full frame)
            if frame_buffer:
                remaining = bytes(frame_buffer)
                if len(remaining) < PCM_FRAME_SIZE:
                    remaining = remaining + b'\x00' * (PCM_FRAME_SIZE - len(remaining))
                if frames_queued == 0:
                    remaining = self._apply_fade(remaining, fade_in=True)
                    self._ai_speaking = True
                    self._ai_speaking_started_at = time.time()
                # Fade-out on the final frame
                remaining = self._apply_fade(remaining, fade_out=True)
                try:
                    await self._output_queue.put(remaining)
                except asyncio.QueueFull:
                    pass
                frames_queued += 1
            elif frames_queued > 0:
                # Stream ended without a partial frame — add a fade-out tail
                # Queue a short silence frame with fade to smooth the ending
                tail = b'\x00' * PCM_FRAME_SIZE
                try:
                    await self._output_queue.put(tail)
                except asyncio.QueueFull:
                    pass
                frames_queued += 1

            if response_chunks == 0:
                logger.warning("text_turn_tts_empty",
                    call_id=self.call_id, turn=turn_number)

        except Exception as e:
            tts_ttfb = 0.0
            response_chunks = 0
            response_bytes = 0
            logger.error("text_turn_tts_error",
                call_id=self.call_id, turn=turn_number,
                error=str(e), error_type=type(e).__name__)

            # Signal TTS failure to recovery system
            self._recovery.on_tts_failure(str(e))

            # TTS failure recovery chain:
            # 1. Try simplified text (shorter, no special chars)
            # 2. Try semantic cache if available
            # 3. Use generic fallback
            simplified_text = self._recovery.simplify_text_for_tts_retry(response_text)
            if simplified_text and simplified_text != response_text:
                logger.info("text_turn_tts_retry_simplified",
                    call_id=self.call_id, turn=turn_number,
                    original=response_text[:60],
                    simplified=simplified_text[:60])
                try:
                    # Retry with simplified text
                    if use_prosodic and hasattr(active_tts, 'synthesize_prosodic_streamed'):
                        tts_generator = active_tts.synthesize_prosodic_streamed(
                            text=simplified_text.strip(),
                            voice_id=self.agent_config.voice_id,
                        )
                    else:
                        tts_generator = active_tts.synthesize_single_streamed(
                            text=simplified_text.strip(),
                            voice_id=self.agent_config.voice_id,
                        )

                    async for audio_result in tts_generator:
                        tts_ttfb = audio_result.get("ttfb_ms", tts_ttfb)
                        audio = audio_result.get("audio", b"")
                        if not audio:
                            continue
                        response_chunks += 1
                        response_bytes += len(audio)
                        frame_buffer.extend(audio)
                        while len(frame_buffer) >= PCM_FRAME_SIZE:
                            frame = bytes(frame_buffer[:PCM_FRAME_SIZE])
                            del frame_buffer[:PCM_FRAME_SIZE]
                            try:
                                await self._output_queue.put(frame)
                                frames_queued += 1
                            except asyncio.QueueFull:
                                pass
                    self._recovery.metrics.tts_retries_succeeded += 1
                    logger.info("text_turn_tts_retry_succeeded",
                        call_id=self.call_id, turn=turn_number)
                except Exception as e2:
                    logger.warning("text_turn_tts_retry_also_failed",
                        call_id=self.call_id, turn=turn_number, error=str(e2))
                    response_chunks = 0
                    response_bytes = 0

            # If retry failed or wasn't possible, try semantic cache or generic fallback
            if response_chunks == 0:
                # Try semantic cache as fallback audio
                if turn_number >= 3 and self._semantic_cache:
                    semantic_key, semantic_audio, similarity = (
                        self._semantic_cache.find_best_match(response_text))
                    if semantic_audio and similarity >= 0.65:
                        logger.info("text_turn_tts_using_semantic_cache_fallback",
                            call_id=self.call_id, turn=turn_number,
                            key=semantic_key, similarity=round(similarity, 3))
                        self._recovery.metrics.tts_cache_fallbacks += 1
                        response_bytes = len(semantic_audio)
                        frames_queued = self._queue_audio(semantic_audio)
                    else:
                        # Generic fallback
                        logger.warning("text_turn_tts_using_generic_fallback",
                            call_id=self.call_id, turn=turn_number)
                        fallback_text = self._recovery.get_tts_fallback_text()
                        try:
                            fallback_audio = await self.orchestrator.tts.synthesize_single(
                                fallback_text)
                            if fallback_audio:
                                response_bytes = len(fallback_audio)
                                frames_queued = self._queue_audio(fallback_audio)
                                response_text = fallback_text  # Use fallback as response
                        except Exception as e3:
                            logger.error("text_turn_tts_generic_fallback_failed",
                                call_id=self.call_id, turn=turn_number, error=str(e3))
                else:
                    # Generic fallback (no semantic cache)
                    logger.warning("text_turn_tts_using_generic_fallback",
                        call_id=self.call_id, turn=turn_number)
                    fallback_text = self._recovery.get_tts_fallback_text()
                    try:
                        fallback_audio = await self.orchestrator.tts.synthesize_single(
                            fallback_text)
                        if fallback_audio:
                            response_bytes = len(fallback_audio)
                            frames_queued = self._queue_audio(fallback_audio)
                            response_text = fallback_text  # Use fallback as response
                    except Exception as e3:
                        logger.error("text_turn_tts_generic_fallback_failed",
                            call_id=self.call_id, turn=turn_number, error=str(e3))

        finally:
            # ── RESTORE ORIGINAL SPEECH SPEED ──────────────────────────
            # Reset TTS voice params to original speed after synthesis completes
            if hasattr(active_tts, 'update_voice_params'):
                active_tts.update_voice_params(speed=self.agent_config.speed)

        # Update state
        self._last_output_bytes = response_bytes

        # Clear interruption tracking vars (TTS completed without interruption)
        self._current_response_text = ""
        self._tts_start_time = 0.0

        # Check for repetition before adding to history
        is_repeat = self._is_repetition(response_text) if response_text else False

        if response_text:
            self.orchestrator._conversation_history.append(
                {"role": "assistant", "content": response_text})

        # Update structured call state tracker — tracks script progress,
        # collected info, and questions asked to prevent repetition
        if response_text:
            self._call_state.update_from_exchange(transcript, response_text)

        # Trigger background context compression (keeps history lean for fast TTFT)
        self._trigger_background_compression()

        elapsed = (time.time() - t0) * 1000
        perceived_latency = round(llm_done_ms + tts_ttfb, 1)  # What user actually waits
        logger.info("text_turn_complete",
            is_repetition=is_repeat,
            call_id=self.call_id, turn=turn_number,
            transcript=transcript[:200],
            response=response_text[:500],
            response_full=response_text,  # Full text for grading
            response_bytes=response_bytes,
            response_words=len(response_text.split()) if response_text else 0,
            llm_ms=round(llm_done_ms, 1),
            llm_ttft_ms=round(llm_ttft, 1),
            tts_ms=round(tts_ttfb, 1),
            perceived_latency_ms=perceived_latency,
            total_ms=round(elapsed, 0),
            frames_queued=frames_queued)

        # Delay silence clock until audio finishes PLAYING on the phone.
        # on_ai_done() fires when TTS chunks are done being QUEUED, but the
        # audio still needs to play through Twilio's buffer (~50 frames/sec).
        # Without this delay, the 6s silence timer starts while the prospect
        # is still listening to the AI — causing premature nudge/exit.
        playback_s = frames_queued * 0.02  # 20ms per 640-byte PCM frame
        self._schedule_silence_clock(playback_s)

        # Signal AI response end to recovery system
        if response_text:
            self._recovery.on_ai_response_end()

        # ── Transfer detection ──
        # Check if the LLM response signals a transfer to a licensed agent.
        if response_text and not self._transfer_initiated:
            response_lower = response_text.lower()
            for trigger in self.TRANSFER_TRIGGERS:
                if trigger in response_lower:
                    self._transfer_initiated = True
                    logger.info("transfer_trigger_detected",
                        call_id=self.call_id, trigger=trigger,
                        response=response_text[:100])
                    asyncio.create_task(self._initiate_transfer())
                    break

    async def _initiate_transfer(self):
        """
        Initiate TRUE warm transfer to licensed agent.

        Flow:
        1. Dial agent in background with Gather TwiML (press 1 to accept)
        2. AI keeps talking hold phrases to prospect via media stream
        3. When agent presses 1 → agent joins a conference room
        4. AI says "Sarah is joining us now" to prospect
        5. Prospect's call is updated to join the conference with warm intro
        6. Both parties connected — AI exits
        """
        from .warm_transfer import WarmTransferManager, TransferConfig
        from .transfer_endpoints import register_transfer_manager, update_conference_mapping

        transfer_cfg_raw = getattr(self.agent_config, 'transfer_config', None)
        if not transfer_cfg_raw:
            transfer_cfg_raw = {}

        # Build TransferConfig from agent config
        config = TransferConfig(
            agent_dids=transfer_cfg_raw.get("agent_dids", ["+19048404634"]),
            ring_timeout_seconds=transfer_cfg_raw.get("ring_timeout_seconds", 20),
            max_hold_time_seconds=transfer_cfg_raw.get("max_hold_time_seconds", 90),
            max_agent_retries=transfer_cfg_raw.get("max_agent_retries", 2),
            record_conference=transfer_cfg_raw.get("record_conference", True),
            machine_detection=transfer_cfg_raw.get("machine_detection", True),
            whisper_enabled=transfer_cfg_raw.get("whisper_enabled", True),
            callback_enabled=transfer_cfg_raw.get("callback_enabled", True),
            caller_id="+13187222561",  # Our Twilio number
        )

        self._transfer_manager = WarmTransferManager(
            config=config,
            twilio_client=self.twilio_client,
        )

        # Register in the global transfer registry so webhooks can find it
        register_transfer_manager(self.call_id, self._transfer_manager)

        # Pause silence management during transfer hold
        self._silence_manager.pause()

        logger.info("transfer_initiating",
            call_id=self.call_id,
            call_sid=self.twilio_call_sid,
            agent_dids=config.agent_dids,
            has_twilio=self.twilio_client is not None,
            webhook_base_url=self.webhook_base_url or "https://wellheard.ai")

        if not self.twilio_client:
            logger.error("transfer_no_twilio_client",
                call_id=self.call_id,
                msg="Cannot perform real transfer — twilio_client is None. Transfer will be simulated only.")

        if not self.twilio_call_sid:
            logger.error("transfer_no_call_sid",
                call_id=self.call_id,
                msg="Cannot move prospect — twilio_call_sid is empty.")

        try:
            # Pre-synthesize hold audio during dial time (non-blocking)
            # This ensures audio is ready instantly when transfer starts
            if self.orchestrator.tts and self._transfer_manager:
                try:
                    presynth_count = await self._transfer_manager.presynthesize_hold_audio(
                        synthesize_fn=self.orchestrator.tts.synthesize_single,
                    )
                    logger.info("transfer_hold_audio_presynth",
                        call_id=self.call_id, count=presynth_count)
                except Exception as e:
                    logger.warning("transfer_hold_audio_presynth_failed",
                        call_id=self.call_id, error=str(e))

            # Start agent dial in background (non-blocking)
            await self._transfer_manager.initiate_transfer(
                prospect_call_sid=self.twilio_call_sid,
                contact_name=self.prospect_name or "",
                last_name="",
                call_id=self.call_id,
                webhook_base_url=self.webhook_base_url or "https://wellheard.ai",
            )
            logger.info("transfer_initiated_ok",
                call_id=self.call_id,
                state=str(self._transfer_manager.state),
                conference=self._transfer_manager._conference_name)

            # Update conference mapping now that conference name is set
            if self._transfer_manager._conference_name:
                update_conference_mapping(self.call_id, self._transfer_manager._conference_name)

            # Start hold audio + warm handoff loop in background
            asyncio.create_task(self._transfer_hold_loop())

        except Exception as e:
            logger.error("transfer_initiation_failed",
                call_id=self.call_id, error=str(e),
                error_type=type(e).__name__,
                twilio_client_present=self.twilio_client is not None,
                call_sid=self.twilio_call_sid)

    async def _transfer_hold_loop(self):
        """
        Background loop during transfer:
        - Check every 0.5s if agent accepted (responsive handoff)
        - Play pre-synthesized hold audio at intervals while waiting
        - If all agents fail, offer callback

        CRITICAL: Must be responsive — when agent accepts, handoff
        should happen within 1 second, not after a long wait.

        Pre-synthesized hold audio provides:
        - Zero TTS latency (audio ready during dial time)
        - Natural intervals (8-12 seconds apart)
        - Seamless reassuring messaging about the agent
        - Fallback to on-demand synthesis if queue exhausted
        """
        from .inbound_handler import TRANSFER_AGENT_NAME
        mgr = self._transfer_manager
        hold_audio_interval = mgr.config.hold_audio_interval_seconds
        last_audio_time = time.time()
        first_audio_played = False

        logger.info("transfer_hold_loop_started", call_id=self.call_id,
            hold_audio_queue_size=mgr._hold_audio_queue.get_metrics()["queue_size"])

        while self._active and self._transfer_manager:
            mgr = self._transfer_manager

            # ── Priority 1: Check if agent accepted ──
            if mgr._agent_accepted.is_set():
                logger.info("transfer_agent_accepted", call_id=self.call_id,
                    hold_elapsed=round(mgr.hold_elapsed, 1))
                await self._perform_warm_handoff()
                return

            # ── Priority 2: Check for failure ──
            if mgr.is_failed:
                logger.info("transfer_failed_offering_callback", call_id=self.call_id)
                fallback = mgr.get_fallback_phrase()
                await self._synthesize_and_queue(fallback)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": fallback})
                # Allow conversation to continue — prospect can respond
                self._transfer_initiated = False
                return

            # ── Priority 3: Check max hold time ──
            if mgr.hold_elapsed > mgr.config.max_hold_time_seconds:
                logger.warning("transfer_max_hold_exceeded", call_id=self.call_id,
                    elapsed=round(mgr.hold_elapsed, 1))
                fallback = mgr.get_fallback_phrase()
                await self._synthesize_and_queue(fallback)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": fallback})
                # Allow conversation to continue — prospect can respond
                self._transfer_initiated = False
                return

            # ── Play pre-synthesized hold audio at intervals ──
            time_since_audio = time.time() - last_audio_time
            if time_since_audio >= hold_audio_interval or not first_audio_played:
                # Try pre-synthesized hold audio first (zero TTS latency)
                hold_audio_item = mgr.get_next_hold_audio()
                if hold_audio_item and hold_audio_item.audio_bytes and self._active:
                    first_audio_played = True
                    last_audio_time = time.time()
                    logger.info("transfer_hold_audio_playing",
                        call_id=self.call_id,
                        text=hold_audio_item.text[:60],
                        audio_size=len(hold_audio_item.audio_bytes))
                    self._queue_audio(hold_audio_item.audio_bytes)
                    self.orchestrator._conversation_history.append(
                        {"role": "assistant", "content": hold_audio_item.text})
                else:
                    # Fallback: use on-demand hold phrases if queue exhausted
                    phrase = mgr.get_next_hold_phrase()
                    if phrase and self._active:
                        first_audio_played = True
                        last_audio_time = time.time()
                        logger.info("transfer_hold_phrase",
                            call_id=self.call_id,
                            phrase_idx=mgr._hold_phrase_index,
                            phrase=phrase[:60])
                        await self._synthesize_and_queue(phrase)
                        self.orchestrator._conversation_history.append(
                            {"role": "assistant", "content": phrase})

            # ── Short wait — check agent acceptance frequently ──
            try:
                await asyncio.wait_for(
                    mgr._agent_accepted.wait(),
                    timeout=0.5,  # Check every 500ms for responsive handoff
                )
                # Agent accepted!
                logger.info("transfer_agent_accepted_quick", call_id=self.call_id,
                    hold_elapsed=round(mgr.hold_elapsed, 1))
                await self._perform_warm_handoff()
                return
            except asyncio.TimeoutError:
                pass  # Keep looping

        logger.info("transfer_hold_loop_ended", call_id=self.call_id)

    async def _perform_warm_handoff(self):
        """
        Agent has accepted the transfer. Execute the warm handoff FAST:
        1. AI says a SHORT, contextual message via media stream
        2. IMMEDIATELY move prospect into conference — no waiting for audio to finish
        3. Agent is already in conference — instant connection

        CRITICAL: Speed matters. Agent is waiting in an empty conference.
        Every second of delay = agent hearing hold music. Target: <2s total.

        Handoff message:
        "Great news — Sarah's here now. Sarah, I've got [prospect name] on the line,
         they're interested in the preferred offer. [Prospect name], you're in great hands!"

        This gives the agent context AND reassures the prospect.
        """
        from .inbound_handler import TRANSFER_AGENT_NAME
        mgr = self._transfer_manager

        # Step 1: Queue a contextual pre-transfer message
        # Gives agent context about the prospect while reassuring them
        prospect_name = self.prospect_name or "you"
        if prospect_name.lower() == "you":
            # If no name extracted, use simpler message
            pre_transfer_msg = (
                f"Great news — {TRANSFER_AGENT_NAME}'s here now. "
                f"You're in great hands!"
            )
        else:
            # Personalized message that gives agent context
            pre_transfer_msg = (
                f"Great news — {TRANSFER_AGENT_NAME}'s here now. "
                f"{TRANSFER_AGENT_NAME}, I've got {prospect_name} on the line, "
                f"they're interested in the preferred offer. "
                f"{prospect_name}, you're in great hands!"
            )

        logger.info("warm_handoff_start",
            call_id=self.call_id, prospect_name=prospect_name, msg=pre_transfer_msg[:60])

        # Fire-and-forget: synthesize and queue (plays in background while we move prospect)
        try:
            audio = await self.orchestrator.tts.synthesize_single(
                text=pre_transfer_msg, voice_id=self.agent_config.voice_id)
            if audio:
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                self._queue_audio(audio)
                self.orchestrator._conversation_history.append(
                    {"role": "assistant", "content": pre_transfer_msg})
        except Exception as e:
            logger.warning("warm_handoff_tts_failed", error=str(e))

        # Step 2: Brief pause to let the "connecting you now" audio play.
        # The TwiML update disconnects the media stream, so the prospect
        # needs to hear the transition phrase first. 1.5s is enough for
        # a short phrase without leaving the agent waiting too long.
        await asyncio.sleep(1.5)

        logger.info("warm_handoff_moving_prospect",
            call_id=self.call_id,
            conference=mgr._conference_name)

        try:
            await mgr.move_prospect_to_conference(
                self.webhook_base_url or "https://wellheard.ai")
        except Exception as e:
            logger.error("warm_handoff_move_failed",
                call_id=self.call_id, error=str(e))
            await self._synthesize_and_queue(
                "I'm having trouble connecting right now. Can I have the agent call you back?")
            # Allow conversation to continue
            self._transfer_initiated = False
            return

        logger.info("warm_handoff_complete",
            call_id=self.call_id,
            hold_elapsed=round(mgr.hold_elapsed, 1))

        # Stop Becky's conversation after successful handoff.
        # The prospect is now talking to the licensed agent.
        # Give a brief delay for audio to flush, then deactivate.
        await asyncio.sleep(1.0)
        self._active = False
        logger.info("bridge_deactivated_after_transfer", call_id=self.call_id)

    def _check_voicemail_pattern(self, speech_duration_ms: int, silence_gaps: list) -> bool:
        """
        Detect voicemail/IVR patterns based on timing heuristics.
        
        Voicemail signatures:
        - Continuous speech >6 seconds (long automated greeting)
        - Pattern: long speech → silence (beep) → more speech
        - No natural back-and-forth like human greeting
        
        Args:
            speech_duration_ms: Total continuous speech duration in milliseconds
            silence_gaps: List of silence durations detected
        
        Returns:
            True if pattern matches voicemail/IVR, False if likely human
        """
        # Heuristic 1: >6 second continuous speech = automated message
        if speech_duration_ms > 6000:
            logger.info("voicemail_detected_long_speech",
                call_id=self.call_id, duration_ms=speech_duration_ms)
            return True
        
        # Heuristic 2: Silence pattern matching voicemail
        # Typical voicemail: greeting (3-5s) → silence (0.5-1s) → beep/prompt
        if silence_gaps:
            # Check for long silence gaps (beep detection proxy)
            long_silences = [s for s in silence_gaps if s > 2000]  # >2 seconds silence
            if long_silences:
                logger.info("voicemail_detected_silence_pattern",
                    call_id=self.call_id, silence_gaps=silence_gaps)
                return True
        
        return False

    async def _synthesize_and_queue(self, text: str):
        """Synthesize text and queue for output (single-shot, no pauses)."""
        try:
            audio_bytes = await self.orchestrator.tts.synthesize_single(
                text=text, voice_id=self.agent_config.voice_id)
            if audio_bytes:
                self._ai_speaking = True
                self._ai_speaking_started_at = time.time()
                self._queue_audio(audio_bytes)
        except Exception as e:
            logger.error("synthesis_error", call_id=self.call_id, error=str(e))
