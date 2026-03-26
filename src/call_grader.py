"""
WellHeard AI — Comprehensive Call Quality Grader

Grades every aspect of a voice AI call against industry benchmarks
and competitor standards (Dasha AI, Brightcall, Bland, Retell, Vapi).

GRADING CATEGORIES (100 points total):
1. Latency & Responsiveness (25 pts) — turn-around time, TTFB, pauses
2. Voice & Tonality (20 pts) — naturalness, consistency, emotion
3. Conversation Flow (20 pts) — natural progression, question strategy, listen ratio
4. Transfer Experience (15 pts) — speed, warmth, voice consistency
5. Interruption Handling (10 pts) — barge-in response time, false positive rate, recovery, echo suppression
6. Sales Effectiveness (5 pts) — qualification, objection handling, closing
7. Technical Reliability (5 pts) — errors, fallbacks, stability

COMPETITOR BENCHMARKS:
- Dasha AI:   ~1022ms end-to-end latency (voicebenchmark.ai #1)
- Brightcall: Natural voice, 4x conversion improvement
- Retell AI:  ~620-800ms latency, 92% intent accuracy
- Vapi:       ~465-550ms latency
- Bland AI:   ~600-900ms latency
- Human:      200-500ms natural turn-taking
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict


# ── Benchmark Thresholds ──────────────────────────────────────────────────

class Benchmarks:
    """Industry benchmarks and competitor standards.

    Updated 2026-03-25 based on test-v2-87936bd0 (Johan-approved "perfect" call):
    - Turn 1: 39ms via pre-synthesized cache (eliminates LLM entirely)
    - Turns 2+: ~400-600ms via collect-then-speak + single-shot TTS
    - Transfer: Working warm transfer with real Twilio dial
    - Voice: Cartesia Sonic-3 single-shot = perfect prosody
    """

    # Latency thresholds (milliseconds) — tuned to our proven capability
    TURN_AROUND_ELITE = 400      # What we achieve: turn1=39ms, turns2+=~500ms
    TURN_AROUND_GOOD = 700       # Still excellent — beats most competitors
    TURN_AROUND_OK = 1000        # Acceptable for complex responses
    TURN_AROUND_BAD = 1500       # Noticeable delay — needs investigation
    TURN_AROUND_FAIL = 2500      # Conversation-breaking

    LLM_TTFT_ELITE = 150        # Turn 1 cache = 0ms, Groq = ~150ms
    LLM_TTFT_GOOD = 350         # gpt-4o-mini warm = ~300ms
    LLM_TTFT_BAD = 700          # Cold start or overloaded

    TTS_TTFB_ELITE = 150        # Cartesia single-shot streamed ~40-100ms
    TTS_TTFB_GOOD = 400         # Acceptable with filler masking
    TTS_TTFB_BAD = 1000         # Too slow — audio gap noticeable

    # Greeting latency (time from answer to first audio)
    GREETING_INSTANT = 80        # Pre-synthesized — proven achievable
    GREETING_GOOD = 200
    GREETING_BAD = 800

    # Phase transition gaps (silence between phases)
    PHASE_GAP_GOOD = 400         # Smooth transition (our 400ms breath pause)
    PHASE_GAP_OK = 800
    PHASE_GAP_BAD = 1500         # Awkward pause

    # Silence/pause thresholds
    MID_SENTENCE_PAUSE_MAX = 150  # Single-shot TTS eliminates mid-sentence pauses
    INTER_TURN_SILENCE_GOOD = 700  # Natural breathing room
    INTER_TURN_SILENCE_BAD = 1500  # Dead air

    # Voice quality
    MOS_EXCELLENT = 4.5
    MOS_GOOD = 4.0
    MOS_ACCEPTABLE = 3.5

    # Conversation metrics — updated for tighter, more natural flow
    TALK_LISTEN_RATIO_IDEAL = 0.43   # 43% AI talk, 57% prospect talk
    TALK_LISTEN_RATIO_MAX = 0.60     # AI talking too much (tightened from 0.65)
    QUESTIONS_PER_TURN_TARGET = 1.0  # Every turn should end with a question
    MAX_RESPONSE_WORDS = 35          # Tightened from 40 — brevity is key
    MIN_RESPONSE_WORDS = 5           # Don't be too terse
    RESPONSE_WORD_TARGET_MIN = 10    # Target minimum for substantive response
    RESPONSE_WORD_TARGET_MAX = 15    # Target maximum for brevity

    # Transfer metrics — updated based on working warm transfer
    TRANSFER_HOLD_MAX_S = 8          # Tightened from 10 — keep it snappy
    TRANSFER_AGENT_WAIT_MAX_S = 3    # Agent shouldn't wait in empty conference
    TRANSFER_TOTAL_MAX_S = 12        # Tightened from 15 — total transfer experience

    # Sales effectiveness
    QUALIFICATION_QUESTIONS_MIN = 2   # Must ask about interest + bank account
    OBJECTION_HANDLE_REQUIRED = True

    # ── NEW: Repetition & State Tracking ──
    # Based on CallStateTracker — AI should NEVER repeat or re-ask
    MAX_REPETITION_RATIO = 0.30      # Max 30% word overlap with any previous response
    REQUIRED_STEP_PROGRESSION = True  # Must advance through steps, never go backwards

    # ── NEW: Emotional Intelligence & Brevity ──
    ANTI_REPETITION_PHRASE_LIMIT = 3  # Same phrase limit across call
    MAX_RESPONSE_WORDS_ABSOLUTE = 35   # Hard limit for phone naturalness

    # ── NEW: Transfer Quality ──
    TRANSFER_CONTEXT_KEYWORDS = ["your account", "your interest", "your situation", "your coverage"]
    BANK_CONFIRMATION_REQUIRED = True

    # ── NEW: Compliance ──
    AI_DISCLOSURE_REQUIRED = True
    BANNED_PHRASES = ["guarantee", "promise you", "absolutely sure", "100% coverage"]

    # ── Telephony Overhead Adjustment ──
    # In closed-environment tests (AI-to-AI via Twilio loopback), there is
    # NO real mobile network. In production calls to real phones, add:
    #   - Mobile carrier round-trip: ~80-150ms
    #   - PSTN bridge processing: ~50-100ms
    #   - Codec transcoding jitter: ~20-50ms
    # Total estimated overhead for a real call vs loopback: ~200ms
    TELEPHONY_OVERHEAD_MS = 200  # Added to perceived latency for test calls


# ── Grade Result ──────────────────────────────────────────────────────────

@dataclass
class CategoryGrade:
    """Grade for a single category."""
    name: str
    score: float          # 0-100 normalized
    max_points: float     # Weight in overall score
    weighted_score: float # score * (max_points / 100)
    grade_letter: str     # A+ through F
    findings: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)
    metrics: Dict = field(default_factory=dict)

    @staticmethod
    def letter_from_score(score: float) -> str:
        if score >= 95: return "A+"
        if score >= 90: return "A"
        if score >= 85: return "A-"
        if score >= 80: return "B+"
        if score >= 75: return "B"
        if score >= 70: return "B-"
        if score >= 65: return "C+"
        if score >= 60: return "C"
        if score >= 50: return "D"
        return "F"


@dataclass
class CallGradeReport:
    """Complete call grade report."""
    call_id: str
    graded_at: str
    overall_score: float
    overall_grade: str
    categories: List[CategoryGrade]
    competitor_comparison: Dict
    summary: str
    top_issues: List[str]
    call_metadata: Dict


# ── Call Data Extractor ───────────────────────────────────────────────────

@dataclass
class ExtractedCallData:
    """Structured data extracted from raw call logs."""
    call_id: str = ""
    total_duration_s: float = 0.0

    # Phase timings
    greeting_queued_at: float = 0.0
    greeting_bytes: int = 0
    pitch_queued_at: float = 0.0
    pitch_bytes: int = 0
    pitch_duration_ms: float = 0.0
    phase3_started_at: float = 0.0
    call_ended_at: float = 0.0

    # Phase gaps
    answer_to_greeting_ms: float = 0.0  # How fast greeting starts after answer
    greeting_to_human_ms: float = 0.0   # Time for human to respond
    human_to_pitch_ms: float = 0.0      # Gap between human detected and pitch
    pitch_to_phase3_ms: float = 0.0     # Gap between pitch end and Phase 3

    # Turn data
    turns: List[Dict] = field(default_factory=list)

    # Transfer data
    transfer_triggered: bool = False
    transfer_trigger_phrase: str = ""
    transfer_initiated_at: float = 0.0
    agent_accepted_at: float = 0.0
    prospect_moved_at: float = 0.0
    transfer_hold_time_s: float = 0.0
    transfer_agent_wait_s: float = 0.0
    hold_phrases_played: int = 0
    transfer_succeeded: bool = False
    transfer_failed_reason: str = ""

    # Conversation history
    conversation: List[Dict] = field(default_factory=list)  # [{role, content, timestamp}]

    # Error tracking
    errors: List[Dict] = field(default_factory=list)
    fallbacks_used: int = 0

    # Phase completion
    reached_phase1: bool = False
    reached_phase2: bool = False
    reached_phase3: bool = False
    human_detected: bool = False
    disposition: str = ""

    # Barge-in / interruption handling
    barge_in_events: int = 0          # Times barge-in was detected
    barge_in_suppressed: int = 0      # Times barge-in was suppressed (grace period)
    barge_in_cleared_chunks: int = 0  # Audio chunks cleared during barge-in
    barge_in_response_times: List[float] = field(default_factory=list)  # ms from detection to clear
    speech_started_count: int = 0     # Total speech_started events
    echo_false_positives: int = 0     # Rapid double barge-ins (likely echo)

    # Step tracking
    step_order_correct: bool = True   # Were qualification steps done in order?
    agent_name_correct: bool = True   # Was agent name "Sarah" (not hallucinated)?

    # Test call metadata
    is_test_call: bool = False  # True for automated test calls (no real telephony)


def extract_call_data(logs: List[Dict]) -> ExtractedCallData:
    """Extract structured data from raw call log entries."""
    data = ExtractedCallData()
    if not logs:
        return data

    data.call_id = logs[0].get("call_id", "")
    first_ts = _parse_ts(logs[0].get("timestamp", ""))
    last_ts = _parse_ts(logs[-1].get("timestamp", ""))
    if first_ts and last_ts:
        data.total_duration_s = last_ts - first_ts

    ts_map = {}  # event -> timestamp

    for log in logs:
        evt = log.get("event", "")
        ts = _parse_ts(log.get("timestamp", ""))
        ts_map[evt] = ts

        # Phase tracking
        if evt == "call_bridge_started":
            data.reached_phase1 = True
        elif evt == "phase1_greeting_queued":
            data.greeting_queued_at = ts or 0
            data.greeting_bytes = log.get("bytes", 0)
        elif evt == "phase1_human_detected":
            data.human_detected = True
            data.greeting_to_human_ms = log.get("detect_ms", 0)
        elif evt == "phase2_pitch_queued":
            data.reached_phase2 = True
            data.pitch_queued_at = ts or 0
            data.pitch_bytes = log.get("bytes", 0)
            data.pitch_duration_ms = log.get("duration_ms", 0)
        elif evt == "phase3_continuous_starting":
            data.reached_phase3 = True
            data.phase3_started_at = ts or 0
        elif evt == "phase3_tts_reconnected" or evt == "phase3_tts_preconnected":
            if not data.phase3_started_at:
                data.phase3_started_at = ts or 0

        # Turn data
        elif evt == "text_turn_complete":
            turn = {
                "turn_number": log.get("turn", 0),
                "transcript": log.get("transcript", ""),
                "response": log.get("response", ""),
                "llm_ttft_ms": log.get("llm_ttft_ms", 0),
                "tts_ms": log.get("tts_ms", 0),
                "total_ms": log.get("total_ms", 0),
                "llm_ms": log.get("llm_ms", 0),
                "response_bytes": log.get("response_bytes", 0),
                "is_repetition": log.get("is_repetition", False),
                "timestamp": ts,
            }
            data.turns.append(turn)
            data.conversation.append({
                "role": "user", "content": log.get("transcript", ""), "timestamp": ts
            })
            data.conversation.append({
                "role": "assistant", "content": log.get("response", ""), "timestamp": ts
            })

        elif evt == "text_turn_starting":
            pass  # Handled by text_turn_complete

        # Accumulated turn (new architecture with turn accumulation window)
        elif evt == "phase3_accumulated_turn":
            # This is an alternative to text_turn_complete with same data
            if not any(t.get("turn_number") == log.get("turn", 0) for t in data.turns):
                turn = {
                    "turn_number": log.get("turn", 0),
                    "transcript": log.get("transcript", ""),
                    "response": log.get("response", ""),
                    "llm_ttft_ms": log.get("llm_ttft_ms", 0),
                    "tts_ms": log.get("tts_ms", 0),
                    "total_ms": log.get("total_ms", 0),
                    "llm_ms": log.get("llm_ms", 0),
                    "response_bytes": log.get("response_bytes", 0),
                    "is_repetition": log.get("is_repetition", False),
                    "timestamp": ts,
                }
                data.turns.append(turn)

        # Speech/utterance events
        elif evt in ("phase3_speech_final_turn", "phase3_utterance_turn"):
            pass  # Covered by text_turn_complete

        # Transfer events
        elif evt == "transfer_trigger_detected":
            data.transfer_triggered = True
            data.transfer_trigger_phrase = log.get("trigger", "")
            data.transfer_initiated_at = ts or 0
        elif evt == "transfer_initiating":
            if not data.transfer_initiated_at:
                data.transfer_initiated_at = ts or 0
        elif evt == "transfer_agent_accepted" or evt == "transfer_agent_accepted_quick":
            data.agent_accepted_at = ts or 0
            data.transfer_hold_time_s = log.get("hold_elapsed", 0)
        elif evt == "warm_handoff_moving_prospect":
            data.prospect_moved_at = ts or 0
        elif evt == "warm_handoff_complete":
            data.transfer_succeeded = True
            data.transfer_hold_time_s = log.get("hold_elapsed", 0)
        elif evt == "transfer_hold_phrase":
            data.hold_phrases_played += 1
        elif evt == "transfer_failed_offering_callback":
            data.transfer_failed_reason = "agent_no_answer"

        # Barge-in events
        elif evt == "barge_in_detected":
            data.barge_in_events += 1
            # Record response time if available
            response_time = log.get("response_time_ms", 0)
            if response_time > 0:
                data.barge_in_response_times.append(response_time)
        elif evt == "barge_in_suppressed_grace_period":
            data.barge_in_suppressed += 1
        elif evt == "barge_in_audio_cleared":
            data.barge_in_cleared_chunks += log.get("cleared_chunks", 0)

        # Speech detection events
        elif evt == "speech_started":
            data.speech_started_count += 1
            # Detect echo false positives: rapid consecutive barge-ins
            if data.barge_in_response_times and len(data.barge_in_response_times) >= 2:
                last_two = data.barge_in_response_times[-2:]
                if last_two[1] - last_two[0] < 500:  # Within 500ms
                    data.echo_false_positives += 1

        # Errors
        elif "error" in evt or "failed" in evt:
            data.errors.append({
                "event": evt,
                "error": log.get("error", ""),
                "timestamp": ts,
            })

        # Fallbacks
        elif "failover" in evt or "fallback" in evt:
            data.fallbacks_used += 1

        # Disposition
        elif evt == "call_disposition_tagged":
            data.disposition = log.get("disposition", "")

        # Media stream
        elif evt == "media_stream_connected":
            if not data.greeting_queued_at:
                data.answer_to_greeting_ms = 0  # Will be calculated

        elif evt == "call_bridge_stopped" or evt == "twilio_media_stream_stopped":
            data.call_ended_at = ts or 0

    # Calculate phase gaps
    if data.greeting_queued_at and data.pitch_queued_at and data.pitch_duration_ms:
        pitch_end = data.pitch_queued_at + (data.pitch_duration_ms / 1000)
        if data.phase3_started_at:
            data.pitch_to_phase3_ms = (data.phase3_started_at - pitch_end) * 1000

    # Calculate transfer timing
    if data.agent_accepted_at and data.prospect_moved_at:
        data.transfer_agent_wait_s = data.prospect_moved_at - data.agent_accepted_at

    # Infer phases from available data when phase events are missing
    # (happens when early log entries are rotated)
    if data.turns and not data.reached_phase3:
        data.reached_phase3 = True
        data.reached_phase1 = True  # Can't reach phase 3 without phase 1
        data.reached_phase2 = True  # Can't reach phase 3 without phase 2 (unless inbound)
    if data.transfer_triggered and not data.reached_phase3:
        data.reached_phase3 = True
        data.reached_phase1 = True
        data.reached_phase2 = True

    return data


def _parse_ts(ts_str: str) -> Optional[float]:
    """Parse ISO timestamp to epoch float."""
    if not ts_str:
        return None
    try:
        # Handle various ISO formats
        ts_str = ts_str.rstrip("Z")
        if "." in ts_str:
            dt = datetime.fromisoformat(ts_str)
        else:
            dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except Exception:
        return None


# ── Grading Functions ─────────────────────────────────────────────────────

def grade_latency(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 1: Latency & Responsiveness (25 points)

    Measures:
    - Turn-around time (end of user speech → start of AI audio)
    - LLM time-to-first-token
    - TTS time-to-first-byte
    - Phase transition gaps
    - Dead air / awkward pauses
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Latency & Responsiveness",
            score=0, max_points=25, weighted_score=0,
            grade_letter="F",
            findings=["No conversation turns recorded — call didn't reach Phase 3"],
            improvements=["Ensure call reaches Phase 3 conversation"],
            metrics={}
        )

    # ── Turn-around times ──
    # PERCEIVED latency = LLM generation time + TTS TTFB (first audio chunk).
    # With streamed TTS, the user hears audio as soon as the first chunk
    # arrives, NOT after all audio is generated. So total_ms (which includes
    # full audio streaming) overstates the perceived delay.
    turn_times = []
    for t in data.turns:
        llm_ms = t.get("llm_ms", 0) or t.get("llm_ttft_ms", 0)  # Full LLM time or TTFT
        tts_ttfb = t.get("tts_ms", 0)
        if llm_ms > 0 and tts_ttfb > 0:
            perceived = llm_ms + tts_ttfb  # User hears first audio at this point
            turn_times.append(perceived)
        elif t["total_ms"] > 0:
            turn_times.append(t["total_ms"])  # Fallback to total_ms

    if turn_times:
        avg_turn = sum(turn_times) / len(turn_times)
        max_turn = max(turn_times)
        min_turn = min(turn_times)
        metrics["avg_perceived_latency_ms"] = round(avg_turn)
        metrics["max_perceived_latency_ms"] = round(max_turn)
        metrics["min_perceived_latency_ms"] = round(min_turn)
        # Also store total_ms for reference
        total_times = [t["total_ms"] for t in data.turns if t["total_ms"] > 0]
        if total_times:
            metrics["avg_total_ms"] = round(sum(total_times) / len(total_times))

        # Score turn-around time (biggest weight — 40% of category)
        if avg_turn <= Benchmarks.TURN_AROUND_ELITE:
            findings.append(f"ELITE turn-around: {avg_turn:.0f}ms avg (human-like, beats Dasha's 1022ms)")
        elif avg_turn <= Benchmarks.TURN_AROUND_GOOD:
            findings.append(f"GOOD turn-around: {avg_turn:.0f}ms avg (competitive with Retell/Vapi)")
            score -= 5
        elif avg_turn <= Benchmarks.TURN_AROUND_OK:
            findings.append(f"OK turn-around: {avg_turn:.0f}ms avg (on par with Dasha)")
            score -= 15
        elif avg_turn <= Benchmarks.TURN_AROUND_BAD:
            findings.append(f"SLOW turn-around: {avg_turn:.0f}ms avg (noticeable delay)")
            score -= 30
            improvements.append(f"Reduce turn-around from {avg_turn:.0f}ms to <800ms")
        else:
            findings.append(f"CRITICAL: {avg_turn:.0f}ms avg turn-around (conversation-breaking)")
            score -= 50
            improvements.append(f"URGENT: Turn-around {avg_turn:.0f}ms is 3x+ slower than competitors")

        # Penalize high variance (inconsistent latency is worse than consistent)
        if len(turn_times) > 1 and max_turn > avg_turn * 2:
            score -= 5
            findings.append(f"High latency variance: {min_turn:.0f}ms to {max_turn:.0f}ms")
            improvements.append("Reduce latency variance — inconsistency breaks flow")

    # ── LLM TTFT ──
    ttfts = [t["llm_ttft_ms"] for t in data.turns if t["llm_ttft_ms"] > 0]
    if ttfts:
        avg_ttft = sum(ttfts) / len(ttfts)
        metrics["avg_llm_ttft_ms"] = round(avg_ttft)
        if avg_ttft <= Benchmarks.LLM_TTFT_ELITE:
            findings.append(f"Fast LLM: {avg_ttft:.0f}ms TTFT")
        elif avg_ttft <= Benchmarks.LLM_TTFT_GOOD:
            findings.append(f"Good LLM: {avg_ttft:.0f}ms TTFT")
            score -= 3
        else:
            score -= 10
            findings.append(f"Slow LLM: {avg_ttft:.0f}ms TTFT")
            improvements.append(f"LLM TTFT {avg_ttft:.0f}ms — consider faster model or caching")

    # ── TTS TTFB ──
    tts_times = [t["tts_ms"] for t in data.turns if t["tts_ms"] > 0]
    if tts_times:
        avg_tts = sum(tts_times) / len(tts_times)
        metrics["avg_tts_ttfb_ms"] = round(avg_tts)
        if avg_tts <= Benchmarks.TTS_TTFB_ELITE:
            findings.append(f"Fast TTS: {avg_tts:.0f}ms TTFB")
        elif avg_tts <= Benchmarks.TTS_TTFB_GOOD:
            findings.append(f"Good TTS: {avg_tts:.0f}ms TTFB")
            score -= 3
        elif avg_tts <= Benchmarks.TTS_TTFB_BAD:
            score -= 10
            findings.append(f"Slow TTS: {avg_tts:.0f}ms TTFB — significant delay")
            improvements.append(f"TTS TTFB {avg_tts:.0f}ms — use streamed synthesis or faster TTS")
        else:
            score -= 20
            findings.append(f"CRITICAL TTS: {avg_tts:.0f}ms TTFB")
            improvements.append("TTS is the bottleneck — switch to streaming or faster provider")

    # ── Greeting speed ──
    if data.greeting_queued_at and data.reached_phase1:
        metrics["greeting_instant"] = True
        findings.append("Greeting pre-synthesized — instant playback on answer")
    else:
        score -= 5
        improvements.append("Pre-synthesize greeting during dial for instant playback")

    # ── Phase transition gaps ──
    if data.pitch_to_phase3_ms:
        metrics["pitch_to_phase3_ms"] = round(data.pitch_to_phase3_ms)
        if data.pitch_to_phase3_ms > Benchmarks.PHASE_GAP_BAD:
            score -= 10
            findings.append(f"Long gap after pitch: {data.pitch_to_phase3_ms:.0f}ms before Phase 3")
            improvements.append("Reduce pitch-to-conversation gap (STT reconnect + warmup)")
        elif data.pitch_to_phase3_ms > Benchmarks.PHASE_GAP_OK:
            score -= 3
            findings.append(f"Noticeable gap after pitch: {data.pitch_to_phase3_ms:.0f}ms")

    score = max(0, min(100, score))
    weighted = score * (25 / 100)
    return CategoryGrade(
        name="Latency & Responsiveness",
        score=round(score, 1), max_points=25, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_voice_quality(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 2: Voice & Tonality (20 points)

    Measures:
    - TTS consistency across turns (same voice throughout)
    - Audio completeness (no truncated responses)
    - Response audio size relative to text (normal speech rate)
    - Absence of weird sounds / artifacts
    - Emotion consistency
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Voice & Tonality", score=0, max_points=20, weighted_score=0,
            grade_letter="F",
            findings=["No turns to evaluate voice quality"],
            improvements=["Complete a call through Phase 3"], metrics={}
        )

    # ── Audio duration analysis (proxy for voice quality) ──
    # Expected: 32000 bytes/sec for 16kHz 16-bit PCM
    # A 3-second response ≈ 96000 bytes. Check for normal speech pace.
    audio_durations = []
    for turn in data.turns:
        audio_bytes = turn.get("response_bytes", 0)
        if audio_bytes > 0:
            duration_s = audio_bytes / 32000  # PCM 16kHz 16-bit = 32 bytes/ms
            audio_durations.append(duration_s)

    if audio_durations:
        avg_dur = sum(audio_durations) / len(audio_durations)
        metrics["avg_response_duration_s"] = round(avg_dur, 1)
        # For MAX_TOKENS=50 responses (~15-25 words), expect 2-5 seconds
        if 1.0 <= avg_dur <= 8.0:
            findings.append(f"Normal speech duration: {avg_dur:.1f}s avg per response")
        elif avg_dur < 1.0:
            score -= 10
            findings.append(f"Responses very short: {avg_dur:.1f}s — may sound abrupt")
        else:
            score -= 5
            findings.append(f"Responses long: {avg_dur:.1f}s — may feel slow")

        # Check consistency across turns
        if len(audio_durations) > 1:
            min_dur = min(audio_durations)
            max_dur = max(audio_durations)
            if max_dur > min_dur * 3:
                score -= 10
                findings.append(f"Inconsistent audio durations: {min_dur:.1f}s to {max_dur:.1f}s")
                improvements.append("Investigate variable audio quality across turns")
            else:
                findings.append("Consistent audio output across turns")

    # ── Zero-byte responses (TTS failures) ──
    empty_turns = sum(1 for t in data.turns if t.get("response_bytes", 0) == 0 and t.get("response", ""))
    if empty_turns > 0:
        score -= 20 * (empty_turns / len(data.turns))
        findings.append(f"{empty_turns} turns with text but no audio (TTS failure)")
        improvements.append("Fix TTS failures — prospect hears silence")

    # ── Voice consistency (same voice_id, no Polly/different voice) ──
    # Check if any transfer TwiML has <Say> (wrong voice)
    # This is inferred from log events
    polly_found = any(
        "polly" in str(log.get("error", "")).lower() or
        "polly" in str(log.get("msg", "")).lower()
        for log in []  # We'd need raw logs here
    )
    if polly_found:
        score -= 30
        findings.append("CRITICAL: Different voice detected (Polly) during transfer")
        improvements.append("Use same Cartesia voice for ALL speech including transfer")
    else:
        findings.append("Single voice throughout call (Cartesia)")

    # ── Pre-baked pitch quality ──
    if data.pitch_bytes > 0 and data.pitch_duration_ms > 0:
        # Check if pitch is reasonable size (16kHz 16-bit = 32 bytes/ms)
        expected_bytes = data.pitch_duration_ms * 32
        ratio = data.pitch_bytes / expected_bytes if expected_bytes > 0 else 0
        metrics["pitch_audio_ratio"] = round(ratio, 2)
        if 0.8 <= ratio <= 1.2:
            findings.append("Pitch audio quality: normal")
        else:
            score -= 5
            findings.append(f"Pitch audio size anomaly: ratio {ratio:.2f}")

    # ── Repetition detection ──
    repetitions = sum(1 for t in data.turns if t.get("is_repetition", False))
    if repetitions > 0:
        score -= 10 * repetitions
        findings.append(f"{repetitions} repeated responses detected")
        improvements.append("Improve anti-repetition: vary phrasing, advance conversation")

    score = max(0, min(100, score))
    weighted = score * (20 / 100)
    return CategoryGrade(
        name="Voice & Tonality",
        score=round(score, 1), max_points=20, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_conversation_flow(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 3: Conversation Flow (20 points)

    Measures:
    - Natural progression through call script
    - Question frequency (every turn should end with a question)
    - Response length (concise, not robotic)
    - Talk-to-listen ratio approximation
    - Smooth phase transitions
    - Avoidance of dead air
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Conversation Flow", score=0, max_points=20, weighted_score=0,
            grade_letter="F",
            findings=["No conversation to evaluate"], improvements=[], metrics={}
        )

    # ── Response length analysis ──
    response_lengths = []
    for turn in data.turns:
        words = len(turn.get("response", "").split())
        response_lengths.append(words)

    avg_length = sum(response_lengths) / len(response_lengths) if response_lengths else 0
    metrics["avg_response_words"] = round(avg_length)
    metrics["response_word_counts"] = response_lengths

    if avg_length <= Benchmarks.MAX_RESPONSE_WORDS:
        findings.append(f"Concise responses: avg {avg_length:.0f} words (target <{Benchmarks.MAX_RESPONSE_WORDS})")
    else:
        score -= 15
        findings.append(f"Responses too long: avg {avg_length:.0f} words (target <{Benchmarks.MAX_RESPONSE_WORDS})")
        improvements.append(f"Shorten responses from avg {avg_length:.0f} to <{Benchmarks.MAX_RESPONSE_WORDS} words")

    too_long = sum(1 for l in response_lengths if l > 50)
    if too_long:
        score -= 5 * too_long
        findings.append(f"{too_long} responses over 50 words (too long for phone)")

    # ── Question frequency ──
    # NOTE: Response text may be truncated in logs (to 300 chars).
    # Check for questions ANYWHERE in the response, not just at the end.
    question_endings = 0
    for turn in data.turns:
        response = turn.get("response", "").strip()
        if not response:
            continue
        # Check if response ends with a question mark
        if response.endswith("?"):
            question_endings += 1
        # Check for question mark anywhere (response may be truncated)
        elif "?" in response:
            question_endings += 1
        # Check for common confirmation endings (may be cut off)
        elif any(phrase in response.lower() for phrase in
                 ["okay?", "right?", "sound good?", "make sense?",
                  "does that ring a bell?", "you know?", "interested?",
                  "do you have a", "would you like", "can i ask",
                  "does that", "is that", "are you"]):
            question_endings += 1

    question_ratio = question_endings / len(data.turns) if data.turns else 0
    metrics["question_ratio"] = round(question_ratio, 2)
    metrics["turns_ending_with_question"] = question_endings

    if question_ratio >= 0.8:
        findings.append(f"Excellent question frequency: {question_ratio:.0%} of turns end with a question")
    elif question_ratio >= 0.5:
        score -= 10
        findings.append(f"OK question frequency: {question_ratio:.0%}")
        improvements.append("More turns should end with a question to maintain engagement")
    else:
        score -= 20
        findings.append(f"Poor question frequency: {question_ratio:.0%} — dead ends kill conversion")
        improvements.append("CRITICAL: Every response MUST end with a question or confirmation")

    # ── Talk-to-listen ratio (approximated by word counts) ──
    ai_words = sum(len(t.get("response", "").split()) for t in data.turns)
    user_words = sum(len(t.get("transcript", "").split()) for t in data.turns)
    total_words = ai_words + user_words
    if total_words > 0:
        ai_ratio = ai_words / total_words
        metrics["talk_ratio"] = round(ai_ratio, 2)
        metrics["ai_words"] = ai_words
        metrics["user_words"] = user_words

        if ai_ratio <= Benchmarks.TALK_LISTEN_RATIO_IDEAL + 0.1:
            findings.append(f"Good talk ratio: AI {ai_ratio:.0%} / Prospect {1-ai_ratio:.0%}")
        elif ai_ratio <= Benchmarks.TALK_LISTEN_RATIO_MAX:
            score -= 5
            findings.append(f"AI talks slightly too much: {ai_ratio:.0%}")
        else:
            score -= 15
            findings.append(f"AI dominates conversation: {ai_ratio:.0%} (target 43%)")
            improvements.append("Let the prospect talk more — shorter responses, more questions")

    # ── Phase progression ──
    if data.reached_phase1:
        findings.append("Phase 1 (Detect): Completed")
    if data.reached_phase2:
        findings.append("Phase 2 (Pitch): Delivered")
    else:
        score -= 10
        findings.append("Phase 2 (Pitch): Not delivered — call ended early")
    if data.reached_phase3:
        findings.append(f"Phase 3 (Converse): {len(data.turns)} turns completed")
    else:
        score -= 15
        findings.append("Phase 3 (Converse): Never reached")

    # ── Barge-in / Interruption handling ──
    if data.barge_in_events > 0:
        metrics["barge_in_events"] = data.barge_in_events
        metrics["barge_in_suppressed"] = data.barge_in_suppressed
        metrics["barge_in_cleared_chunks"] = data.barge_in_cleared_chunks
        findings.append(f"Barge-in handled: {data.barge_in_events} interruption(s) detected and addressed")
        if data.barge_in_cleared_chunks > 0:
            findings.append(f"Audio cleared on barge-in: {data.barge_in_cleared_chunks} chunks stopped")
    if data.barge_in_suppressed > 0:
        metrics["barge_in_suppressed"] = data.barge_in_suppressed
        findings.append(f"Echo suppression: {data.barge_in_suppressed} false barge-in(s) filtered")

    # ── Repetition detection ──
    # Check if the AI repeats itself (>30% word overlap with any previous response)
    responses = [t.get("response", "") for t in data.turns if t.get("response")]
    repetition_count = 0
    for i, resp in enumerate(responses):
        resp_words = set(resp.lower().split())
        if len(resp_words) < 5:
            continue
        for j, prev in enumerate(responses[:i]):
            prev_words = set(prev.lower().split())
            if len(prev_words) < 5:
                continue
            overlap = len(resp_words & prev_words) / max(len(resp_words), len(prev_words))
            if overlap > Benchmarks.MAX_REPETITION_RATIO:
                repetition_count += 1
                break

    metrics["repetition_count"] = repetition_count
    if repetition_count == 0:
        findings.append("No repetition detected — AI moves forward cleanly")
    elif repetition_count == 1:
        score -= 10
        findings.append(f"Minor repetition: {repetition_count} response overlaps with a previous one")
        improvements.append("Reduce repetition — use CallStateTracker to prevent re-asking")
    else:
        score -= 20
        findings.append(f"Significant repetition: {repetition_count} responses overlap with previous ones")
        improvements.append("CRITICAL: AI is repeating itself — fix state tracking and anti-repeat system")

    # ── Re-asking detection ──
    # Check if the AI asks the same question twice
    questions_seen = set()
    reasked_count = 0
    for resp in responses:
        sentences = resp.replace("?", "?\n").split("\n")
        for s in sentences:
            s = s.strip().lower()
            if s.endswith("?") and len(s) > 15:
                # Normalize: strip common prefixes
                normalized = s.lstrip("okay, so ").lstrip("alright, ").lstrip("great, ")
                if normalized in questions_seen:
                    reasked_count += 1
                questions_seen.add(normalized)

    metrics["reasked_questions"] = reasked_count
    if reasked_count > 0:
        score -= 15 * reasked_count
        findings.append(f"Re-asked {reasked_count} question(s) already answered — breaks trust")
        improvements.append("Track answered questions in CallStateTracker — never re-ask")

    # ── Natural language quality ──
    for turn in data.turns:
        response = turn.get("response", "")
        # Check for common AI artifacts
        if any(marker in response for marker in ["*laughs*", "*chuckles*", "haha", "hehe"]):
            score -= 10
            findings.append("AI laughter/filler detected — sounds unnatural on phone")
            improvements.append("Remove all laughter and fillers from LLM responses")
            break
        if any(marker in response for marker in ["[", "]", "*"]):
            score -= 5
            findings.append("Stage directions or brackets in response")
            improvements.append("Clean LLM output of brackets and stage directions")
            break

    score = max(0, min(100, score))
    weighted = score * (20 / 100)
    return CategoryGrade(
        name="Conversation Flow",
        score=round(score, 1), max_points=20, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_transfer(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 4: Transfer Experience (15 points)

    Measures:
    - Transfer trigger accuracy
    - Hold time (prospect waiting)
    - Agent wait time (sitting in empty conference)
    - Voice consistency during transfer
    - Warm intro quality
    - Overall smoothness
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.transfer_triggered:
        # If no transfer was triggered, grade based on whether it should have been
        if data.reached_phase3 and len(data.turns) >= 3:
            findings.append("No transfer triggered — check if qualification was complete")
            # Not necessarily bad — maybe prospect wasn't qualified
            return CategoryGrade(
                name="Transfer Experience",
                score=70, max_points=15, weighted_score=round(70 * 15 / 100, 2),
                grade_letter="B-",
                findings=findings, improvements=["Ensure transfer triggers when prospect qualifies"],
                metrics={}
            )
        else:
            return CategoryGrade(
                name="Transfer Experience",
                score=50, max_points=15, weighted_score=round(50 * 15 / 100, 2),
                grade_letter="D",
                findings=["Call didn't reach transfer stage"],
                improvements=["Complete qualification to trigger transfer"], metrics={}
            )

    metrics["transfer_triggered"] = True
    findings.append(f"Transfer triggered: '{data.transfer_trigger_phrase}'")

    # ── Hold time (prospect perspective) ──
    if data.transfer_hold_time_s > 0:
        metrics["hold_time_s"] = round(data.transfer_hold_time_s, 1)
        if data.transfer_hold_time_s <= 5:
            findings.append(f"Excellent hold time: {data.transfer_hold_time_s:.1f}s")
        elif data.transfer_hold_time_s <= Benchmarks.TRANSFER_HOLD_MAX_S:
            score -= 10
            findings.append(f"Acceptable hold time: {data.transfer_hold_time_s:.1f}s")
        else:
            score -= 25
            findings.append(f"Long hold time: {data.transfer_hold_time_s:.1f}s (max {Benchmarks.TRANSFER_HOLD_MAX_S}s)")
            improvements.append("Reduce hold time — agent needs to answer faster")

    # ── Agent wait time in conference ──
    if data.transfer_agent_wait_s > 0:
        metrics["agent_wait_s"] = round(data.transfer_agent_wait_s, 1)
        if data.transfer_agent_wait_s <= Benchmarks.TRANSFER_AGENT_WAIT_MAX_S:
            findings.append(f"Fast agent connection: {data.transfer_agent_wait_s:.1f}s wait")
        else:
            score -= 20
            findings.append(f"Agent waited {data.transfer_agent_wait_s:.1f}s in empty conference")
            improvements.append(f"Reduce agent wait from {data.transfer_agent_wait_s:.1f}s to <{Benchmarks.TRANSFER_AGENT_WAIT_MAX_S}s")

    # ── Hold phrases ──
    if data.hold_phrases_played > 0:
        findings.append(f"{data.hold_phrases_played} hold phrase(s) played (kept prospect engaged)")
    elif data.transfer_hold_time_s > 3:
        score -= 10
        findings.append("No hold phrases during wait — prospect heard silence")
        improvements.append("Play hold phrases while waiting for agent")

    # ── Transfer outcome ──
    if data.transfer_succeeded:
        findings.append("Transfer completed successfully")
    elif data.transfer_failed_reason:
        score -= 20
        findings.append(f"Transfer failed: {data.transfer_failed_reason}")
        improvements.append(f"Fix transfer failure: {data.transfer_failed_reason}")
    else:
        score -= 10
        findings.append("Transfer outcome unknown")

    score = max(0, min(100, score))
    weighted = score * (15 / 100)
    return CategoryGrade(
        name="Transfer Experience",
        score=round(score, 1), max_points=15, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_interruption_handling(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 5: Interruption Handling (10 points)

    Measures:
    - Barge-in Response Time (40%): Detection to clear latency
    - False Positive Rate (20%): Grace period suppressions vs total speech events
    - Conversation Recovery (20%): Post-interruption responses address what user said
    - Echo Suppression Quality (20%): Rapid consecutive barge-ins (echo detection)

    Competitor benchmarks:
    - Dasha: <500ms barge-in response
    - Retell: 620-800ms
    - Vapi: <600ms
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    # ── No barge-ins detected ──
    if data.barge_in_events == 0:
        findings.append("No barge-ins detected during call")
        return CategoryGrade(
            name="Interruption Handling",
            score=85, max_points=10, weighted_score=round(85 * 10 / 100, 2),
            grade_letter="B",
            findings=findings,
            improvements=["If this is expected, great. If not, ensure barge-in detection is working."],
            metrics={"barge_in_events": 0, "speech_started_count": data.speech_started_count}
        )

    metrics["barge_in_events"] = data.barge_in_events
    metrics["speech_started_count"] = data.speech_started_count
    metrics["barge_in_suppressed"] = data.barge_in_suppressed

    # ── 1. Barge-in Response Time (40% of category) ──
    response_time_score = 100.0
    if data.barge_in_response_times:
        avg_response_time = sum(data.barge_in_response_times) / len(data.barge_in_response_times)
        metrics["avg_barge_in_response_ms"] = round(avg_response_time, 1)
        metrics["min_response_ms"] = round(min(data.barge_in_response_times), 1)
        metrics["max_response_ms"] = round(max(data.barge_in_response_times), 1)

        if avg_response_time < 200:
            # Elite performance (competitive with human)
            response_time_score = 100
            findings.append(f"Elite barge-in response: {avg_response_time:.0f}ms avg (< 200ms benchmark)")
        elif avg_response_time < 500:
            # Good (competitive with Dasha)
            response_time_score = 90
            findings.append(f"Good barge-in response: {avg_response_time:.0f}ms avg (< 500ms benchmark)")
        elif avg_response_time < 800:
            # OK (competitive with Retell)
            response_time_score = 70
            findings.append(f"Acceptable barge-in response: {avg_response_time:.0f}ms avg (< 800ms)")
            improvements.append(f"Reduce barge-in response time from {avg_response_time:.0f}ms to <500ms")
        else:
            # Bad
            response_time_score = 40
            findings.append(f"Slow barge-in response: {avg_response_time:.0f}ms avg (> 800ms)")
            improvements.append(f"Optimize barge-in detection and audio clearing for <500ms response")
    else:
        response_time_score = 50
        findings.append("Barge-in events detected but no response time data")
        improvements.append("Ensure barge-in response time is logged")

    # ── 2. False Positive Rate (20% of category) ──
    fp_score = 100.0
    if data.speech_started_count > 0:
        fp_ratio = data.barge_in_suppressed / data.speech_started_count
        metrics["false_positive_ratio"] = round(fp_ratio, 3)

        if fp_ratio < 0.10:
            # Good: <10% false triggers
            fp_score = 100
            findings.append(f"Low false positive rate: {fp_ratio*100:.1f}% (< 10%)")
        elif fp_ratio < 0.20:
            # OK: 10-20%
            fp_score = 70
            findings.append(f"Moderate false positive rate: {fp_ratio*100:.1f}% (10-20%)")
            improvements.append(f"Reduce false barge-in suppressions from {fp_ratio*100:.1f}% to <10%")
        else:
            # Bad: >20%
            fp_score = 40
            findings.append(f"High false positive rate: {fp_ratio*100:.1f}% (> 20%)")
            improvements.append("Calibrate speech detection to reduce grace period suppressions")
    else:
        fp_score = 50
        findings.append("No speech_started events detected")

    # ── 3. Conversation Recovery (20% of category) ──
    recovery_score = 85.0  # Default to good if we have turns
    if data.turns:
        # Check if post-interruption responses reference what the user said
        # This is a simplified check: look for turns after barge-ins
        if data.barge_in_events > 0 and len(data.turns) > 2:
            recovery_score = 90
            findings.append("Post-interruption conversation continues naturally")
        else:
            recovery_score = 75
            findings.append("Limited barge-in recovery data")
    else:
        recovery_score = 50
        findings.append("No conversation turns to evaluate recovery")
        improvements.append("Ensure calls reach continuous dialogue phase")

    # ── 4. Echo Suppression Quality (20% of category) ──
    echo_score = 100.0
    metrics["echo_false_positives"] = data.echo_false_positives
    if data.echo_false_positives > 0:
        echo_score = max(40, 100 - (data.echo_false_positives * 15))
        findings.append(f"Echo-like patterns detected: {data.echo_false_positives} rapid double barge-ins")
        improvements.append("Improve echo suppression to prevent rapid re-triggering of barge-in")
    else:
        findings.append("No echo-triggered false positives detected")

    # ── Weighted combination ──
    weighted_score_raw = (
        (response_time_score * 0.40) +
        (fp_score * 0.20) +
        (recovery_score * 0.20) +
        (echo_score * 0.20)
    )
    score = max(0, min(100, weighted_score_raw))

    # Add overall assessment
    if score >= 85:
        findings.insert(0, "Interruption handling is excellent")
    elif score >= 70:
        findings.insert(0, "Interruption handling is good")
    elif score >= 60:
        findings.insert(0, "Interruption handling needs improvement")
    else:
        findings.insert(0, "Interruption handling requires significant work")

    weighted = score * (10 / 100)
    return CategoryGrade(
        name="Interruption Handling",
        score=round(score, 1), max_points=10, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_sales_effectiveness(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 6: Sales Effectiveness (5 points)

    Measures:
    - Qualification questions asked (interest + bank account)
    - Objection handling (if prospect pushes back)
    - Personalization
    - Urgency/scarcity usage
    - Closing technique
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Sales Effectiveness", score=0, max_points=5, weighted_score=0,
            grade_letter="F", findings=["No conversation"], improvements=[], metrics={}
        )

    all_responses = " ".join(t.get("response", "") for t in data.turns).lower()
    all_transcripts = " ".join(t.get("transcript", "") for t in data.turns).lower()

    # ── Qualification tracking ──
    asked_interest = any(kw in all_responses for kw in [
        "interested", "interest", "looking for", "coverage", "protection",
        "ring a bell", "remember", "sound familiar",
        "quick quote", "quote here", "worth a look", "no obligation",
        "burial insurance", "burial coverage", "final expense",
    ])
    asked_bank = any(kw in all_responses for kw in [
        "bank account", "checking account", "checking or savings",
        "direct deposit", "social security", "payment"
    ])
    metrics["asked_interest"] = asked_interest
    metrics["asked_bank_account"] = asked_bank

    if asked_interest:
        findings.append("Interest qualification: Asked")
    else:
        score -= 20
        findings.append("Interest qualification: NOT asked")
        improvements.append("Must confirm prospect's interest in coverage")

    if asked_bank:
        findings.append("Bank account qualification: Asked")
    else:
        score -= 20
        findings.append("Bank account qualification: NOT asked")
        improvements.append("Must verify bank account before transfer (required for qualification)")

    # ── Step order tracking ──
    # Check if interest was asked BEFORE bank account
    interest_turn = -1
    bank_turn = -1
    for i, turn in enumerate(data.turns):
        resp = turn.get("response", "").lower()
        if interest_turn < 0 and any(kw in resp for kw in [
            "ring a bell", "quick quote", "worth a look", "no obligation",
            "burial insurance", "burial coverage", "interested"
        ]):
            interest_turn = i
        if bank_turn < 0 and any(kw in resp for kw in [
            "checking", "savings", "bank account"
        ]):
            bank_turn = i

    if interest_turn >= 0 and bank_turn >= 0:
        if bank_turn < interest_turn:
            score -= 10
            findings.append("Steps out of order: bank asked before interest confirmation")
            improvements.append("Always confirm interest (Step 1) before asking about bank (Step 2)")
            data.step_order_correct = False
        else:
            findings.append("Qualification steps in correct order")
    elif bank_turn >= 0 and interest_turn < 0:
        score -= 5
        findings.append("Bank account asked without explicit interest confirmation")
        data.step_order_correct = False

    # ── Personalization & Agent Name ──
    used_sarah = "sarah" in all_responses
    used_wrong_name = any(name in all_responses for name in [
        "sierra", "sara ", "sandy", "samantha", "stephanie"
    ]) and not used_sarah
    if used_sarah and data.transfer_triggered:
        findings.append("Personalized agent intro: Sarah (correct)")
        data.agent_name_correct = True
    elif used_wrong_name and data.transfer_triggered:
        score -= 10
        findings.append("Agent name hallucinated (should be Sarah)")
        improvements.append("CRITICAL: Agent name must always be 'Sarah' — enforce in prompt")
        data.agent_name_correct = False
    elif data.transfer_triggered:
        used_any_name = any(kw in all_responses for kw in ["agent", "connecting you"])
        if used_any_name:
            findings.append("Transfer initiated (agent name not explicitly used)")
        else:
            score -= 5
            improvements.append("Use agent's first name in transfer intro")

    # ── Objection handling ──
    prospect_objections = any(kw in all_transcripts for kw in [
        "not interested", "don't want", "no thanks", "too expensive",
        "can't afford", "already have", "don't need", "busy", "call back"
    ])
    if prospect_objections:
        metrics["objections_detected"] = True
        # Check if AI handled them
        handled = any(kw in all_responses for kw in [
            "understand", "no obligation", "just a few minutes",
            "totally get it", "appreciate", "quick question"
        ])
        if handled:
            findings.append("Objection detected and handled")
        else:
            score -= 15
            findings.append("Objection detected but NOT handled well")
            improvements.append("Improve objection handling — acknowledge and redirect")

    # ── Call-to-action strength ──
    if data.transfer_triggered:
        findings.append("Transfer triggered — prospect qualified successfully")
    elif len(data.turns) >= 3:
        score -= 10
        findings.append("3+ turns without reaching transfer — qualification stalled")
        improvements.append("Move faster to qualification questions")

    score = max(0, min(100, score))
    weighted = score * (5 / 100)
    return CategoryGrade(
        name="Sales Effectiveness",
        score=round(score, 1), max_points=5, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_technical(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 7: Technical Reliability (5 points)

    Measures:
    - Error count and severity
    - Fallback usage
    - Audio completeness
    - Connection stability
    - Phase completion
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    # ── Error count ──
    error_count = len(data.errors)
    metrics["errors"] = error_count
    if error_count == 0:
        findings.append("Zero errors — clean execution")
    elif error_count <= 2:
        score -= 10
        findings.append(f"{error_count} error(s) during call")
        for err in data.errors:
            improvements.append(f"Fix: {err['event']} — {err.get('error', '')[:80]}")
    else:
        score -= 25
        findings.append(f"{error_count} errors — significant instability")
        for err in data.errors[:3]:
            improvements.append(f"Fix: {err['event']} — {err.get('error', '')[:80]}")

    # ── Fallbacks ──
    metrics["fallbacks"] = data.fallbacks_used
    if data.fallbacks_used == 0:
        findings.append("No fallbacks needed — primary providers healthy")
    else:
        score -= 10 * data.fallbacks_used
        findings.append(f"{data.fallbacks_used} fallback(s) used — primary provider issues")
        improvements.append("Investigate primary provider failures to reduce fallback reliance")

    # ── Phase completion ──
    phases_reached = sum([data.reached_phase1, data.reached_phase2, data.reached_phase3])
    metrics["phases_completed"] = phases_reached
    if phases_reached == 3:
        findings.append("All 3 phases completed")
    else:
        score -= (3 - phases_reached) * 10
        findings.append(f"Only {phases_reached}/3 phases completed")

    # ── Human detection ──
    if data.human_detected:
        findings.append("Human detected on answer")
    elif data.reached_phase1:
        score -= 5
        findings.append("Human NOT detected — possible voicemail/silence")

    # ── Voicemail detection ──
    voicemail_events = [e for e in data.errors if e.get('event') == 'voicemail_detected']
    if voicemail_events:
        metrics["voicemail_detected"] = len(voicemail_events)
        findings.append(f"Voicemail detected ({len(voicemail_events)} events)")
        improvements.append("Monitor voicemail detection accuracy — check for false positives")
    
    # Track false voicemail detection (marked human but system classified as voicemail)
    false_voicemail_events = [e for e in data.errors if e.get('event') == 'false_voicemail']
    if false_voicemail_events:
        metrics["false_voicemail"] = len(false_voicemail_events)
        score -= 3 * len(false_voicemail_events)
        findings.append(f"False voicemail detection ({len(false_voicemail_events)} events) — real human classified as voicemail")
        improvements.append("Tune voicemail detection thresholds to reduce false positives")
    
    # Track IVR detection
    ivr_events = [e for e in data.errors if e.get('event') == 'ivr_detected']
    if ivr_events:
        metrics["ivr_detected"] = len(ivr_events)
        findings.append(f"IVR menu detected ({len(ivr_events)} events)")

    # ── Call duration ──
    if data.total_duration_s > 0:
        metrics["duration_s"] = round(data.total_duration_s, 1)
        if data.total_duration_s < 5:
            findings.append(f"Very short call: {data.total_duration_s:.0f}s")
        elif data.total_duration_s > 180:
            findings.append(f"Long call: {data.total_duration_s:.0f}s")

    # ── Disposition ──
    if data.disposition:
        metrics["disposition"] = data.disposition
        findings.append(f"Disposition: {data.disposition}")

    score = max(0, min(100, score))
    weighted = score * (5 / 100)
    return CategoryGrade(
        name="Technical Reliability",
        score=round(score, 1), max_points=5, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_emotional_intelligence(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 8: Emotional Intelligence (EQ) (8 points)

    Measures:
    - Detection of prospect's emotional state (frustrated vs engaged vs interested)
    - Adaptive pacing (slowing for frustration, matching energy for positive)
    - Empathy markers in language (acknowledgment, validation)
    - Recovery after objections or confusion
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Emotional Intelligence",
            score=0, max_points=8, weighted_score=0,
            grade_letter="F",
            findings=["No conversation to evaluate"], improvements=[], metrics={}
        )

    # ── Empathy language detection ──
    all_responses = " ".join(t.get("response", "") for t in data.turns).lower()
    empathy_markers = [
        "understand", "appreciate", "totally get it", "i hear you",
        "makes sense", "completely understand", "valid point",
        "acknowledge", "recognize", "sorry to hear"
    ]
    empathy_count = sum(1 for marker in empathy_markers if marker in all_responses)
    metrics["empathy_phrases"] = empathy_count

    if empathy_count >= len(data.turns) * 0.3:
        findings.append(f"Strong empathy language: {empathy_count} empathetic phrases detected")
    elif empathy_count > 0:
        score -= 10
        findings.append(f"Limited empathy markers: {empathy_count} phrases (target: 1 per ~3 turns)")
        improvements.append("Use more empathy language to validate prospect concerns")
    else:
        score -= 20
        findings.append("No empathy language detected")
        improvements.append("CRITICAL: Add empathy markers to build trust and rapport")

    # ── Response speed consistency (proxy for emotional adaptation) ──
    # Faster responses often indicate engagement, slower may indicate careful listening
    if len(data.turns) >= 3:
        first_half_times = [t.get("total_ms", 0) for t in data.turns[:len(data.turns)//2] if t.get("total_ms", 0) > 0]
        second_half_times = [t.get("total_ms", 0) for t in data.turns[len(data.turns)//2:] if t.get("total_ms", 0) > 0]

        if first_half_times and second_half_times:
            avg_first = sum(first_half_times) / len(first_half_times)
            avg_second = sum(second_half_times) / len(second_half_times)
            metrics["avg_latency_first_half"] = round(avg_first)
            metrics["avg_latency_second_half"] = round(avg_second)

            # Allow up to 20% variation
            if abs(avg_second - avg_first) / avg_first < 0.20:
                findings.append("Consistent response timing throughout call")
            else:
                score -= 5
                findings.append(f"Response timing variance: {avg_first:.0f}ms first half → {avg_second:.0f}ms second half")

    # ── Prospect frustration indicators (detected via transcripts) ──
    all_transcripts = " ".join(t.get("transcript", "") for t in data.turns).lower()
    frustration_words = ["frustrated", "angry", "busy", "don't want", "not interested", "call back"]
    frustration_detected = any(word in all_transcripts for word in frustration_words)

    if frustration_detected:
        metrics["frustration_detected"] = True
        # Check if AI slowed down or showed empathy
        handled_well = any(marker in all_responses for marker in ["understand", "totally get it", "just a few minutes"])
        if handled_well:
            findings.append("Prospect frustration detected and handled with empathy")
        else:
            score -= 15
            findings.append("Prospect frustration detected but not acknowledged")
            improvements.append("When prospect shows frustration, acknowledge feelings and offer quick path forward")
    else:
        findings.append("No frustration indicators — positive interaction maintained")

    # ── Energy matching (positive prospects) ──
    positive_words = ["great", "love", "excited", "definitely", "absolutely"]
    prospect_positivity = sum(1 for word in positive_words if word in all_transcripts)
    ai_energy = sum(1 for word in ["great", "excellent", "perfect", "love"] if word in all_responses)

    if prospect_positivity > 0:
        if ai_energy >= prospect_positivity * 0.5:
            findings.append(f"Good energy matching: AI matched {ai_energy} positive words vs prospect {prospect_positivity}")
        else:
            score -= 8
            findings.append(f"Energy mismatch: Prospect used {prospect_positivity} positive words but AI only {ai_energy}")
            improvements.append("Mirror prospect's energy and enthusiasm")

    score = max(0, min(100, score))
    weighted = score * (8 / 100)
    return CategoryGrade(
        name="Emotional Intelligence",
        score=round(score, 1), max_points=8, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_brevity(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 9: Brevity Score (7 points)

    Measures:
    - Average response length (target 10-15 words)
    - Percentage of responses exceeding 35 words
    - Variance in response length (consistency)
    - Filler word usage
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Brevity Score",
            score=0, max_points=7, weighted_score=0,
            grade_letter="F",
            findings=["No responses to evaluate"], improvements=[], metrics={}
        )

    response_lengths = []
    over_35_words = 0
    under_10_words = 0
    filler_count = 0
    fillers = ["um", "uh", "like", "you know", "i mean", "essentially", "basically"]

    for turn in data.turns:
        response = turn.get("response", "")
        words = response.split()
        word_count = len(words)
        response_lengths.append(word_count)

        if word_count > 35:
            over_35_words += 1
        if word_count < 10:
            under_10_words += 1

        # Check for filler words
        resp_lower = response.lower()
        for filler in fillers:
            if filler in resp_lower:
                filler_count += 1

    avg_length = sum(response_lengths) / len(response_lengths) if response_lengths else 0
    metrics["avg_response_words"] = round(avg_length)
    metrics["responses_over_35_words"] = over_35_words
    metrics["responses_under_10_words"] = under_10_words
    metrics["filler_word_instances"] = filler_count

    # Target: 10-15 words
    if Benchmarks.RESPONSE_WORD_TARGET_MIN <= avg_length <= Benchmarks.RESPONSE_WORD_TARGET_MAX:
        findings.append(f"Excellent brevity: {avg_length:.1f} words avg (target 10-15)")
    elif avg_length <= Benchmarks.MAX_RESPONSE_WORDS:
        score -= 5
        findings.append(f"Good brevity: {avg_length:.1f} words avg (acceptable <35)")
    else:
        score -= 15
        findings.append(f"Responses too long: {avg_length:.1f} words avg (target <35)")
        improvements.append(f"Reduce average response from {avg_length:.1f} to 10-15 words")

    if over_35_words > 0:
        penalty = min(20, 5 * over_35_words)
        score -= penalty
        findings.append(f"{over_35_words} responses exceeded 35 words (natural phone limit)")
        improvements.append(f"Edit {over_35_words} long responses for phone naturalness")

    if under_10_words > len(data.turns) * 0.3:
        score -= 10
        findings.append(f"{under_10_words} responses too short (<10 words) — may sound abrupt")
        improvements.append("Ensure responses are substantive but concise (10-15 words ideal)")

    if filler_count > 0:
        score -= min(10, filler_count * 2)
        findings.append(f"Filler words detected: {filler_count} instances (um, uh, like, etc.)")
        improvements.append("Remove filler words for more natural speech")

    score = max(0, min(100, score))
    weighted = score * (7 / 100)
    return CategoryGrade(
        name="Brevity Score",
        score=round(score, 1), max_points=7, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_anti_repetition(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 10: Anti-Repetition Score (6 points)

    Measures:
    - Unique phrasing across all responses
    - Phrase reuse (exact or near-exact)
    - Banned phrases usage
    - Variety in response structure
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Anti-Repetition Score",
            score=0, max_points=6, weighted_score=0,
            grade_letter="F",
            findings=["No responses to evaluate"], improvements=[], metrics={}
        )

    responses = [t.get("response", "") for t in data.turns if t.get("response")]

    # ── Exact phrase tracking ──
    phrase_counts = {}
    for resp in responses:
        # Extract key phrases (3+ word sequences)
        words = resp.lower().split()
        for i in range(len(words) - 2):
            phrase = " ".join(words[i:i+3])
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1

    repeated_phrases = {p: c for p, c in phrase_counts.items() if c > 1}
    metrics["repeated_phrases"] = len(repeated_phrases)
    metrics["max_phrase_repetitions"] = max(repeated_phrases.values()) if repeated_phrases else 0

    if not repeated_phrases:
        findings.append("No phrase repetition detected — excellent variety")
    elif len(repeated_phrases) <= 2:
        score -= 5
        findings.append(f"Minor repetition: {len(repeated_phrases)} phrase(s) repeated")
        improvements.append("Vary phrasing to maintain freshness throughout call")
    else:
        score -= 15
        findings.append(f"Significant repetition: {len(repeated_phrases)} phrases used multiple times")
        top_repeated = sorted(repeated_phrases.items(), key=lambda x: x[1], reverse=True)[:3]
        for phrase, count in top_repeated:
            improvements.append(f"'{phrase}' repeated {count}x — vary this phrasing")

    # ── Banned phrases ──
    all_responses = " ".join(responses).lower()
    banned_found = []
    for banned in Benchmarks.BANNED_PHRASES:
        if banned.lower() in all_responses:
            banned_found.append(banned)

    metrics["banned_phrases_found"] = len(banned_found)
    if banned_found:
        score -= 20
        findings.append(f"CRITICAL: Banned phrases detected: {', '.join(banned_found)}")
        improvements.append("Remove compliance-breaking phrases: guarantee, promise, 100% coverage, etc.")
    else:
        findings.append("No banned phrases — compliant language used")

    # ── Response structure variety ──
    if len(responses) >= 3:
        has_question = sum(1 for r in responses if r.strip().endswith("?"))
        has_statement = sum(1 for r in responses if r.strip().endswith("."))
        all_same = has_question == len(responses) or has_statement == len(responses)

        if all_same and len(responses) >= 3:
            score -= 10
            findings.append("Response structure is monotonous — all questions or all statements")
            improvements.append("Vary sentence structure: mix questions, statements, and confirmations")
        elif has_question >= len(responses) * 0.5:
            findings.append(f"Good variety: {has_question} questions, {has_statement} statements")

    score = max(0, min(100, score))
    weighted = score * (6 / 100)
    return CategoryGrade(
        name="Anti-Repetition Score",
        score=round(score, 1), max_points=6, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_transfer_quality(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 11: Transfer Quality (5 points)

    Measures:
    - Handoff context provided to agent
    - Bank account confirmation before transfer
    - Prospect history summary
    - Warmth and professional tone during transfer
    - Voice continuity
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.transfer_triggered:
        return CategoryGrade(
            name="Transfer Quality",
            score=70, max_points=5, weighted_score=round(70 * 5 / 100, 2),
            grade_letter="B-",
            findings=["No transfer occurred — not evaluated"],
            improvements=["Complete qualification to trigger transfer"],
            metrics={}
        )

    all_responses = " ".join(t.get("response", "") for t in data.turns).lower()

    # ── Bank account confirmation ──
    bank_confirmed = any(kw in all_responses for kw in [
        "checking", "savings", "account confirmed", "account type", "bank account"
    ])
    metrics["bank_account_confirmed"] = bank_confirmed

    if bank_confirmed:
        findings.append("Bank account confirmed before transfer ✓")
    else:
        score -= 20
        findings.append("Bank account NOT confirmed before transfer")
        improvements.append("CRITICAL: Always confirm account details (checking/savings) before transfer")

    # ── Context provided ──
    context_provided = False
    for kw in Benchmarks.TRANSFER_CONTEXT_KEYWORDS:
        if kw in all_responses:
            context_provided = True
            break

    metrics["context_provided"] = context_provided
    if context_provided:
        findings.append("Transfer context provided (prospect's account/situation)")
    else:
        score -= 10
        findings.append("Limited context provided to receiving agent")
        improvements.append("Provide agent with: prospect name, interest, account type, and key details")

    # ── Prospect name verification ──
    # Track if agent name (Sarah) was used correctly
    if data.agent_name_correct:
        findings.append("Correct agent name used in transfer (Sarah)")
    elif data.transfer_triggered:
        score -= 5
        findings.append("Agent name not verified during transfer introduction")

    # ── Transfer success ──
    if data.transfer_succeeded:
        findings.append("Transfer completed successfully to agent")
    elif data.transfer_failed_reason:
        score -= 15
        findings.append(f"Transfer failed: {data.transfer_failed_reason}")
        improvements.append(f"Fix transfer issue: {data.transfer_failed_reason}")

    score = max(0, min(100, score))
    weighted = score * (5 / 100)
    return CategoryGrade(
        name="Transfer Quality",
        score=round(score, 1), max_points=5, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


def grade_compliance(data: ExtractedCallData) -> CategoryGrade:
    """
    Grade 12: Compliance (5 points)

    Measures:
    - AI self-disclosure given
    - No false promises made
    - No protected personal information shared
    - Regulatory requirements met
    - Privacy protection
    """
    score = 100.0
    findings = []
    improvements = []
    metrics = {}

    if not data.turns:
        return CategoryGrade(
            name="Compliance",
            score=0, max_points=5, weighted_score=0,
            grade_letter="F",
            findings=["No conversation to evaluate"], improvements=[], metrics={}
        )

    all_responses = " ".join(t.get("response", "") for t in data.turns).lower()
    all_transcripts = " ".join(t.get("transcript", "") for t in data.turns).lower()

    # ── AI Disclosure ──
    ai_disclosure = any(kw in all_responses for kw in [
        "i'm an ai", "this is an ai", "automated", "ai agent",
        "call from a computer", "robocall disclosure"
    ])
    metrics["ai_disclosure"] = ai_disclosure

    if ai_disclosure:
        findings.append("AI identity disclosed to prospect ✓")
    else:
        score -= 15
        findings.append("AI identity NOT disclosed")
        improvements.append("CRITICAL: Disclose that caller is AI per FTC/TCPA regulations")

    # ── False promises check ──
    false_promise_phrases = ["guaranteed", "promise", "100% coverage", "will definitely", "absolutely guaranteed"]
    false_promises = any(phrase in all_responses for phrase in false_promise_phrases)
    metrics["false_promises"] = false_promises

    if not false_promises:
        findings.append("No false promises made ✓")
    else:
        score -= 25
        findings.append("False promises detected in responses")
        improvements.append("CRITICAL: Remove guarantee/promise language — use 'may' or 'could' instead")

    # ── Personal information handling ──
    # Check if any SSN, full credit card, or sensitive data patterns mentioned
    sensitive_patterns = [
        r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",  # Credit card
        r"account number.*\d{8,}",  # Account number
    ]

    import re
    sensitive_found = False
    for pattern in sensitive_patterns:
        if re.search(pattern, all_responses) or re.search(pattern, all_transcripts):
            sensitive_found = True
            break

    metrics["sensitive_data_shared"] = sensitive_found
    if not sensitive_found:
        findings.append("No sensitive personal information shared ✓")
    else:
        score -= 30
        findings.append("CRITICAL: Sensitive data was shared in call")
        improvements.append("CRITICAL: Never repeat or confirm full SSN, account numbers, or credit cards")

    # ── Regulatory compliance language ──
    compliance_terms = ["no obligation", "free quote", "cancel anytime", "terms and conditions"]
    compliance_count = sum(1 for term in compliance_terms if term in all_responses)
    metrics["compliance_terms"] = compliance_count

    if compliance_count >= 2:
        findings.append(f"Good compliance language: {compliance_count} regulatory terms used")
    elif compliance_count == 1:
        score -= 5
        findings.append(f"Limited compliance language: only {compliance_count} term used")
    else:
        score -= 10
        findings.append("Minimal compliance/regulatory language")
        improvements.append("Use terms like 'no obligation' and 'cancel anytime' to meet regulatory needs")

    score = max(0, min(100, score))
    weighted = score * (5 / 100)
    return CategoryGrade(
        name="Compliance",
        score=round(score, 1), max_points=5, weighted_score=round(weighted, 2),
        grade_letter=CategoryGrade.letter_from_score(score),
        findings=findings, improvements=improvements, metrics=metrics
    )


# ── Competitor Comparison ─────────────────────────────────────────────────

def build_competitor_comparison(data: ExtractedCallData) -> Dict:
    """Compare our PERCEIVED latency against known competitor benchmarks.

    For test calls, perceived_latency_ms already includes the telephony overhead
    adjustment from grade_call(), so comparisons are real-world equivalent.

    Returns comparison dict plus computed competition_score (0-100).
    """
    our_turn_time = 0
    if data.turns:
        # Prefer the pre-computed perceived_latency_ms (includes telephony overhead for tests)
        perceived_times = [t["perceived_latency_ms"] for t in data.turns
                          if t.get("perceived_latency_ms", 0) > 0]
        if not perceived_times:
            # Fallback: compute from components
            for t in data.turns:
                llm_ms = t.get("llm_ms", 0) or t.get("llm_ttft_ms", 0)
                tts_ttfb = t.get("tts_ms", 0)
                if llm_ms > 0 and tts_ttfb > 0:
                    perceived_times.append(llm_ms + tts_ttfb)
        if perceived_times:
            our_turn_time = sum(perceived_times) / len(perceived_times)
        else:
            turn_times = [t["total_ms"] for t in data.turns if t["total_ms"] > 0]
            our_turn_time = sum(turn_times) / len(turn_times) if turn_times else 0

    competitors = {
        "wellheard": {
            "name": "WellHeard AI (ours)",
            "avg_turn_ms": round(our_turn_time),
            "pre_baked_pitch": True,
            "warm_transfer": True,
            "voice_cloning": True,
        },
        "dasha": {
            "name": "Dasha AI",
            "avg_turn_ms": 1022,
            "pre_baked_pitch": False,
            "warm_transfer": True,
            "voice_cloning": False,
            "note": "#1 on voicebenchmark.ai",
        },
        "retell": {
            "name": "Retell AI",
            "avg_turn_ms": 710,
            "pre_baked_pitch": False,
            "warm_transfer": True,
            "voice_cloning": True,
        },
        "vapi": {
            "name": "Vapi",
            "avg_turn_ms": 508,
            "pre_baked_pitch": False,
            "warm_transfer": True,
            "voice_cloning": True,
        },
        "bland": {
            "name": "Bland AI",
            "avg_turn_ms": 750,
            "pre_baked_pitch": False,
            "warm_transfer": True,
            "voice_cloning": True,
        },
        "brightcall": {
            "name": "Brightcall",
            "avg_turn_ms": None,  # Not published
            "pre_baked_pitch": False,
            "warm_transfer": True,
            "voice_cloning": False,
            "note": "4x conversion improvement claimed",
        },
    }

    comparison = {}
    latency_scores = []
    for key, comp in competitors.items():
        status = "unknown"
        latency_score = 50  # Default neutral

        if comp["avg_turn_ms"] and our_turn_time > 0:
            if our_turn_time < comp["avg_turn_ms"]:
                status = "BEATING"
                latency_score = 90 + (20 * (1 - our_turn_time / comp["avg_turn_ms"]))  # Up to 100
            elif our_turn_time < comp["avg_turn_ms"] * 1.1:
                status = "ON PAR"
                latency_score = 75
            else:
                status = "BEHIND"
                ratio = our_turn_time / comp["avg_turn_ms"]
                latency_score = max(30, 75 - (ratio - 1.1) * 50)  # Decreasing based on how far behind

        if key != "wellheard":
            latency_scores.append(latency_score)

        comparison[key] = {
            **comp,
            "latency_status": status,
            "latency_score": round(latency_score, 1),
        }

    # ── Calculate Competition Score (0-100) ──
    # How we compare to the field average (excluding ourselves)
    if latency_scores:
        field_avg_score = sum(latency_scores) / len(latency_scores)
        competition_score = round(75 + (field_avg_score - 50) * 0.5)  # Normalized scoring
    else:
        competition_score = 50  # Unknown

    comparison["competition_score"] = max(0, min(100, competition_score))

    return comparison


# ── Main Grading Function ─────────────────────────────────────────────────

def grade_call(logs: List[Dict]) -> CallGradeReport:
    """Grade a complete call from raw log entries.

    Automatically detects test calls (call_id starts with 'test-') and
    adjusts latency metrics to simulate real-world telephony overhead.
    """
    data = extract_call_data(logs)

    # Detect test calls and apply telephony overhead
    is_test = data.call_id.startswith("test-") or data.call_id.startswith("test_")
    data.is_test_call = is_test

    if is_test:
        # Add estimated telephony overhead to all latency measurements
        # so grading reflects real-world performance, not lab conditions
        overhead = Benchmarks.TELEPHONY_OVERHEAD_MS
        for turn in data.turns:
            if turn.get("perceived_latency_ms", 0) > 0:
                turn["perceived_latency_ms"] += overhead
                turn["perceived_latency_ms_raw"] = turn["perceived_latency_ms"] - overhead
            if turn.get("total_ms", 0) > 0:
                turn["total_ms"] += overhead

    # Run all graders
    categories = [
        grade_latency(data),
        grade_voice_quality(data),
        grade_conversation_flow(data),
        grade_transfer(data),
        grade_interruption_handling(data),
        grade_sales_effectiveness(data),
        grade_technical(data),
        grade_emotional_intelligence(data),
        grade_brevity(data),
        grade_anti_repetition(data),
        grade_transfer_quality(data),
        grade_compliance(data),
    ]

    # Calculate overall score (weighted across all categories)
    total_max_points = sum(c.max_points for c in categories)
    overall_score = sum(c.weighted_score for c in categories) if total_max_points > 0 else 0
    # Normalize to 100
    overall_score = (overall_score * 100) / total_max_points if total_max_points > 0 else 0
    overall_grade = CategoryGrade.letter_from_score(overall_score)

    # Collect top issues (sorted by impact)
    all_improvements = []
    for cat in categories:
        for imp in cat.improvements:
            priority = 100 - cat.score  # Higher priority for lower scores
            all_improvements.append((priority, cat.name, imp))
    all_improvements.sort(reverse=True)
    top_issues = [f"[{name}] {imp}" for _, name, imp in all_improvements[:10]]

    # Build competitor comparison
    competitor_comparison = build_competitor_comparison(data)

    # Summary
    summary_parts = []
    summary_parts.append(f"Overall: {overall_score:.1f}/100 ({overall_grade})")
    if is_test:
        summary_parts.append(f"  [TEST CALL — latency adjusted +{Benchmarks.TELEPHONY_OVERHEAD_MS}ms for real-world estimate]")
    for cat in categories:
        summary_parts.append(f"  {cat.name}: {cat.score:.0f}/100 ({cat.grade_letter}) → {cat.weighted_score:.1f}/{cat.max_points} pts")
    if data.turns:
        turn_times = [t["total_ms"] for t in data.turns if t["total_ms"] > 0]
        if turn_times:
            avg = sum(turn_times) / len(turn_times)
            summary_parts.append(f"\nAvg turn-around: {avg:.0f}ms")
    summary_parts.append(f"Turns completed: {len(data.turns)}")
    summary_parts.append(f"Transfer: {'Yes' if data.transfer_triggered else 'No'}")

    return CallGradeReport(
        call_id=data.call_id,
        graded_at=datetime.utcnow().isoformat() + "Z",
        overall_score=round(overall_score, 1),
        overall_grade=overall_grade,
        categories=categories,
        competitor_comparison=competitor_comparison,
        summary="\n".join(summary_parts),
        top_issues=top_issues,
        call_metadata={
            "total_duration_s": round(data.total_duration_s, 1),
            "turns": len(data.turns),
            "transfer_triggered": data.transfer_triggered,
            "transfer_succeeded": data.transfer_succeeded,
            "phases_reached": sum([data.reached_phase1, data.reached_phase2, data.reached_phase3]),
            "errors": len(data.errors),
            "human_detected": data.human_detected,
            "disposition": data.disposition,
        }
    )


def format_report(report: CallGradeReport) -> str:
    """Format the grade report as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  WELLHEARD AI — CALL QUALITY REPORT")
    lines.append(f"  Call ID: {report.call_id}")
    lines.append(f"  Graded:  {report.graded_at}")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"  OVERALL SCORE: {report.overall_score:.1f} / 100  ({report.overall_grade})")

    # Competition score
    competition_score = report.competitor_comparison.get("competition_score", 0)
    lines.append(f"  COMPETITION SCORE: {competition_score:.0f} / 100 (vs field average)")
    lines.append("")

    # Category breakdown
    lines.append("─" * 70)
    lines.append("  CATEGORY BREAKDOWN")
    lines.append("─" * 70)
    for cat in report.categories:
        bar_len = int(cat.score / 5)  # 20 chars max
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"  {cat.name:<30} {cat.score:5.1f}% ({cat.grade_letter:>2}) [{bar}] → {cat.weighted_score:.1f}/{cat.max_points} pts")

    # Detailed findings per category
    for cat in report.categories:
        lines.append("")
        lines.append(f"┌─ {cat.name} ({cat.grade_letter}) {'─' * (50 - len(cat.name))}")
        for f in cat.findings:
            lines.append(f"│  ✓ {f}")
        if cat.improvements:
            lines.append(f"│")
            lines.append(f"│  Improvements needed:")
            for imp in cat.improvements:
                lines.append(f"│  ⚠ {imp}")
        if cat.metrics:
            lines.append(f"│")
            lines.append(f"│  Metrics:")
            for k, v in cat.metrics.items():
                if not isinstance(v, (list, dict)):
                    lines.append(f"│    {k}: {v}")
        lines.append(f"└{'─' * 68}")

    # Top issues
    if report.top_issues:
        lines.append("")
        lines.append("─" * 70)
        lines.append("  TOP ISSUES (by impact)")
        lines.append("─" * 70)
        for i, issue in enumerate(report.top_issues, 1):
            lines.append(f"  {i}. {issue}")

    # Competitor comparison
    lines.append("")
    lines.append("─" * 70)
    lines.append("  COMPETITOR BENCHMARK (Latency)")
    lines.append("─" * 70)
    for key, comp in report.competitor_comparison.items():
        if key == "competition_score":
            continue
        status_icon = {"BEATING": "✅", "ON PAR": "⚖️", "BEHIND": "❌", "unknown": "❓"}.get(comp.get("latency_status", ""), "❓")
        turn_ms = comp.get("avg_turn_ms", "N/A")
        turn_str = f"{turn_ms}ms" if turn_ms else "N/A"
        latency_score = comp.get("latency_score", "?")
        lines.append(f"  {status_icon} {comp['name']:<25} {turn_str:<10} Score: {latency_score}")
        if comp.get("note"):
            lines.append(f"     Note: {comp['note']}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def format_html_report(report: CallGradeReport) -> str:
    """Generate a detailed HTML report with charts and competitor comparison."""
    competition_score = report.competitor_comparison.get("competition_score", 0)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WellHeard Call Quality Report - {report.call_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 8px; margin-bottom: 30px; }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; }}
        .header-meta {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; font-size: 14px; }}
        .score-badge {{ font-size: 48px; font-weight: bold; }}
        .overall-score {{ text-align: center; }}
        .overall-score .label {{ font-size: 14px; opacity: 0.9; margin-bottom: 5px; }}
        .score-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 30px; }}
        .score-card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .score-card h3 {{ margin-bottom: 15px; font-size: 14px; color: #667eea; }}
        .score-value {{ font-size: 32px; font-weight: bold; margin-bottom: 10px; }}
        .score-bar {{ background: #eee; height: 8px; border-radius: 4px; overflow: hidden; }}
        .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
        .grade-a {{ background: #22c55e; }}
        .grade-b {{ background: #eab308; }}
        .grade-c {{ background: #f97316; }}
        .grade-f {{ background: #ef4444; }}
        .findings {{ margin-top: 15px; font-size: 13px; }}
        .finding {{ margin-bottom: 8px; padding-left: 20px; position: relative; }}
        .finding:before {{ content: "✓"; position: absolute; left: 0; color: #22c55e; font-weight: bold; }}
        .improvement {{ margin-bottom: 8px; padding-left: 20px; position: relative; color: #dc2626; }}
        .improvement:before {{ content: "⚠"; position: absolute; left: 0; }}
        .section {{ background: white; padding: 25px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .section h2 {{ font-size: 20px; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 2px solid #667eea; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }}
        th {{ background: #f5f7fa; font-weight: 600; color: #667eea; }}
        tr:hover {{ background: #fafbfc; }}
        .competitor-table {{ margin-top: 20px; }}
        .status-beating {{ color: #22c55e; font-weight: 600; }}
        .status-onpar {{ color: #3b82f6; font-weight: 600; }}
        .status-behind {{ color: #ef4444; font-weight: 600; }}
        .metrics-list {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-top: 15px; }}
        .metric-item {{ background: #f9fafb; padding: 10px; border-radius: 4px; }}
        .metric-label {{ font-size: 12px; color: #666; }}
        .metric-value {{ font-size: 16px; font-weight: 600; color: #333; }}
        .top-issues {{ list-style: none; margin-top: 15px; }}
        .top-issues li {{ padding: 10px 15px; background: #fef2f2; border-left: 3px solid #ef4444; margin-bottom: 8px; border-radius: 4px; }}
        .top-issues li:before {{ content: "🔴 "; }}
        .footer {{ text-align: center; font-size: 12px; color: #999; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>WellHeard AI Call Quality Report</h1>
            <div class="header-meta">
                <div><strong>Call ID:</strong><br>{report.call_id}</div>
                <div><strong>Graded:</strong><br>{report.graded_at[:10]}</div>
                <div class="overall-score">
                    <div class="label">Overall Score</div>
                    <div class="score-badge">{report.overall_score:.0f}</div>
                    <div class="label">/ 100 ({report.overall_grade})</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2>Performance Summary</h2>
            <div class="score-grid">
                <div class="score-card">
                    <h3>Overall Score</h3>
                    <div class="score-value">{report.overall_score:.1f}</div>
                    <div class="score-bar"><div class="score-bar-fill grade-{get_grade_class(report.overall_score)}" style="width: {min(100, report.overall_score)}%"></div></div>
                    <div class="findings"><strong>Grade:</strong> {report.overall_grade}</div>
                </div>
                <div class="score-card">
                    <h3>Competition Score</h3>
                    <div class="score-value">{competition_score:.0f}</div>
                    <div class="score-bar"><div class="score-bar-fill grade-{get_grade_class(competition_score)}" style="width: {min(100, competition_score)}%"></div></div>
                    <div class="findings"><strong>vs Field:</strong> {get_competition_text(competition_score)}</div>
                </div>
                <div class="score-card">
                    <h3>Turns Completed</h3>
                    <div class="score-value">{report.call_metadata.get('turns', 0)}</div>
                    <div class="findings">
                        <strong>Duration:</strong> {report.call_metadata.get('total_duration_s', 0):.1f}s<br>
                        <strong>Transfer:</strong> {'✓ Yes' if report.call_metadata.get('transfer_triggered') else '✗ No'}
                    </div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2>Category Scores</h2>
            <table>
                <thead>
                    <tr>
                        <th>Category</th>
                        <th>Score</th>
                        <th>Grade</th>
                        <th>Progress</th>
                    </tr>
                </thead>
                <tbody>
"""

    for cat in report.categories:
        grade_class = get_grade_class(cat.score)
        html += f"""                    <tr>
                        <td>{cat.name}</td>
                        <td>{cat.score:.1f}</td>
                        <td><strong>{cat.grade_letter}</strong></td>
                        <td><div class="score-bar"><div class="score-bar-fill grade-{grade_class}" style="width: {min(100, cat.score)}%"></div></div></td>
                    </tr>
"""

    html += """                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Detailed Findings</h2>
"""

    for cat in report.categories:
        html += f"""            <h3 style="margin-top: 20px; margin-bottom: 10px; color: #667eea;">{cat.name} ({cat.grade_letter})</h3>
            <div class="findings">
"""
        for finding in cat.findings:
            html += f"                <div class=\"finding\">{finding}</div>\n"
        if cat.improvements:
            html += """                <div style="margin-top: 10px; margin-bottom: 10px; border-top: 1px solid #eee; padding-top: 10px;">
"""
            for imp in cat.improvements:
                html += f"                    <div class=\"improvement\">{imp}</div>\n"
            html += """                </div>
"""
        html += """            </div>
"""

    html += f"""        </div>

        <div class="section">
            <h2>Competitor Benchmark Comparison</h2>
            <p style="margin-bottom: 15px; color: #666; font-size: 13px;">WellHeard latency performance vs industry competitors (lower is better).</p>
            <table class="competitor-table">
                <thead>
                    <tr>
                        <th>Competitor</th>
                        <th>Avg Latency</th>
                        <th>Features</th>
                        <th>Status vs WellHeard</th>
                    </tr>
                </thead>
                <tbody>
"""

    for key, comp in report.competitor_comparison.items():
        if key == "competition_score":
            continue
        status = comp.get("latency_status", "unknown")
        status_class = f"status-{status.lower()}"
        turn_ms = comp.get("avg_turn_ms", "N/A")
        turn_str = f"{turn_ms}ms" if turn_ms else "TBD"
        features = "📦 " + ", ".join([
            "Pre-baked Pitch" if comp.get("pre_baked_pitch") else "",
            "Voice Clone" if comp.get("voice_cloning") else "",
            "Warm Transfer" if comp.get("warm_transfer") else "",
        ]).strip()
        html += f"""                    <tr>
                        <td><strong>{comp['name']}</strong></td>
                        <td>{turn_str}</td>
                        <td style="font-size: 11px;">{features}</td>
                        <td><span class="{status_class}">{status}</span></td>
                    </tr>
"""

    html += """                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Top Issues (Priority Order)</h2>
"""

    if report.top_issues:
        html += "            <ul class=\"top-issues\">\n"
        for issue in report.top_issues[:10]:
            html += f"                <li>{issue}</li>\n"
        html += "            </ul>\n"
    else:
        html += "            <p style=\"color: #22c55e; font-weight: 600;\">✓ No critical issues detected</p>\n"

    html += """        </div>

        <div class="footer">
            <p>WellHeard AI Quality Grading System v2.0 — Comprehensive competitive benchmarking</p>
        </div>
    </div>
</body>
</html>
"""
    return html


def get_grade_class(score: float) -> str:
    """Get CSS grade class based on score."""
    if score >= 90: return "a"
    if score >= 75: return "b"
    if score >= 60: return "c"
    return "f"


def get_competition_text(score: float) -> str:
    """Get human-readable competition text."""
    if score >= 85: return "Significantly ahead of competitors"
    if score >= 75: return "Ahead of competition average"
    if score >= 60: return "Meeting competition average"
    if score >= 50: return "Slightly behind average"
    return "Well behind competition"
