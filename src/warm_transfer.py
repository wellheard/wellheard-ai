"""
WellHeard AI — True Warm Transfer Manager (DTMF Accept + Conference)

Architecture for seamless warm transfers:
1. Transfer triggers → AI continues talking to prospect (no transfer tone)
2. Agent is dialed in background with Gather TwiML (press 1 to accept)
3. Agent presses 1 → joins a conference room (waiting for prospect)
4. AI says "Sarah is joining now" to prospect via media stream
5. Prospect's call is updated to join the conference with warm intro
6. Prospect + agent connected — AI exits gracefully

Key improvements over cold transfer:
- Prospect NEVER hears hold music or transfer tone
- Agent must actively accept (press 1) — no dropped blind transfers
- AI fills the wait with natural conversation (pre-synthesized hold audio queue)
- Warm intro makes the handoff feel personal and professional
- Machine detection catches agent voicemail
- Pre-synthesized hold audio ready during dial time — instant playback with no TTS latency
"""
import asyncio
import time
import uuid
import structlog
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

logger = structlog.get_logger()


# ── Hold Audio Queue ──────────────────────────────────────────────────────
# Pre-synthesized hold audio played at natural intervals during transfer wait.
# Reduces TTS latency and improves experience with seamless, reassuring messaging.

@dataclass
class HoldAudioItem:
    """One pre-synthesized hold message with its audio bytes."""
    text: str
    audio_bytes: Optional[bytes] = None
    synthesized_at: float = field(default_factory=time.time)


class HoldAudioQueue:
    """
    Queue of pre-synthesized hold audio pieces.

    Pre-synthesizes 3-4 hold messages during dial time so they're ready instantly
    when the transfer starts. Plays them at natural intervals (8-12 seconds apart)
    while the prospect waits for the agent to pick up.

    Benefits:
    - No TTS latency during transfer hold (audio already synthesized)
    - Fills silence with reassuring, contextual messaging
    - Can still respond to prospect interruptions
    - Customizable for agent names and offer context
    """

    def __init__(self):
        self._queue: List[HoldAudioItem] = []
        self._current_index: int = 0
        self._synth_lock = asyncio.Lock()
        self._presynth_count: int = 0

    async def presynthesize_hold_audio(
        self,
        hold_texts: List[str],
        synthesize_fn: Any,
        agent_name: str = "Sarah",
    ) -> int:
        """
        Pre-synthesize hold audio for all provided texts.
        Called during dial time (before transfer starts).

        Returns count of successfully synthesized items.
        """
        async with self._synth_lock:
            count = 0
            for text in hold_texts:
                try:
                    # Personalize with agent name if placeholders exist
                    personalized_text = text.replace("{agent_name}", agent_name)

                    # Synthesize
                    audio = await synthesize_fn(personalized_text)
                    if audio:
                        item = HoldAudioItem(text=personalized_text, audio_bytes=audio)
                        self._queue.append(item)
                        count += 1
                        logger.debug("hold_audio_presynth",
                            text=text[:50], size=len(audio))

                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.15)
                except Exception as e:
                    logger.warning("hold_audio_presynth_failed",
                        text=text[:50], error=str(e))

            self._presynth_count = count
            logger.info("hold_audio_presynthesize_complete",
                count=count, total=len(hold_texts))
            return count

    def get_next_audio(self) -> Optional[HoldAudioItem]:
        """Get next pre-synthesized hold audio item (or None if queue empty/exhausted)."""
        if self._current_index < len(self._queue):
            item = self._queue[self._current_index]
            self._current_index += 1
            return item
        return None

    def has_more(self) -> bool:
        """Check if there are more pre-synthesized items available."""
        return self._current_index < len(self._queue)

    def reset(self) -> None:
        """Reset queue for reuse."""
        self._current_index = 0

    def get_metrics(self) -> Dict:
        """Return queue metrics."""
        return {
            "presynth_count": self._presynth_count,
            "queue_size": len(self._queue),
            "current_index": self._current_index,
            "items_remaining": len(self._queue) - self._current_index,
        }


# ── Transfer States ──────────────────────────────────────────────────────

class TransferState(str, Enum):
    """State machine for warm transfer."""
    IDLE = "idle"                        # No transfer in progress
    DIALING_AGENT = "dialing_agent"      # Ringing the licensed agent
    AGENT_RINGING = "agent_ringing"      # Agent phone is ringing
    AGENT_ACCEPTED = "agent_accepted"    # Agent pressed 1 (in conference, waiting)
    MOVING_PROSPECT = "moving_prospect"  # Updating prospect TwiML to join conference
    CONNECTED = "connected"              # Prospect + agent in conference
    MONITORING = "monitoring"            # Post-transfer quality check (30s)
    TRANSFERRED = "transferred"          # Qualified transfer confirmed
    FAILED_RETRY = "failed_retry"        # Primary agent failed, trying backup
    FAILED = "failed"                    # All agents failed
    CALLBACK_OFFERED = "callback"        # Offered prospect a callback


class TransferFailReason(str, Enum):
    AGENT_NO_ANSWER = "agent_no_answer"
    AGENT_BUSY = "agent_busy"
    AGENT_VOICEMAIL = "agent_voicemail"
    AGENT_DECLINED = "agent_declined"    # Didn't press 1
    PROSPECT_HANGUP = "prospect_hangup"
    CONFERENCE_ERROR = "conference_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"


# ── Transfer Configuration ───────────────────────────────────────────────

@dataclass
class TransferConfig:
    """Production transfer configuration."""
    # Agent pool — ordered by priority. First = primary, rest = failover.
    agent_dids: List[str] = field(default_factory=lambda: ["+19048404634"])

    # Timeouts
    ring_timeout_seconds: int = 20        # How long to ring each agent
    max_hold_time_seconds: int = 90       # Absolute max before fallback
    post_transfer_monitor_seconds: int = 30  # Monitor after bridge

    # Retry
    max_agent_retries: int = 2            # Total agents to try

    # Features
    record_conference: bool = True        # Dual-channel recording
    machine_detection: bool = True        # Catch agent voicemail
    whisper_enabled: bool = True          # Brief agent before connecting
    callback_enabled: bool = True         # Offer callback if all fail

    # Caller ID shown to agent
    caller_id: str = ""

    # Hold-line phrases — what AI says while waiting for agent
    hold_phrases: list = field(default_factory=lambda: [
        "I'm seeing a preferred discounted offer attached to your profile "
        "that reflects the best pricing available today based on your age and health. "
        "That pricing window is expiring soon, so we want to make sure "
        "the agent reviews it with you before it updates.",

        "The main thing with whole life insurance is making sure you have "
        "the right coverage and the right beneficiary so the money goes "
        "exactly where you want. The agent will walk you through all of that.",

        "Just so you know, when the agent joins there might be "
        "a quick moment as they jump in. As soon as you hear them, "
        "just let them know you're there and they'll take great care of you.",
    ])

    hold_fillers: list = field(default_factory=lambda: [
        "The agent should be joining us any moment now.",
        "I appreciate your patience, they're finishing up.",
        "Just a few more seconds.",
    ])

    # Fallback phrases
    fallback_phrase: str = (
        "I apologize, it looks like our agent is helping another client right now. "
        "Can I have them call you back within the next 15 minutes?"
    )

    callback_confirm_phrase: str = (
        "Perfect, I'll make sure someone calls you back shortly. "
        "Thank you for your time today!"
    )

    # Hold phrase pause between repeats
    hold_phrase_pause: int = 8

    # Pre-synthesized hold audio queue configuration
    # High-quality, short hold messages that run during transfer wait
    hold_audio_pieces: list = field(default_factory=lambda: [
        "Sarah's going to love helping you with this. She's one of our best.",
        "While we wait, just so you know — there's zero obligation on this call.",
        "Sarah will be able to show you exact numbers based on your age and situation.",
        "Almost there. Sarah specializes in finding the most affordable coverage.",
    ])

    # Interval between hold audio playback (seconds)
    hold_audio_interval_seconds: int = 9  # 8-12s sweet spot


# ── Warm Transfer Manager ────────────────────────────────────────────────

class WarmTransferManager:
    """
    True warm transfer with DTMF acceptance.

    Architecture:
    ┌──────────┐     media stream      ┌──────────┐
    │ Prospect │◄──────────────────────│   AI     │ (keeps talking)
    └──────────┘                       └──────────┘
         │                                  │
         │ (later: TwiML update)            │ (detects agent_accepted)
         ▼                                  │
    ┌──────────────┐     phone call    ┌──────────┐
    │  Conference  │◄──────────────────│  Agent   │ (pressed 1)
    │   (bridge)   │                   │  (DTMF)  │
    └──────────────┘                   └──────────┘
    """

    def __init__(self, config: Optional[TransferConfig] = None,
                 twilio_client=None):
        self.config = config or TransferConfig()
        self._twilio = twilio_client
        self.state = TransferState.IDLE

        # Conference tracking
        self._conference_name: str = ""
        self._conference_sid: str = ""
        self._prospect_call_sid: str = ""
        self._agent_call_sid: str = ""

        # Timing
        self._start_time: float = 0
        self._agent_accept_time: float = 0
        self._prospect_joined_time: float = 0

        # Hold phrase tracking
        self._hold_phrase_index: int = 0

        # Pre-synthesized hold audio queue
        self._hold_audio_queue = HoldAudioQueue()

        # Agent retry tracking
        self._agent_attempt: int = 0
        self._current_agent_did: str = ""

        # Events
        self._agent_accepted = asyncio.Event()  # Agent pressed 1
        self._prospect_joined = asyncio.Event()  # Prospect in conference
        self._transfer_verified = asyncio.Event()

        # Call context
        self._contact_name: str = ""
        self._last_name: str = ""
        self._call_id: str = ""
        self._agent_name: str = "Sarah"  # For personalization in hold audio

        # Metrics
        self._fail_reasons: List[str] = []
        self._events_log: List[Dict] = []

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_transferring(self) -> bool:
        return self.state in (
            TransferState.DIALING_AGENT,
            TransferState.AGENT_RINGING,
            TransferState.AGENT_ACCEPTED,
            TransferState.MOVING_PROSPECT,
            TransferState.FAILED_RETRY,
        )

    @property
    def is_transferred(self) -> bool:
        return self.state in (TransferState.TRANSFERRED, TransferState.CONNECTED)

    @property
    def is_monitoring(self) -> bool:
        return self.state == TransferState.MONITORING

    @property
    def is_failed(self) -> bool:
        return self.state in (TransferState.FAILED, TransferState.CALLBACK_OFFERED)

    @property
    def hold_elapsed(self) -> float:
        return time.time() - self._start_time if self._start_time else 0

    # ── Main Entry Point ─────────────────────────────────────────────

    async def initiate_transfer(
        self,
        prospect_call_sid: str,
        contact_name: str = "",
        last_name: str = "",
        call_id: str = "",
        webhook_base_url: str = "",
    ) -> None:
        """
        Start the warm transfer by dialing the agent in the background.
        The prospect stays on the media stream — no transfer tone.
        """
        if self.state != TransferState.IDLE:
            logger.warning("transfer_already_in_progress", state=self.state)
            return

        self._prospect_call_sid = prospect_call_sid
        self._contact_name = contact_name
        self._last_name = last_name
        self._call_id = call_id or str(uuid.uuid4())
        self._start_time = time.time()
        self._hold_phrase_index = 0
        self._agent_attempt = 0
        self._conference_name = f"transfer-{self._call_id}"

        # Clear events
        self._agent_accepted.clear()
        self._prospect_joined.clear()
        self._transfer_verified.clear()

        logger.info("warm_transfer_initiated",
            call_id=call_id,
            conference=self._conference_name,
            prospect_sid=prospect_call_sid,
            primary_agent=self.config.agent_dids[0] if self.config.agent_dids else "none",
        )

        # Dial agents (tries each in order until one accepts)
        asyncio.create_task(self._dial_agent_pipeline(webhook_base_url))

    # ── Agent Dial Pipeline ──────────────────────────────────────────

    async def _dial_agent_pipeline(self, webhook_base_url: str) -> None:
        """
        Dial agent(s) with Gather TwiML. Agent must press 1 to accept.
        On acceptance, agent is placed into the conference room.
        """
        try:
            for i, agent_did in enumerate(self.config.agent_dids):
                if i >= self.config.max_agent_retries:
                    break

                self._agent_attempt = i + 1
                self._current_agent_did = agent_did

                if i > 0:
                    self.state = TransferState.FAILED_RETRY
                    logger.info("transfer_retry", attempt=i + 1,
                        agent_did=agent_did, call_id=self._call_id)

                accepted = await self._dial_single_agent(agent_did, webhook_base_url)
                if accepted:
                    return  # Success!

            # All agents failed
            self.state = TransferState.FAILED
            if self.config.callback_enabled:
                self.state = TransferState.CALLBACK_OFFERED
            logger.warning("all_agents_failed",
                attempts=self._agent_attempt,
                fail_reasons=[str(r) for r in self._fail_reasons],
                call_id=self._call_id)

        except Exception as e:
            logger.error("dial_pipeline_error",
                error=str(e), call_id=self._call_id)
            self._fail_reasons.append(TransferFailReason.NETWORK_ERROR)
            self.state = TransferState.FAILED

    async def _dial_single_agent(self, agent_did: str, webhook_base_url: str) -> bool:
        """
        Dial one agent with Gather TwiML. Returns True if agent accepted.
        """
        self.state = TransferState.DIALING_AGENT

        if not self._twilio:
            # Simulation mode
            logger.info("agent_dial_simulated", agent_did=agent_did, call_id=self._call_id)
            await asyncio.sleep(3)
            self._agent_accepted.set()
            self.state = TransferState.AGENT_ACCEPTED
            self._agent_accept_time = time.time()
            return True

        try:
            self.state = TransferState.AGENT_RINGING

            # Build the Gather TwiML URL — agent will hear
            # "Press 1 to accept this transfer" and must press 1
            gather_url = f"{webhook_base_url}/v1/transfer/agent-gather/{self._call_id}"
            status_url = f"{webhook_base_url}/v1/transfer/agent-status"

            caller_id = self.config.caller_id or "+13187222561"

            # Create outbound call to agent with Gather TwiML
            call_kwargs = {
                "to": agent_did,
                "from_": caller_id,
                "url": gather_url,
                "method": "POST",
                "status_callback": status_url,
                "status_callback_event": ["initiated", "ringing", "answered", "completed"],
                "status_callback_method": "POST",
                "timeout": self.config.ring_timeout_seconds,
            }

            # Machine detection — catch voicemail
            if self.config.machine_detection:
                call_kwargs["machine_detection"] = "Enable"
                call_kwargs["machine_detection_timeout"] = 8
                call_kwargs["machine_detection_speech_threshold"] = 1800
                call_kwargs["machine_detection_speech_end_threshold"] = 1200

            call = self._twilio.calls.create(**call_kwargs)
            self._agent_call_sid = call.sid

            logger.info("agent_dial_initiated",
                agent_did=agent_did,
                agent_call_sid=call.sid,
                gather_url=gather_url,
                call_id=self._call_id)

            # Wait for agent to press 1 (set by webhook handler)
            try:
                await asyncio.wait_for(
                    self._agent_accepted.wait(),
                    timeout=self.config.ring_timeout_seconds + 10,
                )
                self._agent_accept_time = time.time()
                self.state = TransferState.AGENT_ACCEPTED
                logger.info("agent_accepted_transfer",
                    agent_did=agent_did,
                    accept_time_s=round(self._agent_accept_time - self._start_time, 1),
                    call_id=self._call_id)
                return True

            except asyncio.TimeoutError:
                self._fail_reasons.append(TransferFailReason.AGENT_NO_ANSWER)
                logger.warning("agent_accept_timeout",
                    agent_did=agent_did,
                    timeout=self.config.ring_timeout_seconds,
                    call_id=self._call_id)
                # Hang up the agent leg
                try:
                    self._twilio.calls(self._agent_call_sid).update(status="completed")
                except Exception:
                    pass
                self._agent_accepted.clear()
                return False

        except Exception as e:
            self._fail_reasons.append(TransferFailReason.NETWORK_ERROR)
            logger.error("agent_dial_error",
                agent_did=agent_did, error=str(e), call_id=self._call_id)
            return False

    # ── Move Prospect to Conference ──────────────────────────────────

    async def move_prospect_to_conference(self, webhook_base_url: str) -> None:
        """
        Move prospect into the conference where agent is already waiting.
        Called by CallBridge after AI says "Sarah is joining now".

        The prospect's TwiML includes a warm intro <Say> before the
        <Conference>, so both hear the handoff message.
        """
        self.state = TransferState.MOVING_PROSPECT

        if not self._twilio:
            logger.info("prospect_move_simulated", call_id=self._call_id)
            self.state = TransferState.CONNECTED
            self._prospect_joined.set()
            return

        try:
            record_attr = 'record-from-start' if self.config.record_conference else 'do-not-record'
            status_url = f"{webhook_base_url}/v1/transfer/conference-events"

            # Verify the agent is still in the conference before moving prospect.
            # Without this check, the prospect joins an empty conference (agent
            # may have hung up during hold) → both sides hear silence → "technical_error".
            agent_still_connected = True
            if self._agent_call_sid:
                try:
                    agent_call = self._twilio.calls(self._agent_call_sid).fetch()
                    if agent_call.status not in ("in-progress", "ringing", "queued"):
                        agent_still_connected = False
                        logger.warning("agent_disconnected_before_prospect_join",
                            agent_status=agent_call.status,
                            call_id=self._call_id)
                except Exception as e:
                    logger.warning("agent_status_check_failed",
                        error=str(e), call_id=self._call_id)

            if not agent_still_connected:
                self._fail_reasons.append(TransferFailReason.CONFERENCE_ERROR)
                self.state = TransferState.FAILED
                raise RuntimeError("Agent disconnected before prospect could join conference")

            # Join conference directly — NO <Say> (avoids wrong voice / delay).
            # The AI already spoke the warm intro through the media stream
            # using the same Cartesia voice. Prospect joins instantly.
            # waitUrl="" prevents hold music while waiting for the prospect to connect.
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>
    <Conference
      beep="false"
      startConferenceOnEnter="true"
      endConferenceOnExit="true"
      record="{record_attr}"
      waitUrl=""
      statusCallback="{status_url}"
      statusCallbackEvent="start end join leave"
      statusCallbackMethod="POST"
    >{self._conference_name}</Conference>
  </Dial>
</Response>'''

            # Update prospect's call to join conference
            self._twilio.calls(self._prospect_call_sid).update(twiml=twiml)

            self.state = TransferState.CONNECTED
            self._prospect_joined.set()
            self._prospect_joined_time = time.time()

            logger.info("prospect_moved_to_conference",
                conference=self._conference_name,
                call_id=self._call_id,
                total_hold_s=round(self.hold_elapsed, 1))

            # Start post-transfer monitoring
            asyncio.create_task(self._monitor_post_transfer())

        except Exception as e:
            self._fail_reasons.append(TransferFailReason.CONFERENCE_ERROR)
            logger.error("prospect_move_failed",
                error=str(e), call_id=self._call_id)
            raise

    # ── Post-Transfer Monitoring ─────────────────────────────────────

    async def _monitor_post_transfer(self) -> None:
        """Monitor for 30s to confirm qualified transfer."""
        self.state = TransferState.MONITORING
        monitor_start = time.time()

        try:
            while True:
                elapsed = time.time() - monitor_start
                if elapsed >= self.config.post_transfer_monitor_seconds:
                    self.state = TransferState.TRANSFERRED
                    self._transfer_verified.set()
                    logger.info("qualified_transfer_confirmed",
                        duration=round(elapsed, 1),
                        call_id=self._call_id)
                    return
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    # ── Webhook Event Handlers ───────────────────────────────────────

    def handle_agent_dtmf_accept(self) -> None:
        """Called when agent presses 1 on the Gather TwiML."""
        self._agent_accepted.set()
        self._agent_accept_time = time.time()
        logger.info("agent_dtmf_accepted",
            agent_did=self._current_agent_did,
            call_id=self._call_id)

    def handle_conference_event(self, event_data: Dict) -> None:
        """Process Twilio conference statusCallback events."""
        event_type = event_data.get("StatusCallbackEvent", "")
        conference_sid = event_data.get("ConferenceSid", "")
        call_sid = event_data.get("CallSid", "")
        reason = event_data.get("Reason", "")

        self._events_log.append({
            "type": event_type,
            "conference_sid": conference_sid,
            "call_sid": call_sid,
            "reason": reason,
            "timestamp": time.time(),
        })

        if conference_sid and not self._conference_sid:
            self._conference_sid = conference_sid

        if event_type == "conference-start":
            logger.info("conference_started",
                conference_sid=conference_sid, call_id=self._call_id)
        elif event_type == "participant-join":
            logger.info("participant_joined",
                call_sid=call_sid[:10], call_id=self._call_id)
        elif event_type == "participant-leave":
            logger.info("participant_left",
                call_sid=call_sid[:10], reason=reason, call_id=self._call_id)
        elif event_type == "conference-end":
            logger.info("conference_ended",
                conference_sid=conference_sid, call_id=self._call_id)

    def handle_agent_status(self, event_data: Dict) -> None:
        """Process Twilio agent call status events."""
        call_status = event_data.get("CallStatus", "")
        answered_by = event_data.get("AnsweredBy", "")
        call_sid = event_data.get("CallSid", "")

        self._events_log.append({
            "type": f"agent-{call_status}",
            "call_sid": call_sid,
            "answered_by": answered_by,
            "timestamp": time.time(),
        })

        if call_status == "in-progress":
            if answered_by in ("machine_start", "machine_end_beep"):
                self._fail_reasons.append(TransferFailReason.AGENT_VOICEMAIL)
                logger.warning("agent_voicemail_detected",
                    agent_did=self._current_agent_did, call_id=self._call_id)
                if self._twilio:
                    try:
                        self._twilio.calls(call_sid).update(status="completed")
                    except Exception:
                        pass
            # Note: agent answering doesn't mean they accepted —
            # they still need to press 1. The Gather TwiML handles that.

        elif call_status in ("no-answer", "busy", "failed", "canceled"):
            reason_map = {
                "no-answer": TransferFailReason.AGENT_NO_ANSWER,
                "busy": TransferFailReason.AGENT_BUSY,
                "failed": TransferFailReason.NETWORK_ERROR,
                "canceled": TransferFailReason.PROSPECT_HANGUP,
            }
            self._fail_reasons.append(
                reason_map.get(call_status, TransferFailReason.NETWORK_ERROR))
            logger.warning("agent_call_failed",
                status=call_status, agent_did=self._current_agent_did,
                call_id=self._call_id)

    # ── Hold Audio Management ────────────────────────────────────────

    async def presynthesize_hold_audio(
        self,
        synthesize_fn: Any,
    ) -> int:
        """
        Pre-synthesize hold audio during dial time.
        This should be called BEFORE the transfer starts (during the dial phase).

        Returns count of successfully pre-synthesized pieces.
        """
        return await self._hold_audio_queue.presynthesize_hold_audio(
            hold_texts=self.config.hold_audio_pieces,
            synthesize_fn=synthesize_fn,
            agent_name=self._agent_name,
        )

    def get_next_hold_audio(self) -> Optional[HoldAudioItem]:
        """
        Get next pre-synthesized hold audio.
        Called during transfer hold to play queued audio at intervals.
        """
        return self._hold_audio_queue.get_next_audio()

    def has_more_hold_audio(self) -> bool:
        """Check if there's more pre-synthesized hold audio available."""
        return self._hold_audio_queue.has_more()

    # ── Hold Phrase Management ───────────────────────────────────────

    def get_next_hold_phrase(self) -> Optional[str]:
        """Get next hold phrase for AI to say while waiting."""
        if self.hold_elapsed > self.config.max_hold_time_seconds:
            return None

        if self._hold_phrase_index < len(self.config.hold_phrases):
            phrase = self.config.hold_phrases[self._hold_phrase_index]
            self._hold_phrase_index += 1
            return phrase

        filler_idx = (self._hold_phrase_index - len(self.config.hold_phrases)) % len(self.config.hold_fillers)
        self._hold_phrase_index += 1
        return self.config.hold_fillers[filler_idx]

    def get_fallback_phrase(self) -> str:
        return self.config.fallback_phrase

    def get_callback_confirm_phrase(self) -> str:
        return self.config.callback_confirm_phrase

    # ── Cancellation ─────────────────────────────────────────────────

    async def cancel(self) -> None:
        """Cancel an in-progress transfer."""
        if self._twilio and self._agent_call_sid:
            try:
                self._twilio.calls(self._agent_call_sid).update(status="completed")
            except Exception:
                pass
        self.state = TransferState.IDLE
        logger.info("transfer_cancelled", call_id=self._call_id)

    # ── Metrics ──────────────────────────────────────────────────────

    def get_transfer_metrics(self) -> Dict:
        """Return comprehensive transfer metrics."""
        metrics = {
            "state": self.state.value,
            "conference_name": self._conference_name,
            "total_hold_seconds": round(self.hold_elapsed, 1) if self._start_time else 0,
            "hold_phrases_used": self._hold_phrase_index,
            "hold_audio_queue": self._hold_audio_queue.get_metrics(),
            "agent_attempts": self._agent_attempt,
            "agent_accepted": self._agent_accepted.is_set(),
            "prospect_joined": self._prospect_joined.is_set(),
            "transfer_verified": self._transfer_verified.is_set(),
            "fail_reasons": [r.value if isinstance(r, TransferFailReason) else str(r)
                            for r in self._fail_reasons],
            "events_count": len(self._events_log),
        }

        if self._agent_accept_time:
            metrics["agent_accept_seconds"] = round(
                self._agent_accept_time - self._start_time, 1)
        if self._prospect_joined_time:
            metrics["time_to_connect_seconds"] = round(
                self._prospect_joined_time - self._start_time, 1)

        return metrics

    def should_handoff(self) -> bool:
        """Check if agent accepted and ready for handoff."""
        return self._agent_accepted.is_set() and self.state == TransferState.AGENT_ACCEPTED
