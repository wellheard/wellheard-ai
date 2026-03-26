"""
WellHeard AI — AI-to-AI Call Test Runner
Simulates SDR calls with AI prospects for quality assurance.
"""
import asyncio
import os
import json
import time
import wave
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import httpx
import structlog

load_dotenv("config/.env")
logger = structlog.get_logger()

@dataclass
class TurnRecord:
    speaker: str  # "sdr" or "prospect"
    text: str
    latency_ms: float = 0
    audio_bytes: int = 0
    timestamp: float = field(default_factory=time.time)

@dataclass
class CallRecord:
    scenario_id: str
    scenario_name: str
    round_number: int
    pipeline: str  # "budget" or "quality"
    turns: list = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float = 0
    total_latency_ms: float = 0
    avg_turn_latency_ms: float = 0
    total_cost_estimate: float = 0
    qa_score: float = 0
    qa_analysis: dict = field(default_factory=dict)
    outcome: str = ""
    sdr_name: str = "Vicky"
    prospect_name: str = "Prospect"

    @property
    def duration_seconds(self):
        return (self.end_time or time.time()) - self.start_time

    @property
    def transcript(self) -> str:
        lines = []
        for turn in self.turns:
            label = f"SDR ({self.sdr_name})" if turn.speaker == "sdr" else f"Prospect ({self.prospect_name})"
            lines.append(f"{label}: {turn.text}")
        return "\n".join(lines)


class CallTestRunner:
    """Runs AI-to-AI test calls and performs QA analysis."""

    def __init__(self):
        self.groq_key = os.environ.get("HV_GROQ_API_KEY", "")
        self.google_key = os.environ.get("HV_GOOGLE_API_KEY", "")
        self.deepgram_key = os.environ.get("HV_DEEPGRAM_API_KEY", "")
        self.cartesia_key = os.environ.get("HV_CARTESIA_API_KEY", "")
        self.results_dir = "test_results"
        os.makedirs(self.results_dir, exist_ok=True)

    async def run_scenario(self, scenario, round_number: int = 1, pipeline: str = "budget") -> CallRecord:
        """Run a single test scenario."""
        record = CallRecord(
            scenario_id=scenario.scenario_id,
            scenario_name=scenario.name,
            round_number=round_number,
            pipeline=pipeline,
        )

        sdr_history = [{"role": "system", "content": scenario.sdr_system_prompt}]
        prospect_history = [{"role": "system", "content": scenario.persona.system_prompt}]

        sdr_text = scenario.sdr_greeting
        record.turns.append(TurnRecord(speaker="sdr", text=sdr_text))
        prospect_history.append({"role": "user", "content": sdr_text})

        total_latency = 0

        for turn_num in range(scenario.max_turns):
            # Prospect responds
            t0 = time.time()
            prospect_response = await self._generate_response(
                messages=prospect_history,
                provider="groq",
            )
            prospect_latency = (time.time() - t0) * 1000

            if not prospect_response:
                break

            record.turns.append(TurnRecord(
                speaker="prospect", text=prospect_response, latency_ms=prospect_latency
            ))

            prospect_history.append({"role": "assistant", "content": prospect_response})
            sdr_history.append({"role": "user", "content": prospect_response})

            if self._is_call_ending(prospect_response, sdr_text):
                break

            # SDR responds
            t0 = time.time()
            if pipeline == "budget":
                sdr_text = await self._generate_response(
                    messages=sdr_history, provider="groq"
                )
            else:
                sdr_text = await self._generate_response(
                    messages=sdr_history, provider="gemini"
                )
            sdr_latency = (time.time() - t0) * 1000
            total_latency += sdr_latency

            if not sdr_text:
                break

            record.turns.append(TurnRecord(
                speaker="sdr", text=sdr_text, latency_ms=sdr_latency
            ))

            sdr_history.append({"role": "assistant", "content": sdr_text})
            prospect_history.append({"role": "user", "content": sdr_text})

            if self._is_call_ending(sdr_text, prospect_response):
                break

        record.end_time = time.time()
        record.total_latency_ms = total_latency
        sdr_turns = [t for t in record.turns if t.speaker == "sdr"]
        record.avg_turn_latency_ms = total_latency / max(len(sdr_turns) - 1, 1)

        return record

    async def _generate_response(self, messages: list, provider: str = "groq", retries: int = 3) -> str:
        """Generate a response using the specified LLM provider, with retry on rate limit."""
        for attempt in range(retries):
            async with httpx.AsyncClient(timeout=30.0) as client:
                if provider == "groq":
                    try:
                        r = await client.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {self.groq_key}"},
                            json={
                                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                                "messages": messages,
                                "max_tokens": 200,
                                "temperature": 0.8,
                            },
                        )
                        if r.status_code == 200:
                            return r.json()["choices"][0]["message"]["content"]
                        elif r.status_code == 429:
                            wait = min(5 * (attempt + 1), 15)
                            logger.warning("groq_rate_limit", wait=wait, attempt=attempt+1)
                            await asyncio.sleep(wait)
                            continue
                        else:
                            logger.error("groq_error", status=r.status_code)
                            return ""
                    except Exception as e:
                        logger.error("groq_exception", error=str(e))
                        return ""
                else:
                    try:
                        gemini_contents = []
                        system_text = ""
                        for msg in messages:
                            if msg["role"] == "system":
                                system_text = msg["content"]
                            elif msg["role"] == "user":
                                gemini_contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
                            elif msg["role"] == "assistant":
                                gemini_contents.append({"role": "model", "parts": [{"text": msg["content"]}]})

                        r = await client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.google_key}",
                            json={
                                "contents": gemini_contents,
                                "systemInstruction": {"parts": [{"text": system_text}]} if system_text else None,
                                "generationConfig": {"maxOutputTokens": 200, "temperature": 0.8}
                            },
                        )
                        if r.status_code == 200:
                            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                        elif r.status_code == 429:
                            wait = min(5 * (attempt + 1), 15)
                            logger.warning("gemini_rate_limit", wait=wait, attempt=attempt+1)
                            await asyncio.sleep(wait)
                            continue
                        else:
                            logger.error("gemini_error", status=r.status_code)
                            return ""
                    except Exception as e:
                        logger.error("gemini_exception", error=str(e))
                        return ""
        return ""

    def _is_call_ending(self, current_text: str, prev_text: str) -> bool:
        """Detect if the call is naturally ending."""
        endings = [
            "goodbye", "bye bye", "take care", "hang up", "hanging up",
            "have a great day", "talk to you", "thank you for calling",
            "i'll let you go", "that's all", "no thank you",
            "you're in good hands", "hand you over", "i'm here",
        ]
        combined = (current_text + " " + prev_text).lower()
        return any(e in combined for e in endings)

    async def qa_analyze(self, record: CallRecord, scenario) -> dict:
        """Use AI to analyze the call quality against checkpoints."""
        prompt = f"""Analyze this AI SDR call transcript for quality.

SCENARIO: {scenario.name}
EXPECTED OUTCOME: {scenario.persona.expected_outcome}

TRANSCRIPT:
{record.transcript}

QA CHECKPOINTS:
{chr(10).join(f'{i+1}. {cp}' for i, cp in enumerate(scenario.qa_checkpoints))}

Return JSON with: overall_score (0-100), detected_outcome, checkpoint_scores array, strengths, improvements_needed, critical_issues.
"""

        # Use Groq with JSON mode for reliable structured output
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.groq_key}"},
                        json={
                            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                            "messages": [
                                {"role": "system", "content": "You are a call center QA analyst. Return ONLY valid JSON."},
                                {"role": "user", "content": prompt}
                            ],
                            "max_tokens": 600,
                            "temperature": 0.3,
                            "response_format": {"type": "json_object"},
                        },
                    )
                    if r.status_code == 200:
                        text = r.json()["choices"][0]["message"]["content"]
                        result = json.loads(text)
                        return result
                    elif r.status_code == 429:
                        wait = 5 * (attempt + 1)
                        logger.warning("qa_rate_limit", wait=wait, attempt=attempt+1)
                        await asyncio.sleep(wait)
                        continue
                    else:
                        logger.error("qa_groq_error", status=r.status_code)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("qa_parse_error", error=str(e), attempt=attempt+1)
                await asyncio.sleep(3)

        return {"overall_score": 0, "error": "Failed to parse QA analysis"}

    async def synthesize_audio(self, text: str, pipeline: str = "budget",
                               voice_id: str = "", voice_preset: str = "") -> bytes:
        """
        Synthesize audio for a text response.
        voice_preset: key from VOICE_PRESETS (e.g. "vicky", "ben")
        voice_id: explicit Cartesia voice ID override
        """
        # Voice presets for natural-sounding Sonic-3 voices
        VOICE_PRESETS = {
            "vicky": {"id": "734b0cda-9091-4144-9d4d-f33ffc2cc025", "emotion": "positivity:medium", "speed": 1.0},
            "ben":   {"id": "c1418ac2-d234-478a-9c53-a0e6a5a473e3", "emotion": "neutral", "speed": 1.0},
            "liam":  {"id": "41f3c367-e0a8-4a85-89e0-c27bae9c9b6d", "emotion": "neutral", "speed": 1.0},
            "ray":   {"id": "565510e8-6b45-45de-8758-13588fbaec73", "emotion": "neutral", "speed": 1.0},
            "tessa": {"id": "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", "emotion": "positivity:medium", "speed": 1.0},
            "molly": {"id": "03b1c65d-4b7f-4c09-91a8-e2f6f78cb2c9", "emotion": "positivity:medium", "speed": 1.0},
        }

        preset = VOICE_PRESETS.get(voice_preset, {})
        active_voice_id = voice_id or preset.get("id", "734b0cda-9091-4144-9d4d-f33ffc2cc025")
        emotion = preset.get("emotion", "neutral")
        speed = preset.get("speed", 1.0)

        async with httpx.AsyncClient(timeout=30.0) as client:
            if pipeline == "budget":
                try:
                    r = await client.post(
                        f"https://api.deepgram.com/v1/speak?model=aura-orpheus-en&encoding=linear16&sample_rate=16000",
                        headers={
                            "Authorization": f"Token {self.deepgram_key}",
                            "Content-Type": "application/json"
                        },
                        json={"text": text},
                    )
                    return r.content if r.status_code == 200 else b""
                except Exception as e:
                    logger.error("deepgram_tts_error", error=str(e))
                    return b""
            else:
                try:
                    r = await client.post(
                        "https://api.cartesia.ai/tts/bytes",
                        headers={
                            "Authorization": f"Bearer {self.cartesia_key}",
                            "Cartesia-Version": "2024-06-10",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model_id": "sonic-3",
                            "transcript": text,
                            "voice": {"mode": "id", "id": active_voice_id},
                            "output_format": {
                                "container": "raw",
                                "encoding": "pcm_s16le",
                                "sample_rate": 24000,
                            },
                            "language": "en",
                            "generation_config": {
                                "speed": speed,
                                "emotion": emotion,
                                "volume": 1.0,
                            },
                        },
                    )
                    if r.status_code == 200:
                        return r.content
                    else:
                        logger.error("cartesia_tts_error", status=r.status_code, body=r.text[:200])
                        return b""
                except Exception as e:
                    logger.error("cartesia_tts_error", error=str(e))
                    return b""

    async def create_call_recording(self, record: CallRecord, pipeline: str = "budget",
                                     sdr_voice: str = "vicky", prospect_voice: str = "ben") -> str:
        """
        Generate a WAV recording of the entire call with distinct voices.
        sdr_voice / prospect_voice: voice preset names (e.g. "vicky", "ben")
        """
        all_audio = bytearray()
        sample_rate = 24000 if pipeline == "quality" else 16000

        for turn in record.turns:
            # Half-second silence between turns (natural pause)
            silence = b"\x00\x00" * (sample_rate // 2)
            all_audio.extend(silence)

            if turn.speaker == "sdr":
                audio = await self.synthesize_audio(turn.text, pipeline, voice_preset=sdr_voice)
            else:
                audio = await self.synthesize_audio(turn.text, pipeline, voice_preset=prospect_voice)

            if audio:
                all_audio.extend(audio)
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)

        filename = f"{self.results_dir}/call_{record.scenario_id}_r{record.round_number}_{pipeline}.wav"
        try:
            with wave.open(filename, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(bytes(all_audio))
        except Exception as e:
            logger.error("wav_write_error", error=str(e))

        return filename

    def save_results(self, records: list, round_number: int):
        """Save round results to JSON."""
        data = {
            "round": round_number,
            "timestamp": datetime.now().isoformat(),
            "scenarios": []
        }
        for record in records:
            data["scenarios"].append({
                "scenario_id": record.scenario_id,
                "name": record.scenario_name,
                "pipeline": record.pipeline,
                "turns": len(record.turns),
                "duration_seconds": round(record.duration_seconds, 1),
                "avg_turn_latency_ms": round(record.avg_turn_latency_ms, 1),
                "outcome": record.outcome,
                "qa_score": record.qa_score,
                "qa_analysis": record.qa_analysis,
                "transcript": record.transcript,
            })

        filename = f"{self.results_dir}/round_{round_number}_results.json"
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        return filename
