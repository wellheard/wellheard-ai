"""
WellHeard AI - Test Prospect & Test Agent System

Provides smart AI-driven test scenarios for testing the full call flow:
- 6 realistic prospect scenarios (weighted random selection)
- Continuous STT → LLM → TTS bridge for prospect AI
- Agent TwiML for transfer testing

The test prospect runs its own LLM conversation independently,
simulating realistic human behavior, objections, and interruptions.
"""

import asyncio
import random
import time
import struct
import structlog
from typing import Optional, AsyncIterator
from enum import Enum

from config.settings import settings
from .providers.deepgram_stt import DeepgramSTTProvider
from .providers.groq_llm import GroqLLMProvider
from .providers.cartesia_tts import CartesiaTTSProvider

logger = structlog.get_logger()


# ── Test Prospect Scenarios ──────────────────────────────────────────────────

class ProspectScenario(str, Enum):
    """Available prospect scenarios for testing."""
    EASY_CLOSE = "easy_close"
    CONFUSED_OPEN = "confused_open"
    PRICE_OBJECTION = "price_objection"
    HAS_INSURANCE = "has_insurance"
    NOT_INTERESTED = "not_interested"
    WRONG_NUMBER = "wrong_number"


PROSPECT_SCENARIOS = {
    ProspectScenario.EASY_CLOSE: {
        "weight": 0.30,
        "name": "Easy Close",
        "description": "Friendly, interested, remembers the form",
        "system_prompt": (
            "You're Robert, a 62-year-old man on a PHONE CALL. You filled out a form for burial "
            "insurance info a few weeks ago and you remember it. You're interested, you have a "
            "checking account, and you're ready to talk to someone. "
            "IMPORTANT RULES: "
            "- Keep EVERY response to 2-8 words MAX. You're on a phone call, not writing an essay. "
            "- Use quick acknowledgments: 'yeah', 'uh-huh', 'right', 'sure', 'okay'. "
            "- Sometimes just say 'yeah' or 'mhm' while the other person is explaining. "
            "- If asked about the form: 'Oh yeah, I remember that.' "
            "- If asked about a bank account: 'Yeah I got a checking account.' "
            "- If offered to connect to an agent: 'Sure, sounds good.' "
            "- Use fillers naturally: 'um', 'uh', 'well'. "
            "- Be warm but brief. Real people don't monologue on the phone. "
            "- SOMETIMES interrupt before they finish: jump in with 'yeah yeah' or 'right, I got it'."
        ),
        "persona": "Robert, 62M - Interested",
        "interrupt_probability": 0.3,  # 30% chance of early response
    },
    ProspectScenario.CONFUSED_OPEN: {
        "weight": 0.20,
        "name": "Confused but Open",
        "description": "Polite, doesn't remember form, but willing to listen",
        "system_prompt": (
            "You're Dorothy, a 70-year-old woman on a PHONE CALL. You don't remember filling out "
            "any form but you're polite. You're confused about why they're calling. "
            "IMPORTANT RULES: "
            "- Keep EVERY response to 3-10 words MAX. "
            "- First response: 'I'm sorry, who is this?' or 'What company?' "
            "- If they explain clearly: 'Oh... I'm not sure I remember that.' "
            "- If they're patient and explain well: gradually warm up. 'Oh okay, well...' "
            "- Eventually agree: 'Alright, I suppose it can't hurt.' "
            "- Use grandmotherly speech: 'oh I see', 'okay dear', 'well let me think'. "
            "- Be genuinely confused but never rude."
        ),
        "persona": "Dorothy, 70F - Confused",
    },
    ProspectScenario.PRICE_OBJECTION: {
        "weight": 0.15,
        "name": "Price Objection",
        "description": "Worried about cost, on fixed income",
        "system_prompt": (
            "You're Mike, a 55-year-old man on a PHONE CALL. You remember the form. "
            "But your MAIN concern is cost. "
            "IMPORTANT RULES: "
            "- Keep EVERY response to 3-12 words MAX. "
            "- First response: 'Yeah I remember. How much is this gonna cost?' "
            "- Key phrases: 'I'm on a fixed income', 'Can't afford much right now'. "
            "- If they mention affordable/cheap/couple bucks: 'Well... I dunno.' "
            "- If they handle cost well and keep it brief: 'I guess I could hear more.' "
            "- If they ramble about cost: 'Look, I really can't afford anything.' "
            "- Sound worried but not hostile. You WANT coverage but money is tight."
        ),
        "persona": "Mike, 55M - Cost-Conscious",
    },
    ProspectScenario.HAS_INSURANCE: {
        "weight": 0.15,
        "name": "Already Has Insurance",
        "description": "Skeptical, already covered at work",
        "system_prompt": (
            "You're Linda, a 65-year-old woman on a PHONE CALL. You remember the form "
            "but you already have life insurance through work. "
            "IMPORTANT RULES: "
            "- Keep EVERY response to 3-12 words MAX. "
            "- First response: 'I already have insurance through my employer.' "
            "- If they ask about coverage gaps: 'I'm pretty sure I'm fully covered.' "
            "- If they explain final expense vs life insurance difference: 'Hmm, I didn't know that.' "
            "- If they ask smart questions: become more open. 'Well maybe I should check.' "
            "- Be polite but skeptical. Not rude."
        ),
        "persona": "Linda, 65F - Already Insured",
    },
    ProspectScenario.NOT_INTERESTED: {
        "weight": 0.10,
        "name": "Not Interested / Rushed",
        "description": "Busy, annoyed, wants to hang up quickly",
        "system_prompt": (
            "You're Dave, a 58-year-old man on a PHONE CALL. You're busy. "
            "IMPORTANT RULES: "
            "- Keep EVERY response to 2-5 words MAX. "
            "- First response: 'Not interested.' or 'I'm busy.' "
            "- If they handle it smoothly and BRIEFLY: 'Fine, make it quick.' "
            "- If they ramble or push hard: 'I gotta go, bye.' then STOP RESPONDING. "
            "- Sound rushed and slightly annoyed. Short, curt sentences. "
            "- You might thaw if they're professional and fast."
        ),
        "persona": "Dave, 58M - Busy",
        "interrupt_probability": 0.5,  # 50% chance — impatient people interrupt a lot
    },
    ProspectScenario.WRONG_NUMBER: {
        "weight": 0.10,
        "name": "Wrong Number",
        "description": "Didn't fill form, doesn't know what this is",
        "system_prompt": (
            "You're someone who definitely did NOT fill out any form. "
            "IMPORTANT RULES: "
            "- Say ONE thing: 'Wrong number.' or 'I think you got the wrong person.' "
            "- That's it. After that, STOP RESPONDING completely. You hung up. "
            "- Do NOT engage further. Just the one response."
        ),
        "persona": "Unknown - Wrong Number",
    },
}


def select_scenario(force: str = None) -> tuple:
    """Randomly select a scenario based on weights.

    Args:
        force: Force a specific scenario by name (e.g., "easy_close")

    Returns:
        (scenario_enum, scenario_dict)
    """
    if force:
        try:
            scenario = ProspectScenario(force)
            return scenario, PROSPECT_SCENARIOS[scenario]
        except (ValueError, KeyError):
            pass

    scenarios = list(PROSPECT_SCENARIOS.keys())
    weights = [PROSPECT_SCENARIOS[s]["weight"] for s in scenarios]
    chosen = random.choices(scenarios, weights=weights, k=1)[0]
    return chosen, PROSPECT_SCENARIOS[chosen]


# ── Test Prospect Bridge ─────────────────────────────────────────────────────

class TestProspectBridge:
    """
    Lightweight bridge for test prospect AI.

    Unlike CallBridge (3 phases: detect → pitch → converse),
    the prospect just listens and responds continuously using:
      audio_feeder → Deepgram STT (continuous) → Groq LLM → Cartesia TTS → output queue

    Same continuous streaming architecture as CallBridge Phase 3, but simpler:
    - No phase detection, no pitch, no transfer logic
    - Just listen → think → speak
    """

    # Cartesia male voice options (variety for different scenarios)
    MALE_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Default male
    FEMALE_VOICE_ID = "694f9389-aac1-45b6-b726-9d9369183238"  # Default female

    def __init__(
        self,
        scenario: ProspectScenario,
        scenario_config: dict,
        call_id: str = "",
    ):
        self.scenario = scenario
        self.scenario_config = scenario_config
        self.call_id = call_id
        self.persona = scenario_config.get("persona", "Test Prospect")
        self.system_prompt = scenario_config.get("system_prompt", "")
        self.interrupt_probability = scenario_config.get("interrupt_probability", 0.0)

        # Pick voice based on persona gender
        if any(g in self.persona for g in ["F -", "70F", "65F"]):
            self.voice_id = self.FEMALE_VOICE_ID
        else:
            self.voice_id = self.MALE_VOICE_ID

        # Providers
        self.stt: Optional[DeepgramSTTProvider] = None
        self.llm: Optional[GroqLLMProvider] = None
        self.tts: Optional[CartesiaTTSProvider] = None

        # Conversation state
        self._conversation_history: list[dict] = []
        self._turn_count = 0

        # Audio queues (same as CallBridge)
        self._input_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=1500)

        # State
        self._active = False
        self._ai_speaking = False
        self._ai_speaking_started_at = 0.0
        self._ai_speech_ended_at = 0.0
        self._last_output_bytes = 0
        self._hung_up = False  # For "wrong number" / "not interested" scenarios

        logger.info("test_prospect_created",
            call_id=self.call_id,
            scenario=scenario.value,
            persona=self.persona,
            voice_id=self.voice_id)

    async def connect_providers(self):
        """Initialize and connect STT, LLM, TTS providers."""
        self.stt = DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_stt_model,
            language=settings.deepgram_stt_language,
        )
        await self.stt.connect()

        self.llm = GroqLLMProvider(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
        )

        self.tts = CartesiaTTSProvider(
            api_key=settings.cartesia_api_key,
            voice_id=self.voice_id,
            model=settings.cartesia_model,
            speed=1.1,  # Slightly faster for natural phone speech
            emotion="neutral",  # Must be a valid Cartesia enum value
        )
        await self.tts.connect()

        logger.info("test_prospect_providers_connected",
            call_id=self.call_id, scenario=self.scenario.value)

    def on_audio_received(self, pcm_data: bytes):
        """Called by the media stream handler when audio arrives from Twilio."""
        if not self._active or self._hung_up:
            return
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

    def _in_echo_zone(self) -> bool:
        """Check if we're in the echo zone (suppress our own audio feedback)."""
        if not self._ai_speech_ended_at:
            return self._ai_speaking
        elapsed = (time.time() - self._ai_speech_ended_at) * 1000
        echo_ms = min(max(self._last_output_bytes / 32, 200), 2000)
        return elapsed < echo_ms

    async def run_conversation_loop(self):
        """
        Main conversation loop — continuous STT → LLM → TTS.
        Mirrors CallBridge Phase 3 architecture but simpler.
        """
        self._active = True

        async def audio_feeder() -> AsyncIterator[bytes]:
            """Continuously feed audio from input queue to Deepgram."""
            silence_frame = b'\x00' * 640
            while self._active and not self._hung_up:
                try:
                    chunk = await asyncio.wait_for(
                        self._input_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    yield silence_frame
                    continue

                if chunk is None:
                    return

                if self._in_echo_zone():
                    yield silence_frame
                    continue

                yield chunk

        accumulated_transcript = ""
        active_turn_task: Optional[asyncio.Task] = None
        interrupt_decided = False  # Per-utterance: have we decided to interrupt this turn?

        try:
            async for result in self.stt.transcribe_continuous(audio_feeder()):
                if not self._active or self._hung_up:
                    break

                event = result.get("event")

                if event == "speech_started":
                    # Reset interrupt decision for new utterance
                    interrupt_decided = False
                    continue

                if event == "utterance_end":
                    if accumulated_transcript.strip():
                        self._turn_count += 1
                        transcript = accumulated_transcript.strip()
                        accumulated_transcript = ""
                        interrupt_decided = False
                        if active_turn_task and not active_turn_task.done():
                            active_turn_task.cancel()
                            try:
                                await active_turn_task
                            except (asyncio.CancelledError, Exception):
                                pass
                        active_turn_task = asyncio.create_task(
                            self._process_turn(transcript, self._turn_count))
                    continue

                text = result.get("text", "")
                is_final = result.get("is_final", False)
                speech_final = result.get("speech_final", False)

                if is_final and text:
                    accumulated_transcript += " " + text

                    # ── Interrupt logic: sometimes respond mid-utterance ──
                    # If we have enough words and the dice roll says interrupt,
                    # fire a turn NOW while Becky is still speaking.
                    # NOTE: We allow this even when speech_final=True because
                    # Deepgram often sends is_final and speech_final together
                    # for short utterances. The interrupt simulates the prospect
                    # jumping in before Becky fully finishes.
                    if (self.interrupt_probability > 0
                            and not interrupt_decided
                            and len(accumulated_transcript.split()) >= 3
                            and self._turn_count >= 1):  # Don't interrupt greeting
                        interrupt_decided = True  # Only decide once per utterance
                        if random.random() < self.interrupt_probability:
                            self._turn_count += 1
                            transcript = accumulated_transcript.strip()
                            accumulated_transcript = ""
                            logger.info("prospect_interrupting",
                                call_id=self.call_id,
                                turn=self._turn_count,
                                heard_so_far=transcript[:80])
                            if active_turn_task and not active_turn_task.done():
                                active_turn_task.cancel()
                                try:
                                    await active_turn_task
                                except (asyncio.CancelledError, Exception):
                                    pass
                            active_turn_task = asyncio.create_task(
                                self._process_turn(transcript, self._turn_count))
                            continue

                if speech_final and accumulated_transcript.strip():
                    self._turn_count += 1
                    transcript = accumulated_transcript.strip()
                    accumulated_transcript = ""
                    interrupt_decided = False
                    if active_turn_task and not active_turn_task.done():
                        active_turn_task.cancel()
                        try:
                            await active_turn_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    active_turn_task = asyncio.create_task(
                        self._process_turn(transcript, self._turn_count))

            # Wait for last turn
            if active_turn_task and not active_turn_task.done():
                try:
                    await asyncio.wait_for(active_turn_task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass

        except Exception as e:
            logger.error("test_prospect_loop_error",
                call_id=self.call_id, error=str(e))

        logger.info("test_prospect_loop_ended",
            call_id=self.call_id,
            scenario=self.scenario.value,
            turns=self._turn_count,
            hung_up=self._hung_up)

    async def _process_turn(self, transcript: str, turn_number: int):
        """Process one conversation turn: LLM → TTS → queue audio."""
        t0 = time.time()

        self._conversation_history.append(
            {"role": "user", "content": transcript})

        logger.info("prospect_turn_start",
            call_id=self.call_id, turn=turn_number,
            scenario=self.scenario.value,
            heard=transcript[:100])

        # ── LLM ──
        response_text = ""
        try:
            async for chunk in self.llm.generate_stream(
                messages=self._conversation_history,
                system_prompt=self.system_prompt,
                temperature=0.9,
                max_tokens=25,
            ):
                text = chunk.get("text", "")
                if text:
                    response_text += text
                if chunk.get("is_complete"):
                    break
        except Exception as e:
            logger.error("prospect_turn_llm_error",
                call_id=self.call_id, turn=turn_number, error=str(e))
            return

        response_text = response_text.strip()
        if not response_text:
            return

        # Clean up stage directions
        import re
        response_text = re.sub(r'\*[^*]+\*', '', response_text).strip()
        response_text = re.sub(r'\[[^\]]+\]', '', response_text).strip()
        if not response_text:
            return

        self._conversation_history.append(
            {"role": "assistant", "content": response_text})

        # Check for "hung up" indicators (wrong number / not interested)
        hang_up_phrases = ["bye", "gotta go", "wrong number", "wrong person"]
        if any(p in response_text.lower() for p in hang_up_phrases):
            if self.scenario in (ProspectScenario.WRONG_NUMBER, ProspectScenario.NOT_INTERESTED):
                self._hung_up = True

        llm_ms = (time.time() - t0) * 1000

        # ── TTS ──
        tts_t0 = time.time()
        frame_buffer = bytearray()
        frames_queued = 0

        try:
            async for audio_result in self.tts.synthesize_single_streamed(
                text=response_text,
                voice_id=self.voice_id,
            ):
                audio = audio_result.get("audio", b"")
                if not audio:
                    continue

                frame_buffer.extend(audio)

                # Emit full 640-byte frames only
                while len(frame_buffer) >= 640:
                    frame = bytes(frame_buffer[:640])
                    del frame_buffer[:640]
                    if frames_queued == 0:
                        self._ai_speaking = True
                        self._ai_speaking_started_at = time.time()
                    frames_queued += 1
                    try:
                        await self._output_queue.put(frame)
                    except asyncio.QueueFull:
                        pass

            # Flush remaining
            if frame_buffer:
                remaining = bytes(frame_buffer)
                if len(remaining) % 2 != 0:
                    remaining = remaining[:-1]
                if remaining:
                    try:
                        await self._output_queue.put(remaining)
                        frames_queued += 1
                    except asyncio.QueueFull:
                        pass

        except Exception as e:
            logger.error("prospect_turn_tts_error",
                call_id=self.call_id, turn=turn_number, error=str(e))

        self._last_output_bytes = frames_queued * 640
        elapsed_ms = (time.time() - t0) * 1000

        logger.info("prospect_turn_complete",
            call_id=self.call_id, turn=turn_number,
            scenario=self.scenario.value,
            heard=transcript[:80],
            said=response_text[:100],
            words=len(response_text.split()),
            llm_ms=round(llm_ms, 1),
            frames=frames_queued,
            total_ms=round(elapsed_ms, 1),
            hung_up=self._hung_up)

    async def close(self):
        """Cleanup."""
        self._active = False
        try:
            if self.stt:
                await self.stt.disconnect()
            if self.tts:
                await self.tts.disconnect()
        except Exception as e:
            logger.warning("prospect_close_error", error=str(e))

        logger.info("test_prospect_closed",
            call_id=self.call_id,
            scenario=self.scenario.value,
            turns=self._turn_count)


# ── Test Agent (for transfer) ────────────────────────────────────────────────

def generate_agent_answer_twiml(call_id: str = "") -> str:
    """
    TwiML for the test licensed agent (Sarah) answering a transfer.

    Flow:
    1. Gather: "Press 1 to accept" (simulating whisper)
    2. Auto-press 1 (we generate a DTMF tone via <Play> or just accept)
    3. Say agent greeting
    4. Stay on for 30s then wrap up

    For automated testing, we skip the gather and just answer directly.
    """
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Pause length="1"/>
  <Play digits="1"/>
  <Pause length="1"/>
  <Say voice="Polly.Joanna">Hi, this is Sarah with the benefits team.
  I have your information right here. Let me pull up your quote and walk
  you through the options. Sound good?</Say>
  <Pause length="5"/>
  <Say voice="Polly.Joanna">So based on what I see here, we have a couple
  of really good plans that would fit your budget. The most popular one starts
  at about fifteen dollars a month. Would you like to hear more about that?</Say>
  <Pause length="10"/>
  <Say voice="Polly.Joanna">Great. Well I appreciate your time today.
  I will send you all the details. Have a wonderful day!</Say>
  <Hangup/>
</Response>'''


def scenario_summary(scenario: ProspectScenario, scenario_config: dict) -> str:
    """Generate a human-readable summary."""
    name = scenario_config.get("name", scenario.value)
    desc = scenario_config.get("description", "")
    persona = scenario_config.get("persona", "")
    return f"{name}: {desc} ({persona})"
